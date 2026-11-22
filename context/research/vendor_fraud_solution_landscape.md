# How Vendor Fraud Is Solved Today: HighRadius and the Wider Field

Prepared 22 November 2026. Companion to `trust_compliance_agent_deep_dive.md`.

Covers:
1. What the three HighRadius pages say and how HighRadius approaches the problem.
2. How other products in the field solve the same problem (Trustpair, Eftsure, Ramp, Persona, Corpay, SAS/SAP analytics, the Indian KYB stack).
3. The principles all of them share.
4. Why GSTIN alone is not enough, and how to use it properly.
5. What this changes in our agent design.

All vendor figures below are the vendors' own claims unless stated otherwise.

---

## 1. HighRadius: what the three pages say

### 1.1 Caliber AP case study

| Item | Content |
|---|---|
| Problem | "$3 million in duplicate payments annually from both unintentional errors and intentional fraud that its traditional AP systems could not detect." |
| Scale | "over 8 million invoices annually from 1,800+ locations", giving "inconsistent data formats, duplicate supplier records, and frequent data capture errors". |
| Knock-on effects | Long month-end close, general ledger errors, frequent vendor disputes, all traced to bad data entering upstream. |
| Fix | Invoices auto-imported into one place, and **duplicate invoices flagged before they are posted**. |
| Result | "$2M saved annually", "12,360 duplicate payments avoided", "8M invoices auto-imported annually". |
| Key quote | "flagging duplicate invoices before they are posted … far more effective … than attempting to recover funds after they have already been paid." |

Caveat: the page's company blurb describes Caliber Technologies (a software firm), but the testimonial is from Caliber Collision. The full story is behind a link that would not load. The page names "duplicate supplier records" as a problem but does not say how it was fixed.

**Lesson:** stop the bad record or payment *before* money moves. Recovering money afterwards is the expensive path.

### 1.2 Financial reconciliation guide

Reconciliation is defined as "comparing two sets of records", usually internal ledger entries against an external source such as a bank statement. A difference is a variance to investigate. HighRadius's five steps:

1. **Ingest** internal records and external records.
2. **Standardise** dates, currencies and reference fields.
3. **Match** one-to-one, one-to-many (a lump-sum deposit against five invoices) or many-to-many.
4. **Handle exceptions.** Unmatched items are classified as a timing difference, a data-entry error, a missing transaction, or one needing an adjusting entry.
5. **Certify and sign off.** A maker-checker review leaves a system-generated audit trail.

On fraud, the guide says reconciliation "surfaces unauthorized transactions and duplicate payments", and reconciling more often shortens the window in which fraud goes unnoticed. Product claims: 90% auto-match rate and AI agents that "learn from past reconciliations".

**Lesson:** verification *is* reconciliation. Compare a claimed record against an independent record. A match counts as evidence; a mismatch becomes an exception that a person reviews.

### 1.3 Vendor fraud blog

| Fraud type | How it works | Red flags named |
|---|---|---|
| Fake vendor creation | Shell vendors with "official-looking credentials" (tax IDs, addresses, forged bank letters), often with a name close to a real supplier, set up by insiders with access to the vendor master | Business name ≠ bank account name; mismatched tax ID, address or bank details |
| Duplicate invoicing | Same invoice resubmitted with a changed number, date or amount | Same amount repeated days apart under different IDs |
| Invoice inflation | Higher unit prices, undelivered goods, vague "miscellaneous" lines | Round totals, vague descriptions |
| Kickbacks and collusion | Insider approves padded invoices for rewards | Invoices split to stay under approval thresholds |
| Check tampering | Intercepted or altered cheques | — |
| (Across all types) | — | Urgent same-day payment requests from new vendors; **requests to change payment accounts by email**; dormant vendors suddenly active |

