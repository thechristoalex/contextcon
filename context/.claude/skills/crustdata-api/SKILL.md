---
name: crustdata-api
description: Use when calling the Crustdata API (api.crustdata.com) for company, person, job, web, or social-post data — searching or enriching companies and people, resolving a domain/name/profile URL/email to a record, finding job postings, web search or page fetch, batch jobs, watchers, or agent search. Also use on Crustdata errors (400 "Unsupported columns", ">=" operator rejected, 401, 402 insufficient_credits, 403 permission_error, 429 rate_limit_error, invalid cursor) or when estimating Crustdata credit cost.
---

# Crustdata API

## Overview

Crustdata is a REST API over indexed B2B datasets (companies, people, jobs, social posts) plus live web search/fetch. One base URL, one auth scheme, one filter grammar shared by every `/…/search` endpoint.

**Core principle:** check what the key can do (free), resolve the entity cheaply (identify / search), then enrich only what you keep, and log what every call cost.

Docs are versioned `2025-11-01`. Every page is fetchable as markdown: `https://docs.crustdata.com/<path>.md` (index: `references/docs-index.md`). Live and enterprise-only endpoint pages redirect to a login, so their request schemas are not public.

## Every request

```
POST https://api.crustdata.com/<path>
authorization: Bearer $CRUSTDATA_API_KEY
x-api-version: 2025-11-01          # required; GET /account/* returns 400 without it
content-type: application/json
```

- Read the key from `CRUSTDATA_API_KEY` (env var, or a git-ignored `.env` at or above the working directory; the client script checks both). Never hard-code, print, or commit it.
- Response headers worth recording on every call: `x-credits-used` (what this call cost), `x-request-id` (look it up later with `GET /account/usage/events/{request_id}`), `x-ratelimit-limit|remaining|reset`.
- `scripts/crustdata_client.py` does all of this (stdlib only): retries 429 using `x-ratelimit-reset`, stops on 402/403, and appends each call to a JSONL cost ledger. Run `python3 scripts/crustdata_client.py --help`.

## Step 0: find out what this key can do (free)

```bash
curl -s https://api.crustdata.com/account/credits \
  -H "authorization: Bearer $CRUSTDATA_API_KEY" -H 'x-api-version: 2025-11-01'

curl -s 'https://api.crustdata.com/account/endpoints?status=enabled' \
  -H "authorization: Bearer $CRUSTDATA_API_KEY" -H 'x-api-version: 2025-11-01' \
  | jq -r '.endpoints[] | [.status, .effective_rate_limit_rpm, .base_credits, .path] | @tsv'
```

`/account/endpoints` is the authority on access, your real per-endpoint price (`base_credits`, `premium_fields`), and your RPM. Filter with `?path=/company/enrich` (exact, leading slash), `?category=Person`, or `?status=enabled`; unfiltered it is ~89 KB. Live, contact, identify, batch, and watcher endpoints are plan-gated: if a path is `disabled` here, a call returns 403. Do not design around it. Agent Search access appears here as `/agent/query`.

Observed on a self-serve Starter key (2026-11-22):
- **Enabled:** company identify/search/enrich/autocomplete, person identify/search/enrich/autocomplete, job search + autocomplete, `/social_post/search`, `/web/search/live`, `/web/enrich/live`, Agent Search.
- **Disabled:** every `…/live` people/company/job/post endpoint, all `/batch/*`, `/person/contact/enrich`, dev platform, and employee reviews.
- **Rate limits:** 5 RPM per endpoint on search/enrich/identify, 100 RPM overall.

## Pick the endpoint

| You have / want | Endpoint | Cost (list price) | Per request |
|---|---|---|---|
| Domain, name, profile URL, or ID → which company | `POST /company/identify` | 0.2 per matched identifier | 25 ids |
| Full company profile (funding, headcount, people, news…) | `POST /company/enrich` | 2 per record (+2 technographics) | 25 ids |
| Companies matching criteria or a description | `POST /company/search` | 0.03/result + premium groups | limit ≤1000 (≤100 ranked) |
| Exact filter values (industry, country…) | `POST /company/search/autocomplete` | free | limit ≤100 |
| People matching criteria or a description | `POST /person/search` | 0.03/result + premium groups | limit ≤1000 |
| Profile URL → full person profile | `POST /person/enrich` | 1 base, up to 9 | 25 URLs |
| Profile URL / email / Scholar URL → who | `POST /person/identify` | 1 per match (gated) | 25 ids |
| Emails and phone numbers | `POST /person/contact/enrich` | 1 biz email, 2 personal, 2 phone (enterprise) | 25 ids |
| Job postings | `POST /job/search` | 0.03/result; `limit:0` + aggregations for counts | limit ≤1000 |
| Web search (web, news, scholar, ai, social) | `POST /web/search/live` | 1 per page | 1 query |
| Page HTML for known URLs | `POST /web/enrich/live` | 1 per page | 10 URLs |
| Posts by text, author, mention | `POST /social_post/search` | 0.5 per post; `limit:0` free | — |
| Plain-English question → records + reasons (beta) | `POST /agent/searches` | varies; async | 1 query |
| Fresh-from-the-web data | `…/professional_network/…/live` endpoints | 2–7 per record (enterprise) | see `references/live-and-async.md` |

