# Payables Desk: Vendor Trust + Pay-Once

ContextCon 05, BITS Pilani · Team Humble Fumblers · Tracks **5. Trust and compliance** + **6. Buying and paying**

An invoice arrives. The agent proves the vendor is real and run by its stated director, screens the director against the news and clears namesakes, matches the invoice to its PO and goods receipt, and catches duplicates, bank-detail changes and look-alike senders. It then **pays once, or holds with a cited reason**. A person approves every payment. Every step is logged and can be replayed.

## The job, and who pays for it today

| | |
|---|---|
| **Job** | Accounts payable for an Indian 3PL: take invoices from the inbox, verify the vendor, match to PO and goods receipt, pay each invoice exactly once, stop fraud before money moves. |
| **Who does it today** | The client's AP team plus outsourced accounting firms (paid per invoice or per month), and KYB/verification vendors (paid per business checked). |
| **Input** | Invoice or email text (`.txt` / `.eml`) in the inbox, the client's vendor master, purchase orders and goods receipts (`data/fixtures/`). |
| **Finished result** | Each document ends as `PAID` (simulated ledger entry with idempotency key), `HELD` (cited reason + drafted verification message), `DUPLICATE` (linked to the original) or `REJECTED` (not payable). |
| **Pass mark** | No invoice paid twice; no payment to an unverified account or unverifiable vendor; every fact cites its source and date; "cannot verify" instead of guessing; a kill at any point resumes without repeating an action. |

## Run it from a fresh clone

Python 3.10+ and the standard library only. No packages to install.

```bash
cp .env.example .env            # then set CRUSTDATA_API_KEY=...
python3 -m vendorcheck.cli inbox        # process every document in data/fixtures/inbox
python3 -m vendorcheck.cli status       # one line per document
python3 -m vendorcheck.cli approve "V001:2026-451" --by "AP lead"   # irreversible step: pay
python3 -m vendorcheck.cli serve --port 8080                       # UI at http://127.0.0.1:8080
```

A vendor check on its own (5.1 + 5.2):

```bash
python3 -m vendorcheck.cli vendor --name "TCI Express" --website tciexpress.in --director "Anil Kumar"
```

HTTP API (for another system to call): `POST /cases`, `GET /cases/<id>`, `GET /api/invoices`, `GET /api/invoices/<key>`, `POST /api/upload`, `POST /api/approve`, `POST /api/recheck`, `GET /api/payments`.

## Results on the test set (live data, 22 Nov 2026)

| # | Document | Expected | Result |
|---|---|---|---|
| 1 | Shadowfax invoice, clean | Pay after approval | `AWAITING_APPROVAL` → approved → `PAID` once |
| 2 | Shreeji Cargo Movers (fictitious front company) | Hold | `HELD`: no company record carries the domain; cannot verify |
| 3 | TCI Express invoice; director "Anil Kumar" (very common name) | Pay, namesakes cleared | `AWAITING_APPROVAL`; 6 adverse articles about other people named Anil Kumar cleared with the reason "does not mention TCI Express" |
| 4 | Shadowfax invoice re-sent as `INV-2026-451` (was `INV/2026/0451`) | Duplicate | `DUPLICATE` of `V001:2026-451` |
| 5 | "We have changed our bank" email | Hold, verify out of band | `HELD` + drafted call-back message to the contact on file (never to the sender) |
| 6 | TCI invoice asking for payment to a new account | Hold | `HELD`: account differs from verified account; PO already invoiced |
| 7 | Invoice from `shadowfaxx.in` (look-alike domain) | Hold | `HELD`: look-alike sender, edit distance 1 |
| 8 | Kill after approval, before payment | Pay once | Restart → `resume` pays once; second resume and re-approve do nothing |

## How it works

```mermaid
flowchart LR
  IN[Inbox: .txt / .eml / upload] --> CL[Classify: invoice / bank change / receipt / reminder]
  CL --> EX[Extract fields: deterministic regex]
  EX --> VI[Identify vendor: GSTIN, then sender domain, then name]
  VI --> DUP{Duplicate?<br/>normalised invoice no.,<br/>or same vendor+amount+date+PO}
  DUP -- yes --> D[DUPLICATE]
  DUP -- no --> CHECKS
  subgraph CHECKS [Independent checks, each returns an evidenced verdict]
    S[Sender domain vs vendor domain]
    G[GSTIN check char, PAN type, binding to vendor master]
    M[3-way match: PO, goods receipt, PO not already billed]
    B[Bank details vs verified account and holder name]
    T[Vendor trust 5.1 + 5.2]
  end
  T --> CD[(Crustdata: identify by domain, enrich, web fetch, web search)]
  CHECKS --> AGG[Aggregate: weights, risk score, reason]
  AGG -- any fail/unavailable/warn --> H[HELD + drafted message]
  AGG -- all pass --> AP{Human approval}
  AP -- approve --> PAY[Pay once: INSERT keyed by vendor:invoice no.]
  PAY --> L[(Simulated payment ledger)]
  CD -. every call .-> LED[(Cost ledger + response cache)]
  CHECKS -. every step .-> ST[(SQLite: jobs, steps, invoices, actions, events)]
```

**Vendor trust (5.1 + 5.2)**, `vendorcheck/pipeline.py`:

