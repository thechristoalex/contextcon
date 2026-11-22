# Company background check on every bill

## Intent
Whenever a bill lands in the inbox (email, upload or scanned photo), look up the business behind it and show only facts we can prove. Today a known vendor gets the full vendor check, but a bill from a business not on file is held as "no vendor on record" and nothing is looked up.

## Goals
- A bill from an **unknown sender** runs the existing vendor check (`pipeline.run_request`), using what the bill says about itself.
- The **known-vendor** check also checks the vendor's GSTIN against public pages. The request never passed the GSTIN, so that step was skipped.
- Two **free GSTIN checks** that need no extra API calls:
  - **Entity type:** the PAN inside the GSTIN encodes the kind of business, compared with the name on the bill.
  - **Bank account name:** compared with the legal name found.

## Non-goals and anti-goals
- No new data sources and no scraping of GST lookup sites.
- Comparing GST state with the bill's address is left out. The address isn't extracted, and head offices often sit in another state, so it would raise false alarms. The state is reported as a fact only.
- **Never pay an unknown vendor:** the bill stays HELD whatever the lookup finds.
- **Never state a fact without a source:** the existing citation rules apply unchanged.

## Approaches
- **A (picked by the user): reuse the existing vendor check for every bill, plus the free GSTIN checks above.**
- B: A plus activity signals (headcount trend, job postings, director link, domain owner). Deferred to a follow-up PR.
- C: scrape third-party GST sites. Rejected: copied and stale data, against "make sure it's correct".

## Design
1. **`ap.check_unknown_vendor(x)`** builds a request from the bill:
   - **Name:** `vendor_name_text`, else `account_name`.
   - **Website:** `sender_domain`, unless it is free mail (gmail, yahoo, outlook, hotmail, live, rediffmail, icloud, proton).
   - **GSTIN:** taken from the bill.
   - **Request id:** `bill:<sha of name|domain|gstin>`, so the same unknown business is looked up once and cached after that.
   - **Nothing to look up** (no name, domain or GSTIN): an info check says so, and no call is made.

   It returns a `background_check` check and the report:
   - CLEAR: `warn`, weight 0. It reads "this business appears real, but it is not a vendor on file".
   - HOLD or REJECT: `fail`, weight 20.
   - CANNOT_VERIFY: `fail`, weight 20.
   - Source down: `unavailable`.

   `process_file`'s unknown-vendor branch passes `trust=report` to `_finish`. The UI already renders `trust_report`.
2. **`check_trust(v)`** adds `"gstin": v.get("gstin")` from the vendor master, not from the bill. The cache key `trust_job_id` is unchanged, because it already hashes the vendor's GSTIN.
3. **`pipeline.Job`, GSTIN block: entity type.**
   - PAN 4th character: C = company, F = firm/LLP, P = individual, H = HUF, T = trust, A = AOP, B = BOI, G = government, L = local authority, J = artificial juridical person.
   - Fact: "PAN type: company".
   - Amber flag when the name says Pvt Ltd/Limited and the type isn't C, or says LLP and it isn't F. Matching is whole-word.
4. **Bank account name (unknown-vendor bills):** compare the bill's `account_name` with the legal name the check resolved, normalised (case, punctuation, "pvt"/"private", "ltd"/"limited"). A mismatch is a `warn`.
5. **Tests:** `tests/test_background.py`, offline, with a fake Crustdata.
   - Each outcome: lookup runs, bill held, report attached.
   - Free mail isn't used as a website.
   - A second bill from the same business doesn't re-query.
   - Outage leads to unavailable and held.
   - Nothing to look up.
   - PAN-type flag fires and doesn't fire.
   - Known vendor now passes the GSTIN.
6. **Coordination:** this takes over step 4 of the background-check session's plan (`HANDOFF_bgcheck.md`). That session gets a message so it isn't built twice.

## Left to the builder
Exact wording of the check reasons, and where the PAN-type fact sits among the existing facts.
