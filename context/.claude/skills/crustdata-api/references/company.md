# Company API reference

Source pages: `company-docs/search/introduction`, `company-docs/search/reference`, `company-docs/identify/introduction`, `company-docs/enrichment/reference`, `company-docs/autocomplete/introduction` (fetch as `https://docs.crustdata.com/<page>.md`).

## Contents
- Identify: `POST /company/identify`
- Enrich: `POST /company/enrich`
- Search: `POST /company/search`
- Autocomplete: `POST /company/search/autocomplete`

---

## Identify: `POST /company/identify`

Resolve partial info to Crustdata company records. **0.2 credits per identifier that matches**. Unmatched and repeated identifiers are free.

Request: exactly **one** identifier key, max **25** values:

| Key | Type |
|---|---|
| `domains` | string[] (`"retool.com"`) |
| `names` | string[] |
| `professional_network_profile_urls` | string[] (`"https://www.linkedin.com/company/mintlify"`) |
| `crustdata_company_ids` | int[] |
| `exact_match` | bool, optional: strict domain match (can still return several records sharing a domain) |

```bash
curl -s -X POST https://api.crustdata.com/company/identify \
  -H "authorization: Bearer $CRUSTDATA_API_KEY" -H 'content-type: application/json' \
  -H 'x-api-version: 2025-11-01' \
  -d '{"domains": ["serverobotics.com"]}'
```

Response is a **top-level array**, one entry per input:

```json
[{
  "matched_on": "serverobotics.com",
  "match_type": "domain",
  "matches": [{
    "confidence_score": 1.0,
    "company_data": {
      "crustdata_company_id": 628895,
      "basic_info": {
        "name": "Serve Robotics", "primary_domain": "serverobotics.com",
        "all_domains": ["serverobotics.com"], "website": "https://www.serverobotics.com/",
        "professional_network_url": "https://www.linkedin.com/company/serverobotics",
        "professional_network_id": "72049930", "employee_count_range": "51-200",
        "industries": ["Technology, Information and Internet"]
      }
    }
  }]
}]
```

- No match → `200` with `"matches": []`. The spec also allows `404`, so handle both.
- **Name lookups return lookalikes.** `names: ["Serve Robotics"]` returned 25 candidates live: Serve Robotics (1.0), Site Serve Robotics (0.8), then a run of 0.6 scores (Serve Legal, ACU-Serve, …). The docs example even shows two at 1.0. Disambiguate on `primary_domain`, `employee_count_range`, and industries. Domain and profile-URL lookups are direct.
- `match_type` values: `domain`, `name`, `crustdata_company_id`, `professional_network_profile_url`.
- Errors: 400 `Exactly one identifier must be provided…` and `Maximum 25 values allowed for domains. Found 26`.

## Enrich: `POST /company/enrich`

Same identifier keys and 25-value cap as identify, plus `fields`. **2 credits per record**, plus 2 per company that returns `technographics`, plus 5 for `social_posts` (beta, gated). Response shape matches identify (`[{matched_on, match_type, matches:[{confidence_score, company_data}]}]`), with `company_data` holding the requested sections.

```bash
curl -s -X POST https://api.crustdata.com/company/enrich \
  -H "authorization: Bearer $CRUSTDATA_API_KEY" -H 'content-type: application/json' \
  -H 'x-api-version: 2025-11-01' \
  -d '{"crustdata_company_ids": [628895], "fields": ["basic_info", "headcount", "funding", "people", "news"]}'
```

**`fields` defaults to `["basic_info"]` only.** List every section you want, and include `basic_info` explicitly when you add others.

