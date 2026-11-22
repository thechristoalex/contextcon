# Architecture

Payables Desk is one Python process (stdlib only) over one SQLite file. Every external effect is either
idempotent by key or cached by request hash. A `kill -9` at any instruction therefore loses no work and
never repeats a paid API call or a payment.

```
                 ┌──────────────────────────── vendorcheck/ ────────────────────────────┐
 .txt/.eml ─────►│ ap.py        invoice state machine, checks, approve → pay, resume     │
 bill photo ────►│ scan.py      photo → OCR lines → rows → inbox text  ─┐                │
                 │ bill.py      GST invoice parser over OCR geometry  ◄─┘                │
 bank CSV ──────►│ bank.py      statement line → payee → ledger account / reconcile      │
 vendor req ────►│ pipeline.py  vendor trust job (5.1 + 5.2), checkpointed steps         │
                 │ jev.py       typed-decision client (TypeSafe Jev), redaction, quoting │
                 │ crust.py     Crustdata client: cache, budget, retry, cost ledger      │
                 │ store.py     SQLite (WAL): jobs, steps, http_cache, ledger, invoices, │
                 │              payments, actions, files_seen, events, bank_lines        │
                 │ procure/     onboarding → uploaded invoices → bills → payments made   │
                 │              (state machines, domain events, idempotency keys)        │
                 │ server.py    ThreadingHTTPServer: JSON API + ui/landing/portal pages  │
                 │ cli.py       argparse front end over the same functions               │
                 └──────────────────────────────────────────────────────────────────────┘
                        │                         │                         │
              api.crustdata.com          api.typesafe.ai/v1/systemone   Apple Vision / Tesseract
              (identify, enrich,         (yes/no, choice, score;        (local subprocess,
               web fetch, web search)     never a source)                no network)
```

| Module | LOC | Responsibility |
|---|---:|---|
| `ap.py` | 636 | Extraction, vendor identification, 9 invoice checks, scoring, `approve`/`pay`/`resume`/`recheck` |
| `pipeline.py` | 629 | `Job`: entity resolution, profile, website, registry, GSTIN lookup, adverse media, verdict |
| `bill.py` | 1001 | De-skew, row/column reconstruction, labelled-field extraction, arithmetic self-checks, Jev tie-break |
| `scan.py` | 370 | Photo storage by content hash, OCR backend selection, OCR-error repair, provenance attach |
| `store.py` | 263 | Schema, connection, checkpoint/cache/ledger/action primitives |
| `bank.py` | 211 | Descriptor normalisation, payee resolution, legal-name extraction, reconciliation |
| `server.py` | 210 | HTTP routing, single-writer lock, startup recovery |
| `jev.py` | 177 | Typed-question client, redaction, threshold mapping, sentence-id citations |
| `crust.py` | 112 | HTTP client with hash cache, credit budget and backoff |
| `procure/onboarding.py` | 350 | Vendor portal sign-up, submission checks, approval flow, bank-change re-approval |
| `procure/invoices.py` | 484 | Upload intake chain, deterministic invoice UUIDs, document → bill, bill balance |
| `procure/payables.py` | 405 | Payments Made: idempotent requests, reservation rules, `PayCommand`, payout dedupe |
| `procure/core.py` | 130 | `tx()`, compare-and-set `StateMachine`, in-process `EventBus`, audit table |
| `procure/api.py` | 142 | Router for `/api/procure/*` and `/api/portal/*` |

## 1. Design invariants

These hold for every code path. The tests in `tests/` assert them.

1. **At-most-once payment.** `payments.idempotency_key` is `PRIMARY KEY` = `vendor_id:norm_invoice_no`.
   The insert and the `invoices.state='paid'` update run in one `BEGIN IMMEDIATE` transaction.
2. **At-most-once billing per request.** Every Crustdata and Jev response is written to `http_cache`
   under `sha256(endpoint ‖ canonical_json(body))` before the step that used it is checkpointed.
3. **Rules decide; the model only restricts.** Jev answers can add a `fail`/`warn` or choose a sentence
   to quote. No Jev answer can turn a `fail` into a `pass` or route a document to a less careful path.
4. **Every fact has a source.** A fact is `{claim, value, source}`. A source is an API response
   (`endpoint`, `field`, `record_date`, `retrieved_at`, `request_hash`), a fetched URL plus the verbatim
   passage, the vendor master, the email header, a photo row with its bounding box, or `computed`.
   Search snippets and redirect URLs are never sources.
5. **Unknown ≠ negative.** A source outage gives `unavailable`. A missing record gives `CANNOT_VERIFY`
   with the reason. Both hold the invoice. Neither is reported as "no record".
6. **Determinism.** Extraction is regex/geometry. Jev is pinned (`jev-1.14.0`) and cached by
   `sha256(model ‖ state ‖ questions)`. Re-running a document gives the same checks and verdict.

