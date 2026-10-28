"""Tolerant date parsing shared by ingest (postedAt normalization) and rank."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
import re

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_FMTS = ("%Y/%m/%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y")
# Unix-epoch window, 2000-01-01 .. 2100-01-01, in seconds and milliseconds.
# Anything outside it is not a plausible posting date: a bare year (2025),
# a 1970-01-01 from `True` (= 1), or a junk 15-digit number must not become
# a date. Booleans are excluded explicitly — `bool` subclasses `int`.
_EPOCH_SECONDS_RANGE = (946684800, 4102444800)
_EPOCH_MILLIS_RANGE = (946684800000, 4102444800000)


def _epoch_date(raw: Any) -> date | None:
    """Epoch seconds / milliseconds → UTC date, else None.

    Accepts ints, integral floats, and numeric strings. A float with a
    fractional part, a boolean, or a value outside the plausible window is
    not an epoch — the caller keeps the raw value instead of inventing a
    1970 date from it.
    """
    if isinstance(raw, bool):
        return None
    value: float | None = None
    if isinstance(raw, int):
        value = float(raw)
    elif isinstance(raw, float):
        if not raw.is_integer():
            return None
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        if not number.is_integer():
            return None
        value = number
    else:
        return None
    low_s, high_s = _EPOCH_SECONDS_RANGE
    low_ms, high_ms = _EPOCH_MILLIS_RANGE
    if low_s <= value <= high_s:
        seconds = value
    elif low_ms <= value <= high_ms:
        seconds = value / 1000.0
    else:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def parse_date_flexible(raw: Any) -> date | None:
    """Best-effort anything → date. ATS formats vary wildly; None when unknown."""
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    m = _ISO_RE.match(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    for fmt in _FMTS:
        try:
            return datetime.strptime(s[:24], fmt).date()
        except ValueError:
            continue
    # Epoch last: a numeric string that is not an ISO/formatted date is
    # most plausibly an Eightfold-style `postedTs` / `creationTs` (#75).
    return _epoch_date(raw)


def normalize_date_iso(raw: Any) -> str | None:
    """Canonical YYYY-MM-DD when parseable, else None (caller keeps raw)."""
    d = parse_date_flexible(raw)
    return d.isoformat() if d else None
