"""Skill library storage: procedures HyprChat saves for later chats, scoped per user.

A skill is a name, a one-line description (the only part always in the prompt), a markdown body
loaded on demand, and optional files. Every change to a live skill snapshots the previous text into
``skill_revisions`` so it can be undone. A change that must wait for the owner (a scheduled run, a
conversation that read mail, a scan hit) is parked in ``pending_json`` and never touches the live body.

Policy (what may be written, and with which status) lives in ``skills.py``; this module only stores.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta

import database as db

STATUSES = ("active", "draft", "stale", "archived")
ORIGINS = ("user", "agent", "reviewer", "import")
LEARNED = ("agent", "reviewer")     # origins the curator may retire
USABLE = ("active", "stale")        # statuses skill_view will load
REVISION_LIMIT = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills(
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'general',
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    origin TEXT NOT NULL DEFAULT 'user',
    pinned INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    source_conv_id TEXT NOT NULL DEFAULT '',
    scan_flags TEXT NOT NULL DEFAULT '[]',
    pending_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, name));
CREATE INDEX IF NOT EXISTS idx_skills_user_status ON skills(user_id, status);
CREATE TABLE IF NOT EXISTS skill_revisions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_skill_revisions_skill ON skill_revisions(skill_id, id);
CREATE TABLE IF NOT EXISTS skill_files(
    skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    path TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(skill_id, path));
CREATE TABLE IF NOT EXISTS skill_conv_taint(
    conv_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL);
"""

TABLES = ("skill_revisions", "skill_files", "skill_conv_taint", "skills")   # child tables first


async def migrate(conn):
    await conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.utcnow().isoformat()


def _row(row) -> dict:
    skill = dict(row)
    skill["pinned"] = bool(skill.get("pinned"))
    for key, column, empty in (("scan_flags", "scan_flags", []), ("pending", "pending_json", None)):
        try:
            skill[key] = json.loads(skill.get(column) or "null") or empty
        except (json.JSONDecodeError, TypeError):
            skill[key] = empty
    skill.pop("pending_json", None)
    return skill