## 2. Persistence (`store.py`)

One SQLite file (`data/vendorcheck.db`, override `VENDORCHECK_DB`), `journal_mode=WAL`,
`isolation_level=None` (autocommit, explicit `BEGIN IMMEDIATE` where atomicity matters), one shared
connection guarded by a process-wide `threading.RLock` (re-entrant, so a read inside a
`procure.core.tx()` write on the same thread does not deadlock).

| Table | Primary key | Purpose |
|---|---|---|
| `jobs` | `job_id` | Vendor-trust job: request, `running\|done\|failed`, verdict, report JSON |
| `steps` | `(job_id, step)` | Checkpoint of each pipeline step's output |
| `http_cache` | `req_hash` | Response body, HTTP status, credits charged, `retrieved_at` |
| `ledger` | autoinc | One row per call, cache hits included: endpoint, credits, latency, status |
| `invoices` | `invoice_key` | Extracted fields, state, decision JSON, `vendor_hash_at_approval` |
| `payments` | `idempotency_key` | Simulated ledger entry: amount, account, IFSC, payee, approver, `SIMPAY-` ref |
| `files_seen` | `source_file` | `(path, content_hash)` → outcome; same file + same bytes is a no-op |
| `actions` | `idempotency_key` | Proposed irreversible actions (`add_to_vendor_master`), approval state |
| `bank_lines` | `line_key` | Posted statement lines, result, payee, account, claimed `payment_key` |
| `events` | autoinc | Append-only activity log, keyed by job id |
| `proc_*`, `sim_bank_transfers` | see §8 | Procurement services; created lazily by `core.ensure()` |

`job_credits(job_id) = SUM(ledger.credits)` is the budget meter. Cache hits are ledgered at 0 credits,
so `paid_calls` and `cache_hits` are both reported per job.

## 3. External call layer

### 3.1 Crustdata (`crust.py`)

```
call(job_id, endpoint, body):
  h = sha256(endpoint + json.dumps(body, sort_keys=True))[:32]
  if http_cache[h]: ledger(cache_hit=1, credits=0); return cached
  if job_credits(job_id) + FALLBACK_COST[endpoint] > max_credits_per_job: raise BudgetExceeded
  for attempt in 0..max_retries(3):
    POST api.crustdata.com{endpoint}  (x-api-version: 2025-11-01, timeout 60 s)
    2xx  → credits = X-Credits-Used header (else fallback); cache_put; ledger; return
    404  → cache_put(status=404, credits=0); return {not_found: True}     # "no match" is an answer
    429/5xx → sleep(min(x-ratelimit-reset, 30) or 2^attempt); retry
    other 4xx → raise ApiError
    URLError/timeout → sleep(2^attempt); retry
  raise last error
```

The budget check runs before the request, against the pre-call estimate. Endpoints used:
`/company/identify` (0.2), `/company/enrich` (2.0), `/web/enrich/live` (1.0), `/web/search/live` (1.0).

### 3.2 Jev (`jev.py`)

Jev is a typed classifier: each question is `noul` (P(yes)), `choice` (option + confidence) or `score`
(probability per ordinal level). Several questions go in one request against one `state`.

- **Redaction before send.** GSTIN, PAN, IFSC, email addresses and any run of 9+ digits are replaced by
  placeholders. On bills, GSTINs are replaced by stable aliases `[GSTIN-n]` so Jev can still say which
  block one sits in without seeing the number.
- **Threshold mapping** (`config.py`, live-tunable through `POST /api/settings`):
  `noul ≥ 0.80 → yes`, `≤ 0.20 → no`, else `None` ("cannot tell", never "medium").
  `choice` counts at confidence ≥ 0.80. `score` counts only if the argmax level's own probability ≥ 0.70.
- **Citations without generation.** Code splits the text into sentences (≤ 280 chars, ≤ 40–60 per call)
  and offers them as `choice` options `s0…sN, none`. `quoted()` returns `sents[i]` only if it occurs
  verbatim in the original text. Jev never produces text that reaches the user.
- **Failure contract.** `ask()` raises `Unavailable` on: disabled, no key, 401/400/422, non-JSON, an
  answer set ≠ question set, or retries exhausted on 429/529/5xx (backoff `min(5, 0.5·2^n)`, honours
  `retry-after`). Every caller catches it and keeps the rule's outcome, logging
  "jev unavailable (…); fell back to rule". Jev calls are ledgered under `jev:/v1/systemone` at 0 credits.

## 4. Invoice pipeline (`ap.py`)

### 4.1 State machine

