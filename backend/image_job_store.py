"""Owner-scoped Image Studio submissions, retained across backend restarts."""
import json
import time
import uuid
from datetime import datetime, timezone
import database as db


async def create(params):
    job = {'id': 'img-' + uuid.uuid4().hex, 'status': 'submitting', 'params': params,
           'created': time.time(), 'prompt_id': '', 'images': []}
    conn = await db.get_db()
    try:
        await conn.execute('INSERT INTO image_jobs(id,user_id,job_json) VALUES(?,?,?)',
                           (job['id'], db.current_user_id(), json.dumps(job)))
        await conn.commit()
    finally:
        await conn.close()
    return job


async def get(job_id):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall('SELECT job_json FROM image_jobs WHERE id=? AND user_id=?',
                                          (job_id, db.current_user_id()))
        return json.loads(rows[0]['job_json']) if rows else None
    finally:
        await conn.close()


async def pending():
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT user_id,id FROM image_jobs WHERE json_extract(job_json,'$.status') IN ('queued','running','cancelling') AND COALESCE(json_extract(job_json,'$.retryable'),0)=0")
        return [(r['user_id'],r['id']) for r in rows]
    finally:
        await conn.close()


async def reconcile_submissions():
    conn = await db.get_db()
    try:
        await conn.execute("UPDATE image_jobs SET job_json=json_set(job_json,'$.status','interrupted','$.error','Submission outcome uncertain; inspect ComfyUI before resubmitting') WHERE json_extract(job_json,'$.status')='submitting'")
        await conn.commit()
    finally:
        await conn.close()


async def save(job):
    job['updated_at'] = datetime.now(timezone.utc).isoformat()
    if job.get('status') in {'done', 'error', 'cancelled'}:
        job.setdefault('ended_at', job['updated_at'])
    conn = await db.get_db()
    try:
        await conn.execute('UPDATE image_jobs SET job_json=? WHERE id=? AND user_id=?',
                           (json.dumps(job), job['id'], db.current_user_id()))
        await conn.commit()
    finally:
        await conn.close()


# Serialize polling/publication and cancellation for one job, not all users.
import asyncio
import functools
import weakref
_locks = weakref.WeakValueDictionary()


def serialized(fn):
    @functools.wraps(fn)
    async def guarded(job_id, *args, **kwargs):
        lock = _locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            return await fn(job_id, *args, **kwargs)
    return guarded


async def purge_finished():
    """Remove the caller's terminal rows; report any retained upstream work."""
    conn = await db.get_db()
    active = False
    try:
        for row in await conn.execute_fetchall('SELECT id,user_id,job_json FROM image_jobs'):
            job = json.loads(row['job_json'])
            if job.get('status') in {'done', 'error', 'cancelled'}:
                if row['user_id'] == db.current_user_id():
                    await conn.execute('DELETE FROM image_jobs WHERE id=? AND user_id=?', (row['id'], db.current_user_id()))
            else:
                active = True
        await conn.commit()
    finally:
        await conn.close()
    return active
