"""Offline unit tests for pim calendar update validation.

update_event with an unparseable start/end must raise instead of silently
dropping the field while the caller reports success. DB is monkeypatched.
"""
import asyncio
import sys
from pathlib import Path

import pytest

from .optional_deps import HAS_AIOSQLITE, install_aiosqlite_stub

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

if not HAS_AIOSQLITE:
    install_aiosqlite_stub()

import pim  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _wire(monkeypatch):
    state = {"updated": None}

    async def fake_tz(user_id=None):
        return "UTC"

    async def fake_get(event_id, user_id=None):
        return {"id": event_id, "sync_state": "synced"}

    async def fake_update(event_id, fields, user_id=None):
        state["updated"] = (event_id, fields)
        return {"id": event_id, **fields}

    monkeypatch.setattr(pim, "user_timezone", fake_tz)
    monkeypatch.setattr(pim.db, "get_calendar_event", fake_get)
    monkeypatch.setattr(pim.db, "update_calendar_event", fake_update)
    return state


def test_update_event_invalid_start_raises(monkeypatch):
    state = _wire(monkeypatch)
    with pytest.raises(ValueError):
        _run(pim.update_event("event-x", {"start_local": "3pm tomorrow"}))
    assert state["updated"] is None  # nothing written


def test_update_event_invalid_end_raises(monkeypatch):
    state = _wire(monkeypatch)
    with pytest.raises(ValueError):
        _run(pim.update_event("event-x", {"end_local": "not-a-time"}))
    assert state["updated"] is None


def test_update_event_valid_time_updates_and_rearms_reminder(monkeypatch):
    state = _wire(monkeypatch)
    out = _run(pim.update_event("event-x", {"start_local": "2026-07-14T09:00"}))
    assert out is not None
    _, fields = state["updated"]
    assert fields["start_at"] == "2026-07-14T09:00:00"
    assert fields["reminded"] is False  # moved event re-arms its reminder
