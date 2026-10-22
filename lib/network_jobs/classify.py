"""Deterministic title/department → category, track, and seniority.

LLM is only for leftover ambiguous titles (low confidence).
"""

from __future__ import annotations

import re
from typing import Any

from .fingerprint import fingerprint
from .locations import location_buckets_for, parse_locations, primary_location_bucket
from .text import collapse_ws, slugify, tokenize

CATEGORIES = (
    "engineering", "product", "design", "data", "ai-ml", "sales", "marketing",
    "customer-success", "operations", "finance", "people", "legal",
    "it-security", "retail", "other",
)

# Adjacent categories that are close enough to count as a soft match when a
# seeker prefers one category but the job is classified as another. Used by
# prefs.score_job: affinity adds a small bonus without the exact-category
# points, and — unlike an unrelated category — never hard-fails.
CATEGORY_AFFINITY: dict[str, frozenset[str]] = {
    "ai-ml": frozenset({"engineering", "data"}),
    "engineering": frozenset({"ai-ml", "data", "it-security", "product"}),
    "data": frozenset({"ai-ml", "engineering", "product"}),
    "product": frozenset({"engineering", "data", "design", "operations"}),
    "design": frozenset({"product", "engineering", "marketing"}),
    "sales": frozenset({"customer-success", "marketing"}),
    "marketing": frozenset({"sales", "design", "product"}),
    "customer-success": frozenset({"sales", "operations"}),
    "operations": frozenset({"product", "customer-success", "finance"}),
    "finance": frozenset({"operations", "legal"}),
    "people": frozenset({"operations"}),
    "legal": frozenset({"finance", "operations"}),
    "it-security": frozenset({"engineering", "operations"}),
    "retail": frozenset({"sales", "customer-success"}),
    "other": frozenset(),
}

# Longer / more specific phrases first.
TITLE_CATEGORY_PATTERNS: list[tuple[str, str]] = [
    (r"\b(machine learning|ml engineer|ml ops|mlops|deep learning|artificial intelligence|\bai[ -]?ml\b|llm |research scientist)\b", "ai-ml"),
    (r"\b(data scientist|data engineer|analytics engineer|bi engineer|business intelligence|data analyst|analytics)\b", "data"),
    (r"\b(product design(?:er)?|ux designer|ui designer|brand designer|graphic designer|design system|user experience|user interface)\b", "design"),
    # A program manager delivers another team's program, so the domain word
    # decides the category, not the "manager" suffix: a compliance, legal,
    # regulatory or privacy program manager is legal (#57 — it used to claim
    # product's confirmed +5 and outrank actual PMs on a "product manager"
    # search). Only product and technical programs are product roles below.
    (r"\b(compliance|legal|regulatory|privacy)\s+(?:program|project)\s+managers?\b", "legal"),
    (r"\b(security|infosec)\s+(?:program|project)\s+managers?\b", "it-security"),
    (r"\b(people|talent|hr)\s+(?:program|project)\s+managers?\b", "people"),
    (r"\boperations\s+(?:program|project)\s+managers?\b", "operations"),
    # `technical program manager` is listed explicitly: it used to match only
    # through the generic `program manager` phrase removed in #57. A bare
    # "Program Manager" now carries no domain and falls through to
    # `other`/low, so a weak guess never earns a category bonus (#40).
    (r"\b(product manager|group pm|staff pm|principal pm|product lead|product owner|technical product manager|technical program manager|\btpm\b|\bpm\b)\b", "product"),
    (r"\b(account executive|\bae\b|\bsdr\b|\bbdr\b|sales engineer|solutions engineer|business development|account director|sales manager)\b", "sales"),
    (r"\b(product marketing|demand gen|growth marketing|content marketer|communications|copywriter|\bpmm\b|\bpr\b)\b", "marketing"),
    (r"\b(customer success|customer support|solutions architect|implementation manager|account manager|support engineer|cx manager)\b", "customer-success"),
    (r"\b(software|backend|front[- ]?end|full[- ]?stack|sre|devops|site reliability|platform engineer|infrastructure engineer|qa engineer|quality engineer|android|ios |mobile engineer|security engineer|firmware|embedded)\b", "engineering"),
    (r"\b(engineering manager|\bem\b|director of engineering|vp of engineering|cto|staff engineer|principal engineer|distinguished engineer)\b", "engineering"),
    (r"\b(recruiter|recruiting|talent acquisition|people ops|people partner|human resources|\bhr\b|l&d|learning and development)\b", "people"),
    (r"\b(counsel|attorney|lawyer|compliance|legal ops|contracts manager|privacy counsel)\b", "legal"),
    (r"\b(fp&a|accountant|controller|treasur|auditor|finance manager|financial analyst)\b", "finance"),
    (r"\b(sysadmin|systems administrator|it support|infosec|information security|soc analyst|network admin)\b", "it-security"),
    (r"\b(store manager|retail |shop associate|field sales)\b", "retail"),
    (r"\b(biz ops|business operations|operations manager|supply chain|logistics|chief of staff|strategy & operations)\b", "operations"),
    (r"\b(designer)\b", "design"),
    (r"\b(engineer|developer|programmer|architect)\b", "engineering"),
    (r"\b(marketer|marketing)\b", "marketing"),
    (r"\b(salesperson|sales )\b", "sales"),
]

