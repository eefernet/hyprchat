"""Summarizing a chat's earlier messages when the owner asks, and forgetting the tool results it keeps.

Auto-compaction waits until a chat reaches the chosen share of its window. "Compact now" in the context
breakdown does the same thing on request: before a large paste, or to make a long chat faster. It runs
outside a reply, so it works from the saved messages instead of from a request, with the helpers that
auto-compaction uses (``chat_context``): the chat's own model writes the summary, everything older
than a short recent part goes into it, and the cut is stored as ``summary_until_msg_id``. From the next
turn on, ``chat_context.compact`` sends "summary + every message after the cut" as it does after an
automatic compaction.

Both actions also correct the reading saved on the newest reply. The next turn starts from that reading:
left as it was, the pill would still show the old size and the next turn could compact again for nothing.
"""
from __future__ import annotations

import json

import chat_context
import chat_tool_memory
import config
import database as db
import model_limits
from db import tool_memory

KEEP_SHARE = 0.10       # of the window stays word for word...
KEEP_TOKENS = 4000      # ...and never more than this: the owner asked for room now
MIN_FOLD_CEILING = 2000  # the least worth summarizing is 5 % of the window, but never more than this


def refused(reason: str) -> dict:
    return {"compacted": False, "reason": reason}


def as_sent(rows, records: dict) -> list[dict]:
    """The saved messages as the model gets them: the text of an attached PDF instead of the bubble's
    text, and each reply's kept tool record on the message that asked (as the chat loop sends them)."""
    out, asked = [], None
    for row in rows:
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        content = row.get("content") or ""
        if row.get("role") == "user":
            content = meta.get("model_content") or content
        message = {"role": row.get("role"), "content": content}
        out.append(message)
        if row.get("role") == "user":
            asked = message
        elif asked is not None and records.get(int(row.get("id") or 0)):
            asked["content"] += "\n\n" + records[int(row["id"])]
    return out


async def lower_saved_reading(conversation_id: str, *, freed: int = 0, kept_tools: bool = False) -> int:
    """Take ``freed`` tokens (or the kept tool results) off the reading saved on the newest reply.
    Returns how many tokens came off. The category split no longer matches, so it is dropped."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT m.id, m.metadata FROM messages m JOIN conversations c ON c.id = m.conversation_id
               WHERE m.conversation_id = ? AND c.user_id = ? AND m.role = 'assistant'
               ORDER BY m.id DESC LIMIT 4""", (conversation_id, db._scope_user()))
        for row in rows:
            try:
                meta = json.loads(row["metadata"] or "{}") or {}
            except (TypeError, ValueError):
                continue
            reading = meta.get("context")
            if not (isinstance(reading, dict) and reading.get("used")):
                continue
            if kept_tools:
                freed = int((reading.get("parts") or {}).get("kept_tools") or 0)
            freed = max(0, min(int(freed or 0), int(reading["used"])))
            if freed:
                reading = {key: value for key, value in reading.items() if key not in ("parts", "peak")}
                reading.update(used=int(reading["used"]) - freed, measured=False)
                meta["context"] = reading
                await conn.execute("UPDATE messages SET metadata = ? WHERE id = ?", (json.dumps(meta), row["id"]))
                await conn.commit()
            return freed
        return 0
    finally:
        await conn.close()


async def compact_now(http, conversation_id: str, *, model: str = "", asked: int = 0) -> dict:
    """Summarize the earlier part of a saved conversation now. Raises LookupError when the conversation
    is not the caller's. Returns ``{"compacted": True, "summarized", "cursor", "freed", "window"}`` or
    ``{"compacted": False, "reason"}``; nothing is changed unless a summary was written."""
    conversation = await db.get_conversation(conversation_id)
    if not conversation:
        raise LookupError(conversation_id)
    if not chat_context.enabled():
        return refused("Auto-compaction is switched off in Settings, so a summary would not be used.")
    name = model_limits.local_name(model or conversation.get("model") or "")
    if not name:
        return refused("Compaction works with local models only.")
    rows = [row for row in conversation.get("messages") or [] if row.get("role") in ("user", "assistant")]
    newest = next((row for row in reversed(rows) if row.get("role") == "assistant"), None)
    if newest and ((newest.get("metadata") or {}) if isinstance(newest.get("metadata"), dict) else {}).get("delivery_status") == "running":
        return refused("A reply is being written in this chat. Try again when it has finished.")

    previous = await chat_context.previous_reading(conversation_id) or {}
    window = model_limits.effective(asked or previous.get("asked") or previous.get("window") or config.coerce_num_ctx(config.DEFAULT_NUM_CTX),
                                    await model_limits.context_limit(http, name))
    ratio = chat_context.ratio_of(float(previous.get("ratio") or 0), 1.0, chat_context.DEFAULT_RATIO)
    old_summary, cursor = await db.get_conversation_summary(conversation_id)
    if not (old_summary or "").strip():
        old_summary, cursor = "", 0
    body_rows = [row for row in rows if int(row.get("id") or 0) > int(cursor or 0)]
    records = await chat_tool_memory.load_for(
        conversation_id, [{"role": row["role"], "id": row["id"]} for row in body_rows], window=window, after=cursor)
    body = as_sent(body_rows, records)

    keep = chat_context.keep_start(body, min(window * KEEP_SHARE, KEEP_TOKENS), ratio) if body else 0
    fold = body[:keep]
    fold_tokens = round(chat_context.payload_estimate(fold) * ratio) if fold else 0
    # Rewriting the summary for a few hundred tokens only wears it down (each rewrite loses a little).
    if fold_tokens < max(chat_context.MIN_FOLD_TOKENS, min(window * chat_context.MIN_FOLD_SHARE, MIN_FOLD_CEILING)):
        return refused("There is not enough earlier conversation to summarize yet.")

    words = chat_context.summary_words(window)
    fits = fold_tokens + chat_context.estimate(old_summary) * ratio + words * 2 + 300 <= window
    if fits:
        summary = await chat_context._plain_summary(http, name, window, old_summary, fold, words)
        folded = len(fold) if summary else 0
    else:       # too much for one request: the helper model folds it in passes
        summary, folded = await chat_context._helper_summary(
            http, old_summary, fold, [row["id"] for row in body_rows[:keep]], words)
    if not summary or not folded:
        return refused("The summary could not be written. Nothing was changed.")

    new_cursor = int(body_rows[folded - 1]["id"])
    await db.set_conversation_summary(conversation_id, summary, new_cursor)
    freed = max(0, round((chat_context.payload_estimate(fold[:folded]) + chat_context.estimate(old_summary)
                          - chat_context.estimate(summary)) * ratio))
    freed = await lower_saved_reading(conversation_id, freed=freed)
    count = sum(1 for row in rows if int(row.get("id") or 0) <= new_cursor)
    print(f"[CONTEXT] compacted on request conv={conversation_id}: {count} messages summarized, about {freed} tokens freed")
    return {"compacted": True, "summarized": count, "cursor": new_cursor, "freed": freed, "window": window}


async def forget_tool_results(conversation_id: str) -> dict:
    """Delete the tool results the chat keeps for later turns. Raises LookupError for someone else's chat."""
    if not await db.get_conversation(conversation_id):
        raise LookupError(conversation_id)
    removed = await tool_memory.clear_conversation(conversation_id)
    freed = await lower_saved_reading(conversation_id, kept_tools=True) if removed else 0
    return {"removed": removed, "freed": freed}
