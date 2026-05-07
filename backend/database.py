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

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    steps TEXT NOT NULL DEFAULT '[]',
    webhook_id TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    conversation_id TEXT,
    status TEXT DEFAULT 'pending',
    input TEXT DEFAULT '',
    step_results TEXT DEFAULT '[]',
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    error TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workflow_schedules (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    cron_expr TEXT NOT NULL,
    input_template TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    last_run_at TIMESTAMP,
    next_run_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
);

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
-- (architect / builder.* / reviewer / fixer / qa / generate_code wrapper).
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
                              ("pinned", "0"),
                              ]:
            try:
                await db.execute(f"ALTER TABLE conversations ADD COLUMN {col} TEXT DEFAULT {default}")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"[DB MIGRATION] Warning: {e}")
        # Migrate council_configs: add debate_rounds and kb_ids columns
        for col, coltype, default in [("debate_rounds", "INTEGER", "0"), ("kb_ids", "TEXT", "'[]'")]:
            try:
                await db.execute(f"ALTER TABLE council_configs ADD COLUMN {col} {coltype} DEFAULT {default}")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"[DB MIGRATION] Warning: {e}")
        # Migrate workflows: add webhook_id column
        try:
            await db.execute("ALTER TABLE workflows ADD COLUMN webhook_id TEXT DEFAULT ''")
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

## Avoiding Research Loops — CRITICAL
Every `deep_research` call costs ~1–3 minutes of wall-clock and ~1,500–3,000 prompt tokens. Chaining them is expensive and rarely productive. Hard rules:

- **Same-topic cap — 2 calls maximum.** If you've called `deep_research` twice on the same underlying topic (even with reworded `focus` / `topic`) and both returned approximate, similar, or "as-of" data, **STOP searching**. That IS the data. Move to synthesis: use `execute_code` on the approximations if math is needed, then present findings. Flag any uncertainty in a `> [!NOTE]` callout. Do not call `deep_research` a third time hoping a new wording will surface magically-exact numbers.
- **Recency realism.** Do not hunt for exact current-year or very-recent figures that may not yet be audited or published. If research returns "projected" / "estimated" / "preliminary" / "YTD" / "as of [date]" figures, treat those as the answer — **use them**, compute with them if needed, and flag the preliminary status in a `> [!NOTE]` or `> [!CAUTION]` callout. Re-searching for exact figures that don't exist yet is a loop, not rigor.
- **Reframe before re-searching.** If the first call's data feels insufficient, ask: is the gap *real* (a legitimately missing angle) or *perfectionism* (wanting a rounder number)? Only re-search for the former. For perfectionism, synthesize what you have.

A chart built from approximate-but-acknowledged numbers with a `[!NOTE]` about data freshness is strictly better than an infinite hunt for exact numbers that don't exist.

## After Research Returns
Synthesize — don't dump. Your value is in analysis:
1. **Lead with the answer** — direct response to what was asked
2. **Structure clearly** — headers, tables, bullets; make it scannable
3. **Cite sources** — reference [N] citations where they add weight
4. **Add analysis** — implications, caveats, confidence level, what's still uncertain
5. **Offer follow-ups** — 2-3 targeted directions to go deeper

## Computation — Use `execute_code` for Real Math
You have `execute_code` for a reason: LLM mental math is unreliable past trivial cases, and research findings are worthless if the numbers are wrong.

**Always call `execute_code` for:**
- Compound growth rates (CAGR, CMGR), weighted averages, variance, standard deviation
- Percentage shares summing to 100 (e.g. market-share breakdowns)
- Aggregating / filtering / sorting numbers pulled from research
- Date arithmetic, unit conversion, currency conversion
- Anything where the user would notice if you were off by 5%

