"""Owner-scoped GUI previews; generated application HTML never reaches this origin."""
import asyncio
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Body, HTTPException, WebSocket, WebSocketDisconnect

import coder_jobs
import database as db
from access_policy import authorize
from db import coder_jobs as store
from db.coder_promotion import candidate_for
from .context import route_context

router = APIRouter()


async def owned_job(identity):
    job = await store.get(identity)
    if not job: raise HTTPException(404, 'Workflow not found')
    if job.get('engine') != 'openhands159': raise HTTPException(409, 'Previews require the new engine')
    return job


def published(job, revision):
    return bool(candidate_for(job, revision) or
                (job.get('artifact') or {}).get('metadata', {}).get('revision_id') == revision)


def public_session(state):
    return {k: state[k] for k in ('id','revision','status','expires_at','error','lifecycle','desired_state') if k in state}


async def worker(job, method, suffix='', **kwargs):
    response = await route_context().http.request(method,
        coder_jobs.worker_base(job)+f"/jobs/{job['id']}/previews"+suffix, **kwargs, timeout=45)
    if response.status_code >= 400:
        raise HTTPException(response.status_code, response.json().get('detail','Preview worker unavailable'))
    return response.json()


@router.post('/api/coder/workflows/{workflow_id}/previews', status_code=202)
async def create(workflow_id: str, body: dict = Body(...)):
    job = await owned_job(workflow_id)
    if not published(job, body.get('revision','')): raise HTTPException(409,'Preview requires an exact delivered revision')
    return public_session(await worker(job,'POST',json=body))


@router.get('/api/coder/workflows/{workflow_id}/previews/{identity}')
async def status(workflow_id: str, identity: str):
    job = await owned_job(workflow_id)
    return public_session(await worker(job,'GET','/'+identity))


@router.get('/api/coder/workflows/{workflow_id}/previews')
async def listing(workflow_id: str):
    job=await owned_job(workflow_id)
    return [public_session(state) for state in await worker(job,'GET') if published(job,state['revision'])]


@router.post('/api/coder/workflows/{workflow_id}/previews/{identity}/control')
async def control(workflow_id: str, identity: str, body: dict=Body(...)):
    job=await owned_job(workflow_id)
    state=await worker(job,'GET','/'+identity)
    if not published(job,state['revision']): raise HTTPException(409,'Application requires its exact delivered revision')
    return public_session(await worker(job,'POST','/'+identity+'/control',json=body))


@router.get('/api/coder/workflows/{workflow_id}/previews/{identity}/logs')
async def logs(workflow_id: str, identity: str):
    return await worker(await owned_job(workflow_id),'GET','/'+identity+'/logs')


@router.delete('/api/coder/workflows/{workflow_id}/previews/{identity}')
async def close(workflow_id: str, identity: str):
    return await worker(await owned_job(workflow_id),'DELETE','/'+identity)


@router.websocket('/api/coder/workflows/{workflow_id}/previews/{identity}/stream')
async def stream(websocket: WebSocket, workflow_id: str, identity: str):
    # HTTP middleware does not cover WebSockets. Revalidate password and owner here.
    context = route_context()
    user, error = await authorize('profile', context.request_user_id(websocket), context.request_session_token(websocket), db)
    if error:
        await websocket.close(code=4403); return
    owner_token = db.set_current_user_id(user)
    tasks = []
    try:
        job = await owned_job(workflow_id)
        state = await worker(job,'GET','/'+identity)
        if not published(job, state['revision']):
            await websocket.close(code=4409); return
        from websockets.asyncio.client import connect
        base = coder_jobs.worker_base(job)
        url = ('wss'+base[5:] if base.startswith('https:') else 'ws'+base[4:])
        url += f"/jobs/{workflow_id}/previews/{identity}/stream?"+urlencode({'token':state['token']})
        async with connect(url, max_size=1024*1024, proxy=None) as remote:
            await websocket.accept()
            async def outward():
                while True: await remote.send(await websocket.receive_bytes())
            async def inward():
                async for packet in remote:
                    if isinstance(packet, bytes): await websocket.send_bytes(packet)
            tasks = [asyncio.create_task(outward()),asyncio.create_task(inward())]
            await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
    except (HTTPException, httpx.HTTPError, OSError):
        await websocket.close(code=4410)
    finally:
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        db.reset_current_user_id(owner_token)
        try: await websocket.close()
        except (RuntimeError, WebSocketDisconnect): pass
