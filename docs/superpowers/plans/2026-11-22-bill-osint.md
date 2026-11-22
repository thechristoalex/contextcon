# Company Background Check on Every Bill: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every bill in the inbox, from a known or unknown sender, gets the existing vendor check. GSTIN facts that cost nothing are added, and nothing is shown without a source.

**Architecture:**
- `pipeline.py` gains a pure GSTIN profile (validity, state, PAN, PAN holder type) and an entity-type mismatch rule. `Job._run` uses both.
- `ap.py` gains `check_unknown_vendor(x)`, which builds a vendor-check request from the bill, runs `pipeline.run_request` and returns a check plus the report. The unknown-vendor branch of `process_file` attaches both, and the bill stays held.
- `check_trust` now also passes the vendor master's GSTIN.

**Tech Stack:** Python 3 standard library, unittest, the existing fake `run_request` and fake Crustdata patterns.

**Spec:** `docs/superpowers/specs/2026-11-22-bill-osint-design.md`

## Global Constraints
- An unknown vendor is never paid: the unknown-vendor branch always finishes `held`.
- Facts carry a source. Computed facts use `{"type": "computed", "field": ...}`, as the existing GSTIN fact does.
- Free-mail domains are never used as a website: gmail.com, googlemail.com, yahoo.com, yahoo.co.in, outlook.com, hotmail.com, live.com, rediffmail.com, icloud.com, proton.me, protonmail.com.
- PAN 4th character → holder type: C company, F firm/LLP, P individual, H Hindu undivided family, T trust, A association of persons, B body of individuals, G government, L local authority, J artificial juridical person.
- Tests stay offline: import `offline` before `vendorcheck` (see `tests/test_bank.py`).
- No UI changes. The case view already renders `decision.trust_report`.

## Review Focus
1. **Bill with only a GSTIN** (no name, no non-free domain): `run_request` would raise ValueError. Expect the offline GSTIN profile as an info check, no API call, and the bill held. Test: `test_gstin_only_bill_gets_offline_profile_without_a_call`.
2. **Vendor check raises something unexpected** (ValueError, RuntimeError): the inbox must not crash. Expect `unavailable` and held. Test: `test_lookup_error_holds_instead_of_crashing`.
3. **Second bill from the same unknown business with a different invoice number:** expect the same `request_id`, so the job cache is reused. Test: `test_same_business_reuses_one_lookup`.
4. **Account name written differently from the legal name** ("TCI Express Ltd." vs "TCI Express Limited"): expect no warning. A different name is warned. Test: `test_account_name_matches_legal_name_ignoring_suffixes`.
5. **"Limited" bill on an individual's PAN (P):** amber flag. On a company PAN (C), no flag. Words such as "Unlimited" must not trigger it. Test: `test_entity_type_mismatch`.

---

### Task 1: GSTIN profile and entity-type rule in the vendor check

**Files:**
- Modify: `vendorcheck/pipeline.py` (helpers after `gstin_valid`; the GSTIN block in `Job._run`)
- Test: `tests/test_background.py` (new; class `GstinProfileTest`)

**Interfaces:**
- Produces:
  - `pipeline.PAN_TYPES: dict[str, str]`
  - `pipeline.gstin_profile(g: str) -> dict | None`, returning `{"gstin", "valid": bool, "state_code", "state", "pan", "pan_type"}` (`pan_type` is a label or None; None for empty input)
  - `pipeline.entity_type_mismatch(name: str, pan_type_code: str) -> str | None`, which returns a reason string or None

