# Workday CXS (POST/JSON-body job APIs)

Workday career sites are POST, not GET: the public CXS search endpoint
is `POST /wday/cxs/{tenant}/{site}/jobs` on the tenant's
`myworkdayjobs.com` host. A GET to that endpoint returns nothing
useful — that is a transport difference, not an empty board.

## Cost model

| Action | Cost |
|---|---|
| List openings | free, no login (public CXS search) |
| Job detail | one page fetch per role (listing page) |
| Apply | n/a for this skill |

## Auth

Public. No key, no login for the CXS search endpoint.

## Discovery

Workday career pages live at URLs like
`https://{tenant}.wd{N}.myworkdayjobs.com/{site}`. The CXS endpoint
sits under the same host: `POST
https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`.
Read the tenant and site from the career page's own URL — never guess
one tenant's site name for another company.

## Listing index

The request body is JSON, e.g.:

```json
{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
```

The response is shape-based, not tenant-specific:

```json
{"total": 800, "jobPostings": [{"title": "…", "externalPath": "/…",
  "locationsText": "…", "postedOn": "…"}]}
```

Pagination is `total` + `offset`/`limit`: step `offset` by the page
size while fetched rows are below `total`. There is no per-tenant
adapter matrix — any POST board with a `total` and an offset key in
its body pages the same way.

## Detail page

Each posting's `externalPath` is a board-relative listing path. The
helper maps it to an absolute URL (host + path), exactly like
`job_path` on other boards, so staged rows carry a real link.
`locationsText` maps to `location` and `postedOn` to `postedAt`.

## Recipe — quick start

```bash
python3 "$SUITE/skills/careers-discover/helpers/paginate-listings.py" \
  --url "https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs" \
  --triage-dir "$TRIAGE" --company "$NAME" \
  --method POST --body '{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}'
```

A `--body` without `--method` implies POST. Extra headers go on
`--header 'Name: value'` (repeatable); the offset key stepped per
page is `--body-offset-key` (default `offset`).

## Known caveats

- The #70 retry/backoff applies to POST exactly as to GET: 429 and
  5xx retry (bounded, `Retry-After` honoured, capped at 60s per wait);
  other 4xx fail fast and a persistent failure stays `blocked` /
  `complete: false`, so ingest cannot expire on it.
- A POST board that publishes no total is left after its first page:
  never invent a next request body for it.
- `--input` capture is unchanged: a saved page is never fetched,
  whatever descriptor is given, and always records `captured` /
  `complete: false`.
- The 15-page / 2000-listing caps apply to POST crawls exactly as to
  GET crawls; hitting one records `truncated`, never `complete`.
- This recipe was verified against a synthetic POST server in the
  test suite, not against a live tenant at crawl scale.
