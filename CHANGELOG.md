# Changelog

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