Controls the blog recommends:
- separate people for onboarding, approving and paying
- vendor onboarding that checks tax ID, legal registration and **bank account ownership**, and screens OFAC and watchlists
- quarterly cleanups of the vendor master file, plus surprise audits
- approval levels by amount
- real-time duplicate flagging
- "**Vendor changes trigger automatic validation workflows**"
- a digital audit trail
- three-way match (invoice, purchase order, goods receipt)
- machine-learning anomaly detection on payment patterns
- reconciling supplier statements

Statistics it cites: nearly 10% of fraud incidents start at onboarding (PwC 2023), about 40% of occupational fraud involves collusion, and median billing-fraud loss is $100,000 (ACFE).

### 1.4 How HighRadius approaches the problem overall

HighRadius treats vendor fraud as a **process-control problem inside accounts payable**:
- clean data at the point of entry
- one place for all invoices
- duplicate and anomaly detection before posting
- matching against purchase orders and receipts
- re-checks triggered by any vendor change
- reconciliation to catch what got through
- approvals and audit trails throughout

Its weakest area is external identity. The material says "KYC/KYB checks" but gives no detail. That is the gap our track fills.

---

## 2. How others solve it

| Player | Core idea | Mechanism |
|---|---|---|
| **Trustpair** (EU/US; inside SAP, Oracle, Coupa, Kyriba) | Verify that the **bank account belongs to the vendor**, and keep checking | Automated ownership checks in 190 countries; continuous monitoring from onboarding through vendor-master changes to payment; external data plus pooled vendor data and payment history. Argues a penny drop alone is not enough. |
| **Eftsure** (Australia/NZ) | **Safety in numbers**: a shared network of verified vendor details | Checks business registration and bank account; **sources the vendor's phone number independently for call-backs**, in case the vendor's email is compromised. If many companies independently verified and paid the same details, they are treated as correct. A fraudulent account caught for one customer triggers alerts to every customer paying that vendor. Claims ~25% of vendor-master records are anomalous. Admits fraud "rarely starts at the bank screen". |
| **Ramp** (May 2026) | KYB in three layers | Verify before onboarding (registries, tax registration, email domain, bank ownership), then monitor, then automate. Apply it **even to well-known companies**: fraudsters borrow real names. Red flags include vendor bank accounts matching employee details, dormant vendors suddenly paid, and PO-box-only addresses. |
| **Corpay** | The account is the attack surface | "A real company with an unverified account is as dangerous as a fake company." |
| **Persona** (KYB) | Registry plus the people behind the company | Official registries, legal status and address, ultimate beneficial owners confirmed from an authoritative source, sanctions and adverse media. Keeps an audit trail of documents and decisions. |
| **SAS / SAP fraud analytics** | **Link analysis** across master data | Match employee and vendor bank accounts and addresses. One SAS study found 7,216 vendors with different names sharing an address and 4,745 sharing a bank account, though bad data can cause the same pattern. Also flags a vendor whose profile does not fit the work, such as a residential address and no employees on a large contract. |
| **Indian KYB stack** (Perfios-Karza, Signzy, AuthBridge, Probe42; ~₹1–5 per check at volume per one guide) | Cross-check government identifiers | Check GSTIN yourself on the portal, not from the vendor's PDF. The GSTIN must contain the vendor's PAN. Check MCA status, directors and last filings, plus Udyam. **Bank account holder name (penny drop) must match the legal name from MCA/GSTIN.** Search by PAN, GSTIN and bank account before creating a vendor. Fuzzy name match with a manual review queue. |
| **AFP 2026 survey** (US treasury teams) | Context | 76% of organisations faced attempted or actual payments fraud in 2025. Business email compromise hit 74% (up from 63%). Vendor impersonation is rising. Only 17% use AI against payments fraud. |

---

## 3. The common principles

Every solution above, HighRadius included, applies the same idea:

> **Do not trust what the vendor tells you. Check each claim against a record the vendor does not control. Make sure all the identifiers point to the same entity. Pay only into an account proven to belong to that entity. Check again whenever anything changes. Send mismatches to a person and keep the trail.**

Broken into seven principles:

