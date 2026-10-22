"""Fetch job-description bodies for staged listings (#21).

Triage captures title-only rows; every downstream stage (classify,
match-prefs, search, fit brief, intros) then reasons over titles alone.
This fills `description` (truncated plain text) for matches only, so crawl
cost stays bounded — a 712-row board typically yields dozens of matches.
Failures never fail the run: undescribed jobs score exactly as before.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .matches import MatchesFormatError, load_matches, write_matches
from .text import collapse_ws

MAX_JD_CHARS = 10000
MAX_DOWNLOAD_BYTES = 2_000_000
DEFAULT_TIMEOUT = 30

_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "nav", "header", "footer",
    "svg", "form", "button", "select", "option",
})

_JSON_TEXT_KEYS = (
    "content", "description", "jobDescription", "job_description",
    "body", "text", "about", "summary",
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "ul", "ol"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = collapse_ws(data)
        if text:
            self.parts.append(text)

    def text(self) -> str:
        paras: list[str] = []
        buf: list[str] = []
        for part in self.parts:
            if part == "\n":
                if buf:
                    paras.append(collapse_ws(" ".join(buf)))
                    buf = []
            else:
                buf.append(part)
        if buf:
            paras.append(collapse_ws(" ".join(buf)))
        return "\n\n".join(p for p in paras if p)


def strip_html(html: str) -> str:
    """Best-effort HTML → plain text with stdlib only."""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:
        return collapse_ws(html)
    return extractor.text()


def _dept_from_payload(payload: Any) -> str:
    """Department name from an ATS JSON payload, else ''."""
    if not isinstance(payload, dict):
        return ""
    for key in ("department", "team"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict) and str(value.get("name") or "").strip():
            return str(value["name"]).strip()
    departments = payload.get("departments")
    if isinstance(departments, list) and departments:
        first = departments[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
        if isinstance(first, dict) and str(first.get("name") or "").strip():
            return str(first["name"]).strip()
    return ""


def _texts_from_payload(payload: Any) -> str:
    """Longest plausible body field from an ATS JSON payload."""
    if not isinstance(payload, dict):
        return ""
    best = ""
    for key in _JSON_TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and len(value) > len(best):
            best = value
    jobs = payload.get("job") if isinstance(payload.get("job"), dict) else None
    if jobs:
        for key in _JSON_TEXT_KEYS:
            value = jobs.get(key)
            if isinstance(value, str) and len(value) > len(best):
                best = value
    return best


def fetch_job_description(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    fetch=None,
) -> dict[str, Any]:
    """Fetch one JD. Returns {description, department, source} or {error}."""
    if not url or not url.startswith(("http://", "https://")):
        return {"error": "non-http-url"}
    try:
        if fetch is not None:
            status, content_type, body = fetch(url)
        else:
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; NetworkJobs/1.0)",
                "Accept": "text/html,application/json,text/plain,*/*",
            })
            with urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200) or 200
                content_type = resp.headers.get("Content-Type", "")
                body = resp.read(MAX_DOWNLOAD_BYTES + 1)
    except Exception as exc:
        return {"error": f"fetch-failed: {type(exc).__name__}"}
    if status and status >= 400:
        return {"error": f"http-{status}"}
    if len(body or b"") > MAX_DOWNLOAD_BYTES:
        return {"error": "too-large"}
    raw = (body or b"").decode("utf-8", errors="replace")
    department = ""
    if "json" in (content_type or "").lower():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if payload is not None:
            department = _dept_from_payload(payload)
            text = _texts_from_payload(payload)
            if len(text.strip()) >= 200:
                return _pack(text, department, url)
    text = strip_html(raw)
    if len(text.strip()) < 200:
        return {"error": "no-usable-body"}
    return _pack(text, department, url)


def _pack(text: str, department: str, url: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "description": collapse_ws(text)[:MAX_JD_CHARS],
        "descriptionFetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "descriptionSource": url,
    }
    if department:
        out["department"] = department
    return out


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_descriptions(
    triage_dir: str | Path,
    *,
    source: str = "matches",
    timeout: int = DEFAULT_TIMEOUT,
    max_chars: int = MAX_JD_CHARS,
    fetch=None,
) -> dict[str, Any]:
    """Fill `description` for staged jobs in place. Never raises for fetch
    failures — they land in `failed` and the run continues.

    `matches.json` keeps its documented object shape (#32): the wrapper and
    its summary keys are read through `load_matches` and written back through
    `write_matches`, so a legacy bare array is upgraded rather than preserved.
    Summary values are left exactly as `match-prefs` computed them —
    `nMatches`/`showing` still describe the shortlist and `departments` stays
    the listing-wide histogram. The latter can be stale after this helper
    fills `department` from an ATS payload; re-running `match-prefs`
    recomputes it. We do not recompute it here because `jobs` is only the
    shortlist, so counting it would silently redefine the histogram.
    """
    triage = Path(triage_dir).expanduser().resolve()
    index = triage / "index"
    name = "matches.json" if source == "matches" else "listings.json"
    path = index / name
    if source == "matches":
        try:
            envelope = load_matches(path)
        except MatchesFormatError:
            return {"fetched": 0, "skipped": 0, "failed": 0,
                    "errors": [f"unreadable {name}"], "out": str(path)}
        jobs = envelope["jobs"]
    else:
        try:
            jobs = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"fetched": 0, "skipped": 0, "failed": 0,
                    "errors": [f"unreadable {name}"], "out": str(path)}
        if not isinstance(jobs, list):
            return {"fetched": 0, "skipped": 0, "failed": 0,
                    "errors": ["unexpected shape"], "out": str(path)}
    fetched = skipped = failed = 0
    errors: list[str] = []
    for job in jobs:
        if not isinstance(job, dict):
            skipped += 1
            continue
        if job.get("description"):
            skipped += 1
            continue
        url = str(job.get("url") or "")
        result = fetch_job_description(url, timeout=timeout, fetch=fetch)
        if "error" in result:
            failed += 1
            if len(errors) < 5:
                errors.append(f"{url or '(no url)'}: {result['error']}")
            continue
        desc = str(result.get("description") or "")
        if max_chars and len(desc) > max_chars:
            desc = desc[:max_chars]
        job["description"] = desc
        job["descriptionFetchedAt"] = result.get(
            "descriptionFetchedAt") or _utcnow()
        job["descriptionSource"] = result.get("descriptionSource") or url
        if result.get("department") and not job.get("department"):
            job["department"] = result["department"]
        fetched += 1
    if source == "matches":
        write_matches(path, envelope)
    else:
        path.write_text(json.dumps(jobs, indent=2, ensure_ascii=False) + "\n")
    return {"fetched": fetched, "skipped": skipped, "failed": failed,
            "errors": errors, "out": str(path)}