# A title may name the team/domain the role sits in after a dash, pipe or
# comma: "Incident Response Manager - Product & Engineering". That qualifier
# is not the role, so category matching for bare words looks at the head
# segment only (#31).
TITLE_QUALIFIER_RE = re.compile(r"\s+[-\u2013\u2014]\s*|\s*[-\u2013\u2014]\s+|\s*[|;]\s*|,\s+")

# "product" names a product role only when it is the role's function: it
# modifies a role noun ("Product Manager", "Product Strategy") or a leader
# owns the function ("Head of Product", "VP, Product"). A title that merely
# contains the word ("New Product Introduction ... Sourcing Manager",
# "Incident Response Manager - Product & Engineering") is not a product role —
# there the word describes the product/org, not the job.
PRODUCT_ROLE_RE = re.compile(
    r"\bproduct\s+(manager|management|lead|owner|ops|operations|strategy|"
    r"program|portfolio|planning|partnerships?)\b"
    r"|\bchief product officer\b",
    re.I,
)
# What may follow "product" when a leader owns the function: either nothing
# ("Head of Product", "VP, Product", "Director, Product") or a noun that still
# names the product function itself ("Director of Product Management",
# "VP, Product Strategy"). Any other word after "product" names a different
# function that already owns a category — "Head of Product Design" (design),
# "Head of Product Security" (it-security), "Head of Product Finance"
# (finance) — so the leader pattern must not claim it. A whitelist rather
# than a lookahead blocklist: unknown words fail closed to the weak-token /
# department pass (which routes the function word to its own category) and
# `needsLlm`, instead of silently earning product's +5. This is #31's bug
# class one level deeper (#37 review).
PRODUCT_FUNCTION_TAIL = (
    r"(?=$|[\s,]+(?:manager|management|lead|owner|ops|operations|strategy|"
    r"program|portfolio|planning|partnerships?)\b)"
)
PRODUCT_LEADER_RE = re.compile(
    r"\b(head|director|vp|vice president|svp|evp|avp|chief|gm|general manager)"
    r"\b(?:\s+of)?[\s,]+product\b" + PRODUCT_FUNCTION_TAIL,
    re.I,
)
# Level-only titles ("Manager", "Senior Director", "VP") carry no domain, so a
# comma qualifier supplies the function: "Manager, Product Program
# Management" reads as "Manager of Product Program Management".
BARE_ROLE_RE = re.compile(
    r"^(?:(?:senior|sr\.?|staff|principal|lead|group|chief|associate|junior|"
    r"jr\.?|executive|deputy|assistant|interim|acting|vice president|vp|svp|"
    r"evp|avp|head|director|manager|gm|general manager)\s*)+$",
    re.I,
)
BARE_LEADER_RE = re.compile(
    r"^(?:senior\s+|staff\s+|principal\s+|group\s+)?(?:vice president|vp|svp|"
    r"evp|avp|head|director|chief|gm|general manager|executive|deputy|"
    r"interim|acting)\s*$",
    re.I,
)
PRODUCT_QUALIFIER_RE = re.compile(r"^product\b" + PRODUCT_FUNCTION_TAIL, re.I)


