"""Jarvis scheduled-task routes: /api/tasks* CRUD, run-now, run history,
NL→draft parsing, and the unauthenticated /api/hooks/{token} webhook trigger."""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import config
import database as db
import cancel_registry
import model_providers
import scheduler
from .context import route_context

router = APIRouter()

_WEBHOOK_BODY_CAP = 32 * 1024


class TaskCreate(BaseModel):
    title: str
    prompt: str = ""
    task_type: str = "llm"
    schedule_kind: str = "once"
    schedule_json: dict = {}
    timezone: str = ""
    model: str = ""
    tool_ids: list[str] = []
    delivery_json: dict = {}
    conversation_id: str = ""
    event_trigger_json: dict = {}
    enabled: bool = True


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    prompt: Optional[str] = None
    task_type: Optional[str] = None
    schedule_kind: Optional[str] = None
    schedule_json: Optional[dict] = None
    timezone: Optional[str] = None
    model: Optional[str] = None
    tool_ids: Optional[list[str]] = None
    delivery_json: Optional[dict] = None
    conversation_id: Optional[str] = None
    event_trigger_json: Optional[dict] = None
    enabled: Optional[bool] = None


class TaskParseRequest(BaseModel):
    text: str


def _validate_task_fields(task_type: str, schedule_kind: str) -> None:
    if task_type not in db.TASK_TYPES:
        raise HTTPException(400, f"task_type must be one of {db.TASK_TYPES}")
    if schedule_kind not in db.SCHEDULE_KINDS:
        raise HTTPException(400, f"schedule_kind must be one of {db.SCHEDULE_KINDS}")


async def _computed_next_run(schedule_kind: str, schedule_json: dict,
                             timezone: str) -> str | None:
    return await scheduler.recompute_next_run(schedule_kind, schedule_json, timezone)


@router.post("/api/tasks")
async def create_task(req: TaskCreate):
    _validate_task_fields(req.task_type, req.schedule_kind)
    next_run = await _computed_next_run(req.schedule_kind, req.schedule_json, req.timezone)
    if req.schedule_kind not in ("event", "webhook") and not next_run:
        raise HTTPException(400, "schedule_json does not produce a next run time")
    task = await db.create_scheduled_task(
        f"task-{uuid.uuid4().hex[:10]}",
        title=req.title.strip()[:200] or "Untitled task",
        prompt=req.prompt,
        task_type=req.task_type,
        schedule_kind=req.schedule_kind,
        schedule_json=req.schedule_json,
        timezone=req.timezone,
        next_run=next_run,
        model=req.model,
        tool_ids=req.tool_ids,
        delivery_json=req.delivery_json,
        conversation_id=req.conversation_id,
        event_trigger_json=req.event_trigger_json,
        webhook_token=scheduler.new_webhook_token() if req.schedule_kind == "webhook" else "",
        enabled=req.enabled,
    )
    return task


@router.get("/api/tasks")
async def list_tasks():
    return await db.list_scheduled_tasks()


@router.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    task = await db.get_scheduled_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.patch("/api/tasks/{task_id}")
async def update_task(task_id: str, req: TaskUpdate):
    task = await db.get_scheduled_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if "task_type" in fields or "schedule_kind" in fields:
        _validate_task_fields(fields.get("task_type", task["task_type"]),
                              fields.get("schedule_kind", task["schedule_kind"]))
    kind = fields.get("schedule_kind", task["schedule_kind"])
    # Recompute next_run whenever the schedule, tz, or enablement changes.
    if any(k in fields for k in ("schedule_kind", "schedule_json", "timezone")) or fields.get("enabled"):
        fields["next_run"] = await _computed_next_run(
            kind, fields.get("schedule_json", task["schedule_json"]),
            fields.get("timezone", task["timezone"]))
    if kind == "webhook" and not task.get("webhook_token"):
        fields["webhook_token"] = scheduler.new_webhook_token()
    updated = await db.update_scheduled_task(task_id, fields)
    return updated


@router.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    if not await db.delete_scheduled_task(task_id):
        raise HTTPException(404, "Task not found")
    return {"status": "deleted"}


@router.post("/api/tasks/{task_id}/run")
async def run_task(task_id: str):
    run_id = await scheduler.run_task_now(task_id)
    if not run_id:
        raise HTTPException(404, "Task not found")
    return {"status": "started", "run_id": run_id}


@router.post("/api/tasks/{task_id}/pause")
async def pause_task(task_id: str):
    task = await db.update_scheduled_task(task_id, {"enabled": False})
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str):
    task = await db.get_scheduled_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    next_run = await _computed_next_run(task["schedule_kind"], task["schedule_json"], task["timezone"])
    return await db.update_scheduled_task(task_id, {"enabled": True, "next_run": next_run})


@router.get("/api/tasks/{task_id}/runs")
async def task_runs(task_id: str, limit: int = 20):
    if not await db.get_scheduled_task(task_id):
        raise HTTPException(404, "Task not found")
    return await db.list_task_runs(task_id, limit=min(limit, 100))


