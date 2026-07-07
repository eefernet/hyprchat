"""
Database layer — SQLite with aiosqlite for async access.
Stores conversations, messages, knowledge bases, tools, model configs.
"""
import aiosqlite
import asyncio
import base64
import contextvars
import hashlib
import hmac
import os
import json
import secrets
import uuid
import re
from datetime import datetime
from config import DATABASE_PATH
from db.artifacts import (
    ARTIFACT_KIND_SET as _ARTIFACT_KIND_SET,
    ARTIFACT_STATUSES as _ARTIFACT_STATUSES,
    artifact_kind_for_filename,
    artifact_mime_type_for_filename,
    clean_artifact_tags as _clean_artifact_tags,
    normalize_artifact_kind as _normalize_artifact_kind,
    normalize_artifact_row as _normalize_artifact_row,
)

DEFAULT_USER_ID = "default"
_CURRENT_USER_ID = contextvars.ContextVar("hyprchat_user_id", default=DEFAULT_USER_ID)
_PASSWORD_ITERATIONS = 260_000


def current_user_id() -> str:
    user_id = (_CURRENT_USER_ID.get() or DEFAULT_USER_ID).strip()
    return user_id or DEFAULT_USER_ID


def set_current_user_id(user_id: str):
    return _CURRENT_USER_ID.set((user_id or DEFAULT_USER_ID).strip() or DEFAULT_USER_ID)


def reset_current_user_id(token) -> None:
    _CURRENT_USER_ID.reset(token)


def _scope_user(user_id: str | None = None) -> str:
    return (user_id or current_user_id() or DEFAULT_USER_ID).strip() or DEFAULT_USER_ID


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PASSWORD_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def _verify_password(password: str, stored: str) -> bool:
    if not stored:
        return not password
    try:
        scheme, iterations, salt_b64, digest_b64 = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

from db.schema import DB_SCHEMA


async def get_db() -> aiosqlite.Connection:
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    # Wait for a writer lock instead of instantly raising "database is locked"
    # when a background task holds a write transaction.
    await db.execute("PRAGMA busy_timeout=5000")
    return db


def _session_token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _clean_user_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip())[:80] or "User"


def _public_user(row, counts: dict | None = None) -> dict:
    d = dict(row)
    return {
        "id": d["id"],
        "name": d.get("name") or "User",
        "password_enabled": bool(d.get("password_hash")),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
        "last_login_at": d.get("last_login_at"),
        **(counts or {}),
    }


async def _ensure_default_user_conn(conn: aiosqlite.Connection) -> None:
    existing = await conn.execute_fetchall("SELECT COUNT(*) AS n FROM users")
    if existing and existing[0]["n"]:
        return
    now = datetime.utcnow().isoformat()
    await conn.execute(
        """INSERT OR IGNORE INTO users(id,name,password_hash,created_at,updated_at)
           VALUES(?,?,?,?,?)""",
        (DEFAULT_USER_ID, "Main", "", now, now),
    )


async def _ensure_user_column(conn: aiosqlite.Connection, table: str) -> None:
    try:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT DEFAULT '{DEFAULT_USER_ID}'")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            print(f"[DB MIGRATION] Warning adding {table}.user_id: {e}")
    try:
        await conn.execute(
            f"UPDATE {table} SET user_id=? WHERE user_id IS NULL OR user_id=''",
            (DEFAULT_USER_ID,),
        )
    except Exception as e:
        print(f"[DB MIGRATION] Warning backfilling {table}.user_id: {e}")


async def _hydrate_artifact_rows_conn(conn: aiosqlite.Connection, artifacts: list[dict]) -> list[dict]:
    if not artifacts:
        return artifacts
    ids = [a["id"] for a in artifacts if a.get("id")]
    if not ids:
        return artifacts
    placeholders = ",".join("?" for _ in ids)
    tags_by_id = {artifact_id: [] for artifact_id in ids}
    for row in await conn.execute_fetchall(
        f"SELECT artifact_id, tag FROM artifact_tags WHERE artifact_id IN ({placeholders}) ORDER BY tag",
        tuple(ids),
    ):
        tags_by_id.setdefault(row["artifact_id"], []).append(row["tag"])
    ws_by_id = {artifact_id: [] for artifact_id in ids}
    for row in await conn.execute_fetchall(
        f"""SELECT aw.artifact_id, w.id, w.name, aw.added_at
            FROM artifact_workspaces aw
            JOIN workspaces w ON w.id=aw.workspace_id
            WHERE aw.artifact_id IN ({placeholders})
            ORDER BY aw.added_at DESC, w.name COLLATE NOCASE""",
        tuple(ids),
    ):
        ws_by_id.setdefault(row["artifact_id"], []).append({
            "id": row["id"],
            "name": row["name"],
            "added_at": row["added_at"],
        })
    for artifact in artifacts:
        aid = artifact.get("id")
        artifact["tags"] = tags_by_id.get(aid, [])
        artifact["workspaces"] = ws_by_id.get(aid, [])
        artifact["workspace_ids"] = [w["id"] for w in artifact["workspaces"]]
        if not artifact.get("workspace_id") and artifact["workspaces"]:
            artifact["workspace_id"] = artifact["workspaces"][0]["id"]
        if not artifact.get("workspace_name") and artifact["workspaces"]:
            artifact["workspace_name"] = artifact["workspaces"][0]["name"]
    return artifacts


def _artifact_source_metadata(artifact: dict) -> dict:
    metadata = artifact.get("metadata") or {}
    return {
        "conversation_id": artifact.get("conversation_id"),
        "conversation_title": artifact.get("conversation_title"),
        "message_id": artifact.get("message_id"),
        "run_id": artifact.get("run_id"),
        "workflow_id": artifact.get("workflow_id"),
        "source_tool": metadata.get("source_tool") or metadata.get("source"),
        "source_path": metadata.get("source_path") or metadata.get("directory"),
    }


def _artifact_file_health_metadata(artifact: dict) -> dict:
    return {
        "exists_status": artifact.get("exists_status") or "unknown",
        "last_checked_at": artifact.get("last_checked_at"),
        "size_bytes": artifact.get("size_bytes") or 0,
        "sha256": artifact.get("sha256") or "",
        "storage_path": artifact.get("storage_path") or "",
    }


def _artifact_summary(artifact: dict) -> dict:
    return {
        "id": artifact.get("id"),
        "title": artifact.get("title") or artifact.get("filename") or "",
        "filename": artifact.get("filename") or "",
        "kind": artifact.get("kind") or "file",
        "mime_type": artifact.get("mime_type") or "",
        "status": artifact.get("status") or "draft",
        "pinned": artifact.get("pinned") or 0,
        "created_at": artifact.get("created_at"),
        "updated_at": artifact.get("updated_at"),
        "workspaces": artifact.get("workspaces") or [],
        "workspace_ids": artifact.get("workspace_ids") or [],
        "tags": artifact.get("tags") or [],
        "file_health": _artifact_file_health_metadata(artifact),
        "provenance": _artifact_source_metadata(artifact),
    }


def _normalize_artifact_event_row(row) -> dict:
    d = dict(row)
    try:
        d["metadata"] = json.loads(d.pop("metadata_json", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["metadata"] = {}
    d["source"] = "event"
    return d


async def _add_artifact_event_conn(
    conn: aiosqlite.Connection,
    artifact_id: str,
    user_id: str,
    event_type: str,
    summary: str = "",
    metadata: dict | None = None,
    *,
    created_at: str | None = None,
) -> dict | None:
    event_type = re.sub(r"[^a-z0-9_.:-]+", "_", str(event_type or "").strip().lower())[:80]
    if not artifact_id or not event_type:
        return None
    rows = await conn.execute_fetchall(
        "SELECT id FROM artifacts WHERE id=? AND user_id=?",
        (artifact_id, user_id),
    )
    if not rows:
        return None
    now = created_at or datetime.utcnow().isoformat()
    event_id = f"artevt-{uuid.uuid4().hex[:12]}"
    await conn.execute(
        """INSERT INTO artifact_events(id,artifact_id,user_id,event_type,summary,metadata_json,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (
            event_id,
            artifact_id,
            user_id,
            event_type,
            _scrub_surrogates(str(summary or ""))[:1000],
            json.dumps(metadata or {}),
            now,
        ),
    )
    return {
        "id": event_id,
        "artifact_id": artifact_id,
        "user_id": user_id,
        "event_type": event_type,
        "summary": summary or "",
        "metadata": metadata or {},
        "created_at": now,
        "source": "event",
    }


async def add_artifact_event(
    artifact_id: str,
    event_type: str,
    summary: str = "",
    metadata: dict | None = None,
) -> dict | None:
    user_id = _scope_user()
    db = await get_db()
    try:
        event = await _add_artifact_event_conn(db, artifact_id, user_id, event_type, summary, metadata)
        await db.commit()
        return event
    finally:
        await db.close()


async def _list_artifact_duplicates_conn(
    conn: aiosqlite.Connection,
    artifact: dict,
    user_id: str,
) -> list[dict]:
    sha = (artifact.get("sha256") or "").strip()
    if not sha:
        return []
    rows = await conn.execute_fetchall(
        """SELECT a.*, c.title AS conversation_title, w.name AS workspace_name
           FROM artifacts a
           LEFT JOIN conversations c ON c.id=a.conversation_id
           LEFT JOIN workspaces w ON w.id=a.workspace_id
           WHERE a.user_id=? AND a.id<>? AND COALESCE(a.sha256,'')<>'' AND a.sha256=?
           ORDER BY a.created_at DESC, a.id DESC""",
        (user_id, artifact.get("id"), sha),
    )
    hydrated = await _hydrate_artifact_rows_conn(conn, [_normalize_artifact_row(r) for r in rows])
    return [_artifact_summary(row) for row in hydrated]


async def _set_artifact_workspaces_conn(
    conn: aiosqlite.Connection,
    artifact_id: str,
    workspace_ids,
    user_id: str,
) -> str | None:
    clean_ids: list[str] = []
    seen = set()
    for ws_id in workspace_ids or []:
        value = (str(ws_id or "").strip())
        if value and value not in seen:
            seen.add(value)
            clean_ids.append(value)
    valid_rows = []
    if clean_ids:
        placeholders = ",".join("?" for _ in clean_ids)
        valid_rows = await conn.execute_fetchall(
            f"SELECT id FROM workspaces WHERE user_id=? AND id IN ({placeholders})",
            (user_id, *clean_ids),
        )
    valid = {row["id"] for row in valid_rows}
    now = datetime.utcnow().isoformat()
    await conn.execute("DELETE FROM artifact_workspaces WHERE artifact_id=?", (artifact_id,))
    first_valid: str | None = None
    for ws_id in clean_ids:
        if ws_id not in valid:
            continue
        if first_valid is None:
            first_valid = ws_id
        await conn.execute(
            "INSERT OR IGNORE INTO artifact_workspaces(artifact_id,workspace_id,added_at) VALUES(?,?,?)",
            (artifact_id, ws_id, now),
        )
    await conn.execute(
        "UPDATE artifacts SET workspace_id=?, updated_at=? WHERE id=? AND user_id=?",
        (first_valid, now, artifact_id, user_id),
    )
    return first_valid


async def _set_artifact_tags_conn(conn: aiosqlite.Connection, artifact_id: str, tags) -> None:
    clean_tags = _clean_artifact_tags(tags)
    now = datetime.utcnow().isoformat()
    await conn.execute("DELETE FROM artifact_tags WHERE artifact_id=?", (artifact_id,))
    for tag in clean_tags:
        await conn.execute(
            "INSERT OR IGNORE INTO artifact_tags(artifact_id,tag,created_at) VALUES(?,?,?)",
            (artifact_id, tag, now),
        )


async def _backfill_artifact_workspaces_conn(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """INSERT OR IGNORE INTO artifact_workspaces(artifact_id,workspace_id,added_at)
           SELECT a.id, a.workspace_id, COALESCE(a.created_at, CURRENT_TIMESTAMP)
           FROM artifacts a
           JOIN workspaces w ON w.id=a.workspace_id AND w.user_id=a.user_id
           WHERE a.workspace_id IS NOT NULL AND a.workspace_id<>''"""
    )


async def _backfill_artifacts_conn(conn: aiosqlite.Connection) -> None:
    rows = await conn.execute_fetchall(
        """SELECT cf.*, c.user_id
           FROM conversation_files cf
           JOIN conversations c ON c.id=cf.conversation_id
           WHERE NOT EXISTS (
             SELECT 1 FROM artifacts a
             WHERE a.conversation_id=cf.conversation_id
               AND a.filename=cf.filename
               AND a.url=cf.url
           )"""
    )
    if not rows:
        return
    now = datetime.utcnow().isoformat()
    for row in rows:
        filename = row["filename"]
        mime_type = artifact_mime_type_for_filename(filename)
        kind = artifact_kind_for_filename(filename, mime_type)
        ws_rows = await conn.execute_fetchall(
            """SELECT wc.workspace_id
               FROM workspace_conversations wc
               JOIN workspaces w ON w.id=wc.workspace_id
               WHERE wc.conversation_id=? AND w.user_id=?
               ORDER BY wc.added_at DESC LIMIT 1""",
            (row["conversation_id"], row["user_id"]),
        )
        workspace_id = ws_rows[0]["workspace_id"] if ws_rows else None
        metadata = {"source": "conversation_files_backfill", "conversation_file_id": row["id"]}
        await conn.execute(
            """INSERT INTO artifacts(
                   id,user_id,conversation_id,workspace_id,filename,url,kind,mime_type,
                   title,description,metadata_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"art-{uuid.uuid4().hex[:12]}",
                row["user_id"] or DEFAULT_USER_ID,
                row["conversation_id"],
                workspace_id,
                filename,
                row["url"],
                kind,
                mime_type,
                filename,
                "",
                json.dumps(metadata),
                row["created_at"] or now,
                now,
            ),
        )


async def list_users() -> list[dict]:
    db = await get_db()
    try:
        await _ensure_default_user_conn(db)
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM users ORDER BY CASE id WHEN 'default' THEN 0 ELSE 1 END, name COLLATE NOCASE")
        users = []
        for row in rows:
            uid = row["id"]
            counts = {
                "conversation_count": (await db.execute_fetchall("SELECT COUNT(*) AS n FROM conversations WHERE user_id=?", (uid,)))[0]["n"],
                "workspace_count": (await db.execute_fetchall("SELECT COUNT(*) AS n FROM workspaces WHERE user_id=?", (uid,)))[0]["n"],
                "knowledge_base_count": (await db.execute_fetchall("SELECT COUNT(*) AS n FROM knowledge_bases WHERE user_id=?", (uid,)))[0]["n"],
            }
            users.append(_public_user(row, counts))
        return users
    finally:
        await db.close()


async def get_user(user_id: str | None = None) -> dict | None:
    uid = _scope_user(user_id)
    db = await get_db()
    try:
        await _ensure_default_user_conn(db)
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM users WHERE id=?", (uid,))
        return _public_user(rows[0]) if rows else None
    finally:
        await db.close()


async def get_user_private(user_id: str | None = None) -> dict | None:
    uid = _scope_user(user_id)
    db = await get_db()
    try:
        await _ensure_default_user_conn(db)
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM users WHERE id=?", (uid,))
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def create_user(name: str, password: str = "") -> dict:
    user_id = f"user-{uuid.uuid4().hex[:12]}"
    clean_name = _clean_user_name(name)
    now = datetime.utcnow().isoformat()
    # PBKDF2 with a high iteration count blocks the event loop for ~100ms;
    # run it in a worker thread.
    password_hash = await asyncio.to_thread(_hash_password, password) if password else ""
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users(id,name,password_hash,created_at,updated_at) VALUES(?,?,?,?,?)",
            (user_id, clean_name, password_hash, now, now),
        )
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM users WHERE id=?", (user_id,))
        return _public_user(rows[0])
    finally:
        await db.close()


async def create_user_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = _session_token_hash(token)
    now = datetime.utcnow().isoformat()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO user_sessions(token_hash,user_id,created_at,last_seen_at) VALUES(?,?,?,?)",
            (token_hash, user_id, now, now),
        )
        await db.execute("UPDATE users SET last_login_at=?, updated_at=? WHERE id=?", (now, now, user_id))
        await db.commit()
        return token
    finally:
        await db.close()


async def login_user(user_id: str, password: str = "") -> tuple[dict | None, str | None]:
    uid = _scope_user(user_id)
    db = await get_db()
    try:
        await _ensure_default_user_conn(db)
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM users WHERE id=?", (uid,))
        if not rows:
            return None, None
        row = dict(rows[0])
        if row.get("password_hash") and not await asyncio.to_thread(
                _verify_password, password or "", row["password_hash"]):
            return None, None
        now = datetime.utcnow().isoformat()
        token = None
        if row.get("password_hash"):
            token = secrets.token_urlsafe(32)
            await db.execute(
                "INSERT INTO user_sessions(token_hash,user_id,created_at,last_seen_at) VALUES(?,?,?,?)",
                (_session_token_hash(token), uid, now, now),
            )
        await db.execute("UPDATE users SET last_login_at=?, updated_at=? WHERE id=?", (now, now, uid))
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM users WHERE id=?", (uid,))
        return _public_user(rows[0]), token
    finally:
        await db.close()


async def validate_user_session(user_id: str, token: str | None) -> bool:
    uid = _scope_user(user_id)
    user = await get_user_private(uid)
    if not user:
        return False
    if not user.get("password_hash"):
        return True
    if not token:
        return False
    token_hash = _session_token_hash(token)
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT token_hash FROM user_sessions WHERE token_hash=? AND user_id=?",
            (token_hash, uid),
        )
        if not rows:
            return False
        await db.execute(
            "UPDATE user_sessions SET last_seen_at=? WHERE token_hash=?",
            (datetime.utcnow().isoformat(), token_hash),
        )
        await db.commit()
        return True
    finally:
        await db.close()


async def logout_user_session(user_id: str, token: str | None = None) -> None:
    uid = _scope_user(user_id)
    db = await get_db()
    try:
        if token:
            await db.execute(
                "DELETE FROM user_sessions WHERE user_id=? AND token_hash=?",
                (uid, _session_token_hash(token)),
            )
        else:
            await db.execute("DELETE FROM user_sessions WHERE user_id=?", (uid,))
        await db.commit()
    finally:
        await db.close()


async def owns_event_channel(channel_id: str, user_id: str | None = None) -> bool:
    """True when the id names a conversation OR research report owned by the
    current (scoped) user. Used to gate the /api/events SSE stream."""
    uid = _scope_user(user_id)
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id FROM conversations WHERE id=? AND user_id=?", (channel_id, uid))
        if rows:
            return True
        rows = await db.execute_fetchall(
            "SELECT id FROM research_reports WHERE id=? AND user_id=?", (channel_id, uid))
        return bool(rows)
    finally:
        await db.close()


async def update_user(user_id: str, *, name: str | None = None,
                      password: str | None = None, clear_password: bool = False) -> tuple[dict | None, str | None]:
    uid = _scope_user(user_id)
    fields = {}
    if name is not None:
        fields["name"] = _clean_user_name(name)
    if clear_password:
        fields["password_hash"] = ""
    elif password is not None:
        fields["password_hash"] = await asyncio.to_thread(_hash_password, password) if password else ""
    if not fields:
        return await get_user(uid), None
    fields["updated_at"] = datetime.utcnow().isoformat()
    sets = ",".join(f"{k}=?" for k in fields)
    db = await get_db()
    try:
        await db.execute(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), uid))
        if "password_hash" in fields:
            await db.execute("DELETE FROM user_sessions WHERE user_id=?", (uid,))
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM users WHERE id=?", (uid,))
        if not rows:
            return None, None
    finally:
        await db.close()
    return await get_user(uid), None


# ── User preference KV (server-side home for per-user UI state) ──

async def get_user_prefs(user_id: str | None = None) -> dict:
    """All prefs for the current user as {key: parsed_value}."""
    uid = _scope_user(user_id)
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT key, value_json FROM user_prefs WHERE user_id=?", (uid,))
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value_json"])
            except (json.JSONDecodeError, TypeError):
                continue
        return out
    finally:
        await db.close()


async def get_user_pref(key: str, user_id: str | None = None):
    uid = _scope_user(user_id)
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT value_json FROM user_prefs WHERE user_id=? AND key=?", (uid, key))
        if not rows:
            return None
        try:
            return json.loads(rows[0]["value_json"])
        except (json.JSONDecodeError, TypeError):
            return None
    finally:
        await db.close()


