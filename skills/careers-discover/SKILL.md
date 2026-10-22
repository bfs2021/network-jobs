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
   5. If still blocked → ask **one question**: save the page and point me at the file, share a CDP endpoint, or stop here (recommended when the site hard-blocks: stop rather than invent openings).

4. **Extract listings**
   - Prefer structured JSON if the page or network tab exposes a jobs API
   - When a jobs JSON/API URL is known, **paginate in the helper** (not in the model context):

```bash
python3 "$SUITE/skills/careers-discover/helpers/paginate-listings.py" \
  --url "$JOBS_JSON_URL" --triage-dir "$TRIAGE" --company "$NAME" --max-pages 15
```

     Follows `page` / `cursor` / `offset` / `links.next` generically. Writes `index/listings.json`, `index/pagination.json` (`pages`, `complete`, `truncated`), and one fetch-log metadata file **per page**. Cap `maxPages`.
   - Else parse visible listing cards / table rows (still write fetch-log before summarizing)
   - For each role capture: `title`, `url` (required), `location` and `locations[]` when shown, `department`, `externalId` if the JSON has an id, `salary` / `postedAt` only if shown, `sourceUrl`
   - Never invent ATS-specific parsers. If the helper truncates, set `complete: false` and say so in INVENTORY.

5. **Stage**

```text
$DATA/triage/careers-<slug>-<YYYY-MM-DD>/
├── INVENTORY.md
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

Compares listing-set hash to `companies.json`. Caps: **15 API pages**, **5 browser load-more pages**, 2000 listings. Fetch-log metadata is written per page by paginate (default). If `unchanged: true`, say so and **skip ingest** (delta is empty) — do not expire anything. If the hash changed, ingest is a fingerprint merge (new/changed rows only; expiry still requires `pagination.complete`).

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

Titles alone cap everything downstream. Fetch bodies for the shortlist —
never the whole board:

```bash
python3 "$SUITE/skills/careers-discover/helpers/fetch-descriptions.py" \
  --triage-dir "$TRIAGE" --company "$NAME"
```

Fills `description` (truncated plain text) plus `descriptionFetchedAt` /
`descriptionSource`, and fills `department` from the ATS payload when the
row lacks one. Fetch failures never fail the run — undescribed rows score
exactly as before. Bodies ride through ingest automatically.

7b. **Fetch descriptions for matches (required helper)**

Titles alone cap everything downstream (scoring, fit brief, intros). Fetch
bodies for the shortlist — never the whole board:

```bash
python3 "$SUITE/skills/careers-discover/helpers/fetch-descriptions.py" \
  --triage-dir "$TRIAGE" --company "$NAME"
```

This fills `description` (truncated plain text, ~10k chars) plus
`descriptionFetchedAt`/`descriptionSource` on matches.json rows, and fills
`department` from the ATS payload when the row lacks one. Fetch failures
never fail the run — undescribed rows score exactly as before. Bodies flow
through ingest automatically (`dict` copies end to end).

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
   - Careers URL(s) used
   - Listing count **and** “N of M match prefs”
   - Department histogram (from the helper)
   - Pagination `{pages, complete, truncated}` and crawl hash / skip-if-unchanged
   - Connections at company (from graph) + sample people
   - Caveats (bot wall, partial pagination, uncertain domain)
   - Next steps (“ingest matches?” / “ingest all” / department slice)

9. **Hand off (hard stop)**
   - Point at each triage dir. Quote the helper’s `showing` line (e.g. `14 of 120 match prefs`) — do not paste the full listings array.
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

Workflow: open URL → snapshot → interact by refs → re-snapshot after DOM changes → write fetch-log before summarizing. Cap **5** “Load more” / next-page clicks; if you stop early, pagination.complete is false.

### 3. User capture / CDP

Use when tier 2 still can’t see listings (hard login, hard captcha, geo block). Ask **one question** — save/share the page (recommended), share a CDP endpoint, or stop — then stage from that capture. Do not invent openings.

### Decision rule

```text
try WebFetch/curl
  → real listings?        stage them
  → empty / JS / challenge?  agent-browser once
  → still blocked?           ask one question (save/share capture, or stop)
```

Never start with agent-browser for a simple static page. Never keep retrying browser against a hard auth wall.

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
   Put the version in the fetch-log `notes` so the next run does not re-derive it.

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
- [`helpers/paginate-listings.py`](helpers/paginate-listings.py) — page/cursor/offset into `listings.json` + `pagination.json`
- [`helpers/crawl-state.py`](helpers/crawl-state.py) — listing-set hash vs lastCrawl; `--stamp` after a successful extract
- [`helpers/match-prefs.py`](helpers/match-prefs.py) — score listings vs `preferences.json` → `index/matches.json`
- [`helpers/review-matches.py`](helpers/review-matches.py) — cluster shortlist into families, explain leaks, apply vetoes

## Related

- After confirm → **jobs-ingest**
- Search corpus → **network-jobs**
