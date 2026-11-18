"""What a chat's context window is filled with, by category.

The context pill shows one number. Clicking it shows where the tokens go (2026-11-15): instructions,
tool definitions, memory, skills, the summary of earlier messages, the messages themselves, the search
and knowledge excerpts sent for this turn only, and this reply's own tool calls and results.

Nothing is re-parsed. The chat loop tells a ``Ledger`` the size of each named block where it assembles
the prompt, and ``Ledger.sizes`` splits one request into categories: what was noted, what can be found
by position (the sent history, the summary block, messages added during this reply), and instructions
as the remainder. All of that is in the estimator's units; ``scale`` turns one split into real tokens
that add up to the count the model reported for that request.

Every function here returns None or a plain reading on failure. A breakdown must never fail a reply.
"""
from __future__ import annotations

import chat_context

# In the order the browser lists them.
CATEGORIES = ("instructions", "tools", "memory", "skills", "summary", "messages", "kept_tools", "excerpts", "tool_calls")
# Sent with every request, and close to code or JSON in how they tokenize.
FIXED = ("instructions", "tools", "memory", "skills")
# Sent again on the next turn. Excerpts ride along for one turn; this reply's tool traffic is dropped
# when the reply ends.
KEPT = ("instructions", "tools", "memory", "skills", "summary", "messages", "kept_tools")


def _units(message) -> int:
    return chat_context.estimate({key: value for key, value in message.items() if key != "images"})


def _kept_tool_units(history) -> int:
    try:
        import chat_tool_memory
        return sum(chat_context.estimate(block) for message in history
                   for block in chat_tool_memory.blocks(message.get("content")))
    except Exception:
        return 0


def _is_summary(message) -> bool:
    return (message.get("content") or "").startswith(chat_context.SUMMARY_MARK) if isinstance(message.get("content"), str) else False


class Ledger:
    """Sizes noted while one request is assembled."""

    def __init__(self):
        self.notes: dict[str, int] = {}
        self.in_history = 0      # noted excerpts that were appended to a message of the sent history
        self.base: set[int] | None = None

    def note(self, category: str, text, in_history: bool = False) -> None:
        """Record a block as it is added to the prompt. ``in_history``: it was appended to the user's
        message, so it sits inside the sent history and must not be counted as conversation."""
        try:
            size = chat_context.estimate(text or "") if text else 0
            if size:
                self.notes[category] = self.notes.get(category, 0) + size
                if in_history:
                    self.in_history += size
        except Exception:
            pass

    def turn_only(self) -> int:
        """What is sent with this turn only: search and knowledge excerpts, one-turn hints."""
        return self.notes.get("excerpts", 0)

    def start(self, messages) -> None:
        """Call once, right before the first model call: every message added later belongs to this reply."""
        self.base = {id(message) for message in messages}

    def sizes(self, messages, history, tools) -> dict | None:
        """One request split into categories, in estimator units. None when it cannot be worked out."""
        try:
            total = chat_context.payload_estimate(messages, tools)
            tool_units = chat_context.estimate(tools) if tools else 0
            summary = sum(_units(message) for message in messages if _is_summary(message))
            added = 0
            if self.base is not None:
                added = sum(_units(message) for message in messages
                            if id(message) not in self.base and not _is_summary(message))
            place = chat_context.region(messages, history)
            sent = sum(_units(message) for message in messages[place[0]:place[1]]) if place else 0
            # Records of earlier replies' tool calls ride on the user messages that asked (chat_tool_memory.py).
            # Sized from the messages themselves, so a compaction that folded some of them is reflected.
            kept_tools = min(sent, _kept_tool_units(messages[place[0]:place[1]])) if place else 0
            excerpts = self.notes.get("excerpts", 0)
            memory, skills = self.notes.get("memory", 0), self.notes.get("skills", 0)
            lead_excerpts = max(0, excerpts - self.in_history)
            parts = {
                "tools": tool_units, "memory": memory, "skills": skills, "summary": summary,
                "messages": max(0, sent - self.in_history - kept_tools), "kept_tools": kept_tools,
                "excerpts": excerpts, "tool_calls": added,
            }
            parts["instructions"] = max(0, total - tool_units - memory - skills - summary - sent - lead_excerpts - added)
            return {name: int(parts[name]) for name in CATEGORIES}
        except Exception as error:
            print(f"[CONTEXT] breakdown unavailable (non-fatal): {error}")
            return None

    def fixed(self, messages, history, tools) -> int:
        """Estimator units of what every request carries before any conversation: instructions, tool
        definitions, memory and skills. Excerpts are not part of it (they were, and were also counted as
        one-turn context: the same tokens under two headings)."""
        parts = self.sizes(messages, history, tools)
        if not parts:
            return chat_context.fixed_estimate(messages, history, tools)
        return sum(parts[name] for name in FIXED)


def scale(parts: dict | None, real: int, ratio: float = 0.0) -> dict | None:
    """``parts`` (estimator units) as real tokens that add up to ``real``.

    Instructions and tool definitions tokenize at about ``DEFAULT_RATIO`` however long the chat gets,
    while the whole-prompt ratio falls as prose is added (0.80 to 0.61 over a ten-turn chat). So the
    fixed categories keep at least that ratio and the prose categories share what is left.
    """
    if not parts or real <= 0:
        return None
    try:
        units = {name: max(0, int(parts.get(name) or 0)) for name in CATEGORIES}
        total = sum(units.values())
        if not total:
            return None
        whole = real / total
        fixed_units = sum(units[name] for name in FIXED)
        prose_units = total - fixed_units
        fixed_real = min(real, fixed_units * max(whole, ratio or 0.0, chat_context.DEFAULT_RATIO)) if prose_units else real
        prose_real = real - fixed_real
        exact = {name: (units[name] * fixed_real / fixed_units if fixed_units else 0.0) if name in FIXED
                 else (units[name] * prose_real / prose_units if prose_units else 0.0) for name in CATEGORIES}
        out = {name: int(value) for name, value in exact.items()}
        # Largest remainder: hand out what rounding down left, so the rows add up to the header.
        for name in sorted(CATEGORIES, key=lambda key: exact[key] - out[key], reverse=True)[:max(0, real - sum(out.values()))]:
            out[name] += 1
        return out
    except Exception:
        return None


def kept(first: dict | None, used: int) -> dict | None:
    """What the conversation holds after the reply: the first request's standing categories, with
    Messages taking the rest of ``used`` (it grows by the answer; excerpts and tool traffic are gone)."""
    if not first or used <= 0:
        return None
    parts = {name: int(first.get(name) or 0) for name in KEPT if name != "messages"}
    standing = sum(parts.values())
    if standing > used * 1.2:       # the split and the reading disagree: show no split rather than a wrong one
        return None
    if standing > used:
        over, largest = standing - used, max(parts, key=parts.get)
        parts[largest] = max(0, parts[largest] - over)
    parts["messages"] = max(0, used - sum(parts.values()))
    return parts