| # | Principle | Why it works | Who does it |
|---|---|---|---|
| 1 | **Independent source.** Never verify a claim with material the claimant supplied. | A fraudster controls their own documents, website and email, but not the registry, the bank's KYC record or a news archive. | Indian KYB ("check the GSTIN yourself"), Eftsure (independently sourced phone number), Persona (authoritative ownership source) |
| 2 | **Binding.** Identifiers must agree with each other, not just exist. | Each identifier on its own can be real yet borrowed. Making name, GSTIN, PAN, registry, bank holder, domain and director all agree is much harder. | HighRadius red flags (business name ≠ bank name), Indian KYB (GSTIN contains PAN; bank name = MCA name) |
| 3 | **Follow the money endpoint.** The bank account is what the fraud targets. | Every scheme ends in a payment into an account the fraudster controls. | Trustpair, Eftsure, Corpay, penny drop |
| 4 | **History is expensive to fake.** Weight evidence built up over time. | A website takes a day and a company a week. Years of filings, headcount history, news and an aged domain cannot be made quickly. | Ramp (dormant vendors), SAS (profile fit), registry filing history |
| 5 | **Verify on change, not just at onboarding.** | Most losses come from changes to a vendor that was once legitimate: bank-detail change requests by email, vendor impersonation. | HighRadius ("vendor changes trigger automatic validation"), Trustpair, Eftsure network alerts, AFP |
| 6 | **Compare against everything you already hold.** | Near-duplicate vendors and insider shells only show up against the existing vendor master and employee records. | HighRadius Caliber (duplicate supplier records and invoices), SAS/SAP link analysis, Indian KYB (search before create) |
| 7 | **Prevent before money moves. Exceptions go to people, with a trail.** | Recovery after payment is costly and often fails. Judgement belongs to accountable people. | HighRadius (flag before posting, maker-checker, audit trail), every product's review queue |

Principle 7 also matches the hackathon rules: "a person approves only the actions that cannot be undone", "each claim has a source", and "cannot verify" when sources do not settle a question. The industry pattern and the judging criteria point the same way.

### 3.1 How cheap is each identifier to fake?

| Identifier | Cost to fake or borrow | Evidence weight |
|---|---|---|
| Company name | Free (copy a real one) | Very low |
| Website and domain email | A day, a few thousand rupees | Low. Domain **age** is higher. |
| Company page and staff profiles | Cheap to create; **years of headcount history** are not | Low for existence, high for history |
| GSTIN number | Free to **copy** a real one; a front registered on a stolen or rented identity costs a racket some effort | Medium, and only when bound to name, PAN, address and bank |
| Company incorporation (CIN) | Days. **Aged shell companies can be bought.** | Medium. Check **director appointment dates** against incorporation date. |
| Bank account in the legal entity's name | Needs bank KYC for that entity (mule accounts exist) | High, when the holder name matches the registry name |
| Registry filing history, GST return history | Years | High |
| Independent news coverage over time | Years | High |
| Several independent companies paying the same details (Eftsure network) | Very high | Very high, but needs a network we do not have |

**Design rule:** weight hard-to-fake evidence above easy evidence. A front that is real on every cheap axis and empty on every expensive one is the classic shell pattern.

A new red flag from this: the **aged shell**. Old incorporation date, but a recent director change, a recent domain and a dormant filing history. Someone bought an old company to borrow its age.

---

## 4. GSTIN: what can be faked, and how to use it properly

### 4.1 You are right that GSTIN alone is not enough

A GSTIN is hard to invent, because it has a check character and the portal will reject a number that does not exist. The weakness is **what a real GSTIN proves**. There are three attacks:

