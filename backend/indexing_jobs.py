"""Saved indexing submissions; interrupted work requires an explicit retry."""
import asyncio
import hashlib
import json
from pathlib import Path
import uuid
import database as db


def digest(path):
    result=hashlib.sha256()
    with open(path,'rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''): result.update(block)
    return result.hexdigest()


async def submit(kb_id,filename,filepath):
    if not await db.get_kb(kb_id): raise LookupError('Knowledge base not found')
    identity='index-'+uuid.uuid4().hex
    sha=await asyncio.to_thread(digest,filepath)
    conn=await db.get_db()
    try:
        await conn.execute('INSERT INTO indexing_jobs(id,user_id,kb_id,filename,filepath,sha256,status) VALUES(?,?,?,?,?,?,\'queued\')',
            (identity,db.current_user_id(),kb_id,filename,str(Path(filepath).resolve()),sha))
        await conn.commit()
    finally: await conn.close()
    return identity


async def get(identity):
    conn=await db.get_db()
    try:
        rows=await conn.execute_fetchall('SELECT * FROM indexing_jobs WHERE id=? AND user_id=?',(identity,db.current_user_id()))
        return dict(rows[0]) if rows else None
    finally: await conn.close()


async def execute(identity):
    import rag
    job=await get(identity)
    if not job: raise LookupError('Index job not found')
    conn=await db.get_db()
    try:
        claimed=await conn.execute("UPDATE indexing_jobs SET status='running',updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=? AND status='queued'",(identity,db.current_user_id()))
        await conn.commit()
        if claimed.rowcount!=1: return
    finally: await conn.close()
    state,error,result='failed','',{}
    try:
        if not await db.get_kb(job['kb_id']): raise LookupError('Knowledge base no longer exists')
        if await asyncio.to_thread(digest,job['filepath']) != job['sha256']:
            raise ValueError('Source changed after submission; upload or reindex its current version')
        result=await rag.index_file(job['kb_id'],job['filename'],job['filepath'])
        if result.get('error'): raise ValueError(result['error'])
        state='partial' if result.get('warnings') else 'succeeded'
        return result
    except asyncio.CancelledError:
        state,error='interrupted','Indexing interrupted; verify the saved source before retrying'
        raise
    except Exception as exc:
        error=str(exc)[:1000]
        return {'error':error,'filename':job['filename']}
    finally:
        conn=await db.get_db()
        try:
            await conn.execute('UPDATE indexing_jobs SET status=?,error=?,result_json=?,updated_at=CURRENT_TIMESTAMP,ended_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?',
                (state,error,json.dumps(result),identity,db.current_user_id()))
            await conn.commit()
        finally: await conn.close()


async def retry(identity,track_bg):
    job=await get(identity)
    if not job: raise LookupError('Index job not found')
    conn=await db.get_db()
    try:
        changed=await conn.execute("UPDATE indexing_jobs SET status='queued',error='',ended_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=? AND status IN ('failed','interrupted','partial')",(identity,db.current_user_id()))
        await conn.commit()
        if changed.rowcount!=1: raise ValueError('Index job is already active or complete')
    finally: await conn.close()
    track_bg(execute(identity))
    return {'job_id':identity,'status':'queued'}


async def latest(kb_id,filename):
    conn=await db.get_db()
    try:
        rows=await conn.execute_fetchall('SELECT * FROM indexing_jobs WHERE kb_id=? AND filename=? AND user_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',
            (kb_id,filename,db.current_user_id()))
        if not rows:return None
        row=dict(rows[0]);state=row['status']
        return {**json.loads(row['result_json']), 'filename':filename,'job_id':row['id'],
            'state':state, 'status':'indexing' if state in {'queued','running'} else 'done' if state=='succeeded' else 'partial' if state=='partial' else 'error',
            'error':row['error']}
    finally: await conn.close()


async def reconcile():
    conn=await db.get_db()
    try:
        await conn.execute("UPDATE indexing_jobs SET status='interrupted',error='Backend restarted; retry indexing from its saved source',updated_at=CURRENT_TIMESTAMP,ended_at=CURRENT_TIMESTAMP WHERE status IN ('queued','running')")
        await conn.commit()
    finally: await conn.close()
