#!/usr/bin/env bash
# Shared agent skill directory map for Network Jobs.
# shellcheck shell=bash

# Agent ids we support. Paths are resolved at call time (so HOME overrides work).
NETWORK_JOBS_AGENT_IDS=(
  claude-code
  cursor
  codex
  opencode
  pi
  gemini-cli
  hermes-agent
  muse
  agents
)

# Skill folders this suite installs (must match skills/ and setup).
NETWORK_JOBS_SKILL_IDS=(
  network-jobs-setup
  network-jobs-import
  careers-discover
  jobs-ingest
  network-jobs
  intro-email-generator
)

network_jobs_agent_dir() {
  local name="$1"
  case "$name" in
    claude-code) echo "$HOME/.claude/skills" ;;
    cursor) echo "$HOME/.cursor/skills" ;;
    codex) echo "$HOME/.codex/skills" ;;
    opencode) echo "$HOME/.config/opencode/skills" ;;
    pi) echo "$HOME/.pi/agent/skills" ;;
    gemini-cli) echo "$HOME/.gemini/skills" ;;
    hermes-agent) echo "$HOME/.hermes/skills" ;;
    muse) echo "$HOME/.agents/skills" ;;
    agents) echo "$HOME/.agents/skills" ;;
    *) return 1 ;;
  esac
}

network_jobs_agent_hint() {
  local name="$1"
  case "$name" in
    claude-code) echo "$HOME/.claude" ;;
    cursor) echo "$HOME/.cursor" ;;
    codex) echo "$HOME/.codex" ;;
    opencode) echo "$HOME/.config/opencode" ;;
    pi) echo "$HOME/.pi" ;;
    gemini-cli) echo "$HOME/.gemini" ;;
    hermes-agent) echo "$HOME/.hermes" ;;
    muse) echo "$HOME/.agents" ;;
    agents) echo "$HOME/.agents" ;;
    *) return 1 ;;
  esac
}

# CLI binary proving an agent's presence when it keeps no home dir
# (Muse reads the shared ~/.agents/skills like the generic agents entry).
network_jobs_agent_bin() {
  local name="$1"
  case "$name" in
    muse) echo "muse" ;;
    *) return 1 ;;
  esac
}

