# Network Jobs

Local-first agent skill suite: import your resume and preferences, import LinkedIn connections, discover company career pages, build a personal job corpus, search openings, and draft warm intros — on your machine.

Works with any agent that loads [Agent Skills](https://agentskills.io) (Claude Code, Cursor, Codex, OpenCode, Muse, Pi, Gemini CLI, Grok Bot, and others).

## For agents

If you're an agent working from this repo rather than a human reading it:

1. **Install the skills into yourself** — `npx --yes @hirefrank/network-jobs@latest setup --agent auto` (or `npx skills add hirefrank/network-jobs -g -a …`, then `network-jobs setup` for data + launcher).
2. **Start with `network-jobs-setup`** — it turns the user's resume into `profile.json` + `preferences.json` via an interview. Everything downstream reads those two files.
3. **Follow the skills table below** — each `SKILL.md` is self-contained and states its inputs/outputs. The day-to-day pair is `careers-discover` + `jobs-ingest` (build the corpus) and `network-jobs` (search it).
4. **For routing**, run `network-jobs routing` — it prints a snippet to drop into a project instruction file so future sessions pick the right skill.

Always read [SCHEMA.md](SCHEMA.md) for data paths and JSON shapes. Daily use lives in the agent; the CLI covers setup, import, and quick `search` / `companies` / `refresh`. Release notes are in [CHANGELOG.md](CHANGELOG.md).

## Install

```bash
npm i -g @hirefrank/network-jobs@latest
network-jobs setup --agent auto
```

This creates `~/.network-jobs/`, installs `agent-browser` when missing (and upgrades it when it is below the floor declared in `package.json`), puts `network-jobs` on PATH (`~/.local/bin`), and symlinks all six skills into each detected agent’s skills directory.

Search works out of the box with keyword scoring. **Semantic matching** is optional: `network-jobs embed-setup` adds resume↔job similarity on top of keyword scoring for better fit ranking. It pip-installs fastembed (and dependencies such as onnxruntime) into your Python, downloads a one-time ~60MB model (needs network), and uses a local Ollama instead when one is already running. Setup does not install it; skip it if you're offline or want a lean install.

Zero-install try (no global install):

```bash
npx --yes @hirefrank/network-jobs@latest setup --agent auto
```

Bleeding edge from git main:

```bash
npx --yes 'github:hirefrank/network-jobs#main' setup --agent auto
```

```bash
network-jobs setup --agent claude-code,cursor,codex
network-jobs setup --agent all
network-jobs setup --force          # replace skill dirs from an older install
network-jobs setup --no-browser     # skip agent-browser install
network-jobs setup -v               # print every path
network-jobs update                 # fetch latest + relink
network-jobs doctor
```

Requires: `curl`, `jq`, `unzip`, `python3`, and a working `npm` (for [`agent-browser`](https://github.com/vercel-labs/agent-browser)). Optional: `pdftotext` for PDF resume extraction.

`agent-browser` is only needed for JavaScript-rendered career pages — skip it with
`setup --no-browser` and static boards still work. `network-jobs doctor` prints the
installed version and warns when it is below `agentBrowser.minVersion` in
`package.json`; `network-jobs update` upgrades only below that floor. See
[`skills/careers-discover/references/agent-browser.md`](skills/careers-discover/references/agent-browser.md)
for the command reference (subcommands are grouped: `network har`, not `har`).

Optional alternate skill placement: `npx skills add hirefrank/network-jobs -g -a …` then still run `network-jobs setup` for data + launcher.

### Then use your agent

- “Set up network jobs from my resume” → profile + **dynamic** preferences interview  
- “Import my LinkedIn zip at ~/Downloads/….zip”  
- “Find open roles at my top companies” → review triage → “ingest that batch”  
- “Senior PM jobs in NYC” → “Draft an intro to Jane”

Daily use is **in the agent**. The CLI also covers install, update, doctor, resume/LinkedIn import, corpus clear, and quick `search` / `companies` / `refresh`.

## Tips

- **Pipeline stop:** `careers-discover` only stages under `triage/`. Review, then explicitly ask to ingest.
- **Resume + prefs:** `network-jobs profile import ~/resume.pdf`, then in your agent run setup from the resume. Interview covers remote/hybrid/onsite (**onsite scoped to specific cities**, not every office), location, IC vs manager, **former employers include/exclude**, stage/domain, and other gaps the resume implies.
- **Model choice (agnostic):** prefer a **stronger** model for career-page discovery; mid-tier is usually enough for local search and intro drafts.
- **Refresh:** `network-jobs update`
- **Health:** `network-jobs doctor`
- **Semantic matching (optional):** `network-jobs embed-setup` pip-installs fastembed (one-time ~60MB model, uses a local Ollama if already running) so `search`/`rank` also score resume↔job similarity. Keyword scoring is unchanged when it's not installed.
- **Data:** everything under `~/.network-jobs/` — no hosted API.

## Pipeline

```text
Resume
    → profile import + setup skill   (profile.json + preferences.json)
LinkedIn ZIP
    → network-jobs-import            (connections + companies graph)
    → careers-discover               (stage openings under triage/)
    → you confirm
    → jobs-ingest                    (promote to corpus/)
    → network-jobs                   (search local corpus using prefs defaults)
    → intro-email-generator          (forwardable warm intro; uses local resume)
```

Always read [SCHEMA.md](SCHEMA.md) for paths and JSON shapes.

## Quick start

1. **Install** — `npm i -g @hirefrank/network-jobs@latest && network-jobs setup --agent auto`

2. **Resume**

```bash
network-jobs profile import ~/Downloads/Resume.pdf
```

Then in your agent: “set up network jobs from my resume” (profile + prefs, including whether to consider roles at former employers).

3. **LinkedIn connections**  
   Settings → Data privacy → Get a copy of your data → **Connections** → download ZIP.

```bash
network-jobs import ~/Downloads/Connections.zip
```

4. **Discover + ingest** in the agent  
   “Find open roles at my top 5 companies” → review triage → “ingest the Stripe batch”

5. **Search + intro** in the agent  
   “Find senior PM roles in NYC” → “Draft an intro to …”

## Skills

| Skill | Role |
|-------|------|
| [`network-jobs-setup`](skills/network-jobs-setup/SKILL.md) | Resume → profile, preferences interview, LinkedIn export help |
| [`network-jobs-import`](skills/network-jobs-import/SKILL.md) | ZIP → local company graph |
| [`careers-discover`](skills/careers-discover/SKILL.md) | Model-driven career page discovery + staging |
| [`jobs-ingest`](skills/jobs-ingest/SKILL.md) | Triage → normalized corpus shards (after you confirm) |
| [`network-jobs`](skills/network-jobs/SKILL.md) | Search local corpus (prefs as defaults) |
| [`intro-email-generator`](skills/intro-email-generator/SKILL.md) | Forwardable warm intro emails |

## CLI

```bash
network-jobs setup | update | doctor | which | agents | routing
network-jobs report [--title T] [--dry-run] [--yes]
network-jobs import <zip-or-csv>
network-jobs profile import <resume-file>
network-jobs profile show
network-jobs profile clear [--resume] [--yes]
network-jobs corpus clear [--triage] [--yes]
network-jobs reset [--data] [--purge-cache] [--yes]
network-jobs uninstall [--yes]    # alias for reset --data

# Day-to-day (also available in your agent):
network-jobs search [--query "..."] [-k N] [--company-cap N] [--include-stale] [-v]
network-jobs intros [--k-roles N] [--k-forwarders N] [--title T] [--url U] [--company C]
network-jobs companies [--sort connections|name|crawl]
network-jobs refresh [--company NAME] [--limit N]
network-jobs review-matches --triage-dir DIR [--veto PHRASE] [--json]
network-jobs build-pack <zip-or-csv> [--label NAME]
network-jobs fetch-pack <url> [--as NAME]
network-jobs demo [-k N] [--company-cap N]   # synthetic jobs, no network/disk
network-jobs embed-setup                     # one-time fastembed for semantic search
```

Skills self-describe for routing. `network-jobs routing` prints an optional snippet for a project instruction file.

| Wipe | Keeps |
|------|--------|
| `corpus clear` | profile, prefs, resume, LinkedIn graph |
| `profile clear` | corpus, LinkedIn graph (`--resume` also drops resume files) |
| `uninstall` / `reset --data` | nothing under `~/.network-jobs/` — the npm package and the launcher stay installed; both commands print how to remove them |

## Design notes

- **Provenance-style scraping:** orchestration + optional recipes — no ATS adapter matrix.
- **Shared data:** one `~/.network-jobs/` for every agent on the machine.
- **Symlink installs:** one suite copy; `update` re-points agent skill links.
- **Prefs-aware discovery/search:** after extract, `match-prefs.py` writes `index/matches.json` (confirm matches by default; ingest all / department slice still available). Search runs `rank-jobs.py` and reports `Showing K of N` instead of dumping shards.
- **Crawl budget:** listing-set hash + `lastCrawl` on the company; skip unchanged boards; paginate with page/browser caps; ingest is a fingerprint delta. Expiry requires `pagination.complete` and a status that is not `truncated`/`blocked`/`captured` (`truncated` can be forced).

## Fixtures

```bash
export NETWORK_JOBS_HOME=/tmp/nj-test
npx --yes @hirefrank/network-jobs@latest setup --agent auto --no-browser
export PATH="$HOME/.local/bin:$PATH"   # if setup just installed the launcher there
SUITE="$(network-jobs which | sed -n 's/^suite=//p')"   # fixtures ship in the suite
network-jobs import "$SUITE/fixtures/Connections.csv"
python3 "$SUITE/tests/test_pipeline.py"   # prefs, ranker, pagination, fingerprint, classifier, crawl, intros
```

## License

MIT
