"""What a chat keeps of its tools' results after the reply that ran them.

Until 2026-11-15 a reply's tool calls and results were thrown away when it ended: only its visible text
was sent on the next turn. Seen live: "continue" after a reply that had read a whole repository started
again from 8k tokens and cloned the repository a second, third and fourth time.

Each tool call is stored with the reply that made it (``db/tool_memory.py``). On later turns the server
adds a short record of them to the message that asked, in front of the reply's text:

    [Results of the tools you ran while answering this message. ...]
    #1 execute_code: Count the lines (ok)
    code: ...
    result: ...
    [End of tool results]

A record, not a replay of the raw tool messages: it is the same for native, text-style and cloud tool
calling, and it adds no messages, so compaction still finds the sent history where it expects it.

Never stored: what the email tools, connector tools and the chat-history search return (email bodies
are never stored anywhere; connector output belongs to another system). After such a call the rest of
the turn keeps one line per call, because later code or arguments can contain what it returned.
Nothing is kept for Ghost chats, Daedalus chats, scheduled runs or very small windows.

Everything here fails open: a database error, a bad row or a full budget never fails the reply.
"""
from __future__ import annotations

import json
import re

import chat_context
import config
import context_inspector
import database as db
from db import tool_memory as store

OPEN = ("[Results of the tools you ran while answering this message. They are kept so you do not have to "
        "repeat the work. The user did not write this and cannot see it.]")
CLOSE = "[End of tool results]"

# Results that are never stored.
PRIVATE = frozenset({"list_emails", "read_email", "send_email", "reply_to_email", "archive_email",
                     "mark_email_read", "delete_email", "search_history"})
# Tools whose arguments or results are user content that is kept elsewhere (an image prompt, a skill).
HEADER_ONLY = frozenset({"generate_image", "skill_view", "skill_manage"})
FULL, HEADER, NOT_KEPT = 1, 2, 0

RESULT_CHARS, HEAD_CHARS, TAIL_CHARS = 4000, 1500, 2500
LONG_ARGUMENTS = {"code": 1500, "content": 1500}
ARGUMENT_CHARS = 300
STORED_CHARS = 48_000                    # per reply; later calls keep one line
MIN_WINDOW = 8192                        # below this there is no room to carry results
MIN_SHORTENED = 600                      # a record with less room than this lists calls without their output
TURN_SHARE, TURN_MIN, TURN_MAX = 0.3, 1500, 24_000   # characters of record sent for one earlier reply
TOTAL_SHARE, TOTAL_SHARE_UNCOMPACTED = 1.2, 0.45     # ... and for all of them, as characters per window token


def enabled() -> bool:
    return str(getattr(config, "CHAT_TOOL_MEMORY", "on")).lower() in {"1", "true", "on", "yes"}


def usable(window) -> bool:
    return enabled() and int(window or 0) >= MIN_WINDOW


def turn_budget(window) -> int:
    return int(min(TURN_MAX, max(TURN_MIN, int(window or 0) * TURN_SHARE)))


def total_budget(window) -> int:
    share = TOTAL_SHARE if chat_context.enabled() else TOTAL_SHARE_UNCOMPACTED    # nothing folds them when compaction is off
    return int(int(window or 0) * share)


_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")


def redact(value) -> str:
    """The shared redactor, plus the token after "Bearer" (it removes the word and leaves the token)."""
    return context_inspector.redact(_BEARER.sub("Bearer [credential removed]", str(value or "")))


