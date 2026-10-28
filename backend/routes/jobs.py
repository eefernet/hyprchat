"""Owner-scoped adapters for existing domain job controllers."""
from fastapi import APIRouter, BackgroundTasks, Body, HTTPException
import job_registry

router = APIRouter()


@router.get('/api/messages/{message_id}/reviews')
async def message_reviews(message_id: int):
    import database as db
    import json
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall('''SELECT m.metadata FROM messages m JOIN conversations c ON c.id=m.conversation_id
            WHERE m.id=? AND c.user_id=?''', (message_id, db.current_user_id()))
        if not rows:
            raise HTTPException(404, 'Message not found')
        meta = json.loads(rows[0]['metadata'] or '{}')
        return {'revisions': meta.get('review_history', []), 'in_progress': meta.get('in_progress', False)}
    finally:
        await conn.close()


@router.post('/api/messages/{message_id}/reviews/{revision}/restore')
async def restore_message_review(message_id: int, revision: int, background_tasks: BackgroundTasks):
    import database as db
    import json
    conn = await db.get_db()
    try:
        await conn.execute('BEGIN IMMEDIATE')
        rows = await conn.execute_fetchall('''SELECT m.content,m.metadata,m.conversation_id FROM messages m JOIN conversations c ON c.id=m.conversation_id
            WHERE m.id=? AND c.user_id=?''', (message_id, db.current_user_id()))
        if not rows:
            raise HTTPException(404, 'Message not found')
        meta = json.loads(rows[0]['metadata'] or '{}')
        history = meta.get('review_history', [])
        if meta.get('in_progress'):
            raise HTTPException(409, 'Wait for the response to finish before restoring a review')
        if revision < 0 or revision >= len(history):
            raise HTTPException(404, 'Review not found')
        content = history[revision]['content']
        meta['restored_review'] = revision
        await conn.execute('UPDATE messages SET content=?,metadata=? WHERE id=?', (content, json.dumps(meta), message_id))
        conv_id = rows[0]['conversation_id']
        await conn.execute("UPDATE conversations SET summary='',summary_until_msg_id=0 WHERE id=? AND summary_until_msg_id>=?", (conv_id, message_id))
        await conn.commit()
        import rag
        # Invalidate stale recall text even if the restored answer is too short to index.
        background_tasks.add_task(rag.remove_history_message, message_id)
        return {'content': content, 'metadata': meta}
    finally:
        await conn.close()


@router.get('/api/library/search')
async def library_search(query: str, kind: str = '', workspace_id: str = '', start: str = '', end: str = '', limit: int = 50, offset: int = 0):
    import library_search as library
    return await library.search(query,kind=kind,workspace_id=workspace_id,start=start,end=end,limit=limit,offset=offset)


@router.post('/api/library/rebuild')
async def rebuild_library():
    import library_search
    return await library_search.rebuild()


@router.get('/api/messages/{message_id}/context')
async def message_context(message_id: int):
    import context_inspector
    try:
        return await context_inspector.inspect(message_id)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc


@router.get('/api/jobs')
async def list_jobs(kind: str = '', status: str = '', limit: int = 100, offset: int = 0, workspace_id: str = ''):
    return await job_registry.list_jobs(kind=kind, status=status, limit=limit, offset=offset,workspace_id=workspace_id)


@router.get('/api/jobs/{job_id}')
async def get_job(job_id: str):
    job = await job_registry.get(job_id)
    if not job:
        raise HTTPException(404, 'Job not found')
    return job


@router.post('/api/jobs/{job_id}/actions/{action}')
async def job_action(job_id: str, action: str, body: dict = Body(default={})):
    try:
        result = await job_registry.act(job_id, action, body)
        return {'result': result, 'job': await job_registry.get(job_id)}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
