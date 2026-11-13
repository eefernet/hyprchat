"""Skill library routes: the owner's view of what HyprChat has learned.

Everything here is an explicit user action, so it bypasses the go-live policy that applies to the
model's own writes (``skills.save_from_agent``): a skill the owner writes or approves is active.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import database as db
import skills
from db import skill_store

router = APIRouter()


class SkillCreate(BaseModel):
    name: str
    description: str
    body: str
    category: str = ""


class SkillUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    body: Optional[str] = None
    category: Optional[str] = None
    pinned: Optional[bool] = None
    status: Optional[str] = None        # active | archived


class SkillImport(BaseModel):
    content: str


class RevertRequest(BaseModel):
    revision_id: int


class FileWrite(BaseModel):
    path: str
    content: str


class SettingsPatch(BaseModel):
    enabled: Optional[bool] = None
    auto_learn: Optional[bool] = None
    approval: Optional[str] = None
    model: Optional[str] = None
    index_chars: Optional[int] = None
    min_tool_calls: Optional[int] = None
    stale_days: Optional[int] = None
    archive_days: Optional[int] = None


def _checked(call, *args):
    try:
        return call(*args)
    except skills.SkillError as error:
        raise HTTPException(400, str(error))


async def _owned(skill_id: str) -> dict:
    skill = await skill_store.get(skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    return skill


def _summary(skill: dict) -> dict:
    return {**{k: v for k, v in skill.items() if k != "body"}, "body_chars": len(skill.get("body") or "")}


async def _detail(skill_id: str) -> dict:
    skill = await _owned(skill_id)
    skill["files"] = await skill_store.files(skill_id)
    skill["revisions"] = await skill_store.revisions(skill_id)
    return skill


# Fixed paths come before /api/skills/{skill_id} so they are not read as an id.

@router.get("/api/skills/settings")
async def get_skill_settings():
    return {**await skills.settings(), "limits": {"description": skills.MAX_DESCRIPTION, "body": skills.MAX_BODY,
                                                  "files": skills.MAX_FILES, "file_bytes": skills.MAX_FILE_BYTES}}


@router.put("/api/skills/settings")
async def put_skill_settings(req: SettingsPatch):
    patch = req.model_dump(exclude_unset=True)
    if patch.get("approval") not in (None, "auto", "review"):
        raise HTTPException(400, "approval must be auto or review")
    return await skills.save_settings(patch)


@router.post("/api/skills/import")
async def import_skill(req: SkillImport):
    parsed = _checked(skills.from_markdown, req.content)
    name, description, body = _checked(skills.validate, parsed["name"], parsed["description"], parsed["body"])
    flags = skills.scan(name, description, body)
    try:
        skill = await skill_store.create(name, description, body, category=parsed["category"], origin="import",
                                         status="draft" if flags else "active", scan_flags=flags)
    except ValueError as error:
        raise HTTPException(409, str(error))
    return await _detail(skill["id"])


@router.post("/api/skills/curate")
async def curate_skills(dry_run: bool = True):
    """What the daily upkeep would retire for this user (dry_run) or retire it now."""
    config = await skills.settings()
    moves = await skill_store.curate(db.current_user_id(), config["stale_days"], config["archive_days"], dry_run=dry_run)
    return {"dry_run": dry_run, "moves": moves}


@router.get("/api/skills")
async def list_skills(status: str = ""):
    wanted = [s for s in status.split(",") if s in skill_store.STATUSES] or None
    return [_summary(skill) for skill in await skill_store.list_skills(wanted)]


@router.post("/api/skills")
async def create_skill(req: SkillCreate):
    name, description, body = _checked(skills.validate, req.name, req.description, req.body)
    try:
        skill = await skill_store.create(name, description, body, category=skills.slug(req.category)[:24] or "general",
                                         origin="user", scan_flags=skills.scan(name, description, body))
    except ValueError as error:
        raise HTTPException(409, str(error))
    return await _detail(skill["id"])


@router.get("/api/skills/{skill_id}")
async def get_skill(skill_id: str):
    return await _detail(skill_id)


@router.patch("/api/skills/{skill_id}")
async def update_skill(skill_id: str, req: SkillUpdate):
    skill = await _owned(skill_id)
    if req.status is not None and req.status not in ("active", "archived"):
        raise HTTPException(400, "status must be active or archived")
    fields = {"pinned": req.pinned, "status": req.status}
    if req.name is not None and skills.slug(req.name) != skill["name"]:
        fields["name"] = skills.slug(req.name)
        if not skills.NAME.match(fields["name"]):
            raise HTTPException(400, "name must be 2-48 characters of lowercase letters, digits and hyphens")
    if req.description is not None or req.body is not None:
        _, description, body = _checked(
            skills.validate, skill["name"],
            skill["description"] if req.description is None else req.description,
            skill["body"] if req.body is None else req.body)
        await skill_store.update(skill_id, description=description, body=body, origin="user", reason="edited by you",
                                 category=None if req.category is None else skills.slug(req.category)[:24])
        fields["scan_flags"] = skills.scan(skill["name"], description, body)
    elif req.category is not None:
        await skill_store.update(skill_id, category=skills.slug(req.category)[:24])
    try:
        await skill_store.set_fields(skill_id, **fields)
    except ValueError as error:
        raise HTTPException(409, str(error))
    return await _detail(skill_id)


@router.delete("/api/skills/{skill_id}")
async def delete_skill(skill_id: str):
    if not await skill_store.delete(skill_id):
        raise HTTPException(404, "Skill not found")
    return {"status": "deleted"}


@router.post("/api/skills/{skill_id}/approve")
async def approve_skill(skill_id: str):
    """Make a draft live, or apply a change that was waiting for review."""
    skill = await _owned(skill_id)
    if skill.get("pending"):
        await skill_store.apply_pending(skill_id)
    if skill["status"] == "draft":
        await skill_store.set_fields(skill_id, status="active")
    return await _detail(skill_id)


@router.post("/api/skills/{skill_id}/reject")
async def reject_skill(skill_id: str):
    """Drop a waiting change; a rejected draft is archived so the same skill is not proposed again."""
    skill = await _owned(skill_id)
    if skill.get("pending"):
        await skill_store.set_pending(skill_id, None)
    if skill["status"] == "draft":
        await skill_store.set_fields(skill_id, status="archived")
    return await _detail(skill_id)


@router.post("/api/skills/{skill_id}/revert")
async def revert_skill(skill_id: str, req: RevertRequest):
    await _owned(skill_id)
    if not await skill_store.revert(skill_id, req.revision_id):
        raise HTTPException(404, "Revision not found")
    return await _detail(skill_id)


@router.get("/api/skills/{skill_id}/export")
async def export_skill(skill_id: str):
    skill = await _owned(skill_id)
    return PlainTextResponse(skills.to_markdown(skill), media_type="text/markdown", headers={
        "Content-Disposition": f'attachment; filename="{skill["name"]}.SKILL.md"'})


@router.get("/api/skills/{skill_id}/files")
async def get_skill_file(skill_id: str, path: str):
    await _owned(skill_id)
    item = await skill_store.get_file(skill_id, path)
    if not item:
        raise HTTPException(404, "File not found")
    return item


@router.put("/api/skills/{skill_id}/files")
async def put_skill_file(skill_id: str, req: FileWrite):
    await _owned(skill_id)
    path, content = _checked(skills.validate_file, req.path, req.content)
    existing = {item["path"] for item in await skill_store.files(skill_id)}
    if len(existing) >= skills.MAX_FILES and path not in existing:
        raise HTTPException(400, f"a skill holds at most {skills.MAX_FILES} files")
    await skill_store.put_file(skill_id, path, content)
    return await _detail(skill_id)


@router.delete("/api/skills/{skill_id}/files")
async def delete_skill_file(skill_id: str, path: str):
    await _owned(skill_id)
    if not await skill_store.delete_file(skill_id, path):
        raise HTTPException(404, "File not found")
    return await _detail(skill_id)
