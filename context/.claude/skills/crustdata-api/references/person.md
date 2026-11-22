# Person API reference

Source pages: `person-docs/search/introduction`, `person-docs/search/reference`, `guides/person-semantic-search`, `person-docs/enrichment/introduction`, `person-docs/enrichment/reference`, `person-docs/identify/introduction`, `person-docs/contact/enrich`, `person-docs/autocomplete/introduction`.

Person API error bodies sometimes use `reason` instead of `description`, so parse defensively.

## Contents
- Search: `POST /person/search` (filters, semantic search)
- Enrich: `POST /person/enrich`
- Identify: `POST /person/identify`
- Contact: `POST /person/contact/enrich`
- Autocomplete: `POST /person/search/autocomplete`

---

## Search: `POST /person/search`

Body: `filters` (required unless `search` is given), `fields`, `sorts` (`[{field, order}]`), `limit` 1–1000 (default 20; `count` is an alias), `cursor`, `post_processing` (`exclude_profiles`, `exclude_names`), `explain` (on an empty filter-only result, `remarks` names the condition that removed everyone), `search` (semantic, beta), `mode`.

Response: `{profiles, next_cursor, total_count, total_count_relation, remarks}`. Each profile always has `crustdata_person_id`.

Pricing: 0.03 per result. These premium groups charge **per result, once when filtered on and once when returned**: experience / education / skills 0.1, certifications / honors / professional_network (followers, connections, open-to, languages) / summary (returned only) / research_profiles 0.2, dev_platform_profiles 0.8 (2.5 for repos).

Operators: as in SKILL.md, except **`contains` is not supported** (use `(.)`). `has_all` and `all_of` work on nested `experience.employment_details.*`.

### Key filter fields

| Field | Notes |
|---|---|
| `basic_profile.name`, `.first_name`, `.last_name`, `.headline` | name is sortable |
| `basic_profile.location.country` | **full name** (`"India"`), not ISO |
| `basic_profile.location.city` / `.state` / `.continent` | full names |
| `professional_network.location.raw` | target of `geo_distance` / `geo_exclude` / `geo_polygon` (docs examples use this field) |
| `experience.employment_details.current.title` | filter for current title (the returned twin is `basic_profile.current_title`) |
| `experience.employment_details.current.company_name` | alias of `current.name` |
| `experience.employment_details.current.company_id` | Crustdata company id (int). Returned as `current.crustdata_company_id` |
| `experience.employment_details.current.company_website_domain` | bare domain `"stripe.com"` |
| `experience.employment_details.current.company_professional_network_profile_url` | employer page URL |
| `experience.employment_details.current.seniority_level`, `.function_category` | use autocomplete for values |
| `experience.employment_details.current.start_date` | date: "joined in window" |
| `experience.employment_details.current.is_default` | primary current role |
| `experience.employment_details.past.*` | same leaves for past roles |
| `experience.employment_details.*` (no `current`/`past`) | any role; `start_date`, `company_id`, `years_at_company_raw` are sortable |
| `experience.employment_details.company_headquarters_country` | ISO-3 or full name |
| `recently_changed_jobs` | previous role ended within 90 days of the last index rebuild (field may be gated) |
| `education.schools.school`, `.degree`, `.field_of_study` | conditions in one `and` group must match the **same** school; use `all_of` for different schools |
| `education.schools.start_year` / `.end_year` | integer year; only `= < =< > => is_null is_not_null` |
| `skills.professional_network_skills` | filterable without access, returned only if enabled |
| `professional_network.followers`, `.connections` | sortable |
| `contact.has_business_email` | `=`/`!=` with `true`/`false` only |
| `years_of_experience_raw` | number, sortable |
| `assessment.authenticity.tier` (0 most trusted … 9) / `.verdict` | filter or sort only, never returned by search |
| `crustdata_person_id`, `metadata.updated_at` | sortable |

`social_handles.professional_network_identifier.profile_url` is returned but **not filterable**. To look up by URL, use enrich or identify.

Returned sections: `basic_profile`, `experience`, `education`, `contact` (only `has_business_email`), `social_handles`, plus `skills`, `certifications`, `honors`, `years_of_experience(_raw)`, and `experience.recently_changed_jobs` when enabled. Naming a non-enabled one in `fields` returns 403. `dev_platform_profiles` and `research_profiles` come back only when named.

### Example: people at a company by title

```bash
curl -s -X POST https://api.crustdata.com/person/search \
  -H "authorization: Bearer $CRUSTDATA_API_KEY" -H 'content-type: application/json' \
  -H 'x-api-version: 2025-11-01' \
  -d '{
    "filters": { "op": "and", "conditions": [
      { "field": "experience.employment_details.current.company_website_domain", "type": "=", "value": "retool.com" },
      { "field": "experience.employment_details.current.title", "type": "(.)", "value": "engineering manager" }
    ]},
    "fields": ["crustdata_person_id", "basic_profile.name", "basic_profile.current_title",
               "social_handles.professional_network_identifier.profile_url"],
    "sorts": [{ "field": "crustdata_person_id", "order": "asc" }],
    "limit": 50
  }'
```

### Semantic search (beta)

