"""Deterministic INVENTORY.md summary block (helpers, not the model)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MARKER_START = "<!-- nj-summary:start -->"
MARKER_END = "<!-- nj-summary:end -->"

# Header fields the helpers own. They must agree with index/*.json, so they
# are patched by code rather than left to the model (see #33).
HEADER_FIELDS = ("Listings", "Careers URL", "Pagination", "Status")
CRAWL_STATUSES = ("complete", "truncated", "blocked")

_HEADER_FIELD_RE = re.compile(r"^- \*\*[^*\n]+:\*\*[^\n]*(?:\n|$)", re.M)


def careers_url_from_listings(listings: Any, fallback: str = "") -> str:
    """The crawl URL the rows were extracted from, when the data knows it."""
    for item in listings or []:
        if not isinstance(item, dict):
            continue
        for key in ("sourceUrl", "source_url"):
            value = str(item.get(key) or "").strip()
            if value.startswith(("http://", "https://")):
                return value
    fb = str(fallback or "").strip()
    return fb if fb.startswith(("http://", "https://")) else ""


def crawl_status(pagination: Any, n_listings: Any) -> str:
    """complete | truncated | blocked, derived from the crawl result.

    `blocked` means no listing was captured — the board could not be read, not
    that the company has no openings. Back-compat: `complete`/`truncated`
    stay the source of truth; `status` is additive.
    """
    pages = pagination if isinstance(pagination, dict) else {}
    status = pages.get("status")
    if status in CRAWL_STATUSES:
        return status
    has_n = isinstance(n_listings, int) and not isinstance(n_listings, bool)
    if has_n and n_listings == 0:
        return "blocked"
    if pages.get("truncated") or pages.get("complete") is False:
        return "truncated"
    return "complete"


def _header_values(summary: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    n_listings = summary.get("nListings", summary.get("listings"))
    has_n = isinstance(n_listings, int) and not isinstance(n_listings, bool)
    if has_n:
        values["Listings"] = str(n_listings)
    careers_url = str(summary.get("careersUrl") or "").strip()
    if careers_url.startswith(("http://", "https://")):
        values["Careers URL"] = careers_url
    pages = summary.get("pagination")
    if isinstance(pages, dict) and pages:
        values["Pagination"] = (
            f"pages={pages.get('pages', 0)}, "
            f"complete={'true' if pages.get('complete') else 'false'}, "
            f"truncated={'true' if pages.get('truncated') else 'false'}"
        )
        values["Status"] = crawl_status(pages, n_listings)
    elif has_n:
        values["Status"] = crawl_status(None, n_listings)
    return values


def patch_inventory_header(text: str, values: dict[str, str]) -> str:
    """Replace header fields in the top block only, never the summary block."""
    if not values:
        return text
    marker = text.find(MARKER_START)
    if marker >= 0:
        head, tail = text[:marker], text[marker:]
    else:
        # No marker yet: treat the title block (before the next ## heading) as
        # the header so we never rewrite unrelated prose sections.
        match = re.search(r"\n## ", text)
        if match:
            head, tail = text[: match.start() + 1], text[match.start() + 1:]
        else:
            head, tail = text, ""
    for label, value in values.items():
        pattern = re.compile(rf"^- \*\*{re.escape(label)}:\*\*[^\n]*", re.M)
        line = f"- **{label}:** {value}"
        if pattern.search(head):
            head = pattern.sub(lambda _m: line, head, count=1)
    missing = [
        label for label in HEADER_FIELDS
        if label in values
        and not re.search(rf"^- \*\*{re.escape(label)}:\*\*", head, re.M)
    ]
    if missing:
        block = "".join(f"- **{label}:** {values[label]}\n" for label in missing)
        fields = list(_HEADER_FIELD_RE.finditer(head))
        if fields:
            pos = fields[-1].end()
        else:
            title = re.match(r"# [^\n]*(?:\n|$)", head)
            pos = title.end() if title else 0
            if pos:
                block = "\n" + block
        head = head[:pos] + block + head[pos:]
    return head + tail


def department_histogram_line(departments: dict[str, int], limit: int = 12) -> str:
    items = sorted(departments.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    if not items:
        return "(none)"
    return ", ".join(f"{name} ({count})" for name, count in items)


def render_summary_block(summary: dict[str, Any]) -> str:
    listings = summary.get("nListings", summary.get("listings", 0))
    matches = summary.get("nMatches")
    pages = summary.get("pagination") or {}
    lines = [
        MARKER_START,
        f"- **Listings:** {listings}",
    ]
    if matches is not None:
        lines.append(f"- **Matches:** {matches} of {listings} match prefs")
    depts = summary.get("departments") or {}
    if depts:
        lines.append(f"- **Departments:** {department_histogram_line(depts)}")
    if pages:
        complete = "true" if pages.get("complete") else "false"
        truncated = "true" if pages.get("truncated") else "false"
        lines.append(
            f"- **Pagination:** pages={pages.get('pages', 0)}, complete={complete}, truncated={truncated}"
        )
        extra = pages.get("scheme")
        if extra:
            lines.append(f"- **Pagination scheme:** {extra}")
    crawl = summary.get("crawl") or {}
    if crawl:
        if crawl.get("unchanged"):
            lines.append("- **Crawl:** listing-set hash unchanged — skip refresh")
        elif crawl.get("listingSetHash"):
            lines.append(f"- **Crawl hash:** {crawl['listingSetHash']}")
    ingest = summary.get("ingestDefault") or "matches"
    lines.append(f"- **Ingest default:** {ingest} (say “ingest all” or a department to override)")
    lines.append(MARKER_END)
    return "\n".join(lines) + "\n"


def upsert_inventory(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    block = render_summary_block(summary)
    header = _header_values(summary)
    if path.is_file():
        text = path.read_text()
        text = patch_inventory_header(text, header)
        if MARKER_START in text and MARKER_END in text:
            text = re.sub(
                re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
                block.strip(),
                text,
                count=1,
                flags=re.S,
            )
        else:
            text = text.rstrip() + "\n\n## Pipeline summary\n\n" + block
        path.write_text(text if text.endswith("\n") else text + "\n")
    else:
        name = summary.get("company") or "company"
        lines = [f"# Careers triage — {name}", ""]
        lines.extend(f"- **{label}:** {header[label]}" for label in HEADER_FIELDS if label in header)
        if len(lines) > 2:
            lines.append("")
        body = "\n".join(lines) + block
        path.write_text(body if body.endswith("\n") else body + "\n")


def write_summary_json(index_dir: Path, summary: dict[str, Any]) -> Path:
    index_dir.mkdir(parents=True, exist_ok=True)
    dest = index_dir / "summary.json"
    dest.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return dest
