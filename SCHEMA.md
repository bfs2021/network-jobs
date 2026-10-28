# Network Jobs — Local Schema

Operating contract for all skills in this suite. Read this before writing or reading data under `~/.network-jobs/`.

## Data root

Default: `~/.network-jobs/` (override with `NETWORK_JOBS_HOME`).

```text
~/.network-jobs/
├── profile.json
├── preferences.json              # search prefs from agent interview
├── resume/
│   ├── source.<ext>              # original upload (pdf/docx/md/…)
│   └── text.md                   # extracted plain text (when available)
├── connections/
│   └── connections.json
├── companies/
│   └── companies.json
├── config/
│   ├── companies-to-ignore.json      # optional user overrides
│   ├── company-words-to-ignore.json
│   └── company-overrides.json
├── triage/
│   └── careers-<slug>-<YYYY-MM-DD>/
│       ├── INVENTORY.md
│       ├── captures/                  # user-saved JSON/HTML for a gated board
│       ├── index/
│       │   ├── listings.json
│       │   ├── matches.json          # prefs-scored shortlist
│       │   ├── pagination.json       # pages / complete / truncated / status
│       │   └── summary.json
│       └── fetch-log/
│           └── <timestamp>-<label>.json
├── corpus/
│   ├── manifest.json
│   ├── <category>.json
│   └── <category>-<loc>-<seniority>.json
├── search/
│   ├── ranked.json                   # top-K from local ranker
│   └── intros.json                   # 1–2 roles + 1–2 forwarders
└── logs/
    └── setup.log
```

## profile.json

```json
{
  "name": "Frank Harris",
  "email": "frank@example.com",
  "title": "Executive Coach",
  "company": "",
  "url": ""
}
```

Used by `network-jobs` (search header + intro footer) and `intro-email-generator` (job seeker identity when drafting). Prefer filling from a resume via `network-jobs profile import` + the setup skill — do not invent email.

## preferences.json

Search and discovery defaults from a **resume-grounded** agent interview (not a fixed questionnaire). Ask only what the background leaves open; skip what the resume already answers clearly.

```json
{
  "updatedAt": "2026-08-18T23:00:00Z",
  "interviewComplete": true,
  "workModes": ["remote", "hybrid"],
  "locationBuckets": ["nyc", "remote"],
  "locations": ["New York", "Remote US"],
  "onsiteLocations": ["New York"],
  "categories": ["product"],
  "seniority": ["senior"],
  "track": ["ic"],
  "companyStages": ["seed", "series-b", "public"],
  "companySizes": [],
  "industries": ["developer-tools", "fintech"],
  "formerEmployers": ["Acme Corp", "Example Labs"],
  "formerEmployerPolicy": "exclude",
  "salaryMin": null,
  "mustHaves": ["strong eng partnership"],
  "dealBreakers": ["pure people-management with no craft"],
  "notes": "Open to SF onsite for the right role"
}
```

| Field | Meaning |
|-------|---------|
| `workModes` | `remote` \| `hybrid` \| `onsite` (multi-select) |
| `locationBuckets` | Corpus buckets to prefer — see Location buckets below (`nyc`, `sf`, `la`, `seattle`, `austin`, `boston`, `chicago`, `denver`, `dc`, `remote`, `other`) |
| `locations` | Free-text places the user cares about (display / soft filter) |
| `onsiteLocations` | Where onsite/hybrid is acceptable. **Required whenever `workModes` includes `hybrid` or `onsite`.** Never treat “open to onsite” as every office worldwide — scope it to these places (and matching `locationBuckets`). |
| `categories` | Preferred role categories (same 15 as corpus) |
| `seniority` | `senior` and/or `mid` — a soft scoring signal, never a veto (match +3, mismatch −2, level-ambiguous −1); only `intern`/`junior` signals hard-fail |
| `track` | `ic` \| `manager` \| `either` (individual contributor vs people manager) |
| `companyStages` | Free-form tags the user cares about (e.g. `seed`, `series-a`, `growth`, `public`) |
| `companySizes` | Optional size bands the user stated (free-form) |
| `industries` | Domains from resume + interview (free-form) |
| `formerEmployers` | Company names from the resume (past employers); used with `formerEmployerPolicy` |
| `formerEmployerPolicy` | `include` — allow roles there · `exclude` — skip them in discover/search defaults · `ask` — confirm per company when they appear |
| `salaryMin` | Annual USD floor, or `null` if undisclosed / no floor |
| `mustHaves` | Aspirational requirements, matched AND-set over title + department + company + description (+2, reason `mustHave`); stated-but-unmatched on a described role records `mustHave-unmet` (visible, no penalty) |
| `targetRoles` | Role-shape slugs (`vp-product`, `head-of-product`) matched token-wise against title + department (+3, reason `targetRole`); aspirational bonus only, never a veto |
| `dealBreakers` | Short exclusions |
| `dealBreakersConfirmed` | `true` once the interview explicitly asked for deal-breakers (even when the answer is none). An empty `dealBreakers` *without* this flag means "never asked" — `match-prefs` warns on it, because unasked filters silently widen the shortlist. |
| `notes` | Catch-all soft constraints |
| `interviewComplete` | `true` after the user finishes the preferences interview |

