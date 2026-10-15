"""Durable job index maintained in the domain write transaction.

Domain controllers own execution and recovery. This index projects only their
recorded state; it never accepts work or restarts uncertain external operations.
"""
import json
from datetime import datetime, timezone

import database as db

ACTIVE = {'queued', 'running', 'cancelling'}
STATES = ACTIVE | {'succeeded', 'partial', 'failed', 'cancelled', 'interrupted'}


def _status(expression):
    return f"""CASE
        WHEN {expression} IN ('queued','pending','submitting') THEN 'queued'
        WHEN {expression} IN ('complete','completed','done','succeeded','accepted') THEN 'succeeded'
        WHEN {expression} IN ('partial','ready_for_review','waiting_for_input','blocked') THEN 'partial'
        WHEN {expression} IN ('failed','error') THEN 'failed'
        WHEN {expression} IN ('cancelled','cancelling','interrupted') THEN {expression}
        WHEN {expression} IN ('running','inspecting','baselining','planning','verifying','coding','checking',
            'reviewing','auditing','visual_review','accepting','packaging','candidate_packaging','building','fixing') THEN 'running'
        ELSE 'partial' END"""


async def migrate(conn):
    await conn.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, domain_id TEXT NOT NULL, status TEXT NOT NULL,
            title TEXT NOT NULL, phase TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, ended_at TEXT,
            details_json TEXT NOT NULL DEFAULT '{}', UNIQUE(kind,domain_id));
        CREATE INDEX IF NOT EXISTS jobs_owner_status ON jobs(user_id,status,created_at);
        CREATE TABLE IF NOT EXISTS job_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            job_id TEXT NOT NULL, event TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS job_events_owner ON job_events(user_id,seq);
        CREATE TRIGGER IF NOT EXISTS jobs_event_insert AFTER INSERT ON jobs BEGIN
            INSERT INTO job_events(user_id,job_id,event) VALUES(new.user_id,new.id,'created'); END;
        CREATE TRIGGER IF NOT EXISTS jobs_event_update AFTER UPDATE ON jobs BEGIN
            INSERT INTO job_events(user_id,job_id,event) VALUES(new.user_id,new.id,'updated'); END;
        CREATE TRIGGER IF NOT EXISTS jobs_event_delete AFTER DELETE ON jobs BEGIN
            INSERT INTO job_events(user_id,job_id,event) SELECT old.user_id,old.id,'deleted'
                WHERE EXISTS(SELECT 1 FROM users WHERE id=old.user_id); END;
    ''')
    # Columns: kind, domain_id, owner, domain state, title, error, created,
    # updated, ended, details. Views deliberately exclude prompts and credentials.
    sources = {
        'indexing_jobs': '''SELECT 'indexing',id,user_id,status,filename,error,created_at,updated_at,ended_at,
            json_object('kb_id',kb_id,'chunks',json_extract(result_json,'$.chunks')) FROM indexing_jobs''',
        'research_reports': '''SELECT 'research',id,user_id,status,COALESCE(NULLIF(title,''),query),error,
            created_at,updated_at,completed_at,json_object('report_id',id) FROM research_reports''',
        'coder_workflows': '''SELECT 'coding',w.id,c.user_id,
            CASE WHEN w.workflow_version=2 AND w.state IN ('planning','building','fixing','reviewing','accepting')
            AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.id=w.active_run_id AND r.status IN ('queued','pending','running','cancelling'))
            THEN 'interrupted' ELSE w.state END,substr(w.user_task,1,200),
            COALESCE(json_extract(w.job_json,'$.blocker'),''),w.created_at,w.updated_at,
            CASE WHEN w.state IN ('completed','cancelled','blocked','ready_for_review') THEN w.updated_at END,
            json_object('conversation_id',w.conversation_id,'workflow_version',w.workflow_version,
                'revision_id',json_extract(w.job_json,'$.revision_id'),
                'artifact_id',COALESCE(json_extract(w.job_json,'$.artifact.id'),json_extract(w.job_json,'$.candidate_artifact.id')))
            FROM coder_workflows w JOIN conversations c ON c.id=w.conversation_id''',
        'runs': '''SELECT CASE WHEN r.role='documents' THEN 'document' ELSE 'coding_run' END,
            r.id,c.user_id,r.status,r.role,COALESCE(json_extract(r.result_envelope,'$.error'),''),
            r.started_at,COALESCE(r.ended_at,r.started_at),r.ended_at,
            json_object('conversation_id',r.conversation_id,'role',r.role,'workflow_id',r.workflow_id,
                'artifact_id',json_extract(r.result_envelope,'$.artifact.id'),
                'results',json_extract(r.result_envelope,'$.results'),'completed',json_extract(r.result_envelope,'$.completed'),'total',json_extract(r.result_envelope,'$.total'))
            FROM runs r JOIN conversations c ON c.id=r.conversation_id''',
        'image_jobs': '''SELECT 'image',id,user_id,json_extract(job_json,'$.status'),'Image generation',
            COALESCE(json_extract(job_json,'$.error'),''),
            datetime(json_extract(job_json,'$.created'),'unixepoch'),
            COALESCE(json_extract(job_json,'$.updated_at'),datetime(json_extract(job_json,'$.created'),'unixepoch')),
            json_extract(job_json,'$.ended_at'),json_object('images',json_extract(job_json,'$.images'),
                'retryable',json_extract(job_json,'$.retryable')) FROM image_jobs''',
        'task_runs': '''SELECT 'scheduled',r.id,r.user_id,r.status,t.title,r.error,
            COALESCE(i.queued_at,r.started_at),COALESCE(r.finished_at,r.started_at),r.finished_at,
            json_object('task_id',r.task_id,'conversation_id',t.conversation_id,'report_id',i.child_report_id)
            FROM task_runs r JOIN scheduled_tasks t ON t.id=r.task_id LEFT JOIN task_run_inputs i ON i.run_id=r.id''',
    }
    for table, query in sources.items():
        view = 'job_projection_' + table
        await conn.execute(f'DROP VIEW IF EXISTS {view}')
        await conn.execute(f'''CREATE VIEW {view}(kind,domain_id,user_id,phase,title,error,created_at,updated_at,ended_at,details_json) AS {query}''')
        select = f'''SELECT kind||':'||domain_id,user_id,kind,domain_id,{_status('phase')},
            substr(COALESCE(title,''),1,200),phase,substr(COALESCE(error,''),1,2000),
            created_at,updated_at,ended_at,details_json FROM {view}'''
        upsert = '''ON CONFLICT(id) DO UPDATE SET status=excluded.status,phase=excluded.phase,
            title=excluded.title,error=excluded.error,updated_at=excluded.updated_at,
            ended_at=excluded.ended_at,details_json=excluded.details_json
            WHERE jobs.status IS NOT excluded.status OR jobs.phase IS NOT excluded.phase
                OR jobs.title IS NOT excluded.title OR jobs.error IS NOT excluded.error
                OR jobs.updated_at IS NOT excluded.updated_at OR jobs.ended_at IS NOT excluded.ended_at
                OR jobs.details_json IS NOT excluded.details_json'''
        for action in ('insert', 'update'):
            name = f'jobs_{table}_{action}'
            await conn.execute(f'DROP TRIGGER IF EXISTS {name}')
            await conn.execute(f'''CREATE TRIGGER {name} AFTER {action.upper()} ON {table} BEGIN
                INSERT INTO jobs {select} WHERE domain_id=new.id {upsert}; END''')
        await conn.execute(f'''CREATE TRIGGER IF NOT EXISTS jobs_{table}_delete AFTER DELETE ON {table} BEGIN
            DELETE FROM jobs WHERE id IN ({"'document:'||old.id,'coding_run:'||old.id" if table == 'runs' else "'" + {'research_reports':'research','coder_workflows':'coding','image_jobs':'image','task_runs':'scheduled','indexing_jobs':'indexing'}[table] + ":'||old.id"}); END''')
        await conn.execute(f'INSERT INTO jobs {select} WHERE true {upsert}')
        if table == 'coder_workflows':
            await conn.execute('DROP TRIGGER IF EXISTS jobs_legacy_run_update')
            await conn.execute(f'''CREATE TRIGGER jobs_legacy_run_update AFTER UPDATE ON runs BEGIN
                INSERT INTO jobs {select} WHERE domain_id IN
                (SELECT id FROM coder_workflows WHERE workflow_version=2 AND active_run_id=new.id) {upsert}; END''')
        if table == 'task_runs':
            for action in ('insert','update'):
                await conn.execute(f'DROP TRIGGER IF EXISTS jobs_task_inputs_{action}')
                await conn.execute(f'''CREATE TRIGGER jobs_task_inputs_{action} AFTER {action.upper()} ON task_run_inputs BEGIN
                    INSERT INTO jobs {select} WHERE domain_id=new.run_id {upsert}; END''')
    await conn.commit()


_actions = {}


def configure_actions(actions):
    global _actions
    _actions = actions


async def act(job_id, action, options=None):
    job = await get(job_id)
    if not job:
        raise LookupError('Job not found')
    handler = _actions.get((job['kind'], action))
    if action not in job['actions'] or handler is None:
        raise ValueError('This action is not supported in the current state')
    return await handler(job['domain_id'], options or {})


def decode(row):
    item = dict(row)
    details = json.loads(item.pop('details_json'))
    item['details'] = details
    item['progress'] = None  # Domain phases are real; invented percentages are not.
    item['actions'] = []
    kind, state = item['kind'], item['status']
    if state in ACTIVE and kind in {'research', 'document', 'scheduled', 'image', 'coding'}:
        item['actions'].append('cancel')
    if kind == 'coding' and details.get('workflow_version') == 3 and state in {'partial', 'cancelled'}:
        item['actions'].append('resume')
    if kind == 'image' and details.get('retryable'):
        item['actions'].append('retry')
    if kind == 'indexing' and state in {'failed','interrupted','partial'}:
        item['actions'].append('retry')
    if kind == 'research' and state in {'failed', 'interrupted', 'partial', 'cancelled'}:
        item['actions'].append('rerun')
    if kind == 'coding' and details.get('workflow_version') != 3:
        item['actions'] = []  # Legacy controller has no safe generic continuation.
    def parse(raw):
        return datetime.fromisoformat(raw.replace('Z','+00:00')).replace(tzinfo=timezone.utc)
    try:
        end = parse(item['ended_at'] or item['updated_at']) if state not in ACTIVE else datetime.now(timezone.utc)
        item['elapsed_seconds'] = max(0, int((end - parse(item['created_at'])).total_seconds()))
    except (ValueError, TypeError):
        item['elapsed_seconds'] = None
    return item


async def _progress(conn, item):
    if item['kind']=='document' and item['details'].get('total'):
        item['progress']=min(100,100*int(item['details'].get('completed') or 0)/int(item['details']['total']))
    if item['kind'] == 'research':
        if item['status'] in {'failed','interrupted','partial','cancelled'}:
            saved=await conn.execute_fetchall('SELECT id FROM research_checkpoints WHERE report_id=? LIMIT 1',(item['domain_id'],))
            if saved:item['actions'].append('resume')
        rows = await conn.execute_fetchall("SELECT payload FROM research_events WHERE report_id=? AND json_extract(payload,'$.type')='research_phase' ORDER BY seq DESC LIMIT 1", (item['domain_id'],))
        if rows:
            data = json.loads(rows[0]['payload']).get('data', {})
            item['phase'] = data.get('label') or item['phase']
            pct = data.get('pct')
            if isinstance(pct, (int,float)) and 0 <= pct <= 100:
                item['progress'] = pct
    return item


async def list_jobs(*, kind='', status='', limit=100, offset=0):
    conn = await db.get_db()
    try:
        where, args = 'user_id=?', [db.current_user_id()]
        if kind: where += ' AND kind=?'; args.append(kind)
        if status == 'active': where += " AND status IN ('queued','running','cancelling')"
        elif status: where += ' AND status=?'; args.append(status)
        rows = await conn.execute_fetchall(f'SELECT * FROM jobs WHERE {where} ORDER BY created_at DESC,id LIMIT ? OFFSET ?',
            (*args, max(1,min(limit,200)), max(0,offset)))
        cursor = await conn.execute_fetchall('SELECT COALESCE(MAX(seq),0) n FROM job_events WHERE user_id=?', (db.current_user_id(),))
        return {'jobs': [await _progress(conn,decode(r)) for r in rows], 'cursor': cursor[0]['n']}
    finally:
        await conn.close()


async def get(job_id):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall('SELECT * FROM jobs WHERE id=? AND user_id=?', (job_id,db.current_user_id()))
        return await _progress(conn,decode(rows[0])) if rows else None
    finally:
        await conn.close()


async def changes(after):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall('SELECT seq,job_id,event FROM job_events WHERE user_id=? AND seq>? ORDER BY seq LIMIT 100',
            (db.current_user_id(),max(0,after)))
        return [dict(r) for r in rows]
    finally:
        await conn.close()
