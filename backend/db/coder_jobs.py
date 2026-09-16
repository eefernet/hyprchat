"""Transactional version-3 workflow storage on the existing SQLite database."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import database as db


TERMINAL = {"completed", "cancelled"}
ACTIVE = {"queued", "inspecting", "baselining", "planning", "coding", "checking", "accepting", "packaging", "cancelling"}


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


async def migrate(connection=None):
    owned = connection is None
    connection = connection or await db.get_db()
    try:
        for table, columns in {
            "coder_workflows": {"workflow_version": "INTEGER NOT NULL DEFAULT 2", "job_json": "TEXT NOT NULL DEFAULT '{}'"},
            "runs": {"workflow_id": "TEXT DEFAULT ''", "milestone_id": "TEXT DEFAULT ''", "revision_id": "TEXT DEFAULT ''"},
        }.items():
            existing = {r[1] for r in await connection.execute_fetchall(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    await connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        await connection.executescript("""
            CREATE TABLE IF NOT EXISTS coder_workflow_events (
                workflow_id TEXT NOT NULL REFERENCES coder_workflows(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(workflow_id,seq));
            CREATE TABLE IF NOT EXISTS coder_revisions (
                workflow_id TEXT NOT NULL REFERENCES coder_workflows(id) ON DELETE CASCADE,
                revision_id TEXT NOT NULL, tree_hash TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(workflow_id,revision_id));
            CREATE TABLE IF NOT EXISTS coder_checks (
                workflow_id TEXT NOT NULL REFERENCES coder_workflows(id) ON DELETE CASCADE,
                operation_id TEXT NOT NULL, revision_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(workflow_id,operation_id));
            CREATE INDEX IF NOT EXISTS runs_workflow ON runs(workflow_id);
        """)
        await connection.commit()
    finally:
        if owned:
            await connection.close()


def _decode(row):
    if not row:
        return None
    result = dict(row)
    result.update(json.loads(result.pop("job_json", "{}") or "{}"))
    return result


async def get(workflow_id):
    connection = await db.get_db()
    try:
        rows = await connection.execute_fetchall(
            "SELECT w.*, COALESCE((SELECT MAX(seq) FROM coder_workflow_events WHERE workflow_id=w.id),0) event_sequence FROM coder_workflows w JOIN conversations c ON c.id=w.conversation_id "
            "WHERE w.id=? AND c.user_id=? AND w.workflow_version=3", (workflow_id, db.current_user_id()))
        return _decode(rows[0]) if rows else None
    finally:
        await connection.close()


async def create(conversation_id, task, mode, project_id, model, key, *,
                 source_job_id="", source_revision_id="", source_project_id=""):
    owner = db.current_user_id()
    identity = "cw3-" + hashlib.sha256(f"{owner}:{conversation_id}:{key}".encode()).hexdigest()[:24]
    body = dict(task=task, mode=mode, project_id=project_id, model=model)
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    connection = await db.get_db()
    try:
        await connection.execute("BEGIN IMMEDIATE")
        conv = await connection.execute_fetchall("SELECT id FROM conversations WHERE id=? AND user_id=?", (conversation_id, owner))
        if not conv:
            raise LookupError("Conversation not found")
        rows = await connection.execute_fetchall("SELECT * FROM coder_workflows WHERE id=?", (identity,))
        if rows:
            result = _decode(rows[0])
            if result.get("request_fingerprint") != fingerprint:
                raise ValueError("Idempotency key was already used for another request")
            return result
        job = {"model": model, "request_fingerprint": fingerprint, "milestones": [],
               "source_job_id":source_job_id, "source_revision_id":source_revision_id, "source_project_id":source_project_id,
               "milestone_index": 0, "calls_used": 0, "seconds_used": 0,
               "allowance": 1, "revision_id": "", "baseline_revision": "", "worker_operation": "",
               "operation_sequence": 0, "blocker": "", "checks": [], "artifact": None}
        await connection.execute(
            "INSERT INTO coder_workflows(id,conversation_id,project_id,mode,state,user_task,workflow_version,job_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,3,?,?,?)", (identity, conversation_id, project_id, mode, "queued", task, json.dumps(job), now(), now()))
        await _event(connection, identity, "created", {"state": "queued"})
        await connection.commit()
    finally:
        await connection.close()
    return await get(identity)


async def _event(connection, workflow_id, kind, data):
    await connection.execute(
        "INSERT INTO coder_workflow_events VALUES(?,COALESCE((SELECT MAX(seq)+1 FROM coder_workflow_events WHERE workflow_id=?),1),?,?)",
        (workflow_id, workflow_id, json.dumps({"type": kind, "data": data}), now()))


async def save(workflow_id, *, state=None, event="updated", **changes):
    connection = await db.get_db()
    try:
        await connection.execute("BEGIN IMMEDIATE")
        rows = await connection.execute_fetchall(
            "SELECT w.* FROM coder_workflows w JOIN conversations c ON c.id=w.conversation_id WHERE w.id=? AND c.user_id=? AND w.workflow_version=3",
            (workflow_id, db.current_user_id()))
        if not rows:
            raise LookupError("Workflow not found")
        row = dict(rows[0])
        if row["state"] in TERMINAL and event != "resume":
            raise ValueError("Workflow is terminal")
        if row.get("cancel_requested") and state not in (None, "cancelling", "cancelled"):
            raise InterruptedError("Cancellation requested")
        payload = json.loads(row["job_json"])
        payload.update(changes)
        new_state = state or row["state"]
        artifact_status = "delivered" if new_state == "completed" and payload.get("artifact") else "not_ready"
        await connection.execute("UPDATE coder_workflows SET state=?,job_json=?,updated_at=?,artifact_status=? WHERE id=?",
                                 (new_state, json.dumps(payload), now(), artifact_status, workflow_id))
        await _event(connection, workflow_id, event, {"state": new_state, **changes})
        await connection.commit()
    finally:
        await connection.close()
    return await get(workflow_id)


async def request_cancel(workflow_id):
    connection = await db.get_db()
    try:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            "UPDATE coder_workflows SET cancel_requested=1,state='cancelling',updated_at=? WHERE id=? AND workflow_version=3 "
            "AND state NOT IN ('completed','cancelled') AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)",
            (now(), workflow_id, db.current_user_id()))
        if cursor.rowcount:
            await _event(connection, workflow_id, "cancelling", {"state":"cancelling", "cancel_requested":1})
        await connection.commit()
    finally:
        await connection.close()
    return await get(workflow_id)


async def events(workflow_id, after=0, limit=100):
    if not await get(workflow_id):
        raise LookupError("Workflow not found")
    connection = await db.get_db()
    try:
        rows = await connection.execute_fetchall("SELECT * FROM coder_workflow_events WHERE workflow_id=? AND seq>? ORDER BY seq LIMIT ?",
                                                 (workflow_id, after, limit))
        return [{"seq": r["seq"], "created_at": r["created_at"], **json.loads(r["payload"])} for r in rows]
    finally:
        await connection.close()


async def record_revision(workflow_id, revision):
    if not await get(workflow_id):
        raise LookupError("Workflow not found")
    connection = await db.get_db()
    try:
        await connection.execute("INSERT OR IGNORE INTO coder_revisions VALUES(?,?,?,?)",
                                 (workflow_id, revision["revision"], revision["tree"], json.dumps(revision)))
        await connection.commit()
    finally:
        await connection.close()


async def record_check(workflow_id, operation_id, revision_id, result):
    if not await get(workflow_id):
        raise LookupError("Workflow not found")
    connection = await db.get_db()
    try:
        await connection.execute("INSERT OR REPLACE INTO coder_checks VALUES(?,?,?,?)",
                                 (workflow_id, operation_id, revision_id, json.dumps(result)))
        await connection.commit()
    finally:
        await connection.close()


async def recoverable():
    """Startup-only unscoped enumeration; runner re-enters each owner's scope."""
    connection = await db.get_db()
    try:
        rows = await connection.execute_fetchall(
            "SELECT w.id,c.user_id FROM coder_workflows w JOIN conversations c ON c.id=w.conversation_id "
            "WHERE w.workflow_version=3 AND w.state NOT IN ('completed','cancelled','blocked','waiting_for_input') "
            "ORDER BY CASE WHEN w.state='queued' THEN 1 ELSE 0 END,w.created_at")
        return [dict(row) for row in rows]
    finally:
        await connection.close()


async def start_attempt(job, operation_id, kind):
    """Idempotent attempt identity is shared with the durable worker operation."""
    connection = await db.get_db()
    try:
        await connection.execute("BEGIN IMMEDIATE")
        await connection.execute(
            "INSERT OR IGNORE INTO runs(id,conversation_id,role,status,project_id,started_at,result_envelope,events_log,workflow_id,milestone_id,revision_id) "
            "SELECT ?,conversation_id,?,'running',project_id,?,'{}','[]',id,?,? FROM coder_workflows WHERE id=? "
            "AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)",
            (operation_id, {"plan":"architect","code":"builder","check":"reviewer","accept":"acceptance","qa":"qa"}.get(kind,kind),
             now(), str(job.get("milestone_index",0)), job.get("revision_id",""), job["id"], db.current_user_id()))
        await connection.execute("UPDATE coder_workflows SET active_run_id=? WHERE id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)",
                                 (operation_id, job["id"], db.current_user_id()))
        await connection.commit()
    finally:
        await connection.close()


async def finish_artifact(workflow_id, revision_id, **artifact_fields):
    """Publish the artifact and completed job together, fenced against Stop."""
    connection = await db.get_db()
    try:
        await connection.execute("BEGIN IMMEDIATE")
        rows = await connection.execute_fetchall(
            "SELECT w.* FROM coder_workflows w JOIN conversations c ON c.id=w.conversation_id WHERE w.id=? AND c.user_id=? AND w.workflow_version=3",
            (workflow_id, db.current_user_id()))
        if not rows:
            raise LookupError("Workflow not found")
        job = _decode(rows[0])
        if job["state"] == "completed":
            return job["artifact"]
        if job["cancel_requested"]:
            raise InterruptedError("Cancellation requested before artifact publication")
        acceptance = job.get("acceptance") or {}
        if job["state"] != "packaging" or job["revision_id"] != revision_id or acceptance.get("revision_id") != revision_id or acceptance.get("accepted") is not True:
            raise ValueError("Delivery requires Acceptance for the current revision")
        artifact = await db.add_artifact(_connection=connection, conversation_id=job["conversation_id"], workflow_id=workflow_id, **artifact_fields)
        if not artifact:
            raise ValueError("Artifact registration failed")
        payload = json.loads(rows[0]["job_json"])
        payload["artifact"] = artifact
        project_id = job["project_id"] or workflow_id
        prior = await connection.execute_fetchall("SELECT * FROM coding_projects WHERE id=? AND user_id=?", (project_id,db.current_user_id()))
        if prior:
            await connection.execute("UPDATE coding_projects SET openhands_project_id=?,conversation_id=?,updated_at=? WHERE id=? AND user_id=?",
                                     (workflow_id,job["conversation_id"],now(),project_id,db.current_user_id()))
        else:
            await connection.execute("INSERT INTO coding_projects(id,user_id,name,description,language,file_manifest,last_plan,conversation_id,openhands_project_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                     (project_id,db.current_user_id(),"Daedalus project",job["user_task"],"","[]","",job["conversation_id"],workflow_id,now(),now()))
        await connection.execute("UPDATE coder_workflows SET state='completed',project_id=?,job_json=?,artifact_status='delivered',updated_at=? WHERE id=?",
                                 (project_id,json.dumps(payload),now(),workflow_id))
        await _event(connection,workflow_id,"delivered",{"state":"completed","artifact":artifact})
        await connection.commit()
        return artifact
    finally:
        await connection.close()