1. **Resolve the entity by domain, never by name alone.** By name, Crustdata returned a French "SAFEXPRESS" at confidence 1.0 above the Indian Safexpress, and three different "Shadowfax" companies at 1.0. If a name maps to more than one company, the result is `CANNOT_VERIFY` with the candidates listed.
2. **Company profile** (`/company/enrich`): name, type, year founded, headcount, HQ, founders/CXOs, news. Crustdata's `status` is cited as the dataset's status, not as registry status.
3. **Website** (`/web/enrich/live`): live, names the company, not parked.
4. **Registry IDs** (`/web/search/live`): a CIN is accepted only from a result that also names the company. It is cited as a secondary listing, not the MCA registry. If listings show several CINs (for example several TCI group companies), the output says "registry not confirmed" instead of picking one.
5. **Adverse media.** The company and the director named on the request are screened first, then the leaders on record. Only legal and regulatory terms count (raid, FIR, ED/CBI, NCLT, GST evasion, money laundering); consumer complaints do not. An article counts only if it names the person **and** the company. Otherwise it is cleared as a namesake and the reason is stated. The report shows `hits_found / hits_cleared / hits_escalated`.
6. **Verdict:** `CLEAR`, `HOLD` (with flags) or `CANNOT_VERIFY` (naming what is missing). The verdict is cached per vendor, so later invoices from the same vendor cost 0 credits.

**Where a person is needed:** every payment, since it cannot be undone (the approver's name is recorded). Every `HELD` item. Sending any drafted message: the system never sends email or contacts anyone.

## Robustness

- **Exactly once.** The payment row's primary key is `vendor_id:normalised_invoice_no`. Approval and payment are separate states. `resume` (run on every start) pays approved-but-unpaid invoices. Re-running, re-approving or re-resuming changes nothing. Test it with `CRASH_AFTER_APPROVE=1 python3 -m vendorcheck.cli approve ...`, then `python3 -m vendorcheck.cli resume`.
- **Master-data manipulation.** A hash of the vendor record (bank, GSTIN, domain) is stored at approval. If it differs at payment time, the payment is held for re-approval.
- **Restart mid-check.** Each pipeline step is checkpointed. Each API response is cached by request hash before the step is marked done, so a restart replays from cache and is not billed twice.
- **Source failures.** Up to 3 retries with backoff, honouring `x-ratelimit-reset` on 429. If a source is still down, the check is marked `unavailable` ("source unavailable, not the same as no record") and the invoice is held. `Replay checks` (UI) or `POST /api/recheck` re-runs it later.
- **Limits.** Cost limit per job (`MAX_CREDITS_PER_JOB`, default 12 credits) and time limit (`MAX_SECONDS_PER_JOB`, default 180 s). Hitting either gives `CANNOT_VERIFY`, never a guess.
- **Deterministic.** Extraction and decisions are rules, with no LLM in the decision path, so the same input always gives the same verdict.

## Cost per job (measured)

| Job | Crustdata calls | Credits | ₹ (at ₹25/credit*) |
|---|---|---|---|
| New vendor check (identify, enrich, website, registry search, 3 adverse searches) | 7–8 | 7.2–8.2 | ₹180–205 |
| Invoice from an already-checked vendor | 0 | 0 | ₹0 (local checks) |
| Duplicate / bank-change email | 0 | 0 | ₹0 |

\*The event promo gave $4,500 of usage for 15,000 credits, which is $0.30 per credit, about ₹25. The cost of every call is in the `ledger` table and shown on each case (`python3 -m vendorcheck.cli ledger trust:V001`).

**Proposed price** (our pricing, not a market quote): per vendor verified, plus per invoice processed, in ₹. The marginal cost is the Crustdata credits above; there are no LLM tokens in the decision path.

## Data sources the service calls

| Source | Endpoint | Used for |
|---|---|---|
| Crustdata Company Identify | `POST /company/identify` | Domain → company record, duplicates and look-alikes |
| Crustdata Company Enrich | `POST /company/enrich` | Name, type, year founded, headcount, HQ, founders/CXOs, news |
| Crustdata Web Fetch | `POST /web/enrich/live` | Website liveness and content |
| Crustdata Web Search | `POST /web/search/live` | CIN listings, adverse media for the company and directors |
| Client records | `data/fixtures/*.json` | Vendor master (verified bank, GSTIN, contact), POs, goods receipts |
| Local computation | none | GSTIN check character, PAN entity type, state code |

## Approaches tried

- **Name-first entity resolution failed** on look-alikes (see above), so we resolve by domain and treat a name-only match as ambiguous.
- **Taking CINs from search snippets** gave four CINs for "TCI Express" (group companies). We now accept only a CIN from a result that names the company, and only if it is unique.
- **A broad adverse-media word list** flagged Shadowfax on consumer complaints ("fake delivery"). We switched to legal and regulatory terms only and require person and company in the same article.
- The research behind the design is in `context/research/` on the `0x1622` branch: a track deep-dive and a vendor-fraud landscape covering HighRadius, Trustpair, Eftsure and the Indian KYB stack. The binding checks (GSTIN ↔ vendor master, bank holder ↔ legal name, sender domain ↔ vendor domain) and re-verification on bank change come from there.

## What is left out, and what's next

- No MCA/GST registry API: MCA has no public API, and the portals use CAPTCHAs, which we do not bypass. Registry status is reported as "not confirmed" with the reason.
- No real bank rails or penny drop. Payments go to a simulated ledger.
- No PEP/sanctions lists yet; OpenSanctions is the next source.
- Next: Person Enrich to test "director works elsewhere", domain age via RDAP, and a Tally / Zoho Books connector in place of the simulated ledger.

## Credits

No third-party code. Python standard library only. Data from the Crustdata API.
