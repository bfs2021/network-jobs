"""Merge corpus jobs by fingerprint; expire unseen only when pagination is complete."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .embeddings import maybe_update_embeddings
from .fingerprint import fingerprint
from .inventory import NO_EXPIRY_STATUSES
from .locations import location_buckets_for, primary_location_bucket
from .matches import MatchesFormatError, normalize_matches
from .text import normalize_company, slugify

SHARD_SKIP = {"manifest.json", "jobs-all.json"}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_jobs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    try:
        return list(normalize_matches(data, origin=str(path))["jobs"])
    except MatchesFormatError as exc:
        raise SystemExit(str(exc)) from exc


def job_fingerprint(job: dict[str, Any]) -> str:
    fp = fingerprint(job)
    job["fingerprint"] = fp
    return fp


def load_existing_jobs(corpus: Path) -> list[dict[str, Any]]:
    existing_path = corpus / "jobs-all.json"
    if existing_path.exists():
        existing = load_jobs(existing_path)
        if existing:
            return existing
    by_fp: dict[str, dict[str, Any]] = {}
    shard_names: set[str] = set()
    manifest_path = corpus / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            for meta in (manifest.get("categories") or {}).values():
                if isinstance(meta, dict) and meta.get("file"):
                    shard_names.add(meta["file"])
                for loc_meta in (meta.get("byLocation") or {}).values():
                    if not isinstance(loc_meta, dict):
                        continue
                    for sen_meta in loc_meta.values():
                        if isinstance(sen_meta, dict) and sen_meta.get("file"):
                            shard_names.add(sen_meta["file"])
        except json.JSONDecodeError:
            pass
    for path in corpus.glob("*.json"):
        if path.name in SHARD_SKIP:
            continue
        shard_names.add(path.name)
    for name in sorted(shard_names):
        path = corpus / name
        if not path.exists():
            continue
        for job in load_jobs(path):
            by_fp[job_fingerprint(job)] = job
    return list(by_fp.values())


def _company_key(job: dict[str, Any]) -> str:
    return normalize_company(str(job.get("company") or ""))


def _matches_expire_company(job: dict[str, Any], expire_company: str) -> bool:
    target = normalize_company(expire_company) or slugify(expire_company)
    key = _company_key(job)
    slug = slugify(key)
    return key == target or slug == slugify(target) or slug == target


def _open_count_for_company(jobs: list[dict[str, Any]], expire_company: str) -> int:
    return sum(
        1 for job in jobs
        if str(job.get("status") or "open") != "closed"
        and _matches_expire_company(job, expire_company)
    )


def merge_jobs(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    expire_company: str | None = None,
    pagination_complete: bool = False,
    pagination_status: str | None = None,
    expected_total: int | None = None,
    force_expire: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Merge incoming into existing by fingerprint.

    Expiry closes unseen jobs for expire_company only when the batch can
    plausibly be the whole board: pagination must be complete AND (when
    expected_total is known) the incoming fingerprint set must cover it.
    A matches-only batch over a bigger board skips expiry loudly instead of
    silently closing live roles (#16) — pass force_expire only when you know
    the batch is the full board despite a smaller count.

    `pagination_status` is the crawl headline from `index/pagination.json`.
    `blocked` (unreadable board) and `captured` (user-saved page) can never
    expire, even with `pagination_complete` set — the flag is model-supplied,
    the status is written by the helper (#39). `truncated` is a partial crawl
    and is refused unless `force_expire` is set; it only fires when the caller
    passed `pagination_complete`, so the normal `--pagination-incomplete` path
    stays a silent no-op. A sudden drop to zero incoming
    rows for that company, with N>0 prior open roles, is skipped unless
    `force_expire` is set — regardless of status (omitted, the jq string
    "null", or `empty`). A filtered or region-limited crawl URL can look
    empty and would otherwise wipe the company. A company that already had
    zero open roles may record empty.

    Fingerprint drift detection (#25): if an incoming job's externalId matches
    an existing job under a different fingerprint, we merge into the existing
    entry (updating its fingerprint to the new one) and emit a warning. This
    prevents silent duplication when --company was used inconsistently.
    """
    by_fp: dict[str, dict[str, Any]] = {}
    by_ext: dict[str, str] = {}  # externalId -> fingerprint
    for job in existing:
        fp = job_fingerprint(job)
        by_fp[fp] = job
        ext = job.get("externalId")
        if ext:
            by_ext[str(ext)] = fp
    seen: set[str] = set()
    today = _today()
    drift_warnings: list[str] = []
    for job in incoming:
        fp = job_fingerprint(job)
        seen.add(fp)
        ext = job.get("externalId")
        # Reconcile by externalId: if this ATS id already exists under a
        # different fingerprint, merge into the existing entry (#25).
        if ext:
            ext_str = str(ext)
            existing_fp = by_ext.get(ext_str)
            if existing_fp and existing_fp != fp:
                drift_warnings.append(
                    f"Fingerprint drift for externalId={ext_str}: "
                    f"{existing_fp} -> {fp} (merging into existing)"
                )
                # Merge into the existing entry, preserving its fingerprint
                fp = existing_fp
        prev = by_fp.get(fp)
        merged = dict(job)
        if prev:
            if not merged.get("firstSeen"):
                merged["firstSeen"] = prev.get("firstSeen") or today
            if prev.get("url") and not merged.get("url"):
                merged["url"] = prev.get("url")
            if not merged.get("lastSeen"):
                merged["lastSeen"] = prev.get("lastSeen") or today
            if not merged.get("postedAt") and prev.get("postedAt"):
                merged["postedAt"] = prev.get("postedAt")
        else:
            merged.setdefault("firstSeen", today)
            merged["lastSeen"] = merged.get("lastSeen") or today
        merged["status"] = merged.get("status") or "open"
        if merged["status"] == "closed":
            merged["status"] = "open"
            merged.pop("closedAt", None)
        by_fp[fp] = merged
        if ext:
            by_ext[str(ext)] = fp

    info: dict[str, Any] = {"expired": 0, "expirySkipped": False, "expiryReason": "", "driftWarnings": drift_warnings}
    if expire_company and pagination_status in NO_EXPIRY_STATUSES:
        # Never expire from an unreadable board or a user capture — not even
        # with force_expire, because those rows are not evidence a role closed.
        info["expirySkipped"] = True
        info["expiryReason"] = (
            f"pagination status={pagination_status} is not evidence that roles "
            "closed (blocked = unreadable board, captured = user-saved page): "
            "expiry skipped")
    elif (expire_company and pagination_status == "truncated"
          and pagination_complete and not force_expire):
        # A model that passes --pagination-complete anyway must not open the
        # expiry gate for a partial crawl. The normal truncated path uses
        # --pagination-incomplete and is already a no-op below; an explicit
        # force_expire still can expire.
        info["expirySkipped"] = True
        info["expiryReason"] = (
            "pagination status=truncated is a partial crawl (page/listing "
            "cap, mid-board start, or fewer roles than the board reported): "
            "expiry skipped (pass --force-expire to override)")
    elif expire_company and pagination_complete:
        prior_open = _open_count_for_company(existing, expire_company)
        incoming_for_company = any(
            _matches_expire_company(j, expire_company) for j in incoming
        )
        # Key on the data, not the status label: an omitted status or jq's
        # "null" (older pagination.json) used to fall through and expire
        # every prior open role. Check before the expect-count subset rule
        # because expected_total=0 makes len(seen) < expected_total false.
        if not force_expire and prior_open > 0 and not incoming_for_company:
            info["expirySkipped"] = True
            info["expiryReason"] = (
                f"incoming 0 rows for company but {prior_open} open "
                f"role{'s' if prior_open != 1 else ''} already in the corpus: "
                "expiry skipped (a filtered or region-limited URL can look "
                "empty; pass --force-expire if every role really closed)")
        elif (expected_total is not None and not force_expire
                and len(seen) < expected_total):
            info["expirySkipped"] = True
            info["expiryReason"] = (
                f"incoming {len(seen)} fingerprints < expected board "
                f"{expected_total}: treating batch as filtered; expiry skipped "
                "(pass force_expire to override)")
        else:
            for fp, job in list(by_fp.items()):
                if fp in seen:
                    continue
                if not _matches_expire_company(job, expire_company):
                    continue
                if str(job.get("status") or "open") == "closed":
                    continue
                closed = dict(job)
                closed["status"] = "closed"
                closed["closedAt"] = today
                by_fp[fp] = closed
                info["expired"] += 1
    elif expire_company and not pagination_complete:
        # Explicit no-op: incomplete crawls must not close unseen roles.
        pass

    return list(by_fp.values()), info