async def create(name: str, description: str, body: str, *, category: str = "general",
                 status: str = "active", origin: str = "user", source_conv_id: str = "",
                 scan_flags=(), user_id: str | None = None) -> dict:
    """Insert a skill. Raises ValueError when the user already has one with this name."""
    uid = db._scope_user(user_id)
    skill_id, now = "sk-" + uuid.uuid4().hex[:12], _now()
    conn = await db.get_db()
    try:
        try:
            await conn.execute(
                """INSERT INTO skills(id, user_id, name, description, category, body, status, origin,
                   source_conv_id, scan_flags, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (skill_id, uid, name, description, category or "general", body, status, origin,
                 source_conv_id or "", json.dumps(list(scan_flags)), now, now))
        except sqlite3.IntegrityError:
            raise ValueError(f"a skill named '{name}' already exists")
        await conn.commit()
    finally:
        await conn.close()
    return await get(skill_id, user_id=uid)


async def get(skill_id: str, user_id: str | None = None) -> dict | None:
    uid = db._scope_user(user_id)
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM skills WHERE id=? AND user_id=?", (skill_id, uid))
        return _row(rows[0]) if rows else None
    finally:
        await conn.close()


async def get_by_name(name: str, user_id: str | None = None) -> dict | None:
    uid = db._scope_user(user_id)
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM skills WHERE name=? AND user_id=?", (name, uid))
        return _row(rows[0]) if rows else None
    finally:
        await conn.close()


async def list_skills(statuses=None, user_id: str | None = None) -> list[dict]:
    """Skills in category/name order — a stable order, so the prompt index does not churn."""
    uid = db._scope_user(user_id)
    wanted = [s for s in (statuses or STATUSES) if s in STATUSES]
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            f"SELECT * FROM skills WHERE user_id=? AND status IN ({','.join('?' * len(wanted))}) "
            "ORDER BY category, name", (uid, *wanted))
        return [_row(r) for r in rows]
    finally:
        await conn.close()


async def update(skill_id: str, *, description: str | None = None, body: str | None = None,
                 category: str | None = None, origin: str = "user", reason: str = "",
                 user_id: str | None = None) -> dict | None:
    """Change a skill's text. The previous description and body are kept as a revision."""
    uid = db._scope_user(user_id)
    current = await get(skill_id, user_id=uid)
    if not current:
        return None
    new_description = current["description"] if description is None else description
    new_body = current["body"] if body is None else body
    text_changed = (new_description, new_body) != (current["description"], current["body"])
    now = _now()
    conn = await db.get_db()
    try:
        if text_changed:
            await conn.execute(
                """INSERT INTO skill_revisions(skill_id, user_id, version, description, body, origin, reason, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (skill_id, uid, current["version"], current["description"], current["body"], origin, reason[:300], now))
            await conn.execute(
                """DELETE FROM skill_revisions WHERE skill_id=? AND id NOT IN (
                   SELECT id FROM skill_revisions WHERE skill_id=? ORDER BY id DESC LIMIT ?)""",
                (skill_id, skill_id, REVISION_LIMIT))
        await conn.execute(
            "UPDATE skills SET description=?, body=?, category=?, version=version+?, updated_at=? WHERE id=? AND user_id=?",
            (new_description, new_body, current["category"] if category is None else (category or "general"),
             1 if text_changed else 0, now, skill_id, uid))
        await conn.commit()
    finally:
        await conn.close()
    return await get(skill_id, user_id=uid)


async def set_fields(skill_id: str, *, user_id: str | None = None, **fields) -> dict | None:
    """Set bookkeeping columns (status, pinned, name, scan_flags, origin) without a revision."""
    uid = db._scope_user(user_id)
    allowed = {"status", "pinned", "name", "scan_flags", "origin"}
    values = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "status" in values and values["status"] not in STATUSES:
        raise ValueError("unknown status")
    if "pinned" in values:
        values["pinned"] = 1 if values["pinned"] else 0
    if "scan_flags" in values:
        values["scan_flags"] = json.dumps(list(values["scan_flags"]))
    if not values:
        return await get(skill_id, user_id=uid)
    conn = await db.get_db()
    try:
        try:
            await conn.execute(
                f"UPDATE skills SET {', '.join(k + '=?' for k in values)}, updated_at=? WHERE id=? AND user_id=?",
                (*values.values(), _now(), skill_id, uid))
        except sqlite3.IntegrityError:
            raise ValueError(f"a skill named '{values.get('name')}' already exists")
        await conn.commit()
    finally:
        await conn.close()
    return await get(skill_id, user_id=uid)


async def set_pending(skill_id: str, pending: dict | None, user_id: str | None = None) -> dict | None:
    uid = db._scope_user(user_id)
    conn = await db.get_db()
    try:
        await conn.execute("UPDATE skills SET pending_json=?, updated_at=? WHERE id=? AND user_id=?",
                           (json.dumps(pending) if pending else "", _now(), skill_id, uid))
        await conn.commit()
    finally:
        await conn.close()
    return await get(skill_id, user_id=uid)


async def apply_pending(skill_id: str, user_id: str | None = None) -> dict | None:
    """Make a parked change live (the owner approved it)."""
    skill = await get(skill_id, user_id=user_id)
    if not skill or not skill.get("pending"):
        return skill
    pending = skill["pending"]
    await update(skill_id, description=pending.get("description"), body=pending.get("body"),
                 origin=pending.get("origin") or "reviewer", reason=pending.get("reason") or "approved change",
                 user_id=user_id)
    return await set_pending(skill_id, None, user_id=user_id)


async def touch(skill_id: str, user_id: str | None = None) -> None:
    """Record a use. A stale skill that gets used is live again."""
    uid = db._scope_user(user_id)
    conn = await db.get_db()
    try:
        await conn.execute(
            """UPDATE skills SET use_count=use_count+1, last_used_at=?,
               status=CASE status WHEN 'stale' THEN 'active' ELSE status END WHERE id=? AND user_id=?""",
            (_now(), skill_id, uid))
        await conn.commit()
    finally:
        await conn.close()


async def revisions(skill_id: str, user_id: str | None = None) -> list[dict]:
    uid = db._scope_user(user_id)
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT * FROM skill_revisions WHERE skill_id=? AND user_id=? ORDER BY id DESC", (skill_id, uid))
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def revert(skill_id: str, revision_id: int, user_id: str | None = None) -> dict | None:
    """Restore an earlier text. The text being replaced becomes a revision itself."""
    uid = db._scope_user(user_id)
    wanted = next((r for r in await revisions(skill_id, user_id=uid) if r["id"] == int(revision_id)), None)
    if not wanted:
        return None
    return await update(skill_id, description=wanted["description"], body=wanted["body"], origin="user",
                        reason=f"reverted to version {wanted['version']}", user_id=uid)


async def delete(skill_id: str, user_id: str | None = None) -> bool:
    uid = db._scope_user(user_id)
    conn = await db.get_db()
    try:
        cursor = await conn.execute("DELETE FROM skills WHERE id=? AND user_id=?", (skill_id, uid))
        await conn.commit()
        return cursor.rowcount > 0
    finally:
        await conn.close()


# ── Files ──

async def files(skill_id: str, user_id: str | None = None, *, content: bool = False) -> list[dict]:
    uid = db._scope_user(user_id)
    columns = "path, content, updated_at" if content else "path, length(content) AS size, updated_at"
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            f"SELECT {columns} FROM skill_files WHERE skill_id=? AND user_id=? ORDER BY path", (skill_id, uid))
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_file(skill_id: str, path: str, user_id: str | None = None) -> dict | None:
    return next((f for f in await files(skill_id, user_id, content=True) if f["path"] == path), None)


async def put_file(skill_id: str, path: str, content: str, user_id: str | None = None) -> None:
    uid = db._scope_user(user_id)
    conn = await db.get_db()
    try:
        await conn.execute(
            """INSERT INTO skill_files(skill_id, user_id, path, content, updated_at) VALUES(?,?,?,?,?)
               ON CONFLICT(skill_id, path) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at""",
            (skill_id, uid, path, content, _now()))
        await conn.commit()
    finally:
        await conn.close()


async def delete_file(skill_id: str, path: str, user_id: str | None = None) -> bool:
    uid = db._scope_user(user_id)
    conn = await db.get_db()
    try:
        cursor = await conn.execute(
            "DELETE FROM skill_files WHERE skill_id=? AND user_id=? AND path=?", (skill_id, uid, path))
        await conn.commit()
        return cursor.rowcount > 0
    finally:
        await conn.close()


# ── Conversations that read untrusted, attacker-addressable content ──

async def taint(conv_id: str, reason: str, user_id: str | None = None) -> None:
    if not conv_id:
        return
    uid = db._scope_user(user_id)
    conn = await db.get_db()
    try:
        await conn.execute("INSERT OR IGNORE INTO skill_conv_taint(conv_id, user_id, reason, at) VALUES(?,?,?,?)",
                           (conv_id, uid, reason[:80], _now()))
        await conn.commit()
    finally:
        await conn.close()


async def taint_reason(conv_id: str, user_id: str | None = None) -> str:
    """Why this conversation's skills must wait for review; '' when they need not."""
    if not conv_id:
        return ""
    uid = db._scope_user(user_id)
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT reason FROM skill_conv_taint WHERE conv_id=? AND user_id=?", (conv_id, uid))
        return (rows[0]["reason"] or "untrusted content") if rows else ""
    finally:
        await conn.close()


# ── Upkeep (runs from the scheduler, outside any request) ──

async def owners() -> list[str]:
    conn = await db.get_db()
    try:
        return [r["user_id"] for r in await conn.execute_fetchall("SELECT DISTINCT user_id FROM skills")]
    finally:
        await conn.close()


async def curate(user_id: str, stale_days: int, archive_days: int, *, dry_run: bool = False,
                 now: datetime | None = None) -> list[dict]:
    """Retire learned skills nobody uses: active → stale → archived. Never deletes.

    Pinned skills and skills the owner wrote or imported are left alone.
    """
    now = now or datetime.utcnow()
    stale_before = (now - timedelta(days=stale_days)).isoformat()
    archive_before = (now - timedelta(days=archive_days)).isoformat()
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            f"""SELECT id, name, status, COALESCE(last_used_at, created_at) AS seen FROM skills
                WHERE user_id=? AND pinned=0 AND status IN ('active','stale')
                  AND origin IN ({','.join('?' * len(LEARNED))})""", (user_id, *LEARNED))
        moves = []
        for row in rows:
            target = "archived" if row["seen"] < archive_before else "stale" if row["seen"] < stale_before else row["status"]
            if target != row["status"]:
                moves.append({"id": row["id"], "name": row["name"], "from": row["status"], "to": target})
        if moves and not dry_run:
            stamp = now.isoformat()
            for move in moves:
                await conn.execute("UPDATE skills SET status=?, updated_at=? WHERE id=? AND user_id=?",
                                   (move["to"], stamp, move["id"], user_id))
            await conn.commit()
        return moves
    finally:
        await conn.close()
