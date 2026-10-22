---
name: network-jobs-setup
description: First-run setup for the local Network Jobs suite. Use when installing network jobs, importing a resume into profile, running the job-search preferences interview (location, remote/hybrid/onsite), filling profile.json, verifying ~/.network-jobs, or helping export LinkedIn connections.
license: MIT
metadata:
  version: "1.2.0"
---

# Network Jobs Setup

Onboard a user onto the local Network Jobs suite. No cloud account.

Always read [SCHEMA.md](../../SCHEMA.md) first.

## Data home

`NETWORK_JOBS_HOME` or `~/.network-jobs/`.

## Asking the user

Whenever the next step is picking from a known set, use the host's choice tool —
`AskUserQuestion` (Claude Code), `question` (opencode), `ask_user_choice`, or the
harness equivalent — instead of a prose list.

- **One question per call.** Ask it, wait for the answer, then ask the next.
  Never bundle two questions into one prompt or one tool call, even when the
  tool accepts several.
- **Recommend, and say why.** Lead with the option you would pick and a one-line
  reason ("Recommended: senior IC — the resume reads as an IC trajectory and
  manager seats would widen the search against your own signal").
- Put the deciding data in the option label (count, fit, family), never in
  surrounding prose the user has to correlate.
- Always keep a free-text / "none of these" path.
- No such tool in this host? Use a short numbered list, recommendation first,
  never more than 4 options.

## Workflow

1. **Verify install**
   - Confirm suite skills are discoverable (`network-jobs-import`, `careers-discover`, `jobs-ingest`, `network-jobs`, `intro-email-generator`).
   - If missing, tell the user to run:
     ```bash
     npx skills add hirefrank/network-jobs -g -a claude-code -a cursor -a codex
      npx --yes @hirefrank/network-jobs@latest setup --agent auto
     ```
     or `network-jobs setup --agent auto` / `network-jobs doctor`.
   - Confirm dirs exist: `connections/`, `companies/`, `triage/`, `corpus/`, `config/`, `logs/`, `resume/`.

2. **Resume → profile** (preferred before prefs interview)
   - If the user has a resume path: run  
     `network-jobs profile import <path>`  
     or [`helpers/import-resume.sh`](helpers/import-resume.sh).
   - Read `$DATA/resume/text.md` when present (else `resume/source.*`).
   - Propose updates to `profile.json`: `name`, `email`, `title`, `company`, `url`.
   - **Do not invent an email.** Only use an address found in the resume or confirmed by the user.
   - If `profile.json` already has values, show the diff and ask **one question** — a multi-select over the differing fields to overwrite (all preselected) — then write only the fields kept.
   - Write `~/.network-jobs/profile.json` after confirmation.

   If there is no resume yet, ask for name/email/title as free text, **one question at a time** (these are facts, not choices — free text, not the choice tool).

3. **Preferences interview** (after background is known — **dynamic**)
   Run a short interview **grounded in the resume/profile**. This is not a fixed form.

   **How to run it**
   1. Skim resume + current `profile.json`. Note likely track (IC vs manager), domains, seniority signals, recent employers, and gaps.
   2. Propose a **draft prefs summary** from what you already know (“Sounds like senior IC product in fintech/devtools, NYC-leaning…”).
   3. Ask **only the open questions** — skip anything the resume already settles. Put each one to the user on its own: one question, wait for the answer, then the next. Give your recommended answer and a one-line reason with every one, and let the user reply in their own words instead of picking.
   4. Branch: if they were a manager, ask **one question** whether they want to stay on a people-manager track, return to IC, or either (recommend whichever their last two roles point at); if lifelong IC, ask **one question** whether they’re open to managing. Same idea for domain switches, founding vs bigco, etc.
   5. Write `$DATA/preferences.json` (SCHEMA). Set `interviewComplete: true` and `updatedAt`. Extra keys OK when useful.

   **Core dimensions to cover if still unknown** (ask conversationally — one question per call, each with a recommendation — not a checklist dump):

   | Dimension | Examples |
   |-----------|----------|
   | Work mode | remote / hybrid / onsite — and if hybrid/onsite, **where** (never “any office”) |
   | Location | cities → map to a corpus location bucket (`nyc`, `sf`, `la`, `seattle`, `austin`, `boston`, `chicago`, `denver`, `dc`, `remote`, `other`) when clear; fill `onsiteLocations` for in-person willingness |
   | Role category | from the 15 corpus categories, biased by resume — then **confirm** (see below) |
   | Seniority | senior / mid |
   | Track | `ic` / `manager` / `either` |
   | Company shape | stage, size, industry (from their history + desires) |
   | Former employers | include / exclude / ask — see below |
   | Comp | optional `salaryMin`, or skip |
   | Must-haves / deal-breakers | e.g. no pure people-mgmt, must have eng partnership — always ask explicitly, then set `dealBreakersConfirmed: true` |

   **Dynamic follow-ups** (pick what fits *this* person):
   - IC ↔ manager trajectory
   - Stay in current domain vs pivot (use resume industries)
   - Startup vs scale-up vs public
   - Individual contributor craft depth vs broader GM/ops
    - **Confirm categories — affinity widens silently.** Propose the resume-derived
      set, then read it back for confirmation. Warn that each category also
      accepts its affinity neighbors (e.g. `product` also accepts `engineering`,
      `data`, `design`, `operations` — see `CATEGORY_AFFINITY`), so extra
      categories widen the net in directions the user never named. Ask what to
      drop with **one multi-select** over the proposed set (all preselected,
      likely drops marked "Recommended to drop"), not just what to add.
    - **Always ask deal-breakers explicitly.** Propose candidates from the
      resume's anti-signals (domains/roles they have never touched, levels they
      aged out of), then ask directly — including "none". Ask as **one
      multi-select** over the proposed candidates plus a "none — nothing to
      exclude yet" option (recommended until something has actually cost them).
      An empty `dealBreakers` with no confirmation reads as "never asked", and
      `match-prefs` will warn on it. Set `dealBreakersConfirmed: true` once
      asked, even when the answer is none.
    - **Former employers** — LinkedIn graphs are dense with connections at places they used to work. From the resume, list past employers into `formerEmployers`, then ask **one question**: **include**, **exclude**, or **ask each time** (`formerEmployerPolicy`). Recommended: exclude unless they say otherwise.
   - **Onsite scope** — if they want remote *and* are open to onsite/hybrid, ask **one question** — which cities/metros count, offering the `locations` / `locationBuckets` cities as the recommended multi-select. Write the answer to `onsiteLocations` (and matching buckets). Do not interpret “I’ll consider onsite” as every onsite role globally.
    - **Trajectory / domain / shape** (the positive side — the resume can't
      supply it): where they're moving *toward* (not just from), domain leanings
      within the category (payments, devtools, enterprise…), and role shape
      (0→1, platform, GTM-adjacent). This one is open-ended, so ask it as free
      text — still a single question, not a three-part menu — and record 2–3
      sentences in `notes` for narrative use (fit brief, intros); these are not
      scored buckets, so keep them out of the structured fields. Then reconcile
      `targetRoles` with `track` as **one question** (recommending the value the
      resume supports), so a VP-seeking `either` doesn't silently keep seeing
      Staff IC seats.
    - Travel, visa/work auth, commute limits — only if relevant
   - Anything the resume makes surprising (“you’ve been EM for 5 years — still want that?”)

   Do **not** ask every dimension every time. Prefer 3–6 tight questions — each its own call, each with your recommendation — then confirm the written prefs.

   **STOP** after writing prefs — summarize them, then ask **one question** what to do next: import LinkedIn connections (recommended if the graph is empty), discover roles, or search (recommended if the corpus already has jobs).

