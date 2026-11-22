# ContextCon 05 — BITS Pilani (22 Nov 2026) — working context

Source: participant dashboard (crustdata.com/contextcon/bits-pilani-2026/dashboard), Rules.PDF, ContextCon05_BITS_Problem_Statement.pdf.

## Hard deadlines
- 10:15 build began (public repo history must start after this)
- 16:30 submission form opens · 17:30 last call · **18:00 code freeze + form closes**
- 19:30 10 finalists announced · 19:45 finals: 2 min demo + 1 min judge Q&A

## Dashboard judging (100)
1. End-to-end execution — 25: real request in, finished work out, no manual steps except irreversible approvals
2. Facts and verification — 20: live data, exact citations + dates, says "can't verify" instead of guessing
3. Robustness and edge cases — 20: messy data, timeouts, crashes, idempotent (never double-charge / double-send), approval on irreversible actions
4. Customer value and idea — 20: a real paid outcome, not an LLM wrapper
5. Demo and understanding — 15: trade-offs, edge cases, cost per run, roadmap
Ties: end-to-end, then facts.

(PDF version also has: hidden test cases incl. duplicate record / lookalike name / cannot-verify; requirement change at hour 3; kill-on-stage restart; live fix; ethics 25 + finance 25 judges.)

## Tracks (dashboard) with sample requests and suggested Crustdata route
1. Hiring — 1.1 Search Desk (3 manager lines → first call), 1.2 Career Check (every CV line vs public record). Route: Person Search → Person Enrich → Watcher
2. Investing — 2.1 Pre-Round Scout (who raises next, replayed from a past date), 2.2 Diligence Desk (deck claims checked). Route: Company Search → Company Enrich → Person Enrich
3. Research and Markets — 3.1 Market Map, 3.2 Research Desk (dated sourced answer or false premise). Route: Job Search → Company Enrich → Web Search & Fetch
4. Insurance — 4.1 Underwriting Desk (quote/refer/decline vs verified records). Route: Company Identify → Company Enrich → Web Search & Fetch
5. Trust and Compliance — 5.1 Vendor Check (real, active, run by its director), 5.2 Adverse Media Screen (directors vs news, clear namesakes). Route: Company Identify → Person Enrich → Web Search & Fetch
6. Buying and Paying — 6.1 Payables Desk (invoices paid once, duplicates, bank fraud). Route: Company Search → Company Identify → Company Enrich
7. Agents That Spend — 7.1 Research Buyer (buy data inside a budget). Route: Web Search & Fetch → Company Identify
8. Supply Chain — 8.1 Sourcing Desk (real makers, quotes, recommend order). Route: Company Search → Company Identify → Web Search & Fetch
9. Revenue — 9.1 Outbound Desk (meetings on 30-day triggers), 9.2 Inbound Desk (route lead in 2 min). Route: Job Search → Person Search → Watcher
10. Infrastructure for Agents — 10.1 Evidence Ledger, 10.2 Sign-Off Gate. Route: MCP Server → Watcher

## Crustdata access (tested)
- Base https://api.crustdata.com, headers: authorization Bearer, x-api-version 2025-11-01
- Promo CONTEXTCON-BITS redeemed (+$4,500 usage)
- company/identify 0.2 cr per matched id · company/enrich 2 cr · web/search/live 1 · web/enrich/live 1
- Name lookups return lookalikes at high confidence (Safexpress → French company at 1.0; Shadowfax → 3 companies at 1.0)
- No Indian registry (GST/MCA) data inside Crustdata; web search links are google goto redirects
- Not on plan: batch_person_verify (hurts Hiring 1.2), person_search withholds first/last name

## Submission form
Team (Humble Fumblers), one-liner, theme, public GitHub URL, 2-min demo video (Loom/unlisted YT/public Drive), how it works, diagram URL, anything written before 10:15, member details.

## Our tracks: exact dashboard briefs (scraped 22 Nov, ~2:30 PM)
**5. Trust and compliance:** "Decide who to trust." Paid today: verification and screening firms, paid per business or person checked for a bank, payment firm or large buyer.
- 5.1 Vendor Check: prove a new vendor is real, active and run by its director. 5.2 Adverse Media Screen: screen directors against the news and clear namesakes.
- What judges look for: *fronts caught with evidence, while honest firms and innocent namesakes pass.*
- Sample request: "Here is a new vendor's name and website. Tell me whether it is a real, trading business: who runs it, how long it has existed, and anything that looks off."

**6. Buying and paying:** "An agent you can hand the books." Paid today: outsourced accounting firms that run a company's payables and close its books each month.
- 6.1 Payables Desk: pay each invoice once, through crashes and bank-detail fraud.
- What judges look for: *books an auditor can replay line by line, with no rupee paid twice or to the wrong account.*
- Sample request: "Here are this week's invoices from our accounts inbox. Match each one to its purchase order, check the vendor and its bank account, pay each one once, and hold anything unusual for my approval with the reason."

## "What every service does" (dashboard guide, in addition to the PDF)
- A source timeout must lead to a retry or a hold for a person. **Never read a source's silence as an answer.**
- The cost ledger must include **model calls as well as Crustdata calls**.
- "Rejecting an applicant" and "placing an order" also count as irreversible and need approval.
- Example citation from the dashboard: payee Swiggy Limited, from the company-name field of its MCA record, retrieved on 9 Oct 2026. Dashboard example lines: SWIGGY BANGALORE; GOOGLE CLOUD x2 (duplicate, so hold); UPI SHARMA TRADERS (cannot verify, because many businesses trade under that name; hold).
- Suggested pace: one request running end to end by 1:00 PM; README and video done by 5:30 PM.

## Crustdata error codes (dashboard)
- 401: bad key.
- **402: credits exhausted. Don't retry; check with `GET /account/credits` (free).**
- 403: endpoint not on the plan.
- 429: wait the number of seconds in `x-ratelimit-reset`, then retry.
- No results: loosen filters and check exact values with autocomplete.

## Finals: what the judges want to hear (2-minute demo)
1. The service, who pays for it today, and why you picked it.
2. The business case: buyer, **price in ₹**, cost to run one job.
3. The approaches you considered and why you chose yours.
4. What worked and what didn't.
5. The hardest case and how the product handles it.
6. How you tested it and how you know it works.
7. What you left out to ship in 7h45m.
8. What comes next, and why now isn't the time.
- If possible, show more than the prepared request: a duplicate record, a changed fact, a failing call, a restart mid-run. Expect a scale question (100× volume, or a second client).
- Open with the request in the customer's words and let it run to the end. If anything breaks, play the demo video.
- **The 10 finalists are picked from the submissions, so the demo video matters a great deal.**

## README sections required by the submission form
The service (who does it today, who pays) · What it does now (and where it still needs a person) · What it could do in the future · The future of this service · How to run it from a fresh clone · How it's built (agents, models, tools, data, where Crustdata is called, where state lives) · Its sources and checks (each data source, approval points, cost ledger) · The approaches you tried.
Form also asks: one-liner, theme, public repo, 2-min video link (Loom, unlisted YouTube, or public Drive file, **not a folder**), "how does it work", public diagram link, anything written before 10:15, and each member's name, BITS email, YC email, BITS ID, personal email and GitHub username.

## Prizes
$8k / $3k / $1k in Crustdata credits. The top 3 get office hours with Jon Xu (YC GP). Crustdata PPIs for standout builders. People's Choice voting runs until 8:30 PM.