```
            ┌────────── files_seen(path, sha256) hit ──────────► no-op
 document ──┤
            └─► extract ─► classify ─► identify vendor ─┬─ none ─────────────► HELD (+ background check)
                                                         ├─ bank_change ──────► HELD (+ call-back draft)
                                                         ├─ unknown type ─────► HELD
                                                         ├─ receipt/reminder ─► REJECTED, or HELD if flagged
                                                         ├─ no inv_no/amount ─► HELD
                                                         └─ invoice ─► duplicate? ─ yes ─► DUPLICATE
                                                                          │ no
                                                                          ▼
                                       sender · gstin · po · bank · trust · business_match · jev signals
                                                                          │
                                                 any fail/unavailable/warn ┼─► HELD (+ draft if bank mismatch)
                                                                          ▼
                                                               AWAITING_APPROVAL
                                                                          │ approve(key, who)
                                                                          ▼
                                                     APPROVED (vendor_hash_at_approval stored)
                                                                          │ pay(key)
                                              vendor_hash changed? ─ yes ─┼─► HELD (re-approval)
                                                                          ▼
                                                                        PAID
```

Terminal rows are written by `_finish` with `INSERT … ON CONFLICT(invoice_key) DO NOTHING`, so a
replayed document cannot overwrite a decided invoice.

### 4.2 Keys

| Key | Form | Collision means |
|---|---|---|
| Invoice | `V001:2026-451` | Same vendor, same invoice number after normalisation |
| Duplicate row | `V001:2026-451#dup-<sha16>` | Stored next to the original, `duplicate_of` set |
| Bank change | `V003:BANKCHG:<sha16>` | Same request bytes |
| Unreadable | `V001:unreadable:<sha16>` / `unreadable:<sha16>` | Same bytes |
| Trust job | `trust:V001:<sha8(legal_name, domain, director, gstin, check_version)>` | Same vendor facts and check version |
| Bill background job | `bill:<sha12(name|domain|gstin)>` | Same business, any invoice |
| Bank line | `sha16(date|DESC|amount|occurrence)` | Same nth identical line, any file name |

`norm_invoice_no` tokenises on `[A-Za-z]+|\d+`, drops `INV|INVOICE|NO|BILL|TAX|TI`, strips leading zeros:
`INV/2026/0451`, `INV-2026-451` and `2026 451` all map to `2026-451`.

### 4.3 Checks

Each check returns `{check, result ∈ pass|warn|fail|unavailable|info|n/a, reasoning, evidence[], weight}`.

| Check | Rule | Fail weight |
|---|---|---:|
| `vendor_identified` | GSTIN exact → sender domain exact → token-Jaccard name similarity ≥ 0.5 | 80 |
| `duplicate` | Same key, or same `(vendor, amount, invoice_date, po_no)` under a different number | 100 |
| `sender_domain` | Equal → pass; Levenshtein ≤ 3 or contains a legal-name token → look-alike fail; else warn | 40 |
| `gstin_binding` | Mod-36 check character (60); legal name says Limited/Pvt but PAN holder ≠ `C` (50); ≠ vendor-master GSTIN (60) | 50–60 |
| `three_way_match` | PO exists and is this vendor's (40); amount within max(₹1, 1%) of PO (40); open invoices on the PO + this one ≤ 101% of PO (60); GRN exists (30); qty ≤ qty received (30) | 30–60 |
| `bank_details` | Account or IFSC ≠ verified account on file (60, plus a call-back draft); holder-name similarity to legal name < 0.5 (50). No bank details → pay the account on file | 50–60 |
| `vendor_trust` | §5 verdict: `CLEAR` pass, red flag fail 60, amber warn 20, `CANNOT_VERIFY` fail 70, outage 50 | 50–70 |
| `business_match` | Jev: does the vendor's own site describe what is billed (invoice text or PO item)? | warn 25 |
| `instructions_to_reviewer` | Jev: text addressed to an automated reviewer (prompt injection) | 60 |
| `payment_redirect` | Jev: says the account is new/changed, and the account ≠ verified | 40 |
| `pressure` | Jev score level 3 (urgency + secrecy) fail; level 2 warn | 40 / 15 |
| `classify` | Jev may only reroute to `bank_change_request`; any other disagreement warns | warn 20 |

`risk_score = min(100, Σ weight·[fail|unavailable] + Σ ⌊weight/2⌋·[warn])`. The score is for ranking the
queue. The state does not depend on it: any `fail`, `unavailable` or `warn` holds.

### 4.4 Exactly-once payment

```python
approve(key, who):
  UPDATE invoices SET state='approved', vendor_hash_at_approval=H(vendor)
   WHERE invoice_key=? AND state='awaiting_approval'          # guarded transition
  pay(key)

pay(key):
  if state == 'paid': return already_paid                      # idempotent re-entry
  if state != 'approved': refuse
  if H(vendor) != vendor_hash_at_approval: state='held'; refuse   # master-data tamper (TOCTOU) guard
  BEGIN IMMEDIATE
    INSERT OR IGNORE INTO payments(idempotency_key=key, …)
    UPDATE invoices SET state='paid'
  COMMIT

resume(): for each invoice in state 'approved': pay(key)       # runs on `serve` start and before `inbox`
```

