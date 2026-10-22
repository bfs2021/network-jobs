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
# Of those, the ones render_summary_block also emits. A fresh file must not
# repeat them in the header, so each label appears exactly once (#55).
SUMMARY_BLOCK_FIELDS = ("Listings", "Pagination")
# prefs.match_listings histograms the missing `department` as this sentinel.
UNKNOWN_DEPARTMENT = "(none)"
CRAWL_STATUSES = ("complete", "truncated", "empty", "blocked", "captured")
# Statuses that are not evidence a role closed: an unreadable board and a
# user-supplied capture both say nothing about the rest of the board (#39).
NO_EXPIRY_STATUSES = ("blocked", "captured")

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
    """complete | truncated | empty | blocked | captured, from the crawl result.

    `blocked` means nothing parseable came back; `empty` means a clean, parsed
    board with zero rows (every role closed); `captured` means the rows came
    from a user save, not a crawl. Back-compat: `complete`/`truncated` stay the
    source of truth; `status` is additive.
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


def pagination_line(pages: Any, n_listings: Any = None) -> str:
    """The `Pagination:` line shared by the INVENTORY header and summary block."""
    pag = pages if isinstance(pages, dict) else {}
    return (
        f"pages={pag.get('pages', 0)}, "
        f"complete={'true' if pag.get('complete') else 'false'}, "
        f"truncated={'true' if pag.get('truncated') else 'false'}, "
        f"status={crawl_status(pag, n_listings)}"
    )


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
        values["Pagination"] = pagination_line(pages, n_listings)
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
        # Search the whole file, not just the head: render_summary_block already
        # carries Listings/Pagination, so adding those to the header would show
        # each label twice (see the fresh-file branch in upsert_inventory).
        and not re.search(rf"^- \*\*{re.escape(label)}:\*\*", text, re.M)
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
    if set(departments) == {UNKNOWN_DEPARTMENT}:
        # Every listing lacks a department (e.g. Greenhouse). "(none) (679)"
        # reads as a broken crawl; say the board does not provide the field.
        return f"not provided by this board ({sum(departments.values())})"
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
        lines.append(
            f"- **Pagination:** {pagination_line(pages, summary.get('nListings', summary.get('listings')))}"
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
        # Only the header fields the summary block does not carry; repeating
        # Listings/Pagination here would duplicate them (#55).
        fields = "".join(
            f"- **{label}:** {header[label]}\n"
            for label in HEADER_FIELDS
            if label in header and label not in SUMMARY_BLOCK_FIELDS
        )
        body = f"# Careers triage — {name}\n"
        if fields:
            body += "\n" + fields
        body += "\n## Pipeline summary\n\n" + block
        path.write_text(body if body.endswith("\n") else body + "\n")


def write_summary_json(index_dir: Path, summary: dict[str, Any]) -> Path:
    index_dir.mkdir(parents=True, exist_ok=True)
    dest = index_dir / "summary.json"
    dest.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return dest