1. **Borrowed GSTIN (impersonation).** The fraudster puts a real company's GSTIN on their own invoice or onboarding form. The portal says "Active", but it is someone else's registration. The blog's "near-duplicate name of a legitimate supplier" scheme is this.
2. **Front registered on a stolen or rented identity.** Rackets collect identity papers from people promised jobs or loans and register firms in their names. India's first nationwide drive (May–July 2023) found **21,791 registered entities that did not exist**. The second (Aug–Oct 2024) found about **10,700 bogus firms** and ₹10,179 crore of evasion. Later cases include a Gurugram racket that applied for 1,400 fake firms and a ₹645 crore scheme run through 229 bogus firms (Nov 2025). These GSTINs are **genuine and active**, and the firm behind them is still a front.
3. **Status changes after onboarding.** A GSTIN can be suspended or cancelled later, and an invoice dated after cancellation is invalid.

Since 2025, GST registration has used risk-based **biometric Aadhaar authentication** and physical verification for flagged applicants. Reports say it now covers all states. This raises the cost of attack 2 but does not remove it, and it does nothing against attack 1.

### 4.2 Use GSTIN as one link in the chain, not as the proof

Checks from cheapest to strongest:

| Check | How | Catches |
|---|---|---|
| Format and check character | 15 characters = 2-digit state code + 10-character PAN + entity number + `Z` + a check character computed from the first 14. Computed locally, free, no API. | Typos, invented numbers |
| **PAN-type vs claimed legal form** | The 4th character of the embedded PAN gives entity type: `C` company, `F` firm/LLP, `P` individual (proprietor), `H` HUF, `T` trust, and so on. Computed locally, free. | A "Pvt Ltd" whose GSTIN is a proprietor's (`P`) is a mismatch at no cost |
| State code vs claimed address state | First 2 digits. Local, free. | Address and registration in different states |
| Embedded PAN vs PAN on the form, bank form and registry | Local comparison | Borrowed GSTIN, mixed-up documents |
| Portal record (status, legal name, trade name, registration date, constitution, principal place of business) | GST "Search Taxpayer" (CAPTCHA), a verification API, or a fetched public lookup page | Cancelled or suspended GSTINs, name mismatch, recently registered entity |
| **Legal name = MCA name = bank holder name = name on website** | Cross-source comparison (binding) | Impersonation: a borrowed GSTIN fails here |
| Registration date vs claimed history | Compare with website claims and domain age | "Serving since 2009" with a 2026 registration |
| Search by PAN (lists all GSTINs on that PAN) | Portal or API | Claimed multi-state presence that does not exist |
| Return filing history (GSTR-1/3B regularity) | Third-party trackers; official access needs the taxpayer's consent | Dormant or non-filing fronts |
| **After the first invoice:** e-invoice IRN/QR verified on the portal, and the invoice appears in your GSTR-2B | e-invoice portal or app, GSTR-2B | Proves the vendor reported *your* invoice to GST. Strong evidence of real trade. Not every supplier must e-invoice, so a missing IRN is not proof of fraud on its own. |

In short: **a GSTIN proves that some registered taxpayer exists. Binding proves that the taxpayer is this vendor. History proves it trades.**

---

## 5. What this changes in our agent

### 5.1 Recast the vendor check as reconciliation

Use HighRadius's own reconciliation shape, applied to identity:

| Reconciliation step | Vendor-check version |
|---|---|
| Internal record | The **claimed identity bundle**: name, website, email domain, GSTIN, PAN, CIN, address, bank account holder, named directors, website claims ("since 2009", "200 trucks") |
| External record | Independent sources: Crustdata, registry, GST record, RDAP, news, sanctions lists |
| Standardise | Normalise names, legal suffixes, domains, addresses, dates |
| Match | Each claim against its independent record, **plus** a cross-binding matrix (5.2) |
| Exceptions, classified | `contradicted` (hard), `inconsistent` (soft), `missing` (cannot verify), `stale` (record too old) |
| Certify and sign off | Human approval gate, maker-checker, audit trail of claims |

This also gives a clear story for the judges: "we reconcile what a vendor claims against records the vendor cannot control."

### 5.2 Add a binding matrix to the output

Each cell is `agree`, `conflict` or `missing`, with its sources.