async def set_user_pref(key: str, value, user_id: str | None = None) -> None:
    uid = _scope_user(user_id)
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO user_prefs(user_id, key, value_json, updated_at)
               VALUES(?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id, key) DO UPDATE SET
                 value_json=excluded.value_json, updated_at=excluded.updated_at""",
            (uid, key, json.dumps(value)))
        await db.commit()
    finally:
        await db.close()


async def delete_user_pref(key: str, user_id: str | None = None) -> bool:
    uid = _scope_user(user_id)
    db = await get_db()
    try:
        cur = await db.execute(
            "DELETE FROM user_prefs WHERE user_id=? AND key=?", (uid, key))
        await db.commit()
        return (cur.rowcount or 0) > 0
    finally:
        await db.close()


async def _delete_user_conn(conn: aiosqlite.Connection, uid: str, *, allow_default: bool = False) -> bool:
    uid = _scope_user(uid)
    if uid == DEFAULT_USER_ID and not allow_default:
        return False
    rows = await conn.execute_fetchall("SELECT id FROM users WHERE id=?", (uid,))
    if not rows:
        return False
    await conn.execute("DELETE FROM conversation_files WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id=?)", (uid,))
    await conn.execute("DELETE FROM workspace_conversations WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id=?)", (uid,))
    await conn.execute("DELETE FROM workspace_conversations WHERE workspace_id IN (SELECT id FROM workspaces WHERE user_id=?)", (uid,))
    await conn.execute("DELETE FROM workspace_research_reports WHERE workspace_id IN (SELECT id FROM workspaces WHERE user_id=?)", (uid,))
    await conn.execute("DELETE FROM workspace_research_reports WHERE report_id IN (SELECT id FROM research_reports WHERE user_id=?)", (uid,))
    await conn.execute("DELETE FROM research_sources WHERE report_id IN (SELECT id FROM research_reports WHERE user_id=?)", (uid,))
    await conn.execute("DELETE FROM research_events WHERE report_id IN (SELECT id FROM research_reports WHERE user_id=?)", (uid,))
    await conn.execute("DELETE FROM run_events WHERE run_id IN (SELECT id FROM runs WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id=?))", (uid,))
    await conn.execute("DELETE FROM workspace_memory_blocks WHERE workspace_id IN (SELECT id FROM workspaces WHERE user_id=?)", (uid,))
    await conn.execute("DELETE FROM council_members WHERE council_id IN (SELECT id FROM council_configs WHERE user_id=?)", (uid,))
    await conn.execute("DELETE FROM artifact_tags WHERE artifact_id IN (SELECT id FROM artifacts WHERE user_id=?)", (uid,))
    await conn.execute("DELETE FROM artifact_workspaces WHERE artifact_id IN (SELECT id FROM artifacts WHERE user_id=?)", (uid,))
    await conn.execute("DELETE FROM artifact_events WHERE user_id=?", (uid,))
    for table in (
        "messages",
        "runs",
        "coder_workflows",
    ):
        await conn.execute(
            f"DELETE FROM {table} WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id=?)",
            (uid,),
        )
    for table in (
        "conversations", "knowledge_bases", "tools", "model_configs",
        "workspaces", "council_configs", "memories", "research_reports",
        "token_usage", "coding_projects",
        "artifacts",
        "mcp_servers", "openapi_connectors", "connector_tools",
    ):
        await conn.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
    await conn.execute("DELETE FROM user_prefs WHERE user_id=?", (uid,))
    await conn.execute("DELETE FROM user_profile WHERE id=?", (uid,))
    await conn.execute("DELETE FROM user_sessions WHERE user_id=?", (uid,))
    await conn.execute("DELETE FROM users WHERE id=?", (uid,))
    return True


async def delete_user(user_id: str, *, allow_default: bool = False) -> bool:
    uid = _scope_user(user_id)
    if uid == DEFAULT_USER_ID and not allow_default:
        return False
    db = await get_db()
    try:
        ok = await _delete_user_conn(db, uid, allow_default=allow_default)
        await db.commit()
        return ok
    finally:
        await db.close()


async def delete_users_except(keep_user_id: str) -> dict:
    keep_uid = _scope_user(keep_user_id)
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM users WHERE id != ?", (keep_uid,))
        deleted = 0
        for row in rows:
            if await _delete_user_conn(db, row["id"], allow_default=True):
                deleted += 1
        await db.commit()
        return {"deleted": deleted, "kept_user_id": keep_uid}
    finally:
        await db.close()


async def delete_all_users_and_data() -> dict:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM users")
        deleted = 0
        for row in rows:
            if await _delete_user_conn(db, row["id"], allow_default=True):
                deleted += 1
        for table in (
            "conversation_files", "workspace_conversations", "workspace_research_reports",
            "research_sources", "research_events", "run_events", "workspace_memory_blocks",
            "artifact_tags", "artifact_workspaces", "artifact_events", "council_members",
        ):
            await db.execute(f"DELETE FROM {table}")
        for table in (
            "messages", "runs", "coder_workflows", "conversations", "knowledge_bases",
            "tools", "model_configs", "workspaces", "council_configs", "memories",
            "research_reports", "token_usage", "coding_projects", "artifacts",
            "mcp_servers", "openapi_connectors", "connector_tools",
            "user_prefs", "user_profile", "user_sessions", "users",
        ):
            await db.execute(f"DELETE FROM {table}")
        await db.commit()
        return {"deleted": deleted}
    finally:
        await db.close()


async def init_db():
    db = await get_db()
    try:
        await db.executescript(DB_SCHEMA)
        await _ensure_default_user_conn(db)
        for table in (
            "conversations", "knowledge_bases", "tools", "model_configs",
            "workspaces", "council_configs", "memories", "token_usage",
            "coding_projects", "research_reports", "artifacts",
            "mcp_servers", "openapi_connectors", "connector_tools",
            "model_provider_credentials",
        ):
            await _ensure_user_column(db, table)
        for table in ("mcp_servers", "openapi_connectors"):
            for col, coltype, default in [
                ("allow_private", "INTEGER", "0"),
                ("enabled", "INTEGER", "1"),
                ("health", "TEXT", "'unknown'"),
                ("last_error", "TEXT", "''"),
                ("discovered_at", "TIMESTAMP", "NULL"),
            ]:
                try:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype} DEFAULT {default}")
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        print(f"[DB MIGRATION] Warning adding {table}.{col}: {e}")
        for col, coltype, default in [
            ("storage_path", "TEXT", "''"),
            ("size_bytes", "INTEGER", "0"),
            ("sha256", "TEXT", "''"),
            ("exists_status", "TEXT", "'unknown'"),
            ("last_checked_at", "TIMESTAMP", "NULL"),
            ("status", "TEXT", "'draft'"),
            ("pinned", "INTEGER", "0"),
            ("parent_artifact_id", "TEXT", "NULL"),
            ("supersedes_artifact_id", "TEXT", "NULL"),
            ("content_text", "TEXT", "''"),
            ("content_indexed_at", "TIMESTAMP", "NULL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE artifacts ADD COLUMN {col} {coltype} DEFAULT {default}")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"[DB MIGRATION] Warning adding artifacts.{col}: {e}")
        # Custom OpenAI-compatible provider config (base URL + display label)
        for col, coltype, default in [
            ("base_url", "TEXT", "''"),
            ("label", "TEXT", "''"),
        ]:
            try:
                await db.execute(f"ALTER TABLE model_provider_credentials ADD COLUMN {col} {coltype} DEFAULT {default}")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"[DB MIGRATION] Warning adding model_provider_credentials.{col}: {e}")
        # Thumbs feedback on messages (-1 / 0 / 1)
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN rating INTEGER DEFAULT 0")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"[DB MIGRATION] Warning adding messages.rating: {e}")
        # KB files ingested from a URL keep their source for provenance/re-sync
        try:
            await db.execute("ALTER TABLE kb_files ADD COLUMN source_url TEXT DEFAULT ''")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"[DB MIGRATION] Warning adding kb_files.source_url: {e}")
        # Context auto-compaction: rolling summary of turns older than the keep-window
        for col, coltype, default in [
            ("summary", "TEXT", "''"),
            ("summary_until_msg_id", "INTEGER", "0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE conversations ADD COLUMN {col} {coltype} DEFAULT {default}")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"[DB MIGRATION] Warning adding conversations.{col}: {e}")
        await db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_messages_conv_created_id ON messages(conversation_id, created_at, id);
            CREATE INDEX IF NOT EXISTS idx_kb_files_kb_source_url ON kb_files(kb_id, source_url);
            CREATE INDEX IF NOT EXISTS idx_knowledge_bases_user ON knowledge_bases(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_tools_user ON tools(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_model_configs_user ON model_configs(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_model_provider_credentials_user ON model_provider_credentials(user_id, provider);
            CREATE INDEX IF NOT EXISTS idx_workspaces_user ON workspaces(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_council_configs_user ON council_configs(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_token_usage_user ON token_usage(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_coding_projects_user ON coding_projects(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_research_reports_user ON research_reports(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_mcp_servers_user ON mcp_servers(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_openapi_connectors_user ON openapi_connectors(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_connector_tools_user ON connector_tools(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_connector_tools_connector ON connector_tools(user_id, connector_type, connector_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_user_created ON artifacts(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_artifacts_conv ON artifacts(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_workspace ON artifacts(workspace_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_workflow ON artifacts(workflow_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind);
            CREATE INDEX IF NOT EXISTS idx_artifacts_status ON artifacts(status);
            CREATE INDEX IF NOT EXISTS idx_artifacts_pinned ON artifacts(pinned);
            CREATE INDEX IF NOT EXISTS idx_artifacts_parent ON artifacts(parent_artifact_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_supersedes ON artifacts(supersedes_artifact_id);
            CREATE TABLE IF NOT EXISTS artifact_workspaces (
                artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (artifact_id, workspace_id)
            );
            CREATE INDEX IF NOT EXISTS idx_artifact_workspaces_ws ON artifact_workspaces(workspace_id, added_at);
            CREATE TABLE IF NOT EXISTS artifact_tags (
                artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (artifact_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_artifact_tags_tag ON artifact_tags(tag);
            CREATE TABLE IF NOT EXISTS artifact_events (
                id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL DEFAULT 'default',
                event_type TEXT NOT NULL,
                summary TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_artifact_events_artifact ON artifact_events(artifact_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_artifact_events_user ON artifact_events(user_id, created_at);
        """)
        # HyprChat automation now lives in external n8n; remove legacy internal
        # tables on startup so old installs do not keep stale definitions/runs.
        for table in ("workflow_schedules", "workflow_runs", "workflows"):
            await db.execute(f"DROP TABLE IF EXISTS {table}")
        # Migrate: add new columns to existing tables if missing
        for col, default in [("tool_ids", "'[]'"), ("persona_name", "''"), ("persona_avatar", "''"),
                              ("is_council", "0"), ("council_config_id", "NULL"), ("use_memories", "0"),
                              ("pinned", "0"),
                              ]:
            try:
                await db.execute(f"ALTER TABLE conversations ADD COLUMN {col} TEXT DEFAULT {default}")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"[DB MIGRATION] Warning: {e}")
        # Migrate workspaces: add reviewed-memory controls
        for col, coltype, default in [("memory_enabled", "INTEGER", "0"), ("instructions", "TEXT", "''")]:
            try:
                await db.execute(f"ALTER TABLE workspaces ADD COLUMN {col} {coltype} DEFAULT {default}")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"[DB MIGRATION] Warning: {e}")
        # Migrate memories: evolve the legacy global memory table into
        # workspace-scoped reviewed memories. Old rows remain valid but are not
        # injected unless assigned to a workspace.
        for col, coltype, default in [
            ("workspace_id", "TEXT", "NULL"),
            ("type", "TEXT", "'semantic'"),
            ("status", "TEXT", "'accepted'"),
            ("source_conversation_id", "TEXT", "NULL"),
            ("source_message_id", "INTEGER", "NULL"),
            ("confidence", "REAL", "0"),
            ("valid_from", "TIMESTAMP", "NULL"),
            ("valid_until", "TIMESTAMP", "NULL"),
            ("supersedes_id", "TEXT", "NULL"),
            ("entities_json", "TEXT", "'[]'"),
            ("metadata_json", "TEXT", "'{}'"),
            ("updated_at", "TIMESTAMP", "NULL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE memories ADD COLUMN {col} {coltype} DEFAULT {default}")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"[DB MIGRATION] Warning: {e}")
        await db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_memories_workspace ON memories(workspace_id);
            CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
            CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
            CREATE INDEX IF NOT EXISTS idx_memories_global_status ON memories(status, type) WHERE workspace_id IS NULL;
            CREATE TABLE IF NOT EXISTS user_profile (
                id TEXT PRIMARY KEY DEFAULT 'default',
                display_name TEXT DEFAULT '',
                legal_name TEXT DEFAULT '',
                birthday TEXT DEFAULT '',
                age TEXT DEFAULT '',
                bio TEXT DEFAULT '',
                interests_json TEXT DEFAULT '[]',
                links_json TEXT DEFAULT '[]',
                extra_json TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS workspace_memory_blocks (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                label TEXT NOT NULL,
                description TEXT DEFAULT '',
                value TEXT DEFAULT '',
                limit_chars INTEGER DEFAULT 1200,
                read_only INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(workspace_id, label)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_memory_blocks_ws ON workspace_memory_blocks(workspace_id);
        """)
        # Migrate council_configs: add debate_rounds and kb_ids columns
        for col, coltype, default in [("debate_rounds", "INTEGER", "0"), ("kb_ids", "TEXT", "'[]'")]:
            try:
                await db.execute(f"ALTER TABLE council_configs ADD COLUMN {col} {coltype} DEFAULT {default}")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"[DB MIGRATION] Warning: {e}")
        # Migrate council_members: linked persona profile support
        try:
            await db.execute("ALTER TABLE council_members ADD COLUMN model_config_id TEXT DEFAULT NULL")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"[DB MIGRATION] Warning: {e}")
        # Migrate conversations: add fork columns
        for col, default in [("forked_from", "NULL"), ("fork_point_msg_id", "NULL")]:
            try:
                await db.execute(f"ALTER TABLE conversations ADD COLUMN {col} TEXT DEFAULT {default}")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"[DB MIGRATION] Warning: {e}")
        # FTS5 full-text search index for messages
        try:
            await db.execute("SELECT * FROM messages_fts LIMIT 1")
        except Exception:
            await db.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    content,
                    content='messages',
                    content_rowid='id',
                    tokenize='porter unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE OF content ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
                    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
                END;
            """)
            # Rebuild index from existing messages
            await db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            print("[DB MIGRATION] Created FTS5 search index for messages")
        # FTS5 keyword index for KB chunks (hybrid RAG). Standalone table —
        # rag.py writes rows explicitly alongside ChromaDB, no triggers.
        try:
            await db.execute("SELECT * FROM kb_chunks_fts LIMIT 1")
        except Exception:
            await db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
                    text,
                    filename,
                    kb_id UNINDEXED,
                    chunk_id UNINDEXED,
                    chunk_index UNINDEXED,
                    tokenize='porter unicode61'
                )
            """)
            print("[DB MIGRATION] Created FTS5 keyword index for KB chunks")
        # The legacy Deep Researcher agent duplicates the first-class Deep
        # Research workspace and the Agent Research tool. Remove only the fixed
        # preset row; user-created research agents use their own IDs.
        try:
            await db.execute("DELETE FROM model_configs WHERE id='mc-preset-deepresearch' AND user_id=?", (DEFAULT_USER_ID,))
            await db.execute("DELETE FROM model_configs WHERE name='💻 Coder Bot' AND user_id=?", (DEFAULT_USER_ID,))
        except Exception as e:
            print(f"[DB SEED] Legacy agent cleanup failed: {e}")
        try:
            await _backfill_artifacts_conn(db)
            await _backfill_artifact_workspaces_conn(db)
        except Exception as e:
            print(f"[DB MIGRATION] Artifact backfill failed: {e}")
        await db.commit()
    finally:
        await db.close()


# ============================================================
# CONVERSATION CRUD
# ============================================================
async def create_conversation(
    id: str,
    title: str = "New Chat",
    model: str = "",
    system_prompt: str = "",
    model_config_id: str = None,
    use_memories: str = "0",
):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO conversations (id, user_id, title, model, system_prompt, model_config_id, use_memories) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                id,
                user_id,
                title,
                model,
                system_prompt,
                model_config_id,
                "1" if str(use_memories).lower() in {"1", "true", "yes", "on"} else "0",
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def get_conversations(limit: int = 50, offset: int = 0):
    user_id = _scope_user()
    db = await get_db()
    try:
        # The CAST(...pinned...) sort key defeats index-ordered scans on
        # purpose-built indexes, but conversation counts per user are small
        # (hundreds) and idx_conversations_user narrows to the user first, so
        # the in-memory sort is cheap. Leave as-is rather than adding a
        # computed/pinned index migration for legacy text-valued pinned rows.
        cursor = await db.execute(
            "SELECT * FROM conversations WHERE user_id=? ORDER BY CAST(COALESCE(pinned,'0') AS INTEGER) DESC, updated_at DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset)
        )
        rows = await cursor.fetchall()
        convs = []
        for r in rows:
            c = dict(r)
            try:
                c["tool_ids"] = json.loads(c.get("tool_ids", "[]"))
            except (json.JSONDecodeError, TypeError):
                c["tool_ids"] = []
            convs.append(c)
        return convs
    finally:
        await db.close()


async def get_conversation(id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM conversations WHERE id = ? AND user_id = ?", (id, user_id))
        row = await cursor.fetchone()
        if not row:
            return None
        conv = dict(row)
        try:
            conv["tool_ids"] = json.loads(conv.get("tool_ids", "[]"))
        except (json.JSONDecodeError, TypeError):
            conv["tool_ids"] = []
        cursor = await db.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, id ASC", (id,)
        )
        messages = await cursor.fetchall()
        parsed_msgs = []
        for m in messages:
            msg = dict(m)
            if isinstance(msg.get("metadata"), str):
                try:
                    msg["metadata"] = json.loads(msg["metadata"])
                except (json.JSONDecodeError, TypeError):
                    msg["metadata"] = {}
            elif msg.get("metadata") is None:
                msg["metadata"] = {}
            parsed_msgs.append(msg)
        conv["messages"] = parsed_msgs
        return conv
    finally:
        await db.close()


async def get_latest_user_message(conversation_id: str) -> dict | None:
    """Most recent user-role message for a conversation as {id, content}.

    Cheap single-row lookup for the chat agent's defensive-persist check —
    hydrating the whole conversation (every message + metadata parse) per
    turn just to peek at the last user row was the hot-path cost.
    Returns None when the conversation doesn't exist for this user or has
    no user messages yet.
    """
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT m.id, m.content FROM messages m
               JOIN conversations c ON c.id = m.conversation_id
               WHERE m.conversation_id=? AND c.user_id=? AND m.role='user'
               ORDER BY m.created_at DESC, m.id DESC LIMIT 1""",
            (conversation_id, user_id))
        if not rows:
            return None
        return {"id": rows[0]["id"], "content": rows[0]["content"] or ""}
    finally:
        await db.close()


async def get_conversation_summary(id: str) -> tuple[str, int]:
    """Rolling compaction summary + the highest message id folded into it."""
    if not id:
        return "", 0
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT summary, summary_until_msg_id FROM conversations WHERE id=? AND user_id=?",
            (id, user_id))
        if not rows:
            return "", 0
        return str(rows[0]["summary"] or ""), int(rows[0]["summary_until_msg_id"] or 0)
    finally:
        await db.close()


async def set_conversation_summary(id: str, summary: str, until_msg_id: int) -> None:
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(
            "UPDATE conversations SET summary=?, summary_until_msg_id=? WHERE id=? AND user_id=?",
            (summary or "", int(until_msg_id or 0), id, user_id))
        await db.commit()
    finally:
        await db.close()


async def get_conversation_title(id: str) -> str:
    if not id:
        return ""
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT title FROM conversations WHERE id=? AND user_id=?", (id, user_id))
        return str(rows[0]["title"] or "") if rows else ""
    finally:
        await db.close()


