"""Offline unit tests for quiet_hours.py — window math and push suppression.

Pure functions over the assistant profile's quiet_hours JSON; no server, no DB.
Run: python -m pytest tests/test_quiet_hours_unit.py -v
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import quiet_hours  # noqa: E402


def _profile(enabled=True, start="22:00", end="07:00", urgent_override=True,
             tz="UTC"):
    return {"timezone": tz, "quiet_hours": {
        "enabled": enabled, "start": start, "end": end,
        "urgent_override": urgent_override}}


# ── is_quiet ─────────────────────────────────────────────────────────────

def test_disabled_never_quiet():
    p = _profile(enabled=False)
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 12, 23, 0)) is False


def test_missing_config_never_quiet():
    assert quiet_hours.is_quiet(None) is False
    assert quiet_hours.is_quiet({}) is False
    assert quiet_hours.is_quiet({"quiet_hours": "garbage"}) is False


def test_overnight_window_spans_midnight():
    p = _profile(start="22:00", end="07:00")
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 12, 23, 30)) is True   # before midnight
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 13, 3, 0)) is True     # after midnight
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 13, 6, 59)) is True    # last minute
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 13, 7, 0)) is False    # end exclusive
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 13, 12, 0)) is False   # daytime


def test_same_day_window():
    p = _profile(start="13:00", end="15:00")
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 12, 13, 0)) is True
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 12, 14, 59)) is True
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 12, 15, 0)) is False
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 12, 12, 59)) is False


def test_zero_length_window_never_matches():
    p = _profile(start="09:00", end="09:00")
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 12, 9, 0)) is False


def test_window_uses_profile_timezone():
    # 03:00 UTC = 22:00 America/Chicago (CDT, UTC-5) the previous evening —
    # inside a 22:00–07:00 window on the Chicago wall clock.
    p = _profile(tz="America/Chicago")
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 13, 3, 0)) is True
    # 03:00 UTC is 03:00 on a UTC wall clock too — also quiet for UTC.
    # Pick 20:00 UTC = 15:00 Chicago → not quiet there, quiet nowhere.
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 12, 20, 0)) is False


def test_malformed_times_fall_back():
    p = _profile(start="not-a-time", end="junk")  # falls back to 22:00–07:00
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 12, 23, 0)) is True
    assert quiet_hours.is_quiet(p, datetime(2026, 7, 12, 12, 0)) is False


# ── suppress_push ────────────────────────────────────────────────────────

def test_suppress_inside_window():
    p = _profile()
    assert quiet_hours.suppress_push(p, now_utc=datetime(2026, 7, 12, 23, 0)) is True


def test_no_suppress_outside_window():
    p = _profile()
    assert quiet_hours.suppress_push(p, now_utc=datetime(2026, 7, 12, 12, 0)) is False


def test_urgent_pierces_when_override_on():
    p = _profile(urgent_override=True)
    assert quiet_hours.suppress_push(p, urgent=True,
                                     now_utc=datetime(2026, 7, 12, 23, 0)) is False


def test_urgent_suppressed_when_override_off():
    p = _profile(urgent_override=False)
    assert quiet_hours.suppress_push(p, urgent=True,
                                     now_utc=datetime(2026, 7, 12, 23, 0)) is True