Free: all three `/…/search/autocomplete`, `GET /account/*`, `GET /batch/{batch_id}`, cancel/list agent searches.

Area references (exact fields, response shapes, examples):
- `references/company.md`: search, identify, enrich, autocomplete
- `references/person.md`: search (incl. semantic), enrich, identify, contact, autocomplete
- `references/jobs-web-social.md`: job search + aggregations, web search, web fetch, social post search
- `references/live-and-async.md`: live endpoints, batch jobs, watchers, agent search
- `references/docs-index.md`: every docs page URL, to fetch when this skill does not cover something

## Filter grammar (all indexed `/…/search` endpoints and discovery watchers)

```json
{
  "filters": {
    "op": "and",
    "conditions": [
      { "field": "locations.country", "type": "in", "value": ["IND"] },
      { "field": "headcount.total", "type": "=>", "value": 200 },
      { "op": "or", "conditions": [
          { "field": "funding.last_round_type", "type": "=", "value": "series_b" },
          { "field": "funding.last_round_type", "type": "=", "value": "series_c" } ] }
    ]
  },
  "fields": ["crustdata_company_id", "basic_info.name", "basic_info.primary_domain"],
  "sorts": [{ "field": "headcount.total", "order": "desc" }],
  "limit": 50
}
```

A single condition can be passed directly as `filters` without a group.

| Operator | Meaning |
|---|---|
| `=` `!=` | exact (case-insensitive on text) |
| `<` `=<` `>` `=>` | numbers and `"YYYY-MM-DD"` dates. **`>=` and `<=` are rejected with 400** |
| `in` `not_in` | array value; matches if any element matches |
| `is_null` `is_not_null` | omit `value` (or send `null`) |
| `(.)` | fuzzy all-words, any order, typo-tolerant |
| `[.]` | exact contiguous phrase, case-insensitive |
| `(!)` | fuzzy exclusion |
| `contains` `not_contains` | company search only (aliases of `(.)`/`(!)`); person search rejects them |
| `has_all` | nested list fields only (technographics, SIC, `experience.employment_details.*`) |
| `geo_distance` `geo_exclude` `geo_polygon` | `{"location":"Bengaluru, India","distance":50,"unit":"km"}` or `{"points":[[lat,lng],…]}` |

Job search supports only `= != < =< > => in not_in (.) [.]` (plus geo on `location`).

## Pagination

- Response: `{companies|profiles|job_listings|posts, next_cursor, total_count}`. Loop until `next_cursor` is `null`.
- Send `cursor` with **identical** `filters`, `sorts`, `search` (and `fields` on company search). Changing them invalidates the cursor (400 `Invalid cursor`): restart without `cursor`, never retry the same one.
- Always send `sorts` when paginating a filter-only search (stable order). Never send `sorts` with a ranked `search` query (400).
- Ranked (`search.query`) company search: ≤100 per page, ≤1,000 positions total, cursor expires 20 min after the first call, `total_count` is `null`.
- Pass `next_cursor` back byte-for-byte; it is opaque.

## Errors

| Status | `error.type` / cause | Do |
|---|---|---|
| 400 | `invalid_request`: unknown field (`Unsupported columns in conditions`), bad operator, >25 ids, mixed identifier types, bad cursor | Fix the request. The message names the problem and often the valid options. |
| 401 | missing/invalid key | Check the env var. Not retryable. |
| 402 | `insufficient_credits` (or per-key monthly cap) | **Stop.** Do not retry. Surface to the operator. |
| 403 | `permission_error`: endpoint not enabled for this key, or `Access denied to fields: …` | Endpoint denied: stop. Field denied: resend with a narrower `fields` (the `metadata[0].permitted_fields` list says what is allowed). |
| 404 | identify/enrich "no data" | Treat as no match (most no-matches are `200` with `matches: []`). |
| 413 | body > 10 MB | Chunk `in` lists into 5,000–10,000 values per request. |
| 429 | `rate_limit_error` | Sleep `x-ratelimit-reset` seconds (+ jitter), retry. Batch concurrency 429 carries `Retry-After`. |
| 500 | server error (job search also returns 500 for an unknown field) | Retry with backoff a few times, then report. |