- [ ] Step 1: Write the failing tests.
  - `gstin_profile("06AAACT1234B1Z3")` (the fixture's TCI GSTIN): `valid` is whatever `gstin_valid` says, `state == "Haryana"`, `pan == "AAACT1234B"`, `pan_type == "company"`.
  - `test_entity_type_mismatch`:
    - `("Ramesh Traders Private Limited", "P")` → a reason containing "individual".
    - `("TCI Express Limited", "C")` → None.
    - `("Acme LLP", "C")` → a reason (an LLP should be F).
    - `("Unlimited Logistics", "P")` → None.
  - `test_job_reports_pan_type_and_flags_mismatch`: patch `crust.call` so `/company/identify` returns `[]`. Run `pipeline.run_request({"vendor_name": "Ramesh Traders Private Limited", "gstin": <a valid GSTIN with P at index 5>, "request_id": "t1"})`.
    - Expect a fact whose claim is "PAN holder type" with value "individual".
    - Expect an amber flag mentioning "individual".
    - Build the valid P-GSTIN in the test by computing the checksum character with the `gstin_valid` algorithm.
- [ ] Step 2: Run `python3 -m unittest tests.test_background -v`. Expect them to FAIL (no `gstin_profile`).
- [ ] Step 3: Implement.
  - **Entity-type rule:** "private limited", "pvt ltd", "limited" or "ltd" as whole words means company (C). "llp" as a whole word means F. Return `"Name says <company|LLP> but the PAN in the GSTIN belongs to <label>"`.
  - **In `Job._run`:** when the GSTIN is valid, add the fact `("PAN holder type", label, {"type": "computed", "field": "PAN 4th character"})`. If `entity_type_mismatch(name, code)` returns a reason, add an amber flag.
- [ ] Step 4: Run the full suite with `python3 -m unittest discover tests`. Expect everything to pass.
- [ ] Step 5: Commit: "Vendor check: PAN holder type from the GSTIN, flag a company name on a non-company PAN".

### Task 2: Known vendors' GSTIN reaches the vendor check

**Files:**
- Modify: `vendorcheck/ap.py` (`check_trust`)
- Test: `tests/test_background.py` (class `KnownVendorTest`, using `fake_run_request` from `tests/test_payables.py`)

- [ ] Step 1: Write the failing test. Process the inbox with the fake. The request recorded for Shadowfax has `gstin` equal to V001's GSTIN in `vendors.json`.
- [ ] Step 2: Run it and expect FAIL.
- [ ] Step 3: Add `"gstin": v.get("gstin")` to the request. `trust_job_id` stays unchanged.
- [ ] Step 4: Run the full suite and expect a PASS.
- [ ] Step 5: Commit.

### Task 3: Background check for bills from unknown senders

**Files:**
- Modify: `vendorcheck/ap.py` (new functions before `process_file`; the `if not v:` branch)
- Test: `tests/test_background.py` (class `UnknownVendorTest`, with a temp DB, a patched `pipeline.run_request`, and bills written to a temp dir and passed to `ap.process_file(path)`)

**Interfaces:**
- Consumes: `pipeline.gstin_profile` (Task 1).
- Produces:
  - `ap.FREE_MAIL: set[str]`
  - `ap.bill_request(x: dict) -> dict | None`, returning `{"vendor_name", "website", "gstin", "request_id"}`, or None when there is no name and no usable domain
  - `ap.check_unknown_vendor(x: dict) -> tuple[list[dict], dict | None]`, returning checks and the report

- [ ] Step 1: Write the failing tests.
  - `test_unknown_vendor_is_looked_up_and_held`:
    - Bill text has `Vendor: Delhivery Limited`, a `From:` at `delhivery.com`, `Invoice No: DL-1`, and `Grand Total: ₹10,000.00`.
    - The fake returns CLEAR with `entity {"name": "Delhivery Limited"}`.
    - Expect state `held`, one `background_check` check with result `warn` and weight 0, and `decision.trust_report.verdict == "CLEAR"`.
    - Expect the request website `delhivery.com` and the name `Delhivery Limited`.
  - `test_unknown_vendor_flagged_by_lookup_fails_the_check`: the fake returns HOLD with a red flag. Expect `background_check` to be `fail` with weight 20, and the bill held.
  - `test_free_mail_is_not_a_website`: sender `billing@gmail.com`. Expect request `website` to be None and the name to be used.
  - `test_nothing_to_look_up`: no name, account name, GSTIN or domain. Expect no `run_request` call, a `background_check` info check saying there was nothing to look up, and the bill held.
  - `test_gstin_only_bill_gets_offline_profile_without_a_call`: Review Focus 1.
  - `test_lookup_error_holds_instead_of_crashing`: the fake raises `ValueError` once and `crust.ApiError` once. Both give `unavailable` and held.
  - `test_same_business_reuses_one_lookup`: two bills with different invoice numbers. Expect identical `request_id`s.
  - `test_account_name_matches_legal_name_ignoring_suffixes`: Review Focus 4. Expect check `account_name` as `pass` or `warn` (warn weight 15).
- [ ] Step 2: Run and expect FAIL.
- [ ] Step 3: Implement.
  - **`bill_request`:**
    - Name: `vendor_name_text`, else `account_name`.
    - Domain: `sender_domain`, unless it is in `FREE_MAIL`.
    - `request_id = "bill:" + sha256(f"{name}|{domain}|{gstin}".lower())[:12]`.
  - **`check_unknown_vendor`:**
    - Nothing usable: return the info check.
    - Name and domain both missing but a GSTIN present: return an info check from `gstin_profile`. Use `fail` with weight 60 when the GSTIN is invalid, with evidence for state, PAN and type.
    - Otherwise call `pipeline.run_request` and catch `Exception`, which becomes `unavailable` with weight 50.
    - Map the verdict:
      - CLEAR: `warn`, weight 0. Wording: "<entity> appears to be a real company (…), but it is not a vendor on file. Add it to the vendor master before paying."
      - HOLD, REJECT or CANNOT_VERIFY: `fail`, weight 20, with the reasons.
    - Evidence: the report's facts, as in `check_trust`.
    - When the report has `entity.name` and the bill has `account_name`: add an `account_name` check. It passes when `norm_tokens` are equal, and is `warn` with weight 15 otherwise.
  - **`process_file`, unknown branch:** extend `checks` and pass `trust=report` to `_finish`. The state stays `held`.
- [ ] Step 4: Run the full suite and expect a PASS.
- [ ] Step 5: Commit: "Background check on bills from unknown senders".

### Task 4: Docs, coordination, live verification

- [ ] **README:** add a "Bills from unknown senders" bullet covering the lookup, the held state, cost and caching, and update the test count.
- [ ] **Coordination:** message the background-check session that step 4 of its plan, and the GSTIN pass-through, are now on main.
- [ ] **Verification:**
  - Run the full suite offline.
  - Run the server smoke test with no keys: the unknown bill is held as unavailable.
  - Do one live run with the real key on one real business that is not on file, using name and domain only, to confirm the facts are real and cited. Record the cost.
- [ ] **Merge:** push, open a PR, check for clashes with main, merge, restart :8081.
