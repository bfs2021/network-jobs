"""Score staged listings against preferences.json."""

from __future__ import annotations

from collections import Counter
from typing import Any
import re

from .classify import CATEGORY_AFFINITY, classify_job
from .embeddings import (
    RESUME_SEMANTIC_FLOOR,
    RESUME_SEMANTIC_MAX,
    cosine,
    semantic_bonus,
)
from .fingerprint import fingerprint
from .locations import city_matches, location_buckets_for
from .text import former_employer_match, tokenize


def name_tokens(profile: dict[str, Any] | None) -> set[str]:
    """Tokens from the user's name, so resume keywords don't echo "ada lovelace"."""
    return set(tokenize(str((profile or {}).get("name") or "")))


#: Generic resume filler that pollutes keyword extraction without describing the
#: actual work. Kept deliberately small; STOPWORDS covers articles/prepositions.
RESUME_NOISE = frozenset({
    "new", "novel", "various", "multiple", "many", "much", "several",
    "work", "worked", "working", "works", "job", "role", "team", "teams",
    "use", "used", "using", "usage", "via", "across", "within",
    "including", "include", "etc", "highly", "strong", "proven",
    "track", "record", "helped", "helping", "led", "leading",
    "drive", "driving", "built", "building", "also", "well",
    "day", "month", "year", "years", "end",
})


def load_resume_keywords(
    text: str,
    limit: int = 40,
    exclude: set[str] | None = None,
) -> list[str]:
    from .text import STOPWORDS
    import re

    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9+\-/#]{2,}", text or "")]
    drop = set(STOPWORDS) | set(RESUME_NOISE)
    if exclude:
        drop |= {w.lower() for w in exclude}
    words = [w for w in words if w not in drop]
    return [w for w, _ in Counter(words).most_common(limit)]


def _work_modes(prefs: dict[str, Any]) -> set[str]:
    return {str(m).lower() for m in (prefs.get("workModes") or [])}


