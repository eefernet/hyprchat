"""Which saved memories go into a chat's prompt, and in what order.

The memory block sits near the top of the prompt, before the whole conversation. It used to be picked
anew for every message: each memory was scored by word overlap with the new message, the top twelve
(ten in a workspace) were listed in score order, and so the block's text changed from one turn to the
next. Ollama can only reuse its cached work for the part of a prompt that is unchanged from the start,
so everything after the block, which is the entire chat, was read again on every turn.

Measured 2026-11-15 with 27 memories and a 9,000-token chat: 1.1 to 1.4 s of prompt reading on every
turn with memory on, against 0.11 s from the second turn on with memory off. On a large model and a long
chat that is tens of seconds per message.

So: when every memory fits the block's size, all of them are sent, in an order that does not depend on
the message. Only when they do not fit are the most relevant chosen, and they are still listed in that
fixed order, so the block changes only when the chosen set does.
"""
from __future__ import annotations


def item_text(memory: dict) -> str:
    content = (memory.get("content") or "").strip()
    return f"- {'[pinned] ' if memory.get('pinned') else ''}{content}" if content else ""


def fixed_order(memory: dict):
    """Pinned first, then the more important, then the older. Nothing here depends on the message."""
    return (0 if memory.get("pinned") else 1, -int(memory.get("importance") or 3),
            str(memory.get("created_at") or ""), str(memory.get("id") or ""))


def relevance(memory: dict, overlap: int) -> int:
    return (20 if memory.get("pinned") else 0) + int(memory.get("importance") or 3) * 4 + overlap * 6


def choose(memories: list[dict], *, room: int, cap: int, overlap) -> list[tuple[dict, str]]:
    """The memories to send as ``(memory, line)`` pairs, in the order to list them.

    ``room`` is the characters left for them, ``cap`` the most to send when they do not all fit, and
    ``overlap(memory)`` how many words the memory shares with the new message.
    """
    usable = [(memory, item_text(memory)) for memory in memories]
    usable = [(memory, line) for memory, line in usable if line]
    if sum(len(line) for _, line in usable) <= max(0, room):
        chosen = usable                                  # everything fits: the same block every turn
    else:
        ranked = sorted(usable, key=lambda pair: relevance(pair[0], overlap(pair[0])), reverse=True)
        chosen, left = [], max(0, room)
        for memory, line in ranked:
            if left <= 0 or len(chosen) >= cap:
                break
            if len(line) > left:
                if left < 40:                # no room for a line worth reading
                    break
                line = line[:left - 3] + "..."
            chosen.append((memory, line))
            left -= len(line)
    return sorted(chosen, key=lambda pair: fixed_order(pair[0]))
