"""Fetch job-description bodies for staged listings (#21).

Triage captures title-only rows; every downstream stage (classify,
match-prefs, search, fit brief, intros) then reasons over titles alone.
This fills `description` (truncated plain text) for matches only, so crawl
cost stays bounded — a 712-row board typically yields dozens of matches.
Failures never fail the run: undescribed jobs score exactly as before.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .matches import MatchesFormatError, load_matches, write_matches
from .retry import parse_retry_after as _parse_retry_after
from .text import collapse_ws

MAX_JD_CHARS = 10000
MAX_DOWNLOAD_BYTES = 2_000_000
DEFAULT_TIMEOUT = 30
MAX_RETRY_AFTER = 30.0
# A CDN bot wall (CloudFront/SigSci) clears in about a minute, so the first
# retries wait much longer than the 429 backoff. Beyond these, the host is
# treated as blocked for the run rather than paying the cooldown per job.
BOT_WALL_DELAYS = (30.0, 60.0)
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
        headers = getattr(exc, "headers", None)
        retry_after = _retry_after_seconds(headers)
        if retry_after is not None:
            if retry_after > MAX_RETRY_AFTER:
                # The server asked for longer than we are willing to wait.
                # Report the cap but retry nothing: retrying at 30s would be
                # ignoring the host's request.
                error["retryAfter"] = MAX_RETRY_AFTER
                error["retryAfterTooLong"] = True
            else:
                error["retryAfter"] = retry_after
        if exc.code == 403 and _is_bot_wall(headers):
            # CloudFront/SigSci blocks a burst for ~a minute and then serves
            # the same page; a bare 403 is still a closed/private role.
            error["botWall"] = True
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
    """`Retry-After` as non-negative seconds, else None.

    Shared parsing lives in `network_jobs.retry` (#70); this wrapper
    keeps the #64 call sites and tests unchanged. The value is **not**
    capped here — the caller decides whether a wait that long is
    acceptable (over `MAX_RETRY_AFTER` we skip the retry instead of
    retrying early). A header the server sent but we cannot parse is
    treated as absent, so the caller falls back to exponential backoff.
    """
    return _parse_retry_after(headers)


def _is_bot_wall(headers) -> bool:
    """True when a 403 carries a CDN bot-wall signature, not an auth denial.

    CloudFront+Signal Sciences answers a burst with a 403 that has nothing to
    do with the role: an `x-sigsci-request-id` (the SigSci marker seen in the
    live wall), or a CloudFront-generated error page (`Server: CloudFront`
    plus `x-cache: Error from cloudfront`). `Server: CloudFront` alone is not
    enough — a CloudFront-fronted site can 403 a private or geo-blocked role
    without a wall. The same URL returns 200 once the wall clears (~45s in
    the live e2e).
    """
    if headers is None:
        return False
    try:
        if headers.get("x-sigsci-request-id") is not None:
            return True
        server = str(headers.get("Server") or "").lower()
        cache = str(headers.get("x-cache") or "").lower()
        return "cloudfront" in server and "error from cloudfront" in cache
    except AttributeError:
        return False


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
    """Server-requested delay for 429/503, a long CDN-wall cooldown, or backoff."""
    error = str(result.get("error") or "")
    retry_after = result.get("retryAfter")
    if error in ("http-429", "http-503") and isinstance(retry_after, (int, float)):
        return min(max(float(retry_after), 0.0), MAX_RETRY_AFTER)
    if result.get("botWall"):
        if isinstance(retry_after, (int, float)):
            return min(max(float(retry_after), 0.0), MAX_RETRY_AFTER)
        index = min(max(attempt, 0), len(BOT_WALL_DELAYS) - 1)
        return BOT_WALL_DELAYS[index]
    return retry_delay * (2 ** attempt)


def _should_retry(result: dict[str, Any], *, allow_bot_wall: bool = True) -> bool:
    """Whether this result is worth another attempt.

    A 403 is retried only when it is a CDN bot wall (a bare one never clears),
    and only while the caller still allows the long cooldown — once a host has
    served one wall, a second wall is treated as blocked instead of retried.
    An over-cap `Retry-After` is never retried, whatever the status.
    """
    if result.get("retryAfterTooLong"):
        return False
    if result.get("botWall"):
        return allow_bot_wall
    return _is_retryable(str(result.get("error") or ""))


class _RetryBudget:
    """Run-level guard so an all-failing board cannot multiply the load.

    `max_total_retries` counts **failed** retries only: a retry that recovers
    a row costs nothing, so a board whose first request is throttled for every
    job — and whose retry succeeds — still fetches the whole board. The
    consecutive counter is **per job** (a job that ends in a retryable failure
    counts once, a success or a non-retryable error such as 404 resets it) and
    trips a **soft stop**: the next successful job re-enables retries, so one
    bad host does not disarm the rest of the run. Exhausting the total budget
    is a hard stop.
    """

    def __init__(
        self,
        max_total_retries: int,
        max_consecutive_failures: int,
    ) -> None:
        self.remaining = max(0, int(max_total_retries))
        self.max_consecutive = max(0, int(max_consecutive_failures))
        self.consecutive = 0
        self.soft_tripped = False
        self.exhausted = self.remaining == 0

    @property
    def disabled(self) -> bool:
        """True while retries are off — the summary reports this at the end."""
        return self.exhausted or self.soft_tripped

    def allow_retries(self) -> bool:
        return not self.disabled

    def note_retry_failed(self) -> None:
        """A retry that still produced no body spends one unit. Recovered
        retries never call this, so they cannot starve later jobs."""
        self.remaining -= 1
        if self.remaining <= 0:
            self.exhausted = True

    def finish_job(self, retryable_failure: bool) -> None:
        if retryable_failure:
            self.consecutive += 1
            if self.max_consecutive and self.consecutive >= self.max_consecutive:
                self.soft_tripped = True
        else:
            self.consecutive = 0
            self.soft_tripped = False


def _describe_with_retry(
    url: str,
    *,
    timeout: int,
    fetch,
    retries: int,
    retry_delay: float,
    sleep,
    budget: _RetryBudget,
    allow_retries: bool = True,
    suppress_bot_wall: bool = False,
) -> tuple[dict[str, Any], bool, bool]:
    """Fetch one JD, retrying transient errors with backoff.

    Returns the result plus whether the host answered 429 and whether it
    served a CDN bot wall at any point, so the caller can pace (and, if the
    wall persisted, block) the host for the rest of the run. `suppress_bot_wall`
    turns the long wall cooldown off for a host that already showed a wall
    once, while leaving normal 429/5xx retries available.
    """
    attempts = max(0, int(retries))
    result = fetch_job_description(url, timeout=timeout, fetch=fetch)
    saw_429 = result.get("error") == "http-429"
    saw_bot_wall = bool(result.get("botWall"))
    if allow_retries:
        attempt = 0
        while True:
            retryable = _should_retry(
                result, allow_bot_wall=not suppress_bot_wall)
            if not retryable or attempt >= attempts or not budget.allow_retries():
                break
            sleep(_retry_delay(result, attempt, retry_delay))
            attempt += 1
            result = fetch_job_description(url, timeout=timeout, fetch=fetch)
            saw_429 = saw_429 or result.get("error") == "http-429"
            saw_bot_wall = saw_bot_wall or bool(result.get("botWall"))
            if "error" in result:
                # Only a retry that did not recover the row costs budget.
                budget.note_retry_failed()
        budget.finish_job(
            _should_retry(result, allow_bot_wall=not suppress_bot_wall))
    return result, saw_429, saw_bot_wall


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
    A 403 that carries a CDN bot-wall signature (`x-sigsci-request-id`, or
    `Server: CloudFront` with a CloudFront error cache) is retried after
    `BOT_WALL_DELAYS` — a bare 403 is not. Once a host answers 429 or serves
    a bot wall, the next job on that host waits `host_delay` first. A host
    that serves a second wall is blocked instead of paying the cooldowns
    again: later jobs get one attempt each and the summary carries
    `botWalled: true`. `sleep` is injectable so tests stay instant.

    The run-level budget only charges **failed** retries, so a board whose
    retry recovers every row fetches the whole board. `max_consecutive_failures`
    counts failing jobs and trips a soft stop that the next success clears,
    while exhausting `max_total_retries` is a hard stop; either way a down
    board falls back to one request per row. The summary carries an additive
    `retriesDisabled: true` only when retries are still off at the end.

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
    walled_hosts: set[str] = set()
    blocked_hosts: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            skipped += 1
            continue
        if job.get("description"):
            skipped += 1
            continue
        url = str(job.get("url") or "")
        # A host that just answered 429 (or served a bot wall) gets a pause
        # before the next job, whether or not the retried request succeeded.
        host = urlsplit(url).netloc
        if host and host in throttled_hosts:
            sleep(host_delay)
        result, saw_429, saw_bot_wall = _describe_with_retry(
            url, timeout=timeout, fetch=fetch, retries=retries,
            retry_delay=retry_delay, sleep=sleep, budget=budget,
            allow_retries=host not in blocked_hosts,
            # A recovered wall is not a licence to pay 30s again per job; a
            # second wall on the same host is treated as blocked instead.
            suppress_bot_wall=host in walled_hosts)
        if host and (saw_429 or saw_bot_wall):
            throttled_hosts.add(host)
        if host and saw_bot_wall:
            walled_hosts.add(host)
        if host and result.get("botWall"):
            # The wall is still up (or came back): fail fast on this host
            # rather than paying the cooldown again for every remaining row.
            blocked_hosts.add(host)
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
    if blocked_hosts:
        summary["botWalled"] = True
    return summary