| `fields` group | Contains |
|---|---|
| `basic_info` | name, primary_domain, all_domains, website, professional_network_url/id, logo, year_founded, description, company_type, status (`active`/`deleted`), employee_count_range, markets |
| `headcount` | total, by_role_absolute/percent, by_region_absolute, growth_percent/absolute keyed `mom,qoq,six_months,yoy,two_years` |
| `funding` | total_investment_usd, last_round_amount_usd, last_fundraise_date, last_round_type, investors, funding_rounds, milestones, acquisitions |
| `locations` | country, state, headquarters, street_address, all_office_addresses |
| `taxonomy` | professional_network_industry/industries, categories, specialities, primary_naics_detail, sic_detail_list |
| `revenue` | estimated.lower_bound_usd/upper_bound_usd (0 = no estimate), public_markets, acquisition_status |
| `hiring` | openings_count, openings_growth_percent, recent_openings (may be field-gated) |
| `followers` | count, mom/qoq/yoy percent |
| `people` | decision_makers, founders, cxos |
| `news` | article_url, article_title, article_publish_date |
| `seo`, `competitors`, `web_traffic`, `employee_reviews`, `software_reviews`, `public_launches`, `market_intel`, `social_profiles`, `reviews` | as named |
| `technographics` | total_technologies, top_technologies, technologies[] (name, categories, sources, evidence), updated_at (gated add-on) |

`company_data` always carries `crustdata_company_id`. The docs list top-level `updated_at`/`indexed_at`, but live (2026-11-22) neither came back, and `fields: ["metadata"]` returned `metadata.updated_at: null`. For a record date use company **search** with `metadata.updated_at`, or cite the retrieval time and `x-request-id`.

Enrich `headcount` (live) also includes `timeseries`, `by_role_growth_6m_pct`, `by_role_growth_yoy_pct`, `by_region_percent`, `by_skill_absolute/percent`, and `by_function_timeseries`.

A field your account cannot read returns `403` `Access denied to fields: …`, with `metadata[0].permitted_fields`. Resend without the denied fields.

## Search: `POST /company/search`

Body keys: `filters` and/or `search` (at least one), `fields`, `sorts`, `limit`, `cursor`. Response: `{companies, next_cursor, total_count}`. Empty → `200` with `companies: []`.

- Filter-only: `limit` 1–1000 (default 20), cursor pagination, include `sorts` for a stable order.
- Ranked: `"search": {"query": "...", "mode": "hybrid"|"lexical"|"semantic"}` with optional `filters` as hard constraints. `limit` 1–100, no `sorts`, ≤1,000 candidate positions, cursor expires 20 min after the first page, `total_count` always `null`.

```json
{
  "search": { "query": "freight forwarding and 3PL warehousing companies", "mode": "hybrid" },
  "filters": { "field": "locations.country", "type": "=", "value": "IND" },
  "fields": ["crustdata_company_id", "basic_info.name", "basic_info.primary_domain", "headcount.total"],
  "limit": 20
}
```

Pricing: 0.03 per result. These premium groups add a charge **per result, once for filtering and once for receiving**: taxonomy 0.1, followers / headcount (incl. `roles.*`) / funding / hiring / software_reviews / maps 0.2, technographics 1 (filter only).

### Searchable fields (most used)

