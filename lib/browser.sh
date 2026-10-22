#!/usr/bin/env bash
# agent-browser version helpers, shared by `setup` and `bin/network-jobs`.
#
# Browser automation is optional (JS-heavy career pages only), so nothing here
# ever aborts a run: these helpers report, and the caller decides.
#
# Why a floor exists at all: presence checks cannot tell a current build from a
# stale one, and a stale build fails in ways that look like the site's fault
# (missing flags, changed output, dead subcommands). A version comparison makes
# that visible before it costs a crawl. The floor is the version the suite is
# tested against, not a claim that newer builds are safe or older ones broken.

# Print the installed version ("0.39.1") or return 1 when absent/unparseable.
ab_installed_version() {
  command -v agent-browser >/dev/null 2>&1 || return 1
  local out
  out="$(agent-browser --version 2>/dev/null | head -n1 || true)"
  printf '%s\n' "$out" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1
}

# Print agentBrowser.minVersion from a package.json, or return 1.
ab_min_version() {
  local pkg="${1:-}"
  [[ -n "$pkg" && -f "$pkg" ]] || return 1
  python3 - "$pkg" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    version = (data.get("agentBrowser") or {}).get("minVersion") or ""
except Exception:
    version = ""
print(version)
PY
}

# Exit 0 when $1 >= $2 (semver-ish: numeric, missing parts treated as 0).
# python3 rather than `sort -V`, which is GNU-only and absent on macOS.
ab_version_ge() {
  local have="${1:-}" want="${2:-}"
  [[ -n "$have" && -n "$want" ]] || return 1
  python3 - "$have" "$want" <<'PY'
import sys


def parts(value: str) -> tuple[int, int, int]:
    nums = []
    for chunk in value.split(".")[:3]:
        digits = "".join(ch for ch in chunk if ch.isdigit())
        nums.append(int(digits or 0))
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)  # type: ignore[return-value]


sys.exit(0 if parts(sys.argv[1]) >= parts(sys.argv[2]) else 1)
PY
}

# One-line description of an installed version against the floor.
# "0.39.1" when current (or no floor), "0.38.1 < 0.39.1" when below, "unknown" when absent.
ab_describe_version() {
  local have="${1:-}" min="${2:-}"
  if [[ -z "$have" ]]; then
    printf 'unknown'
    return 0
  fi
  if [[ -n "$min" ]] && ! ab_version_ge "$have" "$min"; then
    printf '%s < %s' "$have" "$min"
  else
    printf '%s' "$have"
  fi
}
