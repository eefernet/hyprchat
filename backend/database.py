"""
Database layer — SQLite with aiosqlite for async access.
Stores conversations, messages, knowledge bases, tools, model configs.
"""
import aiosqlite
import os
import json
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
    content TEXT NOT NULL,
    category TEXT DEFAULT 'General',
    importance INTEGER DEFAULT 3,
    pinned INTEGER DEFAULT 0,
    source_conv_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS service_health_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    status TEXT NOT NULL,
    response_ms INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_health_service_time ON service_health_log(service, checked_at);
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
        # Migrate: add new columns to existing tables if missing
        for col, default in [("tool_ids", "'[]'"), ("persona_name", "''"), ("persona_avatar", "''"),
                              ("is_council", "0"), ("council_config_id", "NULL"), ("use_memories", "0"),
                              ]:
            try:
                await db.execute(f"ALTER TABLE conversations ADD COLUMN {col} TEXT DEFAULT {default}")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"[DB MIGRATION] Warning: {e}")
        # Migrate council_configs: add debate_rounds column
        for col, default in [("debate_rounds", "0")]:
            try:
                await db.execute(f"ALTER TABLE council_configs ADD COLUMN {col} INTEGER DEFAULT {default}")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"[DB MIGRATION] Warning: {e}")
        # Seed preset personas (idempotent — fixed ID)
        DEEP_RESEARCHER_PROMPT = """You are Deep Researcher — an expert research analyst powered by real-time multi-source intelligence gathering.

## Primary Directive
Use deep_research as your first action for any substantive question. Training data goes stale — always verify with live research.

## Depth Selection
Default to **depth 3** for most questions. Scale up/down:
- **1** — Quick fact-check, simple definition, current price/stat (~20s)
- **2** — Topic overview, recent news summary (~40s)
- **3** — Technical deep-dive, how-something-works, best practices (~60s) ← DEFAULT
- **4** — Comprehensive analysis, policy/legal/medical topics (~90s)
- **5** — Exhaustive sweep — explicitly requested or genuinely complex (~120s)

## Mode & Focus
- Set `mode:"compare"` + `topic_b` for any head-to-head question (e.g. "X vs Y")
- Set `focus` to the user's specific angle: security, performance, cost, recent developments, tutorials, etc.
- Use `mode:"quick"` only for trivially simple lookups

## After Research Returns
Synthesize — don't dump. Your value is in analysis:
1. **Lead with the answer** — direct response to what was asked
2. **Structure clearly** — headers, tables, bullets; make it scannable
3. **Cite sources** — reference [N] citations where they add weight
4. **Add analysis** — implications, caveats, confidence level, what's still uncertain
5. **Offer follow-ups** — 2-3 targeted directions to go deeper

## Skip the Tool When
- Pure math, code generation, or creative writing
- The user says "quickly" / "from memory" / "off the top of your head"
- A follow-up is already covered by prior research in this chat

Be rigorous, structured, and honest about uncertainty. When sources conflict, say so."""
        try:
            exists = await db.execute_fetchall(
                "SELECT id FROM model_configs WHERE id='mc-preset-deepresearch'"
            )
            if not exists:
                now = datetime.utcnow().isoformat()
                await db.execute(
                    "INSERT INTO model_configs(id,name,base_model,system_prompt,tool_ids,kb_ids,parameters,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("mc-preset-deepresearch", "🔬 Deep Researcher", "qwen3.5:27b",
                     DEEP_RESEARCHER_PROMPT, '["deep_research"]', '[]', '{}', now, now)
                )
        except Exception as e:
            print(f"[DB SEED] {e}")
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
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ? OFFSET ?",
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
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC", (id,)
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


async def update_conversation(id: str, **kwargs):
    if not kwargs:
        return
    db = await get_db()
    try:
        # Serialize list/dict fields
        if "tool_ids" in kwargs and isinstance(kwargs["tool_ids"], list):
            kwargs["tool_ids"] = json.dumps(kwargs["tool_ids"])
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [id]
        await db.execute(f"UPDATE conversations SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_conversation(id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM conversations WHERE id = ?", (id,))
        await db.commit()
    finally:
        await db.close()


async def add_message(conversation_id: str, role: str, content: str, metadata: dict = None):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content, metadata) VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, json.dumps(metadata or {}))
        )
        await db.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conversation_id,)
        )
        await db.commit()
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
            "INSERT INTO workspaces(id,name,description,topics,created_at,updated_at) VALUES(?,?,?,'[]',?,?)",
            (id, name, description, now, now)
        )
        await db.commit()
    finally:
        await db.close()
    return {"id": id, "name": name, "description": description, "topics": [], "conv_count": 0, "file_count": 0}


async def get_workspaces():
    db = await get_db()
    try:
        rows = await db.execute_fetchall("""
            SELECT w.*,
                (SELECT COUNT(*) FROM workspace_conversations wc WHERE wc.workspace_id=w.id) as conv_count,
                (SELECT COUNT(*) FROM conversation_files cf JOIN workspace_conversations wc2 ON cf.conversation_id=wc2.conversation_id WHERE wc2.workspace_id=w.id) as file_count
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
        return ws
    finally:
        await db.close()


async def update_workspace(ws_id: str, **kwargs):
    allowed = {"name", "description", "topics"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    if "topics" in fields and isinstance(fields["topics"], list):
        fields["topics"] = json.dumps(fields["topics"])
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
# COUNCIL CRUD
# ============================================================
async def create_council(id: str, name: str, host_model: str, host_system_prompt: str = ""):
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO council_configs(id,name,host_model,host_system_prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (id, name, host_model, host_system_prompt, now, now)
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
        members = await db.execute_fetchall(
            "SELECT * FROM council_members WHERE council_id=? ORDER BY rowid ASC", (council_id,)
        )
        c["members"] = [dict(m) for m in members]
        return c
    finally:
        await db.close()


async def update_council(council_id: str, **kwargs):
    allowed = {"name", "host_model", "host_system_prompt", "debate_rounds"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
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
