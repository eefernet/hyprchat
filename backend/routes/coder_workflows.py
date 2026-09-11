"""Version-3 workflow commands and replayable progress, under user scoping."""
import asyncio
import json

import httpx
from pydantic import BaseModel, Field

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse

import coder_jobs
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


@router.post("/api/coder/workflows", status_code=202)
async def create_workflow(request: CreateWorkflow):
    body = request.model_dump()
    try:
        return await coder_jobs.create(body.get("conversation_id", ""), body.get("task", ""),
            body.get("mode", "build_from_prompt"), body.get("project_id", ""),
            body.get("model", ""), body.get("idempotency_key", ""))
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/api/coder/workflows/{workflow_id}/resume")
async def resume_workflow(workflow_id: str):
    try:
        return await coder_jobs.resume(workflow_id)
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
            batch = await store.events(workflow_id, cursor)
            for event in batch:
                cursor = event["seq"]
                yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
            job = await store.get(workflow_id)
            if not job:
                break
            if not batch and job["state"] in store.TERMINAL | {"blocked", "waiting_for_input"}:
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
