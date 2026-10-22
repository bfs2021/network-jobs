"""Cluster a match-prefs shortlist into job families for empirical vetoing.

#9: deal-breaker leaks are emergent — the user cannot pre-list families they
haven't seen. Grouping matches by (category, title stem) lets them confirm or
reject a family of 20 at once instead of patching substrings one by one.
"""

from __future__ import annotations

import re
from collections import Counter
from statistics import median
from typing import Any

# Title tokens that describe level/shape, not family. Stripped so seniority
# variants share a stem.
SENIORITY_WORDS = frozenset({
    "senior", "staff", "principal", "lead", "director", "vp", "head",
    "manager", "management", "junior", "mid", "intern", "internship",
    "associate", "assistant", "sr", "jr", "i", "ii", "iii", "iv", "v",
    "hiring",
})

# Common abbreviations expanded before stemming so "Staff PM" lands with
# "Senior Product Manager" instead of forming its own family.
ABBREV = {
    "pm": "product",
    "eng": "engineering",
    "swe": "engineering",
    "sre": "reliability",
    "tpm": "program",
    "em": "engineering",
}

_WS_RE = re.compile(r"[^a-z0-9]+")


def title_stem(title: str) -> str:
    """Reduce a title to a `head/domain` family stem (#13).

    The head alone ("product") collapses whole boards into one family, so
    retain the first qualifier as the domain: "Staff PM, Payments" and
    "Senior Product Manager" become different families
    (`product/payments` vs `product`), while seniority/shape variants of
    the same team still merge. Single-qualifier titles keep a bare head.
    Coarser than a taxonomy on purpose — vetoes stay explicit phrases.
    """
    words = [
        ABBREV.get(w, w)
        for w in _WS_RE.sub(" ", str(title or "").lower()).split()
        if w not in SENIORITY_WORDS and len(w) > 1
    ]
    if not words:
        return str(title or "").lower().strip()
    head = words[0]
    domain = next((w for w in words[1:] if w != head), "")
    return f"{head}/{domain}" if domain else head


def _family_of(job: dict[str, Any]) -> tuple[str, str]:
    category = str(job.get("category") or job.get("department") or "(none)")
    return (category.strip().lower() or "(none)", title_stem(str(job.get("title") or "")))


def cluster_matches(
    matches: list[dict[str, Any]],
    min_cluster: int = 1,
) -> list[dict[str, Any]]:
    """Group matched jobs into families, biggest first.

    Each family: category, stem, label, suggestedVeto, count, median score,
    locations observed, and up to 3 sample titles. Families below min_cluster
    members fold into a single "(smaller families)" bucket so the review stays
    short; the bucket carries the same keys (with `suggestedVeto=None`) so
    `--json` consumers and printers see one consistent shape (#30).
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for job in matches:
        groups.setdefault(_family_of(job), []).append(job)

    fams: list[dict[str, Any]] = []
    small: list[dict[str, Any]] = []
    for (category, stem), members in groups.items():
        if len(members) < max(1, min_cluster):
            small.extend(members)
            continue
        scores = sorted(float(m.get("matchScore") or 0) for m in members)
        locs: list[str] = []
        for m in members:
            loc = str(m.get("location") or "").strip()
            if loc and loc not in locs:
                locs.append(loc)
        titles: list[str] = []
        for m in members:
            t = str(m.get("title") or "").strip()
            if t and t not in titles:
                titles.append(t)
        head, _, domain = stem.partition("/")
        fams.append({
            "category": category,
            "stem": stem,
            "label": stem if head == category else f"{category} / {stem}",
            "suggestedVeto": domain or head,
            "count": len(members),
            "medianScore": round(median(scores), 2) if scores else 0,
            "locations": locs[:4],
            "sampleTitles": titles[:3],
        })
    fams.sort(key=lambda f: (-f["count"], -f["medianScore"], f["stem"]))
    if small:
        scores = sorted(float(m.get("matchScore") or 0) for m in small)
        fams.append({
            "category": "(mixed)",
            "stem": "(smaller families)",
            "label": "(smaller families)",
            "suggestedVeto": None,
            "count": len(small),
            "medianScore": round(median(scores), 2) if scores else 0,
            "locations": [],
            "sampleTitles": [],
        })
    return fams


def explain_leaks(scored: list[dict[str, Any]]) -> dict[str, Any]:
    """Group non-matched jobs by the rule that caught them.

    Vetoes bucket by deal-breaker phrase (exact counts per phrase, so the
    user sees which veto does the work); other hard fails bucket by their
    mismatch reason. Matched jobs are ignored.
    """
    veto_phrases: Counter[str] = Counter()
    hard_fails: Counter[str] = Counter()
    n_vetoed = 0
    n_failed = 0
    for job in scored:
        if job.get("matched"):
            continue
        reasons = [str(r) for r in job.get("matchReasons") or []]
        vetoed = [r.split(":", 1)[1] for r in reasons if r.startswith("dealBreaker:")]
        if vetoed or job.get("veto"):
            n_vetoed += 1
            for phrase in vetoed or ["(vetoed)"]:
                veto_phrases[phrase] += 1
            continue
        n_failed += 1
        tagged = False
        for reason in reasons:
            if reason.endswith("-mismatch"):
                hard_fails[reason] += 1
                tagged = True
        if not tagged:
            hard_fails["(other hard-fail)"] += 1
    return {
        "vetoed": n_vetoed,
        "vetoPhrases": dict(veto_phrases.most_common()),
        "hardFailed": n_failed,
        "hardFailReasons": dict(hard_fails.most_common()),
    }
