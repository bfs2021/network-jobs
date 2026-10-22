"""ATS-agnostic pagination: follow page / cursor / offset into listings.json.

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
from .fingerprint import fingerprint
from .salary import parse_salary
from .text import collapse_ws

LIST_KEYS = (
    "jobs", "jobPostings", "job_postings", "postings", "results", "items",
    "listings", "openings", "positions", "vacancies", "data", "content",
    "nodes", "records",
)
TITLE_KEYS = ("title", "jobTitle", "job_title", "name", "text", "role")
URL_KEYS = (
    "url", "absolute_url", "absoluteUrl", "hostedUrl", "hosted_url",
    "jobUrl", "job_url", "applyUrl", "apply_url", "url_next_step",
    "urlNextStep", "gh_src", "absolute_url",
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
ID_KEYS = ("externalId", "atsId", "jobId", "job_id", "requisitionId", "requisition_id", "id")
NEXT_KEYS = ("next", "nextPage", "next_page", "nextUrl", "next_url")
CURSOR_KEYS = ("nextCursor", "next_cursor", "cursor", "pageId", "page_id", "continuationToken")

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
    url = _url_from(raw)
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
    ext = _first(raw, ID_KEYS)
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
    """Return (next_url, scheme). scheme is page|cursor|offset|link|none."""
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

        total = None
        for key in ("total", "totalFound", "totalCount", "count", "total_results"):
            if isinstance(payload.get(key), (int, float)):
                total = int(payload[key])
                break
        meta = payload.get("meta") or payload.get("pagination") or {}
        if total is None and isinstance(meta, dict):
            for key in ("total", "totalCount", "totalFound"):
                if isinstance(meta.get(key), (int, float)):
                    total = int(meta[key])
                    break

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
) -> dict[str, Any]:
    fetch = fetch or fetch_json
    listings: list[dict[str, Any]] = []
    pages = 0
    truncated = False
    complete = True
    scheme = "none"
    current = url
    offset = 0
    page = 1
    if current:
        q = dict(parse_qsl(urlsplit(current).query, keep_blank_values=True))
        if q.get("page", "").isdigit():
            page = int(q["page"])
        if q.get("offset", "").isdigit():
            offset = int(q["offset"])
    seen_urls: set[str] = set()
    seen_fps: set[str] = set()
    fetch_log: list[dict[str, Any]] = []

    while True:
        if pages >= max_pages:
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
        if page_listings and not new_listings:
            # Whole page was already seen — the "next" URL added nothing.
            complete = True
            break
        listings.extend(new_listings)
        pages += 1
        last_count = len(page_listings)
        if len(listings) >= max_listings:
            listings = listings[:max_listings]
            truncated = True
            complete = False
            break
        if not current:
            complete = True
            break
        next_url, found = detect_next(
            current, payload, page=page, offset=offset,
            last_count=last_count, page_size=page_size,
            fetched=len(listings), scheme=scheme,
        )
        if found != "none":
            scheme = found
        if not next_url or next_url == current or last_count == 0:
            complete = True
            break
        if found == "offset":
            offset += last_count
        if found == "page":
            page += 1
        current = next_url
        payload = None

    return {
        "listings": listings,
        "sourceUrl": source_url,
        "pagination": {
            "pages": pages,
            "complete": complete and not truncated,
            "truncated": truncated,
            "scheme": scheme,
            "maxPages": max_pages,
            # Additive, machine-readable crawl state (#34): a blocked board
            # captured no listings — it is not a board with no openings.
            "status": "blocked" if not listings else ("truncated" if truncated else "complete"),
        },
        "fetchLog": fetch_log,
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
