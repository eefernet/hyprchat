"""Quiet hours for Jarvis push notifications.

Pure window math over the assistant profile's `quiet_hours` JSON:
    {"enabled": false, "start": "22:00", "end": "07:00", "urgent_override": true}

Enforced in notifications.notify: the in-app row ALWAYS lands (that invariant
is untouched); only the ntfy/email push fanout is suppressed inside the
window, unless the notification is urgent and urgent_override is on.
Overnight windows (start > end) span midnight. All comparisons happen on the
profile-timezone wall clock via timeutil.safe_zone.
"""

from __future__ import annotations

from datetime import datetime, time as dtime

from timeutil import safe_zone


def _parse_hhmm(value, fallback: dtime) -> dtime:
    try:
        hh, mm = str(value).split(":", 1)
        return dtime(max(0, min(23, int(hh))), max(0, min(59, int(mm))))
    except (ValueError, AttributeError, TypeError):
        return fallback


def is_quiet(profile: dict | None, now_utc: datetime | None = None) -> bool:
    """True when the profile's quiet-hours window is enabled and covers now.
    A zero-length window (start == end) never matches."""
    cfg = (profile or {}).get("quiet_hours") or {}
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return False
    start = _parse_hhmm(cfg.get("start"), dtime(22, 0))
    end = _parse_hhmm(cfg.get("end"), dtime(7, 0))
    if start == end:
        return False
    tz = safe_zone((profile or {}).get("timezone") or "UTC")
    now = (now_utc or datetime.utcnow()).replace(tzinfo=safe_zone("UTC")).astimezone(tz)
    wall = dtime(now.hour, now.minute)
    if start < end:                       # same-day window, e.g. 13:00–15:00
        return start <= wall < end
    return wall >= start or wall < end    # overnight window, e.g. 22:00–07:00


def suppress_push(profile: dict | None, *, urgent: bool = False,
                  now_utc: datetime | None = None) -> bool:
    """Should the ntfy/email push fanout be skipped for this notification?"""
    if not is_quiet(profile, now_utc):
        return False
    cfg = (profile or {}).get("quiet_hours") or {}
    if urgent and cfg.get("urgent_override", True):
        return False
    return True