async def get_latest_user_message_id(conv_id: str) -> int:
    """Row id of the newest user message in a conversation (0 if none)."""
    if not conv_id:
        return 0
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT m.id FROM messages m JOIN conversations c ON c.id = m.conversation_id
               WHERE m.conversation_id=? AND c.user_id=? AND m.role='user'
               ORDER BY m.id DESC LIMIT 1""",
            (conv_id, user_id))
        return int(rows[0]["id"]) if rows else 0
    finally:
        await db.close()


async def get_conversation_use_memories(id: str) -> bool:
    if not id:
        return False
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT use_memories FROM conversations WHERE id=? AND user_id=?", (id, user_id))
        if not rows:
            return False
        return str(rows[0]["use_memories"] or "0").lower() in {"1", "true", "yes", "on"}
    finally:
        await db.close()


def _scrub_surrogates(v):
    """SQLite's UTF-8 bindings reject any string containing a surrogate
    codepoint (lone \\uD83D etc.). The frontend occasionally sends unpaired
    surrogates when a JavaScript string gets sliced mid-emoji. We combine any
    valid high/low pairs into their real codepoint, and replace truly lone
    surrogates with '?' so the UPDATE never 500s."""
    if not isinstance(v, str):
        return v
    if not any(0xD800 <= ord(c) <= 0xDFFF for c in v):
        return v  # fast path — no surrogates present
    out = []
    i = 0
    n = len(v)
    while i < n:
        co = ord(v[i])
        if 0xD800 <= co <= 0xDBFF and i + 1 < n:
            no = ord(v[i + 1])
            if 0xDC00 <= no <= 0xDFFF:
                out.append(chr(0x10000 + ((co - 0xD800) << 10) + (no - 0xDC00)))
                i += 2
                continue
        if 0xD800 <= co <= 0xDFFF:
            out.append("?")  # lone surrogate — replace
            i += 1
            continue
        out.append(v[i])
        i += 1
    return "".join(out)


async def update_conversation(id: str, **kwargs):
    if not kwargs:
        return
    user_id = _scope_user()
    db = await get_db()
    try:
        # Serialize list/dict fields
        if "tool_ids" in kwargs and isinstance(kwargs["tool_ids"], list):
            kwargs["tool_ids"] = json.dumps(kwargs["tool_ids"])
        if "use_memories" in kwargs:
            kwargs["use_memories"] = "1" if str(kwargs["use_memories"]).lower() in {"1", "true", "yes", "on"} else "0"
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = [_scrub_surrogates(v) for v in kwargs.values()] + [id, user_id]
        await db.execute(f"UPDATE conversations SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_conversation(id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM conversations WHERE id=? AND user_id=?", (id, user_id))
        if not rows:
            return
        # Explicitly delete related rows (don't rely solely on CASCADE)
        await db.execute("UPDATE artifacts SET conversation_id=NULL, message_id=NULL, updated_at=CURRENT_TIMESTAMP WHERE conversation_id=? AND user_id=?", (id, user_id))
        await db.execute("DELETE FROM messages WHERE conversation_id = ?", (id,))
        await db.execute("DELETE FROM conversation_files WHERE conversation_id = ?", (id,))
        await db.execute("DELETE FROM workspace_conversations WHERE conversation_id = ?", (id,))
        await db.execute("DELETE FROM conversations WHERE id = ?", (id,))
        await db.commit()
    finally:
        await db.close()


async def add_message(conversation_id: str, role: str, content: str, metadata: dict = None) -> int:
    """Insert a new message; return its auto-generated id so callers can update it later
    (e.g. chat agent persisting the assistant message progressively as rounds complete)."""
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id))
        if not rows:
            return 0
        cur = await db.execute(
            "INSERT INTO messages (conversation_id, role, content, metadata) VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, json.dumps(metadata or {}))
        )
        new_id = cur.lastrowid
        await db.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conversation_id,)
        )
        await db.commit()
        return new_id
    finally:
        await db.close()


async def update_message(message_id: int, *, content: str = None, metadata: dict = None) -> None:
    """Update content and/or metadata of an existing message. Used by the chat agent to
    save its assistant message progressively at round boundaries — so a mid-stream
    disconnect leaves a recoverable message in the conversation."""
    sets = []
    vals = []
    if content is not None:
        sets.append("content=?")
        vals.append(content)
    if metadata is not None:
        sets.append("metadata=?")
        vals.append(json.dumps(metadata))
    if not sets:
        return
    user_id = _scope_user()
    vals.extend([message_id, user_id])
    db = await get_db()
    try:
        await db.execute(
            f"""UPDATE messages SET {', '.join(sets)}
                WHERE id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)""",
            tuple(vals),
        )
        await db.commit()
    finally:
        await db.close()


async def delete_messages_from(conversation_id: str, from_id: int) -> list[int] | None:
    """Delete a message and everything after it in a conversation (regenerate/
    edit truncation). User-scoped. Returns the deleted message ids, or None
    when the conversation isn't owned by the current user.

    Resets the compaction summary when the truncation overlaps it — a summary
    folded over deleted turns would keep resurrecting their content."""
    user_id = _scope_user()
    from_id = int(from_id or 0)
    if not conversation_id or from_id <= 0:
        return None
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT summary_until_msg_id FROM conversations WHERE id=? AND user_id=?",
            (conversation_id, user_id))
        if not rows:
            return None
        anchor = await db.execute_fetchall(
            "SELECT created_at, id FROM messages WHERE id=? AND conversation_id=?",
            (from_id, conversation_id))
        if not anchor:
            return []
        doomed = await db.execute_fetchall(
            """SELECT id FROM messages WHERE conversation_id=?
               AND (created_at > ? OR (created_at = ? AND id >= ?))""",
            (conversation_id, anchor[0]["created_at"], anchor[0]["created_at"], from_id))
        ids = [r["id"] for r in doomed]
        if not ids:
            return []
        await db.execute(
            f"DELETE FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids)
        summary_until = int(rows[0]["summary_until_msg_id"] or 0)
        if summary_until >= from_id:
            await db.execute(
                "UPDATE conversations SET summary='', summary_until_msg_id=0 WHERE id=? AND user_id=?",
                (conversation_id, user_id))
        await db.commit()
        return ids
    finally:
        await db.close()


async def set_message_rating(message_id: int, rating: int) -> bool:
    """Thumbs feedback: -1, 0 (cleared), or 1. Stored as a real column — the
    metadata PATCH replaces metadata wholesale and would clobber it there."""
    rating = max(-1, min(1, int(rating)))
    user_id = _scope_user()
    db = await get_db()
    try:
        cur = await db.execute(
            """UPDATE messages SET rating=?
               WHERE id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)""",
            (rating, message_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def get_message_rating_counts(days: int = 0) -> dict:
    """Aggregate thumbs feedback for the current user. days <= 0 = all time."""
    user_id = _scope_user()
    db = await get_db()
    try:
        where = "WHERE c.user_id=? AND m.rating != 0"
        params: list = [user_id]
        if (days or 0) > 0:
            where += " AND m.created_at >= datetime('now', ?)"
            params.append(f"-{days} days")
        rows = await db.execute_fetchall(
            f"""SELECT SUM(m.rating=1) AS up, SUM(m.rating=-1) AS down
                FROM messages m JOIN conversations c ON c.id = m.conversation_id
                {where}""",
            tuple(params),
        )
        row = dict(rows[0]) if rows else {}
        return {"up": int(row.get("up") or 0), "down": int(row.get("down") or 0)}
    finally:
        await db.close()


async def delete_message(message_id: int) -> bool:
    user_id = _scope_user()
    db = await get_db()
    try:
        cur = await db.execute(
            "DELETE FROM messages WHERE id = ? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)",
            (message_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# ============================================================
# KNOWLEDGE BASE CRUD
# ============================================================
async def create_kb(id: str, name: str, description: str = ""):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO knowledge_bases (id, user_id, name, description) VALUES (?, ?, ?, ?)",
            (id, user_id, name, description)
        )
        await db.commit()
    finally:
        await db.close()


async def get_kbs():
    user_id = _scope_user()
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM knowledge_bases WHERE user_id=? ORDER BY updated_at DESC", (user_id,))
        kbs = [dict(r) for r in await cursor.fetchall()]
        if kbs:
            ids = [kb["id"] for kb in kbs]
            placeholders = ",".join("?" * len(ids))
            cursor = await db.execute(f"SELECT * FROM kb_files WHERE kb_id IN ({placeholders})", ids)
            files_by_kb = {}
            for f in await cursor.fetchall():
                fd = dict(f)
                files_by_kb.setdefault(fd["kb_id"], []).append(fd)
            for kb in kbs:
                kb["files"] = files_by_kb.get(kb["id"], [])
        return kbs
    finally:
        await db.close()


async def get_kb(kb_id: str):
    """Single-row KB lookup (user-scoped) with its files in one query.

    Avoids loading every KB + every KB file (the get_kbs N+1) just to find one
    row by id on hot upload/preview/add paths.
    """
    user_id = _scope_user()
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM knowledge_bases WHERE id=? AND user_id=?", (kb_id, user_id))
        row = await cursor.fetchone()
        if not row:
            return None
        kb = dict(row)
        cursor = await db.execute("SELECT * FROM kb_files WHERE kb_id = ?", (kb_id,))
        kb["files"] = [dict(f) for f in await cursor.fetchall()]
        return kb
    finally:
        await db.close()


async def add_kb_file(kb_id: str, filename: str, filepath: str, file_size: int, file_type: str,
                      source_url: str = "") -> int:
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM knowledge_bases WHERE id=? AND user_id=?", (kb_id, user_id))
        if not rows:
            return 0
        cursor = await db.execute(
            "INSERT INTO kb_files (kb_id, filename, filepath, file_size, file_type, source_url) VALUES (?, ?, ?, ?, ?, ?)",
            (kb_id, filename, filepath, file_size, file_type, source_url or "")
        )
        file_id = cursor.lastrowid
        await db.execute("UPDATE knowledge_bases SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (kb_id,))
        await db.commit()
        return file_id
    finally:
        await db.close()


async def get_kb_file_by_source_url(kb_id: str, source_url: str) -> dict | None:
    """Existing kb_file row for a URL, so re-adding the same URL overwrites
    in place (rag.index_file removes-first, making reingest idempotent)."""
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT f.* FROM kb_files f JOIN knowledge_bases k ON k.id = f.kb_id
               WHERE f.kb_id=? AND f.source_url=? AND k.user_id=?""",
            (kb_id, source_url, user_id),
        )
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def update_kb(id: str, **kwargs):
    user_id = _scope_user()
    db = await get_db()
    try:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [id, user_id]
        await db.execute(f"UPDATE knowledge_bases SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_kb(id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute("DELETE FROM knowledge_bases WHERE id = ? AND user_id = ?", (id, user_id))
        await db.commit()
    finally:
        await db.close()


async def delete_kb_file(file_id: int):
    user_id = _scope_user()
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT f.filepath FROM kb_files f
               JOIN knowledge_bases kb ON kb.id=f.kb_id
               WHERE f.id=? AND kb.user_id=?""",
            (file_id, user_id),
        )
        row = await cursor.fetchone()
        # Delete from DB first — if disk removal fails, at least DB is consistent
        await db.execute(
            "DELETE FROM kb_files WHERE id = ? AND kb_id IN (SELECT id FROM knowledge_bases WHERE user_id=?)",
            (file_id, user_id),
        )
        await db.commit()
        if row and os.path.exists(row["filepath"]):
            try:
                os.remove(row["filepath"])
            except OSError as e:
                print(f"[DB] Warning: could not remove file {row['filepath']}: {e}")
    finally:
        await db.close()


# ============================================================
# TOOL CRUD
# ============================================================
async def create_tool(id: str, name: str, description: str, filename: str, code: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO tools (id, user_id, name, description, filename, code) VALUES (?, ?, ?, ?, ?, ?)",
            (id, user_id, name, description, filename, code)
        )
        await db.commit()
    finally:
        await db.close()


async def get_tools():
    user_id = _scope_user()
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM tools WHERE user_id=? ORDER BY updated_at DESC", (user_id,))
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


async def update_tool(id: str, **kwargs):
    user_id = _scope_user()
    db = await get_db()
    try:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [id, user_id]
        await db.execute(f"UPDATE tools SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_tool(id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute("DELETE FROM tools WHERE id = ? AND user_id = ?", (id, user_id))
        await db.commit()
    finally:
        await db.close()


# ============================================================
# CONNECTOR CRUD
# ============================================================
def _json_load(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _json_dump(value, fallback):
    if value is None:
        value = fallback
    return json.dumps(value)


def _normalize_mcp_server(row: dict) -> dict:
    row["args"] = _json_load(row.pop("args_json", "[]"), [])
    row["env"] = _json_load(row.pop("env_json", "{}"), {})
    row["headers"] = _json_load(row.pop("headers_json", "{}"), {})
    row["enabled"] = bool(row.get("enabled", 1))
    row["allow_private"] = bool(row.get("allow_private", 0))
    return row


def _normalize_openapi_connector(row: dict) -> dict:
    row["auth"] = _json_load(row.pop("auth_json", "{}"), {})
    row["headers"] = _json_load(row.pop("headers_json", "{}"), {})
    row["enabled"] = bool(row.get("enabled", 1))
    row["allow_private"] = bool(row.get("allow_private", 0))
    return row


def _normalize_connector_tool(row: dict) -> dict:
    row["input_schema"] = _json_load(row.pop("input_schema_json", "{}"), {})
    row["metadata"] = _json_load(row.pop("metadata_json", "{}"), {})
    row["enabled"] = bool(row.get("enabled", 1))
    return row


async def create_mcp_server(
    id: str,
    name: str,
    transport: str = "stdio",
    command: str = "",
    url: str = "",
    args: list | None = None,
    env: dict | None = None,
    headers: dict | None = None,
    allow_private: bool = False,
    enabled: bool = True,
):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO mcp_servers
               (id,user_id,name,transport,command,url,args_json,env_json,headers_json,allow_private,enabled)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                id,
                user_id,
                name,
                transport,
                command,
                url,
                _json_dump(args or [], []),
                _json_dump(env or {}, {}),
                _json_dump(headers or {}, {}),
                1 if allow_private else 0,
                1 if enabled else 0,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def get_mcp_servers():
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM mcp_servers WHERE user_id=? ORDER BY updated_at DESC", (user_id,))
        out = []
        for r in rows:
            item = _normalize_mcp_server(dict(r))
            count = await db.execute_fetchall(
                "SELECT COUNT(*) AS n FROM connector_tools WHERE user_id=? AND connector_type='mcp' AND connector_id=?",
                (user_id, item["id"]),
            )
            item["tool_count"] = count[0]["n"] if count else 0
            out.append(item)
        return out
    finally:
        await db.close()


async def get_mcp_server(id: str) -> dict | None:
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM mcp_servers WHERE id=? AND user_id=?", (id, user_id))
        return _normalize_mcp_server(dict(rows[0])) if rows else None
    finally:
        await db.close()


async def update_mcp_server(id: str, **kwargs):
    user_id = _scope_user()
    db = await get_db()
    try:
        mapping = {
            "args": "args_json",
            "env": "env_json",
            "headers": "headers_json",
        }
        fields = {}
        for k, v in kwargs.items():
            col = mapping.get(k, k)
            if k in mapping:
                v = _json_dump(v, [] if k == "args" else {})
            elif k in {"enabled", "allow_private"}:
                v = 1 if v else 0
            fields[col] = v
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [id, user_id]
        await db.execute(f"UPDATE mcp_servers SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_mcp_server(id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute("DELETE FROM connector_tools WHERE user_id=? AND connector_type='mcp' AND connector_id=?", (user_id, id))
        await db.execute("DELETE FROM mcp_servers WHERE id=? AND user_id=?", (id, user_id))
        await db.commit()
    finally:
        await db.close()


async def create_openapi_connector(
    id: str,
    name: str,
    spec_url: str = "",
    spec_json: str = "",
    base_url: str = "",
    auth: dict | None = None,
    headers: dict | None = None,
    allow_private: bool = False,
    enabled: bool = True,
):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO openapi_connectors
               (id,user_id,name,spec_url,spec_json,base_url,auth_json,headers_json,allow_private,enabled)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                id,
                user_id,
                name,
                spec_url,
                spec_json,
                base_url,
                _json_dump(auth or {}, {}),
                _json_dump(headers or {}, {}),
                1 if allow_private else 0,
                1 if enabled else 0,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def get_openapi_connectors():
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM openapi_connectors WHERE user_id=? ORDER BY updated_at DESC", (user_id,))
        out = []
        for r in rows:
            item = _normalize_openapi_connector(dict(r))
            count = await db.execute_fetchall(
                "SELECT COUNT(*) AS n FROM connector_tools WHERE user_id=? AND connector_type='openapi' AND connector_id=?",
                (user_id, item["id"]),
            )
            item["tool_count"] = count[0]["n"] if count else 0
            out.append(item)
        return out
    finally:
        await db.close()


async def get_openapi_connector(id: str) -> dict | None:
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM openapi_connectors WHERE id=? AND user_id=?", (id, user_id))
        return _normalize_openapi_connector(dict(rows[0])) if rows else None
    finally:
        await db.close()


async def update_openapi_connector(id: str, **kwargs):
    user_id = _scope_user()
    db = await get_db()
    try:
        mapping = {
            "auth": "auth_json",
            "headers": "headers_json",
        }
        fields = {}
        for k, v in kwargs.items():
            col = mapping.get(k, k)
            if k in mapping:
                v = _json_dump(v, {})
            elif k in {"enabled", "allow_private"}:
                v = 1 if v else 0
            fields[col] = v
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [id, user_id]
        await db.execute(f"UPDATE openapi_connectors SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_openapi_connector(id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute("DELETE FROM connector_tools WHERE user_id=? AND connector_type='openapi' AND connector_id=?", (user_id, id))
        await db.execute("DELETE FROM openapi_connectors WHERE id=? AND user_id=?", (id, user_id))
        await db.commit()
    finally:
        await db.close()


async def get_connector_tools(connector_type: str | None = None, connector_id: str | None = None, enabled_only: bool = True):
    user_id = _scope_user()
    db = await get_db()
    try:
        wheres = ["user_id=?"]
        vals = [user_id]
        if connector_type:
            wheres.append("connector_type=?")
            vals.append(connector_type)
        if connector_id:
            wheres.append("connector_id=?")
            vals.append(connector_id)
        if enabled_only:
            wheres.append("enabled=1")
            wheres.append(
                """(
                    (connector_type='mcp' AND EXISTS (
                        SELECT 1 FROM mcp_servers ms
                        WHERE ms.id=connector_tools.connector_id
                          AND ms.user_id=connector_tools.user_id
                          AND ms.enabled=1
                    ))
                    OR
                    (connector_type='openapi' AND EXISTS (
                        SELECT 1 FROM openapi_connectors oc
                        WHERE oc.id=connector_tools.connector_id
                          AND oc.user_id=connector_tools.user_id
                          AND oc.enabled=1
                    ))
                )"""
            )
        rows = await db.execute_fetchall(
            f"SELECT * FROM connector_tools WHERE {' AND '.join(wheres)} ORDER BY connector_type, display_name",
            tuple(vals),
        )
        return [_normalize_connector_tool(dict(r)) for r in rows]
    finally:
        await db.close()


async def replace_connector_tools(connector_type: str, connector_id: str, tools: list[dict]):
    user_id = _scope_user()
    now = datetime.utcnow().isoformat()
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM connector_tools WHERE user_id=? AND connector_type=? AND connector_id=?",
            (user_id, connector_type, connector_id),
        )
        for item in tools:
            await db.execute(
                """INSERT INTO connector_tools
                   (id,user_id,connector_type,connector_id,external_name,tool_name,display_name,description,
                    input_schema_json,metadata_json,enabled,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["id"],
                    user_id,
                    connector_type,
                    connector_id,
                    item["external_name"],
                    item["tool_name"],
                    item.get("display_name") or item["external_name"],
                    item.get("description", ""),
                    _json_dump(item.get("input_schema") or {}, {}),
                    _json_dump(item.get("metadata") or {}, {}),
                    1 if item.get("enabled", True) else 0,
                    now,
                    now,
                ),
            )
        await db.commit()
    finally:
        await db.close()


# ============================================================
# MODEL PROVIDER CREDENTIALS
# ============================================================
def _normalize_model_provider(row: dict, include_secret: bool = False) -> dict:
    row["enabled"] = bool(row.get("enabled", 1))
    row["has_key"] = bool(row.get("api_key"))
    if not include_secret:
        row.pop("api_key", None)
    return row


async def list_model_provider_credentials(include_secret: bool = False) -> list[dict]:
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM model_provider_credentials WHERE user_id=? ORDER BY provider",
            (user_id,),
        )
        return [_normalize_model_provider(dict(r), include_secret=include_secret) for r in rows]
    finally:
        await db.close()


async def get_model_provider_credential(provider: str, include_secret: bool = False) -> dict | None:
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM model_provider_credentials WHERE user_id=? AND provider=?",
            (user_id, provider),
        )
        return _normalize_model_provider(dict(rows[0]), include_secret=include_secret) if rows else None
    finally:
        await db.close()


async def upsert_model_provider_credential(
    provider: str,
    *,
    api_key: str | None = None,
    enabled: bool | None = None,
    base_url: str | None = None,
    label: str | None = None,
    last_test_status: str | None = None,
    last_test_error: str | None = None,
    last_test_at: str | None = None,
) -> None:
    user_id = _scope_user()
    existing = await get_model_provider_credential(provider, include_secret=True)
    fields = {
        "api_key": existing.get("api_key", "") if existing else "",
        "enabled": existing.get("enabled", True) if existing else True,
        "base_url": existing.get("base_url", "") if existing else "",
        "label": existing.get("label", "") if existing else "",
        "last_test_status": existing.get("last_test_status", "") if existing else "",
        "last_test_error": existing.get("last_test_error", "") if existing else "",
        "last_test_at": existing.get("last_test_at") if existing else None,
    }
    if api_key is not None:
        fields["api_key"] = api_key
    if enabled is not None:
        fields["enabled"] = bool(enabled)
    if base_url is not None:
        fields["base_url"] = base_url.strip()
    if label is not None:
        fields["label"] = label.strip()
    if last_test_status is not None:
        fields["last_test_status"] = last_test_status
    if last_test_error is not None:
        fields["last_test_error"] = last_test_error
    if last_test_at is not None:
        fields["last_test_at"] = last_test_at

    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO model_provider_credentials
               (user_id, provider, api_key, enabled, base_url, label, last_test_status, last_test_error, last_test_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, provider) DO UPDATE SET
                 api_key=excluded.api_key,
                 enabled=excluded.enabled,
                 base_url=excluded.base_url,
                 label=excluded.label,
                 last_test_status=excluded.last_test_status,
                 last_test_error=excluded.last_test_error,
                 last_test_at=excluded.last_test_at,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                user_id,
                provider,
                fields["api_key"],
                1 if fields["enabled"] else 0,
                fields["base_url"],
                fields["label"],
                fields["last_test_status"],
                fields["last_test_error"],
                fields["last_test_at"],
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def delete_model_provider_credential(provider: str) -> bool:
    user_id = _scope_user()
    db = await get_db()
    try:
        cur = await db.execute(
            "DELETE FROM model_provider_credentials WHERE user_id=? AND provider=?",
            (user_id, provider),
        )
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# ============================================================
# MODEL CONFIG CRUD
# ============================================================
async def create_model_config(id: str, name: str, base_model: str, system_prompt: str = "",
                               tool_ids: list = None, kb_ids: list = None, parameters: dict = None):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO model_configs (id, user_id, name, base_model, system_prompt, tool_ids, kb_ids, parameters) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (id, user_id, name, base_model, system_prompt, json.dumps(tool_ids or []), json.dumps(kb_ids or []), json.dumps(parameters or {}))
        )
        await db.commit()
    finally:
        await db.close()


