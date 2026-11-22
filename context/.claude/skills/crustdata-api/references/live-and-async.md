# Live endpoints, batch jobs, watchers, agent search

Most of this is **plan-gated**. Check `GET /account/endpoints?path=<path>` first. A disabled path returns 403 on every call.

The non-batch live endpoint doc pages (`*/live-search`, `*/live-enrich`, dev platform, employee reviews, live social posts) **redirect to a login**, so their full request schemas are not public. What follows is everything the public docs state. If you have dashboard access, read the full page at `https://docs.crustdata.com/<path>` while signed in.

## Contents
- Live (real-time) endpoints
- Batch jobs (`/batch/...`)
- Watchers (`/watch/...`)
- Agent Search (`/agent/searches`, beta)

---

## Live (real-time) endpoints

These fetch from the web at request time. Use them when an indexed record is missing or too old. Default limit is 10 RPM (5 for batch live).

| Endpoint | Cost | What is public |
|---|---|---|
| `POST /person/professional_network/enrich/live` | 7 / profile | Takes profile URLs and returns the same person data model as `/person/enrich`, but fresh |
| `POST /person/professional_network/search/live` | 2 / profile | Live people search. Sync pages plus async background jobs |
| `POST /company/professional_network/search/live` | 2 / company | 25 results per page |
| `POST /job/professional_network/search/live` | 2 / result | Freshest listings for **one** company, identified by `crustdata_company_id` |
| `POST /social_post/professional_network/enrich/live` | 1 / post (+5 per 100 reactors, +5 per 100 comments) | Exactly one of a person profile URL, a post URL, or `company_name` / `company_domain` / `crustdata_company_id` / `company_professional_network_url`. Send `page` **or** `limit`, not both |
| `POST /social_post/professional_network/search/live` | same as above | Keyword (supports OR/AND) plus filters such as `PROFESSIONAL_NETWORK_MEMBER`, `COMPANY`, `MENTIONING_COMPANY`, `AUTHOR_INDUSTRY`, `AUTHOR_COMPANY`, `AUTHOR_TITLE` |
| `POST /dev_platform/enrich` | 1 / result | Exactly one of `crustdata_person_id`, `profile_url` (developer profile), or `professional_network_profile_url` |
| `POST /employee_review/enrich` | 1 / company | Takes a `crustdata_company_id` |
| `POST /professional_network/search/autocomplete` | free | `field` ∈ `title`, `industry`, `region`, `school` and a non-blank `query`. Values feed the live search filters |

**Live search filters differ from indexed search.** They take a **flat list** of `{field, type, value}` (no `op`/`conditions`), and `field` is an UPPERCASE filter type:

```json
{ "filters": [
    { "field": "CURRENT_TITLE", "type": "in", "value": ["Head of Operations"] },
    { "field": "REGION", "type": "in", "value": ["India"] }
] }
```

- Person filter types: `CURRENT_COMPANY`, `PAST_COMPANY`, `CURRENT_TITLE`, `PAST_TITLE`, `SCHOOL`, `COMPANY_HEADQUARTERS`, `COMPANY_HEADCOUNT`, `COMPANY_TYPE`, `FUNCTION`, `INDUSTRY`, `REGION`, `SENIORITY_LEVEL`, `YEARS_AT_CURRENT_COMPANY`, `YEARS_IN_CURRENT_POSITION`, `YEARS_OF_EXPERIENCE`, `KEYWORD`, `FIRST_NAME`, `LAST_NAME`, `PROFILE_LANGUAGE`.
- Company filter types: `INDUSTRY`, `COMPANY_HEADCOUNT` (e.g. `"11-50"`), `REGION`, `COMPANY_HEADQUARTERS`, `ANNUAL_REVENUE`, `NUM_OF_FOLLOWERS`, `FORTUNE`, `DEPARTMENT_HEADCOUNT`, `DEPARTMENT_HEADCOUNT_GROWTH`, `COMPANY_HEADCOUNT_GROWTH`, `KEYWORD`, `ACCOUNT_ACTIVITIES`, `JOB_OPPORTUNITIES`. Company filters also accept `type: "between"` with an object value.
- An unknown filter type returns 400 listing the valid values.

## Batch jobs (`/batch/...`): enterprise; async; results as a file

| Submit | Max per job | Cost |
|---|---|---|
| `POST /batch/company/enrich` | 10,000 ids (one type: `names`/`domains`/`professional_network_profile_urls`/`crustdata_company_ids`) + `fields` | same as `/company/enrich` |
| `POST /batch/company/search` | 10,000 results (same body as `/company/search`) | same as search |
| `POST /batch/company/professional_network/search/live` | 1,000 (`max_results`) | 2 / company |
| `POST /batch/person/enrich` | 10,000 `professional_network_profile_urls` + `fields` | same as `/person/enrich` |
| `POST /batch/person/search` | 10,000 results | same as search |
| `POST /batch/person/professional_network/search/live` | 1,000 | 2 / profile |
| `POST /batch/person/professional_network/enrich/live` | 300 | 7 / profile |
| `POST /batch/person/contact/enrich` | 300 profile URLs, `fields` required | same as contact enrich |
| `POST /batch/person/identify` | 300 `emails` / `business_emails` / profile URLs (matches **personal** emails too) | 1 / match |
| `POST /batch/person/verify` | 10 profile URLs: work-history verdict, confidence, reasoning, cited sources | 10 / verdict |
| `POST /batch/job/search` | every listing for ≤10 companies | 0.03 / result |
| `POST /batch/social_post/professional_network/enrich/live` | 10 identifiers | as live posts |