DEPT_CATEGORY = [
    (r"\b(machine learning|artificial intelligence|\bai\b|\bml\b)\b", "ai-ml"),
    (r"\b(data|analytics|business intelligence|\bbi\b)\b", "data"),
    (r"\b(design|ux|ui|brand)\b", "design"),
    (r"\b(product)\b", "product"),
    (r"\b(sales|revenue|go.to.market|gtm)\b", "sales"),
    (r"\b(marketing|growth|communications|brand)\b", "marketing"),
    (r"\b(customer success|support|cx|services)\b", "customer-success"),
    (r"\b(engineering|infrastructure|platform|developer|r&d|research)\b", "engineering"),
    (r"\b(people|talent|recruiting|human resources|\bhr\b)\b", "people"),
    (r"\b(legal|compliance|privacy)\b", "legal"),
    (r"\b(finance|accounting|treasury)\b", "finance"),
    (r"\b(security|information technology|\bit\b|infosec)\b", "it-security"),
    (r"\b(retail|stores)\b", "retail"),
    (r"\b(operations|ops|logistics|supply)\b", "operations"),
]

PEOPLE_MANAGER_RE = re.compile(
    r"\b(engineering manager|\bem\b|director|vp|vice president|head of|"
    r"supervisor|chief |people manager|hiring manager|group manager|gm of)\b",
    re.I,
)
IC_MANAGER_RE = re.compile(
    r"\b(account|program|project|product|marketing|community|office|case|"
    r"success|implementation|engagement|partner|customer|store)\s+managers?\b",
    re.I,
)
GENERIC_MANAGER_RE = re.compile(r"\bmanagers?\b", re.I)
IC_RE = re.compile(
    r"\b(engineer|developer|designer|analyst|scientist|specialist|ic\b|"
    r"individual contributor|architect|consultant)\b",
    re.I,
)
PLAYER_COACH_RE = re.compile(r"\b(tech lead|team lead|lead engineer|player.?coach|manager.?ic)\b", re.I)
AMBIGUOUS_LEAD_RE = re.compile(r"\b(lead|head)\b", re.I)
INTERN_RE = re.compile(r"\b(intern|internship|interns|co-?op|apprentice)\b", re.I)
STAFF_PLUS_RE = re.compile(
    r"\b(staff|principal|distinguished|fellow|architect)\b",
    re.I,
)
SENIOR_RE = re.compile(
    r"\b(senior|staff|principal|distinguished|fellow|lead|director|vp|"
    r"vice president|head of|chief )\b",
    re.I,
)
JUNIOR_RE = re.compile(r"\b(junior|associate|entry[-\s]?level)\b", re.I)


def title_head(title: str) -> tuple[str, list[str]]:
    """Split a title into its head segment and trailing qualifiers."""
    parts = TITLE_QUALIFIER_RE.split(collapse_ws(title or ""))
    segments = [s.strip() for s in parts if s.strip()]
    if not segments:
        return "", []
    return segments[0], segments[1:]


def is_product_function_title(title: str) -> bool:
    """True only when "product" names the role's function, not a qualifier."""
    head, qualifiers = title_head(title)
    if not head:
        return False
    if PRODUCT_ROLE_RE.search(head) or PRODUCT_LEADER_RE.search(head):
        return True
    if not qualifiers:
        return False
    # "VP, Product" / "Manager, Product Program Management": the qualifier is
    # the function because the head is only a level + generic role word.
    if BARE_ROLE_RE.match(head):
        combined = " ".join([head, *qualifiers])
        if PRODUCT_ROLE_RE.search(combined) or PRODUCT_LEADER_RE.search(combined):
            return True
    if BARE_LEADER_RE.match(head):
        return bool(PRODUCT_QUALIFIER_RE.search(qualifiers[0]))
    return False


