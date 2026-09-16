"""Shared Jarvis time helpers — ONE implementation of the naive-UTC ↔ local
wall-clock conversions that scheduler.py, pim.py, and caldav_sync.py each
used to re-implement (the CalDAV all-day-shift bug came from that scatter).

Storage convention: DB timestamps are naive-UTC ISO strings; user-facing
times are wall clock in the assistant-profile timezone. Wall-clock math is
done in the target timezone (DST-safe), never by adding fixed UTC deltas.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")


def safe_zone(tz_name: str) -> ZoneInfo:
    """ZoneInfo with a UTC fallback for empty/garbage names."""
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def to_utc_naive_iso(local_dt: datetime, tz: ZoneInfo) -> str:
    """Interpret local_dt as wall clock in tz, return naive-UTC ISO."""
    return local_dt.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None).isoformat()


def local_to_utc(value: str, tz_name: str) -> str | None:
    """'YYYY-MM-DDTHH:MM' local wall clock → naive-UTC ISO. None on garbage."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return to_utc_naive_iso(datetime.fromisoformat(raw), safe_zone(tz_name))
    except ValueError:
        return None


def utc_to_local(value: str, tz_name: str) -> str:
    """Naive-UTC ISO → local wall-clock ISO (minute precision). Empty input
    returns ''; unparseable input is echoed back unchanged."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        utc = datetime.fromisoformat(raw).replace(tzinfo=UTC)
        return (utc.astimezone(safe_zone(tz_name))
                .replace(tzinfo=None).isoformat(timespec="minutes"))
    except ValueError:
        return raw
