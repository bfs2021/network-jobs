"""ATS-agnostic pagination: follow page / cursor / offset / start into listings.json.

No adapter matrix. Detect next-page signals on whatever JSON the site returns.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .inventory import careers_url_from_listings, crawl_status, upsert_inventory, write_summary_json
from .dates import normalize_date_iso
from .fingerprint import fingerprint, plain_id
from .salary import parse_salary
from .text import collapse_ws

LIST_KEYS = (
    "jobs", "jobPostings", "job_postings", "postings", "results", "items",
    "listings", "openings", "positions", "vacancies", "data", "content",
    "nodes", "records",
)
TITLE_KEYS = ("title", "jobTitle", "job_title", "name", "text", "role")
# Canonical posting page first, apply endpoints last: an apply link 302s to an
# application form, so fetching it never yields a JD (#48). `job_path` is the
# board-relative canonical page some APIs emit (Amazon search.json) and is
# absolutized against the crawled URL in normalize_listing, but it never
# outranks an absolute `jobUrl`/`job_url` (#49).
URL_KEYS = (
    "url", "absolute_url", "absoluteUrl", "hostedUrl", "hosted_url",
    "jobUrl", "job_url", "job_path", "positionUrl", "position_url",
    "applyUrl", "apply_url",
    "url_next_step", "urlNextStep", "gh_src",
)
LOC_KEYS = ("location", "jobLocation", "job_location", "workplace", "city")
# First non-empty candidate wins. Precedence: a key literally named
# "department" (Ashby, most custom APIs) beats the Greenhouse plural
# "departments" list, which beats camel/snake spellings of departmentName,
# which beats the broader "job_category" (Amazon) and finally "team"
# (Ashby's secondary org label) and "group". business_category is
# deliberately absent: it names a business unit, not the hiring department.
# Appending a new shape here is the supported extension point — no per-board
# branching (see skills/careers-discover/SKILL.md).
DEPT_KEYS = (
    "department", "departments", "departmentName", "department_name",
    "job_category", "jobCategory", "team", "group",
)
# A per-posting id wins over a requisition id. Greenhouse reuses one
# `requisition_id` across several distinct postings (per location/team), so
# keying identity on it collapses real jobs in the fingerprint dedupe below
# (#48). `requisition_id` stays as a last resort for boards that emit no other id.
ID_KEYS = (
    "externalId", "atsId", "jobId", "job_id", "id",
    "requisitionId", "requisition_id",
)
NEXT_KEYS = ("next", "nextPage", "next_page", "nextUrl", "next_url")
CURSOR_KEYS = ("nextCursor", "next_cursor", "cursor", "pageId", "page_id", "continuationToken")
TOTAL_KEYS = ("total", "totalFound", "totalCount", "total_results", "count")

# Keys the mapping block reads (case-insensitively) into canonical fields, plus
# the canonical field names themselves. Everything else on an ATS row is an
# extra: it rides along so compensation/workplaceType are not dropped, but a
# raw alias like jobUrl never shadows a canonical field.
_CONSUMED_KEYS = frozenset(
    key.lower()
    for key in (
        TITLE_KEYS + URL_KEYS + LOC_KEYS + DEPT_KEYS + ID_KEYS
        + ("locations", "salary", "postedAt", "posted_at", "updated_at",
           "updatedAt", "firstPublished", "publishedAt", "sourceUrl",
           "matchScore")
    )
)
CANONICAL_FIELDS = (
    "title", "url", "location", "sourceUrl", "locations", "department",
    "externalId", "postedAt", "salary",
)
_RESERVED_KEYS = frozenset(CANONICAL_FIELDS) | _CONSUMED_KEYS

# Raw JD bodies are reserved too: they are NOT extras. ATS payloads ship the
# full posting inline — Lever's `description` (HTML) plus `descriptionPlain` and
# `lists`, Greenhouse's `content` (~45KB of escaped HTML with `?content=true`),
# Ashby's `descriptionHtml`. SCHEMA.md reserves `description` for the plain-text
# body truncated to ~10k chars and stamped with descriptionFetchedAt /
# descriptionSource, and fetch-descriptions is the only writer of that trio.
# Carrying a raw body here would (a) make `description` truthy so
# fetch-descriptions skips the row, and (b) run mustHave/veto matching over
# untruncated HTML and EEO boilerplate. Truncate-and-stamp belongs to
# fetch-descriptions, not to staging.
_JD_BODY_KEYS = frozenset({
    "description", "content", "descriptionhtml", "descriptionplain", "lists",
})

DEFAULT_MAX_PAGES = 15
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_LISTINGS = 2000


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first(obj: dict[str, Any], keys: tuple[str, ...]) -> Any:
    lower = {str(k).lower(): v for k, v in obj.items()}
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
        if key.lower() in lower and lower[key.lower()] not in (None, ""):
            return lower[key.lower()]
    return None


def _id_from(raw: dict[str, Any]) -> Any:
    """First usable ATS id, holding a plain `id` to the fingerprint shape guard.

    `normalize_listing()` writes the result into `externalId`, and `ats_id()`
    trusts an `externalId` without re-checking it — so the guard has to run
    here, or a short/slug `id` outranks a good requisition id and collides
    across companies in a fingerprint that carries no company (#49).
    """
    lower = {str(k).lower(): v for k, v in raw.items()}

    def get(key: str) -> Any:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
        return lower.get(key.lower())

    for key in ID_KEYS:
        value = get(key)
        if value is None or value == "":
            continue
        if key == "id":
            text = plain_id(value)
            if text is None:
                continue
            return text
        text = collapse_ws(str(value))
        if not text or text.lower().startswith("http"):
            continue
        return value
    return None


def _looks_like_job(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    title = _first(obj, TITLE_KEYS)
    url = _first(obj, URL_KEYS)
    loc = _first(obj, LOC_KEYS) or obj.get("locations")
    ext = _first(obj, ID_KEYS)
    return bool(title) and bool(url or loc or ext)


def extract_job_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        if payload and all(isinstance(x, dict) for x in payload[:5]):
            return payload
        return []
    if not isinstance(payload, dict):
        return []
    for key in LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list) and value and _looks_like_job(value[0]):
            return value
        if isinstance(value, dict):
            nested = extract_job_list(value)
            if nested:
                return nested
    # One more hop: payload.data.results etc. already covered via LIST_KEYS recursion
    for value in payload.values():
        if isinstance(value, list) and value and _looks_like_job(value[0]):
            return value
        if isinstance(value, dict):
            nested = extract_job_list(value)
            if nested:
                return nested
    return []


def parsed_empty_board(payload: Any) -> bool:
    """True when a parsed payload carries a recognizable job list with no rows.

    A clean HTTP 200 with `jobs: []` means the company closed every role — a
    real answer, not a failed read. An unparseable body, a missing list key or
    a payload we could not normalize stays "nothing parseable came back" and
    must not be mistaken for an empty board (#39).
    """
    if isinstance(payload, list):
        return not payload
    if not isinstance(payload, dict):
        return False
    for key in LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return not value
        if isinstance(value, dict) and parsed_empty_board(value):
            return True
    for value in payload.values():
        if isinstance(value, dict) and parsed_empty_board(value):
            return True
    return False


def _location_from(raw: Any) -> tuple[str, list[Any]]:
    if raw is None:
        return "", []
    if isinstance(raw, str):
        return collapse_ws(raw), [raw] if collapse_ws(raw) else []
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, str) and collapse_ws(item):
                parts.append(collapse_ws(item))
            elif isinstance(item, dict):
                name = _first(item, ("name", "label", "city", "location", "raw")) or ""
                if name:
                    parts.append(collapse_ws(str(name)))
        return " / ".join(parts), parts
    if isinstance(raw, dict):
        name = _first(raw, ("name", "label", "city", "location", "raw")) or ""
        return collapse_ws(str(name)), [collapse_ws(str(name))] if name else []
    return collapse_ws(str(raw)), [collapse_ws(str(raw))]


def _url_from(raw: dict[str, Any]) -> str:
    """Canonical url for a raw ATS row via URL_KEYS, or "" when absent."""
    url = _first(raw, URL_KEYS)
    if isinstance(url, dict):
        url = url.get("href") or url.get("url") or ""
    if url in (None, ""):
        return ""
    return collapse_ws(str(url))


def _merge_extras(listing: dict[str, Any], raw: dict[str, Any]) -> None:
    """Carry unknown ATS fields onto the normalized listing, in place.

    Extras never overwrite a canonical field and never re-introduce a raw
    alias for one (jobUrl, url_next_step, absolute_url, …), so a listings.json
    row cannot disagree with itself about title/url/location. Raw JD bodies
    (description, content, descriptionHtml, descriptionPlain, lists) are
    reserved as well: they stay in the payload and out of the staged row,
    where fetch-descriptions fills a clean truncated `description` instead.
    """
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            continue
        lowered = key.lower()
        if lowered in _RESERVED_KEYS or lowered in _JD_BODY_KEYS or key in listing:
            continue
        if value is None:
            continue
        listing[key] = value


def _normalize_salary_posted(listing: dict[str, Any]) -> None:
    """Parse string salaries and canonicalize postedAt, in place.

    postedAt becomes YYYY-MM-DD when the ATS format parses (raw kept when
    not); a string salary becomes the parsed dict when it parses. Anything
    unparseable is left untouched — never destroy data here.
    """
    posted = listing.get("postedAt")
    if posted:
        listing["postedAt"] = normalize_date_iso(posted) or str(posted)
    salary = listing.get("salary")
    if isinstance(salary, dict) or not isinstance(salary, str) or not salary.strip():
        return
    parsed = parse_salary(salary)
    if parsed:
        listing["salary"] = parsed


def normalize_listing(raw: Any, source_url: str = "") -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    # Already a staged listing — but only when it is genuinely schema-shaped:
    # a non-empty string title, a non-empty string `url`, and a string
    # location. A truthiness check cannot distinguish "has a value" from "is
    # schema-shaped": Greenhouse sends location as an object, while Ashby hides
    # the link under jobUrl and Amazon under url_next_step with a perfectly
    # ordinary string location. Passing those through verbatim meant the
    # mapping block never ran, so url came out missing on 812/812 Ashby and
    # 1500/1500 Amazon rows. Finding the link only through an alias is not
    # schema-shaped — any such row falls through to the mapping block below,
    # which unpacks it into canonical fields.
    title_raw = raw.get("title")
    url_raw = raw.get("url")
    loc_raw = raw.get("location")
    # `url` is first in URL_KEYS, so a non-empty string `url` is exactly what
    # _url_from() resolves to: this is a shape check, not a preference.
    url_is_staged = isinstance(url_raw, str) and bool(collapse_ws(url_raw))
    if (
        isinstance(title_raw, str) and collapse_ws(title_raw)
        and url_is_staged
        and isinstance(loc_raw, str)
        and "matchScore" not in raw
    ):
        listing = dict(raw)
        listing.setdefault("sourceUrl", source_url)
        _normalize_salary_posted(listing)
        return listing
    title = _first(raw, TITLE_KEYS)
    if not title:
        return None
    # Boards may emit a board-relative canonical page (`job_path` for Amazon,
    # `positionUrl` for Eightfold); resolve it against the crawled URL so
    # staged rows always carry an absolute link (#48). A value without a
    # leading slash would otherwise resolve under the search path
    # (`/en/en/jobs/…`), so anchor it at the host root (#49).
    url = _url_from(raw)
    board_relative = raw.get("job_path")
    if not (isinstance(board_relative, str) and board_relative):
        board_relative = raw.get("positionUrl") or raw.get("position_url")
    if (isinstance(board_relative, str) and board_relative
            and url == collapse_ws(board_relative)
            and not url.startswith("/")):
        url = "/" + url
    url = _absolute(source_url, url)
    loc_raw = None
    for key in LOC_KEYS:
        if raw.get(key) is not None:
            loc_raw = raw.get(key)
            break
    if loc_raw is None:
        loc_raw = raw.get("locations")
    location, locations = _location_from(loc_raw)
    dept = _first(raw, DEPT_KEYS)
    if isinstance(dept, list) and dept:
        dept = dept[0]
    if isinstance(dept, dict):
        dept = _first(dept, ("name", "label", "title"))
    ext = _id_from(raw)
    if isinstance(ext, (int, float)):
        ext = str(int(ext)) if float(ext).is_integer() else str(ext)
    posted = _first(raw, ("postedAt", "posted_at", "updated_at", "updatedAt", "firstPublished", "publishedAt"))
    salary = raw.get("salary")
    listing: dict[str, Any] = {
        "title": collapse_ws(str(title)),
        "url": url,
        "location": location,
        "sourceUrl": source_url,
    }
    if locations:
        listing["locations"] = locations
    if dept:
        listing["department"] = collapse_ws(str(dept))
    if ext and not str(ext).lower().startswith("http"):
        listing["externalId"] = str(ext)
    if posted:
        listing["postedAt"] = str(posted)
    if isinstance(salary, dict):
        listing["salary"] = salary
    elif isinstance(salary, str) and salary.strip():
        parsed = parse_salary(salary)
        if parsed:
            listing["salary"] = parsed
    _merge_extras(listing, raw)
    _normalize_salary_posted(listing)
    if not listing["url"] and not listing["location"] and not listing.get("externalId"):
        return None
    return listing


def _set_query(url: str, **updates: Any) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in updates.items():
        if value is None:
            q.pop(key, None)
        else:
            q[key] = str(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def _absolute(base: str, maybe: str) -> str:
    if not maybe:
        return ""
    if maybe.startswith("http://") or maybe.startswith("https://"):
        return maybe
    return urljoin(base, maybe)


def _numeric_total(value: Any) -> int | None:
    """A numeric total, accepting digit strings; booleans and junk are not."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and text.isdigit():
            return int(text)
    return None


def _count_value(obj: Any) -> int | None:
    """The first numeric total on `obj`, or None. Booleans are not counts.

    Digit strings are accepted because a few APIs send `"count": "100"`.
    A bare `count` sorts last: it is the most ambiguous key (page size, facet
    count, row total), so an explicit `total`/`total_results` wins when both
    are present.
    """
    if not isinstance(obj, dict):
        return None
    for key in TOTAL_KEYS:
        total = _numeric_total(obj.get(key))
        if total is not None:
            return total
    return None


def _top_level_total(payload: Any) -> int | None:
    """A board total on the payload or its meta block (detect_next's rule).

    Explicit totals anywhere beat a bare `count`, and the largest explicit
    total wins across the payload/meta. A `count` on the payload is the last
    resort because it is frequently a page size or facet count. This also
    keeps a digit-string `count` from shadowing a real `meta.total`.
    """
    if not isinstance(payload, dict):
        return None
    explicit: list[int] = []
    for key in ("total", "totalFound", "totalCount", "total_results"):
        total = _numeric_total(payload.get(key))
        if total is not None:
            explicit.append(total)
    for key in ("meta", "pagination"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            for name in ("total", "totalCount", "totalFound"):
                total = _numeric_total(nested.get(name))
                if total is not None:
                    explicit.append(total)
    if explicit:
        return max(explicit)
    return _numeric_total(payload.get("count"))


def _rows_envelope(payload: Any, rows: list[Any]) -> dict[str, Any] | None:
    """The dict that holds exactly `rows` (NVIDIA: `data`), else None.

    Identity, not shape: the list must be the one extract_job_list returned, so
    a count in some other envelope never speaks for these rows. The payload
    itself counts when the rows sit directly on it (`{positions, count}`).
    """
    if not isinstance(payload, dict) or not rows:
        return None
    if any(value is rows for value in payload.values()):
        return payload
    for value in payload.values():
        if isinstance(value, dict) and any(nested is rows for nested in value.values()):
            return value
    return None


def envelope_total(payload: Any, rows: list[Any]) -> int | None:
    """The board's total when it sits beside the extracted rows, else None."""
    envelope = _rows_envelope(payload, rows)
    return _count_value(envelope) if envelope is not None else None


def _start_offset(url: str) -> int | None:
    """The digit-valued `start` of a start-paged URL, else None.

    None when the URL already pages by a real `page`/`pageNumber`/`offset`:
    those params stay detect_next's, and `start` may just be a filter there.
    Blank and zero values are decoys, not paging params.
    """
    q = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))

    def zeroish(value: str) -> bool:
        text = value.strip()
        return text == "" or (text.isascii() and text.isdigit() and int(text) == 0)

    if not zeroish(q.get("page", "")):
        return None
    if not zeroish(q.get("pageNumber", "")):
        return None
    if not zeroish(q.get("offset", "")):
        return None
    start = q.get("start", "")
    return int(start) if start.isdigit() else None


def detect_next_envelope(
    url: str,
    payload: Any,
    rows: list[Any],
    *,
    last_count: int,
) -> str | None:
    """Fallback for Eightfold-shaped boards (NVIDIA, UKG): rows and their total
    sit together (`data.positions`, `data.count`, or at the top level) and the
    URL pages by a digit-valued `start`. Returns the next URL or None.

    The step is the rows the board returned, not the normalized count: a row
    that fails normalization must not make the crawl re-read its neighbour.
    The paginate loop calls this after detect_next, and also uses it to
    replace an invented `offset=` on a start-paged board.
    """
    if last_count == 0 or not rows:
        return None
    start = _start_offset(url)
    if start is None:
        return None
    total_candidates = [
        total for total in (
            envelope_total(payload, rows),
            _top_level_total(payload),
        ) if total is not None
    ]
    if not total_candidates:
        return None
    total = max(total_candidates)
    next_start = start + len(rows)
    if next_start >= total:
        return None
    return _set_query(url, start=next_start)


def detect_next(
    url: str,
    payload: Any,
    *,
    page: int,
    offset: int,
    last_count: int,
    page_size: int,
    fetched: int,
    scheme: str = "none",
) -> tuple[str | None, str]:
    """Return (next_url, scheme). scheme is page|cursor|offset|link|none;
    the paginate loop may record `start` when the envelope fallback took over."""
    if isinstance(payload, dict):
        for key in NEXT_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value and value.lower() not in {"null", "none", "false"}:
                return _absolute(url, value), "link"
            if isinstance(value, dict):
                href = value.get("href") or value.get("url")
                if href:
                    return _absolute(url, str(href)), "link"
        links = payload.get("links") or payload.get("paging") or payload.get("pagination") or payload.get("meta") or {}
        if isinstance(links, dict):
            nxt = links.get("next") or links.get("nextPage") or links.get("next_url")
            if isinstance(nxt, str) and nxt:
                return _absolute(url, nxt), "link"
            if isinstance(nxt, dict) and (nxt.get("href") or nxt.get("url")):
                return _absolute(url, str(nxt.get("href") or nxt.get("url"))), "link"
            cursor = _first(links, CURSOR_KEYS)
            if cursor not in (None, "", 0, "0") or (cursor in (0, "0") and last_count):
                if cursor not in (None, ""):
                    parts = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
                    param = "cursor"
                    for cand in ("cursor", "pageId", "page_id", "continuationToken", "next"):
                        if cand in parts:
                            param = cand
                            break
                    return _set_query(url, **{param: cursor}), "cursor"
        cursor = _first(payload, CURSOR_KEYS)
        if cursor not in (None, ""):
            parts = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
            param = "cursor"
            for cand in ("cursor", "pageId", "page_id", "continuationToken"):
                if cand in parts:
                    param = cand
                    break
            next_url = _set_query(url, **{param: cursor})
            if next_url != url:
                return next_url, "cursor"

        total = _top_level_total(payload)

        q = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        page_param = "page" if "page" in q else ("pageNumber" if "pageNumber" in q else None)
        offset_param = "offset" in q or scheme == "offset"

        # Offset only when the request already uses offset, or there is no page param.
        if offset_param or (page_param is None and scheme in {"none", "offset"} and total is not None):
            page_size_eff = max(page_size, last_count or 1)
            next_offset = offset + last_count
            if last_count == 0:
                return None, "none"
            if total is None:
                if last_count >= page_size_eff:
                    return _set_query(url, offset=next_offset), "offset"
            elif next_offset < total:
                return _set_query(url, offset=next_offset), "offset"

        if page_param:
            if last_count == 0:
                return None, "none"
            full_page = last_count >= max(1, page_size)
            more = total is not None and fetched < total
            if full_page or more:
                try:
                    current = int(q.get(page_param) or page)
                except ValueError:
                    current = page
                return _set_query(url, **{page_param: current + 1}), "page"

    if last_count >= max(page_size, 1) and last_count > 0 and scheme in {"none", "page", "offset"}:
        q = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        if "offset" in q or scheme == "offset":
            return _set_query(url, offset=offset + last_count), "offset"
        if scheme in {"none", "page"}:
            return _set_query(url, page=page + 1), "page"
    return None, "none"


def fetch_json(url: str, timeout: int = 60) -> tuple[Any, int, bytes]:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; NetworkJobs/1.0)", "Accept": "application/json,text/plain,*/*"})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        status = getattr(resp, "status", 200) or 200
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        payload = None
    return payload, int(status), body


def paginate(
    url: str | None = None,
    *,
    payload: Any = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_listings: int = DEFAULT_MAX_LISTINGS,
    page_size: int = DEFAULT_PAGE_SIZE,
    fetch=None,
    source_url: str = "",
    company: str = "",
    capture: bool = False,
) -> dict[str, Any]:
    """Follow pagination into a result dict.

    `complete` means the crawl covered the board: it began at the first
    page/row, hit no cap, and fetched at least the board's reported total when
    one was visible. Anything less is `truncated`, because ingest uses
    `complete` to expire roles the crawl did not see.

    `capture=True` marks a `paginate --input` run: the rows came from a single
    page a human saved, so the run can never prove the board is complete. It
    always records `complete: false` / `status: "captured"` (#39).
    """
    fetch = fetch or fetch_json
    listings: list[dict[str, Any]] = []
    pages = 0
    truncated = False
    complete = True
    empty_board = False
    scheme = "none"
    current = url
    offset = 0
    page = 1
    initial_start = _start_offset(current) if current else None
    if current:
        q = dict(parse_qsl(urlsplit(current).query, keep_blank_values=True))
        if q.get("page", "").isdigit():
            page = int(q["page"])
        if q.get("offset", "").isdigit():
            offset = int(q["offset"])
    initial_page, initial_offset = page, offset
    raw_seen = 0
    reported_total: int | None = None
    saw_board_total = False
    start_mode = False
    pending_start = False
    # (base start-paged URL, next offset) captured before an invented
    # page=/offset= is followed; used only if that page comes back wholly
    # duplicated or empty (#69 review: don't hijack a working pager).
    start_fallback: tuple[str, int] | None = None
    seen_urls: set[str] = set()
    seen_fps: set[str] = set()
    fetch_log: list[dict[str, Any]] = []

    while True:
        if pages >= max_pages:
            # Hitting the page cap exactly on a covered board is not
            # truncation; the board's own total says there is no more.
            if reported_total is not None and len(listings) >= reported_total:
                complete = True
                truncated = False
            else:
                truncated = True
                complete = False
            break
        if current:
            if current in seen_urls:
                complete = True
                break
            seen_urls.add(current)
            payload, status, body = fetch(current)
            fetch_log.append({
                "timestamp": _now(),
                "method": "curl",
                "url": current,
                "status": status,
                "bytes": len(body or b""),
                "disposition": "staged",
                "page": pages + 1,
            })
            source_url = source_url or current
        elif payload is None:
            break

        raw_jobs = extract_job_list(payload)
        raw_seen += len(raw_jobs)
        total_candidates = [
            total for total in (
                envelope_total(payload, raw_jobs),
                _top_level_total(payload),
            ) if total is not None
        ]
        page_total = max(total_candidates) if total_candidates else None
        if page_total is not None and page_total < len(raw_jobs):
            # A count below the rows already served is not a board total
            # (page size / facet count); do not let it lower the bar.
            page_total = None
        if page_total is not None:
            # Keep the largest total seen: a later page that reports a smaller
            # (page-scoped or stale) count must not lower the completeness bar.
            reported_total = (
                page_total if reported_total is None
                else max(reported_total, page_total)
            )
            saw_board_total = True
        # Remember the start-envelope next page before detect_next can
        # invent page=/offset=; it is only used if that page turns out to be
        # a duplicate or empty. A working page/offset pager keeps its URL.
        start_at = _start_offset(current) if current else None
        if start_at is not None and page_total is not None:
            env_next = detect_next_envelope(
                current, payload, raw_jobs, last_count=len(raw_jobs),
            )
            fallback_start = _start_offset(env_next) if env_next else None
            if fallback_start is not None and fallback_start > start_at:
                start_fallback = (current or "", fallback_start)
        if not raw_jobs and parsed_empty_board(payload):
            empty_board = True
        page_listings = []
        for raw in raw_jobs:
            item = normalize_listing(raw, source_url=source_url or (current or ""))
            if item:
                page_listings.append(item)
        # Drop repeats: a server that ignores invented page/cursor params would
        # otherwise loop to max_pages staging the same jobs over and over.
        new_listings = []
        for item in page_listings:
            fp = fingerprint(item, company=company or None)
            if fp in seen_fps:
                continue
            seen_fps.add(fp)
            new_listings.append(item)
        if pending_start and new_listings:
            # The start fallback produced fresh rows: this board pages by
            # `start`, so stop probing invented page=/offset= URLs.
            start_mode = True
            pending_start = False
        if page_listings and not new_listings:
            # Whole page was already seen — the "next" URL added nothing.
            # If detect_next had invented page=/offset= on a start-paged
            # board, try the remembered start envelope before calling it done.
            if (start_fallback is not None
                    and (reported_total is None
                         or len(listings) < reported_total)):
                base_url, next_start = start_fallback
                fallback_url = _set_query(base_url, start=next_start)
                if fallback_url not in seen_urls and fallback_url != current:
                    start_fallback = None
                    pending_start = True
                    pages += 1  # the duplicate fetch happened; count it
                    current = fallback_url
                    payload = None
                    scheme = "start"
                    continue
            complete = True
            break
        listings.extend(new_listings)
        pages += 1
        # Step by the rows the board served, not the normalized count: one
        # unparseable row must not make an offset/page request re-read its
        # neighbour. `page_listings` still drives the dedupe/empty-page guard.
        last_count = len(raw_jobs)
        if len(listings) >= max_listings:
            listings = listings[:max_listings]
            # Hitting the cap is only truncation when the board's own total
            # still says there is more (or no total is visible).
            if reported_total is None or len(listings) < reported_total:
                truncated = True
                complete = False
            break
        if not current:
            complete = True
            break
        if start_mode and start_at is not None:
            env_next = detect_next_envelope(
                current, payload, raw_jobs, last_count=last_count,
            )
            next_url, found = env_next, ("start" if env_next else "none")
        else:
            next_url, found = detect_next(
                current, payload, page=page, offset=offset,
                last_count=last_count, page_size=page_size,
                fetched=len(listings), scheme=scheme,
            )
        if found != "none":
            scheme = found
        if not next_url or next_url == current:
            env_next = detect_next_envelope(
                current, payload, raw_jobs, last_count=last_count,
            )
            if env_next and env_next != current:
                # Its own scheme name: detect_next's offset branch keys on
                # scheme == "offset" and would otherwise invent an offset=
                # param on a later, larger page.
                next_url, found = env_next, "start"
                scheme = found
                start_mode = True
        if (last_count == 0 and start_fallback is not None
                and (reported_total is None or len(listings) < reported_total)):
            # An empty page after an invented page=/offset= can still be a
            # start-paged board's real next page; try the envelope once.
            base_url, next_start = start_fallback
            fallback_url = _set_query(base_url, start=next_start)
            if fallback_url not in seen_urls and fallback_url != current:
                start_fallback = None
                pending_start = True
                current = fallback_url
                payload = None
                scheme = "start"
                continue
        if not next_url or next_url == current or last_count == 0:
            complete = True
            break
        if found == "offset":
            offset += last_count
        if found == "page":
            page += 1
        current = next_url
        payload = None

    # A crawl that started mid-board, or staged fewer unique roles than the
    # board says it has, cannot vouch for the whole board. Coverage is unique
    # listings, not raw rows: a re-sorting board can serve duplicates while
    # pushing unseen roles out of the window, and a row that normalization
    # drops is not coverage either. `rowsFetched` stays diagnostic.
    started_mid_board = (
        initial_page > 1
        or initial_offset > 0
        or (initial_start is not None and initial_start > 0 and saw_board_total)
    )
    if complete and not truncated and (
        started_mid_board
        or (reported_total is not None and len(listings) < reported_total)
    ):
        truncated = True
        complete = False
    if scheme == "none" and saw_board_total and initial_start is not None:
        # A one-page envelope board still paged by `start`; report the scheme.
        scheme = "start"

    # Crawl state (#34/#39). `status` is additive: `complete`/`truncated` keep
    # their old meaning and stay the expiry rule; `status` is the readable,
    # machine-checked headline.
    #   complete  — clean crawl covering the board, rows captured
    #   truncated — partial crawl: a cap was hit before the board total was
    #               covered, it began mid-board, or it staged fewer unique
    #               listings than the board reported
    #   empty     — clean 200 with a parsed, empty job list: every role closed
    #   blocked   — nothing parseable came back (unreadable body, no list key)
    #   captured  — `paginate --input`: a saved page, never the whole board
    pagination_complete = complete and not truncated
    if capture:
        pagination_complete = False
        status = "captured" if listings else "blocked"
    elif not listings and empty_board:
        # A parsed empty list only means "every role closed" when the crawl
        # covered the board; a total the crawl never reached contradicts it.
        status = "empty" if pagination_complete else "blocked"
    elif not listings:
        status = "blocked"
        pagination_complete = False
    elif truncated:
        status = "truncated"
    else:
        status = "complete"
    pagination: dict[str, Any] = {
        "pages": pages,
        "complete": pagination_complete,
        "truncated": truncated,
        "scheme": scheme,
        "maxPages": max_pages,
        "status": status,
    }
    if reported_total is not None:
        pagination["reportedTotal"] = reported_total
    pagination["rowsFetched"] = raw_seen
    if raw_seen > len(listings):
        # Diagnostic only: normalization drops and cap overflow land here too,
        # so this is not a trustworthy subtraction from reportedTotal.
        pagination["deduped"] = raw_seen - len(listings)
    return {
        "listings": listings,
        "sourceUrl": source_url,
        "pagination": pagination,
        "fetchLog": fetch_log,
    }


def blocked_result(url: str, error: str, max_pages: int = DEFAULT_MAX_PAGES) -> dict[str, Any]:
    """A `blocked` crawl result for a fetch that raised.

    The `paginate` CLI records this instead of crashing, so a dead URL still
    leaves a readable `index/pagination.json` (status `blocked`, complete
    false) and a fetch-log entry — never an empty board that ingest could
    expire. `paginate()` itself still raises, so callers like `refresh` keep
    their own per-company error reporting.
    """
    return {
        "listings": [],
        "sourceUrl": url,
        "pagination": {
            "pages": 0,
            "complete": False,
            "truncated": False,
            "scheme": "none",
            "maxPages": max_pages,
            "status": "blocked",
            "rowsFetched": 0,
        },
        "fetchLog": [{
            "timestamp": _now(),
            "method": "curl",
            "url": url,
            "status": 0,
            "bytes": 0,
            "disposition": "error",
            "error": error,
            "page": 1,
        }],
    }


def write_pagination(triage_dir: Path, result: dict[str, Any], company: str = "") -> dict[str, Any]:
    triage = Path(triage_dir)
    index = triage / "index"
    index.mkdir(parents=True, exist_ok=True)
    (index / "listings.json").write_text(
        json.dumps(result["listings"], indent=2, ensure_ascii=False) + "\n"
    )
    pag = dict(result["pagination"])
    pag.setdefault(
        "status", crawl_status(pag, len(result["listings"]))
    )
    (index / "pagination.json").write_text(json.dumps(pag, indent=2) + "\n")
    log_dir = triage / "fetch-log"
    log_dir.mkdir(parents=True, exist_ok=True)
    for i, entry in enumerate(result.get("fetchLog") or [], start=1):
        ts = re.sub(r"[^0-9A-Za-z]", "", entry.get("timestamp") or _now())
        (log_dir / f"{ts}-page-{i}.json").write_text(json.dumps(entry, indent=2) + "\n")
    summary = {
        "company": company,
        "nListings": len(result["listings"]),
        "pagination": pag,
    }
    careers_url = careers_url_from_listings(
        result["listings"], str(result.get("sourceUrl") or "")
    )
    if careers_url:
        summary["careersUrl"] = careers_url
    existing = {}
    summary_path = index / "summary.json"
    if summary_path.is_file():
        try:
            existing = json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            existing = {}
    if isinstance(existing, dict):
        summary = {**existing, **summary}
    write_summary_json(index, summary)
    upsert_inventory(triage / "INVENTORY.md", summary)
    return {"listings": len(result["listings"]), "out": str(index / "listings.json"), **pag}
