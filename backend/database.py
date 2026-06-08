"""
Database layer — SQLite with aiosqlite for async access.
Stores conversations, messages, knowledge bases, tools, model configs.
"""
import aiosqlite
import os
import json
import uuid
import re
from datetime import datetime
from config import DATABASE_PATH

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New Chat',
    model TEXT DEFAULT '',
    system_prompt TEXT DEFAULT '',
    model_config_id TEXT,
    tool_ids TEXT DEFAULT '[]',
    persona_name TEXT DEFAULT '',
    persona_avatar TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system', 'tool')),
    content TEXT NOT NULL DEFAULT '',
    metadata TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS knowledge_bases (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS kb_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    filepath TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    file_type TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (kb_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tools (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    filename TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS model_configs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_model TEXT NOT NULL,
    system_prompt TEXT DEFAULT '',
    tool_ids TEXT DEFAULT '[]',
    kb_ids TEXT DEFAULT '[]',
    parameters TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_kb_files_kb ON kb_files(kb_id);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    topics TEXT DEFAULT '[]',
    memory_enabled INTEGER DEFAULT 0,
    instructions TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workspace_conversations (
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workspace_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS conversation_files (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    url TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS council_configs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT 'My Council',
    host_model TEXT NOT NULL DEFAULT '',
    host_system_prompt TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS council_members (
    id TEXT PRIMARY KEY,
    council_id TEXT NOT NULL,
    model TEXT NOT NULL,
    system_prompt TEXT DEFAULT '',
    persona_name TEXT DEFAULT '',
    points INTEGER DEFAULT 0,
    FOREIGN KEY (council_id) REFERENCES council_configs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    content TEXT NOT NULL,
    type TEXT DEFAULT 'semantic',
    status TEXT DEFAULT 'accepted',
    category TEXT DEFAULT 'General',
    importance INTEGER DEFAULT 3,
    pinned INTEGER DEFAULT 0,
    source_conv_id TEXT,
    source_conversation_id TEXT,
    source_message_id INTEGER,
    confidence REAL DEFAULT 0,
    valid_from TIMESTAMP,
    valid_until TIMESTAMP,
    supersedes_id TEXT,
    entities_json TEXT DEFAULT '[]',
    metadata_json TEXT DEFAULT '{}',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

CREATE TABLE IF NOT EXISTS service_health_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    status TEXT NOT NULL,
    response_ms INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_health_service_time ON service_health_log(service, checked_at);

CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    model TEXT NOT NULL,
    persona_name TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_token_usage_model ON token_usage(model);
CREATE INDEX IF NOT EXISTS idx_token_usage_date ON token_usage(created_at);

CREATE TABLE IF NOT EXISTS coding_projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    language TEXT DEFAULT '',
    file_manifest TEXT DEFAULT '[]',
    last_plan TEXT DEFAULT '',
    conversation_id TEXT,
    openhands_project_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_coding_projects_conv ON coding_projects(conversation_id);

-- Coder Bot v2: durable agent runs. One row per agent invocation
-- (architect / builder.* / reviewer / acceptance / fixer / qa / generate_code wrapper).
-- Survives browser disconnects; UI re-renders from this table.
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    project_id      TEXT DEFAULT '',
    parent_run_id   TEXT DEFAULT '',
    started_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at        TIMESTAMP,
    result_envelope TEXT DEFAULT '{}',
    events_log      TEXT DEFAULT '[]',
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_runs_conv    ON runs(conversation_id);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_parent  ON runs(parent_run_id);

-- First-class Deep Research reports. The existing deep_research tool remains
-- compatibility-focused; these rows back the dedicated report workspace.
CREATE TABLE IF NOT EXISTS research_reports (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    query           TEXT NOT NULL,
    focus           TEXT DEFAULT '',
    report_type     TEXT NOT NULL DEFAULT 'analyst',
    status          TEXT NOT NULL DEFAULT 'queued',
    depth           INTEGER DEFAULT 3,
    model           TEXT DEFAULT '',
    planner_model   TEXT DEFAULT '',
    auditor_model   TEXT DEFAULT '',
    kb_ids          TEXT DEFAULT '[]',
    inputs_json     TEXT DEFAULT '[]',
    outline_json    TEXT DEFAULT '{}',
    findings_json   TEXT DEFAULT '[]',
    sources_json    TEXT DEFAULT '[]',
    metrics_json    TEXT DEFAULT '{}',
    report_markdown TEXT DEFAULT '',
    summary         TEXT DEFAULT '',
    error           TEXT DEFAULT '',
    events_log      TEXT DEFAULT '[]',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_research_reports_status  ON research_reports(status);
CREATE INDEX IF NOT EXISTS idx_research_reports_updated ON research_reports(updated_at);

CREATE TABLE IF NOT EXISTS workspace_research_reports (
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    report_id TEXT REFERENCES research_reports(id) ON DELETE CASCADE,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workspace_id, report_id)
);
CREATE INDEX IF NOT EXISTS idx_workspace_research_reports_report ON workspace_research_reports(report_id);

CREATE TABLE IF NOT EXISTS research_sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id     TEXT NOT NULL,
    source_index  INTEGER DEFAULT 0,
    title         TEXT DEFAULT '',
    url           TEXT DEFAULT '',
    snippet       TEXT DEFAULT '',
    tier          INTEGER DEFAULT 2,
    source_type   TEXT DEFAULT 'web',
    metadata_json TEXT DEFAULT '{}',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (report_id) REFERENCES research_reports(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_research_sources_report ON research_sources(report_id);

-- Coder Bot v2 hybrid workflow state. A workflow is the user-facing unit of
-- work; runs remain the per-agent invocations attached to that workflow.
CREATE TABLE IF NOT EXISTS coder_workflows (
    id               TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL,
    project_id       TEXT DEFAULT '',
    mode             TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'planning',
    user_task        TEXT DEFAULT '',
    contract_json    TEXT DEFAULT '{}',
    active_run_id    TEXT DEFAULT '',
    artifact_status  TEXT DEFAULT 'not_ready',
    cancel_requested INTEGER DEFAULT 0,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_coder_workflows_conv ON coder_workflows(conversation_id);
CREATE INDEX IF NOT EXISTS idx_coder_workflows_project ON coder_workflows(project_id);
CREATE INDEX IF NOT EXISTS idx_coder_workflows_state ON coder_workflows(state);
"""


async def get_db() -> aiosqlite.Connection:
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    db = await get_db()
    try:
        await db.executescript(DB_SCHEMA)
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
        # The legacy Deep Researcher agent duplicates the first-class Deep
        # Research workspace and the Agent Research tool. Remove only the fixed
        # preset row; user-created research agents use their own IDs.
        try:
            await db.execute("DELETE FROM model_configs WHERE id='mc-preset-deepresearch'")
            await db.execute("DELETE FROM model_configs WHERE name='💻 Coder Bot'")
        except Exception as e:
            print(f"[DB SEED] Legacy agent cleanup failed: {e}")
        await db.commit()
    finally:
        await db.close()


# ============================================================
# CONVERSATION CRUD
# ============================================================
async def create_conversation(id: str, title: str = "New Chat", model: str = "", system_prompt: str = "", model_config_id: str = None):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO conversations (id, title, model, system_prompt, model_config_id) VALUES (?, ?, ?, ?, ?)",
            (id, title, model, system_prompt, model_config_id)
        )
        await db.commit()
    finally:
        await db.close()


async def get_conversations(limit: int = 50, offset: int = 0):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM conversations ORDER BY CAST(COALESCE(pinned,'0') AS INTEGER) DESC, updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
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
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM conversations WHERE id = ?", (id,))
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
    db = await get_db()
    try:
        # Serialize list/dict fields
        if "tool_ids" in kwargs and isinstance(kwargs["tool_ids"], list):
            kwargs["tool_ids"] = json.dumps(kwargs["tool_ids"])
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = [_scrub_surrogates(v) for v in kwargs.values()] + [id]
        await db.execute(f"UPDATE conversations SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_conversation(id: str):
    db = await get_db()
    try:
        # Explicitly delete related rows (don't rely solely on CASCADE)
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
    db = await get_db()
    try:
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
    vals.append(message_id)
    db = await get_db()
    try:
        await db.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", tuple(vals))
        await db.commit()
    finally:
        await db.close()


async def delete_message(message_id: int) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# ============================================================
# KNOWLEDGE BASE CRUD
# ============================================================
async def create_kb(id: str, name: str, description: str = ""):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO knowledge_bases (id, name, description) VALUES (?, ?, ?)",
            (id, name, description)
        )
        await db.commit()
    finally:
        await db.close()


async def get_kbs():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM knowledge_bases ORDER BY updated_at DESC")
        kbs = [dict(r) for r in await cursor.fetchall()]
        for kb in kbs:
            cursor = await db.execute("SELECT * FROM kb_files WHERE kb_id = ?", (kb["id"],))
            kb["files"] = [dict(f) for f in await cursor.fetchall()]
        return kbs
    finally:
        await db.close()


async def add_kb_file(kb_id: str, filename: str, filepath: str, file_size: int, file_type: str) -> int:
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO kb_files (kb_id, filename, filepath, file_size, file_type) VALUES (?, ?, ?, ?, ?)",
            (kb_id, filename, filepath, file_size, file_type)
        )
        file_id = cursor.lastrowid
        await db.execute("UPDATE knowledge_bases SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (kb_id,))
        await db.commit()
        return file_id
    finally:
        await db.close()


async def update_kb(id: str, **kwargs):
    db = await get_db()
    try:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [id]
        await db.execute(f"UPDATE knowledge_bases SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_kb(id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM knowledge_bases WHERE id = ?", (id,))
        await db.commit()
    finally:
        await db.close()


async def delete_kb_file(file_id: int):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT filepath FROM kb_files WHERE id = ?", (file_id,))
        row = await cursor.fetchone()
        # Delete from DB first — if disk removal fails, at least DB is consistent
        await db.execute("DELETE FROM kb_files WHERE id = ?", (file_id,))
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
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO tools (id, name, description, filename, code) VALUES (?, ?, ?, ?, ?)",
            (id, name, description, filename, code)
        )
        await db.commit()
    finally:
        await db.close()


async def get_tools():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM tools ORDER BY updated_at DESC")
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


async def update_tool(id: str, **kwargs):
    db = await get_db()
    try:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [id]
        await db.execute(f"UPDATE tools SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_tool(id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM tools WHERE id = ?", (id,))
        await db.commit()
    finally:
        await db.close()


# ============================================================
# MODEL CONFIG CRUD
# ============================================================
async def create_model_config(id: str, name: str, base_model: str, system_prompt: str = "",
                               tool_ids: list = None, kb_ids: list = None, parameters: dict = None):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO model_configs (id, name, base_model, system_prompt, tool_ids, kb_ids, parameters) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, name, base_model, system_prompt, json.dumps(tool_ids or []), json.dumps(kb_ids or []), json.dumps(parameters or {}))
        )
        await db.commit()
    finally:
        await db.close()


async def get_model_configs():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM model_configs ORDER BY updated_at DESC")
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
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM model_configs WHERE id = ?", (mc_id,))
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
    db = await get_db()
    try:
        for k in ("tool_ids", "kb_ids", "parameters"):
            if k in kwargs:
                kwargs[k] = json.dumps(kwargs[k])
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [id]
        await db.execute(f"UPDATE model_configs SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_model_config(id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM model_configs WHERE id = ?", (id,))
        await db.commit()
    finally:
        await db.close()


# ============================================================
# WORKSPACE CRUD
# ============================================================
async def create_workspace(id: str, name: str, description: str = ""):
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO workspaces(id,name,description,topics,memory_enabled,instructions,created_at,updated_at) VALUES(?,?,?,'[]',0,'',?,?)",
            (id, name, description, now, now)
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
    db = await get_db()
    try:
        rows = await db.execute_fetchall("""
            SELECT w.*,
                (SELECT COUNT(*) FROM workspace_conversations wc WHERE wc.workspace_id=w.id) as conv_count,
                (SELECT COUNT(*) FROM conversation_files cf JOIN workspace_conversations wc2 ON cf.conversation_id=wc2.conversation_id WHERE wc2.workspace_id=w.id) as file_count,
                (SELECT COUNT(*) FROM workspace_research_reports wrr WHERE wrr.workspace_id=w.id) as report_count
            FROM workspaces w ORDER BY w.updated_at DESC""")
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
    db = await get_db()
    try:
        row = await db.execute_fetchall("SELECT * FROM workspaces WHERE id=?", (ws_id,))
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
               WHERE wc.workspace_id=? ORDER BY wc.added_at DESC""",
            (ws_id,)
        )
        ws["conversations"] = [dict(r) for r in conv_rows]
        file_rows = await db.execute_fetchall(
            """SELECT cf.*,c.title as conversation_title FROM conversation_files cf
               JOIN workspace_conversations wc ON cf.conversation_id=wc.conversation_id
               LEFT JOIN conversations c ON c.id=cf.conversation_id
               WHERE wc.workspace_id=? ORDER BY cf.created_at DESC""",
            (ws_id,)
        )
        ws["files"] = [dict(r) for r in file_rows]
        report_rows = await db.execute_fetchall(
            """SELECT rr.id,rr.title,rr.query,rr.focus,rr.report_type,rr.status,rr.depth,
                      rr.model,rr.summary,rr.error,rr.sources_json,rr.metrics_json,
                      rr.created_at,rr.updated_at,rr.completed_at,wrr.added_at
               FROM research_reports rr
               JOIN workspace_research_reports wrr ON rr.id=wrr.report_id
               WHERE wrr.workspace_id=? ORDER BY wrr.added_at DESC""",
            (ws_id,)
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
    db = await get_db()
    try:
        await db.execute(f"UPDATE workspaces SET {set_clause} WHERE id=?", (*fields.values(), ws_id))
        await db.commit()
    finally:
        await db.close()


async def delete_workspace(ws_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM workspaces WHERE id=?", (ws_id,))
        await db.commit()
    finally:
        await db.close()


async def add_conv_to_workspace(ws_id: str, conv_id: str):
    db = await get_db()
    try:
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
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM workspace_conversations WHERE workspace_id=? AND conversation_id=?",
            (ws_id, conv_id)
        )
        await db.commit()
    finally:
        await db.close()


async def add_research_report_to_workspace(ws_id: str, report_id: str):
    db = await get_db()
    try:
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
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM workspace_research_reports WHERE workspace_id=? AND report_id=?",
            (ws_id, report_id)
        )
        await db.execute("UPDATE workspaces SET updated_at=? WHERE id=?", (datetime.utcnow().isoformat(), ws_id))
        await db.commit()
    finally:
        await db.close()


async def add_conversation_file(id: str, conv_id: str, filename: str, url: str):
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO conversation_files(id,conversation_id,filename,url,created_at) VALUES(?,?,?,?,?)",
            (id, conv_id, filename, url, now)
        )
        await db.commit()
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


async def get_workspace_basic(ws_id: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM workspaces WHERE id=?", (ws_id,))
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
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT w.* FROM workspaces w
               JOIN workspace_conversations wc ON wc.workspace_id=w.id
               WHERE wc.conversation_id=?
               ORDER BY wc.added_at DESC""",
            (conv_id,),
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
    clauses = ["workspace_id=?"]
    vals = [ws_id]
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
               (id,workspace_id,content,type,status,category,importance,pinned,
                source_conv_id,source_conversation_id,source_message_id,confidence,
                valid_from,valid_until,supersedes_id,entities_json,metadata_json,updated_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                mem_id, ws_id, clean_content, mtype, mstatus, category or "General",
                importance, 1 if pinned else 0, source_conv_id or source_conversation_id,
                source_conversation_id or source_conv_id, source_message_id,
                float(confidence or 0), valid_from, valid_until, supersedes_id,
                json.dumps(entities or []), json.dumps(metadata or {}), now, now,
            ),
        )
        await db.execute("UPDATE workspaces SET updated_at=? WHERE id=?", (now, ws_id))
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM memories WHERE id=?", (mem_id,))
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
    vals = list(fields.values()) + [memory_id, ws_id]
    db = await get_db()
    try:
        await db.execute(f"UPDATE memories SET {sets} WHERE id=? AND workspace_id=?", vals)
        await db.execute("UPDATE workspaces SET updated_at=? WHERE id=?", (fields["updated_at"], ws_id))
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM memories WHERE id=? AND workspace_id=?", (memory_id, ws_id))
        return _normalize_memory_row(rows[0]) if rows else None
    finally:
        await db.close()


async def accept_workspace_memory(memory_id: str, ws_id: str, supersedes_id: str | None = None) -> dict | None:
    now = datetime.utcnow().isoformat()
    db = await get_db()
    try:
        if supersedes_id:
            await db.execute(
                "UPDATE memories SET valid_until=?, updated_at=? WHERE id=? AND workspace_id=?",
                (now, now, supersedes_id, ws_id),
            )
        await db.execute(
            """UPDATE memories
               SET status='accepted',
                   supersedes_id=COALESCE(?, supersedes_id),
                   valid_from=COALESCE(valid_from, ?),
                   updated_at=?
               WHERE id=? AND workspace_id=?""",
            (supersedes_id, now, now, memory_id, ws_id),
        )
        await db.execute("UPDATE workspaces SET updated_at=? WHERE id=?", (now, ws_id))
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM memories WHERE id=? AND workspace_id=?", (memory_id, ws_id))
        return _normalize_memory_row(rows[0]) if rows else None
    finally:
        await db.close()


async def reject_workspace_memory(memory_id: str, ws_id: str) -> dict | None:
    return await update_workspace_memory(memory_id, ws_id, status="rejected")


async def delete_workspace_memory(memory_id: str, ws_id: str) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM memories WHERE id=? AND workspace_id=?", (memory_id, ws_id))
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
                  AND status='accepted'
                  AND {_memory_current_clause(now)}
                ORDER BY pinned DESC, importance DESC, COALESCE(updated_at, created_at) DESC
                LIMIT 80""",
            (ws_id, now),
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
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO council_configs(id,name,host_model,host_system_prompt,kb_ids,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (id, name, host_model, host_system_prompt, json.dumps(kb_ids or []), now, now)
        )
        await db.commit()
    finally:
        await db.close()


async def get_councils():
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM council_configs ORDER BY updated_at DESC")
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
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM council_configs WHERE id=?", (council_id,))
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
    db = await get_db()
    try:
        await db.execute(f"UPDATE council_configs SET {set_clause} WHERE id=?", (*fields.values(), council_id))
        await db.commit()
    finally:
        await db.close()


async def delete_council(council_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM council_configs WHERE id=?", (council_id,))
        await db.commit()
    finally:
        await db.close()


async def add_council_member(id: str, council_id: str, model: str, system_prompt: str = "", persona_name: str = ""):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO council_members(id,council_id,model,system_prompt,persona_name,points) VALUES(?,?,?,?,?,0)",
            (id, council_id, model, system_prompt, persona_name)
        )
        await db.execute("UPDATE council_configs SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (council_id,))
        await db.commit()
    finally:
        await db.close()


async def update_council_member(member_id: str, **kwargs):
    allowed = {"model", "system_prompt", "persona_name", "points"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ",".join(f"{k}=?" for k in fields)
    db = await get_db()
    try:
        await db.execute(f"UPDATE council_members SET {set_clause} WHERE id=?", (*fields.values(), member_id))
        await db.commit()
    finally:
        await db.close()


async def delete_council_member(member_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM council_members WHERE id=?", (member_id,))
        await db.commit()
    finally:
        await db.close()


async def get_kb_files_for_kbs(kb_ids: list) -> list:
    """Load all file records (with KB name) for a list of KB IDs."""
    if not kb_ids:
        return []
    db = await get_db()
    try:
        placeholders = ",".join("?" * len(kb_ids))
        cursor = await db.execute(
            f"SELECT kb_files.*, knowledge_bases.name AS kb_name FROM kb_files "
            f"JOIN knowledge_bases ON kb_files.kb_id = knowledge_bases.id "
            f"WHERE kb_files.kb_id IN ({placeholders})",
            kb_ids
        )
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


# ============================================================
# FULL-TEXT SEARCH
# ============================================================
async def search_messages(query: str, limit: int = 20):
    """Full-text search across all messages, returning conversation context."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT m.id, m.conversation_id, m.role, m.content,
                   snippet(messages_fts, 0, '<mark>', '</mark>', '...', 32) AS snippet,
                   c.title AS conv_title
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN conversations c ON c.id = m.conversation_id
            WHERE messages_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit))
        return [dict(r) for r in await cursor.fetchall()]
    except Exception as e:
        print(f"[SEARCH] Error: {e}")
        return []
    finally:
        await db.close()


# ============================================================
# CONVERSATION FORKING
# ============================================================
async def fork_conversation(original_conv_id: str, fork_msg_id: int, new_conv_id: str):
    """Fork a conversation at a specific message, copying messages up to that point."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM conversations WHERE id = ?", (original_conv_id,))
        original = await cursor.fetchone()
        if not original:
            return None
        original = dict(original)
        now = datetime.utcnow().isoformat()
        await db.execute(
            """INSERT INTO conversations
               (id, title, model, system_prompt, model_config_id, tool_ids,
                persona_name, persona_avatar, forked_from, fork_point_msg_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_conv_id, f"Fork of {original.get('title', 'Chat')}", original.get("model", ""),
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
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, title, fork_point_msg_id, created_at FROM conversations WHERE forked_from = ?",
            (conv_id,))
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


# ============================================================
# TOKEN USAGE ANALYTICS
# ============================================================
async def record_token_usage(conversation_id: str, model: str, persona_name: str,
                              prompt_tokens: int, completion_tokens: int):
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO token_usage (conversation_id, model, persona_name,
               prompt_tokens, completion_tokens, total_tokens)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (conversation_id, model, persona_name or "", prompt_tokens, completion_tokens,
             prompt_tokens + completion_tokens)
        )
        await db.commit()
    except Exception as e:
        print(f"[TOKEN USAGE] Error recording: {e}")
    finally:
        await db.close()


async def get_token_usage(days: int = 30, group_by: str = "day"):
    """Aggregate token usage. group_by: day, model, persona."""
    db = await get_db()
    try:
        if group_by == "model":
            q = """SELECT model, SUM(prompt_tokens) as prompt_tokens,
                   SUM(completion_tokens) as completion_tokens,
                   SUM(total_tokens) as total_tokens, COUNT(*) as request_count
                   FROM token_usage WHERE created_at >= datetime('now', ?)
                   GROUP BY model ORDER BY total_tokens DESC"""
        elif group_by == "persona":
            q = """SELECT persona_name, SUM(prompt_tokens) as prompt_tokens,
                   SUM(completion_tokens) as completion_tokens,
                   SUM(total_tokens) as total_tokens, COUNT(*) as request_count
                   FROM token_usage WHERE created_at >= datetime('now', ?)
                   GROUP BY persona_name ORDER BY total_tokens DESC"""
        else:
            q = """SELECT date(created_at) as date,
                   SUM(prompt_tokens) as prompt_tokens,
                   SUM(completion_tokens) as completion_tokens,
                   SUM(total_tokens) as total_tokens, COUNT(*) as request_count
                   FROM token_usage WHERE created_at >= datetime('now', ?)
                   GROUP BY date(created_at) ORDER BY date ASC"""
        cursor = await db.execute(q, (f"-{days} days",))
        return [dict(r) for r in await cursor.fetchall()]
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
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO research_reports(id,title,query,focus,report_type,status,depth,"
            "model,planner_model,auditor_model,kb_ids,inputs_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                report_id, title or query[:80], query, focus or "", report_type or "analyst",
                status, int(depth or 3), model or "", planner_model or "",
                auditor_model or "", json.dumps(kb_ids or []), json.dumps(inputs or []),
                now, now,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def update_research_report(report_id: str, **kwargs) -> None:
    if not kwargs:
        return
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
        return
    sets.append("updated_at=?")
    vals.append(datetime.utcnow().isoformat())
    vals.append(report_id)
    db = await get_db()
    try:
        await db.execute(f"UPDATE research_reports SET {', '.join(sets)} WHERE id=?", tuple(vals))
        await db.commit()
    finally:
        await db.close()


async def append_research_event(report_id: str, event: dict) -> None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT events_log FROM research_reports WHERE id=?", (report_id,))
        if not rows:
            return
        try:
            log = json.loads(rows[0]["events_log"] or "[]")
        except (json.JSONDecodeError, TypeError):
            log = []
        if "ts" not in event:
            event = {**event, "ts": datetime.utcnow().isoformat()}
        log.append(event)
        await db.execute(
            "UPDATE research_reports SET events_log=?, updated_at=? WHERE id=?",
            (json.dumps(log), datetime.utcnow().isoformat(), report_id),
        )
        await db.commit()
    finally:
        await db.close()


async def replace_research_sources(report_id: str, sources: list[dict]) -> None:
    db = await get_db()
    try:
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
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM research_reports WHERE id=?", (report_id,))
        if not rows:
            return None
        report = _parse_research_report(rows[0])
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
    db = await get_db()
    try:
        if query:
            like = f"%{query}%"
            rows = await db.execute_fetchall(
                "SELECT id,title,query,focus,report_type,status,depth,model,summary,error,"
                "sources_json,metrics_json,created_at,updated_at,completed_at "
                "FROM research_reports WHERE title LIKE ? OR query LIKE ? OR summary LIKE ? "
                "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (like, like, like, limit, offset),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT id,title,query,focus,report_type,status,depth,model,summary,error,"
                "sources_json,metrics_json,created_at,updated_at,completed_at "
                "FROM research_reports ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
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
    db = await get_db()
    try:
        await db.execute("DELETE FROM workspace_research_reports WHERE report_id=?", (report_id,))
        await db.execute("DELETE FROM research_sources WHERE report_id=?", (report_id,))
        await db.execute("DELETE FROM research_reports WHERE id=?", (report_id,))
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
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        manifest_json = json.dumps(file_manifest or [])
        existing = await db.execute_fetchall("SELECT id FROM coding_projects WHERE id = ?", (project_id,))
        if existing:
            await db.execute(
                "UPDATE coding_projects SET name=?, description=?, language=?, file_manifest=?, "
                "last_plan=?, conversation_id=?, openhands_project_id=?, updated_at=? WHERE id=?",
                (name, description, language, manifest_json, last_plan,
                 conversation_id, openhands_project_id, now, project_id)
            )
        else:
            await db.execute(
                "INSERT INTO coding_projects(id,name,description,language,file_manifest,last_plan,"
                "conversation_id,openhands_project_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (project_id, name, description, language, manifest_json, last_plan,
                 conversation_id, openhands_project_id, now, now)
            )
        await db.commit()
    finally:
        await db.close()


async def get_coding_project_by_conv(conversation_id: str):
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM coding_projects WHERE conversation_id = ? ORDER BY updated_at DESC LIMIT 1",
            (conversation_id,)
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
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM coding_projects WHERE id = ?", (project_id,))
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
    db = await get_db()
    try:
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
    vals.append(run_id)
    db = await get_db()
    try:
        await db.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", tuple(vals))
        await db.commit()
    finally:
        await db.close()


async def append_run_event(run_id: str, event: dict) -> None:
    """Append a structured event to a run's events_log (JSON array, append-only).

    Reads the current events_log, appends, writes back. Concurrent appends to the
    same run are not expected (one writer per run by design); if that ever changes,
    move to a separate run_events table.
    """
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT events_log FROM runs WHERE id=?", (run_id,))
        if not rows:
            return
        try:
            log = json.loads(rows[0]["events_log"] or "[]")
        except (json.JSONDecodeError, TypeError):
            log = []
        # Stamp the event with a server-side timestamp so order is reliable
        # even when callers don't pass one.
        if "ts" not in event:
            event = {**event, "ts": datetime.utcnow().isoformat()}
        log.append(event)
        await db.execute("UPDATE runs SET events_log=? WHERE id=?",
                         (json.dumps(log), run_id))
        await db.commit()
    finally:
        await db.close()


async def get_run(run_id: str) -> dict | None:
    """Return a single run with parsed result_envelope and events_log."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM runs WHERE id=?", (run_id,))
        if not rows:
            return None
        return _row_to_run(rows[0])
    finally:
        await db.close()


async def get_runs_by_conversation(conversation_id: str, limit: int = 100) -> list[dict]:
    """All runs for a conversation, newest first. Used by the frontend on reconnect
    to rebuild the run cards under each message."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM runs WHERE conversation_id=? ORDER BY started_at DESC LIMIT ?",
            (conversation_id, limit)
        )
        return [_row_to_run(r) for r in rows]
    finally:
        await db.close()


async def get_runs_by_project(project_id: str, limit: int = 50) -> list[dict]:
    """All runs that touched a given project, newest first."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM runs WHERE project_id=? ORDER BY started_at DESC LIMIT ?",
            (project_id, limit)
        )
        return [_row_to_run(r) for r in rows]
    finally:
        await db.close()


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
    db = await get_db()
    try:
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
    vals.append(workflow_id)
    db = await get_db()
    try:
        await db.execute(f"UPDATE coder_workflows SET {', '.join(sets)} WHERE id=?", tuple(vals))
        await db.commit()
    finally:
        await db.close()


async def get_coder_workflow(workflow_id: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM coder_workflows WHERE id=?", (workflow_id,))
        if not rows:
            return None
        return _row_to_coder_workflow(rows[0])
    finally:
        await db.close()


async def get_coder_workflows_by_conversation(conversation_id: str,
                                              limit: int = 50) -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM coder_workflows WHERE conversation_id=? ORDER BY updated_at DESC LIMIT ?",
            (conversation_id, limit),
        )
        return [_row_to_coder_workflow(r) for r in rows]
    finally:
        await db.close()


async def get_latest_coder_workflow(conversation_id: str,
                                    project_id: str = "") -> dict | None:
    db = await get_db()
    try:
        if project_id:
            rows = await db.execute_fetchall(
                "SELECT * FROM coder_workflows WHERE conversation_id=? AND project_id=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (conversation_id, project_id),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM coder_workflows WHERE conversation_id=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (conversation_id,),
            )
        if not rows:
            return None
        return _row_to_coder_workflow(rows[0])
    finally:
        await db.close()