Extra keys are allowed when the interview surfaces something useful (visa, travel %, commute, “staff+ only”, etc.) — keep values JSON-serializable.

`network-jobs` should apply these as **defaults** when the user does not override in the query. User query always wins.

## resume/

| Path | Purpose |
|------|---------|
| `resume/source.*` | Original file from `network-jobs profile import` |
| `resume/text.md` | Extracted text for profile fill, prefs interview, and intro drafts |

PII — never commit `~/.network-jobs/resume/` into git.

## connections.json

Array of people from LinkedIn `Connections.csv`:

```json
[
  {
    "firstName": "Ada",
    "lastName": "Lovelace",
    "company": "Analytical Engines Inc",
    "position": "Engineer",
    "url": "https://www.linkedin.com/in/ada",
    "email": "",
    "connectedOn": "01 Jan 2024"
  }
]
```

## companies.json

Aggregated company graph (connection counts + sample people):

```json
[
  {
    "name": "Stripe",
    "normalized": "stripe",
    "slug": "stripe",
    "domain": "",
    "connectionCount": 12,
    "people": [
      { "name": "Jane Doe", "position": "PM", "url": "https://www.linkedin.com/in/jane" }
    ],
    "lastCrawl": "2026-09-16T02:00:00Z",
    "listingSetHash": "a1b2c3d4e5f60789",
    "jobsUrl": "https://api.example.com/v1/jobs",
    "lastPagination": { "pages": 4, "complete": true, "truncated": false }
  }
]
```

- `normalized` — lowercase, punctuation/suffix stripped (see import helper).
- `slug` — filesystem-safe form of `normalized`.
- `jobsUrl` — listings JSON endpoint stamped by `crawl-state --stamp --source-url`;
  `network-jobs refresh` re-crawls it deterministically (no model in the loop).
- `domain` — filled later during careers discovery when known.
- `people` — up to 10 sample connections (not the full roster). **Intro ranking joins `connections.json`**, not this sample.
- `lastCrawl` / `listingSetHash` / `lastPagination` — crawl budget. If the listing-set hash matches, skip refresh/ingest (delta is empty). Stamp after a successful paginated extract. Caps: 15 API pages, 5 browser “load more” pages, 2000 listings.

## Triage (careers-discover output)

Staging only. Do **not** write corpus shards from `careers-discover`.

### index/listings.json

Raw extracted openings (pre-normalization):

```json
[
  {
    "title": "Senior Backend Engineer",
    "location": "San Francisco, CA",
    "locations": ["San Francisco, CA"],
    "url": "https://example.com/jobs/123",
    "department": "Infrastructure",
    "externalId": "123",
    "salary": { "min": 180000, "max": 250000 },
    "postedAt": "2025-12-15T00:00:00Z",
    "sourceUrl": "https://example.com/careers",
    "rawNotes": ""
  }
]
```

`locations` is optional (one posting, many offices). `externalId` is the ATS/board id when the JSON exposes one — used for fingerprinting, never invented.

`description` is optional free text (JD body, truncated to ~10k chars, plus
`descriptionFetchedAt` / `descriptionSource`). Filled by
`fetch-descriptions.py` for matches only — listings without one are
title-only, and title-only rows score structurally weaker (no requirement
text for mustHaves matching, no body for embeddings or fit briefs) rather
than incidentally weaker. `department` is filled from the ATS payload when
the board provides one and the row lacks it.

### index/pagination.json

