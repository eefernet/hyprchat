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
    delete: bool = False


class AssistantUpdate(BaseModel):
    name: Optional[str] = None
    personality: Optional[str] = None
    model: Optional[str] = None
    tool_ids: Optional[list[str]] = None
    timezone: Optional[str] = None
    enabled_gatherers: Optional[list[str]] = None
    allow_autonomous_email: Optional[bool] = None
    check_ins: Optional[list[CheckInUpdate]] = None


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
        await db.update_model_config(profile["model_config_id"], **persona_fields)

    await db.upsert_assistant_profile(
        timezone=req.timezone,
        enabled_gatherers=req.enabled_gatherers,
        allow_autonomous_email=req.allow_autonomous_email,
    )

    # Timezone change moves every check-in's wall-clock → recompute next_run
    # for all of them, even the ones not mentioned in this PATCH.
    if req.timezone is not None:
        for task in await db.list_scheduled_tasks():
            if task.get("task_type") != "check_in" or not task["enabled"]:
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
            if ci.time is not None:
                fields["schedule_json"] = {"time": ci.time}
            if ci.time is not None or ci.enabled:
                fields["next_run"] = scheduler.compute_next_run(
                    "daily", fields.get("schedule_json", task["schedule_json"]), tz)
            await db.update_scheduled_task(ci.id, fields)
        elif not ci.delete:
            time_str = ci.time or "08:30"
            await db.create_scheduled_task(
                f"task-{uuid.uuid4().hex[:10]}",
                title=(ci.name or "Check-in").strip()[:200],
                prompt=ci.prompt or "",
                task_type="check_in",
                schedule_kind="daily",
                schedule_json={"time": time_str},
                timezone=tz,
                next_run=scheduler.compute_next_run("daily", {"time": time_str}, tz),
                conversation_id=profile.get("conversation_id") or "",
                delivery_json={"conversation": True, "notify": True},
                enabled=ci.enabled if ci.enabled is not None else True,
            )

    return await _profile_payload()