async def get_model_configs():
    user_id = _scope_user()
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM model_configs WHERE user_id=? ORDER BY updated_at DESC", (user_id,))
        configs = [dict(r) for r in await cursor.fetchall()]
        for c in configs:
            c["tool_ids"] = json.loads(c["tool_ids"])
            c["kb_ids"] = json.loads(c["kb_ids"])
            c["parameters"] = json.loads(c["parameters"])
        return configs
    finally:
        await db.close()


async def get_model_config(mc_id: str) -> dict | None:
    """Return a single model config by id, or None if not found."""
    user_id = _scope_user()
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM model_configs WHERE id = ? AND user_id = ?", (mc_id, user_id))
        row = await cursor.fetchone()
        if not row:
            return None
        c = dict(row)
        c["tool_ids"] = json.loads(c["tool_ids"])
        c["kb_ids"] = json.loads(c["kb_ids"])
        c["parameters"] = json.loads(c["parameters"])
        return c
    finally:
        await db.close()


async def update_model_config(id: str, **kwargs):
    user_id = _scope_user()
    db = await get_db()
    try:
        for k in ("tool_ids", "kb_ids", "parameters"):
            if k in kwargs:
                kwargs[k] = json.dumps(kwargs[k])
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [id, user_id]
        await db.execute(f"UPDATE model_configs SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_model_config(id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute("DELETE FROM model_configs WHERE id = ? AND user_id = ?", (id, user_id))
        await db.commit()
    finally:
        await db.close()


# ============================================================
# WORKSPACE CRUD
# ============================================================
async def create_workspace(id: str, name: str, description: str = ""):
    user_id = _scope_user()
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO workspaces(id,user_id,name,description,topics,memory_enabled,instructions,created_at,updated_at) VALUES(?,?,?,?, '[]',0,'',?,?)",
            (id, user_id, name, description, now, now)
        )
        await db.commit()
    finally:
        await db.close()
    return {
        "id": id, "name": name, "description": description, "topics": [],
        "memory_enabled": 0, "instructions": "",
        "conv_count": 0, "file_count": 0, "report_count": 0,
    }


async def get_workspaces():
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("""
            SELECT w.*,
                (SELECT COUNT(*) FROM workspace_conversations wc WHERE wc.workspace_id=w.id) as conv_count,
                (SELECT COUNT(DISTINCT a.id) FROM artifacts a
                   LEFT JOIN workspace_conversations wc2 ON a.conversation_id=wc2.conversation_id
                  WHERE a.user_id=w.user_id
                    AND (a.workspace_id=w.id OR wc2.workspace_id=w.id
                         OR EXISTS (SELECT 1 FROM artifact_workspaces aw WHERE aw.artifact_id=a.id AND aw.workspace_id=w.id))) as file_count,
                (SELECT COUNT(*) FROM workspace_research_reports wrr WHERE wrr.workspace_id=w.id) as report_count
            FROM workspaces w WHERE w.user_id=? ORDER BY w.updated_at DESC""", (user_id,))
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["topics"] = json.loads(d.get("topics", "[]"))
            except Exception:
                d["topics"] = []
            result.append(d)
        return result
    finally:
        await db.close()