`H(vendor) = sha256(vendor_id, legal_name, gstin, bank, domain)[:16]`. A crash between `approve` and
`pay` leaves `approved`. A crash inside the transaction rolls back to `approved`. In both cases `resume`
completes it once. `CRASH_AFTER_APPROVE=1` calls `os._exit(9)` at that point to exercise the path.
The ref is `SIMPAY-` + `sha256(key)[:10]`, so a replayed payment always carries the same ref.

### 4.5 Recheck

`recheck(key)` applies only to `held`. It deletes the invoice and `files_seen` rows, deletes the trust
job only if it is not `done` (an outage left it `failed`), and re-runs `process_file`. Completed steps
and cached responses are replayed at 0 credits, so only the failed call is re-billed.

## 5. Vendor trust job (`pipeline.py`)

`Job.step(name, fn)` = read `steps[(job_id, name)]`; if absent, check the wall-clock limit
(`max_seconds_per_job`, 180 s), run `fn`, write the checkpoint. A job already `done` returns its stored
report. `BudgetExceeded` and `TimeoutError` end the job as `CANNOT_VERIFY` naming the limit.

| Step | Calls | Logic |
|---|---|---|
| GSTIN (no step) | 0 | Check character, state code → state, PAN holder type. "Limited"/"LLP" on a non-C/F PAN → amber |
| `resolve` | identify ×1 | **By domain**: candidates whose domains include the input domain. 1 → chosen; >1 → rank by name similarity, others flagged as duplicate records. **By name only**: accept only if exactly one match has confidence ≥ 0.95 *and* name similarity ≥ 0.99; otherwise `CANNOT_VERIFY` listing candidates. Name vs domain-owner similarity < 0.5 → red (impersonation) |
| `profile` | enrich ×1 | `basic_info, headcount, locations, people, news, funding`. Dataset status ≠ active → red; age < 1 y, headcount < 5, named director absent from founders/CXOs → amber. Profile news titles matched against `ADVERSE_TERMS` + company token → red |
| `website` | fetch ×1–4 | Order: URL, `www.` twin, then each with `human_mode`. Stop at the first response with ≥ 200 chars of visible text. Never a different registrable domain. Regex "for sale/parked" → red. Jev: parked → red; uses the name but is not the company's site → amber |
| `registry` | search ×1 + fetch ×k | `"<legal name>" CIN`. Keep results whose title+snippet contain every name token, regex the CIN, **fetch the page** and require the CIN as a whole word in visible text. Exactly one distinct CIN → fact (strength `secondary`); several → `cannot_verify`; redirect-only → `cannot_verify` |
| `gstin` | search ×1 + fetch | The GSTIN must appear on a fetched page; the passage must contain a legal-name token, otherwise red |
| `adverse_company` | search ×1 + fetch | Query = name + legal/regulatory terms. A result counts only if the term list hits **and** the fetched article contains the company. Hits are amber: the company's role (subject, victim, carrier) is left to a person |
| `adverse_person_{i}` | search ×1 + fetch, i < 2 | Requested director first, then founders/CXOs (deduped at similarity 0.6). Article must contain surname **and** company → red. Search-only match → amber "possible". Others are counted as namesakes cleared; nothing about them is stored |

Verdict: any red → `HOLD`; any amber → `HOLD`; neither → `CLEAR`, which proposes an
`add_to_vendor_master` action (`INSERT OR IGNORE`, executes once after approval). The report carries
`facts[]`, `flags[]`, `cannot_verify[]`, `decisions[]` (Jev answers with request ids),
`screening{hits_found, hits_cleared, hits_escalated}` and `cost{credits, inr, paid_calls, cache_hits, jev_calls}`.

**Verdict reuse.** The trust job id hashes the vendor facts it checked plus `TRUST_CHECK_VERSION`. A
second invoice from an unchanged vendor returns the stored report (0 credits). Editing any of those
facts on the vendor master changes the id and forces a fresh check. Bumping the version re-runs every
vendor once, and only the new steps are billed because the old calls replay from `http_cache`.

## 6. Bill photos (`scan.py`, `bill.py`, `vision_ocr.swift`)

```
bytes ─► magic-byte type ─► sha256 ─► <data>/scans/scan_<h16>.{jpg,png,heic}   (atomic tmp+rename)
                   └─ scan_<h16>.json exists? ─ yes ─► reuse reading (0 ms, ledgered as cache hit)
                                               no ─► OCR ─► lines[{text, conf, box, quad}], size
                                                     └─► bill.parse ─► review_with_jev ─► record
record ─► canonical text ─► inbox/scan_<h16>.txt ─► ap.process_file   (same checks as email)
```

