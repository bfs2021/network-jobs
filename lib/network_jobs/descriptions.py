"""Fetch job-description bodies for staged listings (#21).

Triage captures title-only rows; every downstream stage (classify,
match-prefs, search, fit brief, intros) then reasons over titles alone.
This fills `description` (truncated plain text) for matches only, so crawl
cost stays bounded — a 712-row board typically yields dozens of matches.
Failures never fail the run: undescribed jobs score exactly as before.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .matches import MatchesFormatError, load_matches, write_matches
from .text import collapse_ws

MAX_JD_CHARS = 10000
MAX_DOWNLOAD_BYTES = 2_000_000
DEFAULT_TIMEOUT = 30
MAX_RETRY_AFTER = 30.0
DEFAULT_MAX_TOTAL_RETRIES = 50
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5

# Failures that clear on their own. `TimeoutError`/`ConnectionError` come from
# an injected fetch; through `urlopen` the same causes arrive wrapped in a
# `URLError`, so `_failure_name` unwraps to the reason. A bare `URLError`
# (DNS, bad certificate), `SSLError`, `ValueError` or `InvalidURL` is a
# property of the URL or the environment, not of the moment.
_TRANSIENT_FAILURES = frozenset({
    "TimeoutError",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "ConnectionAbortedError",
    "BrokenPipeError",
    "RemoteDisconnected",
})

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
    except HTTPError as exc:
        # Keep the status so callers can tell 429/5xx (retryable) from 404,
        # and surface Retry-After when the server sent one.
        error: dict[str, Any] = {"error": f"http-{exc.code}"}
        retry_after = _retry_after_seconds(getattr(exc, "headers", None))
        if retry_after is not None:
            error["retryAfter"] = retry_after
        try:
            # HTTPError is also the response object; we never read its body.
            exc.close()
        except Exception:
            pass
        return error
    except Exception as exc:
        return {"error": f"fetch-failed: {_failure_name(exc)}"}
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


def _retry_after_seconds(headers) -> float | None:
    """`Retry-After` as seconds clamped to [0, MAX_RETRY_AFTER], else None.

    Both forms the header allows are accepted: delta-seconds (integer or
    float) and an HTTP-date. A header the server sent but we cannot parse is
    treated as absent, so the caller falls back to exponential backoff.
    """
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        seconds = None
    if seconds is not None:
        if math.isnan(seconds) or math.isinf(seconds):
            return None
        return min(max(seconds, 0.0), MAX_RETRY_AFTER)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(delta, 0.0), MAX_RETRY_AFTER)


def _failure_name(exc: BaseException) -> str:
    """Class name for the error string, unwrapping `URLError`'s cause.

    `urlopen` wraps every `OSError` in `URLError`, which would otherwise
    collapse "connection reset" (transient) and "name or service not known"
    (not transient) into one non-retryable name.
    """
    reason = getattr(exc, "reason", None)
    if isinstance(exc, URLError) and isinstance(reason, BaseException):
        return type(reason).__name__
    return type(exc).__name__


def _is_retryable(error: str) -> bool:
    """Transient failures worth another attempt.

    Throttling (429), server errors (5xx) and a short allowlist of network
    failures usually clear on a second try — a burst of requests gets
    rate-limited but the same page answers moments later. Everything else
    (other 4xx, DNS/TLS `URLError`s, non-http URLs, oversized bodies, pages
    with no usable body) is a property of the URL, not of the moment.
    """
    if not error:
        return False
    if error == "http-429":
        return True
    if error.startswith("http-5") and error[len("http-"):].isdigit():
        return True
    if error.startswith("fetch-failed:"):
        return error.split(":", 1)[1].strip() in _TRANSIENT_FAILURES
    return False


def _retry_delay(result: dict[str, Any], attempt: int, retry_delay: float) -> float:
    """Server-requested delay for 429/503, else exponential backoff."""
    error = str(result.get("error") or "")
    retry_after = result.get("retryAfter")
    if error in ("http-429", "http-503") and isinstance(retry_after, (int, float)):
        return min(max(float(retry_after), 0.0), MAX_RETRY_AFTER)
    return retry_delay * (2 ** attempt)


class _RetryBudget:
    """Run-level guard so an all-failing board cannot multiply the load.

    Bounds the total number of retries and the run of consecutive retryable
    failures. Once tripped, every remaining job gets a single attempt — the
    pre-retry behavior — and the run reports `retriesDisabled`.
    """

    def __init__(
        self,
        max_total_retries: int,
        max_consecutive_failures: int,
    ) -> None:
        self.remaining = max(0, int(max_total_retries))
        self.max_consecutive = max(0, int(max_consecutive_failures))
        self.consecutive = 0
        self.disabled = self.remaining == 0

    def allow(self) -> bool:
        return not self.disabled

    def spend(self) -> None:
        self.remaining -= 1
        if self.remaining <= 0:
            self.disabled = True

    def observe(self, retryable_failure: bool) -> None:
        if not retryable_failure:
            self.consecutive = 0
            return
        self.consecutive += 1
        if self.max_consecutive and self.consecutive >= self.max_consecutive:
            self.disabled = True


def _describe_with_retry(
    url: str,
    *,
    timeout: int,
    fetch,
    retries: int,
    retry_delay: float,
    sleep,
    budget: _RetryBudget,
) -> tuple[dict[str, Any], bool]:
    """Fetch one JD, retrying transient errors with backoff.

    Returns the result and whether the host answered 429 at any point, so the
    caller can pace the next job on that host even if a retry succeeded.
    """
    attempts = max(0, int(retries))
    result = fetch_job_description(url, timeout=timeout, fetch=fetch)
    saw_429 = result.get("error") == "http-429"
    attempt = 0
    while True:
        retryable = _is_retryable(str(result.get("error") or ""))
        budget.observe(retryable)
        if not retryable or attempt >= attempts or not budget.allow():
            return result, saw_429
        budget.spend()
        sleep(_retry_delay(result, attempt, retry_delay))
        attempt += 1
        result = fetch_job_description(url, timeout=timeout, fetch=fetch)
        saw_429 = saw_429 or result.get("error") == "http-429"


def fetch_descriptions(
    triage_dir: str | Path,
    *,
    source: str = "matches",
    timeout: int = DEFAULT_TIMEOUT,
    max_chars: int = MAX_JD_CHARS,
    fetch=None,
    retries: int = 2,
    retry_delay: float = 1.0,
    host_delay: float = 1.0,
    max_total_retries: int = DEFAULT_MAX_TOTAL_RETRIES,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    sleep=time.sleep,
) -> dict[str, Any]:
    """Fill `description` for staged jobs in place. Never raises for fetch
    failures — they land in `failed` and the run continues.

    Transient errors (429, 5xx, network/timeout) get up to `retries` extra
    attempts; a 429/503 honours the server's `Retry-After` (capped at
    `MAX_RETRY_AFTER`) instead of the `retry_delay * 2**attempt` backoff.
    Once a host answers 429, the next job on that host waits `host_delay`
    first. `sleep` is injectable so tests stay instant.

    A run-level budget (`max_total_retries` and `max_consecutive_failures`)
    stops retrying for the remainder of the run when a board is down, and the
    summary then carries an additive `retriesDisabled: true` — an all-failing
    board must not turn one request per row into three plus minutes of sleep.

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
    budget = _RetryBudget(max_total_retries, max_consecutive_failures)
    throttled_hosts: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            skipped += 1
            continue
        if job.get("description"):
            skipped += 1
            continue
        url = str(job.get("url") or "")
        # A host that just answered 429 gets a pause before the next job,
        # whether or not the retried request eventually succeeded.
        host = urlsplit(url).netloc
        if host and host in throttled_hosts:
            sleep(host_delay)
        result, saw_429 = _describe_with_retry(
            url, timeout=timeout, fetch=fetch, retries=retries,
            retry_delay=retry_delay, sleep=sleep, budget=budget)
        if saw_429 and host:
            throttled_hosts.add(host)
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
    summary: dict[str, Any] = {
        "fetched": fetched, "skipped": skipped, "failed": failed,
        "errors": errors, "out": str(path)}
    if budget.disabled:
        summary["retriesDisabled"] = True
    return summary
