# Payables Desk: Vendor Trust + Pay-Once

ContextCon 05, BITS Pilani · Team Humble Fumblers · Tracks **5. Trust and compliance** + **6. Buying and paying**

An accounts-payable agent for an Indian 3PL. For each invoice it verifies the vendor against live
company, web and news data, runs a three-way match against the PO and goods receipt, and checks for
duplicates, bank-detail changes, look-alike senders and prompt injection. It then **pays exactly once
after a person approves**, or **holds the invoice with a cited reason**. It also reads paper bills
from a photo on the local machine and reconciles bank-statement lines.

Design, invariants, schema and failure model: **[ARCHITECTURE.md](ARCHITECTURE.md)**.
Diagram: [architecture (Mermaid)](https://mermaid.ink/img/pako:eNptVVFv4jgQ_isjXm5XpKDd072gUysILKCyNEdYtKtcH0wyBF8dO7IdWoT6329sJ7SV9gUHe-bzzDffjC-9XBXYG0HvINRzfmTawmrzrwRYrrOl3KuXEQzsi4UhDLAStDS1UKx4hJubW4hXWSyYMfxwHgGXJ8VzJJM9k09AWLJ0_zTmyGvrvyouC9SPDj9eeYjZz2z2YjXLLRw4isKMoECLmiy5sTwnpxJfvMfsp_fYLbNlgdLSpXBCWSg9gnm6Xa4jsEeUYNDdAYWqGJftnmQVeozd0mNMfySXKWXCc2bx7u-9Ht5KpSsmuMHimolUg8ifKQ2GENrr-qxSjbT9gnz7ycOrwyVAAoYzmoCf0cZqGY-3s8d3x1IF2haz-D51-6bZl5rVx3YLiPICa5eAtMQg5k8mAmT5kWiwjZYGmAQ8cTrPKdAT6oLn1l8BkGbp-9ThZNqA243WbJ55tgK8K5OOIBmvwZ5rjGBPFeKyBKs654oZ29YM4Hv2580zO9OmzY8jSB4iKJUqTFdlgnqgNC0woZEVZ8ITAovWe5JNnDSowIwLEwLUnOpeAMtzRyrlV8BRCZfGtWgA22wXgrG6MRb-GnyBPv1-9cd04pZt4HaafYqdEVWHkSg7pezPV0Wg1Dw_RvCMezig7T4NMp0fPwdxhnI4wPF8no3LkmRI5R6RJS-PlqqiuXkCkytNpFGqRgV-ydxVmskzHCjJYSPZiVa2Fzh8Zlp6zEW2mK2mlEKh2cFS9hUaw0r8gCAE1NRbIYjksmgqqj2ra61OTHjRjb2owhZ6u2T8K0uoPIrkMaIWTmebLTzhma7YX7vlnbz9heTknVfZp5RXjWAuopqdKydCKl6JumVlCjcDkh_qM-QuvoHzmjnGlelMKSuNplbSIBmRyD4y2vmTpmrvn27p2n9W3JH7n9oTs-6IljZM-qLhwAkvcq7Sms9v8wMa6WTm2KX5Qu0VWjOQ7KwmaZCcsZSVTyhOd2F4Te6zFZfo6KGZQ8cUuUUadf0OpQ8qzxutXbP5OdCJuqZ0sbgD88RrH8zkPkBOL6mbEw4tchQiLQEs-If-ZVpwIkrQ7Xe-kJPp--GxeNtqB8YkcVVFYuhDTxIhgli-yv2qdn9ZnwpSMuHbCA5aVW4UQq6qmsT5hwH1TLOSaA8JJL8vbXfmQzGq0aQbg9YKipUHnuNdFo_X64ct7Gab5bdfERxpjsOBwmRQo-46w8Pcwia-rGfftsPNdp4Ol9-T1AdL44Z1ySlJT4FoqdnE7nI_btCz18r9TaFeum-mTgS_M1m8meRMFySXH0l4DOYk_MRX1E29VsXtPHJa60XQq-hFYrygh_LSIxor_2QWeGCNsL3X1_8BbgldtA).

| | |
|---|---|
| **Job** | Take invoices from the inbox, verify the vendor, match to PO and goods receipt, pay each invoice exactly once, stop fraud before money moves |
| **Done today by** | The client's AP team, an outsourced accounting firm (per invoice or per month) and a KYB vendor (per business checked) |
| **Input** | `.txt`/`.eml` invoices or bill photos, vendor master, POs, goods receipts (`data/fixtures/`), bank-statement CSV |
| **Output** | `PAID` (ledger entry + idempotency key), `HELD` (cited reason + drafted call-back), `DUPLICATE` (linked to the original), `REJECTED` (not payable) |
| **Pass mark** | No invoice paid twice. No payment to an unverified account or vendor. Every fact cites a source and date. "Cannot verify" instead of a guess. A kill at any point resumes without repeating an action |

## Run

Python 3.10+, standard library only.

```bash
cp .env.example .env                       # CRUSTDATA_API_KEY=..., optional TYPESAFE_API_KEY=...
python3 -m vendorcheck.cli inbox           # process data/fixtures/inbox
python3 -m vendorcheck.cli status
python3 -m vendorcheck.cli approve "V001:2026-451" --by "AP lead"
python3 -m vendorcheck.cli scan path/to/bill.jpg
python3 -m vendorcheck.cli post-bank-lines data/fixtures/bank_lines_oct2026.csv
python3 -m vendorcheck.cli vendor --name "TCI Express" --website tciexpress.in --director "Anil Kumar"
python3 -m vendorcheck.cli serve --port 8080   # landing at /, app at /app
python3 -m unittest discover tests         # 96 tests, offline, no keys, no credits
```

Crash test: `CRASH_AFTER_APPROVE=1 python3 -m vendorcheck.cli approve <key> --by X`, then `… resume`.
The invoice is paid once. A second `resume` or `approve` changes nothing.

## How it decides

- **Entity by domain, not name.** Searched by name, Crustdata returned a French "SAFEXPRESS" at
  confidence 1.0 above the Indian one, and three "Shadowfax" records at 1.0. A name maps to a company
  only if exactly one match has confidence ≥ 0.95 and an exact name. Otherwise the result is `CANNOT_VERIFY`
  with the candidates listed.
- **Only fetched pages are sources.** A CIN, GSTIN or adverse-media hit counts only when the page,
  fetched live, states it next to the company's name. Search snippets and redirect links are never cited.
- **Adverse media needs the person and the company in the same article.** Only legal and regulatory
  terms count. Namesakes are counted and cleared, and nothing else about them is stored.
- **Rules decide, Jev restricts.** Jev (TypeSafe, `jev-1.14.0`) answers typed questions about text
  already held: document type, changed account, pressure level, text aimed at an automated reviewer,
  parked site, whether the site's business matches what is billed. It can add a hold, never clear one.
  It quotes by choosing a sentence id. Code splits the sentences and prints the one chosen verbatim.
  Tax IDs, bank numbers, phones and emails are redacted before sending. If Jev is down, the rule stands.
- **Exactly once.** Payment key = `vendor_id:normalised_invoice_no`, the primary key of `payments`. The
  insert and the state change share one transaction. The vendor record is hashed at approval and
  re-checked at payment.
- **Never billed twice.** Every API response is cached by request hash before its step is checkpointed.
  Limits per job: 12 credits, 180 s. Reaching either gives `CANNOT_VERIFY`.

A person approves every payment, judges every `HELD` item, and sends any drafted message. The system
never sends email.

## Results (live Crustdata, 22 Nov 2026)

| # | Document | Result |
|---|---|---|
| 1 | Shadowfax invoice, clean | `AWAITING_APPROVAL` → approved → `PAID` once |
| 2 | Shreeji Cargo Movers (fictitious front) | `HELD`: no company record carries the domain |
| 3 | TCI Express, director "Anil Kumar" | Namesakes cleared. A news report of a raid at a TCI warehouse (TCI was the carrier) holds the invoice with the passage cited, for a person to judge the company's role |
| 4 | Shadowfax re-sent as `INV-2026-451` (was `INV/2026/0451`) | `DUPLICATE` of `V001:2026-451` |
| 5 | "We have changed our bank" email | `HELD` + call-back draft to the contact on file, never the sender |
| 6 | TCI invoice to a new account | `HELD`: account ≠ verified; PO already invoiced |
| 7 | Invoice from `shadowfaxx.in` | `HELD`: look-alike domain, edit distance 1 |
| 8 | Kill after approval, before payment | `resume` pays once; repeats are no-ops |

| Bank line | Result |
|---|---|
| `RAZ*SWIGGY BLR` ₹1,240 | **Swiggy Limited**: one of 3 same-name records owns swiggy.com. The legal name is from the site footer, cited. Account: staff meals |
| `AWS EMEA` ₹18,500 | Amazon Web Services EMEA SARL (vendor-master alias). Account: cloud hosting |
| `AWS EMEA` ₹18,500 (again) | `HELD`: same date, descriptor and amount |
| `PAYTM*SHARMA TRD` ₹4,000 | `CANNOT_VERIFY`: 4 companies match equally |
| `NEFT/SHADOWFAX TECHNOLOGIES` ₹2,36,000 | Reconciled to the payment for invoice 1. With no approved payment it would be `HELD` |
| `UPI/DELHIVERY` ₹5,600 | Delhivery. Account: freight, express parcel |

**Bill photos** are read locally (Apple Vision on macOS, Tesseract elsewhere) at no cost. The image and
the bank details on it never leave the machine. Parties, items, CGST/SGST/IGST, IRN, e-way bill and bank
fields are extracted, then tested against each other: GSTIN check character, taxable + tax = total, total
in words = total in figures, CGST = SGST, intra- vs inter-state tax. A field that cannot be read is held,
never guessed. The bill then goes through the same checks as an emailed invoice.

## Cost and price

Every call's `X-Credits-Used` is recorded in the cost ledger. ₹25 per credit ($4,500 promo / 15,000 credits).

| Job | Calls | Measured | Proposed price |
|---|---|---|---|
| New vendor verified | 7–9 | 7.2–9.2 cr = ₹180–230 | ₹1,500 |
| Invoice, vendor already verified | 0 | ₹0 (verdict cached) | ₹60 |
| Bank line, known payee | 0 | ₹0 | ₹20 |
| Bank line, new merchant | 2 | 1.2 cr = ₹30 | ₹20 |
| Statement of 6 lines, 3 new merchants (live) | 5 | 2.6 cr = ₹65 | ₹120 |

Example month for a mid-size 3PL: 40 new vendors, 2,000 invoices and 1,000 bank lines come to ₹2,00,000
billed against about ₹12,200 of data cost. The prices are our proposal, not a market quote. Jev adds about
1,500–2,000 input tokens per document, roughly ₹0.007.

## Data sources

| Source | Endpoint | Used for |
|---|---|---|
| Crustdata | `POST /company/identify` | Domain or name → company record |
| Crustdata | `POST /company/enrich` | Type, founded, headcount, HQ, founders/CXOs, news |
| Crustdata | `POST /web/enrich/live` | Website liveness/content; verifying cited pages |
| Crustdata | `POST /web/search/live` | CIN, GSTIN, adverse media |
| TypeSafe Jev | `POST /v1/systemone` | Typed decisions about text already held. Not a source |
| Local | — | GSTIN check character, PAN holder type, state code, OCR |
| Client | `data/fixtures/*.json` | Vendor master, POs, goods receipts, chart of accounts |

## Approaches dropped

- **Name-first entity resolution.** It failed on look-alikes, so resolution is by domain first.
- **CINs taken from search snippets.** "TCI Express" gave four group-company CINs. Now the fetched page
  must state the CIN, and there must be exactly one.
- **A broad adverse-media word list.** It flagged Shadowfax for consumer complaints. Now only legal and
  regulatory terms count, and the person and the company must appear together.

## Not built

- MCA/GST registry lookups: MCA has no public API and the portals use CAPTCHAs, which we do not
  bypass. These are reported as "registry not confirmed".
- Real bank rails or penny drop. Payments go to a simulated ledger with an idempotency key.
- PEP/sanctions screening (OpenSanctions), domain age (RDAP), a Tally/Zoho connector, a provider-side
  idempotency key (Razorpay/Stripe test mode).

## Credits

No third-party code. Python standard library only. Data from the Crustdata API.

**PP Mondwest font.** `vendorcheck/static/PPMondwest-Regular.otf` is by Pangram Pangram Foundry
(https://pangrampangram.com) and is **not covered by this repository's licence**. It is used under the
foundry's free personal-use trial licence, downloaded 22 Nov 2026, for this non-commercial hackathon
project only. Copyright © Pangram Pangram Foundry. Do not reuse, redistribute or use it commercially.
For your own use, get the font and a fitting licence from the foundry. The foundry's terms take
precedence over this summary. Without the file, the app falls back to Cormorant Garamond / Georgia.