**OCR backend.** macOS: `vision_ocr.swift`, compiled once with `swiftc -O` into `data/bin/vision_ocr`
(recompiled when the source mtime is newer), `VNRecognizeTextRequest` at `.accurate` with language
correction **off** so GSTINs and amounts are not "corrected". It returns normalised boxes and the four
corners of each line, accounting for EXIF orientations 5–8. Elsewhere: `tesseract … tsv`, words grouped
by `(block, par, line)`, confidence = min word confidence. `warm()` runs a 64×64 blank image at server
start, so the first real scan does not pay Vision's ~45 s model load.

**Geometry.** Page tilt = median `atan2` of the top edges of lines wider than 15% of the page, in pixel
space. Every line centre is rotated by −θ. Rows are formed by sorting on the rotated y and merging a
line into the previous row when `|Δy| < k·min(h)` (k = 0.6 straight, 0.75 tilted). Items within a row are
ordered by x₀. The items table is read column-by-column from a detected header (two-row headers and
wrapped cells included), and each column is classified by kind (taxable, tax rate, tax, amount…).

**Field extraction.** Each label has a regex. Its value is taken to the right on the same row or from
the cell below, which covers the Tally, Busy and Vyapar layouts. A GSTIN belongs to the party whose
section heading (`Bill to`/`Buyer`, `Ship to`/`Consignee`) is nearest above it; with no heading it is
the seller's. OCR repair: in a 15-char GSTIN candidate failing its check character, positions that must
be digits (0,1,7–10) and letters (2–6,11) are transliterated (`O↔0, I↔1, S↔5, B↔8, …`), and the fix is
kept only if the check character then passes. A currency sign directly before a number becomes `₹`.

**Self-consistency checks** pick the total by evidence, never by position:
`taxable + CGST + SGST (+IGST + cess) ± round-off = total`; `words_to_number(total in words) = total`
(handles lakh/crore); `CGST = SGST`; `rate × taxable = tax`; intra-state ⇒ CGST+SGST, inter-state ⇒ IGST;
IFSC `^[A-Z]{4}0[A-Z0-9]{6}$`; bank account holder ≈ seller.

**Jev tie-break.** Only fields with ≥ 2 candidates (seller name, seller GSTIN, total, invoice number,
invoice date) are sent as `choice` questions with the rule's pick hidden among the options. Agreement →
status `checked`. Disagreement → `conflict` (the bill is held) unless the field is `hard`, i.e. already
settled by words, arithmetic or the check character.

**Provenance.** `attach()` links an inbox text back to its scan only if `sha256(text)` equals the
record's `text_sha`. Typed text that only claims to be a scan keeps every email check. Each field keeps
its photo row, boxes and confidence, and the UI outlines them on the photo. `check_legibility` fails if
the invoice number, amount, or both GSTIN and name are missing (weight 80). It warns on a rule/Jev
conflict, on broken arithmetic, or on any field with confidence below `min_read_confidence` (0.5).

## 7. Bank statements (`bank.py`)

Per line, in order:

1. `line_key = sha16(date | upper(collapse_ws(desc)) | amount | occurrence)`, where `occurrence` is the
   ordinal of identical `(date, desc, amount)` tuples within the file. Already in `bank_lines` → skip.
   Re-imports and overlapping statements post nothing twice, and two genuine identical charges keep
   distinct keys.
2. Same `(date, clean_descriptor, amount)` as an earlier line in this file → `HELD` (possible duplicate).
3. `clean_descriptor`: strip rail prefixes (`RAZ*`, `PAYTM*`, `UPI/`, `NEFT/` …), city tokens and
   trailing legal suffixes.
4. Payee: vendor master name/aliases (similarity ≥ 0.5) → else `/company/identify` by name, accepted only
   if exactly one match has confidence ≥ 0.95 and name similarity ≥ 0.99. With several such matches,
   accept the one whose primary domain's first label equals the descriptor and whose headcount band is
   ≥ 1001, if exactly one qualifies. Otherwise `CANNOT_VERIFY`.
5. Legal name: fetch the payee's site and run
   `LEGAL_RE = ([A-Z][\w&.-]*\s+){1,6}(Private\s+)?(Limited|Ltd)` on the visible text. Leading words are
   dropped until the match starts with a brand token (`Copyright Swiggy Limited` → `Swiggy Limited`; a
   parent such as `Bundl Technologies Private Limited` is never taken). Accepted only if exactly one distinct
   name remains; cited to the URL and passage.
6. Ledger account from `accounts.json` by payee keyword, else `HELD`.
7. `NEFT|RTGS|IMPS` to a vendor on file must claim an unclaimed `payments` row with the same vendor and
   `|Δamount| < 0.5`. The `payment_key` is stored on the line, so each payment is claimed at most once.
   No match → `HELD` (money left with no approved invoice behind it).