def location_matches_prefs(job: dict[str, Any], prefs: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    modes = _work_modes(prefs)
    buckets = set(location_buckets_for(job))
    pref_buckets = {str(b).lower() for b in (prefs.get("locationBuckets") or [])}
    onsite_places = [str(x) for x in (prefs.get("onsiteLocations") or [])]
    loc_places = [str(x) for x in (prefs.get("locations") or [])]

    remote_ok = (not modes or "remote" in modes) and "remote" in buckets
    if remote_ok:
        reasons.append("remote")

    onsite_ok = False
    if modes & {"hybrid", "onsite"} or (not modes and pref_buckets - {"remote"}):
        if onsite_places and city_matches(job, onsite_places):
            onsite_ok = True
            reasons.append("onsiteLocations")
        elif not onsite_places and city_matches(job, loc_places):
            onsite_ok = True
            reasons.append("locations")
        elif pref_buckets & (buckets - {"remote"}):
            onsite_ok = True
            reasons.append("locationBucket")

    if not modes and not pref_buckets and not onsite_places and not loc_places:
        return True, ["no-location-prefs"]

    if remote_ok or onsite_ok:
        return True, reasons

    # Query-less default: if they only listed remote, do not leak global onsite.
    if modes == {"remote"}:
        return False, []
    if pref_buckets and buckets & pref_buckets:
        return True, ["locationBucket"]
    return False, []


def _keyword_hit(text: str, phrases: list[str] | None) -> list[str]:
    blob = (text or "").lower()
    hits = []
    for phrase in phrases or []:
        p = phrase.lower().strip()
        if p and p in blob:
            hits.append(phrase)
    return hits


# Character window for mustHave token locality, keyed by token count.
# A longer phrase needs more room for filler words between its tokens.
MUSTHAVE_WINDOW = {1: 40, 2: 80, 3: 120, 4: 160, 5: 200}


def _tokens_local(blob_lower: str, need: list[str], window: int) -> bool:
    """True when every token appears within `window` characters of the others.

    A whole-document token *set* is far too loose once a real JD body is
    in scope: "strong" (in "Not all strong candidates..."), "engineering"
    and "partnership" scattered across 10k chars satisfied an AND-set for
    "strong engineering partnership" and promoted two Anthropic roles on
    boilerplate alone (#24). Locality is what makes the phrase mean
    something.
    """
    if not need:
        return False
    positions: list[int] = []
    for token in need:
        hits: list[int] = []
        start = 0
        while True:
            idx = blob_lower.find(token, start)
            if idx < 0:
                break
            hits.append(idx)
            start = idx + len(token)
        if not hits:
            return False
        positions.append(hits)
    # Every token must have some occurrence inside one shared window.
    for anchor in positions[0]:
        if all(
            any(abs(pos - anchor) <= window for pos in options)
            for options in positions[1:]
        ):
            return True
    return False


def must_have_hits(job: dict[str, Any], must_haves: list[str] | None) -> list[str]:
    """Aspirational phrases need local AND-set matching, not substring (#21).

    Plain substring never fires on natural prose ("strong engineering
    partnership" matches 0 of 30 titles), so mustHaves looked load-bearing
    while doing nothing. Requiring every significant token present lets a
    phrase match a description that actually discusses it — but presence
    alone is not enough, so tokens must also co-occur locally (#24).
    Matches against title + department + company + description (when
    fetched).
    """
    if not must_haves:
        return []
    blob = " ".join([
        str(job.get("title") or ""), str(job.get("department") or ""),
        str(job.get("company") or ""), str(job.get("description") or ""),
    ])
    tokens = set(tokenize(blob.lower()))
    blob_lower = blob.lower()
    hits = []
    for phrase in must_haves:
        toks = [t for t in tokenize(str(phrase).lower()) if len(t) > 2]
        if not toks or not all(t in tokens for t in toks):
            continue
        # Tokens present — but only count it if they co-occur locally.
        width = MUSTHAVE_WINDOW.get(len(toks), 120)
        if _tokens_local(blob_lower, toks, width):
            hits.append(phrase)
    return hits


def category_confirmed(job: dict[str, Any]) -> bool:
    """True when the job's category is a confident classification (#40).

    Only an explicit non-high value (``"low"``) downgrades a row; a missing
    ``categoryConfidence`` counts as high. ``score_job`` always scores the
    output of ``classify_job``, which sets the field on every row, so this
    lenient default only matters to direct callers of this helper —
    hand-written fixtures and tests, not corpus rows.
    """
    return str(job.get("categoryConfidence") or "high").strip().lower() == "high"


def score_job(
    job: dict[str, Any],
    prefs: dict[str, Any] | None,
    resume_keywords: list[str] | None = None,
    company: str | None = None,
    resume_vector: list[float] | None = None,
    job_vector: list[float] | None = None,
) -> dict[str, Any]:
    """Score one job against prefs.

    resume_vector/job_vector activate the semantic resume signal: when both
    are present, cosine similarity *replaces* the keyword `resume` bonus
    (never stacks with it). Absent either vector, keyword scoring runs
    exactly as before.
    """
    prefs = prefs or {}
    classified = classify_job(job, company=company or job.get("company"))
    title = str(classified.get("title") or "")
    dept = str(classified.get("department") or "")
    blob = f"{title} {dept} {classified.get('company') or ''}"
    score = 0.0
    reasons: list[str] = []
    veto = False
    hard_fail = False

    deal = _keyword_hit(blob, prefs.get("dealBreakers"))
    if deal:
        veto = True
        reasons.append(f"dealBreaker:{deal[0]}")

    policy = str(prefs.get("formerEmployerPolicy") or "").lower()
    if policy == "exclude" and former_employer_match(
        str(classified.get("company") or ""), prefs.get("formerEmployers") or []
    ):
        veto = True
        reasons.append("formerEmployer")

    loc_ok, loc_reasons = location_matches_prefs(classified, prefs)
    location_required = bool(_work_modes(prefs) or prefs.get("locationBuckets") or prefs.get("onsiteLocations"))
    if loc_ok:
        score += 4
        reasons.extend(loc_reasons)
    elif location_required:
        score -= 3
        hard_fail = True
        reasons.append("location-mismatch")

    cats = [str(c).lower() for c in (prefs.get("categories") or [])]
    if cats:
        job_cat = str(classified.get("category") or "")
        # Both category branches require the same thing: a HIGH-confidence
        # classification. A weak title-token guess must not rank like a
        # confirmed match (#23) — whether it lands in a preferred category
        # (exact) or an adjacent one (affinity), it records
        # `category-unconfirmed` and earns no bonus (#40).
        confirmed = category_confirmed(classified)
        if job_cat in cats:
            if confirmed:
                score += 5
                reasons.append("category")
            else:
                reasons.append("category-unconfirmed")
        elif any(job_cat in CATEGORY_AFFINITY.get(c, ()) for c in cats):
            # Adjacent category: keep the job in the running with a smaller
            # bonus, but never hard-fail it the way an unrelated category does.
            if confirmed:
                score += 2
                reasons.append("category-affinity")
            else:
                reasons.append("category-unconfirmed")
        else:
            # Not preferred, not adjacent. The hard fail stays even when the
            # classification is low-confidence (#40 decision 3): "other"/low is
            # the fallback for unclassifiable titles, so exempting it would let
            # every unplaced job into category-filtered shortlists. An
            # unconfirmed guess must not *earn* a bonus; it may still fail to
            # match the stated preference.
            score -= 1
            hard_fail = True
            reasons.append("category-mismatch")

    sen_prefs = [str(s).lower() for s in (prefs.get("seniority") or [])]
    signals = [str(s).lower() for s in (classified.get("senioritySignals") or [])]
    seniority = str(classified.get("seniority") or "mid")
    staff_pref = "staff+" in sen_prefs or any("staff" in s for s in sen_prefs)
    intern_pref = "intern" in sen_prefs or any("intern" in s for s in sen_prefs)

    if "intern" in signals and not intern_pref and sen_prefs:
        hard_fail = True
        reasons.append("intern-mismatch")
    elif intern_pref and "intern" in signals:
        score += 4
        reasons.append("intern")
    elif "junior" in signals and sen_prefs and not intern_pref and not staff_pref:
        # Positive junior signal ("Associate", "Junior", "Entry-Level") is a
        # real mismatch for senior/staff seekers — same treatment as intern.
        hard_fail = True
        reasons.append("junior-mismatch")
    elif staff_pref:
        if "staff+" in signals:
            score += 4
            reasons.append("staff+")
        else:
            # Soft: a staff seeker may still want a strong senior role.
            score -= 2
            reasons.append("staff-mismatch")
    elif sen_prefs and seniority in sen_prefs:
        score += 3
        reasons.append("seniority")
    elif sen_prefs and "unmarked" in signals:
        # Level-ambiguous title ("Product Manager", "Software Engineer"):
        # unknown level must not score like a confirmed match (#23) — a
        # small explicit penalty, never an exclusion.
        score -= 1
        reasons.append("seniority-ambiguous")
    elif sen_prefs:
        score -= 2
        reasons.append("seniority-mismatch")

    track_prefs = [str(t).lower() for t in (prefs.get("track") or [])]
    track = str(classified.get("track") or "ic")
    if track_prefs and "either" not in track_prefs:
        if track in track_prefs:
            score += 3
            reasons.append("track")
        else:
            score -= 2
            hard_fail = True
            reasons.append("track-mismatch")

    # classified already carries description when fetched (dict copy).
    must = must_have_hits(classified, prefs.get("mustHaves"))
    if must:
        score += 2
        reasons.append("mustHave")
    elif prefs.get("mustHaves") and classified.get("description"):
        # Stated requirement, described role, no match: visible but not
        # penalized further. Title-only rows get no verdict either way —
        # penalizing undescribed rows for undescribed requirements would
        # punish having data (#23 three-valued shape).
        reasons.append("mustHave-unmet")

    target_roles = prefs.get("targetRoles") or []
    if target_roles:
        shape_tokens = set(tokenize(f"{title} {dept}"))
        for slug in target_roles:
            toks = [t for t in re.split(r"[-_\s]+", str(slug).lower())
                    if t and t not in {"of", "the", "and", "a", "for", "to"}]
            if toks and all(t in shape_tokens for t in toks):
                score += 3
                reasons.append("targetRole")
                break

    if resume_keywords:
        if resume_vector is not None and job_vector is not None:
            # Semantic resume signal replaces the keyword bonus entirely.
            bonus = semantic_bonus(
                cosine(resume_vector, job_vector),
                RESUME_SEMANTIC_FLOOR,
                RESUME_SEMANTIC_MAX,
            )
            if bonus > 0:
                score += bonus
                reasons.append("resume-semantic")
        else:
            tokens = set(tokenize(blob))
            hits = [k for k in resume_keywords if k.lower() in tokens or k.lower() in blob.lower()]
            if hits:
                score += min(4, len(hits))
                reasons.append("resume")

    salary_min = prefs.get("salaryMin")
    salary = classified.get("salary") or {}
    if isinstance(salary_min, (int, float)) and isinstance(salary, dict):
        lo = salary.get("min") or salary.get("max")
        if isinstance(lo, (int, float)):
            if lo >= salary_min:
                score += 1
                reasons.append("salary")
            else:
                score -= 2

    matched = (not veto) and (not hard_fail)
    # Empty prefs: treat everything as a match so discover still shortlists.
    empty_prefs = not prefs or not any(
        prefs.get(k) for k in (
            "categories", "workModes", "locationBuckets", "onsiteLocations",
            "seniority", "track", "mustHaves", "dealBreakers",
        )
    )
    if empty_prefs:
        matched = not veto
        if not reasons:
            reasons.append("no-prefs")

    out = dict(classified)
    out["matchScore"] = round(score, 2)
    out["matchReasons"] = reasons
    out["matched"] = matched
    out["veto"] = veto
    return out


def seniority_unconfirmed(job: dict[str, Any]) -> bool:
    """True when a job's level is unknown rather than determined (#23).

    Jobs scored without seniority prefs carry no markers and count as
    confirmed — there was nothing to confirm against.
    """
    reasons = job.get("matchReasons") or []
    if "seniority-ambiguous" not in reasons:
        return False
    return not any(r in reasons for r in ("seniority", "staff+", "intern"))


def match_sort_key(job: dict[str, Any]) -> tuple[float, int, str]:
    """Rank key: score desc, then confirmed seniority above ambiguous (#23)."""
    return (-float(job.get("matchScore") or 0),
            1 if seniority_unconfirmed(job) else 0,
            str(job.get("title") or ""))


def match_listings(
    listings: list[dict[str, Any]],
    prefs: dict[str, Any] | None,
    resume_keywords: list[str] | None = None,
    company: str | None = None,
    resume_vector: list[float] | None = None,
    job_vectors: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    job_vectors = job_vectors or {}

    def _job_vector(job: dict[str, Any]) -> list[float] | None:
        fp = str(job.get("fingerprint") or "") or fingerprint(
            job, company=company or job.get("company")
        )
        return job_vectors.get(fp)

    scored = [
        score_job(
            j,
            prefs,
            resume_keywords=resume_keywords,
            company=company,
            resume_vector=resume_vector,
            job_vector=_job_vector(j),
        )
        for j in listings
    ]
    matches = [j for j in scored if j.get("matched")]
    # At equal scores, confirmed seniority outranks ambiguous (#23).
    matches.sort(key=match_sort_key)
    depts = Counter(str(j.get("department") or "(none)") for j in listings)
    # Composition of the shortlist itself (#8): a bare count reads as
    # success even when 90% of matches are one unintended family.
    match_depts = Counter(str(j.get("department") or "(none)") for j in matches)
    match_cats = Counter(str(j.get("category") or "(none)") for j in matches)
    reason_counts: Counter[str] = Counter()
    for j in matches:
        for reason in j.get("matchReasons") or []:
            reason_counts[str(reason)] += 1
    warnings: list[str] = []
    if isinstance(prefs, dict):
        if not prefs.get("dealBreakers") and not prefs.get("dealBreakersConfirmed"):
            warnings.append(
                "dealBreakers is empty and was never confirmed in the interview "
                "(dealBreakersConfirmed!=true) — shortlist may be wider than intended"
            )
    return {
        "nListings": len(listings),
        "nMatches": len(matches),
        "showing": f"{len(matches)} of {len(listings)} match prefs",
        "departments": dict(depts.most_common()),
        "matchDepartments": dict(match_depts.most_common()),
        "matchCategories": dict(match_cats.most_common()),
        "reasonCounts": dict(reason_counts.most_common()),
        "ambiguousSeniority": sum(1 for j in matches if seniority_unconfirmed(j)),
        "warnings": warnings,
        "jobs": matches,
        "allScored": scored,
    }