def _match_table(text: str, table: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    blob = collapse_ws(text).lower()
    if not blob:
        return None, None
    for pattern, category in table:
        if re.search(pattern, blob, re.I):
            return category, pattern
    return None, None


def classify_track(title: str, department: str = "") -> tuple[str, str, bool]:
    blob = f"{title} {department}"
    if PLAYER_COACH_RE.search(blob):
        return "ic", "low", True
    if IC_MANAGER_RE.search(blob):
        return "ic", "high", False
    if PEOPLE_MANAGER_RE.search(blob):
        # Explicit manager phrases ("Engineering Manager", "Director of
        # Engineering") win over IC tokens like "engineer".
        return "manager", "high", False
    manager = bool(GENERIC_MANAGER_RE.search(blob))
    ic = bool(IC_RE.search(blob))
    if manager and ic:
        return "manager", "low", True
    if manager:
        return "manager", "low", True
    if ic:
        return "ic", "high", False
    if AMBIGUOUS_LEAD_RE.search(blob):
        return "ic", "low", True
    return "ic", "low", True


def classify_seniority(title: str) -> tuple[str, list[str]]:
    signals: list[str] = []
    if INTERN_RE.search(title or ""):
        signals.append("intern")
        return "mid", signals
    if STAFF_PLUS_RE.search(title or ""):
        signals.append("staff+")
        return "senior", signals
    if SENIOR_RE.search(title or ""):
        return "senior", signals
    if JUNIOR_RE.search(title or ""):
        signals.append("junior")
        return "mid", signals
    # No seniority markers at all ("Product Manager", "Software Engineer"):
    # level-ambiguous, not "mid". Scoring treats unmarked titles as neutral
    # so plain-titled roles are never excluded on seniority alone.
    signals.append("unmarked")
    return "mid", signals


def classify_category(title: str, department: str = "") -> tuple[str, str, bool]:
    cat, _ = _match_table(title, TITLE_CATEGORY_PATTERNS)
    if cat:
        return cat, "high", False
    # "product" is only a title signal when it is the role's function (#31);
    # a bare token ("New Product Introduction ...") no longer decides.
    if is_product_function_title(title):
        return "product", "high", False
    cat, _ = _match_table(department, DEPT_CATEGORY)
    if cat:
        return cat, "high", False
    # Weak title tokens. Only the head segment counts: a trailing qualifier
    # ("... - Product & Engineering") names the team, not the job.
    head, _ = title_head(title)
    tokens = set(tokenize(f"{head} {department}"))
    weak = {
        "design": "design",
        "data": "data",
        "sales": "sales",
        "marketing": "marketing",
        "finance": "finance",
        "legal": "legal",
        "retail": "retail",
        "security": "it-security",
        "engineer": "engineering",
        "engineering": "engineering",
    }
    for token, category in weak.items():
        if token in tokens:
            return category, "low", True
    return "other", "low", True


def classify_job(job: dict[str, Any], company: str | None = None) -> dict[str, Any]:
    title = str(job.get("title") or "")
    department = str(job.get("department") or "")
    category, cat_conf, cat_amb = classify_category(title, department)
    track, track_conf, track_amb = classify_track(title, department)
    seniority, signals = classify_seniority(title)
    if (
        track == "manager"
        and track_conf == "high"
        and seniority == "mid"
        and "intern" not in signals
    ):
        # High-confidence people-manager titles ("Engineering Manager",
        # "Director of Engineering") are senior-level roles. Generic
        # "X Manager" titles (track confidence low, e.g. "Escalations
        # Manager", "Sourcing Manager") are not promoted — "Manager" there
        # usually names a domain, not a team.
        # IC-flavored managers (Account/Program/Project Manager) already map to
        # the ic track above, so this only promotes true people managers.
        seniority, signals = "senior", [*signals, "manager"]
    buckets = location_buckets_for(job)
    parsed = parse_locations(job)
    out = dict(job)
    out["company"] = company or job.get("company") or ""
    out["category"] = category
    out["categoryConfidence"] = cat_conf
    out["track"] = track
    out["trackConfidence"] = track_conf
    out["seniority"] = seniority
    out["senioritySignals"] = signals
    out["locations"] = parsed
    out["locationBuckets"] = buckets
    out["locationBucket"] = primary_location_bucket(job)
    out["needsLlm"] = bool(cat_amb or track_amb)
    out["fingerprint"] = fingerprint(out, company=out.get("company"))
    company_slug = slugify(str(out.get("company") or "company"))
    out.setdefault("id", job_id_for(out, company_slug, out["fingerprint"]))
    if parsed and not out.get("location"):
        out["location"] = parsed[0].get("raw") or ""
    return out


def classify_listings(jobs: list[dict[str, Any]], company: str | None = None) -> dict[str, Any]:
    classified = [classify_job(j, company=company) for j in jobs]
    ambiguous = [j for j in classified if j.get("needsLlm")]
    return {
        "jobs": classified,
        "n": len(classified),
        "ambiguous": len(ambiguous),
        "needsLlm": [{"title": j.get("title"), "department": j.get("department")} for j in ambiguous],
    }


def job_id_for(job: dict[str, Any], company_slug: str, fingerprint_value: str) -> str:
    title_slug = slugify(str(job.get("title") or "role"))
    tail = fingerprint_value.split(":")[-1][:10]
    return f"{company_slug}-{title_slug}-{tail}"