Written by `careers-discover/helpers/paginate-listings.py` when a jobs JSON/API is followed (page / cursor / offset, or `start` for boards whose rows and their count sit together — Eightfold's `data.positions` / `data.count` or `data.positions` / `data.meta.total`, or the same shape at the top level). Cap `maxPages` (default 15). ATS-agnostic — no adapter matrix.

```json
{
  "pages": 4,
  "complete": true,
  "truncated": false,
  "scheme": "link",
  "maxPages": 15,
  "status": "complete",
  "reportedTotal": 120,
  "rowsFetched": 124,
  "deduped": 4
}
```

`scheme` is the paging strategy that produced the last next page: `page`, `cursor`, `offset`, `link`, `start` or `none`. A one-page start-paged board can still report `start`. `reportedTotal` and `deduped` are present only when known/positive; `rowsFetched` is always written.

`status` is `complete` (clean crawl covering the board), `truncated` (a **partial** crawl: a page/listing cap was hit before the board total was covered, it began mid-board, or it staged fewer unique roles than the board reported), `empty` (clean, parsed board with **zero** roles), `blocked` (the fetch failed or nothing parseable came back — **not** “no openings”) or `captured` (a `paginate --input` run over a user-saved page — never the whole board). It is additive: `complete` / `truncated` keep their existing meaning and remain the rule for expiry. Also copied into the INVENTORY `Pagination:` line and surfaced as the header `Status:` field. **`complete` is false** when the cap truncated the crawl before the board total was covered, a next-page signal remained, the board could not be read (a dead URL writes `blocked` with zero rows), the rows came from a user capture (`captured`), the crawl began beyond the first row/page (`offset > 0`, `page > 1`, or an envelope `start > 0`), or it staged fewer **unique** listings than the board's reported total. Ingest must **not** expire unseen jobs unless `complete` is true **and** `status` is neither `blocked`, `captured`, nor `truncated`; `truncated` is partial and the helper refuses it unless `--force-expire` is passed. If the batch has **zero incoming rows** for that company and the company already had any open roles, ingest **skips expiry** and warns — whatever the status (omitted, the jq string `null` from an older `pagination.json`, or `empty`). A filtered or region-limited crawl URL can look empty and would otherwise wipe the company. Pass `--force-expire` only when the board really closed every role. A company that had zero open rows can still record empty.

Coverage fields sit beside `status`: `reportedTotal` is the count the board itself reported when present (read beside the rows, from a `meta`/`pagination` block on the rows' container, or at the top level; a count in an unrelated nested object is ignored); `rowsFetched` is the raw rows served across pages before fingerprint dedupe; `deduped` is `rowsFetched - listings` when positive. `deduped` is diagnostic only — it includes rows that normalization dropped and listings cut by the cap, so never subtract it from the board total. `reportedTotal` is appended to the INVENTORY `Pagination:` line. Feed ingest `--expect-count` from `max(listings, reportedTotal)` when the total is larger than the listing count: the listing length alone is self-referential, while the board's own total makes the subset guard bite if a crawl was wrongly marked complete.

### index/matches.json

Written by `careers-discover/helpers/match-prefs.py` after extract. Score listings against `preferences.json` (+ resume keywords when present). **Confirm matches by default** at ingest time; the user can still say “ingest all” (`listings.json`) or a department slice.

```json
{
  "nListings": 120,
  "nMatches": 14,
  "showing": "14 of 120 match prefs",
  "departments": { "Product": 40, "Engineering": 80 },
  "ingestDefault": "matches",
  "jobs": [
    {
      "title": "Staff Product Manager, Growth",
      "location": "New York, NY or Remote",
      "url": "https://example.com/jobs/pm-2",
      "department": "Product",
      "matchScore": 15,
      "matchReasons": ["remote", "onsiteLocations", "category", "seniority", "track"],
      "matched": true
    }
  ]
}
```

`fetch-descriptions.py` rewrites this file in place and **keeps the object
shape**: every summary key above is preserved untouched and only
`description` / `descriptionFetchedAt` / `descriptionSource` (plus
`department`, when the ATS payload provides one) are added to rows.
`nMatches` / `showing` still describe the shortlist; `departments` stays the
listing-wide histogram computed by `match-prefs` and can go stale after a
department fill — re-running `match-prefs` recomputes it. Readers use
`load_matches()` (`lib/network_jobs/matches.py`), which also accepts the
legacy bare-array shape that older `fetch-descriptions` runs wrote and
normalizes it to this object.

### INVENTORY.md

Required. Summarize: company, careers URL used, listing count, caveats, suggested next steps.

The top header block carries `Slug`, `Date`, `Careers URL`, `Status`, plus free-form fields (`Caveats`, `Next steps`). `stage-company.sh` writes placeholders; `paginate`, `match-prefs` and `crawl-state` refresh `Careers URL` from `index/*.json` on every write (`lib/network_jobs/inventory.py`). `Status` is refreshed when the summary has pagination data, or when the header still holds a placeholder (`(pending)`/empty); a hand-filled value (`captured`, `truncated (manual card extract)`) is preserved. Free-form fields are never patched. `Careers URL` comes from the rows' `sourceUrl` (the crawled endpoint) when available.

`Listings` — and `Pagination` when the helper has pagination data — live in the `<!-- nj-summary -->` block, which is their canonical home: each label appears exactly once per file. The `Listings`/`Pagination` header lines that `stage-company.sh` writes are pre-helper placeholders and are removed on the first helper write that renders them; a manual extract without `index/pagination.json` keeps its header `Pagination` line untouched.

Helpers also append a `<!-- nj-summary -->` block with **N of M match prefs**, a department histogram, and — when available — pagination. Do not dump the full board into chat — point at `matches.json`.

### fetch-log/

Verbatim capture of each fetch/browser snapshot **before** presenting results to the user. One JSON file per call:

```json
{
  "timestamp": "2026-07-08T19:00:00Z",
  "method": "curl|agent-browser|user-capture",
  "url": "https://example.com/careers",
  "status": 200,
  "disposition": "staged",
  "agentBrowserVersion": "0.39.1",
  "notes": ""
}
```

For a gated board, the user-capture handoff writes one entry with `"method": "user-capture"`, `"disposition": "user-capture"`, the careers URL the user opened, and `capturePath` pointing at the saved file under `triage/<dir>/captures/`. Ingest that file with `paginate --input <file>` — the rest of the pipeline is unchanged.

`agentBrowserVersion` is optional and set only for `method: agent-browser`: the
output of `agent-browser --version`, so a later run can tell a site change from
a tool change. Omit it when unknown rather than guessing.

## Corpus (jobs-ingest output)

### Job object

```json
{
  "id": "stripe-senior-backend-engineer-123",
  "fingerprint": "id:123",
  "title": "Senior Backend Engineer",
  "company": "Stripe",
  "companyDomain": "stripe.com",
  "department": "Developer Infrastructure",
  "category": "engineering",
  "location": "San Francisco, CA",
  "locations": [{"raw": "San Francisco, CA", "city": "San Francisco", "region": "CA", "remote": false, "bucket": "sf"}],
  "locationBucket": "sf",
  "locationBuckets": ["sf"],
  "seniority": "senior",
  "track": "ic",
  "url": "https://stripe.com/jobs/123",
  "externalId": "123",
  "status": "open",
  "salary": { "min": 180000, "max": 250000 },
  "postedAt": "2025-12-15",
  "firstSeen": "2026-07-08",
  "lastSeen": "2026-07-08"
}
```

Salary is `{ "min", "max" }` in annual USD with optional `"currency"` (default USD) and optional `"unit": "hourly"` — hourly rates are flagged, never annualized. String salaries (`"$150k–$180k"`, `"up to $200k"`, `"$75/hr"`) are parsed at ingest (`lib/network_jobs/salary.py`); dict salaries pass through. `postedAt` is canonicalized to `YYYY-MM-DD` at ingest when the ATS format parses, otherwise kept raw. Eightfold-style epoch timestamps are a source for it (#75): `postedTs` (preferred) and `creationTs` (fallback), in seconds or milliseconds, convert to `YYYY-MM-DD`; a real `postedAt` always wins, and a junk epoch with no valid fallback stays raw.

`description` (optional free text, JD body truncated to ~10k chars, with `descriptionFetchedAt` / `descriptionSource`) rides through classify, merge, and shards untouched. mustHaves matches against it; embeddings include it when present. Rows without one are title-only and score structurally weaker — not incidentally, but because there are no requirements to match against. When a row's `sourceUrl` is Eightfold's `/api/pcsx/search`, `fetch-descriptions` uses the sibling `/api/pcsx/position_details` endpoint instead of the JS-rendered listing page; every other board keeps the page fetch.

Identity is **`fingerprint`**, not URL: `id:{atsId}` when `externalId` is known, otherwise `fp:{sha1(company+title+locations)}` (company from job data, not CLI flag — see #25). Placeholder ATS ids (`null`, `n/a`, `unknown`, `tbd`, `-`, plus prose label-leaks like `See Opening ID`) are treated as absent so distinct jobs never collide on a shared placeholder. The full set lives in `PLACEHOLDER_ATS_IDS` (`lib/network_jobs/fingerprint.py`). Rebuild merges on fingerprint: incoming wins, but `firstSeen` is preserved, `lastSeen` is inherited from the previous record when the incoming batch lacks it (a batch without `lastSeen` is not evidence the job was seen today), and a previous `postedAt` is kept when incoming lacks one. If an incoming job's `externalId` matches an existing entry under a different fingerprint, the entries are merged and a `driftWarnings` entry is emitted (prevents silent duplication from historical `--company` flag inconsistency). `status` is `open` | `closed`. Closed jobs stay in `jobs-all.json` but are omitted from searchable shards.

**Expiry / close:** when ingesting a company, unseen open jobs for that company are marked `closed` **only if** triage `pagination.complete` is true **and** `pagination.status` is not `truncated`, `blocked`, or `captured`. Incomplete crawls (truncated, cap hit, missing next page) must not expire anything; `truncated` is refused unless `--force-expire` is passed.

### Categories (15)

`engineering` | `product` | `design` | `data` | `ai-ml` | `sales` | `marketing` | `customer-success` | `operations` | `finance` | `people` | `legal` | `it-security` | `retail` | `other`

Adjacent categories count as a soft match via `CATEGORY_AFFINITY` (`lib/network_jobs/classify.py`): when a seeker prefers `engineering` but the job is classified `ai-ml` or `data`, scoring adds +2 with reason `category-affinity` instead of the +5 `category` exact match — and, unlike an unrelated category, never hard-fails the job. Both bonuses require a high-confidence classification; low-confidence guesses record `category-unconfirmed` with no bonus, even when the guessed category is exactly the one preferred (#40). A missing `categoryConfidence` counts as high, but that is only a lenient default for direct callers of the helper: scoring always starts from `classify_job()`, so every corpus row carries the field. Affinity is directional (seeker preference → acceptable job category), e.g. a `marketing` seeker accepts `product` roles but a `product` seeker does not get `marketing` roles boosted.

### Location buckets

| Bucket | Meaning |
|--------|---------|
| `nyc` | New York City metro |
| `sf` | San Francisco / Bay Area |
| `la` | Los Angeles metro |
| `seattle` | Seattle metro (incl. Bellevue, Redmond) |
| `austin` | Austin metro |
| `boston` | Boston metro (incl. Cambridge) |
| `chicago` | Chicago metro |
| `denver` | Denver metro (incl. Boulder) |
| `dc` | Washington, DC metro (incl. Arlington, Alexandria) |
| `remote` | Fully remote (or remote-first) |
| `other` | Everything else |

Hybrid strings such as `New York, NY or Remote` set **`locationBuckets`: `["nyc","remote"]`** so the same job is written to both granular shards. Parsed `locations[].city` / `.region` are used with `onsiteLocations` (never “every office worldwide”).

### Seniority buckets

Keep `senior` / `mid` shards as the fast path.

| Bucket | Title signals |
|--------|----------------|
| `senior` | Senior, Staff, Principal, Lead, Director, VP, Head of, **high-confidence people-manager titles** (`Engineering Manager`, `Director of Engineering`, …) |
| `mid` | Everything else (including internships, junior titles, and level-ambiguous titles) |

Additional **`senioritySignals`** on the job object: `intern`, `junior`, `unmarked`, `staff+`, `manager`. Seniority is a soft scoring signal, never an exclusion: a seniority match scores +3, any other mismatch scores −2, and a level-ambiguous title (no seniority markers, e.g. plain "Product Manager") scores −1 with reason `seniority-ambiguous` — unknown level must not rank like a confirmed match. Only positive junior signals (`intern`, `junior`) hard-fail against non-junior prefs. Interns stay in `mid`; Staff/Principal stay in `senior` plus `staff+`. High-confidence people-manager titles (track `manager`, confidence `high`) land in `senior` with the `manager` signal — generic "X Manager" titles (`Escalations Manager`, `Sourcing Manager`) and IC-flavored "manager" titles (`Account Manager`, `Program Manager`) stay `mid` and are not promoted.

`track` is persisted on the job (`ic` | `manager`) by the classifier.

### manifest.json

```json
{
  "lastUpdated": "2026-07-08T19:00:00Z",
  "totalJobs": 42,
  "categories": {
    "engineering": {
      "count": 10,
      "file": "engineering.json",
      "byLocation": {
        "nyc": {
          "senior": { "count": 2, "file": "engineering-nyc-senior.json" },
          "mid": { "count": 1, "file": "engineering-nyc-mid.json" }
        }
      }
    }
  }
}
```

## Promotion rules

1. `careers-discover` writes only under `triage/`.
2. `jobs-ingest` promotes triage → corpus **only after user confirmation**.
3. Never delete triage batches without asking.
4. PII (connections, profile) stays local — never commit `~/.network-jobs/` into this repo.

## Skill pipeline

| Skill | Reads | Writes |
|-------|-------|--------|
| `network-jobs-setup` | `resume/`, conversation | `profile.json`, `preferences.json` |
| `network-jobs-import` | LinkedIn ZIP | `connections/`, `companies/` |
| `careers-discover` | `companies/`, `preferences.json` (optional focus) | `triage/` (`listings.json` + `matches.json`) |
| `jobs-ingest` | `triage/` (`matches.json` by default, or all / department slice) | `corpus/` |
| `network-jobs` | `corpus/`, `profile.json`, `preferences.json`, `resume/` | `search/ranked.json` |
| `intro-email-generator` | `profile.json`, `resume/text.md`, `connections.json`, `search/intros.json` | `search/intros.json` |

CLI helpers: `network-jobs profile import <file>` stores the resume; `network-jobs profile show` prints profile + prefs + resume status.

Search ranker: `skills/network-jobs/helpers/rank-jobs.py` writes `search/ranked.json` (`showing: "K of N"`). The search skill reads that file — it does not dump whole shards into context.

### search/ranked.json

```json
{
  "k": 25,
  "n": 430,
  "showing": "25 of 430",
  "query": "senior pm nyc",
  "shards": ["product-nyc-senior.json"],
  "staleHidden": 12,
  "companyCap": { "limit": 3, "skipped": 39 },
  "jobs": []
}
```

- `staleHidden` — postings excluded as stale: `postedAt` older than 90 days, or
  no `postedAt` and `lastSeen` older than 120 days. Re-run rank with
  `--include-stale` to see them (they carry `"stale": true` and sort last).
- `companyCap` — present **only** when the per-company cap is what stopped the
  shortlist reaching `k`: `limit` is the cap in force, `skipped` how many
  candidates it withheld. A `showing` of `3 of 430` under a `companyCap` means
  "3 shown, rest held by the cap", **not** "only 3 match". Surface it as its own
  line rather than folding it into `showing`, and remember `--company-cap 0`
  reveals the withheld ones. Absent when the shortlist filled normally.
- Recency boost: postings from the last 14 days get +2 (`matchReasons` gains
  `recent`), last 30 days get +1.
- Company cap: at most 3 jobs per company in the top-K shortlist
  (`--company-cap N` on `rank`/`search`; 0 disables), so one big board can't
  crowd out the shortlist.

### corpus/embeddings.json

Optional semantic-matching cache, written by `rebuild`/`refresh` when an
embedding provider is configured (`network-jobs embed-setup` installs
fastembed, ~60MB one-time model download; a local Ollama is used if already
running). Keyed by job fingerprint; re-embedded only for new/changed jobs, or
all jobs when the model/recipe changes.

```json
{
  "model": "BAAI/bge-small-en-v1.5",
  "dims": 384,
  "recipe": 1,
  "vectors": {"<fingerprint>": [0.013, -0.22]}
}
```

Scoring effects (all soft signals; nothing is a veto):

- `resume-semantic` — cosine(resume, job) mapped to +0–4, **replaces** the
  keyword `resume` bonus (never stacks with it).
- `query-semantic` — cosine(`--query`, job) mapped to +0–2, **additive** to the
  lexical `query` hits.

With no provider configured, scoring is byte-identical to keyword-only.
`NJ_EMBED_PROVIDER=fastembed|ollama|none` overrides provider detection.

