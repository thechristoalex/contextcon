# Jobs, Web, and Social Post reference

Source pages: `job-docs/search/introduction`, `job-docs/search/reference`, `job-docs/autocomplete/introduction`, `web-docs/search/introduction`, `web-docs/search/reference`, `web-docs/fetch/introduction`, `social-post-docs/indexed-search/introduction`, `social-post-docs/indexed-search/reference`.

## Contents
- Job search: `POST /job/search` (+ aggregations)
- Job autocomplete: `POST /job/search/autocomplete`
- Web search: `POST /web/search/live`
- Web fetch: `POST /web/enrich/live`
- Social post search: `POST /social_post/search`

---

## Job search: `POST /job/search`

0.03 credits per result. A query with no results is free. Body (all keys optional, but the body itself is required; `{}` matches everything): `filters`, `fields`, `sorts`, `limit` (0–1000, default 20), `cursor`, `aggregations`.

Response: `{job_listings: [Job], next_cursor, total_count, aggregations?}`. `total_count` can be `null` on very broad queries.

Operators: `= != < =< > => in not_in (.) [.]` (+ `geo_distance`/`geo_exclude`/`geo_polygon` on `location`). **A filter on a non-indexed field returns 500** `Unsupported columns in conditions` (not 400).

`Job` record groups:
- `crustdata_job_id`: the dedupe key
- `job_details`: `title`, `category` (`Engineering`, `Sales`, `Operations`, …), `url`, `workplace_type`, `employment_type`, `source`, `reposted_job`, openings
- `company`: the hiring company's firmographics at index time (`basic_info.crustdata_company_id`, `name`, `primary_domain`, `headcount`, `funding`, …)
- `location`: the job's own location (`raw`, `country`, `state`, `city`, `district`), not the HQ
- `content.description`: full description text (`[.]` for an exact keyword, `(.)` for loose matching)
- `metadata.date_added`: **date posted** on the source portal. `metadata.date_updated` is the last refresh.

Filter on company with `company.basic_info.company_id` (an alias of `crustdata_company_id`, **not sortable**). Get the id from `/company/identify` first. Sort on `metadata.date_added` for stable pagination.

```bash
curl -s -X POST https://api.crustdata.com/job/search \
  -H "authorization: Bearer $CRUSTDATA_API_KEY" -H 'content-type: application/json' \
  -H 'x-api-version: 2025-11-01' \
  -d '{
    "filters": { "op": "and", "conditions": [
      { "field": "company.basic_info.company_id", "type": "=", "value": 631394 },
      { "field": "metadata.date_added", "type": "=>", "value": "2026-10-01" }
    ]},
    "fields": ["crustdata_job_id", "job_details.title", "job_details.url", "location.raw", "metadata.date_added"],
    "sorts": [{ "field": "metadata.date_added", "order": "desc" }],
    "limit": 50
  }'
```

### Aggregations (counts without paying for rows)

```json
{
  "filters": { "field": "job_details.category", "type": "=", "value": "Engineering" },
  "limit": 0,
  "aggregations": [
    { "type": "count" },
    { "type": "group_by", "field": "company.basic_info.company_id", "agg": "count", "size": 20 }
  ]
}
```
→ `aggregations: [{type:"count", value: N}, {type:"group_by", field, buckets:[{key, count, metadata:{company_name, company_website_domain, linkedin_id}}]}]`. `size` is 1–1000 (default 100). `group_by` fields come from a fixed allowlist, and an unsupported field returns 500 listing the valid ones.

Pulling **every** listing for ≤10 companies: `POST /batch/job/search` (async, enterprise). For real-time listings of one company: `POST /job/professional_network/search/live` (gated). Both are covered in `live-and-async.md`.

## Job autocomplete: `POST /job/search/autocomplete` (free; shares `/job/search`'s rate limit)

