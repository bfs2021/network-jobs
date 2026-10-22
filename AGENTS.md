## Learned User Preferences

- Prefer a public, installable multi-agent skill suite (gstack/Keep-style CLI) over Claude-only or private-only packaging.
- Prefer ATS-agnostic, model-driven career-page discovery and parsing rather than hard-coded ATS adapters.
- Day-to-day use should be in the agent after one-time CLI setup; keep the CLI for setup, doctor, and optional import.
- Do not commit real LinkedIn connection dumps or other personal network PII; keep only synthetic fixtures in-repo.
- Prefer quiet CLI output by default; put detail behind a `-v` / verbose flag.
- Prefer setup to install `agent-browser` when missing rather than leaving it optional-only.
- Prefer automatic skill/routing install over asking users to paste AGENTS.md or CLAUDE.md snippets.
- Prefer setup to leave `network-jobs` on PATH (e.g. `~/.local/bin`) without requiring a manual shell `source`.
- Do not surface the deprecated hosted product (`jobs.hirefrank.com`) in skill-suite setup or docs.

## Learned Workspace Facts

- Public repo is `hirefrank/network-jobs`; this workspace is the local skill suite at `~/Projects/network-jobs`.
- Runtime data lives under `~/.network-jobs/` and is shared across agents (Claude Code, Cursor, Codex, etc.).
- Skills live under `skills/`; install via `npx skills add hirefrank/network-jobs` and/or `network-jobs setup --agent`.
- Clone or install the suite as `network-jobs-suite` (not `network-jobs`) so the `network-jobs` skill can be symlinked without path collision.
- Pipeline is import → careers-discover (stage only) → user confirm → jobs-ingest → search → warm intro; never invent job fields; the suite has no hosted API (local files only).
- Scraping-skill patterns are modeled after `~/Projects/provenance`; the hosted 410 Worker source is `worker/` (`jobs-api` on `jobs.hirefrank.com`), with ops notes in `worker/SUNSET.md`.
- Setup links the same skill set into multiple agent skill dirs (claude-code, cursor, codex, opencode, pi, gemini-cli, hermes-agent, muse, agents), typically via symlinks re-pointed on re-setup.
- Install with `npm i -g @hirefrank/network-jobs@latest && network-jobs setup --agent auto` (registry primary; `npx --yes @hirefrank/network-jobs@latest setup …` for zero-install; `npx --yes 'github:hirefrank/network-jobs#main' …` only for bleeding edge — use `#main`, not `@main`); setup should place the CLI on PATH under `~/.local/bin`.
