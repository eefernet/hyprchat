"""Jarvis notes/todos routes."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database as db
import pim
import workspace_service

router = APIRouter()


class NoteCreate(BaseModel):
    kind: str = "note"            # note | todo
    title: str
    content: str = ""
    due_local: str = ""           # YYYY-MM-DDTHH:MM in the assistant timezone
    remind_local: str = ""
    tags: list[str] = []
    workspace_ids: list[str] = []


class NoteUpdate(BaseModel):
    kind: Optional[str] = None          # note | todo
    title: Optional[str] = None
    content: Optional[str] = None
    done: Optional[bool] = None
    due_local: Optional[str] = None     # "" clears
    remind_local: Optional[str] = None  # "" clears
    tags: Optional[list[str]] = None
    workspace_ids: Optional[list[str]] = None


@router.get("/api/notes")
async def list_notes(kind: str = "", include_done: bool = True, workspace_id: str = "", tag: str = "", offset: int = 0, limit: int = 200):
    tz = await pim.user_timezone()
    notes = await db.list_notes(kind=kind, include_done=include_done,workspace_id=workspace_id,tag=tag,offset=offset,limit=max(1,min(limit,200)))
    for n in notes:
        n["workspace_ids"] = await workspace_service.note_workspaces(n["id"])
        n["due_local"] = pim.utc_to_local(n.get("due_at") or "", tz)
        n["remind_local"] = pim.utc_to_local(n.get("remind_at") or "", tz)
    return [n for n in notes if (not workspace_id or workspace_id in n["workspace_ids"]) and (not tag or tag in n.get("tags",[]))]


@router.get("/api/notes/{note_id}")
async def get_note(note_id: str):
    note = await db.get_note(note_id)
    if not note: raise HTTPException(404, "Note not found")
    tz = await pim.user_timezone()
    note['due_local'] = pim.utc_to_local(note.get('due_at') or '',tz)
    note['remind_local'] = pim.utc_to_local(note.get('remind_at') or '',tz)
    note['workspace_ids'] = await workspace_service.note_workspaces(note_id)
    return note


async def validate_workspaces(ids):
    for ws_id in ids:
        if not await db.get_workspace_basic(ws_id): raise HTTPException(404, "Workspace not found")


@router.post("/api/notes")
async def create_note(req: NoteCreate):
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "title is required")
    await validate_workspaces(req.workspace_ids)
    note = await pim.create_note(kind=req.kind, title=title, content=req.content,
                                 due_local=req.due_local, remind_local=req.remind_local,
                                 tags=req.tags)
    await workspace_service.set_note_workspaces(note["id"], req.workspace_ids)
    return await get_note(note["id"])


@router.patch("/api/notes/{note_id}")
async def update_note(note_id: str, req: NoteUpdate):
    if not await db.get_note(note_id):
        raise HTTPException(404, "Note not found")
    if req.workspace_ids is not None: await validate_workspaces(req.workspace_ids)
    tz = await pim.user_timezone()
    fields: dict = {}
    if req.kind is not None and req.kind not in {"note", "todo"}:
        raise HTTPException(400, "kind must be note or todo")
    for key in ("kind", "title", "content", "done", "tags"):
        value = getattr(req, key)
        if value is not None:
            fields[key] = value
    if req.due_local is not None:
        fields["due_at"] = pim.local_to_utc(req.due_local, tz)
    if req.remind_local is not None:
        fields["remind_at"] = pim.local_to_utc(req.remind_local, tz)
        fields["reminded"] = False
    await db.update_note(note_id, fields)
    if req.workspace_ids is not None: await workspace_service.set_note_workspaces(note_id,req.workspace_ids)
    return await get_note(note_id)


@router.delete("/api/notes/{note_id}")
async def delete_note(note_id: str):
    if not await db.delete_note(note_id):
        raise HTTPException(404, "Note not found")
    return {"status": "deleted"}
