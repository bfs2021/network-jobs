"""One reader/writer for triage `index/matches.json` (#32).

SCHEMA.md documents the object shape:

    {nListings, nMatches, showing, departments, ingestDefault, jobs[]}

`fetch-descriptions` used to write back only the job array, so the same path
could hold either an object or a bare array depending on the last writer.
Every reader goes through `load_matches`, which accepts both shapes and
always returns the object; every writer goes through `write_matches`, which
normalizes before writing.

Legacy bare arrays normalize to `{"jobs": [...], "nMatches": N,
"ingestDefault": "matches"}` — the only summary values an array actually
carries. `nListings` / `showing` / `departments` are not invented. Readers
that rewrite the file (`fetch-descriptions`) keep every key they found, so a
legacy directory is upgraded in place on the next pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

INGEST_DEFAULT = "matches"


class MatchesFormatError(ValueError):
    """matches.json is missing/unreadable or is neither supported shape."""


def normalize_matches(payload: Any, *, origin: str = "matches.json") -> dict[str, Any]:
    """Return the SCHEMA object for either on-disk shape.

    A bare array (the pre-#32 `fetch-descriptions` output) is wrapped; an
    object is copied with `jobs` normalized to a list. Anything else raises
    `MatchesFormatError` so callers never guess at a third shape.
    """
    if isinstance(payload, list):
        return {
            "jobs": payload,
            "nMatches": len(payload),
            "ingestDefault": INGEST_DEFAULT,
        }
    if isinstance(payload, dict):
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise MatchesFormatError(f"{origin}: object has no 'jobs' array")
        out = dict(payload)
        out["jobs"] = jobs
        out.setdefault("ingestDefault", INGEST_DEFAULT)
        return out
    raise MatchesFormatError(f"{origin}: expected an object or a bare array")


def load_matches(path: str | Path) -> dict[str, Any]:
    """Read `matches.json` (object or legacy array) as the SCHEMA object.

    Raises `MatchesFormatError` for a missing file, invalid JSON, or an
    unrecognized payload. Callers that treat the file as optional catch it.
    """
    target = Path(path)
    try:
        payload = json.loads(target.read_text())
    except json.JSONDecodeError as exc:
        raise MatchesFormatError(f"{target}: invalid JSON ({exc.msg})") from exc
    except OSError as exc:
        raise MatchesFormatError(
            f"{target}: unreadable ({type(exc).__name__})") from exc
    return normalize_matches(payload, origin=str(target))


def write_matches(path: str | Path, payload: Any) -> dict[str, Any]:
    """Normalize to the documented object, write it, and return what was written."""
    target = Path(path)
    normalized = normalize_matches(payload, origin=str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n")
    return normalized
