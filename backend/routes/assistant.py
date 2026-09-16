"""Jarvis Personal Assistant routes — per-user profile (persona, pinned
conversation, timezone, gatherers) and check-in schedule management.

The assistant persona is a normal model_configs row; check-ins are normal
scheduled_tasks with task_type="check_in" pointed at the pinned conversation.
"""

from __future__ import annotations

import uuid
import zoneinfo
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database as db
import scheduler
from agents import assistant as assistant_mod

router = APIRouter()


class CheckInUpdate(BaseModel):
    id: Optional[str] = None          # existing task id; None creates one
    name: Optional[str] = None
    time: Optional[str] = None        # "HH:MM" local (assistant tz)
    prompt: Optional[str] = None
    enabled: Optional[bool] = None
    schedule_kind: Optional[str] = None  # daily (default) | weekly | monthly | cron
    weekday: Optional[int] = None     # weekly: 0=Monday
    day: Optional[int] = None         # monthly: 1-31 (clamped to month length)
    cron: Optional[str] = None        # cron kind expression
    delete: bool = False


class AssistantUpdate(BaseModel):
    name: Optional[str] = None
    personality: Optional[str] = None
    model: Optional[str] = None
    tool_ids: Optional[list[str]] = None
    timezone: Optional[str] = None
    enabled_gatherers: Optional[list[str]] = None
    allow_autonomous_email: Optional[bool] = None
    location: Optional[str] = None
    quiet_hours: Optional[dict] = None
    check_ins: Optional[list[CheckInUpdate]] = None


_CHECKIN_KINDS = ("daily", "weekly", "monthly", "cron")


def _checkin_schedule(ci: CheckInUpdate, existing: dict | None = None) -> tuple[str, dict]:
    """Effective (schedule_kind, schedule_json) for a check-in create/update,
    merging the request over the existing task's schedule."""
    kind = ci.schedule_kind or (existing or {}).get("schedule_kind") or "daily"
    if kind not in _CHECKIN_KINDS:
        raise HTTPException(400, f"schedule_kind must be one of {_CHECKIN_KINDS}")
    prev = dict((existing or {}).get("schedule_json") or {})
    if kind == "cron":
        cron = ci.cron if ci.cron is not None else prev.get("cron")
        if not str(cron or "").strip():
            raise HTTPException(400, "cron check-ins need a cron expression")
        return kind, {"cron": str(cron).strip()}
    sj = {"time": ci.time if ci.time is not None else (prev.get("time") or "08:30")}
    if kind == "weekly":
        sj["weekday"] = int(ci.weekday if ci.weekday is not None else prev.get("weekday") or 0) % 7
    if kind == "monthly":
        sj["day"] = max(1, min(31, int(ci.day if ci.day is not None else prev.get("day") or 1)))
    return kind, sj


async def _profile_payload() -> dict:
    profile = await assistant_mod.ensure_assistant()
    persona = await db.get_model_config(profile.get("model_config_id") or "") or {}
    tasks = await db.list_scheduled_tasks()
    check_ins = [t for t in tasks if t.get("task_type") == "check_in"]
    return {
        "profile": profile,
        "persona": {
            "id": persona.get("id") or "",
            "name": persona.get("name") or "",
            "personality": persona.get("system_prompt") or "",
            "model": persona.get("base_model") or "",
            "tool_ids": persona.get("tool_ids") or [],
        },
        "check_ins": check_ins,
        "gatherers": assistant_mod.gatherer_names(),
    }


@router.get("/api/assistant")
async def get_assistant():
    return await _profile_payload()


@router.post("/api/assistant/seed")
async def seed_assistant():
    await assistant_mod.ensure_assistant()
    return await _profile_payload()


@router.get("/api/assistant/timezones")
async def list_timezones():
    return {"timezones": sorted(zoneinfo.available_timezones())}


@router.get("/api/assistant/brief")
async def latest_brief():
    """Latest assistant message from the pinned conversation, for the panel's
    inline brief card (avoids pulling the whole conversation client-side)."""
    profile = await assistant_mod.ensure_assistant()
    conv_id = profile.get("conversation_id") or ""
    conv = await db.get_conversation(conv_id)
    message = None
    for m in reversed((conv or {}).get("messages") or []):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            message = {"content": m["content"],
                       "created_at": m.get("created_at") or ""}
            break
    return {"conversation_id": conv_id, "message": message}


