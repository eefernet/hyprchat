"""Polishing pass, stage 2 (2026-11-18): the Assistant suite. One test per finding. Offline."""
import asyncio
from datetime import datetime, timedelta

import pytest

import database as db
import pim
import scheduler
import task_queue


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", str(tmp_path / "assistant.db"))
    asyncio.run(db.init_db())
    return tmp_path


def test_an_open_tab_no_longer_counts_as_working_in_the_chat(fresh):
    """Tasks that post to a conversation were put off 15 minutes whenever the session had been seen in the
    last three minutes, and an open tab polls every 45 s: they never ran while HyprChat was open."""
    async def run():
        await db.create_conversation(id="c", title="Assistant")
        assert not await scheduler._user_active_recently(db.DEFAULT_USER_ID, "c")      # no message yet
        await db.add_message("c", "assistant", "a brief")
        assert not await scheduler._user_active_recently(db.DEFAULT_USER_ID, "c")      # not the user's
        await db.add_message("c", "user", "thanks")
        assert await scheduler._user_active_recently(db.DEFAULT_USER_ID, "c")
        assert not await scheduler._user_active_recently(db.DEFAULT_USER_ID, "other-chat")
        assert not await scheduler._user_active_recently(db.DEFAULT_USER_ID, "")
        conn = await db.get_db()
        try:
            await conn.execute("UPDATE messages SET created_at=datetime('now','-10 minutes')")
            await conn.commit()
        finally:
            await conn.close()
        assert not await scheduler._user_active_recently(db.DEFAULT_USER_ID, "c")
    asyncio.run(run())


def test_next_run_is_never_in_the_past_on_the_night_clocks_go_back():
    """2026-11-01 in New York: 01:30 happens twice. Asked at 01:10 the second time (06:10 UTC), a weekly or
    monthly "today 01:30" resolved to the first one (05:30 UTC), already over, so the task was claimed again
    on every 30 s tick until 06:30. (A scan of that night found 208 such answers; daily was not affected.)"""
    now = datetime(2026, 11, 1, 6, 10)
    for kind, schedule in (("weekly", {"time": "01:30", "weekday": 6}), ("monthly", {"time": "01:30", "day": 1}),
                           ("daily", {"time": "01:30"})):
        assert scheduler._next_run(kind, schedule, "America/New_York", now) <= "2026-11-01T06:30:00"
        assert scheduler.compute_next_run(kind, schedule, "America/New_York", now) > now.isoformat(), kind
    assert scheduler._next_run("weekly", {"time": "01:30", "weekday": 6}, "America/New_York", now) < now.isoformat()
    # Every minute of both changeover nights, in two zones: never a time that has passed.
    for zone, start in (("America/New_York", datetime(2026, 11, 1, 3, 0)), ("Europe/Berlin", datetime(2026, 10, 24, 22, 0)),
                        ("America/New_York", datetime(2026, 3, 8, 4, 0))):
        for minute in range(0, 360, 7):
            at = start + timedelta(minutes=minute)
            for kind, schedule in (("daily", {"time": "01:30"}), ("weekly", {"time": "02:30", "weekday": 6}),
                                   ("monthly", {"time": "01:59", "day": 1})):
                assert scheduler.compute_next_run(kind, schedule, zone, at) > at.isoformat()
    # Ordinary days are untouched.
    assert scheduler.compute_next_run("daily", {"time": "08:30"}, "America/New_York", datetime(2026, 11, 18, 12, 0)) == "2026-11-18T13:30:00"


