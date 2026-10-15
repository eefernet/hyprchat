"""Owner-scoped adapters for existing domain job controllers."""
from fastapi import APIRouter, Body, HTTPException
import job_registry

router = APIRouter()


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
async def list_jobs(kind: str = '', status: str = '', limit: int = 100, offset: int = 0):
    return await job_registry.list_jobs(kind=kind, status=status, limit=limit, offset=offset)


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
