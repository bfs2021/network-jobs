---
name: network-jobs-import
description: Import a LinkedIn connections ZIP into the local Network Jobs graph. Use when the user provides a LinkedIn data export ZIP or Connections.csv and wants companies/people available for careers discovery.
license: MIT
allowed-tools: Bash(*)
metadata:
  version: "1.1.0"
---

# Network Jobs Import

Turn a LinkedIn connections export into local `connections.json` + `companies.json`.

Read [SCHEMA.md](../../SCHEMA.md) first.

## Inputs

- Path to LinkedIn ZIP (preferred) or a bare `Connections.csv`
- Optional: `--min-count N` (default 1) — drop companies with fewer than N connections when summarizing

## Asking the user

Whenever the next step is picking from a known set, use the host's choice tool —
`AskUserQuestion` (Claude Code), `question` (opencode), `ask_user_choice`, or the
harness equivalent — instead of a prose list.

- **One question per call.** Ask it, wait for the answer, then ask the next.
  Never bundle two questions into one prompt or one tool call, even when the
  tool accepts several.
- **Recommend, and say why.** Lead with the option you would pick and a one-line
  reason ("Recommended: merge Meta → Facebook — 38 connections otherwise split
  across two rows").
- Put the deciding data in the option label (connection counts, both names),
  never in surrounding prose the user has to correlate.
- Always keep a free-text / "none of these" path.
- No such tool in this host? Use a short numbered list, recommendation first,
  never more than 4 options.

## Workflow

1. **Confirm data home** — `NETWORK_JOBS_HOME` or `~/.network-jobs/`.
2. **Run the parser** (deterministic — do not reimplement CSV parsing in prose):

```bash
DATA="${NETWORK_JOBS_HOME:-$HOME/.network-jobs}"  # always resolve from the env; never export a literal DATA
# Prefer the CLI when available:
network-jobs import "/path/to/linkedin.zip"

# Or call the helper that ships with this skill (works when the skill is symlinked):
HELPER="$(dirname "$0")/helpers/parse-linkedin.sh"
# When running from an agent without dirname context, resolve via suite root:
SUITE="$(cat "${NETWORK_JOBS_HOME:-$HOME/.network-jobs}/suite-root" 2>/dev/null || true)"
SUITE="${NETWORK_JOBS_SUITE:-${SUITE:-}}"
HELPER="${HELPER:-$SUITE/skills/network-jobs-import/helpers/parse-linkedin.sh}"

"$HELPER" "/path/to/linkedin.zip" --out "$DATA"
```

3. **Report**
   - Total connections kept
   - Companies after ignore-list filtering
   - Top 15 companies by connection count (name + count + sample people)
4. **Optional fuzzy merge** — If the user asks, or obvious duplicates appear (e.g. “Meta” / “Facebook”), propose the merges as **one multi-select** — each option labelled `Meta → Facebook (38 connections)` with your recommended merges marked — write the accepted ones to `$DATA/config/company-overrides.json` as `{ "linkedinName": "...", "matchTo": "..." }`, then re-run the helper.
5. **Hand off** — Suggest `careers-discover` on top companies by count.

## Helper behavior

`helpers/parse-linkedin.sh`:

- Extracts `Connections.csv` from ZIP (or reads CSV directly)
- Skips LinkedIn note preamble; finds header with First Name + Company
- Applies ignore lists from `$DATA/config/` (seeded from `assets/` on setup)
- Applies company-overrides
- Writes:
  - `$DATA/connections/connections.json`
  - `$DATA/companies/companies.json`
  - `$DATA/logs/import-<timestamp>.json` summary

## Non-negotiables

- Do not upload the ZIP or CSV anywhere.
- Do not invent companies or people not in the file.
- Prefer the helper over ad-hoc Python/Node one-offs unless the helper fails — then fix/extend the helper.
