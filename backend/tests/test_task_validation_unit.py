"""Task edits must reject unusable schedules before any database write."""
import asyncio
import sys
from pathlib import Path

import pytest

from .optional_deps import load_route_module

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("aiosqlite")
pytest.importorskip("fastapi")
from fastapi import HTTPException
import scheduler


@pytest.fixture
def task_store(monkeypatch):
    task = {
        "id": "task-test", "title": "Reminder", "prompt": "Check calendar",
        "task_type": "llm", "schedule_kind": "daily", "schedule_json": {"time": "09:00"},
        "timezone": "UTC", "enabled": True, "next_run": "2027-01-01T09:00:00",
        "event_trigger_json": {}, "webhook_token": "",
    }
    writes = []

    async def get(task_id):
        return dict(task)

    async def update(task_id, fields):
        writes.append(fields)
        return {**task, **fields}

    async def create(task_id, **fields):
        writes.append(fields)
        return {"id": task_id, **fields}

    async def profile(**kwargs):
        return {"timezone": "America/Phoenix"}

    monkeypatch.setattr(scheduler.db, "get_scheduled_task", get)
    monkeypatch.setattr(scheduler.db, "update_scheduled_task", update)
    monkeypatch.setattr(scheduler.db, "create_scheduled_task", create)
    monkeypatch.setattr(scheduler.db, "get_assistant_profile", profile)
    return task, writes


BAD_SCHEDULES = [
    ("once", {"run_at": "not-a-date"}),
    ("cron", {"cron": "not a cron"}),
    ("weekly", {"weekday": "Friday"}),
    ("weekly", {"weekday": 7}),
    ("monthly", {"day": 0}),
    ("monthly", {"day": 1.5}),
    ("daily", {"time": "25:00"}),
    ("daily", {"time": "09:99"}),
    ("daily", {"time": "tomorrow"}),
    ("unknown", {}),
]


@pytest.mark.parametrize("kind,schedule", BAD_SCHEDULES)
@pytest.mark.parametrize("action", ["create", "update", "resume"])
def test_invalid_route_schedule_never_writes(monkeypatch, task_store, kind, schedule, action):
    route = load_route_module(monkeypatch, "scheduler")
    task, writes = task_store
    if action == "create":
        call = route.create_task(route.TaskCreate(title="Reminder", schedule_kind=kind, schedule_json=schedule))
    elif action == "update":
        call = route.update_task(task["id"], route.TaskUpdate(schedule_kind=kind, schedule_json=schedule))
    else:
        task.update(schedule_kind=kind, schedule_json=schedule, enabled=False)
        call = route.resume_task(task["id"])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(call)
    assert exc.value.status_code == 400
    assert writes == []


def test_enable_patch_validates_saved_schedule(monkeypatch, task_store):
    route = load_route_module(monkeypatch, "scheduler")
    task, writes = task_store
    task.update(schedule_kind="once", schedule_json={}, enabled=False)
    with pytest.raises(HTTPException):
        asyncio.run(route.update_task(task["id"], route.TaskUpdate(enabled=True)))
    assert writes == []


@pytest.mark.parametrize("kind,trigger", [("event", {"event": "email_received"}), ("webhook", {})])
def test_event_and_webhook_resume_need_no_timestamp(monkeypatch, task_store, kind, trigger):
    route = load_route_module(monkeypatch, "scheduler")
    task, writes = task_store
    task.update(schedule_kind=kind, schedule_json={}, event_trigger_json=trigger, enabled=False)
    result = asyncio.run(route.resume_task(task["id"]))
    assert result["enabled"] is True
    assert result["next_run"] is None
    assert len(writes) == 1


def test_event_without_valid_trigger_cannot_resume(monkeypatch, task_store):
    route = load_route_module(monkeypatch, "scheduler")
    task, writes = task_store
    task.update(schedule_kind="event", event_trigger_json={})
    with pytest.raises(HTTPException):
        asyncio.run(route.resume_task(task["id"]))
    assert writes == []


def test_timezone_fallback_and_checkin_type_are_preserved(monkeypatch, task_store):
    route = load_route_module(monkeypatch, "scheduler")
    task, writes = task_store
    task.update(task_type="check_in", timezone="")
    result = asyncio.run(route.update_task(task["id"], route.TaskUpdate(
        schedule_kind="once", schedule_json={"run_at": "2027-01-01T09:00"})))
    assert result["next_run"] == "2027-01-01T16:00:00"
    assert result["task_type"] == "check_in"
    writes.clear()
    with pytest.raises(HTTPException):
        asyncio.run(route.update_task(task["id"], route.TaskUpdate(task_type="llm")))
    assert writes == []


@pytest.mark.parametrize("action", ["create", "update", "resume"])
@pytest.mark.parametrize("kind,schedule", BAD_SCHEDULES)
def test_invalid_tool_schedule_never_writes(task_store, action, kind, schedule):
    import tools
    task, writes = task_store
    args = {"action": action, "task_id": task["id"], "title": "Reminder", "prompt": "Check calendar",
            "schedule_kind": kind, **schedule}
    if action == "resume":
        task.update(schedule_kind=kind, schedule_json=schedule, enabled=False)
    result = asyncio.run(tools.exec_tool(None, None, "manage_tasks", args, ""))
    assert result.startswith("ERROR:"), result
    assert writes == []


def test_tool_can_change_to_valid_event_schedule(task_store):
    import tools
    task, writes = task_store
    result = asyncio.run(tools.exec_tool(None, None, "manage_tasks", {
        "action": "update", "task_id": task["id"], "schedule_kind": "event", "event": "email_received",
    }, ""))
    assert result.startswith("Updated task:"), result
    assert writes[0]["next_run"] is None
    assert writes[0]["event_trigger_json"]["event"] == "email_received"