def _clip(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n[... {len(text) - head - tail} characters left out ...]\n{text[-tail:] if tail else ''}"


def trim_result(result) -> str:
    return _clip(redact(result), HEAD_CHARS, TAIL_CHARS)


def trim_arguments(arguments) -> dict:
    out = {}
    for key, value in (arguments or {}).items() if isinstance(arguments, dict) else ():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        out[str(key)] = _clip(redact(text), LONG_ARGUMENTS.get(key, ARGUMENT_CHARS), 0)
    return out


def failed(result) -> bool:
    return str(result or "").lstrip().startswith(("ERROR", "Error:", "**FAILED", "**Tool error", "BLOCKED"))


def _arguments(row) -> dict:
    try:
        value = json.loads(row.get("arguments") or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def entry(number: int, row: dict, full: bool, room: int = 0) -> str:
    """One call as it is shown to the model. ``room``: shorten the code and result to about this many
    characters (the newest call of a reply is never left out entirely for want of room)."""
    tool, status = row.get("tool") or "tool", row.get("status") or "ok"
    if int(row.get("kept") or 0) == NOT_KEPT:
        return (f"#{number} {tool}: result not kept (email, connector and chat-search results are never stored). "
                "Call the tool again if you need it.")
    if int(row.get("kept") or 0) != FULL:
        return f"#{number} {tool} ({status}): details not kept."
    arguments = _arguments(row)
    described = " ".join(str(arguments.pop("description", "") or "").split())
    head = f"#{number} {tool}{': ' + described if described else ''} ({status})"
    if not full:
        return head + ": details left out to save room."
    lines = [head]
    code, result = arguments.pop("code", None), str(row.get("result") or "")
    if room:
        code = _clip(str(code), max(80, room // 4), 0) if code else code
        result = _clip(result, max(80, room // 4), max(120, room // 2))
    if arguments:
        lines.append("arguments: " + json.dumps(arguments, ensure_ascii=False))
    if code:
        lines.append("code:\n" + str(code))
    if result:
        lines.append("result:\n" + result)
    return "\n".join(lines)


def digest(rows, budget: int) -> str:
    """The record of one reply's tool calls. The newest calls are given in full while the budget lasts;
    older ones keep their line. The same rows and budget always give the same text (the prompt cache
    depends on it)."""
    rows = list(rows or [])
    if not rows:
        return ""
    full, spent = set(), 0
    for index in range(len(rows) - 1, -1, -1):
        size = len(entry(index + 1, rows[index], True))
        if spent + size > budget:
            break
        full.add(index)
        spent += size
    # A small window cannot hold even the newest call in full: give it shortened instead of not at all.
    last = len(rows) - 1
    shortened = last if not full and budget >= MIN_SHORTENED and int(rows[last].get("kept") or 0) == FULL else -1
    body = "\n".join(entry(index + 1, row, index in full or index == shortened, room=budget if index == shortened else 0)
                     for index, row in enumerate(rows))
    return f"{OPEN}\n{body}\n{CLOSE}"


def records(rows_by_message: dict, order, window) -> dict[int, str]:
    """message id -> record, for the replies in ``order`` (oldest first). Newer replies are served
    first from the overall budget; a reply that no longer fits keeps one line per call."""
    left, per_turn, out = total_budget(window), turn_budget(window), {}
    for message_id in reversed(list(order)):
        rows = rows_by_message.get(message_id)
        if not rows:
            continue
        text = digest(rows, min(per_turn, max(0, left)))
        out[message_id] = text
        left -= len(text)
    return out


async def load_for(conversation_id: str, request_messages, *, window, after: int = 0) -> dict[int, str]:
    """The record to send in front of each earlier reply of this request, keyed by the reply's id.
    ``after``: replies at or before this id are inside the summary and are not sent."""
    try:
        if not usable(window) or not conversation_id:
            return {}
        order = [chat_context.message_id(message.get("id")) for message in request_messages or ()
                 if isinstance(message, dict) and message.get("role") == "assistant"]
        order = [value for value in order if value and value > int(after or 0)]
        if not order:
            return {}
        return records(await store.for_messages(conversation_id, order), order, window)
    except Exception as error:
        print(f"[TOOLMEM] could not load kept results (non-fatal): {error}")
        return {}


def blocks(text) -> list[str]:
    """The records inside one message's text (for sizing: context_breakdown.py)."""
    found, text, start = [], text if isinstance(text, str) else "", 0
    while True:
        begin = text.find(OPEN, start)
        if begin < 0:
            return found
        end = text.find(CLOSE, begin)
        if end < 0:
            return found
        found.append(text[begin:end + len(CLOSE)])
        start = end + len(CLOSE)


class ToolMemoryTurn:
    """The tool calls of the reply being written. An instance made without a message id is inert."""

    def __init__(self, message_id=None, *, window: int = 0, private=(), rows=()):
        self.message_id = message_id
        self.window = int(window or 0)
        self.private = frozenset(private or ())
        self.rows = [dict(row) for row in rows]
        self.tainted = any(int(row.get("kept") or 0) == NOT_KEPT for row in self.rows)
        self.stored = sum(len(row.get("arguments") or "") + len(row.get("result") or "") for row in self.rows)
        # Rows a continued reply already had are in this request's prompt: only what is added counts as new.
        self.sent_units = self._units()

    @property
    def active(self) -> bool:
        return self.message_id is not None

    async def record(self, tool: str, arguments, result, call_id: str = "", round_number: int = 0) -> bool:
        """Store one finished tool call. True when a row was written."""
        if not self.active:
            return False
        try:
            if tool in PRIVATE or tool in self.private:
                kind, self.tainted = NOT_KEPT, True
            elif tool in HEADER_ONLY or self.tainted or self.stored >= STORED_CHARS:
                kind = HEADER
            else:
                kind = FULL
            row = {"tool": tool, "status": "failed" if failed(result) else "ok", "kept": kind,
                   "call_id": str(call_id or ""), "round": int(round_number or 0),
                   "arguments": json.dumps(trim_arguments(arguments) if kind == FULL else {}, ensure_ascii=False),
                   "result": trim_result(result) if kind == FULL else ""}
            await store.add(self.message_id, round_number=row["round"], call_id=row["call_id"], tool=tool,
                            status=row["status"], kept=kind, arguments=row["arguments"], result=row["result"])
            self.rows.append(row)
            self.stored += len(row["arguments"]) + len(row["result"])
            return True
        except Exception as error:
            print(f"[TOOLMEM] could not keep a {tool} result (non-fatal): {error}")
            return False

    def _units(self) -> int:
        try:
            return chat_context.estimate(digest(self.rows, turn_budget(self.window))) if self.rows else 0
        except Exception:
            return 0

    def estimate(self) -> int:
        """Estimator units this reply's record adds to the next request, beyond what this one already carried."""
        return max(0, self._units() - self.sent_units)


async def begin_turn(req, conversation_id: str, message_id, *, ephemeral: bool = False, coder: bool = False,
                     window: int = 0, private=()) -> ToolMemoryTurn:
    """The recorder for this reply. Inert for Ghost chats, Daedalus chats, scheduled runs, a window too
    small to carry results, or when the setting is off."""
    try:
        metadata = getattr(req, "user_metadata", None)
        headless = isinstance(metadata, dict) and metadata.get("source") == "scheduled_task"
        if message_id is None or ephemeral or coder or headless or not usable(window) or not conversation_id:
            return ToolMemoryTurn()
        # Continue mode writes into a reply that already has rows.
        existing = (await store.for_messages(conversation_id, [message_id])).get(int(message_id), [])
        return ToolMemoryTurn(int(message_id), window=window, private=private, rows=existing)
    except Exception as error:
        print(f"[TOOLMEM] recorder unavailable (non-fatal): {error}")
        return ToolMemoryTurn()