| | Website | GST record | Registry (MCA) | Crustdata | Bank holder (if given) | Director profile |
|---|---|---|---|---|---|---|
| Legal name | ✓/✗/– | ✓/✗/– | ✓/✗/– | ✓/✗/– | ✓/✗/– | — |
| PAN / legal form | — | from GSTIN | from CIN | — | — | — |
| Address / state | ✓/✗/– | ✓/✗/– | ✓/✗/– | ✓/✗/– | — | city |
| Age / start date | claim | reg. date | incorporation | `year_founded`, headcount history | — | start date at vendor |
| Director | named | — | director list + appointment date | `people` | — | current employer |
| Domain | — | — | — | `primary_domain` | — | — |

Decision rules from the matrix:
- any **conflict in legal name or PAN across government sources** gives a hard flag (borrowed identity)
- the cheap axes agreeing while every history axis is missing gives REFER ("looks real, no history")
- old incorporation + recent director appointment + recent domain gives an aged-shell flag

### 5.3 Weight evidence by cost to fake

Give each claim an evidence class (`self_declared`, `cheap_external`, `registry`, `history`, `financial_binding`). The pillar rules require at least one claim of `registry`, `history` or `financial_binding` class before a pillar counts as `supported`. A website plus a company page is never enough for `CLEARED`.

### 5.4 Add the checks the field uses that we lacked

| New step | Source in the hackathon | Notes |
|---|---|---|
| Local GSTIN check: check character, PAN-type vs legal form, state vs address | No API, zero cost | Implement on the day. Instant hard flag on mismatch. |
| Bank holder name vs legal name | Holder name from client records or the vendor's submitted details; penny drop only in **test mode**, behind approval | A live penny drop sends ₹1 to a real account. Under the rules (test-mode payments, approval before irreversible actions, no contact with real people), use test mode or client-supplied data. |
| Search the existing vendor master by PAN, GSTIN, bank account, domain, normalised name, address | Client records (kickoff) | Duplicates (Caliber) and shared-account links (SAS) |
| Employee ↔ vendor link check (bank, address, phone) | Only if the client data includes employees | Insider shell vendors |
| **Re-verify on change** | The PDF says organisers may push "an updated carrier record" mid-build | A changed bank account or address on an existing vendor triggers re-verification and a **hold** until a person approves. This is the main attack vector in the AFP data. |
| Aged-shell check | Registry director appointment dates, RDAP, filing history | New red flag |
| Post-onboarding proof of trade (optional) | e-invoice IRN / GSTR-2B | Overlaps with the Buying and paying track |

### 5.5 What we cannot copy, and what to say instead

Eftsure and Trustpair's strongest asset is a **network**: many companies verifying and paying the same vendor. We do not have one. In the system-design answer, say that a multi-client version would pool verified vendor details across clients, keeping the data separate and each client's consent. That is the moat at 100x scale.

### 5.6 A possible two-track angle

HighRadius's material sits across two hackathon tracks. **Trust and compliance** covers vendor onboarding, identity and screening. **Buying and paying** covers duplicates, reconciliation and "bank-detail fraud". The bridge between them is **re-verifying a vendor when its bank details change**. A team joining both tracks could present: vendor cleared at onboarding → bank change request arrives → agent re-verifies, holds the payment and asks for approval.

---

## Sources