@router.patch("/api/assistant")
async def update_assistant(req: AssistantUpdate):
    profile = await assistant_mod.ensure_assistant()

    if req.timezone is not None:
        try:
            zoneinfo.ZoneInfo(req.timezone)
        except Exception:
            raise HTTPException(400, f"Unknown timezone: {req.timezone}")

    persona_fields = {}
    if req.name is not None:
        persona_fields["name"] = req.name.strip()[:100] or assistant_mod.ASSISTANT_CONFIG_NAME
    if req.personality is not None:
        persona_fields["system_prompt"] = req.personality
    if req.model is not None:
        persona_fields["base_model"] = req.model
    if req.tool_ids is not None:
        persona_fields["tool_ids"] = req.tool_ids
    if persona_fields:
        await assistant_mod.update_assistant_model_config(profile, **persona_fields)

    quiet = None
    if req.quiet_hours is not None:
        quiet = {
            "enabled": bool(req.quiet_hours.get("enabled")),
            "start": str(req.quiet_hours.get("start") or "22:00")[:5],
            "end": str(req.quiet_hours.get("end") or "07:00")[:5],
            "urgent_override": bool(req.quiet_hours.get("urgent_override", True)),
        }
    await db.upsert_assistant_profile(
        timezone=req.timezone,
        enabled_gatherers=req.enabled_gatherers,
        allow_autonomous_email=req.allow_autonomous_email,
        location=req.location,
        quiet_hours=quiet,
    )

    # Timezone change moves every check-in's wall-clock → recompute next_run
    # for all of them, even the ones not mentioned in this PATCH. Disabled
    # check-ins get the new timezone too, or they re-enable on stale walls.
    if req.timezone is not None:
        for task in await db.list_scheduled_tasks():
            if task.get("task_type") != "check_in":
                continue
            nxt = scheduler.compute_next_run(
                task["schedule_kind"], task["schedule_json"], req.timezone)
            await db.update_scheduled_task(task["id"], {"timezone": req.timezone, "next_run": nxt})

    profile = await db.get_assistant_profile() or profile
    tz = req.timezone or profile.get("timezone") or "UTC"

    for ci in req.check_ins or []:
        if ci.id:
            task = await db.get_scheduled_task(ci.id)
            if not task or task.get("task_type") != "check_in":
                raise HTTPException(404, f"Check-in {ci.id} not found")
            if ci.delete:
                await db.delete_scheduled_task(ci.id)
                continue
            fields: dict = {}
            if ci.name is not None:
                fields["title"] = ci.name.strip()[:200]
            if ci.prompt is not None:
                fields["prompt"] = ci.prompt
            if ci.enabled is not None:
                fields["enabled"] = ci.enabled
            sched_changed = any(v is not None for v in
                                (ci.schedule_kind, ci.time, ci.weekday, ci.day, ci.cron))
            if sched_changed:
                kind, sj = _checkin_schedule(ci, task)
                fields["schedule_kind"] = kind
                fields["schedule_json"] = sj
            if sched_changed or ci.enabled:
                fields["next_run"] = scheduler.compute_next_run(
                    fields.get("schedule_kind", task["schedule_kind"]),
                    fields.get("schedule_json", task["schedule_json"]), tz)
            await db.update_scheduled_task(ci.id, fields)
        elif not ci.delete:
            kind, sj = _checkin_schedule(ci)
            await db.create_scheduled_task(
                f"task-{uuid.uuid4().hex[:10]}",
                title=(ci.name or "Check-in").strip()[:200],
                prompt=ci.prompt or "",
                task_type="check_in",
                schedule_kind=kind,
                schedule_json=sj,
                timezone=tz,
                next_run=scheduler.compute_next_run(kind, sj, tz),
                conversation_id=profile.get("conversation_id") or "",
                delivery_json={"conversation": True, "notify": True},
                enabled=ci.enabled if ci.enabled is not None else True,
            )

    return await _profile_payload()
