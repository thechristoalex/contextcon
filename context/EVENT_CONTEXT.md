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