```json
{
  "search": { "query": "operations heads at Indian 3PL and warehousing companies", "mode": "hybrid" },
  "mode": "exact",
  "filters": { "field": "basic_profile.location.country", "type": "=", "value": "India" },
  "limit": 25
}
```
- `search.mode`: `hybrid` (default), `lexical`, or `semantic`. `search.query_syntax: "boolean"` reads the query as a boolean keyword expression (`+ - | "phrase" * ()`). It needs `search.mode: "lexical"` and top-level `mode: "exact"`. The query can be up to ~131k characters.
- Top-level `mode`: `managed` (default) may add profiles that match constraints parsed from the query, **even if they fail your filters**. Use `"exact"` when every result must satisfy `filters`.
- No `sorts`. Head results carry a `fit` tier, tail results have `fit: null`. The cursor stops after 10,000. With a query, keep `filters`, `search`, and `mode` identical across pages and fetch all pages within 20 min.

## Enrich: `POST /person/enrich`

Request: `professional_network_profile_urls` (max **25**; vanity `/in/name` or URN URLs) and `fields`. **Default returns only `basic_profile` + `social_handles`.** Cost: 1 base per matched record, plus business email 1, personal email 2, phone 2, dev platform 1, research_profiles 2, and social_posts 5 (gated). Max 9.

```json
{
  "professional_network_profile_urls": ["https://www.linkedin.com/in/abhilashchowdhary"],
  "fields": ["basic_profile", "experience", "education", "skills"]
}
```

Response: `[{matched_on, match_type: "professional_network_profile_url", match_status: "matched"|"not_found"|"redacted", matches: [{confidence_score, person_data}]}]`.
- `redacted`: the person opted out, nothing is billed, and a retry won't change it.
- `not_found`: not in the dataset yet. Live enrich (if enabled) fetches it from the web.

`person_data` sections, each selectable in `fields` as a group or a dot-path such as `basic_profile.summary`:

| Section | Key fields |
|---|---|
| `basic_profile` | name, first/last_name, headline, current_title, normalized_title, summary, location{city,state,country,continent,raw}, languages, last_updated |
| `experience` | employment_details.current[] / past[] (title, name, crustdata_company_id, company_website_domain, start_date, end_date, company_status…), years_of_experience(_raw) |
| `education` | schools[] |
| `skills` | professional_network_skills[] |
| `professional_network` | connections, followers, joined_date, verifications, open_to_cards, location |
| `social_handles` | professional_network_identifier.profile_url, dev_platform_identifier.profile_url, twitter_identifier.slug |
| `certifications`, `honors` | field-gated |
| `dev_platform_profiles`, `research_profiles`, `social_posts` | add-ons |
| `assessment` | authenticity.verdict (`clearly_genuine` … `clearly_fabricated`, or `cannot_verify` for thin evidence), authenticity.tier. Free, but field-gated |
| top level | `crustdata_person_id`, `updated_at` (record date to cite). **`updated_at` comes back only if you list it in `fields`** (verified live) |

Treat `assessment` verdicts as a review flag, never as the sole basis for a decision about a person.

## Identify: `POST /person/identify` (enabled per account)

Exactly one key, max 25: `professional_network_profile_urls`, `emails` (business **or** personal; `business_emails` is not accepted here), or `research_profile_urls` (Google Scholar only). 1 credit per match.

```json
{ "emails": ["abhilash@crustdata.com", "nobody@unknown-domain.com"] }
```
→ one row per input, in order: `{matched_on, match_type: "email", match_status, matches: [{confidence_score, person_data: {crustdata_person_id, basic_profile, social_handles, updated_at}}], error_message}`. `matches` holds 0 or 1 entries. An email matched by name-at-domain scores 0.5–1.0, so check the score before trusting it.

Bulk email lookup (up to 300 per job, personal addresses too) uses `POST /batch/person/identify`. See `live-and-async.md`.

## Contact: `POST /person/contact/enrich` (enterprise; self-serve gets 403)

Exactly one of `professional_network_profile_urls` or `business_emails` (reverse lookup), max 25. `fields` must be contact paths: `contact.business_emails` (1), `contact.personal_emails` (2), `contact.phone_numbers` (2), `contact.phone_numbers_info`. Omitting `fields` requests all three (5 credits). `verified: true` keeps only business emails that pass a live deliverability check (+0.5). No match costs nothing.

```json
{ "professional_network_profile_urls": ["https://www.linkedin.com/in/abhilashchowdhary"], "fields": ["contact.business_emails"] }
```
→ `[{matched_on, match_type, matches: [{confidence_score, person_data: {crustdata_person_id, contact: {business_emails: [{email, status}], personal_emails: [...], phone_numbers: [...], phone_numbers_info: [...]}}}]}]`. `status` is `deliverable`, `catch_all`, `invalid`, or `unknown`.

Contact data is personal data: fetch it only for people you will actually contact, after review.

## Autocomplete: `POST /person/search/autocomplete` (free)

```json
{ "field": "experience.employment_details.current.title", "query": "VP", "limit": 5 }
```
→ `{"suggestions": [{"value": "..."}]}`. `limit` max 100, no pagination. Reuse `value` verbatim. Good fields: `basic_profile.location.country`, `experience.employment_details.current.seniority_level`, `experience.employment_details.current.function_category`, `experience.employment_details.current.company_industries`, `education.schools.school`.

Live search uses a different vocabulary, from `POST /professional_network/search/autocomplete` (`field`: `title` | `industry` | `region` | `school`). See `live-and-async.md`.
