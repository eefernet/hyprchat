"""Storage for the tool results a chat keeps between turns (see ``chat_tool_memory.py``).

One row per tool call, owned by the reply that made it. Rows go with their message: every delete path
removes the ``messages`` row on a connection with foreign keys on, as ``message_context`` already relies on.
"""
from __future__ import annotations

import database as db

SCHEMA = """
CREATE TABLE IF NOT EXISTS message_tool_results (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    round INTEGER NOT NULL DEFAULT 0,
    call_id TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    kept INTEGER NOT NULL DEFAULT 1,
    arguments TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id, seq)
)
"""
COLUMNS = "message_id, seq, round, call_id, tool, status, kept, arguments, result"


async def migrate(conn) -> None:
    await conn.execute(SCHEMA)


async def add(message_id: int, *, round_number: int, call_id: str, tool: str, status: str, kept: int,
              arguments: str, result: str) -> None:
    """Append one row to a reply of the current user. A message that is not theirs gets nothing."""
    conn = await db.get_db()
    try:
        await conn.execute(
            """INSERT INTO message_tool_results (message_id, seq, round, call_id, tool, status, kept, arguments, result)
               SELECT m.id, COALESCE((SELECT MAX(seq) FROM message_tool_results WHERE message_id = m.id), 0) + 1,
                      ?, ?, ?, ?, ?, ?, ?
               FROM messages m JOIN conversations c ON c.id = m.conversation_id
               WHERE m.id = ? AND c.user_id = ?""",
            (int(round_number or 0), call_id or "", tool, status or "", int(kept), arguments, result,
             int(message_id), db._scope_user()))
        await conn.commit()
    finally:
        await conn.close()


async def for_messages(conversation_id: str, message_ids) -> dict[int, list[dict]]:
    """Rows of the given replies of one conversation of the current user, in the order they ran.
    An id that belongs to another chat brings nothing."""
    ids = sorted({int(value) for value in message_ids or () if value})
    if not conversation_id or not ids:
        return {}
    found: dict[int, list[dict]] = {}
    conn = await db.get_db()
    try:
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            rows = await conn.execute_fetchall(
                f"""SELECT r.message_id, r.seq, r.round, r.call_id, r.tool, r.status, r.kept, r.arguments, r.result
                    FROM message_tool_results r
                    JOIN messages m ON m.id = r.message_id
                    JOIN conversations c ON c.id = m.conversation_id
                    WHERE m.conversation_id = ? AND c.user_id = ? AND r.message_id IN ({','.join('?' * len(chunk))})
                    ORDER BY r.message_id, r.seq""",
                (conversation_id, db._scope_user(), *chunk))
            for row in rows:
                found.setdefault(int(row["message_id"]), []).append(dict(row))
    finally:
        await conn.close()
    return found


async def copy_for_fork(conn, original_conversation: str, new_conversation: str, fork_message_id: int) -> int:
    """Give a fork the rows of the messages it copied. The copy kept their order, so the nth message of
    the fork is the nth copied message of the original. Returns how many rows were copied."""
    old = [row["id"] for row in await conn.execute_fetchall(
        "SELECT id FROM messages WHERE conversation_id = ? AND id <= ? ORDER BY id ASC", (original_conversation, fork_message_id))]
    new = [row["id"] for row in await conn.execute_fetchall(
        "SELECT id FROM messages WHERE conversation_id = ? ORDER BY id ASC", (new_conversation,))]
    if len(old) != len(new):
        return 0
    copied = 0
    for source, target in zip(old, new):
        cursor = await conn.execute(
            f"""INSERT INTO message_tool_results ({COLUMNS})
                SELECT ?, seq, round, call_id, tool, status, kept, arguments, result
                FROM message_tool_results WHERE message_id = ?""", (target, source))
        copied += cursor.rowcount or 0
    return copied
