"""Jarvis PIM — notes/todos + calendar business logic.

Times are stored naive-UTC; conversion to/from the user's wall clock happens
here at the edges using the assistant-profile timezone. The reminder scan
rides the scheduler tick (cross-user select, per-row user contextvar). The
calendar/notes check-in gatherers register with agents.assistant at import
time (routes import this module at startup).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import database as db
import notifications
from agents import assistant as assistant_mod
# Shared conversions (timeutil owns the single implementation); local_to_utc /
# utc_to_local stay importable from pim for existing callers.
from timeutil import local_to_utc, utc_to_local, safe_zone as _tz


async def user_timezone(user_id: str | None = None) -> str:
    profile = await db.get_assistant_profile(user_id=user_id)
    return (profile or {}).get("timezone") or "UTC"


# ------------------------------------------------------------------
# notes / todos
# ------------------------------------------------------------------

async def create_note(*, kind: str = "note", title: str, content: str = "",
                      due_local: str = "", remind_local: str = "",
                      tags: list | None = None, user_id: str | None = None) -> dict | None:
    tz = await user_timezone(user_id)
    return await db.create_note(
        f"note-{uuid.uuid4().hex[:10]}", kind="todo" if kind == "todo" else "note",
        title=title, content=content,
        due_at=local_to_utc(due_local, tz), remind_at=local_to_utc(remind_local, tz),
        tags=tags, user_id=user_id)


# ------------------------------------------------------------------
# calendar
# ------------------------------------------------------------------

async def create_event(*, title: str, start_local: str, end_local: str = "",
                       description: str = "", location: str = "",
                       all_day: bool = False, remind_minutes: int | None = None,
                       source: str = "", user_id: str | None = None) -> dict | None:
    tz = await user_timezone(user_id)
    start_at = local_to_utc(start_local, tz)
    if not start_at:
        raise ValueError(f"invalid start time: {start_local!r} (use YYYY-MM-DDTHH:MM)")
    end_at = local_to_utc(end_local, tz) or (
        datetime.fromisoformat(start_at) + timedelta(hours=1)).isoformat()
    desc = description
    if source:
        desc = (desc + f"\n[source: {source}]").strip()
    return await db.create_calendar_event(
        f"event-{uuid.uuid4().hex[:10]}", title=title, start_at=start_at,
        end_at=end_at, description=desc, location=location, all_day=all_day,
        remind_minutes=remind_minutes, sync_state="dirty", user_id=user_id)


async def update_event(event_id: str, fields: dict, user_id: str | None = None) -> dict | None:
    """Route/tool-facing update: local times in, marks the row dirty for CalDAV."""
    tz = await user_timezone(user_id)
    mapped: dict = {}
    for key in ("title", "description", "location", "all_day"):
        if key in fields and fields[key] is not None:
            mapped[key] = fields[key]
    # remind_minutes present-with-None means CLEAR the reminder (the route
    # sends an exclude_unset dump, so the key only appears when explicitly
    # sent — otherwise a set reminder could never be removed).
    if "remind_minutes" in fields:
        mapped["remind_minutes"] = fields["remind_minutes"]
    if fields.get("start_local"):
        start = local_to_utc(fields["start_local"], tz)
        if not start:
            raise ValueError(f"Invalid start_local {fields['start_local']!r} — use YYYY-MM-DDTHH:MM")
        mapped["start_at"] = start
    if fields.get("end_local"):
        end = local_to_utc(fields["end_local"], tz)
        if not end:
            raise ValueError(f"Invalid end_local {fields['end_local']!r} — use YYYY-MM-DDTHH:MM")
        mapped["end_at"] = end
    # Re-arm the reminder whenever the event moves or the reminder changes —
    # without this a rescheduled event keeps reminded=1 and never fires again.
    if "start_at" in mapped or "remind_minutes" in mapped:
        mapped["reminded"] = False
    if mapped:
        existing = await db.get_calendar_event(event_id, user_id=user_id)
        if existing and existing.get("sync_state") in ("synced", "dirty"):
            mapped["sync_state"] = "dirty"
    return await db.update_calendar_event(event_id, mapped, user_id=user_id)


async def delete_event(event_id: str, user_id: str | None = None) -> bool:
    """Tombstone synced events (remote delete happens on next sync); hard-delete
    purely local ones."""
    event = await db.get_calendar_event(event_id, user_id=user_id)
    if not event:
        return False
    if event.get("caldav_uid"):
        await db.update_calendar_event(event_id, {"sync_state": "deleted"}, user_id=user_id)
        return True
    return await db.delete_calendar_event(event_id, user_id=user_id)


# ------------------------------------------------------------------
# reminder scan (rides the scheduler tick)
# ------------------------------------------------------------------

async def reminder_scan() -> int:
    """Fire due note/event reminders. Cross-user select; per-row contextvar."""
    fired = 0
    due = await db.due_reminders(datetime.utcnow().isoformat())
    for note in due["notes"]:
        token = db.set_current_user_id(note["user_id"])
        try:
            tz = await user_timezone(note["user_id"])
            when = utc_to_local(note.get("due_at") or note.get("remind_at") or "", tz)
            await notifications.notify(
                f"Reminder: {note['title']}",
                (note.get("content") or "")[:300] + (f"\n(due {when})" if when else ""),
                kind="reminder", ntfy=True)
            await db.update_note(note["id"], {"reminded": True})
            fired += 1
        except Exception as e:
            print(f"[PIM] note reminder failed for {note['id']}: {e}")
        finally:
            db.reset_current_user_id(token)
    for event in due["events"]:
        token = db.set_current_user_id(event["user_id"])
        try:
            tz = await user_timezone(event["user_id"])
            when = utc_to_local(event.get("start_at") or "", tz)
            loc = f" @ {event['location']}" if event.get("location") else ""
            await notifications.notify(
                f"Upcoming: {event['title']}",
                f"{when}{loc}", kind="reminder", ntfy=True)
            await db.update_calendar_event(event["id"], {"reminded": True})
            fired += 1
        except Exception as e:
            print(f"[PIM] event reminder failed for {event['id']}: {e}")
        finally:
            db.reset_current_user_id(token)
    return fired


# ------------------------------------------------------------------
# check-in gatherers (Odysseus-style: 3 calendar windows + open todos)
# ------------------------------------------------------------------

async def _gather_calendar(user_id: str) -> str:
    tz_name = await user_timezone(user_id)
    tz = _tz(tz_name)
    now_local = datetime.now(tz).replace(tzinfo=None)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    def _utc(dt: datetime) -> str:
        return dt.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None).isoformat()

    windows = [
        ("Today + tomorrow", day_start, day_start + timedelta(days=2), True),
        ("This week", day_start + timedelta(days=2), day_start + timedelta(days=7), True),
        ("Next 30 days", day_start + timedelta(days=7), day_start + timedelta(days=30), False),
    ]
    lines: list[str] = []
    for label, w_start, w_end, detailed in windows:
        events = await db.list_calendar_events(
            start=_utc(w_start), end=_utc(w_end), user_id=user_id)
        if not events:
            continue
        lines.append(f"{label}:")
        for e in events[: 20 if detailed else 8]:
            when = utc_to_local(e.get("start_at") or "", tz_name)
            loc = f" @ {e['location']}" if detailed and e.get("location") else ""
            lines.append(f"  - {when} {e['title']}{loc}")
    return "\n".join(lines)


async def _gather_notes(user_id: str) -> str:
    todos = await db.list_notes(kind="todo", include_done=False, limit=15, user_id=user_id)
    notes = await db.list_notes(kind="note", limit=5, user_id=user_id)
    tz = await user_timezone(user_id)
    lines: list[str] = []
    if todos:
        lines.append("Open todos:")
        for n in todos:
            due = f" (due {utc_to_local(n['due_at'], tz)})" if n.get("due_at") else ""
            lines.append(f"  - {n['title']}{due}")
    if notes:
        lines.append("Recent notes:")
        for n in notes:
            lines.append(f"  - {n['title']}: {(n.get('content') or '')[:80]}")
    return "\n".join(lines)


assistant_mod.register_gatherer("calendar", _gather_calendar)
assistant_mod.register_gatherer("notes", _gather_notes)
