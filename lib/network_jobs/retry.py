"""Shared retry helpers for HTTP fetches (#70, #64).

`descriptions.py` (#64) and `pagination.py` (#70) both retry throttled
and failing hosts, but with different over-cap policies, so this module
shares only the parts that are genuinely common and leaves the policy
to the caller:

- which statuses are retryable (429 and 5xx, never other 4xx),
- parsing `Retry-After` (delta-seconds or HTTP-date, uncapped here),
- exponential backoff with jitter when the header is absent.

`pagination.fetch_json` (#70) honours a `Retry-After` by waiting at
most `MAX_RETRY_AFTER_SECONDS` (60s) for that attempt. `descriptions`
keeps its own rule: a `Retry-After` over its 30s cap means *no* retry
at all, because retrying early would ignore the host's request. Do not
unify the two policies without revisiting #64's tests.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 60.0


def is_retryable_status(status: Any) -> bool:
    """True only for 429 and 5xx. 403 bot walls and 404s are not retried
    here — a 403 is a property of the URL/host, not of the moment, and
    descriptions.py retries a 403 only when it carries a CDN bot-wall
    signature, which is its own check."""
    try:
        return int(status) in RETRYABLE_STATUSES
    except (TypeError, ValueError):
        return False


def parse_retry_after(headers: Any, now: datetime | None = None) -> float | None:
    """`Retry-After` as non-negative seconds, else None.

    Both forms the header allows are accepted: delta-seconds (integer
    or float) and an HTTP-date. The value is **not** capped here — the
    caller decides whether a wait that long is acceptable. A header the
    server sent but we cannot parse is treated as absent, so the caller
    falls back to exponential backoff. `now` is injectable so tests can
    pin the HTTP-date arithmetic without racing the clock.
    """
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        seconds = None
    if seconds is not None:
        if math.isnan(seconds) or math.isinf(seconds):
            return None
        return max(seconds, 0.0)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max((when - reference).total_seconds(), 0.0)


def retry_delay(
    *,
    attempt: int,
    base_delay: float = 1.0,
    retry_after: float | None = None,
    max_retry_after: float = MAX_RETRY_AFTER_SECONDS,
) -> float:
    """Seconds to wait before attempt `attempt`'s retry (0-based).

    A server-supplied `Retry-After` wins, capped at `max_retry_after`
    per wait; without one, exponential backoff (`base_delay * 2**attempt`)
    plus up to one `base_delay` of jitter, so a burst of crawlers does
    not re-hit the host in lockstep.
    """
    if retry_after is not None:
        return min(max(float(retry_after), 0.0), max_retry_after)
    return base_delay * (2 ** attempt) + random.uniform(0.0, base_delay)
