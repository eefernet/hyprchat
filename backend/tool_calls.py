"""Which tool call an event belongs to.

One tool call sends several events: the chat loop's "start", the tool's own "start", a progress line
every few seconds, an "end", an output card. They had nothing in common, so the browser could not tell
one call's five events from five calls. Seen live 2026-11-14: the Activity badge said "26 tools" for
twelve code runs and one search, and an output card could not be matched with the code that produced it.

The chat loop opens a scope at the top of the task that runs a tool. ``EventBus.emit`` then stamps the
id on every tool event sent from that task. asyncio gives each task its own copy of the context, so
tools that run in parallel keep their own ids. The scope is closed when the tool returns: a background
job the tool started inherits the copied context and must not go on stamping a call that is over.
"""
from __future__ import annotations

import contextvars
import json
import secrets

# Events that describe a tool call or something it produced.
STAMPED = frozenset({
    "tool_start", "tool_progress", "tool_status", "tool_end", "tool_done", "tool_error",
    "code_output", "file_ready",
})

_CALL: contextvars.ContextVar = contextvars.ContextVar("hyprchat_tool_call", default=None)


def new_id(round_number: int = 0) -> str:
    """An id for one tool call of this reply. Not the provider's id, which pairs a result with its call
    in the model's messages and is absent for local models."""
    return f"c{int(round_number)}-{secrets.token_hex(4)}"


def begin(call_id: str) -> dict:
    """Open the scope for the current task. Call it inside the task that runs the tool."""
    holder = {"id": str(call_id or ""), "open": True}
    _CALL.set(holder)
    return holder


def end(holder) -> None:
    if isinstance(holder, dict):
        holder["open"] = False


def current() -> str:
    holder = _CALL.get()
    return holder["id"] if holder and holder.get("open") else ""


def description(arguments) -> str:
    """The one-line description a model gave with a call ("Count the lines of Python"), cleaned for
    display. Empty when it gave none."""
    value = arguments.get("description") if isinstance(arguments, dict) else ""
    return " ".join(str(value or "").split())[:120]


def comparable(name: str, arguments):
    """The arguments as the duplicate-call check compares them. The description is a label: the same
    code with a new description is still the same call."""
    if name != "execute_code":
        return arguments
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return arguments
    if isinstance(arguments, dict) and "description" in arguments:
        return {key: value for key, value in arguments.items() if key != "description"}
    return arguments


def stamp(event_type: str, data):
    """``data`` with the running call's id added. Returns the same object when there is nothing to add;
    never changes the caller's dict."""
    call_id = current()
    if not call_id or event_type not in STAMPED or not isinstance(data, dict) or data.get("tool_call_id"):
        return data
    return {**data, "tool_call_id": call_id}