async def get_workspace(ws_id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        row = await db.execute_fetchall("SELECT * FROM workspaces WHERE id=? AND user_id=?", (ws_id, user_id))
        if not row:
            return None
        ws = dict(row[0])
        try:
            ws["topics"] = json.loads(ws.get("topics", "[]"))
        except Exception:
            ws["topics"] = []
        conv_rows = await db.execute_fetchall(
            """SELECT c.id,c.title,c.model,c.updated_at FROM conversations c
               JOIN workspace_conversations wc ON c.id=wc.conversation_id
               WHERE wc.workspace_id=? AND c.user_id=? ORDER BY wc.added_at DESC""",
            (ws_id, user_id)
        )
        ws["conversations"] = [dict(r) for r in conv_rows]
        file_rows = await db.execute_fetchall(
            """SELECT a.*, c.title AS conversation_title, w.name AS workspace_name
               FROM artifacts a
               LEFT JOIN conversations c ON c.id=a.conversation_id
               LEFT JOIN workspaces w ON w.id=a.workspace_id
               LEFT JOIN workspace_conversations wc
                      ON wc.conversation_id=a.conversation_id AND wc.workspace_id=?
               WHERE a.user_id=?
                 AND (a.workspace_id=? OR wc.workspace_id=?
                      OR EXISTS (SELECT 1 FROM artifact_workspaces aw WHERE aw.artifact_id=a.id AND aw.workspace_id=?))
               ORDER BY a.created_at DESC""",
            (ws_id, user_id, ws_id, ws_id, ws_id),
        )
        ws["files"] = await _hydrate_artifact_rows_conn(db, [_normalize_artifact_row(r) for r in file_rows])
        report_rows = await db.execute_fetchall(
            """SELECT rr.id,rr.title,rr.query,rr.focus,rr.report_type,rr.status,rr.depth,
                      rr.model,rr.summary,rr.error,rr.sources_json,rr.metrics_json,
                      rr.created_at,rr.updated_at,rr.completed_at,wrr.added_at
               FROM research_reports rr
               JOIN workspace_research_reports wrr ON rr.id=wrr.report_id
               WHERE wrr.workspace_id=? AND rr.user_id=? ORDER BY wrr.added_at DESC""",
            (ws_id, user_id)
        )
        reports = []
        for row in report_rows:
            r = dict(row)
            try:
                sources = json.loads(r.pop("sources_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                sources = []
            try:
                metrics = json.loads(r.pop("metrics_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                metrics = {}
            r["source_count"] = metrics.get("source_count") or len(sources)
            r["pages_read"] = metrics.get("pages_read", 0)
            r["elapsed"] = metrics.get("elapsed", 0)
            r["metrics"] = metrics
            reports.append(r)
        ws["reports"] = reports
        return ws
    finally:
        await db.close()


async def update_workspace(ws_id: str, **kwargs):
    allowed = {"name", "description", "topics", "memory_enabled", "instructions"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    if "topics" in fields and isinstance(fields["topics"], list):
        fields["topics"] = json.dumps(fields["topics"])
    if "memory_enabled" in fields:
        fields["memory_enabled"] = 1 if str(fields["memory_enabled"]).lower() in {"1", "true", "yes", "on"} else 0
    fields["updated_at"] = datetime.utcnow().isoformat()
    set_clause = ",".join(f"{k}=?" for k in fields)
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(f"UPDATE workspaces SET {set_clause} WHERE id=? AND user_id=?", (*fields.values(), ws_id, user_id))
        await db.commit()
    finally:
        await db.close()


async def delete_workspace(ws_id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM artifact_workspaces WHERE workspace_id IN (SELECT id FROM workspaces WHERE id=? AND user_id=?)",
            (ws_id, user_id),
        )
        await db.execute("UPDATE artifacts SET workspace_id=NULL, updated_at=CURRENT_TIMESTAMP WHERE workspace_id=? AND user_id=?", (ws_id, user_id))
        await db.execute("DELETE FROM workspaces WHERE id=? AND user_id=?", (ws_id, user_id))
        await db.commit()
    finally:
        await db.close()


async def add_conv_to_workspace(ws_id: str, conv_id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT w.id FROM workspaces w
               JOIN conversations c ON c.id=?
               WHERE w.id=? AND w.user_id=? AND c.user_id=?""",
            (conv_id, ws_id, user_id, user_id),
        )
        if not rows:
            return
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO workspace_conversations VALUES(?,?,?)",
            (ws_id, conv_id, now)
        )
        await db.execute("UPDATE workspaces SET updated_at=? WHERE id=?", (now, ws_id))
        await db.commit()
    finally:
        await db.close()


async def remove_conv_from_workspace(ws_id: str, conv_id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(
            """DELETE FROM workspace_conversations
               WHERE workspace_id IN (SELECT id FROM workspaces WHERE id=? AND user_id=?)
                 AND conversation_id IN (SELECT id FROM conversations WHERE id=? AND user_id=?)""",
            (ws_id, user_id, conv_id, user_id)
        )
        await db.commit()
    finally:
        await db.close()


async def add_research_report_to_workspace(ws_id: str, report_id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT w.id FROM workspaces w
               JOIN research_reports rr ON rr.id=?
               WHERE w.id=? AND w.user_id=? AND rr.user_id=?""",
            (report_id, ws_id, user_id, user_id),
        )
        if not rows:
            return
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO workspace_research_reports(workspace_id,report_id,added_at) VALUES(?,?,?)",
            (ws_id, report_id, now)
        )
        await db.execute("UPDATE workspaces SET updated_at=? WHERE id=?", (now, ws_id))
        await db.commit()
    finally:
        await db.close()


async def remove_research_report_from_workspace(ws_id: str, report_id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(
            """DELETE FROM workspace_research_reports
               WHERE workspace_id IN (SELECT id FROM workspaces WHERE id=? AND user_id=?)
                 AND report_id IN (SELECT id FROM research_reports WHERE id=? AND user_id=?)""",
            (ws_id, user_id, report_id, user_id)
        )
        await db.execute("UPDATE workspaces SET updated_at=? WHERE id=? AND user_id=?", (datetime.utcnow().isoformat(), ws_id, user_id))
        await db.commit()
    finally:
        await db.close()


async def _valid_workspace_conn(conn: aiosqlite.Connection, ws_id: str | None, user_id: str) -> str | None:
    if not ws_id:
        return None
    rows = await conn.execute_fetchall("SELECT id FROM workspaces WHERE id=? AND user_id=?", (ws_id, user_id))
    return ws_id if rows else None


async def _default_workspace_for_conversation_conn(conn: aiosqlite.Connection, conv_id: str, user_id: str) -> str | None:
    rows = await conn.execute_fetchall(
        """SELECT wc.workspace_id
           FROM workspace_conversations wc
           JOIN workspaces w ON w.id=wc.workspace_id
           WHERE wc.conversation_id=? AND w.user_id=?
           ORDER BY wc.added_at DESC LIMIT 1""",
        (conv_id, user_id),
    )
    return rows[0]["workspace_id"] if rows else None


async def _valid_message_conn(conn: aiosqlite.Connection, message_id: int | None, conv_id: str | None, user_id: str) -> int | None:
    if not message_id:
        return None
    rows = await conn.execute_fetchall(
        """SELECT m.id FROM messages m
           JOIN conversations c ON c.id=m.conversation_id
           WHERE m.id=? AND c.user_id=? AND (? IS NULL OR m.conversation_id=?)""",
        (int(message_id), user_id, conv_id, conv_id),
    )
    return int(message_id) if rows else None


async def _valid_run_conn(conn: aiosqlite.Connection, run_id: str | None, user_id: str) -> str | None:
    if not run_id:
        return None
    rows = await conn.execute_fetchall(
        "SELECT id FROM runs WHERE id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)",
        (run_id, user_id),
    )
    return run_id if rows else None


async def _valid_workflow_conn(conn: aiosqlite.Connection, workflow_id: str | None, user_id: str) -> str | None:
    if not workflow_id:
        return None
    rows = await conn.execute_fetchall(
        "SELECT id FROM coder_workflows WHERE id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)",
        (workflow_id, user_id),
    )
    return workflow_id if rows else None


async def add_artifact(
    *,
    artifact_id: str | None = None,
    conversation_id: str | None = None,
    message_id: int | None = None,
    workspace_id: str | None = None,
    workspace_ids: list[str] | None = None,
    run_id: str | None = None,
    workflow_id: str | None = None,
    filename: str,
    url: str,
    kind: str | None = None,
    mime_type: str | None = None,
    title: str | None = None,
    description: str | None = None,
    storage_path: str | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
    exists_status: str | None = None,
    status: str | None = None,
    pinned: bool | int = False,
    parent_artifact_id: str | None = None,
    supersedes_artifact_id: str | None = None,
    content_text: str | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> dict | None:
    user_id = _scope_user()
    db = await get_db()
    try:
        conv_id = (conversation_id or "").strip() or None
        if conv_id:
            rows = await db.execute_fetchall("SELECT id FROM conversations WHERE id=? AND user_id=?", (conv_id, user_id))
            if not rows:
                return None
        workspace_id = await _valid_workspace_conn(db, workspace_id, user_id)
        if not workspace_id and conv_id:
            workspace_id = await _default_workspace_for_conversation_conn(db, conv_id, user_id)
        message_id = await _valid_message_conn(db, message_id, conv_id, user_id)
        run_id = await _valid_run_conn(db, run_id, user_id)
        workflow_id = await _valid_workflow_conn(db, workflow_id, user_id)
        mime_type = (mime_type or artifact_mime_type_for_filename(filename)).strip()
        kind = _normalize_artifact_kind(kind or "", filename, mime_type)
        for linked_id in (parent_artifact_id, supersedes_artifact_id):
            if linked_id:
                rows = await db.execute_fetchall("SELECT id FROM artifacts WHERE id=? AND user_id=?", (linked_id, user_id))
                if not rows:
                    return None
        status = (status or "draft").strip().lower()
        if status not in _ARTIFACT_STATUSES:
            status = "draft"
        exists_status = (exists_status or "unknown").strip().lower() or "unknown"
        content_text = _scrub_surrogates(str(content_text or ""))[:800000]
        now = datetime.utcnow().isoformat()
        artifact_id = artifact_id or f"art-{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO artifacts(
                   id,user_id,conversation_id,message_id,workspace_id,run_id,workflow_id,
                   filename,url,kind,mime_type,title,description,storage_path,size_bytes,sha256,
                   exists_status,last_checked_at,status,pinned,parent_artifact_id,supersedes_artifact_id,
                   content_text,content_indexed_at,metadata_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                artifact_id, user_id, conv_id, message_id, workspace_id, run_id, workflow_id,
                filename, url, kind, mime_type, title or filename, description or "",
                storage_path or "", int(size_bytes or 0), sha256 or "",
                exists_status, now if exists_status != "unknown" else None, status,
                1 if pinned else 0, parent_artifact_id, supersedes_artifact_id,
                content_text, now if content_text else None, json.dumps(metadata or {}), now, now,
            ),
        )
        if workspace_ids is None:
            workspace_ids = [workspace_id] if workspace_id else []
        await _set_artifact_workspaces_conn(db, artifact_id, workspace_ids, user_id)
        await _set_artifact_tags_conn(db, artifact_id, tags)
        await db.commit()
        return await _get_artifact_conn(db, artifact_id, user_id)
    finally:
        await db.close()


async def add_conversation_file(
    id: str,
    conv_id: str,
    filename: str,
    url: str,
    *,
    message_id: int | None = None,
    workspace_id: str | None = None,
    workspace_ids: list[str] | None = None,
    run_id: str | None = None,
    workflow_id: str | None = None,
    kind: str | None = None,
    mime_type: str | None = None,
    title: str | None = None,
    description: str | None = None,
    storage_path: str | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
    exists_status: str | None = None,
    status: str | None = None,
    pinned: bool | int = False,
    parent_artifact_id: str | None = None,
    supersedes_artifact_id: str | None = None,
    content_text: str | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> dict | None:
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM conversations WHERE id=? AND user_id=?", (conv_id, user_id))
        if not rows:
            return None
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO conversation_files(id,conversation_id,filename,url,created_at) VALUES(?,?,?,?,?)",
            (id, conv_id, filename, url, now)
        )
        workspace_id = await _valid_workspace_conn(db, workspace_id, user_id)
        if not workspace_id:
            workspace_id = await _default_workspace_for_conversation_conn(db, conv_id, user_id)
        message_id = await _valid_message_conn(db, message_id, conv_id, user_id)
        run_id = await _valid_run_conn(db, run_id, user_id)
        workflow_id = await _valid_workflow_conn(db, workflow_id, user_id)
        mime_type = (mime_type or artifact_mime_type_for_filename(filename)).strip()
        kind = _normalize_artifact_kind(kind or "", filename, mime_type)
        for linked_id in (parent_artifact_id, supersedes_artifact_id):
            if linked_id:
                linked_rows = await db.execute_fetchall("SELECT id FROM artifacts WHERE id=? AND user_id=?", (linked_id, user_id))
                if not linked_rows:
                    return None
        status = (status or "draft").strip().lower()
        if status not in _ARTIFACT_STATUSES:
            status = "draft"
        exists_status = (exists_status or "unknown").strip().lower() or "unknown"
        content_text = _scrub_surrogates(str(content_text or ""))[:800000]
        artifact_id = f"art-{uuid.uuid4().hex[:12]}"
        metadata = {**(metadata or {}), "conversation_file_id": id}
        await db.execute(
            """INSERT INTO artifacts(
                   id,user_id,conversation_id,message_id,workspace_id,run_id,workflow_id,
                   filename,url,kind,mime_type,title,description,storage_path,size_bytes,sha256,
                   exists_status,last_checked_at,status,pinned,parent_artifact_id,supersedes_artifact_id,
                   content_text,content_indexed_at,metadata_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                artifact_id, user_id, conv_id, message_id, workspace_id, run_id, workflow_id,
                filename, url, kind, mime_type, title or filename, description or "",
                storage_path or "", int(size_bytes or 0), sha256 or "",
                exists_status, now if exists_status != "unknown" else None, status,
                1 if pinned else 0, parent_artifact_id, supersedes_artifact_id,
                content_text, now if content_text else None, json.dumps(metadata), now, now,
            ),
        )
        if workspace_ids is None:
            workspace_ids = [workspace_id] if workspace_id else []
        await _set_artifact_workspaces_conn(db, artifact_id, workspace_ids, user_id)
        await _set_artifact_tags_conn(db, artifact_id, tags)
        await db.commit()
        return await _get_artifact_conn(db, artifact_id, user_id)
    finally:
        await db.close()


async def list_artifacts(
    *,
    conversation_id: str | None = None,
    workspace_id: str | None = None,
    run_id: str | None = None,
    workflow_id: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    pinned: bool | int | str | None = None,
    tag: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    source: str | None = None,
    limit: int = 80,
    offset: int = 0,
) -> list[dict]:
    user_id = _scope_user()
    limit = max(1, min(200, int(limit or 80)))
    offset = max(0, int(offset or 0))
    clauses = ["a.user_id=?"]
    params: list = [user_id]
    if conversation_id:
        clauses.append("a.conversation_id=?")
        params.append(conversation_id)
    if run_id:
        clauses.append("a.run_id=?")
        params.append(run_id)
    if workflow_id:
        clauses.append("a.workflow_id=?")
        params.append(workflow_id)
    if workspace_id:
        clauses.append(
            """(a.workspace_id=?
                OR EXISTS (SELECT 1 FROM artifact_workspaces aw WHERE aw.artifact_id=a.id AND aw.workspace_id=?)
                OR a.conversation_id IN (
                    SELECT wc.conversation_id FROM workspace_conversations wc
                    JOIN workspaces w ON w.id=wc.workspace_id
                    WHERE wc.workspace_id=? AND w.user_id=?
                ))"""
        )
        params.extend([workspace_id, workspace_id, workspace_id, user_id])
    kind_norm = (kind or "").strip().lower()
    if kind_norm and kind_norm != "all":
        if kind_norm == "recent":
            pass
        if kind_norm == "project":
            clauses.append("(a.kind='archive' AND (a.workflow_id IS NOT NULL OR a.metadata_json LIKE '%\"project_id\"%'))")
        elif kind_norm in _ARTIFACT_KIND_SET:
            clauses.append("a.kind=?")
            params.append(_normalize_artifact_kind(kind_norm))
    status_norm = (status or "").strip().lower()
    if status_norm and status_norm != "all":
        if status_norm in _ARTIFACT_STATUSES:
            clauses.append("a.status=?")
            params.append(status_norm)
    if pinned is not None and str(pinned).lower() not in {"", "all"}:
        clauses.append("a.pinned=?")
        params.append(1 if str(pinned).lower() in {"1", "true", "yes", "on"} else 0)
    tag_norm = _clean_artifact_tags([tag])[0] if tag else ""
    if tag_norm:
        clauses.append("EXISTS (SELECT 1 FROM artifact_tags at WHERE at.artifact_id=a.id AND at.tag=?)")
        params.append(tag_norm)
    if date_from:
        clauses.append("a.created_at>=?")
        params.append(date_from)
    if date_to:
        clauses.append("a.created_at<=?")
        params.append(date_to)
    if source:
        like = f"%{source.strip()}%"
        clauses.append("(a.metadata_json LIKE ? OR a.url LIKE ?)")
        params.extend([like, like])
    if q:
        like = f"%{q.strip()}%"
        clauses.append(
            """(a.filename LIKE ? OR a.title LIKE ? OR a.description LIKE ?
                OR a.content_text LIKE ? OR c.title LIKE ? OR w.name LIKE ?
                OR EXISTS (SELECT 1 FROM artifact_tags qt WHERE qt.artifact_id=a.id AND qt.tag LIKE ?)
                OR EXISTS (
                    SELECT 1 FROM artifact_workspaces qaw
                    JOIN workspaces qw ON qw.id=qaw.workspace_id
                    WHERE qaw.artifact_id=a.id AND qw.name LIKE ?
                ))"""
        )
        params.extend([like, like, like, like, like, like, like, like])
    params.extend([limit, offset])
    sql = f"""
        SELECT a.*, c.title AS conversation_title, w.name AS workspace_name
        FROM artifacts a
        LEFT JOIN conversations c ON c.id=a.conversation_id
        LEFT JOIN workspaces w ON w.id=a.workspace_id
        WHERE {' AND '.join(clauses)}
        ORDER BY a.created_at DESC, a.id DESC
        LIMIT ? OFFSET ?
    """
    db = await get_db()
    try:
        rows = await db.execute_fetchall(sql, tuple(params))
        return await _hydrate_artifact_rows_conn(db, [_normalize_artifact_row(r) for r in rows])
    finally:
        await db.close()


async def _get_artifact_conn(conn: aiosqlite.Connection, artifact_id: str, user_id: str, *, detail: bool = True) -> dict | None:
    rows = await conn.execute_fetchall(
        """SELECT a.*, c.title AS conversation_title, w.name AS workspace_name
           FROM artifacts a
           LEFT JOIN conversations c ON c.id=a.conversation_id
           LEFT JOIN workspaces w ON w.id=a.workspace_id
           WHERE a.id=? AND a.user_id=?""",
        (artifact_id, user_id),
    )
    if not rows:
        return None
    artifact = (await _hydrate_artifact_rows_conn(conn, [_normalize_artifact_row(rows[0])]))[0]
    if not detail:
        return artifact
    artifact["file_health"] = _artifact_file_health_metadata(artifact)
    artifact["provenance"] = _artifact_source_metadata(artifact)
    artifact["duplicates"] = await _list_artifact_duplicates_conn(conn, artifact, user_id)
    root_id = artifact.get("parent_artifact_id") or artifact.get("id")
    version_rows = await conn.execute_fetchall(
        """SELECT a.*, c.title AS conversation_title, w.name AS workspace_name
           FROM artifacts a
           LEFT JOIN conversations c ON c.id=a.conversation_id
           LEFT JOIN workspaces w ON w.id=a.workspace_id
           WHERE a.user_id=? AND (a.id=? OR a.parent_artifact_id=? OR a.id=? OR a.supersedes_artifact_id=?)
           ORDER BY a.created_at ASC, a.id ASC""",
        (
            user_id,
            root_id,
            root_id,
            artifact.get("parent_artifact_id") or "",
            artifact.get("id"),
        ),
    )
    versions = await _hydrate_artifact_rows_conn(conn, [_normalize_artifact_row(r) for r in version_rows])
    seen_versions = {}
    for version in versions:
        seen_versions[version["id"]] = version
    artifact["versions"] = list(seen_versions.values())
    related_rows = await conn.execute_fetchall(
        """SELECT DISTINCT a.*, c.title AS conversation_title, w.name AS workspace_name
           FROM artifacts a
           LEFT JOIN conversations c ON c.id=a.conversation_id
           LEFT JOIN workspaces w ON w.id=a.workspace_id
           LEFT JOIN artifact_tags at ON at.artifact_id=a.id
           WHERE a.user_id=? AND a.id<>? AND (
                (? IS NOT NULL AND a.conversation_id=?)
                OR (? IS NOT NULL AND a.workflow_id=?)
                OR (? IS NOT NULL AND a.run_id=?)
                OR at.tag IN (SELECT tag FROM artifact_tags WHERE artifact_id=?)
                OR EXISTS (
                    SELECT 1 FROM artifact_workspaces aw
                    WHERE aw.artifact_id=a.id
                      AND aw.workspace_id IN (SELECT workspace_id FROM artifact_workspaces WHERE artifact_id=?)
                )
           )
           ORDER BY a.created_at DESC LIMIT 12""",
        (
            user_id,
            artifact_id,
            artifact.get("conversation_id"), artifact.get("conversation_id"),
            artifact.get("workflow_id"), artifact.get("workflow_id"),
            artifact.get("run_id"), artifact.get("run_id"),
            artifact_id,
            artifact_id,
        ),
    )
    artifact["related"] = await _hydrate_artifact_rows_conn(conn, [_normalize_artifact_row(r) for r in related_rows])
    return artifact


async def get_artifact(artifact_id: str) -> dict | None:
    user_id = _scope_user()
    db = await get_db()
    try:
        return await _get_artifact_conn(db, artifact_id, user_id)
    finally:
        await db.close()


async def get_artifact_duplicates(artifact_id: str) -> list[dict] | None:
    user_id = _scope_user()
    db = await get_db()
    try:
        artifact = await _get_artifact_conn(db, artifact_id, user_id, detail=False)
        if not artifact:
            return None
        return await _list_artifact_duplicates_conn(db, artifact, user_id)
    finally:
        await db.close()


async def merge_duplicate_artifact(artifact_id: str, duplicate_id: str) -> dict | None:
    user_id = _scope_user()
    duplicate_id = (duplicate_id or "").strip()
    if not duplicate_id or duplicate_id == artifact_id:
        raise ValueError("Select a different duplicate artifact")
    db = await get_db()
    try:
        canonical = await _get_artifact_conn(db, artifact_id, user_id, detail=False)
        duplicate = await _get_artifact_conn(db, duplicate_id, user_id, detail=False)
        if not canonical or not duplicate:
            return None
        sha = (canonical.get("sha256") or "").strip()
        if not sha or sha != (duplicate.get("sha256") or "").strip():
            raise ValueError("Artifacts do not share the same non-empty sha256")

        canonical_tags = list(canonical.get("tags") or [])
        copied_tags = [tag for tag in (duplicate.get("tags") or []) if tag not in canonical_tags]
        if copied_tags:
            await _set_artifact_tags_conn(db, artifact_id, canonical_tags + copied_tags)

        canonical_ws = list(canonical.get("workspace_ids") or [])
        copied_ws = [ws_id for ws_id in (duplicate.get("workspace_ids") or []) if ws_id not in canonical_ws]
        if copied_ws:
            await _set_artifact_workspaces_conn(db, artifact_id, canonical_ws + copied_ws, user_id)

        now = datetime.utcnow().isoformat()
        await db.execute(
            "UPDATE artifacts SET status='archived', updated_at=? WHERE id=? AND user_id=?",
            (now, duplicate_id, user_id),
        )
        await db.execute(
            "UPDATE artifacts SET updated_at=? WHERE id=? AND user_id=?",
            (now, artifact_id, user_id),
        )
        event_meta = {
            "duplicate_id": duplicate_id,
            "canonical_artifact_id": artifact_id,
            "copied_tags": copied_tags,
            "copied_workspace_ids": copied_ws,
        }
        await _add_artifact_event_conn(
            db,
            artifact_id,
            user_id,
            "duplicate_merged",
            f"Merged metadata from duplicate {duplicate_id}",
            event_meta,
            created_at=now,
        )
        await _add_artifact_event_conn(
            db,
            duplicate_id,
            user_id,
            "duplicate_archived",
            f"Archived after metadata merge into {artifact_id}",
            event_meta,
            created_at=now,
        )
        await db.commit()
        return {
            "canonical": await _get_artifact_conn(db, artifact_id, user_id),
            "duplicate": await _get_artifact_conn(db, duplicate_id, user_id),
            "copied_tags": copied_tags,
            "copied_workspace_ids": copied_ws,
        }
    finally:
        await db.close()


def _synthetic_artifact_event(
    artifact_id: str,
    event_type: str,
    summary: str,
    created_at: str | None,
    metadata: dict | None = None,
) -> dict:
    safe_type = re.sub(r"[^a-z0-9_.:-]+", "_", str(event_type or "").strip().lower())[:80] or "event"
    stamp = created_at or datetime.utcnow().isoformat()
    return {
        "id": f"synthetic-{artifact_id}-{safe_type}-{uuid.uuid5(uuid.NAMESPACE_URL, artifact_id + safe_type + stamp + summary).hex[:8]}",
        "artifact_id": artifact_id,
        "event_type": safe_type,
        "summary": summary,
        "metadata": metadata or {},
        "created_at": stamp,
        "source": "synthetic",
    }


def _timeline_sort_value(event: dict) -> tuple[str, str]:
    return (str(event.get("created_at") or ""), str(event.get("id") or ""))


async def get_artifact_timeline(artifact_id: str) -> list[dict] | None:
    user_id = _scope_user()
    db = await get_db()
    try:
        artifact = await _get_artifact_conn(db, artifact_id, user_id, detail=False)
        if not artifact:
            return None

        events: list[dict] = []
        events.append(_synthetic_artifact_event(
            artifact_id,
            "created",
            f"Created {artifact.get('title') or artifact.get('filename') or 'artifact'}",
            artifact.get("created_at"),
            {
                "artifact": _artifact_summary(artifact),
                "related_artifact_id": artifact_id,
            },
        ))

        provenance = _artifact_source_metadata(artifact)
        if any(provenance.get(k) for k in ("conversation_id", "run_id", "workflow_id", "source_tool", "source_path")):
            bits = []
            if provenance.get("source_tool"):
                bits.append(f"source {provenance['source_tool']}")
            if provenance.get("conversation_title"):
                bits.append(f"chat {provenance['conversation_title']}")
            if provenance.get("run_id"):
                bits.append(f"run {provenance['run_id']}")
            if provenance.get("workflow_id"):
                bits.append(f"workflow {provenance['workflow_id']}")
            events.append(_synthetic_artifact_event(
                artifact_id,
                "sourced",
                "Created from " + ", ".join(bits) if bits else "Created from source metadata",
                artifact.get("created_at"),
                provenance,
            ))

        if artifact.get("supersedes_artifact_id"):
            events.append(_synthetic_artifact_event(
                artifact_id,
                "revised",
                f"Created as a revision of {artifact['supersedes_artifact_id']}",
                artifact.get("created_at"),
                {"related_artifact_id": artifact["supersedes_artifact_id"], "relation": "supersedes"},
            ))
        parent_id = artifact.get("parent_artifact_id")
        if parent_id and parent_id != artifact_id and parent_id != artifact.get("supersedes_artifact_id"):
            events.append(_synthetic_artifact_event(
                artifact_id,
                "lineage",
                f"Joined lineage rooted at {parent_id}",
                artifact.get("created_at"),
                {"related_artifact_id": parent_id, "relation": "parent"},
            ))

        child_rows = await db.execute_fetchall(
            """SELECT a.*, c.title AS conversation_title, w.name AS workspace_name
               FROM artifacts a
               LEFT JOIN conversations c ON c.id=a.conversation_id
               LEFT JOIN workspaces w ON w.id=a.workspace_id
               WHERE a.user_id=? AND (a.supersedes_artifact_id=? OR a.parent_artifact_id=?)
               ORDER BY a.created_at ASC, a.id ASC""",
            (user_id, artifact_id, artifact_id),
        )
        children = await _hydrate_artifact_rows_conn(db, [_normalize_artifact_row(r) for r in child_rows])
        for child in children:
            if child.get("id") == artifact_id:
                continue
            relation = "superseded_by" if child.get("supersedes_artifact_id") == artifact_id else "lineage_child"
            events.append(_synthetic_artifact_event(
                artifact_id,
                relation,
                f"{'Superseded by' if relation == 'superseded_by' else 'Related revision'} {child.get('title') or child.get('filename') or child.get('id')}",
                child.get("created_at"),
                {"related_artifact_id": child.get("id"), "artifact": _artifact_summary(child)},
            ))

        metadata = artifact.get("metadata") or {}
        bundled_ids = metadata.get("artifact_ids") if isinstance(metadata.get("artifact_ids"), list) else []
        if bundled_ids:
            events.append(_synthetic_artifact_event(
                artifact_id,
                "bundled_from",
                f"Bundled {len(bundled_ids)} artifact(s)",
                artifact.get("created_at"),
                {"related_artifact_ids": bundled_ids[:100]},
            ))

        bundle_rows = await db.execute_fetchall(
            """SELECT a.*, c.title AS conversation_title, w.name AS workspace_name
               FROM artifacts a
               LEFT JOIN conversations c ON c.id=a.conversation_id
               LEFT JOIN workspaces w ON w.id=a.workspace_id
               WHERE a.user_id=? AND a.id<>? AND a.kind='archive' AND a.metadata_json LIKE ?
               ORDER BY a.created_at ASC, a.id ASC""",
            (user_id, artifact_id, f"%{artifact_id}%"),
        )
        bundles = await _hydrate_artifact_rows_conn(db, [_normalize_artifact_row(r) for r in bundle_rows])
        for bundle in bundles:
            ids = (bundle.get("metadata") or {}).get("artifact_ids")
            if isinstance(ids, list) and artifact_id in ids:
                events.append(_synthetic_artifact_event(
                    artifact_id,
                    "included_in_bundle",
                    f"Included in bundle {bundle.get('title') or bundle.get('filename') or bundle.get('id')}",
                    bundle.get("created_at"),
                    {"related_artifact_id": bundle.get("id"), "artifact": _artifact_summary(bundle)},
                ))

        event_rows = await db.execute_fetchall(
            "SELECT * FROM artifact_events WHERE artifact_id=? AND user_id=? ORDER BY created_at ASC, id ASC",
            (artifact_id, user_id),
        )
        durable = [_normalize_artifact_event_row(r) for r in event_rows]
        events.extend(durable)
        durable_types = {event.get("event_type") for event in durable}

        if artifact.get("last_checked_at") and not durable_types.intersection({"file_health_checked", "file_missing_detected"}):
            exists_status = artifact.get("exists_status") or "unknown"
            events.append(_synthetic_artifact_event(
                artifact_id,
                "file_missing_detected" if exists_status == "missing" else "file_health_checked",
                "File is missing" if exists_status == "missing" else f"File health checked: {exists_status}",
                artifact.get("last_checked_at"),
                _artifact_file_health_metadata(artifact),
            ))

        if artifact.get("updated_at") and artifact.get("updated_at") != artifact.get("created_at") and "metadata_updated" not in durable_types:
            events.append(_synthetic_artifact_event(
                artifact_id,
                "metadata_current",
                f"Current metadata: status {artifact.get('status') or 'draft'}, {len(artifact.get('tags') or [])} tag(s), {len(artifact.get('workspace_ids') or [])} workspace(s)",
                artifact.get("updated_at"),
                {
                    "status": artifact.get("status") or "draft",
                    "pinned": artifact.get("pinned") or 0,
                    "tags": artifact.get("tags") or [],
                    "workspace_ids": artifact.get("workspace_ids") or [],
                },
            ))

        events.sort(key=_timeline_sort_value)
        return events
    finally:
        await db.close()


async def update_artifact(artifact_id: str, **kwargs) -> dict | None:
    user_id = _scope_user()
    allowed = {
        "title", "description", "workspace_id", "workspace_ids", "status", "pinned",
        "tags", "parent_artifact_id", "supersedes_artifact_id",
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM artifacts WHERE id=? AND user_id=?", (artifact_id, user_id))
        if not rows:
            return None
        workspace_ids_sentinel = object()
        workspace_ids = workspace_ids_sentinel
        if "workspace_ids" in fields:
            raw_ws = fields.pop("workspace_ids")
            workspace_ids = raw_ws if isinstance(raw_ws, list) else []
        elif "workspace_id" in fields:
            ws_id = (fields.pop("workspace_id") or "").strip() or None
            workspace_ids = [ws_id] if ws_id else []
        tags_sentinel = object()
        tags = tags_sentinel
        if "tags" in fields:
            tags = fields.pop("tags")
        for link_key in ("parent_artifact_id", "supersedes_artifact_id"):
            if link_key in fields:
                link_id = (fields.get(link_key) or "").strip() or None
                if link_id == artifact_id:
                    fields[link_key] = None
                elif link_id:
                    linked = await db.execute_fetchall("SELECT id FROM artifacts WHERE id=? AND user_id=?", (link_id, user_id))
                    fields[link_key] = link_id if linked else None
                else:
                    fields[link_key] = None
        if "status" in fields:
            status_norm = (fields.get("status") or "draft").strip().lower()
            fields["status"] = status_norm if status_norm in _ARTIFACT_STATUSES else "draft"
        if "pinned" in fields:
            fields["pinned"] = 1 if str(fields.get("pinned")).lower() in {"1", "true", "yes", "on"} else 0
        for key in ("title", "description"):
            if key in fields and fields[key] is not None:
                fields[key] = _scrub_surrogates(str(fields[key]))[:500 if key == "title" else 3000]
        changed_metadata = {
            "fields": sorted(k for k in fields.keys() if k != "updated_at"),
            "workspace_ids_updated": workspace_ids is not workspace_ids_sentinel,
            "tags_updated": tags is not tags_sentinel,
        }
        if fields:
            fields["updated_at"] = datetime.utcnow().isoformat()
            sets = ", ".join(f"{k}=?" for k in fields)
            await db.execute(
                f"UPDATE artifacts SET {sets} WHERE id=? AND user_id=?",
                (*fields.values(), artifact_id, user_id),
            )
        if workspace_ids is not workspace_ids_sentinel:
            await _set_artifact_workspaces_conn(db, artifact_id, workspace_ids, user_id)
        if tags is not tags_sentinel:
            await _set_artifact_tags_conn(db, artifact_id, tags)
        if changed_metadata["fields"] or changed_metadata["workspace_ids_updated"] or changed_metadata["tags_updated"]:
            bits = list(changed_metadata["fields"])
            if changed_metadata["workspace_ids_updated"]:
                bits.append("workspaces")
            if changed_metadata["tags_updated"]:
                bits.append("tags")
            await _add_artifact_event_conn(
                db,
                artifact_id,
                user_id,
                "metadata_updated",
                "Updated " + ", ".join(bits),
                changed_metadata,
            )
        await db.commit()
        return await _get_artifact_conn(db, artifact_id, user_id)
    finally:
        await db.close()


async def update_artifact_file_metadata(
    artifact_id: str,
    *,
    storage_path: str | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
    exists_status: str = "unknown",
    content_text: str | None = None,
) -> dict | None:
    user_id = _scope_user()
    now = datetime.utcnow().isoformat()
    fields = {
        "exists_status": (exists_status or "unknown").strip().lower() or "unknown",
        "last_checked_at": now,
    }
    if storage_path is not None:
        fields["storage_path"] = storage_path
    if size_bytes is not None:
        fields["size_bytes"] = int(size_bytes or 0)
    if sha256 is not None:
        fields["sha256"] = sha256 or ""
    if content_text is not None:
        fields["content_text"] = _scrub_surrogates(str(content_text or ""))[:800000]
        fields["content_indexed_at"] = now if content_text else None
    fields["updated_at"] = now
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM artifacts WHERE id=? AND user_id=?", (artifact_id, user_id))
        if not rows:
            return None
        sets = ", ".join(f"{k}=?" for k in fields)
        await db.execute(
            f"UPDATE artifacts SET {sets} WHERE id=? AND user_id=?",
            (*fields.values(), artifact_id, user_id),
        )
        await _add_artifact_event_conn(
            db,
            artifact_id,
            user_id,
            "file_missing_detected" if fields["exists_status"] == "missing" else "file_health_checked",
            "File is missing" if fields["exists_status"] == "missing" else f"File health checked: {fields['exists_status']}",
            {
                "exists_status": fields["exists_status"],
                "storage_path": fields.get("storage_path"),
                "size_bytes": fields.get("size_bytes"),
                "sha256": fields.get("sha256"),
            },
            created_at=now,
        )
        await db.commit()
        return await _get_artifact_conn(db, artifact_id, user_id)
    finally:
        await db.close()


async def count_artifacts_with_storage_path(storage_path: str, exclude_id: str = "") -> int:
    """How many artifact rows (any user) still reference this file on disk.
    Cross-user on purpose: the file must not be unlinked while anyone's row
    points at it (duplicate-merge can leave several rows sharing one path)."""
    if not storage_path:
        return 0
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE storage_path=? AND id != ?",
            (storage_path, exclude_id or ""),
        )
        row = await cursor.fetchone()
        return row["n"] if row else 0
    finally:
        await db.close()


async def find_artifact_by_storage_path(storage_path: str) -> dict | None:
    """Newest artifact row referencing this file. Cross-user like
    count_artifacts_with_storage_path — the post-restart image-job dedupe must
    fire regardless of which session's poll lands first."""
    if not storage_path:
        return None
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM artifacts WHERE storage_path=? ORDER BY created_at DESC LIMIT 1",
            (storage_path,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def scrub_image_traces(filenames: list[str]) -> int:
    """Rewrite chat messages so purged generated images leave no trace.

    For every user-scoped message that references one of the deleted image
    files: strips the inline image markdown / download-link lines, the
    generation seed footer, and any image-tool entries (file refs, saved
    generate_image events) from persisted metadata. Returns the number of
    messages rewritten.

    NOTE: metadata scrubbing is all-or-nothing by design — in a matched
    message it drops EVERY generate_image-related list entry, not just those
    naming the given files. Correct for the full purge (which deletes all
    generated images); do NOT reuse for single-image deletion without
    narrowing that clause.
    """
    user_id = _scope_user()
    filenames = [f for f in (filenames or []) if f]
    if not filenames:
        return 0
    db = await get_db()
    scrubbed = 0
    try:
        rows_all, seen = [], set()
        for i in range(0, len(filenames), 20):
            batch = filenames[i:i + 20]
            conds = " OR ".join(["m.content LIKE ? OR m.metadata LIKE ?"] * len(batch))
            params: list = [user_id]
            for fn in batch:
                like = f"%{fn}%"
                params.extend([like, like])
            rows = await db.execute_fetchall(
                f"SELECT m.id, m.content, m.metadata FROM messages m "
                f"JOIN conversations c ON c.id = m.conversation_id "
                f"WHERE c.user_id=? AND ({conds})", params)
            for r in rows:
                if r[0] not in seen:
                    seen.add(r[0])
                    rows_all.append(r)

        def _mentions(obj) -> bool:
            s = json.dumps(obj, default=str)
            return any(fn in s for fn in filenames) or '"generate_image"' in s

        for mid, content, meta_raw in rows_all:
            new_content = content or ""
            for fn in filenames:
                if fn not in new_content:
                    continue
                esc = re.escape(fn)
                new_content = re.sub(rf"!\[[^\]\n]*\]\([^)\n]*{esc}[^)\n]*\)", "", new_content)
                # Any remaining line still naming the file (download links, bare URLs)
                new_content = re.sub(rf"^.*{esc}.*$", "", new_content, flags=re.M)
            if new_content != (content or ""):
                new_content = re.sub(r"^Seed: `?\d+`?\s*·[^\n]*$", "", new_content, flags=re.M)
                new_content = re.sub(r"\n{3,}", "\n\n", new_content).strip()
            try:
                meta = json.loads(meta_raw or "{}")
                if not isinstance(meta, dict):
                    meta = {}
            except Exception:
                meta = {}
            changed_meta = False
            for key, val in list(meta.items()):
                if isinstance(val, list):
                    kept = [x for x in val if not (isinstance(x, (dict, list)) and _mentions(x))]
                    if len(kept) != len(val):
                        meta[key] = kept
                        changed_meta = True
            if new_content != (content or "") or changed_meta:
                await db.execute("UPDATE messages SET content=?, metadata=? WHERE id=?",
                                 (new_content, json.dumps(meta), mid))
                scrubbed += 1
        await db.commit()
        return scrubbed
    finally:
        await db.close()


async def vacuum_database():
    """WAL-checkpoint and VACUUM the DB so deleted rows leave no residual
    bytes in the database or WAL file. Used by the image purge."""
    db = await get_db()
    try:
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await db.commit()
        await db.execute("VACUUM")
    finally:
        await db.close()


async def delete_conversation_file(file_id: str) -> bool:
    """Remove a conversation_files row (chat attachment reference). Used by the
    image purge so chat-generated images leave no dangling file reference."""
    user_id = _scope_user()
    db = await get_db()
    try:
        cur = await db.execute(
            "DELETE FROM conversation_files WHERE id=? AND conversation_id IN "
            "(SELECT id FROM conversations WHERE user_id=?)",
            (file_id, user_id))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def delete_artifact(artifact_id: str) -> bool:
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute("DELETE FROM artifact_tags WHERE artifact_id=?", (artifact_id,))
        await db.execute("DELETE FROM artifact_workspaces WHERE artifact_id=?", (artifact_id,))
        await db.execute("DELETE FROM artifact_events WHERE artifact_id=? AND user_id=?", (artifact_id, user_id))
        cur = await db.execute("DELETE FROM artifacts WHERE id=? AND user_id=?", (artifact_id, user_id))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# ============================================================
# WORKSPACE MEMORY
# ============================================================
_MEMORY_TYPES = {"semantic", "episodic", "procedural"}
_MEMORY_STATUSES = {"suggested", "accepted", "rejected", "archived"}
_DEFAULT_MEMORY_BLOCKS = [
    {
        "label": "workspace_instructions",
        "description": "Stable instructions for all chats in this workspace.",
        "value": "",
        "limit_chars": 1800,
        "read_only": 0,
        "enabled": 1,
    },
    {
        "label": "preferences",
        "description": "Recurring user or project preferences that should stay in context.",
        "value": "",
        "limit_chars": 1200,
        "read_only": 0,
        "enabled": 1,
    },
    {
        "label": "procedures",
        "description": "Reusable workflows, commands, deploy steps, or lessons learned.",
        "value": "",
        "limit_chars": 1600,
        "read_only": 0,
        "enabled": 1,
    },
]


def _loads_json(value, fallback):
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _normalize_memory_row(row) -> dict:
    d = dict(row)
    d["entities"] = _loads_json(d.pop("entities_json", "[]"), [])
    d["metadata"] = _loads_json(d.pop("metadata_json", "{}"), {})
    d["pinned"] = int(d.get("pinned") or 0)
    d["importance"] = int(d.get("importance") or 3)
    try:
        d["confidence"] = float(d.get("confidence") or 0)
    except Exception:
        d["confidence"] = 0.0
    return d


def _normalize_memory_type(value: str) -> str:
    value = (value or "semantic").strip().lower()
    return value if value in _MEMORY_TYPES else "semantic"


def _normalize_memory_status(value: str) -> str:
    value = (value or "suggested").strip().lower()
    return value if value in _MEMORY_STATUSES else "suggested"


def _coerce_text_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v or "").strip()]
    if isinstance(value, str):
        return [v.strip() for v in re.split(r"[\n,]+", value) if v.strip()]
    return []


def _coerce_links(value) -> list:
    if not isinstance(value, list):
        return []
    links = []
    for item in value:
        if isinstance(item, dict):
            label = _scrub_surrogates(str(item.get("label") or item.get("title") or "").strip())
            url = _scrub_surrogates(str(item.get("url") or item.get("href") or "").strip())
            note = _scrub_surrogates(str(item.get("note") or "").strip())
            if label or url:
                row = {"label": label, "url": url}
                if note:
                    row["note"] = note
                links.append(row)
        elif str(item or "").strip():
            links.append({"label": "", "url": _scrub_surrogates(str(item).strip())})
    return links


def _normalize_profile_row(row) -> dict:
    d = dict(row)
    d["interests"] = _loads_json(d.pop("interests_json", "[]"), [])
    d["links"] = _loads_json(d.pop("links_json", "[]"), [])
    d["extra"] = _loads_json(d.pop("extra_json", "{}"), {})
    return d


async def _ensure_user_profile(conn: aiosqlite.Connection) -> None:
    user_id = _scope_user()
    now = datetime.utcnow().isoformat()
    await conn.execute(
        """INSERT OR IGNORE INTO user_profile
           (id,display_name,legal_name,birthday,age,bio,interests_json,links_json,extra_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, "", "", "", "", "", "[]", "[]", "{}", now, now),
    )


async def get_user_profile() -> dict:
    db = await get_db()
    try:
        await _ensure_user_profile(db)
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM user_profile WHERE id=?", (_scope_user(),))
        return _normalize_profile_row(rows[0])
    finally:
        await db.close()


async def update_user_profile(**kwargs) -> dict:
    allowed = {
        "display_name", "legal_name", "birthday", "age", "bio",
        "interests", "links", "extra",
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    db = await get_db()
    try:
        await _ensure_user_profile(db)
        if fields:
            if "interests" in fields:
                fields["interests_json"] = json.dumps(_coerce_text_list(fields.pop("interests")))
            if "links" in fields:
                fields["links_json"] = json.dumps(_coerce_links(fields.pop("links")))
            if "extra" in fields:
                extra = fields.pop("extra")
                fields["extra_json"] = json.dumps(extra if isinstance(extra, dict) else {"notes": str(extra or "")})
            for key in ("display_name", "legal_name", "birthday", "age", "bio"):
                if key in fields:
                    fields[key] = _scrub_surrogates(str(fields[key] or "").strip())
            fields["updated_at"] = datetime.utcnow().isoformat()
            sets = ",".join(f"{k}=?" for k in fields)
            await db.execute(f"UPDATE user_profile SET {sets} WHERE id=?", (*fields.values(), _scope_user()))
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM user_profile WHERE id=?", (_scope_user(),))
        return _normalize_profile_row(rows[0])
    finally:
        await db.close()


async def get_workspace_basic(ws_id: str) -> dict | None:
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM workspaces WHERE id=? AND user_id=?", (ws_id, user_id))
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def get_workspace_for_conversation(conv_id: str) -> dict | None:
    """Infer a workspace only when the conversation belongs to exactly one.

    This keeps chat context predictable while still supporting older frontend
    clients that do not send workspace_id yet.
    """
    if not conv_id:
        return None
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT w.* FROM workspaces w
               JOIN workspace_conversations wc ON wc.workspace_id=w.id
               JOIN conversations c ON c.id=wc.conversation_id
               WHERE wc.conversation_id=? AND w.user_id=? AND c.user_id=?
               ORDER BY wc.added_at DESC""",
            (conv_id, user_id, user_id),
        )
        return dict(rows[0]) if len(rows) == 1 else None
    finally:
        await db.close()


async def _ensure_workspace_memory_blocks(conn: aiosqlite.Connection, ws_id: str) -> None:
    now = datetime.utcnow().isoformat()
    for block in _DEFAULT_MEMORY_BLOCKS:
        await conn.execute(
            """INSERT OR IGNORE INTO workspace_memory_blocks
               (id,workspace_id,label,description,value,limit_chars,read_only,enabled,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                f"wmb-{uuid.uuid4().hex[:12]}", ws_id, block["label"],
                block["description"], block["value"], block["limit_chars"],
                block["read_only"], block["enabled"], now, now,
            ),
        )


def _normalize_block_row(row) -> dict:
    d = dict(row)
    d["enabled"] = int(d.get("enabled") or 0)
    d["read_only"] = int(d.get("read_only") or 0)
    d["limit_chars"] = int(d.get("limit_chars") or 1200)
    return d


async def get_workspace_memory_blocks(ws_id: str) -> list[dict]:
    if not await get_workspace_basic(ws_id):
        return []
    db = await get_db()
    try:
        await _ensure_workspace_memory_blocks(db, ws_id)
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT * FROM workspace_memory_blocks WHERE workspace_id=? ORDER BY rowid ASC",
            (ws_id,),
        )
        return [_normalize_block_row(r) for r in rows]
    finally:
        await db.close()


async def update_workspace_memory_blocks(ws_id: str, blocks: list[dict]) -> list[dict]:
    if not await get_workspace_basic(ws_id):
        return []
    db = await get_db()
    try:
        await _ensure_workspace_memory_blocks(db, ws_id)
        now = datetime.utcnow().isoformat()
        for block in blocks or []:
            block_id = block.get("id")
            label = block.get("label")
            if not block_id and not label:
                continue
            sets = []
            vals = []
            for key in ("description", "value"):
                if key in block:
                    sets.append(f"{key}=?")
                    vals.append(_scrub_surrogates(block.get(key) or ""))
            if "limit_chars" in block:
                sets.append("limit_chars=?")
                vals.append(max(200, min(8000, int(block.get("limit_chars") or 1200))))
            if "enabled" in block:
                sets.append("enabled=?")
                vals.append(1 if block.get("enabled") else 0)
            if "read_only" in block:
                sets.append("read_only=?")
                vals.append(1 if block.get("read_only") else 0)
            if not sets:
                continue
            sets.append("updated_at=?")
            vals.append(now)
            if block_id:
                vals.extend([ws_id, block_id])
                await db.execute(
                    f"UPDATE workspace_memory_blocks SET {', '.join(sets)} WHERE workspace_id=? AND id=?",
                    vals,
                )
            else:
                vals.extend([ws_id, label])
                await db.execute(
                    f"UPDATE workspace_memory_blocks SET {', '.join(sets)} WHERE workspace_id=? AND label=?",
                    vals,
                )
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT * FROM workspace_memory_blocks WHERE workspace_id=? ORDER BY rowid ASC",
            (ws_id,),
        )
        return [_normalize_block_row(r) for r in rows]
    finally:
        await db.close()


async def list_workspace_memories(
    ws_id: str,
    *,
    status: str | None = None,
    memory_type: str | None = None,
    include_archived: bool = True,
) -> list[dict]:
    user_id = _scope_user()
    clauses = ["workspace_id=?", "user_id=?"]
    vals = [ws_id, user_id]
    if status and status != "all":
        clauses.append("status=?")
        vals.append(_normalize_memory_status(status))
    elif not include_archived:
        clauses.append("status!='archived'")
    if memory_type and memory_type != "all":
        clauses.append("type=?")
        vals.append(_normalize_memory_type(memory_type))
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            f"""SELECT * FROM memories WHERE {' AND '.join(clauses)}
                ORDER BY
                  CASE status WHEN 'suggested' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END,
                  pinned DESC,
                  importance DESC,
                  COALESCE(updated_at, created_at) DESC""",
            vals,
        )
        return [_normalize_memory_row(r) for r in rows]
    finally:
        await db.close()


async def create_workspace_memory(
    ws_id: str,
    *,
    content: str,
    memory_type: str = "semantic",
    status: str = "suggested",
    category: str = "General",
    importance: int = 3,
    pinned: int = 0,
    source_conv_id: str | None = None,
    source_conversation_id: str | None = None,
    source_message_id: int | None = None,
    confidence: float = 0,
    valid_from: str | None = None,
    valid_until: str | None = None,
    supersedes_id: str | None = None,
    entities: list | None = None,
    metadata: dict | None = None,
) -> dict:
    user_id = _scope_user()
    if not await get_workspace_basic(ws_id):
        raise ValueError("workspace not found")
    mem_id = f"mem-{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow().isoformat()
    clean_content = _scrub_surrogates((content or "").strip())
    if not clean_content:
        raise ValueError("memory content is required")
    mtype = _normalize_memory_type(memory_type)
    mstatus = _normalize_memory_status(status)
    importance = max(1, min(5, int(importance or 3)))
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO memories
               (id,user_id,workspace_id,content,type,status,category,importance,pinned,
                source_conv_id,source_conversation_id,source_message_id,confidence,
                valid_from,valid_until,supersedes_id,entities_json,metadata_json,updated_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                mem_id, user_id, ws_id, clean_content, mtype, mstatus, category or "General",
                importance, 1 if pinned else 0, source_conv_id or source_conversation_id,
                source_conversation_id or source_conv_id, source_message_id,
                float(confidence or 0), valid_from, valid_until, supersedes_id,
                json.dumps(entities or []), json.dumps(metadata or {}), now, now,
            ),
        )
        await db.execute("UPDATE workspaces SET updated_at=? WHERE id=? AND user_id=?", (now, ws_id, user_id))
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM memories WHERE id=? AND user_id=?", (mem_id, user_id))
        return _normalize_memory_row(rows[0])
    finally:
        await db.close()


async def update_workspace_memory(memory_id: str, ws_id: str, **kwargs) -> dict | None:
    allowed = {
        "content", "type", "status", "category", "importance", "pinned",
        "confidence", "valid_from", "valid_until", "supersedes_id",
        "entities", "metadata",
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        rows = await list_workspace_memories(ws_id, status="all")
        return next((m for m in rows if m["id"] == memory_id), None)
    if "type" in fields:
        fields["type"] = _normalize_memory_type(fields["type"])
    if "status" in fields:
        fields["status"] = _normalize_memory_status(fields["status"])
    if "importance" in fields:
        fields["importance"] = max(1, min(5, int(fields["importance"] or 3)))
    if "pinned" in fields:
        fields["pinned"] = 1 if fields["pinned"] else 0
    if "content" in fields:
        fields["content"] = _scrub_surrogates((fields["content"] or "").strip())
    if "entities" in fields:
        fields["entities_json"] = json.dumps(fields.pop("entities") or [])
    if "metadata" in fields:
        fields["metadata_json"] = json.dumps(fields.pop("metadata") or {})
    fields["updated_at"] = datetime.utcnow().isoformat()
    sets = ",".join(f"{k}=?" for k in fields)
    user_id = _scope_user()
    vals = list(fields.values()) + [memory_id, ws_id, user_id]
    db = await get_db()
    try:
        await db.execute(f"UPDATE memories SET {sets} WHERE id=? AND workspace_id=? AND user_id=?", vals)
        await db.execute("UPDATE workspaces SET updated_at=? WHERE id=? AND user_id=?", (fields["updated_at"], ws_id, user_id))
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM memories WHERE id=? AND workspace_id=? AND user_id=?", (memory_id, ws_id, user_id))
        return _normalize_memory_row(rows[0]) if rows else None
    finally:
        await db.close()


async def accept_workspace_memory(memory_id: str, ws_id: str, supersedes_id: str | None = None) -> dict | None:
    now = datetime.utcnow().isoformat()
    user_id = _scope_user()
    db = await get_db()
    try:
        if supersedes_id:
            await db.execute(
                "UPDATE memories SET valid_until=?, updated_at=? WHERE id=? AND workspace_id=? AND user_id=?",
                (now, now, supersedes_id, ws_id, user_id),
            )
        await db.execute(
            """UPDATE memories
               SET status='accepted',
                   supersedes_id=COALESCE(?, supersedes_id),
                   valid_from=COALESCE(valid_from, ?),
                   updated_at=?
               WHERE id=? AND workspace_id=? AND user_id=?""",
            (supersedes_id, now, now, memory_id, ws_id, user_id),
        )
        await db.execute("UPDATE workspaces SET updated_at=? WHERE id=? AND user_id=?", (now, ws_id, user_id))
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM memories WHERE id=? AND workspace_id=? AND user_id=?", (memory_id, ws_id, user_id))
        return _normalize_memory_row(rows[0]) if rows else None
    finally:
        await db.close()


async def reject_workspace_memory(memory_id: str, ws_id: str) -> dict | None:
    return await update_workspace_memory(memory_id, ws_id, status="rejected")


async def delete_workspace_memory(memory_id: str, ws_id: str) -> bool:
    user_id = _scope_user()
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM memories WHERE id=? AND workspace_id=? AND user_id=?", (memory_id, ws_id, user_id))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


def _content_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9_]{3,}", (text or "").lower())
    stop = {
        "the", "and", "for", "with", "that", "this", "from", "have", "what",
        "when", "where", "would", "could", "should", "about", "into", "your",
    }
    return {w for w in words if w not in stop}


def _memory_current_clause(now: str) -> str:
    return "(valid_until IS NULL OR valid_until='' OR valid_until>?)"


async def list_global_memories(
    *,
    status: str | None = None,
    memory_type: str | None = None,
    include_archived: bool = True,
) -> list[dict]:
    user_id = _scope_user()
    clauses = ["workspace_id IS NULL", "user_id=?"]
    vals = [user_id]
    if status and status != "all":
        clauses.append("status=?")
        vals.append(_normalize_memory_status(status))
    elif not include_archived:
        clauses.append("status!='archived'")
    if memory_type and memory_type != "all":
        clauses.append("type=?")
        vals.append(_normalize_memory_type(memory_type))
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            f"""SELECT * FROM memories WHERE {' AND '.join(clauses)}
                ORDER BY
                  CASE status WHEN 'suggested' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END,
                  pinned DESC,
                  importance DESC,
                  COALESCE(updated_at, created_at) DESC""",
            vals,
        )
        return [_normalize_memory_row(r) for r in rows]
    finally:
        await db.close()


async def create_global_memory(
    *,
    content: str,
    memory_type: str = "semantic",
    status: str = "suggested",
    category: str = "General",
    importance: int = 3,
    pinned: int = 0,
    source_conv_id: str | None = None,
    source_conversation_id: str | None = None,
    source_message_id: int | None = None,
    confidence: float = 0,
    valid_from: str | None = None,
    valid_until: str | None = None,
    supersedes_id: str | None = None,
    entities: list | None = None,
    metadata: dict | None = None,
) -> dict:
    user_id = _scope_user()
    mem_id = f"mem-{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow().isoformat()
    clean_content = _scrub_surrogates((content or "").strip())
    if not clean_content:
        raise ValueError("memory content is required")
    mtype = _normalize_memory_type(memory_type)
    mstatus = _normalize_memory_status(status)
    importance = max(1, min(5, int(importance or 3)))
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO memories
               (id,user_id,workspace_id,content,type,status,category,importance,pinned,
                source_conv_id,source_conversation_id,source_message_id,confidence,
                valid_from,valid_until,supersedes_id,entities_json,metadata_json,updated_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                mem_id, user_id, None, clean_content, mtype, mstatus, category or "General",
                importance, 1 if pinned else 0, source_conv_id or source_conversation_id,
                source_conversation_id or source_conv_id, source_message_id,
                float(confidence or 0), valid_from, valid_until, supersedes_id,
                json.dumps(entities or []), json.dumps(metadata or {}), now, now,
            ),
        )
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM memories WHERE id=? AND user_id=?", (mem_id, user_id))
        return _normalize_memory_row(rows[0])
    finally:
        await db.close()


async def update_global_memory(memory_id: str, **kwargs) -> dict | None:
    allowed = {
        "content", "type", "status", "category", "importance", "pinned",
        "confidence", "valid_from", "valid_until", "supersedes_id",
        "entities", "metadata",
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        rows = await list_global_memories(status="all")
        return next((m for m in rows if m["id"] == memory_id), None)
    if "type" in fields:
        fields["type"] = _normalize_memory_type(fields["type"])
    if "status" in fields:
        fields["status"] = _normalize_memory_status(fields["status"])
    if "importance" in fields:
        fields["importance"] = max(1, min(5, int(fields["importance"] or 3)))
    if "pinned" in fields:
        fields["pinned"] = 1 if fields["pinned"] else 0
    if "content" in fields:
        fields["content"] = _scrub_surrogates((fields["content"] or "").strip())
    if "entities" in fields:
        fields["entities_json"] = json.dumps(fields.pop("entities") or [])
    if "metadata" in fields:
        fields["metadata_json"] = json.dumps(fields.pop("metadata") or {})
    fields["updated_at"] = datetime.utcnow().isoformat()
    sets = ",".join(f"{k}=?" for k in fields)
    user_id = _scope_user()
    vals = list(fields.values()) + [memory_id, user_id]
    db = await get_db()
    try:
        await db.execute(f"UPDATE memories SET {sets} WHERE id=? AND workspace_id IS NULL AND user_id=?", vals)
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM memories WHERE id=? AND workspace_id IS NULL AND user_id=?", (memory_id, user_id))
        return _normalize_memory_row(rows[0]) if rows else None
    finally:
        await db.close()


async def accept_global_memory(memory_id: str, supersedes_id: str | None = None) -> dict | None:
    now = datetime.utcnow().isoformat()
    user_id = _scope_user()
    db = await get_db()
    try:
        if supersedes_id:
            await db.execute(
                "UPDATE memories SET valid_until=?, updated_at=? WHERE id=? AND workspace_id IS NULL AND user_id=?",
                (now, now, supersedes_id, user_id),
            )
        await db.execute(
            """UPDATE memories
               SET status='accepted',
                   supersedes_id=COALESCE(?, supersedes_id),
                   valid_from=COALESCE(valid_from, ?),
                   updated_at=?
               WHERE id=? AND workspace_id IS NULL AND user_id=?""",
            (supersedes_id, now, now, memory_id, user_id),
        )
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM memories WHERE id=? AND workspace_id IS NULL AND user_id=?", (memory_id, user_id))
        return _normalize_memory_row(rows[0]) if rows else None
    finally:
        await db.close()


async def reject_global_memory(memory_id: str) -> dict | None:
    return await update_global_memory(memory_id, status="rejected")


async def delete_global_memory(memory_id: str) -> bool:
    user_id = _scope_user()
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM memories WHERE id=? AND workspace_id IS NULL AND user_id=?", (memory_id, user_id))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def clear_user_memories(user_id: str | None = None) -> int:
    uid = _scope_user(user_id)
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM memories WHERE user_id=?", (uid,))
        await db.commit()
        return cur.rowcount if cur.rowcount is not None else 0
    finally:
        await db.close()


def _format_profile_context(profile: dict, remaining: int) -> tuple[list[str], int, bool]:
    lines = []
    has_content = False
    field_rows = [
        ("Preferred name", profile.get("display_name")),
        ("Legal name", profile.get("legal_name")),
        ("Birthday", profile.get("birthday")),
        ("Age", profile.get("age")),
        ("Bio", profile.get("bio")),
    ]
    profile_bits = []
    for label, value in field_rows:
        text = str(value or "").strip()
        if text:
            profile_bits.append(f"{label}: {text}")
    interests = [str(v).strip() for v in (profile.get("interests") or []) if str(v or "").strip()]
    if interests:
        profile_bits.append("Interests: " + ", ".join(interests[:30]))
    links = []
    for link in profile.get("links") or []:
        if not isinstance(link, dict):
            continue
        label = str(link.get("label") or "").strip()
        url = str(link.get("url") or "").strip()
        note = str(link.get("note") or "").strip()
        if label and url:
            item = f"{label}: {url}"
        else:
            item = label or url
        if item and note:
            item += f" ({note})"
        if item:
            links.append(item)
    if links:
        profile_bits.append("Links: " + "; ".join(links[:12]))
    extra = profile.get("extra") if isinstance(profile.get("extra"), dict) else {}
    notes = str(extra.get("notes") or "").strip()
    if notes:
        profile_bits.append("Notes: " + notes)
    if profile_bits and remaining > 300:
        text = "\n".join(profile_bits)
        chunk = text[:min(len(text), remaining, 2200)]
        lines.append("[User Profile]\n" + chunk)
        remaining -= len(chunk)
        has_content = True
    return lines, remaining, has_content


async def build_global_memory_context(*, query: str = "", max_chars: int = 4200) -> dict:
    profile = await get_user_profile()
    user_id = _scope_user()
    remaining = max(1000, int(max_chars or 4200))
    lines, remaining, has_memory_content = _format_profile_context(profile, remaining)

    now = datetime.utcnow().isoformat()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            f"""SELECT * FROM memories
                WHERE workspace_id IS NULL
                  AND user_id=?
                  AND status='accepted'
                  AND {_memory_current_clause(now)}
                ORDER BY pinned DESC, importance DESC, COALESCE(updated_at, created_at) DESC
                LIMIT 80""",
            (user_id, now),
        )
    finally:
        await db.close()

    q_tokens = _content_tokens(query)
    scored = []
    for row in rows:
        mem = _normalize_memory_row(row)
        m_tokens = _content_tokens(mem.get("content") or "")
        overlap = len(q_tokens & m_tokens)
        score = (20 if mem.get("pinned") else 0) + int(mem.get("importance") or 3) * 4 + overlap * 6
        scored.append((score, mem))
    scored.sort(key=lambda item: item[0], reverse=True)

    memory_ids = []
    grouped = {"semantic": [], "episodic": [], "procedural": []}
    for _, mem in scored:
        if remaining <= 500:
            break
        content = (mem.get("content") or "").strip()
        if not content:
            continue
        prefix = "[pinned] " if mem.get("pinned") else ""
        item = f"- {prefix}{content}"
        if len(item) > remaining:
            item = item[:remaining - 20] + "..."
        grouped.setdefault(mem.get("type") or "semantic", []).append(item)
        memory_ids.append(mem["id"])
        remaining -= len(item)
        if len(memory_ids) >= 12:
            break

    for typ, title in [("semantic", "Facts and Preferences"), ("episodic", "Past Events"), ("procedural", "Procedures")]:
        if grouped.get(typ):
            lines.append(f"\n[{title}]\n" + "\n".join(grouped[typ]))
            has_memory_content = True

    context = "\n".join(lines).strip() if has_memory_content else ""
    if len(context) > max_chars:
        context = context[:max_chars - 38] + "\n[...HyprChat memory truncated...]"
    return {"profile": profile, "context": context, "memory_ids": memory_ids}


async def build_workspace_memory_context(
    *,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    query: str = "",
    max_chars: int = 5200,
) -> dict:
    ws = await get_workspace_basic(workspace_id) if workspace_id else None
    if not ws and conversation_id:
        ws = await get_workspace_for_conversation(conversation_id)
    if not ws or not int(ws.get("memory_enabled") or 0):
        return {"workspace": ws, "context": "", "memory_ids": [], "block_ids": []}

    user_id = _scope_user()
    ws_id = ws["id"]
    remaining = max(1000, int(max_chars or 5200))
    lines = [f"Workspace: {ws.get('name') or ws_id}"]
    has_memory_content = False
    block_ids = []

    instructions = (ws.get("instructions") or "").strip()
    if instructions:
        chunk = instructions[:min(len(instructions), remaining, 1800)]
        lines.append("\n[Instructions]\n" + chunk)
        has_memory_content = True
        remaining -= len(chunk)

    for block in await get_workspace_memory_blocks(ws_id):
        if remaining <= 400:
            break
        value = (block.get("value") or "").strip()
        if not value or not int(block.get("enabled") or 0):
            continue
        limit = max(200, min(int(block.get("limit_chars") or 1200), remaining))
        label = block.get("label") or "memory"
        lines.append(f"\n[{label}]\n{value[:limit]}")
        has_memory_content = True
        block_ids.append(block["id"])
        remaining -= min(len(value), limit)

    now = datetime.utcnow().isoformat()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            f"""SELECT * FROM memories
                WHERE workspace_id=?
                  AND user_id=?
                  AND status='accepted'
                  AND {_memory_current_clause(now)}
                ORDER BY pinned DESC, importance DESC, COALESCE(updated_at, created_at) DESC
                LIMIT 80""",
            (ws_id, user_id, now),
        )
    finally:
        await db.close()

    q_tokens = _content_tokens(query)
    scored = []
    for row in rows:
        mem = _normalize_memory_row(row)
        m_tokens = _content_tokens(mem.get("content") or "")
        overlap = len(q_tokens & m_tokens)
        score = (20 if mem.get("pinned") else 0) + int(mem.get("importance") or 3) * 4 + overlap * 6
        scored.append((score, mem))
    scored.sort(key=lambda item: item[0], reverse=True)

    memory_ids = []
    grouped = {"semantic": [], "episodic": [], "procedural": []}
    for _, mem in scored:
        if remaining <= 500:
            break
        content = (mem.get("content") or "").strip()
        if not content:
            continue
        prefix = "[pinned] " if mem.get("pinned") else ""
        item = f"- {prefix}{content}"
        if len(item) > remaining:
            item = item[:remaining - 20] + "..."
        grouped.setdefault(mem.get("type") or "semantic", []).append(item)
        memory_ids.append(mem["id"])
        remaining -= len(item)
        if len(memory_ids) >= 10:
            break

    for typ, title in [("semantic", "Facts and Preferences"), ("episodic", "Past Decisions and Events"), ("procedural", "Procedures")]:
        if grouped.get(typ):
            lines.append(f"\n[{title}]\n" + "\n".join(grouped[typ]))
            has_memory_content = True

    context = "\n".join(lines).strip() if has_memory_content else ""
    if len(context) > max_chars:
        context = context[:max_chars - 40] + "\n[...workspace memory truncated...]"
    return {"workspace": ws, "context": context, "memory_ids": memory_ids, "block_ids": block_ids}


# ============================================================
# COUNCIL CRUD
# ============================================================
async def create_council(id: str, name: str, host_model: str, host_system_prompt: str = "", kb_ids: list = None):
    user_id = _scope_user()
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO council_configs(id,user_id,name,host_model,host_system_prompt,kb_ids,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (id, user_id, name, host_model, host_system_prompt, json.dumps(kb_ids or []), now, now)
        )
        await db.commit()
    finally:
        await db.close()


async def get_councils():
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM council_configs WHERE user_id=? ORDER BY updated_at DESC", (user_id,))
        result = []
        for r in rows:
            c = dict(r)
            try:
                c["kb_ids"] = json.loads(c.get("kb_ids") or "[]")
            except Exception:
                c["kb_ids"] = []
            members = await db.execute_fetchall(
                "SELECT * FROM council_members WHERE council_id=? ORDER BY rowid ASC", (c["id"],)
            )
            c["members"] = [dict(m) for m in members]
            result.append(c)
        return result
    finally:
        await db.close()


async def get_council(council_id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM council_configs WHERE id=? AND user_id=?", (council_id, user_id))
        if not rows:
            return None
        c = dict(rows[0])
        try:
            c["kb_ids"] = json.loads(c.get("kb_ids") or "[]")
        except Exception:
            c["kb_ids"] = []
        members = await db.execute_fetchall(
            "SELECT * FROM council_members WHERE council_id=? ORDER BY rowid ASC", (council_id,)
        )
        c["members"] = [dict(m) for m in members]
        return c
    finally:
        await db.close()


async def update_council(council_id: str, **kwargs):
    allowed = {"name", "host_model", "host_system_prompt", "debate_rounds", "kb_ids"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if "kb_ids" in fields:
        fields["kb_ids"] = json.dumps(fields["kb_ids"] if isinstance(fields["kb_ids"], list) else [])
    if not fields:
        return
    fields["updated_at"] = datetime.utcnow().isoformat()
    set_clause = ",".join(f"{k}=?" for k in fields)
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(f"UPDATE council_configs SET {set_clause} WHERE id=? AND user_id=?", (*fields.values(), council_id, user_id))
        await db.commit()
    finally:
        await db.close()


async def delete_council(council_id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute("DELETE FROM council_configs WHERE id=? AND user_id=?", (council_id, user_id))
        await db.commit()
    finally:
        await db.close()


async def add_council_member(id: str, council_id: str, model: str, system_prompt: str = "", persona_name: str = "", model_config_id: str = None):
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM council_configs WHERE id=? AND user_id=?", (council_id, user_id))
        if not rows:
            return
        await db.execute(
            "INSERT INTO council_members(id,council_id,model,model_config_id,system_prompt,persona_name,points) VALUES(?,?,?,?,?,?,0)",
            (id, council_id, model, model_config_id, system_prompt, persona_name)
        )
        await db.execute("UPDATE council_configs SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (council_id,))
        await db.commit()
    finally:
        await db.close()


async def update_council_member(member_id: str, **kwargs):
    allowed = {"model", "model_config_id", "system_prompt", "persona_name", "points"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ",".join(f"{k}=?" for k in fields)
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(
            f"""UPDATE council_members SET {set_clause}
                WHERE id=? AND council_id IN (SELECT id FROM council_configs WHERE user_id=?)""",
            (*fields.values(), member_id, user_id),
        )
        await db.commit()
    finally:
        await db.close()


async def delete_council_member(member_id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM council_members WHERE id=? AND council_id IN (SELECT id FROM council_configs WHERE user_id=?)",
            (member_id, user_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_kb_files_for_kbs(kb_ids: list) -> list:
    """Load all file records (with KB name) for a list of KB IDs."""
    if not kb_ids:
        return []
    user_id = _scope_user()
    db = await get_db()
    try:
        placeholders = ",".join("?" * len(kb_ids))
        cursor = await db.execute(
            f"SELECT kb_files.*, knowledge_bases.name AS kb_name FROM kb_files "
            f"JOIN knowledge_bases ON kb_files.kb_id = knowledge_bases.id "
            f"WHERE knowledge_bases.user_id=? AND kb_files.kb_id IN ({placeholders})",
            [user_id, *kb_ids]
        )
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


# ============================================================
# FULL-TEXT SEARCH
# ============================================================
async def search_messages(query: str, limit: int = 20):
    """Full-text search across all messages, returning conversation context."""
    user_id = _scope_user()
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT m.id, m.conversation_id, m.role, m.content,
                   snippet(messages_fts, 0, '<mark>', '</mark>', '...', 32) AS snippet,
                   c.title AS conv_title
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN conversations c ON c.id = m.conversation_id
            WHERE messages_fts MATCH ? AND c.user_id = ?
            ORDER BY rank
            LIMIT ?
        """, (query, user_id, limit))
        return [dict(r) for r in await cursor.fetchall()]
    except Exception as e:
        print(f"[SEARCH] Error: {e}")
        return []
    finally:
        await db.close()


# ============================================================
# KB CHUNK KEYWORD INDEX (hybrid RAG — kb_chunks_fts)
# ============================================================
# No user scoping here: callers pass kb_ids already scoped by the persona /
# route layer, same trust level as rag.query() over ChromaDB collections.

def _fts_match_expr(query: str, max_terms: int = 12) -> str:
    """Build a safe FTS5 MATCH expression from free-form text.

    RAG queries are full sentences; raw MATCH treats them as implicit AND and
    throws syntax errors on '?', quotes, hyphens, etc. Emit quoted OR-terms so
    any token match ranks (bm25 still orders by how many/which terms hit).
    """
    tokens = re.findall(r"[A-Za-z0-9_]+", query)
    # Drop single-char noise tokens but keep short alphanumerics (part numbers).
    tokens = [t for t in tokens if len(t) > 1][:max_terms]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


async def kb_fts_replace_file(kb_id: str, filename: str, chunks: list[dict]):
    """Replace all FTS rows for one file. chunks: [{chunk_id, text, chunk_index}]."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM kb_chunks_fts WHERE kb_id=? AND filename=?", (kb_id, filename))
        await db.executemany(
            "INSERT INTO kb_chunks_fts (text, filename, kb_id, chunk_id, chunk_index) VALUES (?, ?, ?, ?, ?)",
            [(c["text"], filename, kb_id, c["chunk_id"], c["chunk_index"]) for c in chunks]
        )
        await db.commit()
    finally:
        await db.close()


async def kb_fts_remove_file(kb_id: str, filename: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM kb_chunks_fts WHERE kb_id=? AND filename=?", (kb_id, filename))
        await db.commit()
    finally:
        await db.close()


async def kb_fts_delete_kb(kb_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM kb_chunks_fts WHERE kb_id=?", (kb_id,))
        await db.commit()
    finally:
        await db.close()


async def list_all_kb_ids() -> list[str]:
    """All KB ids across users — startup FTS backfill only (cross-user raw SQL,
    same posture as reap_stale_runs)."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM knowledge_bases")
        return [r["id"] for r in await cursor.fetchall()]
    finally:
        await db.close()


async def kb_fts_count(kb_id: str) -> int:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) AS n FROM kb_chunks_fts WHERE kb_id=?", (kb_id,))
        row = await cursor.fetchone()
        return row["n"] if row else 0
    finally:
        await db.close()


async def kb_fts_search(kb_ids: list[str], query: str, limit: int = 18) -> list[dict]:
    """BM25 keyword search over KB chunks. Returns best-first."""
    if not kb_ids:
        return []
    match = _fts_match_expr(query)
    if not match:
        return []
    db = await get_db()
    try:
        placeholders = ",".join("?" * len(kb_ids))
        cursor = await db.execute(
            f"""SELECT text, filename, kb_id, chunk_id, chunk_index, rank
                FROM kb_chunks_fts
                WHERE kb_chunks_fts MATCH ? AND kb_id IN ({placeholders})
                ORDER BY rank LIMIT ?""",
            (match, *kb_ids, limit)
        )
        out = []
        for r in await cursor.fetchall():
            d = dict(r)
            out.append({
                "text": d["text"],
                "filename": d["filename"],
                "kb_id": d["kb_id"],
                "chunk_id": d["chunk_id"],
                "chunk_index": int(d["chunk_index"] or 0),
                "bm25": -float(d["rank"] or 0),  # fts5 rank is negative-better
            })
        return out
    except Exception as e:
        print(f"[KB FTS] Search error: {e}")
        return []
    finally:
        await db.close()


# ============================================================
# CONVERSATION FORKING
# ============================================================
async def fork_conversation(original_conv_id: str, fork_msg_id: int, new_conv_id: str):
    """Fork a conversation at a specific message, copying messages up to that point."""
    user_id = _scope_user()
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM conversations WHERE id = ? AND user_id = ?", (original_conv_id, user_id))
        original = await cursor.fetchone()
        if not original:
            return None
        original = dict(original)
        now = datetime.utcnow().isoformat()
        await db.execute(
            """INSERT INTO conversations
               (id, user_id, title, model, system_prompt, model_config_id, tool_ids,
                persona_name, persona_avatar, forked_from, fork_point_msg_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_conv_id, user_id, f"Fork of {original.get('title', 'Chat')}", original.get("model", ""),
             original.get("system_prompt", ""), original.get("model_config_id"),
             original.get("tool_ids", "[]"), original.get("persona_name", ""),
             original.get("persona_avatar", ""), original_conv_id, str(fork_msg_id), now, now)
        )
        await db.execute("""
            INSERT INTO messages (conversation_id, role, content, metadata, created_at)
            SELECT ?, role, content, metadata, created_at
            FROM messages WHERE conversation_id = ? AND id <= ?
            ORDER BY id ASC
        """, (new_conv_id, original_conv_id, fork_msg_id))
        await db.commit()
        return await get_conversation(new_conv_id)
    finally:
        await db.close()


async def get_forks(conv_id: str):
    """Get all conversations forked from this one."""
    user_id = _scope_user()
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, title, fork_point_msg_id, created_at FROM conversations WHERE forked_from = ? AND user_id = ?",
            (conv_id, user_id))
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


# ============================================================
# TOKEN USAGE ANALYTICS
# ============================================================
async def record_token_usage(conversation_id: str, model: str, persona_name: str,
                              prompt_tokens: int, completion_tokens: int):
    user_id = _scope_user()
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO token_usage (user_id, conversation_id, model, persona_name,
               prompt_tokens, completion_tokens, total_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, conversation_id, model, persona_name or "", prompt_tokens, completion_tokens,
             prompt_tokens + completion_tokens)
        )
        await db.commit()
    except Exception as e:
        print(f"[TOKEN USAGE] Error recording: {e}")
    finally:
        await db.close()


async def get_token_usage(days: int = 30, group_by: str = "day"):
    """Aggregate token usage. group_by: day, model, persona. days <= 0 means all time."""
    user_id = _scope_user()
    db = await get_db()
    try:
        use_window = (days or 0) > 0
        where = "WHERE user_id=?"
        params = [user_id]
        if use_window:
            where += " AND created_at >= datetime('now', ?)"
            params.append(f"-{days} days")
        if group_by == "model":
            q = """SELECT model, SUM(prompt_tokens) as prompt_tokens,
                   SUM(completion_tokens) as completion_tokens,
                   SUM(total_tokens) as total_tokens, COUNT(*) as request_count
                   FROM token_usage {where}
                   GROUP BY model ORDER BY total_tokens DESC""".format(where=where)
        elif group_by == "persona":
            q = """SELECT persona_name, SUM(prompt_tokens) as prompt_tokens,
                   SUM(completion_tokens) as completion_tokens,
                   SUM(total_tokens) as total_tokens, COUNT(*) as request_count
                   FROM token_usage {where}
                   GROUP BY persona_name ORDER BY total_tokens DESC""".format(where=where)
        elif group_by == "day_model":
            q = """SELECT date(created_at) as date, model,
                   SUM(prompt_tokens) as prompt_tokens,
                   SUM(completion_tokens) as completion_tokens,
                   SUM(total_tokens) as total_tokens, COUNT(*) as request_count
                   FROM token_usage {where}
                   GROUP BY date(created_at), model ORDER BY date ASC""".format(where=where)
        else:
            q = """SELECT date(created_at) as date,
                   SUM(prompt_tokens) as prompt_tokens,
                   SUM(completion_tokens) as completion_tokens,
                   SUM(total_tokens) as total_tokens, COUNT(*) as request_count
                   FROM token_usage {where}
                   GROUP BY date(created_at) ORDER BY date ASC""".format(where=where)
        cursor = await db.execute(q, tuple(params))
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


async def clear_token_usage(user_id: str | None = None) -> int:
    uid = _scope_user(user_id)
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM token_usage WHERE user_id=?", (uid,))
        await db.commit()
        return cur.rowcount if cur.rowcount is not None else 0
    finally:
        await db.close()


# ============================================================
# DEEP RESEARCH REPORTS
# ============================================================
def _parse_research_report(row) -> dict:
    r = dict(row)
    for key, default in (
        ("kb_ids", []),
        ("inputs_json", []),
        ("outline_json", {}),
        ("findings_json", []),
        ("sources_json", []),
        ("metrics_json", {}),
        ("events_log", []),
    ):
        try:
            r[key] = json.loads(r.get(key) or json.dumps(default))
        except (json.JSONDecodeError, TypeError):
            r[key] = default
    r["inputs"] = r.pop("inputs_json", [])
    r["outline"] = r.pop("outline_json", {})
    r["findings"] = r.pop("findings_json", [])
    r["sources"] = r.pop("sources_json", [])
    r["metrics"] = r.pop("metrics_json", {})
    return r


async def create_research_report(report_id: str, *, query: str, title: str = "",
                                 focus: str = "", report_type: str = "analyst",
                                 depth: int = 3, model: str = "",
                                 planner_model: str = "", auditor_model: str = "",
                                 kb_ids: list = None, inputs: list = None,
                                 status: str = "queued") -> None:
    user_id = _scope_user()
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO research_reports(id,user_id,title,query,focus,report_type,status,depth,"
            "model,planner_model,auditor_model,kb_ids,inputs_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                report_id, user_id, title or query[:80], query, focus or "", report_type or "analyst",
                status, int(depth or 3), model or "", planner_model or "",
                auditor_model or "", json.dumps(kb_ids or []), json.dumps(inputs or []),
                now, now,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def update_research_report(report_id: str, *, unless_status: str | None = None, **kwargs) -> bool:
    """Update a report row. With unless_status set, the UPDATE is skipped when
    the row is already in that status (e.g. don't resurrect a cancelled report).
    Returns True when a row was actually updated."""
    if not kwargs:
        return False
    json_fields = {
        "kb_ids": "kb_ids",
        "inputs": "inputs_json",
        "outline": "outline_json",
        "findings": "findings_json",
        "sources": "sources_json",
        "metrics": "metrics_json",
        "events_log": "events_log",
    }
    allowed = {
        "title", "query", "focus", "report_type", "status", "depth", "model",
        "planner_model", "auditor_model", "summary", "error", "report_markdown",
        "completed_at",
        *json_fields.keys(),
    }
    sets = []
    vals = []
    for key, value in kwargs.items():
        if key not in allowed:
            continue
        col = json_fields.get(key, key)
        sets.append(f"{col}=?")
        vals.append(json.dumps(value) if key in json_fields else value)
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.append(datetime.utcnow().isoformat())
    vals.extend([report_id, _scope_user()])
    where = "id=? AND user_id=?"
    if unless_status is not None:
        where += " AND status != ?"
        vals.append(unless_status)
    db = await get_db()
    try:
        cursor = await db.execute(f"UPDATE research_reports SET {', '.join(sets)} WHERE {where}", tuple(vals))
        await db.commit()
        return (cursor.rowcount or 0) > 0
    finally:
        await db.close()


async def append_research_event(report_id: str, event: dict) -> None:
    """Single INSERT into the append-only research_events table (no JSON-array
    rewrite). Also bumps updated_at so the report stays fresh in list ordering."""
    now = datetime.utcnow().isoformat()
    if "ts" not in event:
        event = {**event, "ts": now}
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO research_events(report_id, seq, ts, payload) VALUES("
            "?, COALESCE((SELECT MAX(seq)+1 FROM research_events WHERE report_id=?), 0), ?, ?)",
            (report_id, report_id, event.get("ts"), json.dumps(event)),
        )
        await db.execute("UPDATE research_reports SET updated_at=? WHERE id=?", (now, report_id))
        await db.commit()
    except Exception as e:
        print(f"[DB] append_research_event failed (non-fatal): {e}")
    finally:
        await db.close()


async def replace_research_sources(report_id: str, sources: list[dict]) -> None:
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM research_reports WHERE id=? AND user_id=?", (report_id, user_id))
        if not rows:
            return
        await db.execute("DELETE FROM research_sources WHERE report_id=?", (report_id,))
        for i, src in enumerate(sources or [], start=1):
            metadata = dict(src.get("metadata") or {})
            for key in ("credibility_score", "credibility_factors", "tier_label", "thumbnail", "query", "type"):
                if key in src and key not in metadata:
                    metadata[key] = src.get(key)
            await db.execute(
                "INSERT INTO research_sources(report_id,source_index,title,url,snippet,tier,source_type,metadata_json) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    report_id, int(src.get("index") or i), src.get("title", ""),
                    src.get("url", ""), src.get("snippet", "") or src.get("content", ""),
                    int(src.get("tier", 2)), src.get("type", "web"),
                    json.dumps(metadata),
                ),
            )
        await db.commit()
    finally:
        await db.close()


async def get_research_report(report_id: str) -> dict | None:
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM research_reports WHERE id=? AND user_id=?", (report_id, user_id))
        if not rows:
            return None
        report = _parse_research_report(rows[0])
        # Hydrate events from the append-only table; fall back to the legacy
        # JSON column for reports written before the migration.
        _ev_rows = await db.execute_fetchall(
            "SELECT payload FROM research_events WHERE report_id=? ORDER BY seq", (report_id,)
        )
        if _ev_rows:
            _evs = []
            for _r in _ev_rows:
                try:
                    _evs.append(json.loads(_r["payload"]))
                except (json.JSONDecodeError, TypeError):
                    pass
            report["events_log"] = _evs
        # Token-stream chunks are ephemeral (full text lives in
        # report_markdown); drop them from hydration — covers legacy rows
        # that persisted them before the ephemeral-event change.
        report["events_log"] = [
            e for e in (report.get("events_log") or [])
            if not (isinstance(e, dict) and e.get("type") == "research_token")
        ]
        src_rows = await db.execute_fetchall(
            "SELECT * FROM research_sources WHERE report_id=? ORDER BY source_index ASC, id ASC",
            (report_id,),
        )
        if src_rows:
            parsed = []
            for row in src_rows:
                src = dict(row)
                try:
                    src["metadata"] = json.loads(src.get("metadata_json") or "{}")
                except (json.JSONDecodeError, TypeError):
                    src["metadata"] = {}
                meta = src.get("metadata") or {}
                for key in ("credibility_score", "credibility_factors", "tier_label", "thumbnail", "query", "type"):
                    if key in meta:
                        src[key] = meta[key]
                if "type" not in src:
                    src["type"] = src.get("source_type", "web")
                src.pop("metadata_json", None)
                parsed.append(src)
            report["sources"] = parsed
        return report
    finally:
        await db.close()


async def list_research_reports(limit: int = 50, offset: int = 0, query: str = "") -> list[dict]:
    user_id = _scope_user()
    db = await get_db()
    try:
        if query:
            like = f"%{query}%"
            rows = await db.execute_fetchall(
                "SELECT id,title,query,focus,report_type,status,depth,model,summary,error,"
                "sources_json,metrics_json,created_at,updated_at,completed_at "
                "FROM research_reports WHERE user_id=? AND (title LIKE ? OR query LIKE ? OR summary LIKE ?) "
                "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (user_id, like, like, like, limit, offset),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT id,title,query,focus,report_type,status,depth,model,summary,error,"
                "sources_json,metrics_json,created_at,updated_at,completed_at "
                "FROM research_reports WHERE user_id=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            )
        reports = []
        for row in rows:
            r = dict(row)
            try:
                sources = json.loads(r.pop("sources_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                sources = []
            try:
                metrics = json.loads(r.pop("metrics_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                metrics = {}
            r["source_count"] = metrics.get("source_count") or len(sources)
            r["pages_read"] = metrics.get("pages_read", 0)
            r["elapsed"] = metrics.get("elapsed", 0)
            r["metrics"] = metrics
            reports.append(r)
        return reports
    finally:
        await db.close()


async def delete_research_report(report_id: str) -> None:
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM research_reports WHERE id=? AND user_id=?", (report_id, user_id))
        if not rows:
            return
        await db.execute("DELETE FROM workspace_research_reports WHERE report_id=?", (report_id,))
        await db.execute("DELETE FROM research_sources WHERE report_id=?", (report_id,))
        await db.execute("DELETE FROM research_reports WHERE id=? AND user_id=?", (report_id, user_id))
        await db.commit()
    finally:
        await db.close()


# ============================================================
# CODING PROJECT CRUD
# ============================================================
async def upsert_coding_project(project_id: str, name: str, conversation_id: str = "",
                                 description: str = "", language: str = "",
                                 file_manifest: list = None, last_plan: str = "",
                                 openhands_project_id: str = ""):
    user_id = _scope_user()
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        manifest_json = json.dumps(file_manifest or [])
        existing = await db.execute_fetchall("SELECT id FROM coding_projects WHERE id = ? AND user_id = ?", (project_id, user_id))
        if existing:
            await db.execute(
                "UPDATE coding_projects SET name=?, description=?, language=?, file_manifest=?, "
                "last_plan=?, conversation_id=?, openhands_project_id=?, updated_at=? WHERE id=? AND user_id=?",
                (name, description, language, manifest_json, last_plan,
                 conversation_id, openhands_project_id, now, project_id, user_id)
            )
        else:
            await db.execute(
                "INSERT INTO coding_projects(id,user_id,name,description,language,file_manifest,last_plan,"
                "conversation_id,openhands_project_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, user_id, name, description, language, manifest_json, last_plan,
                 conversation_id, openhands_project_id, now, now)
            )
        await db.commit()
    finally:
        await db.close()


async def get_coding_project_by_conv(conversation_id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM coding_projects WHERE conversation_id = ? AND user_id=? ORDER BY updated_at DESC LIMIT 1",
            (conversation_id, user_id)
        )
        if not rows:
            return None
        p = dict(rows[0])
        try:
            p["file_manifest"] = json.loads(p.get("file_manifest", "[]"))
        except (json.JSONDecodeError, TypeError):
            p["file_manifest"] = []
        return p
    finally:
        await db.close()


async def get_coding_project(project_id: str):
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM coding_projects WHERE id = ? AND user_id=?", (project_id, user_id))
        if not rows:
            return None
        p = dict(rows[0])
        try:
            p["file_manifest"] = json.loads(p.get("file_manifest", "[]"))
        except (json.JSONDecodeError, TypeError):
            p["file_manifest"] = []
        return p
    finally:
        await db.close()


# ============================================================
# RUNS — Coder Bot v2 durable agent invocations
# ============================================================

def _row_to_run(row) -> dict:
    """Decode a runs row into a dict, parsing JSON columns."""
    r = dict(row)
    try:
        r["result_envelope"] = json.loads(r.get("result_envelope") or "{}")
    except (json.JSONDecodeError, TypeError):
        r["result_envelope"] = {}
    try:
        r["events_log"] = json.loads(r.get("events_log") or "[]")
    except (json.JSONDecodeError, TypeError):
        r["events_log"] = []
    return r


async def create_run(run_id: str, conversation_id: str, role: str,
                     project_id: str = "", parent_run_id: str = "",
                     status: str = "queued") -> None:
    """Create a new run row. Status defaults to 'queued'; caller transitions to 'running' when it starts."""
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id))
        if not rows:
            return
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO runs(id, conversation_id, role, status, project_id, parent_run_id, "
            "started_at, result_envelope, events_log) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, conversation_id, role, status, project_id or "", parent_run_id or "",
             now, "{}", "[]")
        )
        await db.commit()
    finally:
        await db.close()


async def update_run(run_id: str, *, status: str = None, result_envelope: dict = None,
                     ended: bool = False) -> None:
    """Update status and/or result envelope on an existing run.

    `ended=True` stamps `ended_at` to now (use when transitioning to a terminal status).
    """
    sets = []
    vals = []
    if status is not None:
        sets.append("status=?")
        vals.append(status)
    if result_envelope is not None:
        sets.append("result_envelope=?")
        vals.append(json.dumps(result_envelope))
    if ended:
        sets.append("ended_at=?")
        vals.append(datetime.utcnow().isoformat())
    if not sets:
        return
    vals.extend([run_id, _scope_user()])
    db = await get_db()
    try:
        await db.execute(
            f"""UPDATE runs SET {', '.join(sets)}
                WHERE id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)""",
            tuple(vals),
        )
        await db.commit()
    finally:
        await db.close()


async def append_run_event(run_id: str, event: dict) -> None:
    """Append a structured event to a run's event log.

    Single INSERT into the append-only run_events table — no read-modify-write
    of a growing JSON array. seq auto-increments per run_id.
    """
    if "ts" not in event:
        event = {**event, "ts": datetime.utcnow().isoformat()}
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO run_events(run_id, seq, ts, payload) VALUES("
            "?, COALESCE((SELECT MAX(seq)+1 FROM run_events WHERE run_id=?), 0), ?, ?)",
            (run_id, run_id, event.get("ts"), json.dumps(event)),
        )
        await db.commit()
    except Exception as e:
        print(f"[DB] append_run_event failed (non-fatal): {e}")
    finally:
        await db.close()


async def _hydrate_run_events(db, run_id: str, legacy_json: str) -> list:
    """Read a run's events newest-table-first, falling back to the legacy
    events_log JSON column for rows written before the table migration."""
    rows = await db.execute_fetchall(
        "SELECT payload FROM run_events WHERE run_id=? ORDER BY seq", (run_id,)
    )
    if rows:
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["payload"]))
            except (json.JSONDecodeError, TypeError):
                pass
        return out
    try:
        return json.loads(legacy_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


async def get_run(run_id: str) -> dict | None:
    """Return a single run with parsed result_envelope and events_log."""
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM runs WHERE id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)",
            (run_id, user_id),
        )
        if not rows:
            return None
        run = _row_to_run(rows[0])
        # Single-run reads (RunCard) hydrate the full event stream from the
        # append-only table; list getters skip this (the gate never reads
        # events, and the frontend always refetches /api/runs/{id}).
        run["events_log"] = await _hydrate_run_events(db, run_id, rows[0]["events_log"])
        return run
    finally:
        await db.close()


async def get_runs_by_conversation(conversation_id: str, limit: int = 100) -> list[dict]:
    """All runs for a conversation, newest first. Used by the frontend on reconnect
    to rebuild the run cards under each message."""
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM runs WHERE conversation_id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?) ORDER BY started_at DESC LIMIT ?",
            (conversation_id, user_id, limit)
        )
        return [_row_to_run(r) for r in rows]
    finally:
        await db.close()


async def latest_edit_run_after(project_id: str) -> str | None:
    """Most recent started_at among successful/partial file-EDITING runs
    (builder.* / fixer / aider.fix) for a project. Reviewer/acceptance/qa/architect
    don't write files, so they're excluded. Used to flag stale archive artifacts:
    if this timestamp is newer than an archive's created_at, the project changed
    after it was packaged. Returns a 'YYYY-MM-DD HH:MM:SS' string (UTC) or None."""
    if not project_id:
        return None
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT MAX(started_at) AS ts FROM runs "
            "WHERE project_id=? "
            "AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?) "
            "AND (role IN ('fixer','aider.fix') OR role LIKE 'builder.%') "
            "AND status NOT IN ('failed','cancelled','skipped','queued','pending','running')",
            (project_id, user_id),
        )
        return (rows[0]["ts"] if rows and rows[0] else None) or None
    finally:
        await db.close()


async def get_runs_by_project(project_id: str, limit: int = 50) -> list[dict]:
    """All runs that touched a given project, newest first."""
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM runs WHERE project_id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?) ORDER BY started_at DESC LIMIT ?",
            (project_id, user_id, limit)
        )
        return [_row_to_run(r) for r in rows]
    finally:
        await db.close()


_REAPER_TERMINAL_WORKFLOW_STATES = (
    "accepted", "partial_delivered", "cancelled", "blocked", "answering",
)


async def reap_stale_runs() -> dict:
    """Mark runs left non-terminal by a previous process as failed.

    Called once at startup before any requests. Cross-user raw SQL by design:
    a restart orphans every in-flight run regardless of owner, and the v2 gate
    treats a 'running' active_run_id as an in-flight workflow forever.
    """
    db = await get_db()
    reaped: list[str] = []
    workflows_blocked = 0
    reports_reaped = 0
    try:
        now = datetime.utcnow().isoformat()
        # Research reports run via asyncio.create_task and are in-process only —
        # a restart strands them 'queued'/'running' forever. Mark them failed.
        rrows = await db.execute_fetchall(
            "SELECT id FROM research_reports WHERE status IN ('queued','pending','running')"
        )
        for rrow in rrows:
            await db.execute(
                "UPDATE research_reports SET status='failed', "
                "error='Orphaned by backend restart', updated_at=?, completed_at=? WHERE id=?",
                (now, now, rrow["id"]),
            )
            reports_reaped += 1
        rows = await db.execute_fetchall(
            "SELECT id, result_envelope FROM runs WHERE status IN ('queued','pending','running')"
        )
        for row in rows:
            try:
                env = json.loads(row["result_envelope"] or "{}")
            except (json.JSONDecodeError, TypeError):
                env = {}
            if not isinstance(env, dict):
                env = {}
            env.update({"status": "error", "summary": "orphaned by backend restart"})
            await db.execute(
                "UPDATE runs SET status='failed', result_envelope=?, ended_at=? WHERE id=?",
                (json.dumps(env), now, row["id"]),
            )
            reaped.append(row["id"])
        if reaped:
            placeholders = ",".join("?" * len(reaped))
            wrows = await db.execute_fetchall(
                f"SELECT id FROM coder_workflows WHERE active_run_id IN ({placeholders}) "
                f"AND state NOT IN ({','.join('?' * len(_REAPER_TERMINAL_WORKFLOW_STATES))})",
                tuple(reaped) + _REAPER_TERMINAL_WORKFLOW_STATES,
            )
            for w in wrows:
                await db.execute(
                    "UPDATE coder_workflows SET state='blocked', artifact_status='not_ready', "
                    "updated_at=? WHERE id=?",
                    (now, w["id"]),
                )
                workflows_blocked += 1
        await db.commit()
    finally:
        await db.close()
    return {"runs_reaped": len(reaped), "workflows_blocked": workflows_blocked,
            "reports_reaped": reports_reaped}


# ============================================================
# CODER WORKFLOWS — user-facing Coder Bot v2 workflow state
# ============================================================

def _row_to_coder_workflow(row) -> dict:
    w = dict(row)
    try:
        w["contract_json"] = json.loads(w.get("contract_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        w["contract_json"] = {}
    w["cancel_requested"] = bool(w.get("cancel_requested"))
    return w


async def create_coder_workflow(workflow_id: str, conversation_id: str, *,
                                project_id: str = "", mode: str = "",
                                state: str = "planning", user_task: str = "",
                                contract: dict | None = None,
                                active_run_id: str = "",
                                artifact_status: str = "not_ready") -> None:
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id))
        if not rows:
            return
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO coder_workflows(id, conversation_id, project_id, mode, state, "
            "user_task, contract_json, active_run_id, artifact_status, cancel_requested, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                workflow_id, conversation_id, project_id or "", mode or "",
                state or "planning", user_task or "", json.dumps(contract or {}),
                active_run_id or "", artifact_status or "not_ready", 0, now, now,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def update_coder_workflow(workflow_id: str, *, state: str | None = None,
                                contract: dict | None = None,
                                active_run_id: str | None = None,
                                artifact_status: str | None = None,
                                cancel_requested: bool | None = None,
                                project_id: str | None = None) -> None:
    sets = []
    vals = []
    if state is not None:
        sets.append("state=?")
        vals.append(state)
    if contract is not None:
        sets.append("contract_json=?")
        vals.append(json.dumps(contract))
    if active_run_id is not None:
        sets.append("active_run_id=?")
        vals.append(active_run_id)
    if artifact_status is not None:
        sets.append("artifact_status=?")
        vals.append(artifact_status)
    if cancel_requested is not None:
        sets.append("cancel_requested=?")
        vals.append(1 if cancel_requested else 0)
    if project_id is not None:
        sets.append("project_id=?")
        vals.append(project_id)
    if not sets:
        return
    sets.append("updated_at=?")
    vals.append(datetime.utcnow().isoformat())
    vals.extend([workflow_id, _scope_user()])
    db = await get_db()
    try:
        await db.execute(
            f"""UPDATE coder_workflows SET {', '.join(sets)}
                WHERE id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)""",
            tuple(vals),
        )
        await db.commit()
    finally:
        await db.close()


async def get_coder_workflow(workflow_id: str) -> dict | None:
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM coder_workflows WHERE id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)",
            (workflow_id, user_id),
        )
        if not rows:
            return None
        return _row_to_coder_workflow(rows[0])
    finally:
        await db.close()


async def get_coder_workflows_by_conversation(conversation_id: str,
                                              limit: int = 50) -> list[dict]:
    user_id = _scope_user()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM coder_workflows WHERE conversation_id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?) ORDER BY updated_at DESC LIMIT ?",
            (conversation_id, user_id, limit),
        )
        return [_row_to_coder_workflow(r) for r in rows]
    finally:
        await db.close()


async def get_latest_coder_workflow(conversation_id: str,
                                    project_id: str = "") -> dict | None:
    user_id = _scope_user()
    db = await get_db()
    try:
        if project_id:
            rows = await db.execute_fetchall(
                "SELECT * FROM coder_workflows WHERE conversation_id=? AND project_id=? "
                "AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?) ORDER BY updated_at DESC LIMIT 1",
                (conversation_id, project_id, user_id),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM coder_workflows WHERE conversation_id=? "
                "AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?) ORDER BY updated_at DESC LIMIT 1",
                (conversation_id, user_id),
            )
        if not rows:
            return None
        return _row_to_coder_workflow(rows[0])
    finally:
        await db.close()
