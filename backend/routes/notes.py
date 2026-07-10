"""Jarvis notes/todos routes."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database as db
import pim

router = APIRouter()


class NoteCreate(BaseModel):
    kind: str = "note"            # note | todo
    title: str
    content: str = ""
    due_local: str = ""           # YYYY-MM-DDTHH:MM in the assistant timezone
    remind_local: str = ""
    tags: list[str] = []


class NoteUpdate(BaseModel):
    kind: Optional[str] = None          # note | todo
    title: Optional[str] = None
    content: Optional[str] = None
    done: Optional[bool] = None
    due_local: Optional[str] = None     # "" clears
    remind_local: Optional[str] = None  # "" clears
    tags: Optional[list[str]] = None


@router.get("/api/notes")
async def list_notes(kind: str = "", include_done: bool = True):
    tz = await pim.user_timezone()
    notes = await db.list_notes(kind=kind, include_done=include_done)
    for n in notes:
        n["due_local"] = pim.utc_to_local(n.get("due_at") or "", tz)
        n["remind_local"] = pim.utc_to_local(n.get("remind_at") or "", tz)
    return notes


@router.post("/api/notes")
async def create_note(req: NoteCreate):
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "title is required")
    return await pim.create_note(kind=req.kind, title=title, content=req.content,
                                 due_local=req.due_local, remind_local=req.remind_local,
                                 tags=req.tags)


@router.patch("/api/notes/{note_id}")
async def update_note(note_id: str, req: NoteUpdate):
    if not await db.get_note(note_id):
        raise HTTPException(404, "Note not found")
    tz = await pim.user_timezone()
    fields: dict = {}
    for key in ("title", "content", "done", "tags"):
        value = getattr(req, key)
        if value is not None:
            fields[key] = value
    if req.kind in ("note", "todo"):
        fields["kind"] = req.kind
    if req.due_local is not None:
        fields["due_at"] = pim.local_to_utc(req.due_local, tz)
    if req.remind_local is not None:
        fields["remind_at"] = pim.local_to_utc(req.remind_local, tz)
        fields["reminded"] = False
    return await db.update_note(note_id, fields)


@router.delete("/api/notes/{note_id}")
async def delete_note(note_id: str):
    if not await db.delete_note(note_id):
        raise HTTPException(404, "Note not found")
    return {"status": "deleted"}