def write_shards(corpus: Path, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    corpus.mkdir(parents=True, exist_ok=True)
    open_jobs = [j for j in jobs if str(j.get("status") or "open") != "closed"]
    categories: dict[str, list] = defaultdict(list)
    granular: dict[tuple[str, str, str], list] = defaultdict(list)
    for job in open_jobs:
        cat = job.get("category") or "other"
        buckets = job.get("locationBuckets") or [job.get("locationBucket") or "other"]
        if not isinstance(buckets, list) or not buckets:
            buckets = location_buckets_for(job)
        sen = job.get("seniority") or "mid"
        if sen not in ("senior", "mid"):
            sen = "mid"
        job.setdefault("locationBucket", primary_location_bucket(job))
        categories[cat].append(job)
        for loc in buckets:
            granular[(cat, loc or "other", sen)].append(job)

    written: set[str] = set()
    for cat, items in categories.items():
        name = f"{cat}.json"
        (corpus / name).write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")
        written.add(name)
    for (cat, loc, sen), items in granular.items():
        name = f"{cat}-{loc}-{sen}.json"
        (corpus / name).write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")
        written.add(name)
    (corpus / "jobs-all.json").write_text(json.dumps(jobs, indent=2, ensure_ascii=False) + "\n")
    written.add("jobs-all.json")

    now = _now()
    manifest: dict[str, Any] = {"lastUpdated": now, "totalJobs": len(open_jobs), "categories": {}}
    for cat, items in sorted(categories.items()):
        by_loc: dict[str, dict[str, list]] = defaultdict(lambda: {"senior": [], "mid": []})
        for job in items:
            buckets = job.get("locationBuckets") or [job.get("locationBucket") or "other"]
            sen = job.get("seniority") or "mid"
            if sen not in ("senior", "mid"):
                sen = "mid"
            for loc in buckets:
                by_loc[loc or "other"][sen].append(job)
        entry = {"count": len(items), "file": f"{cat}.json", "byLocation": {}}
        for loc, sens in sorted(by_loc.items()):
            entry["byLocation"][loc] = {}
            for sen, arr in sens.items():
                if not arr:
                    continue
                fname = f"{cat}-{loc}-{sen}.json"
                entry["byLocation"][loc][sen] = {"count": len(arr), "file": fname}
        manifest["categories"][cat] = entry
    (corpus / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    written.add("manifest.json")

    keep = written | SHARD_SKIP
    removed = []
    for path in corpus.glob("*.json"):
        if path.name in keep:
            continue
        path.unlink()
        removed.append(path.name)
    return {
        "totalJobs": len(open_jobs),
        "closedJobs": len(jobs) - len(open_jobs),
        "files": sorted(written),
        "removedStale": removed,
        "lastUpdated": now,
    }


def rebuild(
    incoming_path: Path,
    corpus: Path,
    *,
    expire_company: str | None = None,
    pagination_complete: bool = False,
    pagination_status: str | None = None,
    expected_total: int | None = None,
    force_expire: bool = False,
) -> dict[str, Any]:
    incoming = load_jobs(incoming_path)
    existing = load_existing_jobs(corpus)
    jobs, merge_info = merge_jobs(
        existing,
        incoming,
        expire_company=expire_company,
        pagination_complete=pagination_complete,
        pagination_status=pagination_status,
        expected_total=expected_total,
        force_expire=force_expire,
    )
    summary = write_shards(corpus, jobs)
    summary["incoming"] = len(incoming)
    summary["expired"] = (
        bool(expire_company) and pagination_complete
        and not merge_info["expirySkipped"]
    )
    summary["expiredCount"] = merge_info["expired"]
    summary["expirySkipped"] = merge_info["expirySkipped"]
    summary["expiryReason"] = merge_info["expiryReason"]
    summary["driftWarnings"] = merge_info.get("driftWarnings", [])
    summary["expireCompany"] = expire_company or ""
    summary["paginationComplete"] = pagination_complete
    summary["paginationStatus"] = pagination_status or ""
    # Semantic cache: embed only new/changed jobs; no-op when no provider.
    summary["embeddings"] = maybe_update_embeddings(corpus, jobs)
    return summary
