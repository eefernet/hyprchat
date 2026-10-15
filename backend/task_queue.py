"""Transactional scheduler queue. The scheduler remains the execution owner."""
import json
import uuid
from datetime import datetime

import database as db

TASK_FIELDS = ('id', 'user_id', 'title', 'prompt', 'task_type', 'model',
               'tool_ids', 'delivery_json', 'conversation_id')


async def enqueue(task, *, trigger, extra_context='', advance=None):
    """Persist before dispatch; advance a due schedule in the same transaction.

    A stale due-task selection cannot enqueue twice. Recurring overlaps coalesce
    into the existing run, while its schedule still advances.
    """
    owner = task.get('user_id') or db.current_user_id()
    conn = await db.get_db()
    try:
        await conn.execute('BEGIN IMMEDIATE')
        rows = await conn.execute_fetchall(
            'SELECT * FROM scheduled_tasks WHERE id=? AND user_id=?', (task['id'], owner))
        if not rows:
            raise LookupError('Task not found')
        current = dict(rows[0])
        if advance is not None:
            if not current['enabled'] or current['next_run'] != task.get('next_run'):
                return None
            await conn.execute('UPDATE scheduled_tasks SET next_run=?,enabled=?,updated_at=? WHERE id=?',
                (advance['next_run'], advance.get('enabled', current['enabled']),
                 datetime.utcnow().isoformat(), task['id']))
        active = await conn.execute_fetchall(
            "SELECT id FROM task_runs WHERE task_id=? AND user_id=? AND status IN ('queued','running','cancelling') ORDER BY started_at LIMIT 1",
            (task['id'], owner))
        if active:
            await conn.commit()
            return active[0]['id']
        run_id = 'taskrun-' + uuid.uuid4().hex
        snapshot = {key: task.get(key) for key in TASK_FIELDS}
        snapshot['user_id'] = owner
        now = datetime.utcnow().isoformat()
        await conn.execute('INSERT INTO task_runs(id,task_id,user_id,status,trigger,started_at) VALUES(?,?,?,\'queued\',?,?)',
                           (run_id, task['id'], owner, trigger, now))
        await conn.execute('INSERT INTO task_run_inputs(run_id,task_json,extra_context,queued_at) VALUES(?,?,?,?)',
                           (run_id, json.dumps(snapshot), extra_context[:32768], now))
        await conn.commit()
        return run_id
    finally:
        await conn.close()


async def pending(run_id=None):
    """Cross-owner recovery; execution must set the persisted owner's context."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT r.id,r.trigger,i.task_json,i.extra_context FROM task_runs r JOIN task_run_inputs i ON i.run_id=r.id WHERE r.status='queued'"
            + (' AND r.id=?' if run_id else '') + ' ORDER BY i.queued_at LIMIT 100',
            (run_id,) if run_id else ())
        return [{**dict(r), 'task': json.loads(r['task_json'])} for r in rows]
    finally:
        await conn.close()


async def start_run(run_id):
    conn = await db.get_db()
    try:
        result = await conn.execute("UPDATE task_runs SET status='running',started_at=? WHERE id=? AND user_id=? AND status='queued'",
                                   (datetime.utcnow().isoformat(), run_id, db.current_user_id()))
        await conn.commit()
        return result.rowcount == 1
    finally:
        await conn.close()


async def cancel(run_id):
    conn = await db.get_db()
    try:
        await conn.execute("UPDATE task_runs SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END, error='Cancellation requested' WHERE id=? AND user_id=? AND status IN ('queued','running')",
                           (run_id, db.current_user_id()))
        await conn.commit()
    finally:
        await conn.close()
    import cancel_registry
    cancel_registry.signal(run_id)
    return await db.get_task_run(run_id)


async def attach_child(run_id, report_id):
    conn = await db.get_db()
    try:
        await conn.execute('UPDATE task_run_inputs SET child_report_id=? WHERE run_id=? AND EXISTS (SELECT 1 FROM task_runs WHERE id=? AND user_id=?)',
                           (report_id, run_id, run_id, db.current_user_id()))
        await conn.commit()
    finally:
        await conn.close()


async def child_report(run_id):
    conn=await db.get_db()
    try:
        rows=await conn.execute_fetchall('SELECT i.child_report_id FROM task_run_inputs i JOIN task_runs r ON r.id=i.run_id WHERE r.id=? AND r.user_id=?',(run_id,db.current_user_id()))
        return rows[0]['child_report_id'] if rows else None
    finally:await conn.close()


async def reconcile_children():
    """Settle pending cancellations only after the authoritative child stopped."""
    conn=await db.get_db()
    try:
        await conn.execute("""UPDATE task_runs SET status='cancelled',finished_at=CURRENT_TIMESTAMP,
            error='Child research stopped after cancellation request' WHERE status='cancelling'
            AND EXISTS (SELECT 1 FROM task_run_inputs i JOIN research_reports p ON p.id=i.child_report_id
            WHERE i.run_id=task_runs.id AND p.user_id=task_runs.user_id
            AND p.status NOT IN ('queued','running','cancelling'))""")
        await conn.execute("""UPDATE runs SET status='cancelled',ended_at=CURRENT_TIMESTAMP
            WHERE role='documents' AND status='cancelling' AND json_valid(result_envelope)
            AND json_extract(result_envelope,'$.action')='batch_export'
            AND EXISTS (SELECT 1 FROM runs child WHERE child.id=json_extract(runs.result_envelope,'$.child_run_id')
            AND child.conversation_id=runs.conversation_id AND child.status NOT IN ('queued','pending','running','cancelling'))""")
        await conn.commit()
    finally:await conn.close()