`{"field": "title", "query": "Software", "limit": 5}` → `{"suggestions": [{"value": ...}]}`. `field` comes from an allowlist with aliases: `title`/`job_details.title`, `category`/`job_details.category`, `workplace_type`, `company.name`/`company.basic_info.name`, `company.industries`, `company.basic_info.primary_domain`, `company.headcount.range`, `company.funding.last_round_type`, `country`/`location.country`, `state`, `city`, and `location.raw`.

## Web search: `POST /web/search/live`

1 credit per page. Available on self-serve plans. Rate limit 10/min.

| Param | Notes |
|---|---|
| `query` | required, ≤5,000 chars, supports `site:` and `filetype:` |
| `sources` | `web`, `news`, `scholar-articles`, `scholar-articles-enriched`, `scholar-author`, `ai`, `social`. **Set it explicitly**: shapes differ, and omitting it searches all of them |
| `location` | ISO-3166 **alpha-2** (`"IN"`, `"US"`) |
| `site` | domain restriction; applies to `web`/`news` only |
| `start_date` / `end_date` | Unix **seconds**, start < end; applies to web/news/scholar-articles |
| `page` | 1–15 pages aggregated, cost = N credits. `page > 1` requires exactly one source |
| `human_mode` | browser-like retrieval when bot-blocked |

```json
{ "query": "\"Acme Logistics\" warehouse fire", "sources": ["news"], "location": "IN",
  "start_date": 1756684800, "end_date": 1764547200 }
```

Response: `{success, query, timestamp (ms), results: [...], metadata: {total_results, failed_pages, empty_pages}}`.

| `source` | Result fields |
|---|---|
| `web`, `news`, `social` | `title`, `url`, `snippet`, `position` |
| `scholar-articles(-enriched)` | + `metadata` (citation string), `pdf_url`, `authors[]`, `citations` |
| `scholar-author` | `name`, `affiliation`, `citations{all,since_2020}`, `h_index`, `articles[]` (no title/snippet) |
| `ai` | `title: "AI Overview"`, `content`, `references[{title,url,snippet}]` |

A snippet is a search-engine excerpt. To cite a fact, fetch the page and quote the passage. Live `news` results returned `url`s of the form `https://www.google.com/goto?url=…` (redirects), so resolve them before citing.

## Web fetch: `POST /web/enrich/live`

1 credit per page. Rate limit 10/min. `urls` holds 1–10 values with an `http(s)://` scheme. `human_mode` is optional.

```json
{ "urls": ["https://example.com", "https://example.org"] }
```
→ **array**: `[{success, url, timestamp (seconds), title, content (full HTML), http_status}]` (`http_status` is live behavior and is not in the docs).
- Order may differ from the request, so match on `url`.
- A failed URL returns `success: false` with every other field `null` (including `url`). Find which input failed by elimination.
- Partial failure is still HTTP 200.
- `content` is raw HTML, so parse it (BeautifulSoup/Cheerio) before quoting.

## Social post search: `POST /social_post/search`

0.5 credits per post. `limit: 0` counts are free. All plans, 30/min. Same filter grammar, `sorts`, `fields`, `cursor`. Response: `{posts, next_cursor, total_count}`.

```json
{
  "filters": { "op": "and", "conditions": [
    { "field": "text", "type": "[.]", "value": "cold chain" },
    { "field": "date_posted", "type": "=>", "value": "2026-09-01" }
  ]},
  "sorts": [{ "field": "date_posted", "order": "desc" }],
  "fields": ["share_url", "text", "actor", "date_posted", "engagement"],
  "limit": 20
}
```

Useful filter fields: `text`, `hashtags`, `post_type`, `date_posted`, `actor.actor_type` (`person`/`company`), `actor.crustdata_entity_id` (the most reliable author selector), `actor.name`, `actor.company_name`, `actor.location` (geo), `mentions.company_id` / `mentions.person_id` (returned as `mentions.company_ids` / `person_ids`), `engagement.total_reactions`, and the nested `reactors.*` / `comments.*` fields.

Recent posts for one person or company fetched live: `/social_post/professional_network/enrich/live` (enterprise). See `live-and-async.md`.