Flow:
1. Submit, which returns `{batch_id, status: "pending", entity, action, identifier_count, entities_requested, status_url}`. Optionally pass `webhook_url`.
2. Poll `GET /batch/{batch_id}` (free; `x-api-version` not required). Status goes `pending` → `processing` → `completed` | `failed` (`error_message` set).
3. On `completed`, download `download_url` (merged) or `download_urls` (parts). These are pre-signed and **valid for 5 days**. Files are **gzipped JSONL**.
   - Enrich records: `{original_identifier, internal_id, data}`. Unresolvable ids are silently dropped, so compare `entities_requested` with `entities_fulfilled`.
   - Search records: flat, same shape as the non-batch record.
4. `GET /batch/` lists jobs (free, cursor-paginated, no download links).

Concurrency: at most 5 active jobs per pool (live pool / standard pool) by default. A sixth returns `429` with `Retry-After`. The check runs before billing, so that 429 costs nothing.

## Watchers (`/watch/...`)

Recurring feeds delivered to a webhook, Slack, Google Chat, or email. Management calls are limited to 10/min per path.

- **Discovery watch** re-runs a search and delivers *new* matches: `POST /watch/{company|person|job|social_post}/search`. It uses the same `filters` grammar as the dataset's search.
- **Entity watch** tracks a list you supply and delivers *changes*: `POST /watch/{person|company}` with `entities`, a `track` tree (when to fire), and `fields` (what to deliver).

```json
{
  "filters": { "op": "and", "conditions": [
    { "field": "locations.country", "type": "in", "value": ["IND"] },
    { "field": "headcount.total", "type": "=>", "value": 200 }
  ]},
  "config": { "trigger": { "type": "interval", "every_hours": 24 }, "max_results_per_run": 25 },
  "notifications": [ { "type": "webhook", "url": "https://your-app.example/webhooks/crustdata" } ]
}
```

- The response carries the watch `id`, `status: "active"`, and counters (`notifications_count`, `credits_consumed`).
- The first run is a **free baseline** (a sample of up to 5). Later runs bill per delivered record: discovery 0.5 (person/job) or 2 (company/post) at the 30-day SLA, entity 5 per changed record. `config.is_realtime: true` (discovery) or `refresh_frequency_days: 1` (person entity) costs **150 per record**.
- Mutable via PATCH: `status`, `config` (resend the current `trigger` too), and `notifications`. Discovery `filters`, `sorts`, and `fields` are fixed at creation. Entity watches can also update `entities`, but `track` and `fields` are fixed.
- Discovery management: `GET|PATCH|DELETE /watch/{dataset}/search/{watch_id}`, `POST /watch/{dataset}/search/{watch_id}/test`, and list with `GET /watch/{dataset}/search`.
- Entity management: `GET|PATCH|DELETE /watch/{dataset}/{watch_id}`, `POST /watch/{dataset}/{watch_id}/test`, list with `GET /watch/{dataset}`, and preview before creating with `POST /watch/{dataset}/test`.
- `DELETE` cancels permanently (204, idempotent).
- Runs, for both kinds and with no `search/` segment: `GET /watch/{dataset}/{watch_id}/runs` and `GET /watch/{dataset}/{watch_id}/runs/{run_id}/summary` (the delivered records sit under `notifications[].payload.notifications[]`).
- `config.payload_delivery_type`: `inline` (default) or `link` (one NDJSON file per run).

Full pages: `watcher-docs/company/discovery`, `watcher-docs/person/entity`, `watcher-docs/job/watch`, `watcher-docs/cross-dataset-filters`, `watcher-docs/payload-delivery`.

## Agent Search (`/agent/searches`, beta)

Ask in plain words and get records back, with per-record reasons. Use it only when the request cannot be expressed as filters, because direct search is faster and cheaper. Requires Agent Search access plus person, company, and job search.

```bash
curl -s -X POST https://api.crustdata.com/agent/searches \
  -H "authorization: Bearer $CRUSTDATA_API_KEY" -H 'content-type: application/json' \
  -H 'x-api-version: 2025-11-01' -H 'Idempotency-Key: job-123-step-4' \
  -d '{"query": "The founders of Crustdata"}'
# → 202 {"search_id": "...", "state": "running"}
```

- Poll `GET /agent/searches/{search_id}` about every 10 s until `state` ∈ `succeeded`, `failed`, `cancelled`. Treat any other state (including a future `queued`) as running.
- `output.person|company|job` holds `constraints[]`, `columns[]`, and `rows[]`. Each row has `values` and per-constraint `{constraint_id, assessment, evidence, fields}`.
- The first read after the search ends charges it (`credits_used` and `X-Credits-Used`). Failed, cancelled, and empty searches cost 0.
- Starting requires a balance of 8 credits per uncharged search (a check, not the price). Limit: 6 starts/min.
- `Idempotency-Key` makes retries safe: the same key and query return the first search.
- `GET /agent/searches/{search_id}/events` streams progress (`Accept: text/event-stream`). `POST /agent/searches/{search_id}/cancel` stops a search.