4. **LinkedIn export instructions** (if companies graph empty)
   Guide the user:

   1. Open LinkedIn → **Settings & Privacy** → **Data privacy** → **Get a copy of your data**
   2. Select **Connections** only (faster than full archive)
   3. Request archive; wait for LinkedIn email
   4. Download the ZIP (contains `Connections.csv`)

   Then hand off to **network-jobs-import**.

5. **Tips** (model-agnostic; paraphrase fine)

   ```text
   Tips
   - Pipeline: import → careers-discover (stages only) → you confirm → jobs-ingest → search → warm intro.
   - Discovery benefits from a stronger model; local search / intros are fine on a mid-tier model.
   - After discover finishes, review triage/ — do not ingest until you say so.
   - Resume: network-jobs profile import ~/path/to/resume.pdf
   - Prefs: re-run this setup skill anytime (“update my job search preferences”).
   - Refresh: network-jobs update · Health: network-jobs doctor
   - Wipe jobs only: network-jobs corpus clear --yes
   ```

## Checks

```bash
DATA="${NETWORK_JOBS_HOME:-$HOME/.network-jobs}"  # always resolve from the env; never export a literal DATA
ls -la "$DATA"
cat "$DATA/profile.json"
cat "$DATA/preferences.json" 2>/dev/null || true
ls -la "$DATA/resume" 2>/dev/null || true
command -v curl jq unzip
command -v agent-browser >/dev/null && agent-browser --version || echo "agent-browser missing — network-jobs update"
command -v network-jobs || echo "network-jobs CLI missing — re-run setup"
```

## Non-negotiables

- Never send profile, resume, or connections to a remote API.
- Never create a hosted account or fetch a remote job export; this suite is local-only.
- Do not name a specific model vendor when giving tips — stay model-agnostic.
- Preferences interview comes **after** resume/background when possible so questions are grounded.
