"""Offline unit tests for caldav_sync.py hashing/ICS round-trip invariants.

The July 2026 sync bugs all came from the push-side etag disagreeing with what
the next pull computes: synthesized DTEND (end_at=None), all-day date
snapping, missing RRULE on push, and seconds-precision drift. These tests
assert the core invariant directly:

    _pushed_content_hash(_build_ics(payload), payload) ==
    _content_hash(_event_fields_from_vevent(parse(_build_ics(payload))))

Run: python -m pytest tests/test_caldav_sync_unit.py -v
Needs `icalendar` (skips cleanly without it).
"""

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from .optional_deps import HAS_AIOSQLITE, install_aiosqlite_stub

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

if not HAS_AIOSQLITE:
    install_aiosqlite_stub()

icalendar = pytest.importorskip("icalendar")

import caldav_sync  # noqa: E402

TZ = ZoneInfo("America/Los_Angeles")


def _roundtrip_fields(ics_text: str) -> dict:
    """Parse our own generated ICS exactly the way the pull path does."""
    for comp in icalendar.Calendar.from_ical(ics_text).walk("VEVENT"):
        fields = caldav_sync._event_fields_from_vevent(comp, TZ)
        if fields:
            return fields
    raise AssertionError("no VEVENT parsed")


def _event(**over) -> dict:
    base = {
        "caldav_uid": "test-uid@jarvis",
        "title": "Dentist",
        "description": "",
        "location": "",
        "start_at": "2026-07-30T22:00",   # naive UTC (3pm PDT)
        "end_at": None,
        "all_day": 0,
        "rrule": "",
    }
    base.update(over)
    return base


def _push_pull_hashes(payload):
    ics_text = caldav_sync._build_ics(payload, TZ)
    push_hash = caldav_sync._pushed_content_hash(ics_text, payload, TZ)
    pull_hash = caldav_sync._content_hash(_roundtrip_fields(ics_text))
    return push_hash, pull_hash


def test_timed_event_without_end_roundtrips():
    # _build_ics synthesizes DTEND=start+1h — the stored etag must match what
    # the pull recomputes, or the event re-pulls forever (the B2 bug).
    push, pull = _push_pull_hashes(_event())
    assert push == pull


def test_timed_event_with_end_roundtrips():
    push, pull = _push_pull_hashes(_event(end_at="2026-07-30T23:30"))
    assert push == pull


def test_all_day_event_roundtrips():
    # All-day: stored start is local-midnight-as-UTC; DTSTART becomes a date
    # and DTEND is synthesized (+1 day) when end_at is None.
    push, pull = _push_pull_hashes(_event(start_at="2026-07-30T07:00", all_day=1))
    assert push == pull


def test_recurring_event_keeps_rrule():
    # B3: _build_ics must emit RRULE — _content_hash includes it, so a push
    # that drops it makes the pull-back wipe local recurrence.
    payload = _event(rrule="FREQ=WEEKLY;BYDAY=WE")
    ics_text = caldav_sync._build_ics(payload, TZ)
    fields = _roundtrip_fields(ics_text)
    assert "FREQ=WEEKLY" in fields["rrule"].upper()
    push, pull = _push_pull_hashes(payload)
    assert push == pull


def test_bad_rrule_does_not_block_push():
    payload = _event(rrule="NOT-A-RULE;;;")
    ics_text = caldav_sync._build_ics(payload, TZ)  # must not raise
    assert "SUMMARY:Dentist" in ics_text


def test_content_hash_seconds_precision_drift():
    # Locally-created rows store minute precision; pull-back isoformat carries
    # seconds. Identical instants must hash identically.
    a = caldav_sync._content_hash(_event(start_at="2026-07-30T22:00"))
    b = caldav_sync._content_hash(_event(start_at="2026-07-30T22:00:00"))
    assert a == b


def test_content_hash_detects_real_change():
    a = caldav_sync._content_hash(_event(title="Dentist"))
    b = caldav_sync._content_hash(_event(title="Dentist MOVED"))
    assert a != b


def test_pushed_hash_falls_back_on_garbage_ics():
    payload = _event()
    fallback = caldav_sync._pushed_content_hash("not an ics document", payload, TZ)
    assert fallback == caldav_sync._content_hash(payload)


def test_legacy_etag_migration_guard_semantics():
    # B4: content-equal local row vs remote snapshot must be detectable by
    # comparing _content_hash(local) to the remote hash — this is what lets
    # sync_account backfill legacy sha256(raw_ics) etags without a conflict.
    payload = _event(end_at="2026-07-30T23:00")
    ics_text = caldav_sync._build_ics(payload, TZ)
    remote_fields = _roundtrip_fields(ics_text)
    remote_hash = caldav_sync._content_hash(remote_fields)
    local_row = {**payload, "caldav_etag": "legacy-raw-ics-hash"}
    assert local_row["caldav_etag"] != remote_hash          # etag mismatch...
    assert caldav_sync._content_hash(local_row) == remote_hash  # ...but same content
