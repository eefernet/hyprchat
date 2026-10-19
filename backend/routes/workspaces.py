"""Project hub additions; existing workspace CRUD remains in main."""
from fastapi import APIRouter, HTTPException
import database as db
import workspace_service as workspaces

router = APIRouter()


@router.get('/api/workspaces/{ws_id}/content/{kind}')
async def content(ws_id: str, kind: str, limit: int = 50, offset: int = 0):
    try:
        if kind in {'knowledge', 'notes', 'tasks'}:
            return await workspaces.related(ws_id, kind, limit, offset)
        ws = await db.get_workspace(ws_id)
        if not ws:
            raise LookupError('Workspace not found')
        field = {'conversations': 'conversations', 'reports': 'reports', 'artifacts': 'files'}.get(kind)
        if not field:
            raise ValueError('Unknown content type')
        items = ws[field]
        limit, offset = max(1, min(limit, 100)), max(0, offset)
        return {'items': items[offset:offset+limit], 'has_more': len(items)>offset+limit}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/api/workspaces/{ws_id}/links/{kind}/{identity}')
async def link(ws_id: str, kind: str, identity: str):
    return await change_link(ws_id, kind, identity)


@router.delete('/api/workspaces/{ws_id}/links/{kind}/{identity}')
async def unlink(ws_id: str, kind: str, identity: str):
    return await change_link(ws_id, kind, identity, remove=True)


async def change_link(ws_id, kind, identity, remove=False):
    if kind not in {'knowledge', 'notes'}:
        raise HTTPException(400, 'Choose knowledge or notes')
    try:
        await workspaces.link(ws_id, kind, identity, remove=remove)
        return {'ok': True}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get('/api/knowledge-bases/{kb_id}/readiness')
async def readiness(kb_id: str):
    kb = await db.get_kb(kb_id)
    if not kb:
        raise HTTPException(404, 'Knowledge base not found')
    return await workspaces.knowledge_status(kb)


@router.post('/api/knowledge-bases/{kb_id}/files/{file_id}/index')
async def index_file(kb_id: str, file_id: int):
    import indexing_jobs
    from .context import route_context
    kb = await db.get_kb(kb_id)
    file = next((f for f in (kb or {}).get('files', []) if f['id'] == file_id), None)
    if not file:
        raise HTTPException(404, 'Knowledge file not found')
    latest = await indexing_jobs.latest(kb_id, file['filename'])
    if latest and latest['status'] == 'indexing':
        return latest
    try:
        identity = await indexing_jobs.submit(kb_id, file['filename'], file['filepath'])
    except OSError as exc:
        raise HTTPException(409, 'Saved source is unavailable; upload it again before indexing') from exc
    route_context().track_bg(indexing_jobs.execute(identity))
    return {'job_id': identity, 'status': 'indexing'}