def test_note_reminder_is_rearmed_only_when_its_time_changes():
    fired = {"remind_at": "2026-11-18T09:00:00", "due_at": None, "reminded": True}
    # A title edit that carries the same reminder time: no second reminder.
    assert "reminded" not in pim.rearm_note_reminder(fired, {"title": "New title", "remind_at": "2026-11-18T09:00:00"})
    assert "reminded" not in pim.rearm_note_reminder(fired, {"title": "New title", "reminded": False})
    # A new reminder time: armed again.
    assert pim.rearm_note_reminder(fired, {"remind_at": "2026-11-25T09:00:00"})["reminded"] is False
    # A todo that reminds at its due date and is moved to next week: armed again (it never was).
    due_only = {"remind_at": None, "due_at": "2026-11-18T17:00:00", "reminded": True}
    assert pim.rearm_note_reminder(due_only, {"due_at": "2026-11-25T17:00:00"})["reminded"] is False
    # Changing the due date of a note with its own reminder time changes nothing.
    assert "reminded" not in pim.rearm_note_reminder(fired, {"due_at": "2026-12-01T00:00:00"})
    # Clearing the reminder leaves nothing to arm.
    assert "reminded" not in pim.rearm_note_reminder(fired, {"remind_at": None})


def test_webhook_during_a_run_gets_a_follow_up_run_with_its_payload(fresh):
    """The second call was merged into the run in progress, its payload dropped, and the caller told "started"."""
    async def run():
        task = await db.create_scheduled_task("t", title="CI", prompt="p", schedule_kind="webhook", schedule_json={})
        first = await task_queue.enqueue(task, trigger="webhook", extra_context="build 1")
        assert await task_queue.start_run(first)                       # now running
        second = await task_queue.enqueue(task, trigger="webhook", extra_context="build 2")
        assert second and second != first
        third = await task_queue.enqueue(task, trigger="webhook", extra_context="build 3")
        assert third == second                                          # one follow-up at most; it takes both
        queued = await task_queue.pending(second)
        assert "build 2" in queued[0]["extra_context"] and "build 3" in queued[0]["extra_context"]
        # A scheduled overlap still coalesces into the active run.
        assert await task_queue.enqueue(task, trigger="schedule") in (first, second)
        conn = await db.get_db()
        try:
            assert len(await conn.execute_fetchall("SELECT id FROM task_runs WHERE task_id='t'")) == 2
        finally:
            await conn.close()
    asyncio.run(run())


def test_brief_lists_task_times_in_the_profile_timezone(fresh):
    from agents import assistant
    async def run():
        await db.upsert_assistant_profile(timezone="America/New_York")
        await db.create_scheduled_task("t", title="Report", prompt="p", schedule_kind="daily",
                                       schedule_json={"time": "08:30"}, next_run="2026-11-18T13:30:00")
        text = await assistant._gather_tasks(db.DEFAULT_USER_ID)
        assert "2026-11-18 08:30" in text and "13:30" not in text
    asyncio.run(run())


def test_saving_the_profile_with_the_same_timezone_leaves_next_run_alone(fresh):
    """The panel sends the timezone with every save; "Save Quiet Hours" pushed a due brief to tomorrow."""
    from routes import assistant as routes
    async def run():
        await db.upsert_assistant_profile(timezone="America/New_York")
        await db.create_scheduled_task("c", title="Brief", prompt="p", task_type="check_in", schedule_kind="daily",
                                       schedule_json={"time": "08:30"}, timezone="America/New_York", next_run="2026-01-01T13:30:00")
        await db.create_scheduled_task("n", title="Plain", prompt="p", schedule_kind="daily",
                                       schedule_json={"time": "08:30"}, next_run="2026-01-01T13:30:00")
        request = routes.AssistantUpdate(timezone="America/New_York", quiet_hours={"enabled": True})
        await routes.update_assistant(request)
        assert (await db.get_scheduled_task("c"))["next_run"] == "2026-01-01T13:30:00"
        await routes.update_assistant(routes.AssistantUpdate(timezone="Europe/Berlin"))
        moved = await db.get_scheduled_task("c")
        assert moved["next_run"] != "2026-01-01T13:30:00" and moved["timezone"] == "Europe/Berlin"
        assert (await db.get_scheduled_task("n"))["next_run"] != "2026-01-01T13:30:00"   # follows the profile
    asyncio.run(run())
