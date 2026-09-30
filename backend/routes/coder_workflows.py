"""Version-3 workflow commands and replayable progress, under user scoping."""
import asyncio
import json

import httpx
from pydantic import BaseModel, Field

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse

import coder_jobs
import database as db
import config
from db import coder_jobs as store
from .context import route_context


router = APIRouter()


class CreateWorkflow(BaseModel):
    conversation_id: str = Field(min_length=1)
    task: str = Field(min_length=1)
    mode: str = "build_from_prompt"
    project_id: str = ""
    model: str = ""
    idempotency_key: str = ""
    visual_review: bool | None = None
    visual_model: str | None = None
    execution_commands: dict | None = None
    protected_files: list[str] | None = None


@router.post("/api/coder/workflows", status_code=202)
async def create_workflow(request: CreateWorkflow):
    body = request.model_dump()
    try:
        return await coder_jobs.create(body.get("conversation_id", ""), body.get("task", ""),
            body.get("mode", "build_from_prompt"), body.get("project_id", ""),
            body.get("model", ""), body.get("idempotency_key", ""), visual_review=request.visual_review, visual_model=request.visual_model,
            execution_commands=request.execution_commands, protected_files=request.protected_files)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/api/coder/workflows/{workflow_id}/resume")
async def resume_workflow(workflow_id: str, body: dict = Body(default={})):
    try:
        return await coder_jobs.resume(workflow_id,visual_review=body.get("visual_review"),visual_model=body.get("visual_model"),candidate_revision=body.get('candidate_revision'),clarification=body.get('clarification'))
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.get("/api/coder/workflows/{workflow_id}/events")
async def workflow_events(workflow_id: str, request: Request, after: int = 0):
    if not await store.get(workflow_id):
        raise HTTPException(404, "Workflow not found")
    try:
        cursor = max(after, int(request.headers.get("last-event-id") or 0))
    except ValueError:
        raise HTTPException(422, "Invalid event cursor") from None

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            try:
                batch = await store.events(workflow_id, cursor)
            except LookupError:
                break   # the workflow was deleted while this client was connected
            for event in batch:
                cursor = event["seq"]
                yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
            job = await store.get(workflow_id)
            if not job:
                break
            if not batch and job["state"] in store.TERMINAL | {"blocked", "waiting_for_input", "ready_for_review"}:
                break
            yield ": heartbeat\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/coder/workflows/{workflow_id}/inspect")
async def inspect_workflow(workflow_id: str, body: dict = Body(...)):
    job = await store.get(workflow_id)
    if not job:
        raise HTTPException(404, "Workflow not found")
    if not job.get("worker_operation"):
        raise HTTPException(409, "Workspace is not initialized")
    try:
        response = await route_context().http.post(coder_jobs.worker_url(job) + "/inspect", json=body, timeout=30)
    except httpx.HTTPError as error:
        raise HTTPException(503, "Project worker is unavailable; retry inspection when it reconnects") from error
    if response.status_code >= 400:
        raise HTTPException(response.status_code, response.json().get("detail", "Inspection failed"))
    return response.json()


@router.get("/api/coder/projects")
async def projects(conversation_id: str = ""):
    active = await db.get_coding_project_by_conv(conversation_id) if conversation_id else None
    return {"projects":await db.list_coding_projects(),"active_project_id":(active or {}).get("id","")}


@router.post("/api/coder/projects/{project_id}/select")
async def select_project(project_id: str, body: dict = Body(...)):
    try: return await db.select_coding_project(body.get("conversation_id",""),project_id)
    except LookupError as error: raise HTTPException(404,str(error)) from error


@router.get("/api/coder/projects/{project_id}/history")
async def project_history(project_id: str):
    try: return await store.project_history(project_id)
    except LookupError as error: raise HTTPException(404,str(error)) from error


async def stream_worker_file(url, media_type, filename=""):
    try:
        client=route_context().http
        response=await client.send(client.build_request("GET",url,timeout=120),stream=True)
        response.raise_for_status()
    except httpx.HTTPError as error:
        if 'response' in locals(): await response.aclose()
        raise HTTPException(503,"Worker evidence is unavailable") from error
    async def content():
        try:
            async for chunk in response.aiter_bytes(): yield chunk
        finally: await response.aclose()
    headers={"Cache-Control":"private, no-store"}
    if filename: headers["Content-Disposition"]=f'attachment; filename="{filename}"'
    return StreamingResponse(content(),media_type=response.headers.get("content-type",media_type),headers=headers)


@router.get("/api/coder/workflows/{workflow_id}/evidence/{identity}")
async def evidence(workflow_id: str, identity: str):
    job=await store.get(workflow_id)
    if not job: raise HTTPException(404,"Workflow not found")
    import re
    if not re.fullmatch(r"[a-f0-9]{64}",identity): raise HTTPException(404,"Evidence not found")
    # The worker checks that this opaque reference belongs to the owned job.
    return await stream_worker_file(config.OPENHANDS_URL.rstrip("/")+f"/jobs/{workflow_id}/evidence/{identity}",
        "application/octet-stream")


@router.get("/api/coder/workflows/{workflow_id}/checkpoint")
async def checkpoint(workflow_id: str):
    job=await store.get(workflow_id)
    if not job: raise HTTPException(404,"Workflow not found")
    if not job.get("revision_id") or job["state"] not in {"blocked","waiting_for_input","cancelled","ready_for_review"}:
        raise HTTPException(409,"Stop the job before exporting an unverified checkpoint")
    return await stream_worker_file(coder_jobs.worker_url(job)+"/checkpoint?revision="+job["revision_id"],
        "application/gzip",f"unverified-{job['id']}-{job['revision_id']}.tar.gz")
