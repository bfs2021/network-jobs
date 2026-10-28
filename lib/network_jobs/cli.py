"""CLI entry points used by skill helpers. Quiet JSON on stdout; -v for detail."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .classify import classify_listings
from .fingerprint import fingerprint
from .corpus import rebuild
from .inventory import careers_url_from_listings, upsert_inventory, write_summary_json
from .matches import MatchesFormatError, load_matches, normalize_matches, write_matches
from .pagination import DEFAULT_MAX_LISTINGS, DEFAULT_MAX_PAGES, blocked_result, paginate, write_pagination
from .paths import data_home
from .prefs import load_resume_keywords, match_listings, name_tokens
from .rank import DEFAULT_K, posting_age_days, rank_corpus
from .review import cluster_matches, explain_leaks


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text())


def _dump(payload: Any, verbose: bool) -> None:
    if verbose:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False))


def _match_semantic(
    data_dir: str | Path | None, resume_text: str
) -> tuple[list[float] | None, dict[str, list[float]]]:
    """(resume_vector, job_vectors) for the triage path.

    Embeds the resume once per run; job vectors come from the corpus cache
    (triage listings that were previously merged resolve by fingerprint).
    (None, {}) when no provider is configured.
    """
    from .embeddings import get_provider, load_embeddings_cache

    provider = get_provider()
    if provider is None or not (resume_text or "").strip():
        return None, {}
    vecs = provider.embed([resume_text.strip()])
    resume_vector = vecs[0] if vecs else None
    data = data_home(data_dir)
    cache = load_embeddings_cache(data / "corpus")
    vectors = cache.get("vectors")
    return resume_vector, vectors if isinstance(vectors, dict) else {}


def _do_match_prefs(
    triage_dir: Path,
    listings: list[dict[str, Any]],
    prefs: dict[str, Any],
    resume_text: str,
    exclude: set[str],
    company: str,
    verbose: bool,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    resume_vector, job_vectors = _match_semantic(data_dir, resume_text)
    result = match_listings(
        listings,
        prefs,
        resume_keywords=load_resume_keywords(resume_text, exclude=exclude) if resume_text else None,
        company=company or None,
        resume_vector=resume_vector,
        job_vectors=job_vectors,
    )
    out_dir = triage_dir / "index"
    out_dir.mkdir(parents=True, exist_ok=True)
    matches_path = out_dir / "matches.json"
    payload = {
        "nListings": result["nListings"],
        "nMatches": result["nMatches"],
        "showing": result["showing"],
        "departments": result["departments"],
        "matchDepartments": result["matchDepartments"],
        "matchCategories": result["matchCategories"],
        "reasonCounts": result["reasonCounts"],
        "ambiguousSeniority": result["ambiguousSeniority"],
        "warnings": result["warnings"],
        "ingestDefault": "matches",
        "jobs": result["jobs"],
    }
    write_matches(matches_path, payload)
    summary = {
        "company": company,
        "nListings": result["nListings"],
        "nMatches": result["nMatches"],
        "departments": result["departments"],
        "ingestDefault": "matches",
    }
    existing = _load_json(out_dir / "summary.json", {})
    if isinstance(existing, dict):
        summary = {**existing, **summary}
    if not isinstance(summary.get("pagination"), dict):
        pagination = _load_json(out_dir / "pagination.json", {})
        if isinstance(pagination, dict) and pagination:
            summary["pagination"] = pagination
    if not summary.get("careersUrl"):
        careers_url = careers_url_from_listings(listings)
        if careers_url:
            summary["careersUrl"] = careers_url
    write_summary_json(out_dir, summary)
    upsert_inventory(triage_dir / "INVENTORY.md", summary)
    return result


_DESCRIPTION_FIELDS = ("description", "descriptionFetchedAt", "descriptionSource")


def _row_keys(job: dict[str, Any], company: str) -> list[str]:
    """Every plausible identity for a row, most specific first.

    `fingerprint()` short-circuits on an existing `fingerprint` field.
    Since #25, fingerprint no longer includes the company segment — it is
    `id:{atsId}` or `fp:{hash(company+title+location)}` from job data.
    The join tries: stored fingerprint, computed fingerprint (from the row's
    own company, or the CLI company when the row has none yet), then raw ATS
    id. This handles both old and new fingerprint spellings.
    """
    keys: list[str] = []
    stored = job.get("fingerprint")
    if isinstance(stored, str) and stored:
        keys.append(stored)
    # `fingerprint()` keys on the job's own company field (the CLI flag is
    # ignored since #25). A listings row often has no company yet while the
    # scored row it must join against does, so try both spellings.
    sources = [job]
    if company and not job.get("company"):
        sources.append({**job, "company": company})
    for source in sources:
        try:
            computed = fingerprint(
                {k: v for k, v in source.items() if k != "fingerprint"}
            )
        except Exception:
            continue
        if computed and computed not in keys:
            keys.append(computed)
    ext = str(job.get("externalId") or "")
    if ext and ext not in keys:
        keys.append(ext)
    return keys


def _rehydrate_descriptions(
    listings: list[dict[str, Any]], matches_path: Path, company: str
) -> int:
    """Carry fetched JD bodies from a previous matches.json back onto listings.

    `fetch-descriptions` writes bodies into matches.json, but match-prefs
    scores `listings.json` — which never holds them. Re-scoring therefore
    destroyed every body, so `mustHaves` could never fire against a
    description no matter the step order (#24). Merging them in *before*
    scoring fixes both: bodies survive a re-score, and the AND-set
    matcher sees them.

    Joining is deliberately forgiving. A row that already carries a
    `fingerprint` keeps it verbatim (`fingerprint()` returns it unchanged).
    Since #25, fingerprint no longer includes the company segment, so the
    stored key is stable regardless of `--company`. The join uses
    `_row_keys` which tries: stored fingerprint, computed fingerprint,
    then raw ATS id. This handles both old (pre-#25) and new fingerprint
    spellings. Rows that genuinely have no prior body are left undescribed.
    """
    try:
        prior = load_matches(matches_path)
    except MatchesFormatError:
        return 0
    rows = prior["jobs"]

    by_key: dict[str, dict[str, Any]] = {}
    by_ext: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("description"):
            continue
        for key in _row_keys(row, company):
            by_key.setdefault(key, row)
        ext = str(row.get("externalId") or "")
        if ext:
            by_ext.setdefault(ext, []).append(row)

    def _prior_row(job: dict[str, Any]) -> dict[str, Any] | None:
        for key in _row_keys(job, company):
            hit = by_key.get(key)
            if hit is not None:
                return hit
        ext = str(job.get("externalId") or "")
        if ext:
            candidates = by_ext.get(ext) or []
            if len(candidates) == 1:
                return candidates[0]
        return None

    restored = 0
    for job in listings:
        if not isinstance(job, dict) or job.get("description"):
            continue
        prior_row = _prior_row(job)
        if not prior_row:
            continue
        for field in _DESCRIPTION_FIELDS:
            value = prior_row.get(field)
            if value:
                job[field] = value
        restored += 1
    return restored


def cmd_match_prefs(args: argparse.Namespace) -> int:
    triage = Path(args.triage_dir).expanduser().resolve()
    listings_path = Path(args.listings) if args.listings else triage / "index" / "listings.json"
    listings = _load_json(listings_path, [])
    if not isinstance(listings, list):
        print("listings.json must be a JSON array", file=sys.stderr)
        return 1
    restored_descriptions = _rehydrate_descriptions(
        listings, triage / "index" / "matches.json", args.company or ""
    )
    data = data_home(args.data)
    prefs_path = Path(args.prefs) if args.prefs else data / "preferences.json"
    prefs = _load_json(prefs_path, {})
    if not isinstance(prefs, dict):
        prefs = {}
    resume_text = ""
    resume_path = Path(args.resume) if args.resume else data / "resume" / "text.md"
    if resume_path.is_file():
        resume_text = resume_path.read_text()
    profile = _load_json(data / "profile.json", {})
    exclude = name_tokens(profile) if isinstance(profile, dict) else set()
    company = args.company or ""
    result = _do_match_prefs(triage, listings, prefs, resume_text, exclude, company, args.verbose,
                                 data_dir=args.data)
    matches_path = triage / "index" / "matches.json"
    quiet = {
        "matches": result["nMatches"],
        "listings": result["nListings"],
        "showing": result["showing"],
        "out": str(matches_path),
        "ingestDefault": "matches",
        "departments": result["departments"],
        "matchDepartments": result["matchDepartments"],
        "matchCategories": result["matchCategories"],
        "reasonCounts": result["reasonCounts"],
        "ambiguousSeniority": result["ambiguousSeniority"],
        "restoredDescriptions": restored_descriptions,
        "warnings": result["warnings"],
    }
    _dump(quiet, args.verbose)
    return 0


def cmd_review_matches(args: argparse.Namespace) -> int:
    """Cluster a match-prefs shortlist into job families for empirical vetoing.

    Read-only by default: prints families + leak report. With --veto,
    appends phrases to preferences.json dealBreakers (marks confirmed),
    rewrites matches.json, and prints before/after counts.

    Prior JD bodies are carried from `matches.json` onto `listings.json`
    before any re-score (same join as `match-prefs`), so `--veto` cannot
    clobber descriptions (#24, #32).
    """
    triage = Path(args.triage_dir).expanduser().resolve()
    listings_path = Path(args.listings) if args.listings else triage / "index" / "listings.json"
    listings = _load_json(listings_path, [])
    if not isinstance(listings, list):
        print("listings.json must be a JSON array", file=sys.stderr)
        return 1
    data = data_home(args.data)
    prefs_path = Path(args.prefs) if args.prefs else data / "preferences.json"
    prefs = _load_json(prefs_path, {})
    if not isinstance(prefs, dict):
        prefs = {}
    resume_text = ""
    resume_path = Path(args.resume) if args.resume else data / "resume" / "text.md"
    if resume_path.is_file():
        resume_text = resume_path.read_text()
    profile = _load_json(data / "profile.json", {})
    exclude = name_tokens(profile) if isinstance(profile, dict) else set()
    company = args.company or ""
    restored_descriptions = _rehydrate_descriptions(
        listings, triage / "index" / "matches.json", company
    )

    def run(current_prefs: dict[str, Any]) -> dict[str, Any]:
        resume_vector, job_vectors = _match_semantic(data, resume_text)
        return match_listings(
            listings,
            current_prefs,
            resume_keywords=load_resume_keywords(resume_text, exclude=exclude) if resume_text else None,
            company=company or None,
            resume_vector=resume_vector,
            job_vectors=job_vectors,
        )

    before = run(prefs)
    vetoes = [v.strip() for v in (args.veto or []) if v and v.strip()]
    after = None
    added: list[str] = []
    if vetoes:
        current = [str(x) for x in prefs.get("dealBreakers") or []]
        have = {c.lower() for c in current}
        for phrase in vetoes:
            if phrase.lower() not in have:
                current.append(phrase)
                have.add(phrase.lower())
                added.append(phrase)
        prefs["dealBreakers"] = current
        prefs["dealBreakersConfirmed"] = True
        prefs_path.write_text(json.dumps(prefs, indent=2, ensure_ascii=False) + "\n")
        after = _do_match_prefs(triage, listings, prefs, resume_text, exclude,
                                company, args.verbose, data_dir=args.data)

    shown = after or before
    families = cluster_matches(shown["jobs"], min_cluster=args.min_cluster)
    leaks = explain_leaks(before["allScored"])
    if getattr(args, "json", False):
        _dump({
            "before": {"matches": before["nMatches"], "listings": before["nListings"]},
            "after": ({"matches": after["nMatches"], "listings": after["nListings"]}
                      if after else None),
            "vetoesAdded": added,
            "restoredDescriptions": restored_descriptions,
            "families": families,
            "leaks": leaks,
            "warnings": shown["warnings"],
        }, args.verbose)
        return 0

    print(f"{shown['nMatches']} matched across {len(families)} families "
          f"(of {shown['nListings']} listings)")
    print()
    for fam in families:
        # #30: tolerate a family shape missing optional keys; the bucket and
        # every normal family now carry them, but the printer should not be
        # the thing that hard-crashes a review.
        label = fam.get("label") or fam.get("stem") or "(unnamed family)"
        locs = ", ".join(fam.get("locations") or []) or "—"
        samples = " · ".join((fam.get("sampleTitles") or [])[:3])
        print(f"  {label} — {fam.get('count', 0)}")
        print(f"    median {fam.get('medianScore', 0)} · {locs}")
        if samples:
            print(f"    e.g. {samples}")
        veto = fam.get("suggestedVeto")
        if veto:
            print(f"    veto with: --veto \"{veto}\"")
    print()
    print(f"  not matched: {before['nListings'] - before['nMatches']}")
    if leaks["vetoed"]:
        print(f"    vetoed by dealBreakers: {leaks['vetoed']} "
              f"across {len(leaks['vetoPhrases'])} phrases")
        for phrase, count in list(leaks["vetoPhrases"].items())[:8]:
            print(f'      "{phrase}" {count}')
    if leaks["hardFailed"]:
        print(f"    hard-failed: {leaks['hardFailed']}")
        for reason, count in list(leaks["hardFailReasons"].items())[:8]:
            print(f"      {reason} {count}")
    if vetoes:
        print()
        if added:
            print(f"  veto added: {', '.join(added)}")
        else:
            print("  veto phrases already present — no change")
        print("  preferences.json updated (dealBreakersConfirmed=true)")
        print(f"  matches.json rewritten: "
              f"{before['nMatches']} → {after['nMatches']} matches")  # type: ignore[index]
    for warning in shown["warnings"]:
        print(f"  warning: {warning}")
    return 0


def cmd_fetch_descriptions(args: argparse.Namespace) -> int:
    from .descriptions import fetch_descriptions

    result = fetch_descriptions(
        args.triage_dir,
        source="listings" if args.all else "matches",
        timeout=args.timeout,
        max_chars=args.max_chars,
    )
    company = getattr(args, "company", "") or ""
    if company:
        # Optional, mirrors match-prefs/review-matches so the skill can pass
        # one --company through the whole pipeline (#33). It only labels the
        # run; the source file is still chosen by --all / matches default.
        result = {"company": company, **result}
    _dump(result, args.verbose)
    return 0


def cmd_rank(args: argparse.Namespace) -> int:
    result = rank_corpus(
        data_dir=args.data,
        k=args.k,
        query=args.query,
        include_stale=getattr(args, "include_stale", False),
        company_cap=getattr(args, "company_cap", 3),
    )
    data = data_home(args.data)
    search_dir = data / "search"
    search_dir.mkdir(parents=True, exist_ok=True)
    dest = Path(args.out) if args.out else search_dir / "ranked.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    quiet = {
        "k": result["k"],
        "n": result["n"],
        "showing": result["showing"],
        "staleHidden": result.get("staleHidden", 0),
        "out": str(dest),
    }
    if result.get("companyCap"):
        quiet["companyCap"] = result["companyCap"]
    if args.verbose:
        quiet["shards"] = result.get("shards")
    _dump(quiet, args.verbose)
    return 0


def _fmt_salary(salary: Any) -> str:
    if not isinstance(salary, dict):
        return ""
    lo, hi = salary.get("min"), salary.get("max")
    if lo is None and hi is None:
        return ""

    def fmt(v: Any) -> str:
        if not isinstance(v, (int, float)):
            return "?"
        return f"${v / 1000:.0f}k" if v >= 1000 else f"${v:.0f}"

    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo != hi:
        text = f"{fmt(lo)}–{fmt(hi)}"
    else:
        text = fmt(hi if hi is not None else lo)
    if salary.get("unit") == "hourly":
        text += "/hr"
    return text


def _fmt_age_days(age: int | None) -> str:
    if age is None:
        return ""
    if age <= 0:
        return "today"
    if age == 1:
        return "1d ago"
    return f"{age}d ago"


def cmd_search(args: argparse.Namespace) -> int:
    from datetime import date as _date

    result = rank_corpus(
        data_dir=args.data,
        k=args.k,
        query=args.query or None,
        include_stale=getattr(args, "include_stale", False),
        company_cap=getattr(args, "company_cap", 3),
    )
    if getattr(args, "json", False):
        _dump(result, args.verbose)
        return 0
    # Keep ranked.json fresh so follow-on steps (rank-intros) can use it.
    data = data_home(args.data)
    search_dir = data / "search"
    search_dir.mkdir(parents=True, exist_ok=True)
    (search_dir / "ranked.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    )
    print(result["showing"] + " match prefs" + (f" for {args.query!r}" if args.query else ""))
    # #50: the cap, not the query, is what trimmed the list — say so, otherwise
    # a one-company corpus reads as "only 3 of 42 match".
    cap = result.get("companyCap")
    if isinstance(cap, dict) and cap.get("skipped"):
        print(f"({cap['skipped']} more held back by the {cap['limit']}-per-company cap"
              f" — use --company-cap 0 to see them)")
    if result.get("staleHidden"):
        print(f"({result['staleHidden']} stale postings hidden — re-run with --include-stale)")
    if not result["jobs"] and not result.get("staleHidden"):
        # #54: a bare "0 of 0" is a dead end on a first run. Say whether the
        # corpus is empty or the filters did it, and name the next step.
        from .corpus import load_existing_jobs

        corpus_jobs = len(load_existing_jobs(data / "corpus"))
        if corpus_jobs == 0:
            print("corpus is empty — have your agent find open roles, review the"
                  " triage batch, then ingest")
        elif args.query:
            print(f"no postings match {args.query!r} in the current corpus"
                  " — prefs filtering also applies; check preferences.json")
        else:
            print(f"no postings match your prefs ({corpus_jobs} in the corpus)"
                  " — check preferences.json")
    today = _date.today()
    for job in result["jobs"]:
        bits = [str(job.get("title") or "untitled"), str(job.get("company") or "")]
        salary = _fmt_salary(job.get("salary"))
        if salary:
            bits.append(salary)
        age = _fmt_age_days(posting_age_days(job.get("postedAt"), today))
        if age:
            bits.append(age)
        if job.get("stale"):
            bits.append("STALE")
        print("• " + " — ".join(bits))
        url = job.get("url") or job.get("jdUrl") or ""
        if url:
            print(f"  {url}")
    if args.verbose:
        _print_fit_brief(data_home(args.data), result["jobs"])
    return 0


def _print_fit_brief(data: Path, jobs: list[dict[str, Any]]) -> None:
    """Verbose-only fit evidence per role (#14): reasons, resume mapping,
    connection warmth, gaps. Default output stays terse; the agent narrates
    from this evidence instead of the skill paraphrasing blind."""
    from .intros import _score_forwarder, connections_at_company
    from .prefs import load_resume_keywords, must_have_hits, name_tokens
    from .text import tokenize

    profile = _load_json(data / "profile.json", {})
    if not isinstance(profile, dict):
        profile = {}
    prefs = _load_json(data / "preferences.json", {})
    if not isinstance(prefs, dict):
        prefs = {}
    resume_text = ""
    resume_path = data / "resume" / "text.md"
    if resume_path.is_file():
        resume_text = resume_path.read_text()
    keywords = (load_resume_keywords(resume_text, exclude=name_tokens(profile))
                if resume_text else [])
    must_haves = prefs.get("mustHaves") or []
    connections = _load_json(data / "connections" / "connections.json", [])
    if not isinstance(connections, list):
        connections = []
    print()
    print("Fit brief (verbose only — default output above is unchanged):")
    for job in jobs:
        title = str(job.get("title") or "untitled")
        company = str(job.get("company") or "")
        print(f"  {title} @ {company}")
        reasons = [str(r) for r in job.get("matchReasons") or []]
        print(f"    fit {job.get('matchScore')} · "
              + (", ".join(reasons) if reasons else "no recorded reasons"))
        if keywords:
            blob = f"{title} {job.get('department') or ''} {company}".lower()
            tokens = set(tokenize(blob))
            hits = sorted({k for k in keywords
                           if k.lower() in tokens or k.lower() in blob})[:6]
            mh = must_have_hits(job, must_haves) if must_haves else []
            print(f"    resume hits: {', '.join(hits) if hits else 'none'} | "
                  f"mustHaves: {', '.join(mh) if mh else 'none matched'}")
        people = connections_at_company(connections, company)
        if people:
            best = max(
                (p for p in people if str(p.get("name") or p.get("firstName") or "").strip()),
                key=lambda p: (_score_forwarder(job, p), str(p.get("name") or "")),
                default=None,
            )
            if best is not None:
                name = str(best.get("name") or
                           f"{best.get('firstName') or ''} {best.get('lastName') or ''}".strip())
                pos = str(best.get("position") or "position unknown")
                # Connection age is display context only — never closeness.
                year = ""
                m = re.search(r"(\d{4})", str(best.get("connectedOn") or ""))
                if m:
                    year = f", connected {m.group(1)}"
                print(f"    warmth: {len(people)} at {company} · "
                      f"closest title match: {name} ({pos}{year})")
            else:
                print(f"    warmth: {len(people)} at {company} (no named contacts)")
        else:
            print(f"    warmth: no connections at {company or 'unknown company'}")
        gaps = []
        signals = [str(s).lower() for s in (job.get("senioritySignals") or [])]
        if "intern" in signals or "junior" in signals:
            gaps.append("junior-adjacent")
        if job.get("stale"):
            gaps.append("stale")
        if gaps:
            print(f"    gaps: {', '.join(gaps)}")


#: Fixed synthetic jobs for the `demo` command. Obviously fake companies —
#: never real postings, never PII. Chosen to exercise the pipeline:
#: exact-category hits, category affinity (ai-ml/data vs engineering),
#: a hard-fail (sales), and four Acme roles to show the company cap.
DEMO_JOBS: list[dict[str, Any]] = [
    {"title": "Senior Backend Engineer", "company": "Acme Corp",
     "location": "New York, NY", "department": "Engineering",
     "salary": {"min": 180000, "max": 220000}, "postedAt": "2026-09-20"},
    {"title": "Frontend Engineer", "company": "Acme Corp",
     "location": "New York, NY", "department": "Engineering",
     "postedAt": "2026-09-22"},
    {"title": "DevOps Engineer", "company": "Acme Corp",
     "location": "Remote", "department": "Engineering", "postedAt": "2026-09-25"},
    {"title": "Site Reliability Engineer", "company": "Acme Corp",
     "location": "Remote", "department": "Engineering", "postedAt": "2026-09-26"},
    {"title": "Product Manager", "company": "Globex",
     "location": "San Francisco, CA", "department": "Product",
     "postedAt": "2026-09-18"},
    {"title": "Machine Learning Engineer", "company": "Initech",
     "location": "Remote", "department": "AI", "postedAt": "2026-09-24"},
    {"title": "Data Analyst", "company": "Hooli",
     "location": "Austin, TX", "department": "Data", "postedAt": "2026-09-19"},
    {"title": "Account Executive", "company": "Umbrella",
     "location": "Chicago, IL", "department": "Sales", "postedAt": "2026-09-21"},
]


def cmd_demo(args: argparse.Namespace) -> int:
    """Rank a fixed synthetic job set: no network, no disk writes, no PII."""
    from datetime import date as _date

    from .prefs import score_job

    data = data_home(args.data)
    prefs = _load_json(data / "preferences.json", {})
    if not isinstance(prefs, dict):
        prefs = {}
    if not prefs.get("categories"):
        prefs = {**prefs, "categories": ["engineering", "product"]}
    scored = [score_job(dict(j), prefs) for j in DEMO_JOBS]
    # Demo uses the triage path's stricter bar: drop vetoes and hard-fails
    # (e.g. the sales role against engineering prefs) so the shortlist
    # shows what a real seeker would see.
    scored = [j for j in scored if j.get("matched") and not j.get("veto")]
    scored.sort(key=lambda j: -float(j.get("matchScore") or 0))
    k_eff = max(0, min(int(args.k), len(scored)))
    top: list[dict[str, Any]] = []
    cap = max(0, int(getattr(args, "company_cap", 3) or 0))
    counts: dict[str, int] = {}
    for job in scored:
        if len(top) >= k_eff:
            break
        if cap:
            key = str(job.get("company") or "").strip().lower()
            if counts.get(key, 0) >= cap:
                continue
            counts[key] = counts.get(key, 0) + 1
        top.append(job)
    print(f"demo: {len(top)} of {len(scored)} synthetic jobs "
          f"(prefs categories: {', '.join(prefs.get('categories', []))})")
    today = _date.today()
    for job in top:
        bits = [str(job.get("title") or "untitled"), str(job.get("company") or "")]
        salary = _fmt_salary(job.get("salary"))
        if salary:
            bits.append(salary)
        age = _fmt_age_days(posting_age_days(job.get("postedAt"), today))
        if age:
            bits.append(age)
        print("• " + " — ".join(bits))
        reasons = job.get("matchReasons") or []
        if reasons:
            print(f"  [{', '.join(str(r) for r in reasons)}]")
    return 0


def cmd_companies(args: argparse.Namespace) -> int:
    from datetime import date as _date

    data = data_home(args.data)
    companies = _load_json(data / "companies" / "companies.json", [])
    if not isinstance(companies, list):
        companies = []
    rows = [c for c in companies if isinstance(c, dict)]
    sort = getattr(args, "sort", "connections")
    if sort == "name":
        rows.sort(key=lambda c: str(c.get("name") or "").lower())
    elif sort == "crawl":
        rows.sort(key=lambda c: str(c.get("lastCrawl") or ""))
    else:
        rows.sort(
            key=lambda c: (-int(c.get("connectionCount") or 0), str(c.get("name") or "").lower())
        )
    if getattr(args, "json", False):
        _dump(rows, args.verbose)
        return 0
    today = _date.today()
    print(f"{len(rows)} companies")
    for c in rows:
        name = str(c.get("name") or c.get("slug") or "?")
        conn = c.get("connectionCount")
        crawl = str(c.get("lastCrawl") or "never")
        age = _fmt_age_days(posting_age_days(c.get("lastCrawl"), today))
        digest = str(c.get("listingSetHash") or "")[:8] or "—"
        jobs_url = "jobsUrl" if c.get("jobsUrl") else "no-url"
        line = f"• {name} — conn={conn} — crawl={crawl}"
        if age:
            line += f" ({age})"
        print(line + f" — hash={digest} — {jobs_url}")
    return 0


def _do_refresh(
    data_dir: str | Path | None,
    company: str = "",
    limit: int = 5,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_listings: int = DEFAULT_MAX_LISTINGS,
    fetch: Any = None,
    today: Any = None,
) -> list[dict[str, Any]]:
    """Re-crawl stored listings-JSON endpoints (oldest first), stage new matches.

    Fully deterministic: no model in the loop. Only companies whose row carries
    a jobsUrl (stamped by crawl-state --stamp --source-url) are eligible.
    """
    from datetime import date as _date
    from .text import slugify

    data = data_home(data_dir)
    today_s = str(today)[:10] if today else _date.today().isoformat()
    companies_path = data / "companies" / "companies.json"
    companies = _load_json(companies_path, [])
    if not isinstance(companies, list):
        companies = []
    prefs = _load_json(data / "preferences.json", {})
    if not isinstance(prefs, dict):
        prefs = {}
    resume_text = ""
    resume_path = data / "resume" / "text.md"
    if resume_path.is_file():
        resume_text = resume_path.read_text()
    profile = _load_json(data / "profile.json", {})
    exclude = name_tokens(profile) if isinstance(profile, dict) else set()

    targets = [c for c in companies if isinstance(c, dict) and c.get("jobsUrl")]
    if company:
        want = slugify(company)
        targets = [
            c for c in targets
            if slugify(str(c.get("name") or "")) == want or str(c.get("slug") or "") == want
        ]
    targets.sort(key=lambda c: str(c.get("lastCrawl") or ""))
    targets = targets[: max(1, int(limit or 5))]

    results: list[dict[str, Any]] = []
    for row in targets:
        name = str(row.get("name") or row.get("slug") or "?")
        slug = str(row.get("slug") or slugify(name))
        url = str(row.get("jobsUrl") or "")
        triage = data / "triage" / f"careers-{slug}-{today_s}"
        try:
            pres = paginate(
                url,
                max_pages=max_pages,
                max_listings=max_listings,
                fetch=fetch,
                source_url=url,
                company=name,
            )
            write_pagination(triage, pres, company=name)
            listings = pres.get("listings") or []
            listings_path = triage / "index" / "listings.json"
            status = _do_crawl_state(
                data, triage, listings_path, companies_path,
                slug, name, False, None, False,
            )
            if status.get("unchanged"):
                _do_crawl_state(
                    data, triage, listings_path, companies_path,
                    slug, name, True, url, False,
                )
                results.append({
                    "company": name, "slug": slug, "status": "unchanged",
                    "listings": len(listings),
                })
            else:
                mres = _do_match_prefs(triage, listings, prefs, resume_text, exclude, name, False,
                                          data_dir=data_dir)
                _do_crawl_state(
                    data, triage, listings_path, companies_path,
                    slug, name, True, url, False,
                )
                results.append({
                    "company": name, "slug": slug, "status": "changed",
                    "listings": len(listings), "matches": mres["nMatches"],
                    "triage": str(triage),
                })
        except Exception as exc:  # noqa: BLE001 — one bad board must not kill the run
            results.append({
                "company": name, "slug": slug, "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            })
    return results


def cmd_refresh(args: argparse.Namespace) -> int:
    if args.company:
        pass  # filtered inside _do_refresh; unknown names just yield no targets
    results = _do_refresh(
        args.data,
        company=args.company or "",
        limit=args.limit,
        max_pages=args.max_pages,
        max_listings=args.max_listings,
    )
    if getattr(args, "json", False):
        _dump({"refreshed": len(results), "results": results}, args.verbose)
        return 0
    if not results:
        print("No companies with a stored jobsUrl to refresh.")
        print("Re-run careers-discover (it stamps jobsUrl via crawl-state --stamp --source-url),")
        print("or stamp one manually: crawl-state --company NAME --stamp --source-url URL --listings …")
        return 0
    for r in results:
        status = r["status"]
        if status == "unchanged":
            print(f"• {r['company']}: unchanged ({r['listings']} listings)")
        elif status == "changed":
            print(f"• {r['company']}: {r['matches']} of {r['listings']} match prefs → staged at {r['triage']}")
        else:
            print(f"• {r['company']}: ERROR {r.get('error')}")
    return 0


def cmd_paginate(args: argparse.Namespace) -> int:
    payload = None
    url = args.url
    if args.input:
        payload = _load_json(Path(args.input), None)
        url = url or ""
    if args.input and args.url:
        print("warning: both --input and --url given; fetching --url, --input ignored",
              file=sys.stderr)
    if not url and payload is None:
        print("paginate requires --url or --input", file=sys.stderr)
        return 1
    # Request descriptor for POST/JSON-body boards (#74). A --body
    # without --method implies POST; --input capture is unchanged —
    # a saved page is never fetched, whatever descriptor is given.
    body = None
    if getattr(args, "body", None):
        try:
            body = json.loads(args.body)
        except json.JSONDecodeError as exc:
            print(f"paginate: --body is not valid JSON: {exc}", file=sys.stderr)
            return 1
        if not isinstance(body, dict):
            print("paginate: --body must be a JSON object", file=sys.stderr)
            return 1
    headers = {}
    for raw_header in getattr(args, "header", None) or []:
        if ":" not in raw_header:
            print(f"paginate: --header must be 'Name: value', got {raw_header!r}",
                  file=sys.stderr)
            return 1
        name, value = raw_header.split(":", 1)
        headers[name.strip()] = value.strip()
    method = getattr(args, "method", None) or ("POST" if body is not None else "GET")
    try:
        result = paginate(
            url or None,
            payload=payload if not url else None,
            max_pages=args.max_pages,
            max_listings=args.max_listings,
            source_url=url or str(args.input or ""),
            company=args.company or "",
            # `--input` is a user capture: one saved page, never a whole crawl.
            capture=bool(args.input and not url),
            method=method,
            headers=headers or None,
            body=body,
            body_offset_key=getattr(args, "body_offset_key", "offset") or "offset",
        )
    except OSError as exc:
        # Dead URL / refused connection: a readable `blocked` result beats a
        # traceback with no pagination.json (#39).
        result = blocked_result(url or "", f"{type(exc).__name__}: {exc}",
                                max_pages=args.max_pages)
    quiet = write_pagination(Path(args.triage_dir), result, company=args.company or "")
    _dump(quiet, args.verbose)
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    data = data_home(args.data)
    complete = bool(args.pagination_complete)
    if args.pagination_incomplete:
        complete = False
    summary = rebuild(
        Path(args.batch),
        data / "corpus",
        expire_company=args.expire_company or None,
        pagination_complete=complete,
        pagination_status=args.pagination_status,
        expected_total=args.expect_count,
        force_expire=args.force_expire,
    )
    if summary.get("expirySkipped") and summary.get("expiryReason"):
        print(f"warning: {summary['expiryReason']}", file=sys.stderr)
    _dump(summary if args.verbose else {
        "totalJobs": summary["totalJobs"],
        "incoming": summary["incoming"],
        "closedJobs": summary.get("closedJobs", 0),
        "expired": summary.get("expired", False),
        "expiredCount": summary.get("expiredCount", 0),
        "expirySkipped": summary.get("expirySkipped", False),
        "expiryReason": summary.get("expiryReason", ""),
        "driftWarnings": summary.get("driftWarnings", []),
        "paginationComplete": summary.get("paginationComplete", False),
        "paginationStatus": summary.get("paginationStatus", ""),
    }, args.verbose)
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    # Ingest reads matches.json by default, so accept the documented object
    # and legacy bare arrays through the same normalizer every other reader
    # uses (#32). `--out` stays a bare array: rebuild-corpus consumes a job
    # list, not a matches envelope. Writing back over the input, though, goes
    # through `write_matches` with the envelope's summary keys intact, because
    # a bare array there is the #32 shape flip again.
    try:
        envelope = normalize_matches(
            _load_json(Path(args.input), []), origin=args.input
        )
    except MatchesFormatError:
        print('input must be a JSON array of jobs, or an object with a "jobs" array',
              file=sys.stderr)
        return 1
    listings = envelope["jobs"]
    result = classify_listings(listings, company=args.company or None)
    dest = Path(args.out) if args.out else Path(args.input)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.resolve() == Path(args.input).resolve():
        # In place: keep the documented object shape. Summary keys (`showing`,
        # `ingestDefault`, `departments`, `nListings`, ...) ride through; a
        # legacy bare array input is upgraded here as every other writer does.
        write_matches(dest, {**envelope, "jobs": result["jobs"]})
    else:
        dest.write_text(json.dumps(result["jobs"], indent=2, ensure_ascii=False) + "\n")
    quiet = {
        "n": result["n"],
        "ambiguous": result["ambiguous"],
        "out": str(dest),
    }
    if args.verbose:
        quiet["needsLlm"] = result["needsLlm"]
    _dump(quiet, args.verbose)
    return 0


def _do_crawl_state(
    data: Path,
    triage: Path | None,
    listings_path: Path | None,
    companies_path: Path,
    slug_or_name: str,
    company_name: str | None,
    stamp: bool,
    source_url: str | None,
    verbose: bool,
) -> dict[str, Any]:
    from .crawl import crawl_status, stamp_company_crawl
    from .inventory import upsert_inventory, write_summary_json

    listings = _load_json(listings_path, []) if listings_path else []
    if not isinstance(listings, list):
        listings = []
    companies = _load_json(companies_path, [])
    if not isinstance(companies, list):
        companies = []
    pagination = {}
    if triage and (triage / "index" / "pagination.json").is_file():
        pagination = _load_json(triage / "index" / "pagination.json", {}) or {}
    if stamp:
        status = stamp_company_crawl(
            companies_path,
            slug_or_name,
            listings,
            pagination=pagination if isinstance(pagination, dict) else None,
            company_name=company_name,
            source_url=source_url,
        )
    else:
        status = crawl_status(
            companies,
            slug_or_name,
            listings,
            company_name=company_name,
        )
    if triage:
        summary = _load_json(triage / "index" / "summary.json", {})
        if not isinstance(summary, dict):
            summary = {}
        summary["crawl"] = {
            "unchanged": status.get("unchanged"),
            "listingSetHash": status.get("listingSetHash"),
            "lastCrawl": status.get("lastCrawl"),
        }
        write_summary_json(triage / "index", summary)
        upsert_inventory(triage / "INVENTORY.md", summary)
    return status


def cmd_crawl_state(args: argparse.Namespace) -> int:
    data = data_home(args.data)
    triage = Path(args.triage_dir).expanduser().resolve() if args.triage_dir else None
    listings_path = Path(args.listings) if args.listings else (
        (triage / "index" / "listings.json") if triage else None
    )
    if listings_path is None:
        print("crawl-state requires --listings or --triage-dir", file=sys.stderr)
        return 1
    companies_path = Path(args.companies) if args.companies else data / "companies" / "companies.json"
    status = _do_crawl_state(
        data,
        triage,
        listings_path,
        companies_path,
        args.company or args.slug,
        args.company or None,
        args.stamp,
        getattr(args, "source_url", None),
        args.verbose,
    )
    quiet = {
        "unchanged": status.get("unchanged"),
        "listingSetHash": status.get("listingSetHash"),
        "lastCrawl": status.get("lastCrawl"),
        "skipIngest": bool(status.get("unchanged")),
    }
    if args.verbose:
        quiet.update(status)
    _dump(quiet, args.verbose)
    return 0


def cmd_rank_intros(args: argparse.Namespace) -> int:
    from .intros import filter_jobs, rank_intros
    from .paths import data_home as dh

    data = dh(args.data)
    jobs_path = Path(args.jobs) if args.jobs else data / "search" / "ranked.json"
    payload = _load_json(jobs_path, {})
    if isinstance(payload, dict):
        jobs = payload.get("jobs") or []
    elif isinstance(payload, list):
        jobs = payload
    else:
        jobs = []
    title = getattr(args, "title", "") or ""
    url = getattr(args, "url", "") or ""
    company = getattr(args, "company", "") or ""
    prefer = [str(p) for p in (getattr(args, "prefer", None) or [])]
    if title or url or company:
        jobs = filter_jobs(jobs, title=title, url=url, company=company)
        if not jobs:
            print("no ranked jobs match the given --title/--url/--company filters",
                  file=sys.stderr)
            return 1
    connections = _load_json(
        Path(args.connections) if args.connections else data / "connections" / "connections.json",
        [],
    )
    if not isinstance(connections, list):
        connections = []
    result = rank_intros(jobs, connections, k_roles=args.k_roles,
                       k_forwarders=args.k_forwarders, prefer=prefer)
    dest = Path(args.out) if args.out else data / "search" / "intros.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    quiet = {
        "roles": result["kRoles"],
        "forwarders": args.k_forwarders,
        "fetchJdUrls": result["fetchJdUrls"],
        "out": str(dest),
        "showing": result["showing"],
    }
    applied = {}
    if title:
        applied["title"] = title
    if url:
        applied["url"] = url
    if company:
        applied["company"] = company
    if prefer:
        applied["prefer"] = prefer
    if applied:
        quiet["filters"] = applied
    _dump(quiet, args.verbose)
    return 0


def cmd_embed_setup(args: argparse.Namespace) -> int:
    """One-time setup for semantic matching: ensure fastembed, warm the model."""
    from .embeddings import get_provider

    provider = get_provider()
    if provider is not None:
        if provider.embed(["network-jobs embedding warmup"]):
            print(
                f"ready: provider={provider.name} "
                f"model={provider.model} dims={provider.dims}"
            )
            return 0
        if provider.name == "fastembed":
            # Reinstalling won't fix a detected-but-broken provider (e.g. the
            # model download was blocked); report the cause instead.
            reason = getattr(provider, "last_error", None)
            print(
                "error: provider fastembed detected but a warmup embed failed.",
                file=sys.stderr,
            )
            if reason:
                print(f"  reason: {reason}", file=sys.stderr)
            print(
                "  Check network/proxy settings, then re-run "
                "`network-jobs embed-setup`.",
                file=sys.stderr,
            )
            return 1
        print(
            f"warning: provider {provider.name} detected but a warmup "
            "embed failed; falling back to fastembed setup",
            file=sys.stderr,
        )
    print("fastembed is not installed. Installing (one time):")
    print("  python3 -m pip install fastembed")
    rc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "fastembed"]
    ).returncode
    if rc != 0:
        print(
            "pip install failed; install fastembed with this Python "
            f"({sys.executable}), then re-run `network-jobs embed-setup`",
            file=sys.stderr,
        )
        print(
            "On Debian/Ubuntu (externally managed Python, PEP 668) the "
            "supported routes are a virtualenv, pipx, or:",
            file=sys.stderr,
        )
        print(
            "  python3 -m pip install --break-system-packages fastembed",
            file=sys.stderr,
        )
        return 1
    provider = get_provider()
    if provider is None or not provider.embed(["network-jobs embedding warmup"]):
        print("fastembed installed but embeddings still not working.", file=sys.stderr)
        return 1
    print(
        f"ready: provider={provider.name} "
        f"model={provider.model} dims={provider.dims}"
    )
    print(
        "Next: `network-jobs rebuild <batch>` (or refresh) populates "
        "corpus/embeddings.json."
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .corpus import load_existing_jobs
    from .embeddings import embeddings_path, get_provider, load_embeddings_cache

    data = data_home(args.data)
    corpus = data / "corpus"
    provider = get_provider()
    cache = load_embeddings_cache(corpus)
    vectors = cache.get("vectors") or {}
    jobs = load_existing_jobs(corpus)
    fps = {str(j.get("fingerprint") or "") for j in jobs} - {""}
    covered = sum(1 for fp in fps if fp in vectors)
    payload = {
        "provider": provider.name if provider else None,
        "model": provider.model if provider else None,
        "dims": provider.dims if provider else 0,
        "embeddingsFile": str(embeddings_path(corpus)) if cache else None,
        "cacheModel": cache.get("model"),
        "corpusJobs": len(fps),
        "vectorsCached": len(vectors),
        "coverage": round(covered / len(fps), 3) if fps else 0.0,
    }
    if getattr(args, "json", False):
        _dump(payload, args.verbose)
        return 0
    if provider:
        print(f"embeddings: {provider.name} ({provider.model}, {provider.dims}d)")
    else:
        print("embeddings: not configured (keyword scoring only)")
        print("  run `network-jobs embed-setup` for semantic resume↔job matching")
    if cache:
        print(
            f"cache: {len(vectors)} vectors, model={cache.get('model')}, "
            f"coverage {payload['coverage']:.0%} of {len(fps)} corpus jobs"
        )
    else:
        print("cache: none (run rebuild or refresh with a provider configured)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="network-jobs-helper", add_help=True)
    p.add_argument("-v", "--verbose", action="store_true", help="print extra fields")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("match-prefs", help="score triage listings against preferences.json")
    m.add_argument("--triage-dir", required=True)
    m.add_argument("--listings")
    m.add_argument("--prefs")
    m.add_argument("--resume")
    m.add_argument("--data")
    m.add_argument("--company", default="")
    m.set_defaults(func=cmd_match_prefs)

    fd = sub.add_parser("fetch-descriptions",
                        help="fetch JD bodies for staged matches into matches.json")
    fd.add_argument("--triage-dir", required=True)
    fd.add_argument("--all", action="store_true",
                    help="fetch for listings.json instead of matches.json")
    fd.add_argument("--company", default="",
                    help="company label for the run / summary output (optional)")
    fd.add_argument("--timeout", type=int, default=30)
    fd.add_argument("--max-chars", type=int, default=10000)
    fd.set_defaults(func=cmd_fetch_descriptions)

    v = sub.add_parser("review-matches", help="cluster a match-prefs shortlist into job families for empirical vetoing")
    v.add_argument("--triage-dir", required=True)
    v.add_argument("--listings")
    v.add_argument("--prefs")
    v.add_argument("--resume")
    v.add_argument("--data")
    v.add_argument("--company", default="")
    v.add_argument("--min-cluster", type=int, default=2,
                   help="families below this size fold into one bucket (default: 2)")
    v.add_argument("--veto", action="append", default=[],
                   help="add a deal-breaker phrase, rewrite prefs + matches (repeatable)")
    v.add_argument("--json", action="store_true", help="machine-readable output instead of text")
    v.set_defaults(func=cmd_review_matches)

    r = sub.add_parser("rank", help="rank corpus shards to search/ranked.json")
    r.add_argument("--data")
    r.add_argument("-k", type=int, default=DEFAULT_K)
    r.add_argument("--query", default="")
    r.add_argument("--out")
    r.add_argument("--company-cap", type=int, default=3,
                   help="max jobs per company in the top-K shortlist (0 = no cap)")
    r.add_argument("--include-stale", action="store_true",
                   help="include stale postings (hidden by default)")
    r.set_defaults(func=cmd_rank)

    s = sub.add_parser("search", help="rank the corpus and print a human-readable list")
    s.add_argument("--query", default="")
    s.add_argument("-k", type=int, default=DEFAULT_K)
    s.add_argument("--data")
    s.add_argument("--company-cap", type=int, default=3,
                   help="max jobs per company in the top-K shortlist (0 = no cap)")
    s.add_argument("--include-stale", action="store_true",
                   help="include stale postings (hidden by default)")
    s.add_argument("--json", action="store_true",
                   help="print the ranked payload instead of a list")
    s.set_defaults(func=cmd_search)

    d = sub.add_parser("demo", help="rank a fixed synthetic job set (no network, no disk writes)")
    d.add_argument("--data")
    d.add_argument("-k", type=int, default=8)
    d.add_argument("--company-cap", type=int, default=3,
                   help="max jobs per company in the demo shortlist (0 = no cap)")
    d.set_defaults(func=cmd_demo)

    co = sub.add_parser("companies", help="list the company graph")
    co.add_argument("--data")
    co.add_argument("--json", action="store_true", help="print companies.json rows")
    co.add_argument("--sort", default="connections",
                    choices=["connections", "name", "crawl"])
    co.set_defaults(func=cmd_companies)

    rf = sub.add_parser("refresh", help="re-crawl known listings endpoints, stage new matches")
    rf.add_argument("--data")
    rf.add_argument("--company", default="", help="only refresh this company (name or slug)")
    rf.add_argument("--limit", type=int, default=5, help="max companies per run (oldest crawl first)")
    rf.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    rf.add_argument("--max-listings", type=int, default=DEFAULT_MAX_LISTINGS)
    rf.add_argument("--json", action="store_true", help="print a machine-readable summary")
    rf.set_defaults(func=cmd_refresh)

    g = sub.add_parser("paginate", help="follow page/cursor/offset/start into listings.json")
    g.add_argument("--url")
    g.add_argument("--input", help="local JSON file instead of fetching")
    g.add_argument("--triage-dir", required=True)
    g.add_argument("--company", default="")
    g.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    g.add_argument("--max-listings", type=int, default=DEFAULT_MAX_LISTINGS)
    # Request descriptor for POST/JSON-body job APIs (#74, e.g. Workday
    # CXS): --body is a JSON object, stepped by its offset key per page.
    g.add_argument("--method", default=None,
                   help="GET (default) or POST; a --body without --method implies POST")
    g.add_argument("--body", default=None,
                   help="JSON object sent as the request body (POST boards)")
    g.add_argument("--header", action="append", default=None,
                   help="extra request header 'Name: value' (repeatable)")
    g.add_argument("--body-offset-key", default="offset",
                   help="JSON body key stepped per POST page (default: offset)")
    g.set_defaults(func=cmd_paginate)

    b = sub.add_parser("rebuild", help="merge batch into corpus by fingerprint")
    b.add_argument("batch")
    b.add_argument("--data")
    b.add_argument("--expire-company", default="")
    b.add_argument("--pagination-complete", action="store_true")
    b.add_argument("--pagination-incomplete", action="store_true")
    b.add_argument("--pagination-status", default=None,
                   help="status from index/pagination.json: blocked/captured never expire; truncated unless --force-expire")
    b.add_argument("--expect-count", type=int, default=None,
                   help="board listing count: skip expiry when the batch is a strict subset (filtered ingest)")
    b.add_argument("--force-expire", action="store_true",
                   help="expire even on a subset batch or an empty crawl that "
                        "would drop prior open roles (operator override)")
    b.set_defaults(func=cmd_rebuild)

    c = sub.add_parser("classify", help="title/department → category, track, seniority")
    c.add_argument("--input", required=True)
    c.add_argument("--out")
    c.add_argument("--company", default="")
    c.set_defaults(func=cmd_classify)

    cr = sub.add_parser("crawl-state", help="listing-set hash vs lastCrawl; skip unchanged boards")
    cr.add_argument("--triage-dir")
    cr.add_argument("--listings")
    cr.add_argument("--companies")
    cr.add_argument("--data")
    cr.add_argument("--company", default="")
    cr.add_argument("--slug", default="")
    cr.add_argument("--stamp", action="store_true", help="write lastCrawl + hash onto companies.json")
    cr.add_argument("--source-url", default="",
                    help="listings JSON endpoint; stored as jobsUrl when stamping (used by refresh)")
    cr.set_defaults(func=cmd_crawl_state)

    ri = sub.add_parser("rank-intros", help="1–2 roles + 1–2 forwarders from full connections.json")
    ri.add_argument("--data")
    ri.add_argument("--jobs")
    ri.add_argument("--connections")
    ri.add_argument("--out")
    ri.add_argument("--k-roles", type=int, default=2)
    ri.add_argument("--k-forwarders", type=int, default=2)
    ri.add_argument("--title", default="",
                    help="only roles whose title contains this text")
    ri.add_argument("--url", default="",
                    help="only roles whose url contains this text")
    ri.add_argument("--company", default="",
                    help="only roles at this company (normalized match)")
    ri.add_argument("--prefer", action="append", default=[],
                    help="pin a person to the top of forwarders by name (repeatable)")
    ri.set_defaults(func=cmd_rank_intros)

    es = sub.add_parser("embed-setup", help="install fastembed + warm the embedding model (one time)")
    es.add_argument("--data")
    es.set_defaults(func=cmd_embed_setup)

    d = sub.add_parser("doctor", help="report embedding provider + cache coverage")
    d.add_argument("--data")
    d.add_argument("--json", action="store_true", help="print the report as JSON")
    d.set_defaults(func=cmd_doctor)

    bp = sub.add_parser(
        "build-pack",
        help="build a publishable company-aggregated network pack from a LinkedIn export",
    )
    bp.add_argument("input", help="LinkedIn Connections.csv or .zip export")
    bp.add_argument("--label", default="",
                    help="pack owner label, e.g. 'Frank Harris'")
    bp.add_argument("--out", default="",
                    help="output JSON path (default: <label>-network-pack-<date>.json)")
    bp.add_argument("--min-count", type=int, default=1,
                    help="drop companies with fewer connections")
    bp.set_defaults(func=cmd_build_pack)

    fp = sub.add_parser(
        "fetch-pack",
        help="download a published network pack into $DATA/packs/",
    )
    fp.add_argument("url", help="pack URL, e.g. https://hirefrank.com/network-pack.json")
    fp.add_argument("--as", dest="as_name", default="",
                    help="pack name (default: slug of the pack label)")
    fp.add_argument("--data")
    fp.set_defaults(func=cmd_fetch_pack)

    # Every subcommand honors -v/--verbose in trailing position (#17).
    # Helpers and the bin wrapper append flags after the subcommand, where
    # the top-level flag is unreachable — without this, `search -v` and
    # friends fail with "unrecognized arguments".
    for _sub in sub.choices.values():
        _sub.add_argument("-v", "--verbose", action="store_true",
                          help="print extra fields")
    return p


def cmd_build_pack(args: argparse.Namespace) -> int:
    from .pack import build_pack, default_pack_path, write_pack

    pack = build_pack(
        args.input,
        label=getattr(args, "label", "") or "",
        min_count=int(getattr(args, "min_count", 1) or 1),
    )
    out = getattr(args, "out", "") or default_pack_path(pack["label"], pack["generatedAt"])
    write_pack(pack, out)
    print(f"pack: {pack['totalConnections']} connections -> "
          f"{len(pack['companies'])} companies -> {out}")
    return 0


def cmd_fetch_pack(args: argparse.Namespace) -> int:
    from .pack import fetch_pack
    from .paths import data_home

    pack, dest = fetch_pack(
        args.url,
        data_dir=getattr(args, "data", None),
        name=getattr(args, "as_name", "") or "",
    )
    data = data_home(getattr(args, "data", None))
    try:
        shown = str(dest.relative_to(data))
    except ValueError:
        shown = str(dest)
    label = pack.get("label") or "unlabeled"
    print(f"pack: {label} — {pack.get('totalConnections')} connections -> "
          f"{len(pack['companies'])} companies -> {shown}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
