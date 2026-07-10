"""Offline unit tests for the Jarvis scheduler's next-run math.

Covers compute_next_run (daily across a DST transition, weekly rollover,
monthly day-31 clamping, once conversion, event/webhook → None) and the
shared recompute_next_run profile-timezone fallback. No DB, no server.
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from .optional_deps import HAS_AIOSQLITE, install_aiosqlite_stub

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

if not HAS_AIOSQLITE:
    install_aiosqlite_stub()

import scheduler  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ── compute_next_run ─────────────────────────────────────────────────────

def test_event_and_webhook_have_no_next_run():
    assert scheduler.compute_next_run("event", {}, "UTC") is None
    assert scheduler.compute_next_run("webhook", {}, "UTC") is None


def test_once_converts_local_wall_clock_to_utc():
    out = scheduler.compute_next_run(
        "once", {"run_at": "2026-07-10T09:00"}, "America/New_York")
    assert out == "2026-07-10T13:00:00"  # EDT is UTC-4


def test_once_garbage_returns_none():
    assert scheduler.compute_next_run("once", {"run_at": "not-a-date"}, "UTC") is None
    assert scheduler.compute_next_run("once", {}, "UTC") is None


def test_daily_rolls_forward_when_time_already_passed():
    after = datetime(2026, 7, 10, 15, 0)  # 15:00 UTC
    out = scheduler.compute_next_run("daily", {"time": "09:00"}, "UTC", after_utc=after)
    assert out == "2026-07-11T09:00:00"


def test_daily_across_dst_spring_forward_keeps_wall_clock():
    # 2026-03-07 20:00 UTC = 15:00 EST. Next daily 14:30 lands on Mar 8,
    # AFTER the US spring-forward — wall clock must stay 14:30, so the UTC
    # offset shifts from -5 to -4.
    after = datetime(2026, 3, 7, 20, 0)
    out = scheduler.compute_next_run(
        "daily", {"time": "14:30"}, "America/New_York", after_utc=after)
    assert out == "2026-03-08T18:30:00"  # 14:30 EDT (UTC-4), not 19:30


def test_weekly_targets_weekday_and_rolls_a_full_week():
    # 2026-07-10 is a Friday (weekday 4). Asking for Friday 08:00 after
    # Friday 12:00 local must land NEXT Friday.
    after = datetime(2026, 7, 10, 12, 0)
    out = scheduler.compute_next_run(
        "weekly", {"time": "08:00", "weekday": 4}, "UTC", after_utc=after)
    assert out == "2026-07-17T08:00:00"


def test_monthly_day31_clamps_to_short_months():
    after = datetime(2026, 2, 1, 0, 0)
    out = scheduler.compute_next_run(
        "monthly", {"time": "09:00", "day": 31}, "UTC", after_utc=after)
    assert out == "2026-02-28T09:00:00"  # 2026 is not a leap year


def test_monthly_rolls_to_next_month_after_clamped_day():
    after = datetime(2026, 2, 28, 10, 0)
    out = scheduler.compute_next_run(
        "monthly", {"time": "09:00", "day": 31}, "UTC", after_utc=after)
    assert out == "2026-03-31T09:00:00"


def test_bad_timezone_falls_back_to_utc():
    out = scheduler.compute_next_run(
        "once", {"run_at": "2026-07-10T09:00"}, "Not/AZone")
    assert out == "2026-07-10T09:00:00"


# ── recompute_next_run (shared route/tool helper) ────────────────────────

def test_recompute_event_kinds_skip_db(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("profile lookup must not run for event/webhook")
    monkeypatch.setattr(scheduler.db, "get_assistant_profile", boom)
    assert _run(scheduler.recompute_next_run("event", {})) is None
    assert _run(scheduler.recompute_next_run("webhook", {})) is None


def test_recompute_empty_tz_falls_back_to_profile(monkeypatch):
    async def profile(user_id=None):
        return {"timezone": "America/New_York"}
    monkeypatch.setattr(scheduler.db, "get_assistant_profile", profile)
    out = _run(scheduler.recompute_next_run("once", {"run_at": "2026-07-10T09:00"}))
    assert out == "2026-07-10T13:00:00"


def test_recompute_explicit_tz_wins(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("explicit tz must not hit the profile")
    monkeypatch.setattr(scheduler.db, "get_assistant_profile", boom)
    out = _run(scheduler.recompute_next_run(
        "once", {"run_at": "2026-07-10T09:00"}, "UTC"))
    assert out == "2026-07-10T09:00:00"
