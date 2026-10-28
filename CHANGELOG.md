# Changelog

## Unreleased

### Fixed
- `careers-discover` called Eightfold-style boards (NVIDIA, UKG) **complete
  after one page**, so a later `jobs-ingest` could expire every role the crawl
  never fetched (#68). Those responses nest the rows and the total together
  (`data.positions` / `data.count`) and page by a row-offset `start`, which
  neither `detect_next`'s top-level total nor its page/offset branches saw. A
  fallback now follows that envelope (and the same shape at the top level),
  steps `start` by the rows the board returned, and replaces an invented
  `offset=` when `start` is the board's only paging param; it still yields to
  a link/cursor/page next page. `positionUrl` / `position_url` map to the
  canonical listing URL, so NVIDIA rows no longer stage with `url: ""` (#69).
- `complete` is now checked against the board, not inferred from "no next
  page". A crawl that began mid-board (`page > 1`, `offset > 0`, or an envelope
  `start > 0`) or staged fewer **unique** listings than the board's
  `reportedTotal` is `truncated`, so the expiry gate stays closed.
  `pagination.json` gains `reportedTotal` / `rowsFetched` / `deduped` (the
  INVENTORY line prints `reportedTotal`), and `jobs-ingest` uses
  `max(listings, reportedTotal)` for `--expect-count` when the total beats the
  staged listing count — a wrongly `complete` crawl can no longer close roles
  it never saw.
- Review follow-up on #69: coverage counts unique staged listings, not raw
  rows, so a re-sorting or duplicate-serving board cannot prove completeness
  with repeats; `deduped` is diagnostic only (it also includes normalization
  drops and cap overflow) and is never subtracted from `reportedTotal`. The
  `start` fallback probes an invented `page=`/`offset=` first and only falls
  back to `start` on a duplicate/empty page; it keeps the largest total when
  later pages report a smaller one, prefers explicit totals over a bare
  `count`, accepts digit-string totals, and anchors a slashless `positionUrl`
  at the host root. Offset/page steps use the raw rows served; an exact
  `max_listings`/`max_pages` board is complete when the total is covered, and
  a `truncated` status refuses expiry unless `--force-expire` is passed.
- `paginate` now retries throttled/failing JSON endpoints instead of
  ending the crawl on the first 429/5xx (#70): 429, 500, 502, 503 and
  504 only, at most 3 attempts, `Retry-After` (delta-seconds or
  HTTP-date) capped at 60s per wait, otherwise exponential backoff with
  jitter. Other 4xx fail fast, and a persistent failure still records
  `blocked` / `complete: false`, so ingest cannot expire on it.
- Eightfold-style `postedTs` / `creationTs` epoch timestamps now map to
  `postedAt` (#75): seconds or milliseconds, a real `postedAt` wins, a
  junk `postedTs` falls back to `creationTs`, and booleans/out-of-window
  values are ignored instead of becoming 1970 dates.
- `fetch-descriptions` resolves Eightfold rows through the sibling
  `/api/pcsx/position_details` endpoint instead of the JS-rendered listing
  page, and parses `{data: {jobDescription}}` (stripping HTML before the
  ~10k-char truncation). Other boards keep the page fetch; the schema,
  retry/backoff, and failure behavior are unchanged (#76).
- `_set_query` preserves repeated query parameters when building the next
  page URL. It previously rebuilt the query through a dict, so
  `?location=US&location=CA` became `?location=CA` on page 2 and the
  filter changed mid-crawl. The target key is replaced once in place;
  other repeated/blank pairs and the fragment are kept (#77).
- `paginate` now finds a board total one level deeper than the rows
  (Eightfold's `data.positions` plus `data.meta.total`) instead of only
  beside the list or at the top level. The total feeds `reportedTotal` and
  the completeness gate, and the `start` fallback can follow it; an empty
  `positions` with a count is no longer called an empty board, and a count
  in an unrelated nested object (`data.facets.count`) is still ignored (#73).

### Added
- `skills/careers-discover/references/eightfold.md` documents the
  Eightfold PCSX family: 10 rows per page with `start` offsets,
  server-side `query`/`location` filters, `position_details`, the 429
  behavior, and filter-first coverage guidance for huge boards (#71).
  `skills/careers-discover/SKILL.md` now tells the agent to filter
  first and quote coverage instead of silently crawling 150 of 20k.
- `paginate` accepts a POST/JSON request descriptor (`--method POST
  --body`, repeatable `--header`, `--body-offset-key`) for boards like
  Workday CXS (#74). It steps the body's offset while the board's
  `total` says rows remain, maps `externalPath` / `locationsText` /
  `postedOn`, and sends POST through the same retry/backoff as GET.
  A POST board with no total ends as `truncated`, never `complete`.

## v1.9.0 — 2026-10-22

### Changed
- `setup` now explains the optional semantic-matching upgrade (`embed-setup`):
  what it adds, that it pip-installs fastembed (+onnxruntime) and downloads a
  ~60MB model, and that it prefers an already-running local Ollama. Keyword
  scoring still works without it (#53).
- The `~/.local/bin/network-jobs` launcher now **fails loudly when
  `suite-root` records a suite that no longer exists**, instead of silently
  running the registry build. A dangling cached/npm install and a healthy one
  printed the same `--version`, so testing a checkout could quietly test
  `@latest` instead. The message names the dead path and prints runnable
  recovery commands (`npx --yes @hirefrank/network-jobs@latest setup --agent
  auto`, or `rm` of the quoted `suite-root`). A missing or whitespace-only
  `suite-root` still falls back and says so. `reset --purge-cache` now clears a
  dangling `suite-root` and prints that explicit runner command for re-linking,
  so its recovery path no longer dead-ends at the launcher; `which` warns (exit
  0) about a dead recorded suite while the executing copy is fine.
- `search` and `rank` say when the **per-company cap** trimmed the shortlist.
  A one-company corpus printed `3 of 42` for every query — identical output for
  `product`, `manager`, and nonsense — which reads as "only 3 match" or "the
  query is ignored", when 39 rows were merely held behind the diversity cap
  (`--company-cap 0` reveals them). `ranked.json` now carries
  `companyCap: {limit, skipped}` when the cap actually bound, `search` prints a
  `(N more held back by the … cap …)` line, and `showing` keeps its documented
  `K of N` shape. Absent when the shortlist fills normally.
- `setup`'s `Next:` steps now point at the CLI it just installed. The launcher
  lands in `~/.local/bin`, which is normally not on `PATH` on a first run, so
  `command -v network-jobs` missed it and the steps fell back to `npx --yes
  @hirefrank/network-jobs@latest` — re-fetching the suite setup had just
  resolved. Off-PATH installs now print the launcher's absolute path (quote-safe);
  installs whose directory is on `PATH` keep the bare `network-jobs`.
- `fetch-descriptions` no longer loses bodies to a throttled pass, and says why
  a page failed (#64). `fetch-failed: HTTPError` became `http-<code>`, so a
  closed role (404) no longer looks like throttling (429) or a timeout;
  transient failures — `http-429`, `http-5xx`, and `TimeoutError`/connection
  resets (including the same causes when `urlopen` wraps them in `URLError`) —
  get up to two retries, while DNS/TLS failures, other 4xx, non-http URLs and
  oversized bodies get exactly one request. A 429/503 is obeyed per its
  `Retry-After` header (seconds or HTTP-date) instead of the exponential guess;
  a request longer than 30s is not retried at all rather than retried early.
  A host that answered 429 gets a 1s pause before the next job on it. The
  run-level budget only charges retries that failed, so a board whose retry
  recovers every row still fetches the whole board; 5 consecutive failing jobs
  trip a soft stop that the next success clears, and 50 failed retries is a
  hard stop. `retriesDisabled: true` appears only when retries are still off
  at the end. On the live Anthropic board the first pass fetched 185 of 222
  bodies and the immediate rerun fetched the missing 37/37.
- A CloudFront/SigSci bot-wall 403 is the one 4xx that is retried (#67): the
  wall clears in about a minute, so it waits 30s and then 60s (or its own
  capped `Retry-After`), while a bare 403 still gets exactly one request. After
  a recovered wall, a second wall on that host is treated as blocked instead of
  paying the cooldowns again; blocked hosts get one attempt per later job and
  the summary carries `botWalled: true`. On the live board a later pass showed
  183/222 with all 39 failures from CloudFront, and every one of those URLs
  answered 200 about 45s later while `boards-api.greenhouse.io` stayed up.

### Fixed
- `search` explains an empty shortlist instead of printing a bare `0 of 0`:
  an empty corpus points at discovery + ingest, and a query with no matches
  says what to try instead (#54, #56).
- Program-manager titles are classified by their domain: compliance, legal,
  regulatory and privacy roles are `legal`, security roles are `it-security`,
  and so on. Only product and technical programs stay `product`, so a
  compliance program manager no longer outranks actual PMs on a product
  search (#57, #60).
- The landing page's version pill is checked against `package.json` by the
  test suite and updated by the release checklist, so
  `hirefrank.com/network-jobs` cannot silently serve a stale version (#61,
  #62).
- `INVENTORY.md` no longer duplicates **`Listings`** and **`Pagination`** when
  a triage dir came from the `stage-company.sh` skeleton. The helpers patch the
  skeleton's header placeholders *and* write the Pipeline summary block, so the
  real stage → paginate → match flow showed each label twice. The block is now
  their canonical home: the existing-file path removes the header copies after
  writing it, including duplicates from hand-edited files, and old triage dirs
  heal on the next helper write. Only labels the block actually renders are
  removed, so a manual card extract with no `index/pagination.json` keeps its
  header `Pagination` line (`status=captured`). A hand-filled `Status` is
  preserved, but a skeleton placeholder is still patched from the listing count
  (`(pending)` with zero rows becomes `blocked`).
- Staging silently dropped distinct postings whose board reuses one
  `requisition_id` (Greenhouse): `requisition_id` outranked the per-posting
  `id` in both `ID_KEYS` and `fingerprint.ats_id()`, so the fingerprint dedupe
  in `paginate()` collapsed them while `pagination.json` still reported
  `complete`. On Anthropic's board that lost 51 of 679 postings — including
  genuinely different roles sharing one requisition. A plain `id` now wins;
  `requisition_id` stays as a last resort, and `ats_id()` keeps its shape
  heuristics so a junk `id` still falls through. Migration: **every** row
  carrying a `requisition_id` re-keys, not only the ones that collided. On a
  `complete` crawl the old row is closed in the same batch — bumping
  `expiredCount`, with no drift warning, because the reconcile keys on
  `externalId` and the URL is unchanged — while the new row comes back with
  `firstSeen` reset to today. Nothing shows twice in that case; duplicates
  persist only across `captured`/`truncated`/`blocked` crawls, where expiry is
  skipped by design. URLs never feed the fingerprint, so absolutizing them
  re-keys nothing.
- Amazon rows staged the **apply** endpoint as their `url`. `search.json`
  sends `url: null`, `url_next_step` (an apply link that 302s) and `job_path`
  (the canonical page); `URL_KEYS` lacked `job_path`, so every JD fetch failed
  and those rows ingested title-only. `job_path` is now preferred over apply
  aliases and resolved against the crawled URL, so staged links are absolute.
- `docs/RELEASE.md` verification steps used `npm view
  @hirefrank/network-jobs/latest version`, which npm parses as a **path** and
  fails with `ENOENT` rather than resolving a tarball. Corrected to
  `npm view @hirefrank/network-jobs version` (`@latest` if the tag is named),
  with a note that the registry trails a publish by a few minutes and that
  `npm view --prefer-online` avoids a cached packument.

## v1.8.0 — 2026-10-22

### Added
- `skills/careers-discover/references/agent-browser.md` documents the verified
  `agent-browser` commands for the crawl path: `network har` is nested (there is
  no top-level `har`), `network request <id>` prints the full request and
  response including the body, and the output path belongs to
  `har stop <path>`. Written because a stale build's `--version` help makes
  existing commands look missing.
- The fetch-log records `agentBrowserVersion` (optional, `method:
  agent-browser` only), so a run that decided an outcome can be traced back to
  the browser build that produced it.

### Fixed
- `reset` / `uninstall` now print the commands that actually remove the CLI
  (`npm uninstall -g @hirefrank/network-jobs`, `rm -f` on the launcher, plus a
  cache step where one applies) instead of implying a full wipe. `uninstall` was described as "Full wipe" but
  only removed skill links and `~/.network-jobs`; the npm package and the
  launcher survived, and because the launcher falls back to
  `npx @hirefrank/network-jobs@latest`, the next invocation silently re-fetched
  the suite. The hint is printed, never executed — a CLI cannot safely
  uninstall the package it is running from — and is tailored: it suggests
  `npm uninstall` only for an npm-global install, prints the launcher line only
  when the launcher exists, and omits the `--purge-cache` suggestion when that
  flag was already passed.
- `index/matches.json` no longer flips between the SCHEMA object and a bare
  array depending on the last writer (#32). `fetch-descriptions` now writes
  the object back — wrapper and summary keys preserved — instead of
  `json.dumps(jobs)`. A legacy bare array is read and upgraded in place on
  the next pass, so existing triage dirs keep working.
- `classify` run in place (`--input` with no `--out`) now keeps
  `index/matches.json` an object: the classified jobs replace `jobs` and the
  summary keys (`showing`, `ingestDefault`, `departments`, `nListings`,
  `nMatches`) ride through. A legacy bare array is upgraded to the object on
  that pass. With `--out` it still writes a bare job array, which is what
  `rebuild-corpus` consumes.
- `review-matches --veto` no longer drops fetched JD bodies when it
  re-scores: it carries prior descriptions onto `listings.json` the same way
  `match-prefs` does (#24). The join also matches rows that have no
  `company` field yet but whose scored counterpart does.

### Changed
- A crawl with zero incoming rows no longer expires a company's prior
  open roles by default, whatever the pagination status (omitted, jq
  `null`, or `empty`). Rebuild skips expiry, prints a warning, and
  requires `--force-expire` — a filtered or region-limited URL that
  returns `[]` would otherwise wipe the company. A company that already
  had zero open rows can still record empty. `blocked` / `captured`
  remain never-expire.
- `setup` installs and upgrades `agent-browser` pinned to
  `agentBrowser.minVersion` in `package.json`, instead of `@latest`.
  `doctor` recovery hints use the same pin. A missing minVersion prints a
  warning and installs untagged rather than failing silently. The
  version-floor check after install is unchanged. The old comment about a
  stale build advertising a command in `--version` help is gone.
- One shared reader for the artifact: `network_jobs.matches.load_matches()`
  (plus `normalize_matches` / `write_matches`) is now used by
  `fetch-descriptions`, `match-prefs`' description restore, `review-matches`,
  and the ingest path (`classify`). No caller needs a
  `payload["jobs"] if isinstance(payload, dict) else payload` conditional.
- `departments` in `matches.json` stays the listing-wide histogram computed
  by `match-prefs`. `fetch-descriptions` documents that filling `department`
  from a JD can make it stale until the next `match-prefs` run; it is not
  recomputed from the shortlist, which would silently change its meaning.

## v1.7.1 — 2026-10-22

### Changed
- Docs sweep after the hosted sunset: `docs/DECOMMISSION.md` and
  `docs/hosted-d1-export.md` are gone (every checklist item was complete —
  `hirefrank/skills` no longer carries a hosted-API skill copy). The two
  durable facts survive in `worker/SUNSET.md`: the 2026-10-04 D1 export was
  taken and cannot be regenerated, and there is no importer into
  `~/.network-jobs/`.
- `README.md` no longer surfaces the retired hosted product, matching the
  project's own rule about not advertising `jobs.hirefrank.com` in suite docs.
- `docs/RELEASE.md` now documents tag-triggered trusted publishing and records
  the remaining npm publishing-access follow-up.

## v1.7.0 — 2026-10-22

### Added
- `.github/workflows/publish.yml` publishes on a version tag through npm
  trusted publishing (OIDC): no `NPM_TOKEN`, no per-release passkey step-up,
  and automatic provenance. It re-runs the tests and refuses to publish when
  the tag doesn't match `package.json`. Manual publishing stays documented as
  the fallback in `docs/RELEASE.md`.

### Changed
- Every customer-facing skill now asks structured questions through the host's
  choice tool (`AskUserQuestion` / `question` / `ask_user_choice`), **one
  question per call** — never a bundled prompt or a multi-question tool call.
  Each question leads with the recommended option and a one-line reason, keeps
  the deciding data in the option labels, and keeps a free-text path; hosts with
  no such tool fall back to a numbered list (max 4, recommendation first).
- Wired into all bounded decisions: setup interview (overwrite fields, track,
  categories, deal-breakers, former-employer policy, onsite metros, next step),
  search (first-run gates, former employers, role → location → seniority,
  network packs), careers-discover (company batch, capture fallback, family
  vetoes, ingest set), jobs-ingest (dir, then set), intros (role, then
  forwarder), and import (fuzzy merges).
- `skills/network-jobs/examples.md` Example 3 no longer demonstrates the prose
  three-question clarification dump; it shows three sequential choice calls.

### Fixed
- `network-jobs-setup` had the "Trajectory / domain / shape" bullet duplicated
  with contradictory detail; merged into one, keeping the `targetRoles` ↔
  `track` reconciliation.

## v1.6.2 — 2026-10-04

### Fixed
- `classify` now accepts a `{"jobs": [...]}` envelope as well as a bare array.
  `match-pregs` writes the envelope and `fetch-descriptions` writes the bare
  array, so the documented order (`match-prefs` → `classify`) previously failed
  with `input must be a JSON array` and had to be un-wrapped by hand. Genuinely
  unrecognised shapes still exit 1.

### Changed
- `CHANGELOG.md` is now included in the published npm tarball, and the README
  links to it. Release notes previously shipped to the repo but not to anyone
  installing from npm — which is the only channel most users get.

## v1.6.1 — 2026-10-04

Data-integrity fixes (#24, #25).

### Fixed
- `match-prefs` no longer destroys fetched JD bodies. It rehydrates
  `description` / `descriptionFetchedAt` / `descriptionSource` from the
  existing `matches.json` onto `listings.json` (joined on the same
  fingerprint the classify path computes) before scoring, so a re-score
  keeps bodies and `mustHaves` can finally fire. Reported in #24, where the
  only ways to run the steps all left `mustHave` permanently unreachable.
- `mustHaves` now require token **locality**, not just co-presence. With
  real JD text in scope, a whole-document AND-set matched
  "strong engineering partnership" on Anthropic's legal boilerplate ("Not
  all strong candidates will meet every single qualification") plus
  scattered "engineering"/"partnership" elsewhere, promoting two roles on
  nothing. Tokens must co-occur within a window scaled to phrase length.
- Fingerprints no longer include the company segment (#25). `fingerprint()`
  keyed jobs `id:{company}:{atsId}`, with the company coming from the
  optional `--company` flag — so a re-score without the flag rewrote every
  key and the documented re-ingest silently doubled the corpus (30→60).
  Keys are now `id:{atsId}` (company lives on the job object, not in the
  key), and `rebuild` reconciles an incoming `externalId` that matches an
  existing entry under a different fingerprint, merging in place and
  reporting `driftWarnings` instead of appending a duplicate.

## v1.6.0 — 2026-09-29

Fit quality, forwarder honesty, and scoring honesty (issues #13–#23).

### Job descriptions
- New `fetch-descriptions` step fills JD bodies (truncated plain text) for
  matches only, plus ATS departments when rows lack one. Bodies ride
  classify → merge → shards untouched.
- `mustHaves` moved from dead substring to AND-set matching over
  title + department + company + description; unmatched on a described
  role records `mustHave-unmet` (visible, no penalty).

### Review, intros & reporting
- `review-matches` clusters by `head/domain` family stems with suggested
  vetoes; leak reports group vetoes and hard-fails.
- `intros --title/--url/--company/--prefer` targets reviewed roles and
  pins people the user actually knows; forwarder order documented as
  title-overlap only, with connection year shown as context.
- `search -v` fit brief (reasons, resume hits, warmth, gaps); default
  output unchanged.
- New `report` command: sanitized diagnostic bundle → GitHub issue.
- `build-pack` / `fetch-pack` wired into the CLI.

### Scoring honesty
- Level-ambiguous titles score −1 (was neutral); confirmed seniority wins
  score ties in both match and rank paths.
- Affinity bonus requires high-confidence classification
  (`category-unconfirmed` otherwise).
- `targetRoles` slugs score +3 as an aspirational bonus, never a veto.

### Locations
- Unknown-location sentinels, word-boundary short-token matching,
  idempotent `parse_locations`, comma-list city splitting (with `City, ST`
  preservation), `anywhere`-as-remote.
- Greenhouse object-`location` normalization; prose placeholder ATS ids.

## v1.5.0 — 2026-09-29

Location correctness, review precision, ingest safety, and fit evidence
(issues #11–#16).

### Locations
- Unknown-location sentinels (`N/A`, …) never match onsite prefs; short
  tokens match only on word boundaries (`DC` still hits `Washington, DC`;
  `N` can no longer ride inside `New York`).
- `parse_locations` is idempotent and splits comma-separated multi-city
  strings (`San Francisco, Seattle, …` → one entry per city); `City, ST`
  pairs stay whole. `anywhere`/`global`/`worldwide` parse as remote.

### Review & intros
- `review-matches` clusters by `head/domain` family stems
  (`product/payments`), each with a suggested `--veto` phrase.
- `intros --title/--url/--company` targets reviewed roles; empty selection
  is an error, not a silent default.
- `search -v` appends a fit brief per role (reasons, résumé hits, warmth,
  gaps); default output unchanged.

### Safety
- Expiry refuses filtered batches: `--expect-count` (board size) makes
  matches-only ingest skip closing live roles, loudly; `--force-expire`
  overrides. The skill always passes the board size now.

## v1.4.0 — 2026-09-29

New commands plus extraction and precision fixes (issues #6–#10).

### Extraction
- Greenhouse `boards-api` object-`location` payloads now normalize instead of
  passing through verbatim (type-aware staged guard in `normalize_listing`).
- Prose placeholder ATS ids (`See Opening ID`, … — 20 entries) filter to
  absent, so boards stop collapsing onto one shared fingerprint.

### Matching & review
- `match-prefs` prints composition (`matchDepartments`, `matchCategories`,
  `reasonCounts`) and warns on unconfirmed empty `dealBreakers` (new
  `dealBreakersConfirmed` flag, set by the interview).
- New `review-matches`: family clustering, leak reports, `--veto` loop that
  rewrites prefs and rematches in one step.
- Interview confirms proposed categories (with affinity warning) and always
  asks deal-breakers explicitly.
- Hard-fail reasons now recorded (`category/location/track-mismatch`).

### Reporting & packs
- New `report`: sanitized diagnostic bundle (PII redacted) → GitHub issue,
  plus an agent nudge to offer filing.
- `build-pack` / `fetch-pack` wired into the `network-jobs` CLI.

## v1.3.2 — 2026-09-29

- **Muse support in setup** — `network-jobs setup --agent auto` detects the
  `muse` CLI and links all six skills into the shared `~/.agents/skills`
  (verified: Muse reads that dir). `network-jobs agents` lists it, and
  detection also works off the binary when no home dir exists.
- **LICENSE capitalization fix** (hirefrank, lowercase one word).
- Landing page shipped at `hirefrank.com/network-jobs` (site only, not in
  the npm tarball).

## v1.3.1 — 2026-09-29

First npm release (`@hirefrank/network-jobs`). Install with
`npm i -g @hirefrank/network-jobs@latest && network-jobs setup --agent auto`
(`npx --yes @hirefrank/network-jobs@latest setup …` for zero-install).

- **`network-jobs update` handles npm-global installs** — detects
  `*/lib/node_modules/@hirefrank/network-jobs` and refreshes via
  `npm i -g @hirefrank/network-jobs@latest` instead of purging the npx cache.
- **Launcher + setup hints prefer the registry** over the GitHub tarball.
- **Packaging fixes** — `publishConfig.access: public`, normalized `bin` path,
  merged duplicate `scripts` keys, `prepack` removes `__pycache__/` so test
  runs never pollute the tarball.

## v1.3.0 — 2026-09-28

Everything since v1.2.0: five rounds of matching/ranking review, a full
first-time-user dogfood pass, and the fixes it found.

### Matching & ranking
- **Round 2** — seniority fix, recency/stale ranking, keyword cleanup, search/companies refresh
- **Round 3** — ranking quality, ingest hygiene, demo CLI
- **Round 4** — seniority is a soft signal, not a veto
- **Round 5** — opt-in semantic (embedding) matching: fastembed by default with
  Ollama fallback (never raises, never downloads on detection), incremental
  `corpus/embeddings.json` cache at rebuild, `resume-semantic` (+0–4, replaces
  the keyword bonus) and `query-semantic` (+0–2, additive) signals,
  `network-jobs embed-setup` one-time setup and embedding coverage in
  `network-jobs doctor`

### CLI
- **New `network-jobs intros`** — pick 1–2 roles from the last search plus 1–2
  forwarders each from your connections (writes `search/intros.json` for the
  intro-email-generator skill). Previously reachable only via the skill helper,
  which `search` advertised as a follow-on step with no command to run.
- **`embed-setup` reports the real failure** when a provider is detected but the
  warmup embed fails (e.g. model download blocked) — previously it misleadingly
  printed "fastembed is not installed" and re-ran pip install. Exit 1 with the
  underlying reason; the Ollama → fastembed fallback is unchanged.
- **`doctor` shows the embedding section** — provider + cache coverage were added
  to the Python doctor in round 5 but the bash CLI's native doctor never
  surfaced them.
- **CLI resolves npm `.bin` symlinks** when locating the suite — the README
  `npx` one-liner (and `network-jobs update`) failed without this.

### First-run reliability (from a clean-install dogfood as a new user)
- `setup --no-browser` is persisted so `doctor` stops nagging on re-runs
- `parse-linkedin.sh` prints one summary line; full JSON stays in the log
- All `DATA=` assignments across the skills carry "always resolve from the env"
- `embed-setup` pip failure names the real PEP 668 options
- README Fixtures block uses the installed CLI + `network-jobs which`
- `tests/` now ships in the npm package so the fixtures test command works

### Docs
- README "For agents" section near the top (install → setup skill → skill table)
- Agent Skills compatibility list (Claude Code, Cursor, Codex, OpenCode, Pi,
  Grok Bot, Muse, and others)

### CI
- `tests/run.sh` runs on Python 3.11, 3.12, and 3.13
