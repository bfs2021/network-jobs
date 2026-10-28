---
name: careers-discover
description: Find company career pages and stage open job listings into local triage for Network Jobs. Use when discovering openings at companies from the user's LinkedIn graph — no ATS adapters; the model finds and extracts.
license: MIT
allowed-tools: Bash(*)
metadata:
  version: "1.1.0"
---

# Careers Discover

Orchestration only: discover a company's careers surface, extract open roles, stage under `triage/`. Does **not** write the corpus — that is `jobs-ingest` after user confirmation.

Read [SCHEMA.md](../../SCHEMA.md) first. Pattern inspired by Provenance `source-scrape`.

## When to use

- User wants openings at companies from `~/.network-jobs/companies/companies.json`
- User names companies to refresh
- User asks to “crawl” / “find jobs at …” their network

## Non-negotiables

- Stage everything under `triage/careers-<slug>-<YYYY-MM-DD>/` — never write `corpus/` from this skill.
- Do **not** invent titles, salaries, or dates. Omit unknown fields.
- Write fetch-log entries **before** summarizing results to the user.
- No ATS allowlist / no Greenhouse-Lever-Ashby parser code. Prefer whatever public page or JSON the site exposes.
- One company (or small batch the user approved) per focused run when possible — cleaner logs.

## Asking the user

Whenever the next step is picking from a known set, use the host's choice tool —
`AskUserQuestion` (Claude Code), `question` (opencode), `ask_user_choice`, or the
harness equivalent — instead of a prose list.

- **One question per call.** Ask it, wait for the answer, then ask the next.
  Never bundle two questions into one prompt or one tool call, even when the
  tool accepts several.
- **Recommend, and say why.** Lead with the option you would pick and a one-line
  reason.
- Put the deciding data in the option label (match counts, department, family),
  never in surrounding prose the user has to correlate.
- Always keep a free-text / "none of these" path.
- No such tool in this host? Use a short numbered list, recommendation first,
  never more than 4 options.

## Workflow

```bash
DATA="${NETWORK_JOBS_HOME:-$HOME/.network-jobs}"  # always resolve from the env; never export a literal DATA
SUITE="$(cat "$DATA/suite-root" 2>/dev/null || true)"
SUITE="${NETWORK_JOBS_SUITE:-${SUITE:-}}"
```

1. **Pick companies**
   - Read `$DATA/companies/companies.json`
   - Prefer high `connectionCount`, or filter by user query
   - If the user named no companies, ask **one question** — a multi-select over the top ~8 by connection count (counts in the labels) — and run only what they pick. Recommend the top 3 by `connectionCount`.
   - If `preferences.json` lists `categories` / `notes`, bias toward matching companies when the user has not named any
   - If `formerEmployerPolicy` is `exclude` (default when set), **skip** companies in `formerEmployers` unless the user explicitly named them
   - If policy is `ask` and a top company is a former employer, call that out and ask **one multi-select** over the flagged companies (recommended: include none of them this run) before discovering.
   - Ask before bulk-running more than ~5 companies in one go, as **one question** (recommended: start with the top 3–5 by connection count).

   High connection counts at former employers are common and **not** automatically a signal to prioritize those companies.

2. **Check recipes**
   - Look in this skill’s [`references/`](references/) for a matching company or site-family recipe
   - If none exists and the site is painful, draft one from [`references/_template.md`](references/_template.md) **with the user** before scaling