HighRadius
- [Caliber AP case study](https://www.highradius.com/en-gb/resources/case-studies/caliber-ap/)
- [What is financial reconciliation?](https://www.highradius.com/resources/Blog/what-is-financial-reconciliation-guide/)
- [How to detect and prevent vendor fraud in AP](https://www.highradius.com/resources/Blog/vendor-fraud/)

Other solutions
- [Trustpair: vendor fraud prevention](https://trustpair.com/gb/vendor-fraud-prevention/), [Trustpair: account validation](https://trustpair.com/account-validation-compliance/), [Trustpair: why penny drop is insufficient](https://trustpair.com/?p=13293), [Trustpair on SAP Store](https://www.sap.com/store/trustpair-on-sap-store-linking-trust-with-verified-data-to-thwart-vendor-fraud.html)
- [Eftsure FAQ](https://www.eftsure.com/faq/), [Eftsure supplier information](https://www.eftsure.com/en-au/supplier-information/)
- [Ramp: Your vendors aren't who you think they are (May 2026)](https://ramp.com/leading-indicators/your-vendors-arent-who-you-think-they-are)
- [Persona: vendor verification in procurement (Jul 2026)](https://withpersona.com/blog/vendor-verification-procurement/), [Persona: UBO verification](https://withpersona.com/blog/making-kyb-feel-like-kyc-with-intelligent-ubo-verification)
- [Corpay: vendor verification and banking validation](https://www.corpay.com/pt/resources/blog/vendor-verification-banking-validation)
- [SAS: stop contract and procurement fraud](https://www.sas.com/en_us/insights/articles/risk-fraud/stop-contract-and-procurement-fraud.html), [SAP: employee and vendor bank data detection](https://help.sap.com/saphelp_fra110/helpdata/en/0f/dc5e537bdca548e10000000a423f68/content.htm)
- [AFP 2026 Payments Fraud and Control Survey press release](https://www.financialprofessionals.org/about/learn-more/press-releases/Details/over-75-percent-of-us-firms-experienced-payments-fraud-in-2025-while-ai-adoption-for-fraud-mitigation-lags), [Nacha: BEC attempts rose sharply in 2025](https://www.nacha.org/news/business-email-compromise-attempts-rose-sharply-2025-report-finds)

India: KYB, GST and bank verification
- [B2B lending India: KYC and KYB vendors](https://javajack.github.io/b2b-lending-india/04-integrations/kyc-kyb-vendors/)
- [CAclubindia: five checks before you pay a new vendor](https://www.caclubindia.com/articles/five-checks-before-you-pay-a-new-vendor-56540.asp)
- [India Briefing: guide to vendor onboarding in India](https://india-briefing.com/news/guide-to-vendor-onboarding-in-india-34332.html)
- [HyperVerge: penny drop verification, fraud, RBI rules](https://hyperverge.co/blog/penny-drop-fraud/), [Razorpay: reverse penny drop](https://razorpay.com/docs/x/fund-accounts/reverse-penny-drop/)
- [Outlook Business: fake registration drive finds 10,700 bogus firms](https://www.outlookbusiness.com/news/fake-registration-drive-gst-officers-detect-10700-bogus-firms-over-rs-10k-crore-evasion)
- [CBIC Instruction 01/2023 on fake registrations](https://gstcouncil.gov.in/sites/default/files/2024-06/gst-ins-01-2023.pdf)
- [CAclubindia: second all-India drive](https://www.caclubindia.com/articles/fake-gst-registrations-second-all-india-drive-from-16th-august-55612.asp)
- [Moneylife: ₹645 crore ITC scam via 229 bogus firms](https://www.moneylife.in/article/rs645-crore-gst-itc-scam-syndicate-used-229-bogus-firms-delhi-kingpin-arrested/78838.html)
- [Daily Pioneer: CGST Gurugram busts fake registration racket](https://dailypioneer.com/news/cgst-gurugram-busts-fake-gst-registration-racket)
- [ThePrint: GST fraud and shell firms](https://theprint.in/opinion/gst-fraud-shell-firms-input-tax-credit-enforcement/3066676/)
- [MahaGST advisory: biometric Aadhaar authentication for GST registration](https://www.mahagst.gov.in/public/uploads/gstnadvisory/1760531920_291%20Advisory%20for%20Biometric-Based%20Aadhaar%20Authentication%20and%20Document%20Verification%20for%20GST_1.pdf), [CBIC Rule 8](https://taxinformation.cbic.gov.in/content/html/tax_repository/gst/rules/cgst_rules/active/chapter3/rule8_v1.00.html)
- [ClearTax: checking vendor compliance in e-invoicing](https://cleartax.in/s/vendor-compliance-e-invoicing), [ClearTax: QR code verify app](https://cleartax.in/s/qr-code-verify-app-e-invoicing)
