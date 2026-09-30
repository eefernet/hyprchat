"""Transactional version-3 workflow storage on the existing SQLite database."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import database as db


TERMINAL = {"completed", "cancelled"}
PARKED = {"blocked", "waiting_for_input", "ready_for_review"}
ACTIVE = {"queued", "inspecting", "baselining", "planning", "verifying", "coding", "checking", "reviewing", "auditing", "visual_review", "accepting", "packaging", "candidate_packaging", "cancelling"}


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


async def migrate(connection=None):
    owned = connection is None
    connection = connection or await db.get_db()
    try:
        for table, columns in {
            "coder_workflows": {"workflow_version": "INTEGER NOT NULL DEFAULT 2", "job_json": "TEXT NOT NULL DEFAULT '{}'"},
            "runs": {"workflow_id": "TEXT DEFAULT ''", "milestone_id": "TEXT DEFAULT ''", "revision_id": "TEXT DEFAULT ''"},
            "conversations": {"active_coding_project_id": "TEXT DEFAULT ''"},
            "coding_projects": {"accepted_job_id": "TEXT DEFAULT ''", "accepted_revision_id": "TEXT DEFAULT ''", "accepted_artifact_id": "TEXT DEFAULT ''"},
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
        # Materialize the accepted head for projects published before policy v2.
        await connection.execute("""UPDATE coding_projects SET
            accepted_job_id=openhands_project_id,
            accepted_revision_id=COALESCE((SELECT json_extract(job_json,'$.revision_id') FROM coder_workflows WHERE id=openhands_project_id),''),
            accepted_artifact_id=COALESCE((SELECT json_extract(job_json,'$.artifact.id') FROM coder_workflows WHERE id=openhands_project_id),'')
            WHERE COALESCE(accepted_job_id,'')='' AND EXISTS
            (SELECT 1 FROM coder_workflows WHERE id=openhands_project_id AND workflow_version=3 AND state='completed'
             AND json_extract(job_json,'$.acceptance.accepted')=1)""")
        await connection.commit()
    finally:
        if owned:
            await connection.close()


def _decode(row):
    if not row:
        return None
    result = dict(row)
    result.update(json.loads(result.pop("job_json", "{}") or "{}"))
    from coder_presentation import presentation
    result['presentation'] = presentation(result)
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
                 source_job_id="", source_revision_id="", source_project_id="", policy_version=1,
                 visual_policy=None, request_options=None, parent_artifact_id="", inherited=None):
    owner = db.current_user_id()
    identity = "cw3-" + hashlib.sha256(f"{owner}:{conversation_id}:{key}".encode()).hexdigest()[:24]
    body = dict(task=task, mode=mode, project_id=project_id, model=model)
    if request_options:
        body["options"] = request_options
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
        if policy_version >= 2:
            if mode == "build_from_prompt":
                if project_id:
                    raise ValueError("A new build cannot inherit an existing project")
                project_id = "cp-" + identity.removeprefix("cw3-")
                await connection.execute(
                    "INSERT INTO coding_projects(id,user_id,name,description,conversation_id,openhands_project_id,created_at,updated_at) VALUES(?,?,?,?,?,'',?,?)",
                    (project_id, owner, task.strip().splitlines()[0][:100], task, conversation_id, now(), now()))
            else:
                projects = await connection.execute_fetchall("SELECT * FROM coding_projects WHERE id=? AND user_id=?", (project_id, owner))
                if not projects:
                    raise LookupError("Project not found")
                head = projects[0]["accepted_revision_id"] or ""
                if head and head != source_revision_id:
                    raise ValueError("Project changed while the request was starting; retry using its current revision")
            if mode != "ask_uploaded_project":
                await _assert_project_idle(connection, project_id)
            await connection.execute("UPDATE conversations SET active_coding_project_id=? WHERE id=? AND user_id=?", (project_id, conversation_id, owner))
        job = {"model": model, "request_fingerprint": fingerprint, "milestones": [],
               "source_job_id":source_job_id, "source_revision_id":source_revision_id, "source_project_id":source_project_id,
               "milestone_index": 0, "calls_used": 0, "seconds_used": 0,
               "allowance": 1, "revision_id": "", "baseline_revision": "", "worker_operation": "",
               "operation_sequence": 0, "blocker": "", "checks": [], "artifact": None}
        if policy_version >= 2:
            job.update(policy_version=policy_version, visual_policy=visual_policy or {"enabled":False,"model":""},
                       parent_artifact_id=parent_artifact_id, requirements=[], visual_review={"status":"pending"})
        if policy_version >= 6:
            job.update(candidate_artifact=None, delivery_status='building', verification_summary=None,
                repair_round=0, check_corrections=0, recovery_used=False,
                execution_commands=(request_options or {}).get('execution_commands', {}),
                protected_files=(request_options or {}).get('protected_files', []))
        if policy_version >= 7:
            job['inherited'] = inherited or []
        await connection.execute(
            "INSERT INTO coder_workflows(id,conversation_id,project_id,mode,state,user_task,workflow_version,job_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,3,?,?,?)", (identity, conversation_id, project_id, mode, "queued", task, json.dumps(job), now(), now()))
        await _event(connection, identity, "created", {"state": "queued"})
        await connection.commit()
    finally:
        await connection.close()
    return await get(identity)


async def _assert_project_idle(connection, project_id, except_job=""):
    rows = await connection.execute_fetchall(
        "SELECT id,state FROM coder_workflows WHERE project_id=? AND id<>? AND mode<>'ask_uploaded_project' "
        "AND state IN (" + ",".join("?" for _ in ACTIVE) + ") LIMIT 1", (project_id, except_job, *sorted(ACTIVE)))
    if rows:
        raise ValueError(f"Project already has an active job ({rows[0]['id']}); wait or stop it before starting another edit")


async def project_history(project_id):
    if not await db.get_coding_project(project_id):
        raise LookupError("Project not found")
    connection = await db.get_db()
    try:
        rows = await connection.execute_fetchall(
            "SELECT w.* FROM coder_workflows w JOIN conversations c ON c.id=w.conversation_id "
            "WHERE w.project_id=? AND c.user_id=? AND w.workflow_version=3 ORDER BY w.created_at DESC", (project_id,db.current_user_id()))
        return [_decode(row) for row in rows]
    finally:
        await connection.close()


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
        if row.get("cancel_requested") and state not in (None, "cancelling", "cancelled") and event != 'resume':
            raise InterruptedError("Cancellation requested")
        payload = json.loads(row["job_json"])
        if event == 'resume' and payload.get('policy_version', 1) >= 6:
            from coder_policy7_evidence import DURABLE_LIMITS
            if payload.get('policy_version', 1) >= 7 and payload.get('stop_limit') in DURABLE_LIMITS:
                raise ValueError(payload.get('blocker') or 'Retained repair limits prevent continuation')
            if row['state'] not in ({'blocked', 'waiting_for_input', 'ready_for_review', 'cancelled'} if payload.get('policy_version', 1) >= 7 else {'blocked', 'waiting_for_input', 'ready_for_review'}):
                raise ValueError('Workflow is already active or terminal')
            if payload.get('candidate_artifact'):
                candidate_revision = payload['candidate_artifact'].get('metadata', {}).get('revision_id')
                if changes.get('candidate_revision') != candidate_revision or payload.get('revision_id') != candidate_revision:
                    raise ValueError('Candidate revision changed before continuation')
        if event == "resume" and payload.get("policy_version", 1) >= 2:
            await _assert_project_idle(connection, row["project_id"], workflow_id)
            heads = await connection.execute_fetchall("SELECT accepted_revision_id FROM coding_projects WHERE id=? AND user_id=?", (row["project_id"], db.current_user_id()))
            if not heads or (heads[0][0] or "") != payload.get("source_revision_id", ""):
                raise ValueError("The project has a newer accepted revision; start a new edit instead of resuming stale work")
        if state and state != row['state'] or changes.get('activity') and changes['activity'] != payload.get('activity'):
            changes['meaningful_update_at'] = now()
        payload.update(changes)
        new_state = state or row["state"]
        artifact_status = "delivered" if new_state == "completed" and payload.get("artifact") else "not_ready"
        await connection.execute("UPDATE coder_workflows SET state=?,job_json=?,updated_at=?,artifact_status=? WHERE id=?",
                                 (new_state, json.dumps(payload), now(), artifact_status, workflow_id))
        if event == 'resume':
            await connection.execute('UPDATE coder_workflows SET cancel_requested=0 WHERE id=?', (workflow_id,))
        await _event(connection, workflow_id, event, {"state": new_state, **changes})
        await connection.commit()
    finally:
        await connection.close()
    return await get(workflow_id)


async def request_cancel(workflow_id):
    connection = await db.get_db()
    try:
        await connection.execute("BEGIN IMMEDIATE")
        rows = await connection.execute_fetchall('SELECT state,job_json FROM coder_workflows WHERE id=? AND workflow_version=3 AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)', (workflow_id, db.current_user_id()))
        previous = rows[0]['state'] if rows else ''
        if rows and previous not in {'cancelling', 'cancelled', 'completed'}:
            payload = json.loads(rows[0]['job_json'] or '{}')
            # Continue returns to a STAGE. A parked job keeps the stage it was parked with, and a job
            # stopped while queued for Continue keeps the stage that Continue was heading for; writing
            # 'ready_for_review' or 'queued' here made every later Continue park or re-plan again.
            if previous in {'queued', 'inspecting'}:
                stage = payload.get('resume_after_inspect') or previous
            else:
                stage = payload.get('resume_state') or previous if previous in PARKED else previous
            payload['resume_state'] = stage
            await connection.execute('UPDATE coder_workflows SET job_json=? WHERE id=?', (json.dumps(payload), workflow_id))
        cursor = await connection.execute(
            "UPDATE coder_workflows SET cancel_requested=1,state='cancelling',updated_at=? WHERE id=? AND workflow_version=3 "
            "AND state NOT IN ('completed','cancelled') AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)",
            (now(), workflow_id, db.current_user_id()))
        # A cancel that is still waiting for its worker is polled; only the transition is an event.
        if cursor.rowcount and previous != 'cancelling':
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
            "WHERE w.workflow_version=3 AND w.state NOT IN ('completed','cancelled','blocked','waiting_for_input','ready_for_review') "
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
        if job.get('policy_version', 1) >= 7:
            summary = job.get('verification_summary') or {}
            if summary.get('evidence_version') != 3 or summary.get('revision_id') != revision_id or not summary.get('accepted'):
                raise ValueError('Policy 7 delivery requires current trusted verification evidence')
            if any(c.get('status') != 'passed' for c in summary.get('criteria', [])):
                raise ValueError('Unverified behavior cannot advance the accepted project head')
        if job.get("policy_version",1) >= 2:
            heads = await connection.execute_fetchall("SELECT accepted_revision_id FROM coding_projects WHERE id=? AND user_id=?", (job["project_id"], db.current_user_id()))
            if not heads or (heads[0][0] or "") != job.get("source_revision_id", ""):
                raise ValueError("Project head changed; this revision cannot replace newer accepted work")
        artifact = await db.add_artifact(_connection=connection, conversation_id=job["conversation_id"], workflow_id=workflow_id, **artifact_fields)
        if not artifact:
            raise ValueError("Artifact registration failed")
        payload = json.loads(rows[0]["job_json"])
        payload["artifact"] = artifact
        if job.get('policy_version', 1) >= 6:
            payload['delivery_status'] = 'accepted'
        project_id = job["project_id"] or workflow_id
        prior = await connection.execute_fetchall("SELECT * FROM coding_projects WHERE id=? AND user_id=?", (project_id,db.current_user_id()))
        if prior:
            await connection.execute("UPDATE coding_projects SET openhands_project_id=?,accepted_job_id=?,accepted_revision_id=?,accepted_artifact_id=?,updated_at=? WHERE id=? AND user_id=?",
                                     (workflow_id,workflow_id,revision_id,artifact["id"],now(),project_id,db.current_user_id()))
        else:
            await connection.execute("INSERT INTO coding_projects(id,user_id,name,description,language,file_manifest,last_plan,conversation_id,openhands_project_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                     (project_id,db.current_user_id(),"Daedalus project",job["user_task"],"","[]","",job["conversation_id"],workflow_id,now(),now()))
            await connection.execute("UPDATE coding_projects SET accepted_job_id=?,accepted_revision_id=?,accepted_artifact_id=? WHERE id=?", (workflow_id,revision_id,artifact["id"],project_id))
        await connection.execute("UPDATE conversations SET active_coding_project_id=? WHERE id=? AND user_id=?", (project_id,job["conversation_id"],db.current_user_id()))
        await connection.execute("UPDATE coder_workflows SET state='completed',project_id=?,job_json=?,artifact_status='delivered',updated_at=? WHERE id=?",
                                 (project_id,json.dumps(payload),now(),workflow_id))
        await _event(connection,workflow_id,"delivered",{"state":"completed","artifact":artifact,"project_id":project_id,"artifact_status":"delivered"})
        await connection.commit()
        return artifact
    finally:
        await connection.close()


async def finish_candidate(workflow_id, revision_id, **artifact_fields):
    """Publish a review candidate without changing any accepted project head."""
    connection = await db.get_db()
    try:
        await connection.execute('BEGIN IMMEDIATE')
        rows = await connection.execute_fetchall(
            'SELECT w.* FROM coder_workflows w JOIN conversations c ON c.id=w.conversation_id '
            'WHERE w.id=? AND c.user_id=? AND w.workflow_version=3', (workflow_id, db.current_user_id()))
        if not rows:
            raise LookupError('Workflow not found')
        job = _decode(rows[0])
        if job.get('cancel_requested'):
            raise InterruptedError('Cancellation requested before candidate publication')
        if job['state'] == 'ready_for_review' and job['revision_id'] == revision_id:
            return job['candidate_artifact']
        if job['state'] != 'candidate_packaging' or job['revision_id'] != revision_id or job.get('policy_version', 1) < 6:
            raise ValueError('Candidate must match the current immutable revision')
        summary = job.get('verification_summary') or {}
        if summary.get('revision_id') != revision_id or summary.get('accepted'):
            raise ValueError('Candidate needs an unaccepted verification summary for this revision')
        heads = await connection.execute_fetchall('SELECT accepted_revision_id FROM coding_projects WHERE id=? AND user_id=?',
                                                  (job['project_id'], db.current_user_id()))
        if not heads or (heads[0][0] or '') != job.get('source_revision_id', ''):
            raise ValueError('Project head changed; this candidate is stale')
        artifact = await db.add_artifact(_connection=connection, conversation_id=job['conversation_id'], workflow_id=workflow_id, **artifact_fields)
        if not artifact:
            raise ValueError('Candidate registration failed')
        payload = json.loads(rows[0]['job_json'])
        payload.update(candidate_artifact=artifact, delivery_status='review_candidate')
        await connection.execute("UPDATE coder_workflows SET state='ready_for_review',job_json=?,artifact_status='not_ready',updated_at=? WHERE id=?",
                                 (json.dumps(payload), now(), workflow_id))
        await _event(connection, workflow_id, 'candidate_delivered', {'state':'ready_for_review', 'candidate_artifact':artifact,
                                                                   'delivery_status':'review_candidate'})
        await connection.commit()
        return artifact
    finally:
        await connection.close()


async def account_operation(workflow_id, operation_id, kind, operation):
    """Cancel and normal completion may race; final worker usage is charged once."""
    if operation['status'] in {'queued', 'starting', 'running', 'cancelling'}:
        raise ValueError('Usage requires terminal worker acknowledgement')
    connection = await db.get_db()
    try:
        await connection.execute('BEGIN IMMEDIATE')
        rows = await connection.execute_fetchall(
            'SELECT w.job_json FROM coder_workflows w JOIN conversations c ON c.id=w.conversation_id '
            'WHERE w.id=? AND c.user_id=? AND w.workflow_version=3', (workflow_id, db.current_user_id()))
        if not rows:
            raise LookupError('Workflow not found')
        payload = json.loads(rows[0]['job_json'])
        if payload.get('worker_operation') == operation_id and not payload.get('operation_accounted'):
            begun = operation.get('started') or operation.get('ended') or 0
            elapsed = max(0, (operation.get('ended') or begun) - begun)
            usage = [r for r in payload.get('stage_usage', []) if r['operation_id'] != operation_id]
            usage.append({'operation_id': operation_id, 'stage': kind, 'seconds': elapsed,
                          'calls': operation.get('calls', 0), 'status': operation['status']})
            payload.update(calls_used=payload.get('calls_used', 0) + operation.get('calls', 0),
                seconds_used=payload.get('seconds_used', 0) + elapsed, operation_accounted=True,
                last_operation_status=operation['status'], operation_calls=0, operation_seconds=0, stage_usage=usage)
            await connection.execute('UPDATE coder_workflows SET job_json=?,updated_at=? WHERE id=?',
                                     (json.dumps(payload), now(), workflow_id))
            await _event(connection, workflow_id, 'operation_accounted', {'operation_id': operation_id, 'calls': operation.get('calls', 0)})
        await connection.commit()
    finally:
        await connection.close()
    return await get(workflow_id)