3. **Discover careers URL** (in order)
   1. Known `domain` on the company record, try `/careers`, `/jobs`, `/careers/openings`, `/join`
   2. Web search: `"{company}" careers` / `"{company}" jobs`
   3. Company homepage → follow Careers/Jobs nav
   4. If blocked or SPA-only → `agent-browser` (snapshot → refs)
   5. If still blocked → the concrete user-capture handoff in [§3](#3-user-capture-the-default-for-gated-boards): exact URL, what to save, `$TRIAGE/captures/`, then `paginate --input`. Recommend stopping rather than inventing openings when the site hard-blocks.

4. **Extract listings**
   - Prefer structured JSON if the page or network tab exposes a jobs API
   - When a jobs JSON/API URL is known, **paginate in the helper** (not in the model context):

```bash
python3 "$SUITE/skills/careers-discover/helpers/paginate-listings.py" \
  --url "$JOBS_JSON_URL" --triage-dir "$TRIAGE" --company "$NAME" --max-pages 15
```

     Follows `page` / `cursor` / `offset` / `start` / `links.next` generically. Writes `index/listings.json`, `index/pagination.json` (`pages`, `complete`, `truncated`, `status`, plus `reportedTotal` when the board reports a count and `rowsFetched` / `deduped` coverage), and one fetch-log metadata file **per page**. Cap `maxPages`.

     `status` is `complete` (clean crawl covering the board), `truncated` (a **partial** crawl: a page/listing cap was hit before the board total was covered, it began mid-board, or it staged fewer unique roles than the board reported), `empty` (clean, parsed board with zero roles), `blocked` (the fetch failed or nothing parseable came back — **not** “no openings”) or `captured` (a `paginate --input` run over a user-saved page — never the whole board). `complete` / `truncated` stay the source of truth for expiry; `status` is the readable headline. `--input` always records `complete: false` / `status: captured`. A crawl with zero incoming rows does **not** expire prior open roles at ingest unless the operator passes `--force-expire` — even if status is omitted or `null`. When `pagination.reportedTotal` is present, quote coverage (`nListings` of `reportedTotal`) and, if the board is much larger than what fit in the cap, offer a deeper run with `--max-pages` / `--max-listings` before calling it done.

     **Huge boards: filter first, quote coverage (#71).** When a
     board's reported total is much larger than the 15-page budget,
     do not silently crawl a slice of it and present the slice as the
     board. The standing example is Eightfold (see
     [`references/eightfold.md`](references/eightfold.md)): 10 rows
     per page, so 15 pages cover only 150 — Starbucks at 21,629 roles
     would need ~2,163 requests unfiltered. Prefer a filtered URL
     first (`query` / `location` where the API applies them
     server-side), or ask the user **one question** before raising
     `--max-pages` / `--max-listings`. Always quote coverage in the
     handoff (`150 of 21,629`, plus the filter used) — a partial
     count is never the whole board. The 15-page cap stays a hard
     cap; no adaptive budget knob was added, because a budget that
     quietly grows to thousands of requests is a rate-limit incident,
     not a feature. Retry defaults for throttled boards (#70):
     `fetch_json` retries 429/500/502/503/504 only, up to 3 attempts
     total, honouring `Retry-After` capped at 60s per wait, otherwise
     exponential backoff with jitter; other 4xx fail fast and a
     persistent failure stays `blocked` / `complete: false`.

     **POST/JSON-body boards (#74).** Some boards are POST, not GET —
     Workday CXS is the pattern (see
     [`references/workday.md`](references/workday.md)): the search
     is `POST …/wday/cxs/{tenant}/{site}/jobs` with a JSON body
     (`limit`, `offset`, `appliedFacets`, `searchText`) and returns
     `{total, jobPostings}`. A GET to that endpoint returns nothing
     useful, so do not conclude the board is empty. Use the helper's
     request descriptor instead — a `--body` without `--method`
     implies POST, and the helper steps the body's `offset` key by
     page size while the board total says rows remain:

```bash
python3 "$SUITE/skills/careers-discover/helpers/paginate-listings.py" \
  --url "$JOBS_JSON_URL" --triage-dir "$TRIAGE" --company "$NAME" \
  --method POST --body '{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}'
```

     Stepping is shape-based (`total` + `offset`/`limit`) with no
     per-tenant adapter; `externalPath` is mapped to an absolute
     listing URL, and the #70 retry/backoff applies to POST exactly
     as to GET. A POST board that publishes no total is left as
     `truncated` / `complete: false` after its first page — never
     invent a next body for it. `--input` capture is unchanged: a
     saved page is never fetched.
   - Else parse visible listing cards / table rows (still write fetch-log before summarizing). If the board is gated and you cannot read it, stop here and use the user-capture handoff in [§3](#3-user-capture-the-default-for-gated-boards) — do not stage an empty board as success.
   - For each role capture: `title`, `url` (required), `location` and `locations[]` when shown, `department`, `externalId` if the JSON has an id, `salary` / `postedAt` only if shown, `sourceUrl`
   - Never invent ATS-specific parsers. If the helper truncates, `status` is `truncated`; if nothing parseable came back, `status` is `blocked`; if the board parses cleanly and lists zero roles, `status` is `empty`. Never pad rows from partial cards.

5. **Stage**

```text
$DATA/triage/careers-<slug>-<YYYY-MM-DD>/
├── INVENTORY.md
├── captures/                  # user-supplied JSON/HTML for a gated board
├── index/listings.json
├── index/matches.json
└── fetch-log/<timestamp>-<label>.json
```

Use [`helpers/stage-company.sh`](helpers/stage-company.sh) to mkdir + write skeleton files, then fill listings.

6. **Crawl budget (required helper)**

```bash
python3 "$SUITE/skills/careers-discover/helpers/crawl-state.py" \
  --triage-dir "$TRIAGE" --company "$NAME"
# After a successful extract (even truncated), stamp lastCrawl + hash.
# Pass the listings JSON endpoint as --source-url so `network-jobs refresh`
# can re-crawl this board later without a model in the loop.
python3 "$SUITE/skills/careers-discover/helpers/crawl-state.py" \
  --triage-dir "$TRIAGE" --company "$NAME" --stamp --source-url "$JOBS_JSON_URL"
```

Compares listing-set hash to `companies.json`. Caps: **15 API pages**, **5 browser load-more pages**, 2000 listings. A board serving 10 rows per page covers only 150 roles at the default cap; check `pagination.reportedTotal` and tell the user the coverage, then raise `--max-pages` / `--max-listings` for a real crawl if they want the rest. Fetch-log metadata is written per page by paginate (default). If `unchanged: true`, say so and **skip ingest** (delta is empty) — do not expire anything. If the hash changed, ingest is a fingerprint merge (new/changed rows only; expiry still requires `pagination.complete`).

7. **Shortlist against prefs (required helper)**

After `index/listings.json` is written, run — do **not** score the board in the model:

```bash
DATA="${NETWORK_JOBS_HOME:-$HOME/.network-jobs}"  # always resolve from the env; never export a literal DATA
SUITE="$(cat "$DATA/suite-root" 2>/dev/null || true)"
SUITE="${NETWORK_JOBS_SUITE:-${SUITE:-}}"
python3 "$SUITE/skills/careers-discover/helpers/match-prefs.py" \
  --triage-dir "$TRIAGE" --company "$NAME"
```

This writes `index/matches.json` and patches INVENTORY with **N of M match prefs** plus a department histogram. Quiet JSON on stdout includes composition (`matchDepartments`, `matchCategories`, `reasonCounts`, `warnings`).

7b. **Fetch descriptions for matches (required helper)**

Titles alone cap everything downstream (scoring, fit brief, intros). Fetch
bodies for the shortlist — never the whole board:

```bash
python3 "$SUITE/skills/careers-discover/helpers/fetch-descriptions.py" \
  --triage-dir "$TRIAGE" --company "$NAME"
```

`--company` is optional and only labels the run's JSON summary (same flag as
`match-prefs` / `review-matches`); the source file is still `matches.json`
unless you pass `--all`. This fills `description` (truncated plain text, ~10k
chars) plus `descriptionFetchedAt`/`descriptionSource` on matches.json rows,
and fills `department` from the ATS payload when the row lacks one. Fetch
failures never fail the run — undescribed rows score exactly as before. Bodies
flow through ingest automatically (`dict` copies end to end). The file stays the
documented object (`jobs[]` + summary), and re-running `match-prefs`
restores the bodies instead of clobbering them.

To review the shortlist as job families instead of a flat list (so deal-breakers
are set against observed counts), run:

```bash
python3 "$SUITE/skills/careers-discover/helpers/review-matches.py" \
  --triage-dir "$TRIAGE" --company "$NAME"
```

This clusters matches by category + title stem, reports near-miss leaks grouped
by rule, and — with `--veto "phrase"` (repeatable) — appends to
`preferences.json` deal-breakers, re-runs match-prefs, and prints
before/after counts.

8. **INVENTORY.md must include**
   - Company name + slug
   - **`Careers URL`** in the header, patched from `index/*.json` by `paginate`, `match-prefs` and `crawl-state` on every write, so do not hand-maintain it. **`Status`** is patched too when the helper has pagination data or when the header still holds a placeholder (`(pending)`/empty); a hand-filled value such as `captured` is kept. Free-form fields (`Slug`, `Date`, `Caveats`, `Next steps`) are never patched.
   - **`Listings`** — and **`Pagination`** when the helper has pagination data — in the `<!-- nj-summary -->` Pipeline summary block. The block is their canonical home, so each label appears exactly once in the file. The skeleton's `Listings`/`Pagination` header lines are pre-helper placeholders and are removed on the first helper write that renders those labels.
   - If no helper ran (a manual card extract), fill the header fields yourself from the files you wrote. A manual extract without `index/pagination.json` writes `Pagination` in the header — e.g. `pages=1, complete=false, truncated=false, status=captured (manual card extract)` — and `match-prefs` must not delete it (the summary block simply omits `Pagination`).
   - Listing count **and** “N of M match prefs”
   - Department histogram (from the helper)
   - Pagination `{pages, complete, truncated, status}` and crawl hash / skip-if-unchanged
   - Connections at company (from graph) + sample people
   - Caveats (bot wall, partial pagination, uncertain domain)
   - Next steps (“ingest matches?” / “ingest all” / department slice)

   `Status` must agree with `index/pagination.json`: `complete` (clean crawl),
   `truncated` (cap hit — say which), `empty` (clean board, zero roles — say so
   plainly), `blocked` (nothing parseable came back — say which URL was
   unreachable and what the user can do) or `captured` (rows from a user save —
   say “user capture, not a full crawl”). Without `index/pagination.json`, a
   placeholder `Status` is filled from the listing count — `blocked` for zero
   rows, otherwise `complete` — while a hand-filled value is preserved. A
   `blocked` board is **not** a company with no openings; never present it as
   one in chat or in the header.

9. **Hand off (hard stop)**
   - Point at each triage dir. Quote the helper’s `showing` line (e.g. `14 of 120 match prefs`) — do not paste the full listings array.
   - For a `blocked`, `truncated` or `captured` dir, lead with the state (“could not read this board — 0 captured”, “page cap hit — 10 of ~952”, “user capture — one page of ~952”), then give the one-line capture handoff (URL to open, file to save, path to drop it). Never quote a partial count as if it were the whole board.
   - Offer `review-matches` to walk families before ingest (“18 matched across 4 families — want to veto any?”). Ask it as **one multi-select** over families, with match counts in the labels and your recommended vetoes marked, not a prose list.
   - If the run surfaced something that looks like a **suite defect** rather than a site quirk (malformed listings, dedup collapse, zero-yield pagination on a board that should list), say so and offer to file it: `network-jobs report --dry-run` first, show the bundle, and only file after explicit confirmation (one question: file it / show me the bundle first).
   - Then ask **one question**: run `jobs-ingest` on **matches** (recommended — the board was already scored against prefs), **all** listings, or a department slice. Put the counts in the labels (`matches — 14`, `all — 120`, `Engineering — 31`).
   - **STOP.** Do **not** invoke `jobs-ingest`, rebuild the corpus, or resume a job search in the same turn.
   - Only after the user explicitly confirms (e.g. “ingest these”, “ingest matches”, “ingest all”, “promote the Google batch”) should you load **jobs-ingest**.

## Bot / fetch tiers

Escalate only as far as needed. Prefer the cheapest tier that returns real listings.

### 1. `curl` / WebFetch (default)

Use when:
- The careers URL or a linked jobs JSON returns substantive HTML/JSON (titles + links visible in the body)
- A public jobs API is obvious (`/api/…`, `…/jobs.json`, common ATS JSON feeds)
- You’re probing `/careers`, `/jobs`, or a domain homepage for a careers link

Skip straight past this tier only if you already know the site is a heavy SPA from a recipe or prior fetch-log.

### 2. `agent-browser`

Use when tier 1 yields:
- Empty shell / “enable JavaScript” / framework root with no listings
- Soft bot interstitial or cookie wall that blocks content
- Pagination or filters that require click / “Load more” / infinite scroll
- Client-side routing where listing URLs aren’t discoverable from static HTML

Workflow: open URL → snapshot → interact by refs → re-snapshot after DOM changes → write fetch-log before summarizing.

**Budget per board: 2 endpoint/route probes and 5 browser interactions, total.**
Count an API/XHR probe (including a replay attempt) and a route you haven't tried as a probe; count every open/snapshot/click/scroll as an interaction, with “Load more” / next-page clicks capped at 5. When the budget is spent, stop interacting: record what was captured, let the helper stamp `status: truncated` (rows captured but incomplete) or `blocked` (nothing parseable), write the fetch-log, and escalate to tier 3. Do not keep improvising on a hard wall, and do not pad rows from the cards you managed to see.

Site-family recipes in [`references/`](references/) may document a known endpoint and override the probe budget for that board only — say which recipe you used in the fetch-log `notes`.

### 3. User capture (the default for gated boards)

Use when tier 2 still can’t see listings (signed GraphQL, authenticated SPA, hard login, captcha, geo block). This is a concrete handoff, not a vague ask:

1. Ask **one question** — save a capture (recommended), share a CDP endpoint, or stop. Recommend capture because `paginate --input` already ingests it.
2. Tell the user the **exact URL to open** (the careers/search URL, filters and query included) and **what to save**:
   - best: the jobs JSON/API response — DevTools → Network → the request that returned the postings → “Save response” as `.json`
   - acceptable: the fully rendered page — browser “Save page as” → `.html`
3. Tell them **where to drop it**: `$TRIAGE/captures/<label>.json` (or `.html`). Create `captures/` first if needed.
4. Ingest a JSON capture directly:

```bash
python3 "$SUITE/skills/careers-discover/helpers/paginate-listings.py" \
  --triage-dir "$TRIAGE" --input "$TRIAGE/captures/<label>.json" --company "$NAME"
```

   An HTML capture is not parsed by the helper: read the rendered cards yourself, write only the rows that are actually on the page into a JSON array (`title`, `url`, `location`, optional `department`) under `captures/`, then run the same `--input` command. **Never invent listings**, and never scale a partial card count (e.g. “10 of 952”) into rows.
5. Write the fetch-log entry **before** summarizing — `method` and `disposition` are both `user-capture`:

```bash
mkdir -p "$TRIAGE/fetch-log"
cat > "$TRIAGE/fetch-log/$(date -u +%Y%m%dT%H%M%SZ)-user-capture.json" <<JSON
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "method": "user-capture",
  "url": "<careers URL the user opened>",
  "status": null,
  "capturePath": "$TRIAGE/captures/<label>.json",
  "disposition": "user-capture",
  "notes": "JS/GraphQL-gated board; user saved the response"
}
JSON
```

   `paginate --input` is a **user capture**, and records it: `complete: false` and `status: "captured"` in `index/pagination.json` (even when every row parsed). That is machine-readable, so ingest cannot expire on a capture — a one-page save of a ~952-role board will never close the other ~950 rows. Do not flip those fields by hand and never present the count as the whole board.
6. If the user declines or the capture yields nothing, keep `status: blocked` in `pagination.json` and INVENTORY and say plainly that the board could not be read. That is a valid result; a fabricated shortlist is not.

### Decision rule

```text
try WebFetch/curl
  → real listings?            stage them
  → empty / JS / challenge?   agent-browser (≤2 endpoint probes, ≤5 interactions)
  → still blocked?            mark status blocked|truncated, ask one question
                              → user capture → paginate --input captures/<file>
                                 (records status captured, complete false)
                              → CDP → stage from the session, then same helpers
                              → stop → leave status blocked, say why
```

Never start with agent-browser for a simple static page. Never keep retrying browser against a hard auth wall, and never report a blocked board as “no openings”. A board that parses cleanly and lists zero roles is `status: empty` — that is “no openings” on this URL. Ingest still will not expire prior open roles for that company unless you pass `--force-expire`, because a filtered or region-limited crawl can look empty — even when status is omitted or `null`.

## Browser version floor

`agent-browser` is optional but load-bearing for JS-heavy boards, and a stale
build fails in ways that look like the site's fault. Check the version against
the floor declared in the suite's `package.json` (`agentBrowser.minVersion`):

```bash
agent-browser --version                      # e.g. "agent-browser 0.39.1"
python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["agentBrowser"]["minVersion"])' "$SUITE/package.json"
```

If the installed version is **below** the floor:

1. Say it once, plainly: `agent-browser X is below the supported floor Y — run network-jobs update`.
2. Do **not** run `npm i -g` yourself mid-run, and do **not** retry the same failing
   command twice. A version-floor miss is a user action, not something to work around.
3. Fall back to the user-capture path (save the page, `paginate --input`) or stop.
   Record the version in the fetch-log `agentBrowserVersion` field so the next run
   does not re-derive it.

`network-jobs doctor` prints `WARN agent-browser X < Y` for this case, and
`network-jobs update` upgrades only when below the floor — never just because a
newer build exists.

### Before declaring a command missing

`agent-browser --help` groups nested subcommands under headings (`Network:`,
`Storage:`, …) that read like top-level commands. `har` is `agent-browser network har start`,
not `agent-browser har`. A first `Unknown command: X` means "try `<group> X`",
not "this build lacks the feature" — check `agent-browser <group> --help` before
concluding the tool is broken, and never retry the same wrong invocation.

## Helpers

- [`helpers/stage-company.sh`](helpers/stage-company.sh) — create triage dir skeleton
- [`helpers/fetch-page.sh`](helpers/fetch-page.sh) — curl page to fetch-log + stdout path
- [`helpers/paginate-listings.py`](helpers/paginate-listings.py) — page/cursor/offset/start into `listings.json` + `pagination.json`
- [`helpers/crawl-state.py`](helpers/crawl-state.py) — listing-set hash vs lastCrawl; `--stamp` after a successful extract
- [`helpers/match-prefs.py`](helpers/match-prefs.py) — score listings vs `preferences.json` → `index/matches.json`
- [`helpers/fetch-descriptions.py`](helpers/fetch-descriptions.py) — fill JD bodies for matches (`--company` optional)
- [`helpers/review-matches.py`](helpers/review-matches.py) — cluster shortlist into families, explain leaks, apply vetoes

## Related

- After confirm → **jobs-ingest**
- Search corpus → **network-jobs**