network_jobs_agent_detected() {
  local name="$1" hint bin
  hint="$(network_jobs_agent_hint "$name" || true)"
  if [[ -n "$hint" && -d "$hint" ]]; then
    return 0
  fi
  bin="$(network_jobs_agent_bin "$name" || true)"
  if [[ -n "$bin" ]] && command -v "$bin" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

network_jobs_detect_agents() {
  local name
  local -a found=()
  for name in "${NETWORK_JOBS_AGENT_IDS[@]}"; do
    if network_jobs_agent_detected "$name"; then
      found+=("$name")
    fi
  done
  if ((${#found[@]} == 0)); then
    found=("claude-code")
  fi
  printf '%s\n' "${found[@]}"
}

network_jobs_list_agents() {
  local name dir hint
  for name in "${NETWORK_JOBS_AGENT_IDS[@]}"; do
    dir="$(network_jobs_agent_dir "$name")"
    hint="$(network_jobs_agent_hint "$name")"
    if network_jobs_agent_detected "$name"; then
      printf '%s\t%s\tdetected\n' "$name" "$dir"
    else
      printf '%s\t%s\t\n' "$name" "$dir"
    fi
  done
}

# True when this suite root is an npm-global install
# (e.g. ~/.local/share/mise/installs/node/*/lib/node_modules/@hirefrank/network-jobs).
network_jobs_is_npm_global_install() {
  local root="${1:-}"
  [[ -z "$root" ]] && return 1
  case "$root" in
    */lib/node_modules/@hirefrank/network-jobs|*/lib/node_modules/@hirefrank/network-jobs/) return 0 ;;
    *) return 1 ;;
  esac
}

# True when this suite root is an npx/pnpm store copy (not a git checkout).
network_jobs_is_packaged_install() {
  local root="${1:-}"
  [[ -z "$root" ]] && return 1
  [[ -d "$root/.git" ]] && return 1
  case "$root" in
    */node_modules/@hirefrank/network-jobs|*/node_modules/@hirefrank/network-jobs/) return 0 ;;
    */.local/share/pnpm/*) return 0 ;;
    */.npm/_npx/*) return 0 ;;
    */.cache/pnpm/*) return 0 ;;
    *) return 1 ;;
  esac
}

# Where the `network-jobs` launcher may live, in preference order. One list so
# `setup` (which writes it) and `reset` (which tells the user to remove it)
# cannot drift apart.
network_jobs_launcher_candidates() {
  printf '%s\n' "$HOME/.local/bin" "$HOME/bin"
}

# The directory `setup` would install into: first candidate already on PATH,
# else the first candidate (matches the historical ~/.local/bin default).
network_jobs_launcher_dir() {
  local candidate
  while IFS= read -r candidate; do
    case ":$PATH:" in
      *":$candidate:"*) printf '%s\n' "$candidate"; return 0 ;;
    esac
  done < <(network_jobs_launcher_candidates)
  network_jobs_launcher_candidates | head -n1
}

# Existing launcher scripts, checked across every candidate directory (PATH may
# have changed since install). Only setup's own launcher counts: it carries a
# "# network-jobs launcher" marker and is a regular file, so an npm `.bin`
# symlink or the user's own wrapper is never offered up for deletion.
network_jobs_launcher_paths() {
  local dir path
  while IFS= read -r dir; do
    path="$dir/network-jobs"
    if [[ -f "$path" && ! -L "$path" ]] && grep -q "network-jobs launcher" "$path" 2>/dev/null; then
      printf '%s\n' "$path"
    fi
  done < <(network_jobs_launcher_candidates)
  return 0
}

# Print the top-level cache entry directory holding a nested package path.
network_jobs__cache_entry_root() {
  local base="$1" nested="$2" rest
  rest="${nested#"$base"/}"
  printf '%s/%s\n' "$base" "${rest%%/*}"
}

# Drop cached npx/pnpm copies so the next github: fetch resolves fresh main.
# dlx/_npx entry dirs are content hashes, so match on the nested package path.
network_jobs_purge_package_cache() {
  local removed=0 cache entry nested
  local -a entries=()

  for cache in "$HOME"/.local/share/pnpm/store/v*/links/@hirefrank/network-jobs; do
    if [[ -e "$cache" || -L "$cache" ]]; then
      rm -rf "$cache"
      echo "removed $cache"
      removed=1
    fi
  done

  for cache in "$HOME/.cache/pnpm/dlx" "$HOME/.npm/_npx"; do
    [[ -d "$cache" ]] || continue
    entries=()
    while IFS= read -r nested; do
      entries+=("$(network_jobs__cache_entry_root "$cache" "$nested")")
    done < <(find "$cache" -maxdepth 6 -type d -path '*node_modules/@hirefrank' 2>/dev/null)
    ((${#entries[@]})) || continue
    while IFS= read -r entry; do
      [[ -d "$entry" ]] || continue
      rm -rf "$entry"
      echo "removed cached copy $entry"
      removed=1
    done < <(printf '%s\n' "${entries[@]}" | sort -u)
  done

  ((removed)) || echo "no cached suite copies found"
}

# Echo a command prefix that can run a package straight from GitHub.
network_jobs_package_runner() {
  if command -v pnpm >/dev/null 2>&1 && pnpm --version >/dev/null 2>&1; then
    echo "pnpm dlx"
    return 0
  fi
  if command -v npx >/dev/null 2>&1 && npx --version >/dev/null 2>&1; then
    echo "npx --yes"
    return 0
  fi
  return 1
}
