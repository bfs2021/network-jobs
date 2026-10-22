---
name: network-jobs
description: Find jobs where you're already connected. Search openings in your local Network Jobs corpus at companies from your LinkedIn graph. Use when searching for jobs, checking company connections, or exploring career opportunities.
license: MIT
allowed-tools: Bash(*)
metadata:
  version: "3.2.0"
---

# Network Jobs

Search job openings at companies where you have connections — from your **local** corpus under `~/.network-jobs/`.

Read [SCHEMA.md](../../SCHEMA.md) first. For category mapping details see [reference.md](reference.md).

## Configuration

- **DATA**: `NETWORK_JOBS_HOME` or `~/.network-jobs`
- This suite has **no hosted API**. Every read is a local file; never fetch a remote job export.

```bash
DATA="${NETWORK_JOBS_HOME:-$HOME/.network-jobs}"  # always resolve from the env; never export a literal DATA
cat "$DATA/profile.json"
cat "$DATA/corpus/manifest.json"
```

## Data files

| File | Purpose |
|------|---------|
| `profile.json` | Search header + intro footer identity |
| `preferences.json` | Default work mode, locations, categories, seniority |
| `resume/text.md` | Resume text for intros / profile fill |
| `corpus/manifest.json` | Category index + granular file map |
| `corpus/{category}.json` | Full category list |
| `corpus/{category}-{loc}-{seniority}.json` | Preferred granular shards |
| `search/ranked.json` | Top-K from `helpers/rank-jobs.py` — read this, not whole shards |
| `companies/companies.json` | Connection graph (for “who do I know?”) |
| `packs/*.json` | Fetched network packs — someone else's company-aggregated map (see Network packs) |

**Location buckets:** `nyc`, `sf`, `la`, `seattle`, `austin`, `boston`, `chicago`, `denver`, `dc`, `remote`, `other`  
**Seniority buckets:** `senior`, `mid`  
**Categories:** see reference.md

## Asking the user

Whenever the next step is picking from a known set, use the host's choice tool —
`AskUserQuestion` (Claude Code), `question` (opencode), `ask_user_choice`, or the
harness equivalent — instead of a prose list.

- **One question per call.** Ask it, wait for the answer, then ask the next.
  Never bundle two questions into one prompt or one tool call, even when the
  tool accepts several.