Each line runs as its own job `bank:<file>:<n>`, so the credit limit is per line. Repeated merchants
are answered from `http_cache`.

## 8. Procurement services (`procure/`)

Three services follow Zoho Procurement's flows: vendor onboarding with a vendor portal, uploaded
invoices converted to bills, and Payments Made. They share the SQLite file but own disjoint `proc_*`
tables. They communicate through `core.BUS` events (`vendor.submitted`, `vendor.approved`,
`vendor.bank_changed`, `vendor.inactive`, `invoice.uploaded`, `bill.opened`, `payment.paid`), and
handlers run after the publishing transaction commits. Each service can therefore move to its own
process with the event names mapped to topics.

**Shared primitives (`core.py`).** `tx()` = `store._lock` + `BEGIN IMMEDIATE … COMMIT/ROLLBACK`.
`StateMachine(table, id_col, transitions).move(conn, id, frm, action, **cols)` validates `action ∈ T[frm]`,
then runs `UPDATE … SET status=to WHERE id=? AND status=frm`. If `rowcount ≠ 1`, it raises 409 (lost
race), otherwise it appends a `proc_audit` row in the same transaction. `DomainError(msg, code)` carries
the HTTP status, so services never import the server.

### 8.1 Vendor onboarding (`onboarding.py`)

| From | Action → To |
|---|---|
| `draft`, `invited` | `submit` → `approval_pending`; `invited` also `cancel_invite` → `cancelled` |
| `approval_pending` | `approve` → `active`; `request_more_details` → `pending_review`; `reject` → `rejected` |
| `pending_review`, `rejected` | `submit` → `approval_pending` |
| `active` | `mark_inactive` → `inactive`; `bank_change` → `approval_pending` |
| `inactive` | `mark_active` → `active` |

The portal is authorised by an `invite_token` (`UNIQUE`) carried in the URL fragment. On submission a
Strategy list runs: required fields; GSTIN check character, plus PAN holder type `C` when the name says
Limited/Pvt; IFSC regex; holder-vs-legal-name similarity; email domain vs website (free mail warns);
GSTIN or bank account already owned by another vendor (fail). The §5 trust job runs on a daemon thread
with `request_id = onb-<vendor>-<ts>`. Any exception records `CANNOT_VERIFY`, never a pass. A failed
check blocks `approve`. An `active` vendor that edits bank details goes back to `approval_pending` with
`bank_verified_on = NULL` and a call-back warning, and emits `vendor.bank_changed`.
`active_master()` projects active vendors into the shape of `vendors.json`, and `ap.vendors()` merges
them in with `setdefault`, so the client's fixture file wins on id collision.

### 8.2 Uploaded invoices → bills (`invoices.py`)

| Id | Derivation | Names |
|---|---|---|
| `doc_uuid` | `uuid4()` at upload | One uploaded file (every resend gets a new one) |
| `invoice_uuid` | `uuid5(INVOICE_NS, "<vendor_id>:<norm_invoice_no>")` | The invoice, on any machine, under any numbering format. Becomes `proc_bills.bill_uuid` |

`INVOICE_NS` is a fixed constant. Changing it would re-identify every invoice and break de-duplication.

Intake is a Chain of Responsibility. A handler appends checks to `ctx` or raises `Stop(result)`:

```
FileGuard          size ≤ 5 MB, magic-byte sniff (PDF/JPEG/PNG/HEIC, else UTF-8 text/eml)
VendorGuard        vendor exists and may upload
ExactFileDedupe    UNIQUE(vendor_id, sha256) → return the first doc_uuid, nothing stored
Store              write <uploads>/<doc_uuid>.<ext>
Read               Strategy by type: text | pdftotext -layout | scan.read_image → rows;
                   typed form fields vs document text → `typed_vs_document` fail on mismatch
Identify           norm invoice no → invoice_key, invoice_uuid; missing number or amount → fail
DuplicateInvoice   bill with this uuid, or pending doc with this uuid → fail;
                   same vendor + amount within 30 days → warn (re-numbered duplicate)
PurchaseOrderMatch PO exists / this vendor / not over-billed / GRN qty  (no PO or no GRN → warn)
BankOnInvoice,
GstinOnInvoice     reuse ap.check_bank / ap.check_gstin
```

`HARD_FAILS = {duplicate_invoice, invoice_identity, bank_details, typed_vs_document, amount}` block
*Convert to Bill*. `accept()` inserts the bill with `bill_uuid = invoice_uuid` and
`invoice_key UNIQUE`, so a second bill for the same invoice is refused by the database
(`IntegrityError` → 409), not by application logic. The bill snapshots `vendor_bank = account|ifsc`.

