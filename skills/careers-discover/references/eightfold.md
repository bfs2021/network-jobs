# Eightfold (PCSX search)

Eightfold is a shared ATS family, not one company: Microsoft, Starbucks,
Qualcomm, NVIDIA, Lockheed Martin, Ford, John Deere, PayPal, UKG and
others all serve the same PCSX search endpoint, paged 10 rows at a time.
The endpoint is public JSON — no login, no ATS adapter — but the boards
are huge and aggressively rate-limited, so the workflow is filter first,
quote coverage, never brute-force.

> **Status on current main — read this before running anything.**
> `paginate` does **not** yet follow Eightfold `start=` envelopes:
> #69 (Andy Tillo's envelope proposal) is still open and unmerged, and
> #68 (a board marked `complete` after one page, then ingest closing
> the rest) is still open. Verified on current main: an Eightfold-shaped
> page stages 10 rows as `complete: true` with empty `url`s, because
> `positionUrl` is not yet in `URL_KEYS` and no total is read from
> `data.count`. Do **not** run direct `paginate --url` on an Eightfold
> board today and treat its output as complete — that is exactly the
> #68 expiry hazard. Until #69 lands, use the curl + merged-capture
> path in "Recipe — quick start" below (`paginate --input`, which
> records `captured`, never `complete`). Once #69 lands, direct
> `paginate --url` with `start=` should replace it, and
> `pagination.json` will carry the board total as `reportedTotal`.
>
> What Batch 1 did change: #75 maps `postedTs` / `creationTs` epochs
> to `postedAt`, so Eightfold rows now carry a posting date; #70 adds
> bounded 429/5xx retry with `Retry-After` to `fetch_json`.

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
- `data.count` — the board's total. This is the number to quote
  coverage against (`nListings of data.count`). It is **not** yet
  surfaced as `reportedTotal` by `paginate` on current main (see the
  status note above); read it off the first page yourself.
- `start` — a **row offset**, not a page number. Page 2 is
  `start=10`, page 3 is `start=20`, stepping by the rows returned
  (10), not by 1.
- Row fields that matter: `positionUrl` (the canonical listing link
  — not yet mapped to `url` by `paginate` on current main),
  `postedTs` / `creationTs` (Unix seconds; mapped to `postedAt` by
  #75), `workLocationOption` (e.g. `Remote` / `Onsite`).
- `query` and `location` are applied **server-side**: a filtered
  request returns a smaller `data.count`, not the full board with
  client-side filtering. That is what makes huge boards tractable.

Board sizes recorded in #71 (not re-probed live for this recipe):
Starbucks 21,629 open roles, Lockheed Martin 6,672, NVIDIA 2,690,
Qualcomm 2,029, Ford 657, John Deere 348, PayPal 318, UKG 271.
At 10/page, Starbucks unfiltered is ~2,163 requests; the default
15-page cap covers only 150 of them. Never start there.

## Detail page

Search rows carry no description, and the listing page is
JS-rendered, so `fetch-descriptions` is likely title-only for this
family today (see #76, which proposes using this endpoint).
Eightfold exposes the body the site renders:

```
GET https://<careers-host>/api/pcsx/position_details?domain=<domain>&position_id=<positionId>
→ {"data": {"jobDescription": "..."}}
```

## Recipe — quick start

All curl examples below were tested against a local fake server
returning the Eightfold shape above (10/page ignoring `num`,
`start` offsets, server-side `query` / `location` filtering,
`position_details` bodies) — not against live boards at
brute-force scale. Replace `<careers-host>` and `<domain>` with the
real board (e.g. host `jobs.nvidia.com`, domain `nvidia.com`).

```bash
# 1. First page only: read data.count before deciding anything.
curl -s "https://<careers-host>/api/pcsx/search?domain=<domain>&query=&location=&start=0" \
  | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; print(d['count'], len(d['positions']))"
# Example output shape: 2690 10   — 10 of 2,690, not the board.

# 2. If the total dwarfs the 15-page budget (150 rows), filter first.
#    Prefer a query/location the user actually wants; re-read data.count.
curl -s "https://<careers-host>/api/pcsx/search?domain=<domain>&query=product&location=remote&start=0" \
  | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; print(d['count'], len(d['positions']))"

# 3. Only if the filtered total is still large AND the user agreed,
#    raise the budget explicitly — never silently. (Once #69 lands,
#    this is --max-pages on paginate; until then, see step 4.)
#    Ask one question first: filtered crawl, raised budget, or stop.

# 4. Current-main path: fetch the pages you need with curl (start=0,
#    10, 20, ...), merge the positions arrays into one JSON file, and
#    stage it as a capture — never as a complete crawl:
curl -s "https://<careers-host>/api/pcsx/search?domain=<domain>&query=product&location=&start=0" > /tmp/eightfold-p1.json
curl -s "https://<careers-host>/api/pcsx/search?domain=<domain>&query=product&location=&start=10" > /tmp/eightfold-p2.json
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
# on a partial Eightfold crawl. Quote coverage in the handoff, e.g.
# "20 of 318 (query=product)" — never the partial count alone.

# 5. JD body for one role (for #76's future use; fetch-descriptions
#    does not resolve this endpoint yet on current main):
curl -s "https://<careers-host>/api/pcsx/position_details?domain=<domain>&position_id=<positionId>"
```

## Known caveats

- **10/page, no page-size parameter.** `num` / `limit` are ignored;
  `start` steps by rows returned. A board that re-sorts between
  requests can repeat rows across pages — dedupe by `positionId`.
- **Rate limits are the norm, not the exception.** Microsoft 429'd
  on the first request; Starbucks and Qualcomm throttle after short
  bursts. `fetch_json` (#70) retries 429/5xx with `Retry-After`
  (capped at 60s per wait) and bounded attempts — it does not make
  a 2,163-request crawl polite. Filter first; do not brute-force.
- **Totals live one envelope down.** `data.count` sits beside
  `data.positions`, not at the top level — that is why #68 happened
  and why #73 (totals not beside the rows) is a separate, harder
  problem. Never infer completeness from a page that returned fewer
  rows than the page size without checking `data.count`.
- **Links and dates, current main.** `positionUrl` is not yet
  mapped to `url` and `workLocationOption` is not a location key,
  so rows staged today may carry empty links — use `positionUrl`
  from the raw payload when pointing the user at a role, and do not
  present a title-only row as if it had a verified apply link.
  `postedTs` / `creationTs` now map to `postedAt` (#75).
- **No budget knob was added for this recipe.** The 15-page cap
  stays a hard cap: an adaptive budget that quietly grew to 2,163
  requests would be a rate-limit incident, not a feature. A deeper
  crawl is a user decision (filtered URL, or an explicit
  `--max-pages` / `--max-listings` raise after one question), and
  its coverage is always quoted.