@router.post("/api/tasks/runs/{run_id}/cancel")
async def cancel_task_run(run_id: str):
    run = await db.get_task_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    cancel_registry.signal(run_id)
    if run.get("status") == "running":
        await db.finish_task_run(run_id, "cancelled", error="cancelled by user")
    return {"status": "cancelled"}


_PARSE_PROMPT = """Convert the user's request into a scheduled-task draft. Reply with ONLY a JSON object:
{{"title": "...", "prompt": "what the task should do each run, phrased as an instruction",
 "task_type": "llm",
 "schedule_kind": "once|daily|weekly|monthly|cron",
 "schedule_json": {{"time": "HH:MM"}} | {{"time": "HH:MM", "weekday": 0-6}} | {{"time": "HH:MM", "day": 1-31}} | {{"run_at": "YYYY-MM-DDTHH:MM"}} | {{"cron": "..."}}}}
weekday 0 = Monday. Current local time: {now}.

User request: {text}"""


@router.post("/api/tasks/parse")
async def parse_task(req: TaskParseRequest):
    """NL → task DRAFT for user review. Never creates a live task."""
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    ctx = route_context()
    model = (model_providers.reject_cloud(config.WORKSPACE_MODEL)
             or model_providers.reject_cloud(config.DEFAULT_MODEL))
    if not model:
        raise HTTPException(503, "No local model available for task parsing")
    # Anchor "now" to the USER's assistant-profile timezone, not the server
    # clock — "tomorrow at 9" must draft in the user's wall clock.
    import pim
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(await pim.user_timezone() or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    now_local = datetime.now(tz).replace(tzinfo=None)
    prompt = _PARSE_PROMPT.format(now=now_local.isoformat(timespec="minutes"), text=text[:2000])
    raw = await model_providers.complete_chat(
        ctx.http, model, prompt, num_ctx=4096, num_predict=512, format_json=True,
        timeout=60, ollama_url=config.OLLAMA_URL)
    try:
        draft = json.loads(raw or "{}")
    except json.JSONDecodeError:
        draft = {}
    if not isinstance(draft, dict) or not draft.get("title"):
        raise HTTPException(422, "Could not parse a task from that text")
    draft.setdefault("task_type", "llm")
    draft.setdefault("schedule_kind", "once")
    draft.setdefault("schedule_json", {})
    if draft["task_type"] not in db.TASK_TYPES:
        draft["task_type"] = "llm"
    if draft["schedule_kind"] not in db.SCHEDULE_KINDS:
        draft["schedule_kind"] = "once"
    draft["draft"] = True
    return draft


# In-process fixed-window rate limit for the unauthenticated webhook route
# (same pattern as the login limiter in routes/users.py): a leaked URL must
# not let a caller queue unbounded task runs / task_runs rows.
_HOOK_WINDOW_SECONDS = 60
_HOOK_MAX_HITS = 30
_HOOK_HITS_CAP = 512
_HOOK_HITS: dict[str, tuple[int, float]] = {}  # token -> (count, window_start)


def _hook_rate_limited(token: str) -> int | None:
    """Seconds until the window resets when limited, else None (and records
    the hit)."""
    import time
    now = time.time()
    count, start = _HOOK_HITS.get(token, (0, now))
    if now - start >= _HOOK_WINDOW_SECONDS:
        count, start = 0, now
    if count >= _HOOK_MAX_HITS:
        return max(1, int(_HOOK_WINDOW_SECONDS - (now - start)))
    _HOOK_HITS[token] = (count + 1, start)
    if len(_HOOK_HITS) > _HOOK_HITS_CAP:
        cutoff = now - _HOOK_WINDOW_SECONDS
        for stale in [k for k, (_c, s) in _HOOK_HITS.items() if s < cutoff]:
            _HOOK_HITS.pop(stale, None)
    return None


@router.post("/api/hooks/{token}")
async def webhook_trigger(token: str, request: Request):
    """Unauthenticated inbound trigger — the token IS the credential. The
    middleware scoped this request to the default user, so the handler sets
    the task owner's contextvar explicitly before firing."""
    retry_after = _hook_rate_limited(token)
    if retry_after is not None:
        raise HTTPException(429, "Too many webhook calls; try again later",
                            headers={"Retry-After": str(retry_after)})
    task = await db.get_task_by_webhook_token(token)
    if not task or not secrets.compare_digest(task.get("webhook_token") or "", token):
        raise HTTPException(404, "Unknown hook")
    body = (await request.body())[:_WEBHOOK_BODY_CAP]
    body_text = body.decode("utf-8", errors="replace")
    user_token = db.set_current_user_id(task["user_id"])
    try:
        run_id = await scheduler.fire_webhook(task, body_text)
    finally:
        db.reset_current_user_id(user_token)
    return {"status": "started", "run_id": run_id}
