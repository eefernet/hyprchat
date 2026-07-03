"""SQLite schema bootstrap SQL for HyprChat."""

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    password_hash TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    title TEXT NOT NULL DEFAULT 'New Chat',
    model TEXT DEFAULT '',
    system_prompt TEXT DEFAULT '',
    model_config_id TEXT,
    tool_ids TEXT DEFAULT '[]',
    persona_name TEXT DEFAULT '',
    persona_avatar TEXT DEFAULT '',
    use_memories TEXT DEFAULT '0',
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
    user_id TEXT NOT NULL DEFAULT 'default',
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
    user_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    filename TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    transport TEXT NOT NULL DEFAULT 'stdio',
    command TEXT DEFAULT '',
    url TEXT DEFAULT '',
    args_json TEXT DEFAULT '[]',
    env_json TEXT DEFAULT '{}',
    headers_json TEXT DEFAULT '{}',
    allow_private INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    health TEXT DEFAULT 'unknown',
    last_error TEXT DEFAULT '',
    discovered_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS openapi_connectors (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    spec_url TEXT DEFAULT '',
    spec_json TEXT DEFAULT '',
    base_url TEXT DEFAULT '',
    auth_json TEXT DEFAULT '{}',
    headers_json TEXT DEFAULT '{}',
    allow_private INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    health TEXT DEFAULT 'unknown',
    last_error TEXT DEFAULT '',
    discovered_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS connector_tools (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    connector_type TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    external_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT DEFAULT '',
    input_schema_json TEXT DEFAULT '{}',
    metadata_json TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, connector_type, connector_id, external_name),
    UNIQUE(user_id, tool_name)
);

CREATE TABLE IF NOT EXISTS model_configs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    base_model TEXT NOT NULL,
    system_prompt TEXT DEFAULT '',
    tool_ids TEXT DEFAULT '[]',
    kb_ids TEXT DEFAULT '[]',
    parameters TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS model_provider_credentials (
    user_id TEXT NOT NULL DEFAULT 'default',
    provider TEXT NOT NULL,
    api_key TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    last_test_status TEXT DEFAULT '',
    last_test_error TEXT DEFAULT '',
    last_test_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv_created_id ON messages(conversation_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_kb_files_kb ON kb_files(kb_id);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
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

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
    message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    workflow_id TEXT REFERENCES coder_workflows(id) ON DELETE SET NULL,
    filename TEXT NOT NULL,
    url TEXT NOT NULL,
    kind TEXT DEFAULT 'file',
    mime_type TEXT DEFAULT '',
    title TEXT DEFAULT '',
    description TEXT DEFAULT '',
    storage_path TEXT DEFAULT '',
    size_bytes INTEGER DEFAULT 0,
    sha256 TEXT DEFAULT '',
    exists_status TEXT DEFAULT 'unknown',
    last_checked_at TIMESTAMP,
    status TEXT DEFAULT 'draft',
    pinned INTEGER DEFAULT 0,
    parent_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    supersedes_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    content_text TEXT DEFAULT '',
    content_indexed_at TIMESTAMP,
    metadata_json TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_artifacts_user_created ON artifacts(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_conv ON artifacts(conversation_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_workspace ON artifacts(workspace_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_workflow ON artifacts(workflow_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind);

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

CREATE TABLE IF NOT EXISTS council_configs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
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
    model_config_id TEXT DEFAULT NULL,
    system_prompt TEXT DEFAULT '',
    persona_name TEXT DEFAULT '',
    points INTEGER DEFAULT 0,
    FOREIGN KEY (council_id) REFERENCES council_configs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
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
    user_id TEXT NOT NULL DEFAULT 'default',
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
    user_id TEXT NOT NULL DEFAULT 'default',
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
    user_id         TEXT NOT NULL DEFAULT 'default',
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

-- Append-only event logs for runs and research reports. Replaces the old
-- per-event read-modify-rewrite of a JSON array column (O(n^2) write
-- amplification). The legacy events_log columns stay as a read-fallback for
-- rows written before this migration.
CREATE TABLE IF NOT EXISTS run_events (
    run_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    ts      TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, seq);

CREATE TABLE IF NOT EXISTS research_events (
    report_id TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    ts        TEXT,
    payload   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (report_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_research_events_report ON research_events(report_id, seq);

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
