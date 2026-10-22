"""Stable job identity: ATS id when present, else company+title+location."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .locations import parse_locations
from .text import collapse_ws, normalize_company, normalize_title

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)
# Our corpus ids look like "stripe-senior-backend-engineer-ab12"
SLUG_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){2,}$")

#: Placeholder "ids" some ATS payloads emit when they have no real id.
#: Two distinct jobs sharing one must NOT fingerprint as the same posting,
#: so these are treated as absent and fingerprinting falls back to
#: company+title+location.
PLACEHOLDER_ATS_IDS = frozenset({
    "null", "none", "undefined", "n/a", "na", "unknown", "tbd", "todo",
    "-", "--", "---", "?", "nil", "missing", "not set", "not applicable",
    # Prose label-leak placeholders: boards that render a label where an id
    # belongs (e.g. Stripe leaves requisition_id as "See Opening ID" on every
    # posting). Matched case-insensitively after whitespace collapse.
    "see opening id", "see job id", "see details", "see description",
    "view job", "view jobs", "view posting", "view details",
    "job details", "job description", "opening id", "job id",
    "position id", "req id", "requisition id", "reference id", "ref id",
    "apply now", "apply here", "click to apply", "learn more", "more info",
})


def location_key(job: dict[str, Any]) -> str:
    parsed = parse_locations(job)
    if parsed:
        bits = []
        for loc in parsed:
            if loc.get("remote"):
                bits.append("remote")
            city = collapse_ws(str(loc.get("city") or "")).lower()
            region = collapse_ws(str(loc.get("region") or "")).lower()
            raw = collapse_ws(str(loc.get("raw") or "")).lower()
            bit = city or region or raw or (loc.get("bucket") or "")
            if bit and bit not in bits:
                bits.append(bit)
        if bits:
            return "|".join(sorted(bits))
    return collapse_ws(str(job.get("location") or "")).lower()


def plain_id(value: Any) -> str | None:
    """A board's plain `id`, accepted only when it looks like a real posting id.

    Applied wherever a raw `id` becomes identity — including
    `normalize_listing()`, which writes `externalId` that `ats_id()` later
    trusts outright. A one-character or slug id must not outrank a good
    requisition id, or short ids collide across companies in a fingerprint
    that carries no company (#49).
    """
    if value is None:
        return None
    text = collapse_ws(str(value))
    if not text or text.lower().startswith("http"):
        return None
    if text.lower() in PLACEHOLDER_ATS_IDS:
        return None
    if text.isdigit() and len(text) >= 3:
        return text
    if UUID_RE.match(text):
        return text
    # Skip our own corpus slugs and fingerprints
    if text.startswith("id:") or text.startswith("fp:"):
        return None
    if SLUG_ID_RE.match(text) and any(c.isalpha() for c in text) and text.count("-") >= 2:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", text):
        return text
    return None


def _declared_id(job: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """First non-placeholder, non-URL id under `keys`."""
    for key in keys:
        value = job.get(key)
        if value is None:
            continue
        text = collapse_ws(str(value))
        if text.lower() in PLACEHOLDER_ATS_IDS:
            continue
        if text and not text.lower().startswith("http"):
            return text
    return None


def ats_id(job: dict[str, Any]) -> str | None:
    text = _declared_id(job, ("externalId", "atsId", "jobId", "gh_jid"))
    if text:
        return text
    # A per-posting `id` beats a requisition id: Greenhouse reuses one
    # `requisition_id` across several distinct postings (per location/team),
    # so keying identity on it collapses real jobs (#48). The shape guard
    # still applies, so a junk `id` falls through (#49).
    text = plain_id(job.get("id"))
    if text:
        return text
    return _declared_id(job, ("requisitionId", "requisition_id"))


def fingerprint(job: dict[str, Any], company: str | None = None) -> str:
    existing = job.get("fingerprint")
    if isinstance(existing, str) and existing.startswith(("id:", "fp:")):
        return existing
    ext = ats_id(job)
    if ext:
        # Use ATS id as the primary key. Company is stored separately on the
        # job object and does not need to be in the fingerprint key. This
        # prevents silent duplication when --company is passed inconsistently
        # (issue #25).
        return f"id:{ext}"
    # Fallback: no ATS id. Use title + location + company from job data.
    # The company parameter is intentionally ignored here to keep the key
    # stable regardless of CLI flags.
    company_n = normalize_company(str(job.get("company") or ""))
    title_n = normalize_title(str(job.get("title") or ""))
    loc_n = location_key(job)
    basis = f"{company_n}|{title_n}|{loc_n}"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
    return f"fp:{digest}"


def listing_set_hash(jobs: list[dict[str, Any]], company: str | None = None) -> str:
    fps = sorted({fingerprint(j, company=company) for j in jobs})
    blob = "\n".join(fps).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]