Error bodies are usually `{"error": {"type", "message", "metadata"}}`, but some older paths return `{"error": "...", "description"|"reason": "..."}` or `{"message": "..."}`. Parse defensively.

## Rate limits

Sliding window, per endpoint. Self-serve search/enrich/identify run at 5 (Starter), 15 (Standard), or 30 (Plus) RPM per endpoint, with 100–200 RPM overall. Live endpoints run at 10 RPM, autocomplete at 300 RPM (job autocomplete shares `/job/search`'s limit), and `POST /agent/searches` at 6 RPM. Pace from the headers rather than guessing, and spread calls instead of bursting. On a Starter key the autocomplete responses carried `x-ratelimit-limit: 5`, even though `/account/endpoints` listed 300. The headers are what the server enforces.

## Using results as cited evidence

Every record carries its own freshness, so a claim can name the exact field and its date:

| Data | Freshness field (verified live 2026-11-22) |
|---|---|
| Company search | `metadata.updated_at`, `metadata.indexed_at`: **name them in `fields`**. Plain `updated_at` in `fields` returns 400 |
| Company enrich | `metadata.updated_at` is selectable but came back `null`. Cite the retrieval time + `x-request-id` instead |
| Person search | `metadata.updated_at` |
| Person enrich | `updated_at`: **only returned when listed in `fields`** |
| Person identify | `updated_at` (always returned) |
| Job | `metadata.date_added` (posted), `metadata.date_updated` (refreshed) |
| Web fetch | `timestamp` (seconds), plus per-URL `http_status`. Web search `timestamp` is **milliseconds** |
| Social post | `date_posted` |

Indexed data refreshes every 1–30 days (posts up to 45). Record the source as: endpoint, `x-request-id`, the field path, the field's value, and that record date (or the retrieval time when the record has none). If a record is older than your workflow allows, re-check it with a live endpoint (if enabled) or web fetch.

## Common mistakes

| Mistake | Fix |
|---|---|
| Taking `matches[0]` from a **name** identify | A name lookup returns many lookalikes: `names: ["Serve Robotics"]` returned **25** candidates (Serve Robotics 1.0, Site Serve Robotics 0.8, Serve Legal 0.6, …), and the docs show ties at 1.0. Prefer domain or profile URL. With several plausible matches, disambiguate on `primary_domain`, location, and headcount, or report it as unresolved. |
| Citing a web-search `url` as the source | News results can be `https://www.google.com/goto?url=…` redirect links. Fetch the page (`/web/enrich/live`) and cite the real URL and passage. |
| Guessing filter values (`"India"` vs `"IND"`, industry labels) | Autocomplete first (free), then reuse `value` verbatim. Company country accepts ISO-3 or full name. Person `basic_profile.location.country` needs the full name. |
| Omitting `fields` | Enrich returns only `basic_info` (company) or `basic_profile`+`social_handles` (person), while search returns large payloads. Always name the sections you need. |
| Filtering person search on a returned path | Some returned fields filter under another name (`basic_profile.current_title` → `experience.employment_details.current.title`). See `references/person.md`. |
| Expecting technographics/competitors/roles in company **search** results | Filter-only fields. Get the values from `/company/enrich` with `fields:["technographics"]`. |
| Mixing growth keys | Search: `headcount.growth_percent.{1m,3m,6m,12m}`. Enrich: `{mom,qoq,six_months,yoy,two_years}`. |
| Retrying 402/403 in a loop | Both are terminal for that request body. |
| Matching web-fetch results by index | Order can differ from the request. Match on `url`. A failed fetch has `success:false` and `url:null`. |
| Assuming identify/enrich no-match is an error | It is `200` with `matches: []` (person: `match_status: "not_found"` or `"redacted"`). Unmatched ids are not billed. |
| Paying twice for premium groups | In search, filtering on a premium group bills once and receiving it bills again, per result. Request only what you read. |
