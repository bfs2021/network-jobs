# Eightfold (PCSX search)

Eightfold is a shared ATS family, not one company: Microsoft, Starbucks,
Qualcomm, NVIDIA, Lockheed Martin, Ford, John Deere, PayPal, UKG and
others all serve the same PCSX search endpoint, paged 10 rows at a time.
The endpoint is public JSON — no login, no ATS adapter — but the boards
are huge and aggressively rate-limited, so the workflow is filter first,
quote coverage, never brute-force.

> **Prerequisites.** This recipe assumes the #69 pagination fix
> (`start=` envelopes, `positionUrl` → `url`, `data.count` →
> `reportedTotal`) and the #76 `position_details` description path. On an
> older install, update first: the pre-fix crawler staged one page as
> `complete` with empty URLs, which is exactly the #68 expiry hazard.
>
> Eightfold rows now also carry `postedAt` from `postedTs`/`creationTs`
> (#75), and `fetch_json` retries 429/5xx with `Retry-After` (#70).

## Cost model

| Action | Cost |
|---|---|
| List openings | free, public JSON |
| Job detail | free, public JSON (`position_details`, see below) |
| Apply | n/a for this skill |

## Auth

Public. No login wall on the search endpoint. Rate limiting is the
gate, not auth: Microsoft's PCSX host answered **429 on the first
request** from a clean client, and Qualcomm and Starbucks throttle
after short bursts (see #70). A DEV write-up on the same endpoint
documents the same behavior:
https://dev.to/glitchbound/microsoft-starbucks-and-qualcomm-jobs-api-the-eightfold-careers-search-ten-rows-at-a-time-2g4o

## Discovery

The careers site is usually `https://jobs.<domain>/` or
`https://<domain>/careers` (e.g. `jobs.nvidia.com`,
`jobs.starbucks.com`); the API host follows the same domain. Find the
endpoint in the network tab while the careers search loads, or try
the pattern below directly — it is the same across the family, with
only `domain` changing.

## Listing index

```
GET https://<careers-host>/api/pcsx/search?domain=<domain>&query=&location=&start=0
```

Response shape:

```json
{"data": {"positions": [ ... ], "count": 2690}}
```

- `data.positions` — the rows, **10 per page, always**. The endpoint
  ignores `num` / `limit`: asking for 100 still returns 10.
- `data.count` — the board's total. `paginate` records it as
  `reportedTotal`; quote coverage against it (`nListings of
  reportedTotal`, plus the filter used).
- `start` — a **row offset**, not a page number. Page 2 is
  `start=10`, page 3 is `start=20`, stepping by the rows returned
  (10), not by 1. `paginate` follows it.
- Row fields that matter: `positionUrl` (canonical listing link,
  mapped to `url`), `postedTs` / `creationTs` (Unix seconds or millis;
  mapped to `postedAt`), `workLocationOption` (e.g. `Remote` /
  `Onsite`; not a location key — use `locations`).
- `query` and `location` are applied **server-side**: a filtered
  request returns a smaller `data.count`, not the full board with
  client-side filtering. That is what makes huge boards tractable.

Board sizes recorded at the time of #71 (they drift): Starbucks 21,629
open roles, Lockheed Martin 6,672, NVIDIA 2,690, Qualcomm 2,029, Ford
657, John Deere 348, PayPal 318, UKG 271. At 10/page, Starbucks
unfiltered is ~2,163 requests; the default 15-page cap covers only 150
of them. Never start there.

## Detail page

Search rows carry no description, and the listing page is JS-rendered,
so `fetch-descriptions` resolves the body from the same PCSX API (#76):

```
GET https://<careers-host>/api/pcsx/position_details?domain=<domain>&position_id=<positionId>
→ {"data": {"jobDescription": "..."}}
```

The helper derives that URL from a row whose `sourceUrl` ends
`/api/pcsx/search`; other boards keep the listing-page fetch. The body is
stripped to plain text and truncated to the schema's ~10k chars.

## Recipe — quick start

The curl examples below were checked against live hosts for `start`,
`data.count`, `positionUrl` and `position_details`, and against a local
fake server for the paging/filter shape. Replace `<careers-host>` and
`<domain>` with the real board (e.g. host `jobs.nvidia.com`, domain
`nvidia.com`).

```bash
# 1. First page only: read data.count before deciding anything.
curl -s "https://<careers-host>/api/pcsx/search?domain=<domain>&query=&location=&start=0" \
  | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; print(d['count'], len(d['positions']))"
# Example output shape: 2690 10   — 10 of 2,690, not the board.

# 2. If the total dwarfs the 15-page budget (150 rows), filter first.
#    Prefer a query/location the user actually wants; re-read data.count.
curl -s -G "https://<careers-host>/api/pcsx/search" \
  --data-urlencode "domain=<domain>" --data-urlencode "query=product" \
  --data-urlencode "location=remote" --data-urlencode "start=0" \
  | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; print(d['count'], len(d['positions']))"

# 3. Paginate the filtered URL. start= is followed, reportedTotal is
#    recorded, and the crawl is marked truncated when it does not cover
#    the filtered total:
python3 "$SUITE/skills/careers-discover/helpers/paginate-listings.py" \
  --url "https://<careers-host>/api/pcsx/search?domain=<domain>&query=product&location=remote&start=0" \
  --triage-dir "$TRIAGE" --company "$NAME" --max-pages 15
# Quote coverage in the handoff: "150 of 517 (query=product,
# location=remote)" — never the partial count alone.

# 4. Only if the filtered total is still large AND the user agreed,
#    raise the budget explicitly — never silently:
#    --max-pages / --max-listings on paginate, or a narrower filter.
#    Ask one question first: filtered crawl, raised budget, or stop.

# 5. Fallback for a host that keeps 429ing or a gated board: fetch the
#    pages you need with curl (start=0, 10, 20, ...), merge the
#    positions arrays, and stage them as a capture — never as a
#    complete crawl:
curl -s "https://<careers-host>/api/pcsx/search?domain=<domain>&query=product&start=0" > /tmp/eightfold-p1.json
curl -s "https://<careers-host>/api/pcsx/search?domain=<domain>&query=product&start=10" > /tmp/eightfold-p2.json
python3 -c "
import json
rows = []
for f in ('/tmp/eightfold-p1.json', '/tmp/eightfold-p2.json'):
    rows += json.load(open(f))['data']['positions']
json.dump(rows, open('/tmp/eightfold-merged.json', 'w'))
print(len(rows), 'rows merged')"
python3 "$SUITE/skills/careers-discover/helpers/paginate-listings.py" \
  --triage-dir "$TRIAGE" --input /tmp/eightfold-merged.json --company "$NAME"
# Records complete: false / status: captured, so ingest cannot expire
# on a partial Eightfold crawl. Quote coverage, e.g. "20 of 318
# (query=product)" — never the partial count alone.

# 6. JD body for one role (manual check; fetch-descriptions does this
#    automatically for Eightfold rows):
curl -s "https://<careers-host>/api/pcsx/position_details?domain=<domain>&position_id=<positionId>"
```

## Known caveats

- **10/page, no page-size parameter.** `num` / `limit` are ignored;
  `start` steps by rows returned. A board that re-sorts between
  requests can repeat rows across pages — dedupe by position id.
- **Rate limits are the norm, not the exception.** Microsoft 429'd
  on the first request; Starbucks and Qualcomm throttle after short
  bursts. `fetch_json` (#70) retries 429/5xx with `Retry-After`
  (capped at 60s per wait) and bounded attempts — it does not make
  a 2,163-request crawl polite. Filter first; do not brute-force.
- **Totals live in the board envelope.** `data.count` sits beside
  `data.positions`; a `meta` block on the rows' container is also read.
  A count in an unrelated nested object is not. Never infer completeness
  from a page that returned fewer rows than the page size without
  checking the reported total.
- **Links and dates.** `positionUrl` maps to `url`; `postedTs` /
  `creationTs` map to `postedAt`. `workLocationOption` is not a location
  key — use `locations`.
- **No budget knob was added for this recipe.** The 15-page cap
  stays a hard cap: an adaptive budget that quietly grew to 2,163
  requests would be a rate-limit incident, not a feature. A deeper
  crawl is a user decision (filtered URL, or an explicit
  `--max-pages` / `--max-listings` raise after one question), and
  its coverage is always quoted.