**The compute-then-chart pattern is standard:**
1. Research returns numbers (raw sources, survey results, benchmarks).
2. Call `execute_code` to compute derived values — print them as JSON to stdout so they're easy to read back.
3. Emit a ```chart fence using the computed numbers.

Never hand-compute a CAGR, weighted average, or percentage split when `execute_code` would be exact. A chart with wrong numbers is worse than no chart.

## Presenting Findings — Use Rich Rendering
The chat UI renders rich inline formats. Reach for these when they fit the content — don't fall back on bare numbers or "imagine a chart of..." text:
- **Quantitative data** (benchmarks, growth, market share, survey results, distributions — any 3+ comparable numbers) → emit a ```chart fence. Pick the type that fits: `bar` for categorical comparison, `line` for trends over time, `pie`/`doughnut` for proportions of a whole, `scatter` for correlation, `radar` for multi-attribute profiles. The fence renders directly — never write Python/matplotlib/plotly to SAVE a chart image. (You may use `execute_code` to COMPUTE the numbers that go into the fence — that's encouraged — but the visual itself is always a ```chart fence.)
- **Source conflicts or uncertainty** → `> [!NOTE]` callout so the reader knows not to take the synthesis at face value
- **Findings that materially change the conclusion** → `> [!IMPORTANT]` callout
- **Deprecations, security issues, breaking changes, harmful misinformation** → `> [!WARNING]` or `> [!CAUTION]` callout
- **Practical recommendations / actionable advice** → `> [!TIP]` callout
- **Multi-attribute comparisons** → markdown tables with column alignment (`|:---|---:|:---:|`) — right-align numbers, center-align status
- **Commands, keyboard shortcuts** → `<kbd>` tags for keys, inline code for commands
- **Hex / RGB / HSL colors** → write them literally in the text; swatches auto-render

When the answer hinges on "which is bigger / faster / more common / trending up", a chart fence is almost always better than a paragraph. Prefer a 20-word intro + chart over a 200-word number dump.

## Tool Selection — Which One, When
- **`deep_research`** — first action for substantive questions requiring live sources
- **`execute_code`** — pure math, numerical aggregation, statistical work, date/unit conversion, or computing derived values from research results
- **Skip both** when: the user says "quickly" / "from memory" / "off the top of your head", or a follow-up is already covered by prior research in this chat

Be rigorous, structured, and honest about uncertainty. When sources conflict, say so."""
        try:
            exists = await db.execute_fetchall(
                "SELECT id FROM model_configs WHERE id='mc-preset-deepresearch'"
            )
            now = datetime.utcnow().isoformat()
            DEEP_RESEARCHER_TOOLS = '["deep_research", "execute_code"]'
            if not exists:
                await db.execute(
                    "INSERT INTO model_configs(id,name,base_model,system_prompt,tool_ids,kb_ids,parameters,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("mc-preset-deepresearch", "🔬 Deep Researcher", "qwen3.5:27b",
                     DEEP_RESEARCHER_PROMPT, DEEP_RESEARCHER_TOOLS, '[]', '{}', now, now)
                )
            else:
                # Refresh prompt + tool_ids on version bumps so existing installs pick up guidance
                # updates and newly-required tools (e.g. execute_code for numerical reliability).
                # Preserves base_model, kb_ids, parameters — those may be user-customized.
                await db.execute(
                    "UPDATE model_configs SET system_prompt=?, tool_ids=?, updated_at=? WHERE id='mc-preset-deepresearch'",
                    (DEEP_RESEARCHER_PROMPT, DEEP_RESEARCHER_TOOLS, now)
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
# WORKFLOW CRUD
# ============================================================
async def create_workflow(id: str, name: str, description: str, steps: str):
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO workflows (id, name, description, steps, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (id, name, description, steps, now, now)
        )
        await db.commit()
    finally:
        await db.close()


async def get_workflows():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM workflows ORDER BY updated_at DESC")
        wfs = []
        for r in await cursor.fetchall():
            w = dict(r)
            try:
                w["steps"] = json.loads(w["steps"])
            except (json.JSONDecodeError, TypeError):
                w["steps"] = []
            wfs.append(w)
        return wfs
    finally:
        await db.close()


async def get_workflow(id: str):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM workflows WHERE id = ?", (id,))
        row = await cursor.fetchone()
        if not row:
            return None
        w = dict(row)
        try:
            w["steps"] = json.loads(w["steps"])
        except (json.JSONDecodeError, TypeError):
            w["steps"] = []
        return w
    finally:
        await db.close()


async def update_workflow(id: str, **kwargs):
    if not kwargs:
        return
    db = await get_db()
    try:
        if "steps" in kwargs and isinstance(kwargs["steps"], list):
            kwargs["steps"] = json.dumps(kwargs["steps"])
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [id]
        await db.execute(f"UPDATE workflows SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_workflow(id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM workflows WHERE id = ?", (id,))
        await db.commit()
    finally:
        await db.close()


async def create_workflow_run(id: str, workflow_id: str, conversation_id: str, input_text: str):
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO workflow_runs (id, workflow_id, conversation_id, status, input, created_at) VALUES (?, ?, ?, 'pending', ?, ?)",
            (id, workflow_id, conversation_id or "", input_text, now)
        )
        await db.commit()
    finally:
        await db.close()


async def update_workflow_run(id: str, **kwargs):
    if not kwargs:
        return
    db = await get_db()
    try:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [id]
        await db.execute(f"UPDATE workflow_runs SET {sets} WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def get_workflow_runs(workflow_id: str = None, limit: int = 20):
    db = await get_db()
    try:
        if workflow_id:
            cursor = await db.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ? ORDER BY created_at DESC LIMIT ?",
                (workflow_id, limit))
        else:
            cursor = await db.execute(
                "SELECT * FROM workflow_runs ORDER BY created_at DESC LIMIT ?", (limit,))
        runs = []
        for r in await cursor.fetchall():
            run = dict(r)
            try:
                run["step_results"] = json.loads(run.get("step_results", "[]"))
            except (json.JSONDecodeError, TypeError):
                run["step_results"] = []
            runs.append(run)
        return runs
    finally:
        await db.close()


async def get_workflow_by_webhook(webhook_id: str):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM workflows WHERE webhook_id = ?", (webhook_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        w = dict(row)
        try:
            w["steps"] = json.loads(w["steps"])
        except (json.JSONDecodeError, TypeError):
            w["steps"] = []
        return w
    finally:
        await db.close()


# ============================================================
# WORKFLOW SCHEDULES
# ============================================================
async def create_workflow_schedule(id: str, workflow_id: str, cron_expr: str, input_template: str, next_run_at: str):
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO workflow_schedules (id, workflow_id, cron_expr, input_template, enabled, next_run_at, created_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (id, workflow_id, cron_expr, input_template, next_run_at, now)
        )
        await db.commit()
    finally:
        await db.close()


async def get_workflow_schedules(workflow_id: str = None):
    db = await get_db()
    try:
        if workflow_id:
            cursor = await db.execute(
                "SELECT * FROM workflow_schedules WHERE workflow_id = ? ORDER BY created_at DESC",
                (workflow_id,))
        else:
            cursor = await db.execute("SELECT * FROM workflow_schedules ORDER BY created_at DESC")
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


async def update_workflow_schedule(id: str, **kwargs):
    if not kwargs:
        return
    db = await get_db()
    try:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [id]
        await db.execute(f"UPDATE workflow_schedules SET {sets} WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_workflow_schedule(id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM workflow_schedules WHERE id = ?", (id,))
        await db.commit()
    finally:
        await db.close()


async def get_due_schedules():
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        cursor = await db.execute(
            "SELECT ws.*, w.name as workflow_name, w.steps as workflow_steps, w.description as workflow_description "
            "FROM workflow_schedules ws JOIN workflows w ON ws.workflow_id = w.id "
            "WHERE ws.enabled = 1 AND ws.next_run_at <= ?",
            (now,))
        schedules = []
        for r in await cursor.fetchall():
            s = dict(r)
            try:
                s["workflow_steps"] = json.loads(s["workflow_steps"])
            except (json.JSONDecodeError, TypeError):
                s["workflow_steps"] = []
            schedules.append(s)
        return schedules
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
