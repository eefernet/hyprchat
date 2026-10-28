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


@router.get('/api/workspaces/{ws_id}/defaults')
async def defaults(ws_id: str):
    try:
        return await workspaces.resolve_defaults({},ws_id)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@router.get('/api/workspaces/{ws_id}/sources')
async def sources(ws_id: str, query: str='', kind: str='', limit: int=50, offset: int=0, pinned: bool=False):
    try:
        result=await workspaces.list_sources(ws_id,query,kind,limit,offset,pinned)
        from .context import route_context
        for item in result['items']:
            if item['kind']=='artifact' and item['readiness']=='unindexed' and item['enabled']:
                route_context().track_bg(workspaces.prepare_artifact(ws_id,item['source_id']))
        return result
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc


@router.patch('/api/workspaces/{ws_id}/sources/{kind}/{identity}')
async def source_settings(ws_id: str, kind: str, identity: str, body: dict):
    try:
        return await workspaces.set_source_preferences(ws_id,kind,identity,body)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc


@router.get('/api/workspaces/{ws_id}/picker/{kind}')
async def picker(ws_id: str, kind: str, query: str='', limit: int=30, offset: int=0):
    try:
        return await workspaces.picker(ws_id,kind,query,max(1,min(limit,100)),offset)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc


@router.post('/api/workspaces/{ws_id}/sources/{kind}/{identity}/link')
async def link_source(ws_id: str, kind: str, identity: str):
    try:
        await workspaces.link_source(ws_id,kind,identity)
        return {'ok':True}
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc


@router.delete('/api/workspaces/{ws_id}/sources/{kind}/{identity}/link')
async def unlink_source(ws_id: str, kind: str, identity: str):
    try:
        await workspaces.link_source(ws_id,kind,identity,remove=True)
        return {'ok':True}
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc


from fastapi import File, UploadFile


@router.post('/api/workspaces/{ws_id}/upload')
async def upload(ws_id: str, file: UploadFile=File(...)):
    import config, uuid, hashlib
    from pathlib import Path
    from .context import route_context
    try:
        await workspaces.require_workspace(ws_id)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    title=Path(file.filename or 'upload').name
    data=await file.read(config.MAX_UPLOAD_SIZE_MB*1024*1024+1)
    if len(data)>config.MAX_UPLOAD_SIZE_MB*1024*1024:
        raise HTTPException(413,'File exceeds upload size limit')
    return await workspaces.save_source_file(ws_id,'document',uuid.uuid4().hex,hashlib.sha256(data).hexdigest(),title,data,route_context().track_bg)


@router.get('/api/workspaces/{ws_id}/overview')
async def overview(ws_id: str):
    import asyncio, job_registry
    try:
        await workspaces.require_workspace(ws_id)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    pinned,todos,jobs,failed,interrupted,partial=await asyncio.gather(
        workspaces.list_sources(ws_id,limit=8,pinned=True),
        db.list_notes(workspace_id=ws_id,kind='todo',include_done=False,limit=8),
        job_registry.list_jobs(workspace_id=ws_id,status='active',limit=8),
        job_registry.list_jobs(workspace_id=ws_id,status='failed',limit=8),
        job_registry.list_jobs(workspace_id=ws_id,status='interrupted',limit=8),
        job_registry.list_jobs(workspace_id=ws_id,status='partial',limit=8))
    conn=await db.get_db()
    try:
        rows=await conn.execute_fetchall("SELECT COUNT(*) n FROM memories WHERE workspace_id=? AND user_id=? AND status='suggested'",(ws_id,db.current_user_id()))
    finally: await conn.close()
    return {'pinned':pinned['items'],'todos':todos,'jobs':jobs['jobs'],'failures':(failed['jobs']+interrupted['jobs']+partial['jobs'])[:12],'pending_memories':rows[0]['n']}
