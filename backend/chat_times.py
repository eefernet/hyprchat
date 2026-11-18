"""When each message of a chat was written, for the model.

The model is told today's date and nothing else about time. It cannot tell a message from this morning
from one sent three weeks ago in the same chat, which matters most in the long-running Assistant chat
("this morning's brief", "what did I ask yesterday?", "how long has this been open?").

Each user message is sent with one short line in front: when it was written, in the owner's time zone,
and how long after the previous message when that is more than a few hours:

    [Sunday 15 November 2026, 3:42 PM · 3 days after the previous message]

The time is read from the saved row, so a message carries the same line on every later turn and Ollama's
prompt cache stays valid (a line built from "now" would change the prompt each time). Nothing changes in
what the user sees or in what is saved. Off for Daedalus coding chats and ghost chats, and by a setting.
"""
from __future__ import annotations

from datetime import datetime, timezone

import config
import database as db
import timeutil

GAP_HOURS = 6       # a pause at least this long is spelled out
INSTRUCTION = ("A line in square brackets at the start of a user message says when that message was sent, and how "
               "long after the previous one when there was a long pause. Use these lines to reason about time. "
               "Do not repeat or mention them.")


def enabled() -> bool:
    return str(getattr(config, "CHAT_TIME_NOTES", "on")).lower() == "on"


def parse(value) -> datetime | None:
    """A saved timestamp as naive UTC. Rows hold ISO text, with or without a zone, 'T' or a space."""
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


def gap_words(seconds: float) -> str:
    """'7 hours', '1 day', '3 days', '2 weeks', '5 months', '2 years'. Empty for a short pause.
    Rounded, not cut: a pause one second short of three days is three days."""
    hours = seconds / 3600
    if hours < GAP_HOURS:
        return ""

    def many(count: int, name: str) -> str:
        return f"{count} {name}{'' if count == 1 else 's'}"
    if round(hours) < 24:
        return many(round(hours), "hour")
    days = round(hours / 24)
    if days < 14:
        return many(days, "day")
    if days < 60:
        return many(round(days / 7), "week")
    if days < 365:
        return many(round(days / 30), "month")
    return many(round(days / 365), "year")


def line(when: datetime, previous: datetime | None, zone) -> str:
    """The line for a message written at ``when`` (naive UTC), after one written at ``previous``."""
    local = when.replace(tzinfo=timezone.utc).astimezone(zone)
    hour = local.hour % 12 or 12
    text = f"{local:%A} {local.day} {local:%B} {local.year}, {hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    gap = gap_words((when - previous).total_seconds()) if previous and when > previous else ""
    return f"[{text} · {gap} after the previous message]" if gap else f"[{text}]"


async def times(conversation_id: str) -> dict[int, datetime]:
    """When each saved message of the conversation was written. Only the owner's own conversation."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT m.id, m.created_at FROM messages m JOIN conversations c ON c.id = m.conversation_id
               WHERE m.conversation_id = ? AND c.user_id = ?""", (conversation_id, db._scope_user()))
    finally:
        await conn.close()
    found = {}
    for row in rows:
        moment = parse(row["created_at"])
        if moment:
            found[int(row["id"])] = moment
    return found


def lines_for(request_messages, saved: dict[int, datetime], zone, newest_id=None) -> dict[int, str]:
    """Position in the request -> line, for the user messages whose time is known."""
    last_user = max((index for index, message in enumerate(request_messages) if message.get("role") == "user"), default=-1)
    out, previous = {}, None
    for index, message in enumerate(request_messages):
        try:
            identifier = int(message.get("id") or 0)
        except (TypeError, ValueError):
            identifier = 0
        if not identifier and index == last_user and newest_id:      # the message being sent: saved a moment ago
            identifier = int(newest_id)
        when = saved.get(identifier)
        if when is None:
            continue
        if message.get("role") == "user":
            out[index] = line(when, previous, zone)
        previous = when
    return out


async def stamps(conversation_id: str, request_messages, *, newest_id=None) -> dict[int, str]:
    """Position in the request -> the line to put in front of that user message. Empty when switched off."""
    if not enabled() or not conversation_id:
        return {}
    saved = await times(conversation_id)
    if not saved:
        return {}
    profile = await db.get_assistant_profile()
    zone = timeutil.safe_zone((profile or {}).get("timezone") or "UTC")
    return lines_for(request_messages, saved, zone, newest_id)
