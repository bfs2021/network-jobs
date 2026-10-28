# #72 live-board search — evidence log (Batch 3)

Date: 2026-10-27 (probes run from this VM, one small first page per board
unless noted; no board was crawled at scale).

## What would count as proof

Issue #72 is explicit: **do not add aliases speculatively**. A board proves
the widening only if it meets *all* of these at once:

- its GET API pages by a row-offset query param in
  `skip` / `from` / `begin` / `recordStart` (not `start`, which #69 already
  covers, and not `offset`, which `detect_next` already handles);
- a total/count sits **beside the exact extracted rows** (identity rule) in
  the same envelope, so the completeness gate can trust it;
- the URL carries no `page` / `pageNumber` / `offset` param;
- the offset value is a plain row number (`0 <= offset < total`), not a
  filter-like value (`from=2020`, `start=2026-01-01` must still decline).

## Boards probed live

| Board (endpoint pattern) | Paging param actually used | Rows / count shape | Verdict |
|---|---|---|---|
| Lever, Spotify (`api.lever.co/v0/postings/spotify?mode=json&skip=0&limit=2`, then `skip=2`) | `skip` + `limit` — confirmed: `skip=0` and `skip=2` returned disjoint posting ids | **Bare JSON array, no count field at all** | Closest real alias, but **not proof**: with no total, the #72 guard "only follow when a total is known" can never fire. Lever termination is short-page, a different rule — a future skip implementation would need its own issue, not #72. |
| Greenhouse, Stripe (`boards-api.greenhouse.io/v1/boards/stripe/jobs`) | None — one call returned all 714 rows | `jobs` + `meta.total` at top level | Not a row-offset envelope; no pagination to follow. |
| Ashby, OpenAI (`api.ashbyhq.com/posting-api/job-board/openai`) | None — one call returned 801 rows | `jobs`, no total | No pagination, no count. |
| SmartRecruiters, ServiceNow (`api.smartrecruiters.com/v1/companies/servicenow/postings?limit=2&offset=0`) | `offset` (already handled by `detect_next`) | `content` + top-level `totalFound` (735) | Already handled; not a new alias. |
| Eightfold, NVIDIA — control (`jobs.nvidia.com/api/pcsx/search?domain=nvidia.com&start=0`) | `start` (already the #69 case) | `data.positions` + `data.count` beside them (2,687, 10 rows) | Proves `start` only — the case #69 already covers. Not proof for `skip`/`from`/`begin`/`recordStart`. |
| Amazon (`amazon.jobs/en/search.json?offset=0&result_limit=2`) | `offset` (already handled) | `jobs` + top-level `hits` (10,000) | Already handled; not a new alias. |

## Boards checked from documentation only (not live-probed)

- **Oracle Recruiting Cloud**: finder query carries `limit=200,offset=0`
  (documented) — `offset` is already handled, and `TotalJobsCount` sits
  inside `items[0]`, not beside the nested `requisitionList` rows. Not a
  new alias, and the tenant-specific finder was not probed live.
- **SAP SuccessFactors OData**: pagination is `$skip` / `$top`, not the
  bare `skip` param, and the API is OAuth-gated — not a public GET board
  this tool can crawl. Not proof.
- **Phenom career sites**: search is POST-body based and tenant-specific,
  not a GET query-param envelope. Not a #72 candidate; not live-probed.

## Web searches for the remaining aliases

- `recordStart` as a job-board pagination param: hits were unrelated
  systems (a BMC device API, a SmartSimple `othersettings` field, a
  Mimecast pagination meta block) — none is a job board.
- `begin` as a job-listings pagination param: no job-board API found
  using it as a row offset; hits were `page`/`page_size` APIs.
- `from` as a job-search offset: the public job APIs found use
  `page`, `cursor`, `offset`, or POST bodies — no GET `from` + sibling
  count envelope surfaced.

## Conclusion

**No live board proved #72 in this search, so the alias widening was NOT
implemented.** `detect_next` keeps its current behaviour: `start` is the
#69 case (itself still unmerged on this base), `offset` is already
handled, and `skip`/`from`/`begin`/`recordStart` remain treated as
filters. #72 stays open; the next search should start from the one real
lead above (a `skip` board that also publishes a total) or from a board
whose network tab shows `from`/`begin`/`recordStart` beside a count.
