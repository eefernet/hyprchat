"""Replies a restart left unfinished.

A reply is saved as "running" while it is being written. When the service stops in the middle (a deploy,
a crash, a kill) nothing marks it finished. Two such rows sat on the live server for months
(2026-07-14 and 2026-11-11): the chat drew them as still working, and they never got a Continue.

One worker serves the whole app, so a reply that is still "running" when the service starts has nobody
writing it. ``sweep`` marks those interrupted; the chat then offers Continue like after any other stop.
"""
from __future__ import annotations

import database as db

_RUNNING = """role = 'assistant' AND json_valid(metadata)
              AND (json_extract(metadata, '$.delivery_status') = 'running'
                   OR (json_extract(metadata, '$.delivery_status') IS NULL
                       AND json_extract(metadata, '$.in_progress') = 1))"""


async def sweep() -> int:
    """Mark every reply still saved as running as interrupted. Returns how many there were.
    Runs at startup, before any request, for all users."""
    conn = await db.get_db()
    try:
        cursor = await conn.execute(
            f"""UPDATE messages
                SET metadata = json_set(metadata, '$.in_progress', json('false'),
                                        '$.delivery_status', 'interrupted')
                WHERE {_RUNNING}""")
        await conn.commit()
        return cursor.rowcount or 0
    finally:
        await conn.close()