- **Recommend, and say why.** Lead with the option you would pick and a one-line
  reason ("Recommended: exclude — keeps the search focused, and you can opt
  back in per run").
- Put the deciding data in the option label (count, fit, family), never in
  surrounding prose the user has to correlate.
- Always keep a free-text / "none of these" path.
- No such tool in this host? Use a short numbered list, recommendation first,
  never more than 4 options.

## Procedures

**First step for all patterns:** load profile + preferences + manifest.

```bash
DATA="${NETWORK_JOBS_HOME:-$HOME/.network-jobs}"  # always resolve from the env; never export a literal DATA
jq -r '"\(.name)|\(.title)|\(.company)|\(.email)"' "$DATA/profile.json"
jq . "$DATA/preferences.json" 2>/dev/null || echo "(no preferences yet)"
jq . "$DATA/corpus/manifest.json"
```

### Empty / first-run gate (STOP here)

If `companies/companies.json` is missing → ask **one question**: import a LinkedIn ZIP now (recommended — without a graph there is no ranking and no intro matching) or continue without one. Then run **network-jobs-import**. Do not invent a graph.

If `profile.json` `name` or `email` is empty → ask **one question**: set up the profile now or after this search (recommended: now for search/intros, later for discovery). Then run **network-jobs-setup** (resume import + profile).

If `preferences.json` is missing or `interviewComplete` is not true → ask **one question** before broad searches: run the 3–6 question setup interview now (recommended) or override location/mode/roles for this query only.

If `manifest.totalJobs` is 0 or `lastUpdated` is null:

1. Tell the user the corpus is empty.
2. Offer **careers-discover** only (stage openings under `triage/`).
3. **Do not** chain into **jobs-ingest** yourself. After discovery finishes, stop and show staged triage; wait for explicit “ingest this batch” (or equivalent) before loading **jobs-ingest**.
4. Only after ingest completes, resume search with this skill.

Never say you will “discover then ingest then search” in one uninterrupted pass.

### Apply preferences as defaults

When the user does **not** specify location / mode / seniority / category / track:

1. Read `preferences.json`.
2. Prefer shards matching `locationBuckets` + `seniority` + `categories`.
3. Honor `workModes` **with location scope**:
   - `remote` only → prefer `locationBucket=remote`.
   - Includes `hybrid` / `onsite` → include city buckets that match `onsiteLocations` / `locations` / `locationBuckets` — **not** every non-remote job worldwide.
   - If `hybrid`/`onsite` is set but `onsiteLocations` is empty, ask **one question** — which metros count, offering the `locations` / city `locationBuckets` cities as a recommended multi-select — then fall back to `locations` / city `locationBuckets` only; never expand to global onsite.
4. Honor `track` when filtering titles: `manager` → prefer Manager/Director/Head/EM/VP people-lead titles; `ic` → de-prioritize pure people-manager titles unless query asks; `either` → no track filter. Prefer the persisted `track` field on the job when present.
5. Honor `senioritySignals`: intern roles only when asked; `staff+` when the user wants Staff/Principal.
6. Soft-filter with `locations` / parsed `locations[].city`, `industries`, `companyStages`, `mustHaves`, `dealBreakers`, `notes`, `salaryMin` in judgment — do not invent salary on jobs that lack it.
   Hybrid NYC-or-Remote jobs live in both shards; `onsiteLocations` still gates in-person cities.
7. **Former employers:** read `formerEmployers` + `formerEmployerPolicy`.
   - `exclude` — omit those companies from default result sets (still show if the user named the company).
   - `include` — treat like any other company.
   - `ask` — if matches appear, surface them separately and ask **one multi-select** over the flagged companies (recommended: exclude all — they are former employers for a reason).
   Match company names case-insensitively / lightly normalized (ignore Inc, LLC, etc.).
8. If the user query conflicts with prefs, **query wins**.

Mention once when defaults applied: e.g. `Using your prefs: remote + NYC, product, senior IC (excluding former employers)`.

### Pattern A: "Do I have connections at [Company]?"

1. Check `companies/companies.json` for the company (connection count + people)
2. Search corpus files for matching `company` (case-insensitive)
3. Report connections **and** open roles (or “connected but no staged openings yet”)

### Pattern B: "Find me [Role] jobs in [Location]" (PREFERRED)

1. Map role → category, location → bucket, seniority if given (prefs are defaults; query wins)
2. Run the local ranker — do **not** load whole shards into context:

```bash
DATA="${NETWORK_JOBS_HOME:-$HOME/.network-jobs}"  # always resolve from the env; never export a literal DATA
SUITE="$(cat "$DATA/suite-root" 2>/dev/null || true)"
SUITE="${NETWORK_JOBS_SUITE:-${SUITE:-}}"
python3 "$SUITE/skills/network-jobs/helpers/rank-jobs.py" --k 25 --query "$USER_QUERY"
```

3. Read `$DATA/search/ranked.json` only. Use `showing` (`K of N`) as the summary line.
   If `staleHidden` > 0, add one line: `(+N stale postings hidden — say "include stale" to see them)`.
   If `companyCap` is present, add one line before it: `(N more held back by the
   <limit>-per-company cap — use --company-cap 0 to see them)`. A short `showing`
   with `companyCap` set means the cap trimmed the list, **not** that only those
   roles match — say so rather than reporting "only 3 of 42 matched".
4. Output in **strict format** below. Never paste a full category/shard dump.

Shortcut: `network-jobs search --query "..." -k 25` runs the ranker, writes
`search/ranked.json`, and prints a human-readable list in one step. Add
`--include-stale` to surface stale postings; `--company-cap N` limits how many
jobs one company can take in the shortlist (default 3, 0 disables).
`network-jobs demo` ranks a fixed synthetic job set with no network or disk
writes — a quick end-to-end check of the scoring pipeline (exact category,
category affinity, company cap). `network-jobs refresh` re-crawls
known boards (oldest first) and stages new matches under `triage/`.

### Pattern B2: Broad queries

Ask role type, then location, then seniority — **three separate single-question calls**, each recommending the value from `preferences.json` — before loading every shard.

### Pattern C: "What's new?"

Sort preferred category (or ask) by `firstSeen` desc; show latest ~10. If there is no preferred category, ask **one question** over the top categories, recommending the last-searched one.

### Pattern D: Remote / location-only

Same ranker with `--query` including remote / city. Still prefer granular shards inside the helper.

### Pattern E: Salary filter

Ranker already downranks missing/low salary vs `preferences.salaryMin`. For an explicit floor, pass it in the query (`over 200k`) and skip jobs with no salary when presenting.

### Network packs: someone else's map, your search

A **network pack** is a company-aggregated map of someone else's professional
network: company names, connection counts, and title keywords — no names,
emails, URLs, or dates. It answers one question: *"where does this person have
connections?"* It is a map, not a menu: it shows where the owner has
connections; intros are at the owner's discretion. Never promise an intro on
the pack's behalf.

Fetch one with (the suite's one sanctioned remote fetch — everything else stays
local):

```bash
DATA="${NETWORK_JOBS_HOME:-$HOME/.network-jobs}"  # always resolve from the env; never export a literal DATA
network-jobs fetch-pack https://hirefrank.com/network-pack.json
# pack: Frank Harris — 3156 connections -> 2266 companies -> packs/frank-harris.json
```

Packs live under `$DATA/packs/*.json` and are **separate from the user's own
network**: the user's `connections.json` drives ranking and intro matching
(forwarders come from there — real people they can contact directly). A pack is
a read-only overlay with no names, so it can never inject forwarders.

Two uses:

1. **Discovery** — "Where is Frank's network densest?" Read the pack, report
   top companies by `connectionCount` (with `topTitles` where present).
2. **After a search** — cross-reference the ranked companies against each
   fetched pack. When there's overlap, flag it with counts:
   "3 of your top 10 are at companies where Frank Harris has 10+ connections —
   Stripe (12), Figma (9). Want me to draft the ask?" Ask that as **one
   question** (recommended: yes when three or more overlap). The ask itself
   goes through the **intro-email-generator** skill with the pack owner as the
   forwarder.

## Output Format

**STRICT FORMAT REQUIRED for default `search` output** — Do NOT summarize or paraphrase. Do NOT write prose/narrative.

1. Header: `Searching via [Name] ([Title])...` or with `@ [Company]` if set
2. Data freshness: `Data as of [Mon D], [H:MM AM/PM] ([relative] ago)` from `manifest.lastUpdated`
3. Summary line: `Showing K of N` from `search/ranked.json` (then the usual `X [role] roles in [location]:` if useful)
4. Group by company (COMPANY NAME in caps, then `- N roles`)
5. Each job: `• [Title] – [Salary if available], [N]d [↗](url)`
6. Days from `postedAt` else `firstSeen` (`3d`, `14d`, …)
7. Footer: `Want help drafting an intro email?` — if profile has email, include it; also offer connection names from `companies.json` when known

The strict contract covers default output only. When the user asks *why* a role
fits (or you need fit evidence before drafting intros), re-run with
`network-jobs search -v`: it appends a fit brief per role (recorded
`matchReasons`, resume keyword hits, mustHaves hits, connection warmth with
best forwarder, gap flags). Narrate from that evidence — do not invent fit
the brief doesn't support.

**Example:**

```
Searching via Frank Harris (Executive Coach)...
Data as of Jul 8, 3:00 PM (2h ago)

Showing 8 of 42
8 PM roles in NYC:

JUSTWORKS - 5 roles
• Group PM, Growth – 26d [↗](https://example.com/job/1)
• Senior PM, Foundations – $150-180k, 39d [↗](https://example.com/job/2)

Want help drafting an intro email? (profile: frank@example.com)
Connections at Justworks: …
```

**DO NOT:**

- Narrative summaries (“I found 93 roles including…”)
- Omit `[↗](url)` links
- Curl remote job hosts for corpus data
- Dump whole `corpus/*.json` shards into context (use the ranker)

## Helpers

- [`helpers/rank-jobs.py`](helpers/rank-jobs.py) — corpus shards + prefs + resume keywords → `$DATA/search/ranked.json` (`Showing K of N`)

## Related Skills

- **network-jobs-setup** — profile, resume, preferences interview
- **careers-discover** / **jobs-ingest** — refresh local openings
- **intro-email-generator** — draft forwardable warm intro. After ranked jobs, run `rank-intros.py` (1–2 roles, 1–2 forwarders; fetch JD only for those URLs).
- **network-jobs-import** — refresh LinkedIn graph