| Field | Type | Sortable | Notes |
|---|---|---|---|
| `crustdata_company_id` | int | yes | |
| `basic_info.name` | string | yes | |
| `basic_info.primary_domain` | string | yes | |
| `basic_info.website`, `basic_info.professional_network_url` | string | no | URL fields reject text operators; use `=` |
| `basic_info.company_type` | string | no | `"Privately Held"`, `"Public Company"` |
| `basic_info.year_founded` | int | yes | |
| `basic_info.employee_count_range` | string | yes | `"201-500"` |
| `basic_info.industries` | string[] | no | filter-only alias of `taxonomy.professional_network_industries` |
| `taxonomy.professional_network_industry` | string[] | no | |
| `taxonomy.categories`, `taxonomy.professional_network_specialities` | string[] | no | |
| `taxonomy.primary_naics_detail.naics_code` | string | no | six digits, 2022 edition |
| `taxonomy.sic_detail_list.sic_code` | string | no | four digits, supports `has_all` |
| `headcount.total` | int | yes | |
| `headcount.growth_percent.{1m,3m,6m,12m}` | number | **no** | filter on it, sort on `headcount.total` |
| `roles.distribution.<function>` | int | no | e.g. `engineering`, `sales`, `operations`; filter only |
| `funding.total_investment_usd`, `funding.last_round_amount_usd` | number | yes | |
| `funding.last_fundraise_date` | date | yes | |
| `funding.last_round_type` | string | no | `"series_a"`, `"seed"`, … |
| `funding.investors` | string[] | no | all-time |
| `revenue.estimated.lower_bound_usd` / `upper_bound_usd` | int | yes | `0` = no estimate, so use `> 0` |
| `revenue.public_markets.stock_symbols` | string[] | no | |
| `locations.country` | string | yes | ISO-3 (`"IND"`) or full name. Docs say the response is the full name, but a live search filtered on `"IND"` returned `"IND"`, so normalize both forms |
| `locations.state`, `locations.city` | string | no | normalized geocoded values |
| `locations.headquarters` | string | no | target of `geo_distance` / `geo_exclude` / `geo_polygon` |
| `locations.all_office_addresses` | string[] | no | use `(.)` for a city substring |
| `followers.count` | int | yes | |
| `competitors.company_ids`, `competitors.websites` | array | no | filter only |
| `technographics.technologies.name` | string[] | no | filter only, supports `has_all` |
| `updated_at`, `indexed_at` | datetime | yes | filter/sort name. To **return** them, put `metadata.updated_at` / `metadata.indexed_at` in `fields` (`updated_at` in `fields` → 400) |

Returned sections: `basic_info`, `headcount`, `funding`, `locations`, `taxonomy`, `revenue`, `hiring`, `followers`, `social_profiles`, `software_reviews`. Not returned even if filtered on: `basic_info.industries`, `competitors.*`, `roles.*`, `technographics.*`.

### Examples

Companies in a 50 km radius with 200+ staff:
```json
{
  "filters": { "op": "and", "conditions": [
    { "field": "locations.headquarters", "type": "geo_distance", "value": { "location": "Mumbai, India", "distance": 50, "unit": "km" } },
    { "field": "headcount.total", "type": "=>", "value": 200 }
  ]},
  "sorts": [{ "field": "headcount.total", "order": "desc" }],
  "fields": ["crustdata_company_id", "basic_info.name", "basic_info.primary_domain", "locations.city"],
  "limit": 25
}
```

Recently funded:
```json
{
  "filters": { "op": "and", "conditions": [
    { "field": "funding.last_fundraise_date", "type": "=>", "value": "2026-09-01" },
    { "field": "funding.last_round_type", "type": "in", "value": ["series_a", "series_b"] }
  ]},
  "sorts": [{ "field": "funding.last_fundraise_date", "order": "desc" }],
  "fields": ["crustdata_company_id", "basic_info.name", "funding.last_round_type", "funding.last_round_amount_usd", "funding.last_fundraise_date"],
  "limit": 50
}
```

Two values on one list field: put two `=` conditions in an `and` group (`has_all` only works on technographics and SIC).

Large `in` lists: up to ~5,000–10,000 values per condition. Chunk beyond that, since ~50,000+ is rejected and the body cap is 10 MB.

Errors: `400 Unsupported columns in conditions: ['x']`, `400 'filters.type' must be one of … Got '>='. Did you mean '=>'`, `400 Invalid cursor: expired or malformed; restart without cursor`.

## Autocomplete: `POST /company/search/autocomplete` (free)

```json
{ "field": "basic_info.industries", "query": "tech", "limit": 5 }
```
→ `{"suggestions": [{"value": "Technology, Information and Media"}, {"value": "Technology, Information and Internet"}, …]}`

- `query: ""` returns the most frequent values.
- `limit` max 100. No `filters` scope (it is ignored).
- Useful fields: `basic_info.industries`, `taxonomy.professional_network_industry`, `locations.country`, `basic_info.company_type`, `funding.last_round_type`, `basic_info.name`, `technology`, `technology_category`.
- Reuse `value` verbatim in the search filter.