Bill transitions: `draft → pending_approval → open → partially_paid → paid`, `pending_approval →
rejected → pending_approval`, `draft|rejected|open → void → draft`. Column constraint
`balance REAL CHECK (balance >= 0 AND balance <= amount)`. `apply_payment` decrements with
`UPDATE … SET balance = balance - ? WHERE bill_uuid=? AND balance >= ?`, a compare-and-set that
cannot go negative, then moves the bill to `pay` or `part_pay` in the same transaction.

### 8.3 Payments Made (`payables.py`)

| From | Action → To |
|---|---|
| `draft` | `submit` → `pending_approval`; `void` → `void` |
| `pending_approval` | `approve` → `processing`; `reject` → `rejected`; `void` → `void` |
| `rejected` | `submit` → `pending_approval`; `void` → `void` |
| `processing` | `settle` → `paid`; `hold` → `pending_approval` |
| `void` | `convert_to_draft` → `draft` |

Double payment is blocked at three layers:

1. **Request** (`@idempotent(scope)` decorator on `create` and `approve`). The `Idempotency-Key`
   header is required. Row `proc_idempotency(idem_key, scope)` with `request_hash =
   sha256(scope ‖ canonical body)`. A first call inserts `in_progress`, runs, and stores
   `(code, response)` as `completed`. 4xx business answers are final for that key. A replay with the same
   hash returns the stored response with `idempotent_replay: true`. A different hash returns 422. A
   replay while the first call is still `in_progress` returns 409. An unexpected exception deletes the
   row so the client can retry.
2. **Domain.** Rules run on create, submit and approve: vendor active with a verified account; every bill
   `open|partially_paid` and owned by the vendor; `amount ≤ balance − reserved`, where
   `reserved = Σ allocations of other payments in {draft, pending_approval, rejected, processing}`;
   same vendor and amount within 30 days → warn, which needs an approver note. `create` re-checks
   reservations inside its write transaction, so concurrent creates against one bill serialise and only
   those that fit are recorded.
3. **Execution** (`PayCommand`). `approve` commits `processing` together with a snapshot
   `pay_to_account/ifsc/name` *before* any money moves. `execute()` re-reads the vendor. If the account
   differs from the snapshot and no transfer exists for this payment, the payment goes to `hold` →
   `pending_approval`. Otherwise it calls `SimBank.transfer(idem_key = payment_id, …)`, which does
   `INSERT OR IGNORE` into `sim_bank_transfers` (PK `idem_key`) and refuses a reused key carrying
   different details. Settlement then applies allocations to bills and moves to `paid` in one
   transaction. `resume()` re-executes every `processing` payment. A crash after the transfer
   (`PROCURE_CRASH_AFTER_TRANSFER=1`) is finished without a second transfer, because the bank returns
   the existing row. Subscriber `vendor.bank_changed` → `execute()` on that vendor's `processing`
   payments, which holds them.

## 9. HTTP surface (`server.py`)

`ThreadingHTTPServer` on `127.0.0.1`. Reads are concurrent (WAL). Every mutating route that runs the
pipeline (`/api/process`, `/api/upload`, `/api/scan`, `/api/bank`, `/api/recheck`, `/cases`) takes one
process-wide `_busy` lock, so pipeline writers are serialised. `/api/procure/*` and `/api/portal/*` are
dispatched to `procure.api.route` without `_busy`. Their atomicity comes from `tx()` and the
compare-and-set transitions, and they also run concurrently with the pipeline. `ui.html` is re-read per request, while Python modules load once at start.

| Method | Path | Handler |
|---|---|---|
| GET | `/api/invoices`, `/api/invoices/<key>` | Queue; case with decision, checks, evidence, events (invoice + trust job) |
| GET | `/api/payments`, `/api/bank`, `/api/vendors`, `/api/events`, `/api/settings` | Read models |
| GET | `/api/scan/<scan_hex16>` | Stored photo; name validated against `scan_[0-9a-f]{16}` |
| GET | `/cases/<job_id>` | Vendor trust report |
| POST | `/api/upload {name, text}` | Write to inbox (basename only), `process_file` |
| POST | `/api/scan {image}` | Data URL/base64 → `scan_bytes` (≤ 15 MB, magic-byte type check) |
| POST | `/api/approve {key, by}` | `approve` → `pay` |
| POST | `/api/recheck {key}` | Replay a held invoice |
| POST | `/api/bank {name, csv}` | `post_file` |
| POST | `/api/settings` | Live update of limits and Jev thresholds (clamped to [0, 1]) |
| POST | `/cases {vendor_name, website, director, gstin}` | Standalone vendor check |
| GET | `/portal` | Vendor portal page (`#t=<invite token>`) |
| GET/POST | `/api/portal/{me,profile,documents,invoices}` | Vendor side, authorised by token |
| GET/POST | `/api/procure/vendors[/<id>[/<action>]]`, `/vendors/invite` | Onboarding |
| GET/POST | `/api/procure/documents[/<uuid>[/file, /accept, /reject]]` | Uploaded invoices |
| GET/POST | `/api/procure/bills[/<uuid>/<action>]` | Bills |
| GET/POST | `/api/procure/payments[/<id>/<action>]`, `/unpaid-bills`, `/payments/resume` | Payments; `create` and `approve` need `Idempotency-Key` |

