#!/usr/bin/env bash
# Create a triage skeleton for one company careers scrape.
# Slug matches network-jobs-import normalization (suffix-stripped) and
# prefers an existing companies.json slug when the name matches.
set -euo pipefail

NAME="${1:-}"
SLUG="${2:-}"
if [[ -z "$NAME" ]]; then
  echo "Usage: stage-company.sh <Company Name> [slug]" >&2
  exit 1
fi

DATA="${NETWORK_JOBS_HOME:-$HOME/.network-jobs}"
DATE="$(date -u +%Y-%m-%d)"

if [[ -z "$SLUG" ]]; then
  export STAGE_NAME="$NAME"
  export STAGE_COMPANIES="$DATA/companies/companies.json"
  export STAGE_OVERRIDES="$DATA/config/company-overrides.json"
  SLUG="$(python3 <<'PY'
import json, os, re
from pathlib import Path

name = os.environ["STAGE_NAME"]
companies_path = Path(os.environ["STAGE_COMPANIES"])
overrides_path = Path(os.environ.get("STAGE_OVERRIDES", ""))

SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|corp|corporation|llc|ltd|limited|llp|lp|plc|co|company|group|holdings|worldwide|international|global|technologies|technology|solutions|services)\b",
    re.I,
)
PUNCT_RE = re.compile(r"[.,/#!$%^&*;:{}=\-_`~()•]")

def basic_normalize(s: str) -> str:
    s = s.lower()
    s = PUNCT_RE.sub(" ", s)
    s = SUFFIX_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()

def slugify(normalized: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return s or "unknown"

overrides = {}
if overrides_path.exists():
    raw = json.loads(overrides_path.read_text())
    if isinstance(raw, list):
        for o in raw:
            if isinstance(o, dict) and "linkedinName" in o and "matchTo" in o:
                overrides[basic_normalize(o["linkedinName"])] = basic_normalize(o["matchTo"])

norm = basic_normalize(name)
norm = overrides.get(norm, norm)

# Prefer slug from companies.json when normalized name matches (after overrides).
# Do not match on raw display name — that bypasses overrides (e.g. Facebook → Meta).
if companies_path.exists():
    companies = json.loads(companies_path.read_text())
    if isinstance(companies, list):
        for c in companies:
            cname = c.get("name") or ""
            cnorm = c.get("normalized") or basic_normalize(cname)
            if cnorm == norm or basic_normalize(cname) == norm:
                print(c.get("slug") or slugify(cnorm))
                raise SystemExit(0)

print(slugify(norm))
PY
)"
fi

DIR="$DATA/triage/careers-${SLUG}-${DATE}"
mkdir -p "$DIR/index" "$DIR/fetch-log"

if [[ ! -f "$DIR/index/listings.json" ]]; then
  echo '[]' > "$DIR/index/listings.json"
fi

if [[ ! -f "$DIR/INVENTORY.md" ]]; then
  # Pre-helper placeholders. Once a helper writes the Pipeline summary block,
  # `Listings` (and `Pagination` when there is pagination data) are canonical
  # there and these header copies are removed (#63). Keeping the placeholders
  # helps a manual card extract: it has no summary block and keeps its
  # hand-filled header `Pagination` line.
  cat > "$DIR/INVENTORY.md" <<EOF
# Careers triage — ${NAME}

- **Slug:** ${SLUG}
- **Date:** ${DATE}
- **Careers URL:** (fill)
- **Listings:** 0
- **Pagination:** (pending)
- **Status:** (pending)
- **Caveats:**
- **Next steps:** Review listings.json, then run jobs-ingest if OK.
EOF
fi

echo "$DIR"