Startup: open DB → `ap.resume()` → `PayablesService.resume()` → background `scan.warm()` → `serve_forever`.

## 10. Failure model

| Fault | Where it lands | Result |
|---|---|---|
| Process killed mid-trust-job | `steps` checkpoint | Restart replays finished steps from `steps`, unfinished calls from `http_cache` |
| Killed after approve, before pay | `invoices.state='approved'` | `resume()` pays once |
| Killed inside the pay transaction | SQLite rollback | Still `approved`; `resume()` pays once |
| Vendor bank/GSTIN edited after approval | `vendor_hash` mismatch | `HELD`, needs re-approval |
| Crustdata 429/5xx/network | `crust.call` retry ×3 | Then `unavailable` → `HELD`; `recheck` later |
| Crustdata 404 | cached as an answer | "no company record carries the domain" → `CANNOT_VERIFY` |
| Credit or time limit | `BudgetExceeded` / `TimeoutError` | `CANNOT_VERIFY: stopped at a limit` |
| Jev down, no key, malformed | `jev.Unavailable` | Rule outcome stands, logged as fallback |
| Same file re-dropped | `files_seen` | No-op |
| Same invoice, new number/format | key or `(vendor, amount, date, PO)` | `DUPLICATE` |
| Same photo re-uploaded | `scan_<sha16>.json` | Stored reading reused, then `DUPLICATE` |
| OCR unreadable | `check_legibility` | `HELD: cannot read` |
| Prompt injection in the document | Jev `injection` + sentence quote | `HELD` |
| Look-alike sender | Levenshtein ≤ 3 | `HELD` |
| Double-click / retried POST on a payment | `proc_idempotency` | First response replayed |
| Concurrent payments against one bill | reservation re-check in `tx()` + `balance` CAS | Only payments that fit the balance are recorded |
| Crash after bank transfer, before settle | `sim_bank_transfers` PK | `resume()` settles; no second transfer |
| Vendor edits bank details in portal | `vendor.bank_changed` | Vendor → `approval_pending`; in-flight payments held |
| Same invoice uploaded under another number format | `uuid5` = `bill_uuid` PK | Second bill refused by the database |

## 11. Testing

`python3 -m unittest discover tests` runs 106 tests offline. `tests/offline.py` sets `JEV_ENABLED=0` and
removes `TYPESAFE_API_KEY` before import. Crustdata is replaced by canned responses keyed by endpoint.
Each test case runs against a fresh `tempfile.mkdtemp()` database and data directory.

| File | Tests | Covers |
|---|---:|---|
| `test_bill.py` | 29 | Parser on stored OCR output of real TallyPrime/Vyapar photos and public samples (`data/fixtures/ocr/`) |
| `test_scan.py` | 23 | Rows, tilt, rupee/GSTIN repair, provenance, legibility; 2 run the real reader on `data/fixtures/bills/` |
| `test_background.py` | 21 | Unknown-sender checks, PAN types, free mail, GSTIN-only, outage + replay, www fallback |
| `test_payables.py` | 9 | Every inbox outcome, idempotent re-run, crash-resume, vendor-hash guard, outage hold |
| `test_bank.py` | 9 | Statement end to end, legal-name rules, re-import, one-payment reconciliation, budget |
| `test_procure.py` | 10 | Onboarding flow and front-company block, shared bank account, invoice-number formats, file re-upload, key replay/misuse, crash after transfer, 6 concurrent payments → 1 recorded, bank change before transfer |
| `test_website.py` | 5 | Fetch fallback order and human_mode |

## 12. Controls → code

| Control | Enforced in |
|---|---|
| A person approves every payment; approver name recorded | `ap.approve`, `PayablesService.approve` |
| No message is sent; call-back texts are drafts addressed from the vendor master | `ap.draft_bank_verification` |
| "Cannot verify" instead of a guess, with the missing evidence named | `Job.missing`, `bank.decide`, `check_legibility` |
| Namesakes stored as a count and a reason only | `Job.s_adverse` |
| Only fetched pages are cited; no snippets, no redirect URLs | `Job.verify_pages`, `is_redirect` |
| Model output can hold, never clear | `ap.settle_doc_type`, `ap.document_signals`, `Job.read_website` |
| Identifiers redacted before any model call | `jev.redact` |
| Bill photos never leave the machine | `scan.read_image` (local subprocess only) |
