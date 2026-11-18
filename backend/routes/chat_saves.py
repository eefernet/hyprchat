"""Save what the preview pane shows (a page, a document, a table) as a file of its chat.

A previewed page or document exists only as text inside a reply. The pane's Save button sends that
text here; it is written to the chat's own folder (output_paths.py), recorded as one of the chat's
files and so listed in Artifact Studio. Saving the same content again returns the file that already
exists instead of making a second one.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database as db
import output_paths
from artifact_files import artifact_file_metadata, extract_indexable_text

router = APIRouter()

MAX_BYTES = 2 * 1024 * 1024
# Text formats the pane can show. Nothing executable is produced by saving: the file is stored, not run.
ALLOWED = {".html", ".htm", ".md", ".markdown", ".svg", ".csv", ".tsv", ".json", ".txt"}
DEFAULT_EXT = {"html": ".html", "markdown": ".md", "svg": ".svg", "table": ".csv", "json": ".json", "text": ".txt"}
SOURCE = "preview_save"


class SavePreviewRequest(BaseModel):
    filename: str = ""
    content: str
    kind: str = ""                      # html | markdown | svg | table | json | text (used when the name has no extension)
    message_id: Optional[int] = None


def save_name(filename: str, kind: str) -> str:
    """A safe file name with one of the allowed extensions, or a 400."""
    name = output_paths.clean_name(filename or "preview")
    stem, ext = os.path.splitext(name)
    if not ext:
        ext = DEFAULT_EXT.get(str(kind or "").lower(), "")
        name = (stem or "preview") + ext
    if ext.lower() not in ALLOWED:
        raise HTTPException(400, "Only pages, documents and tables can be saved from the preview (html, md, svg, csv, tsv, json, txt)")
    return name


async def _existing(conv_id: str, sha256: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT id, metadata_json FROM artifacts WHERE conversation_id=? AND sha256=? AND user_id=? ORDER BY created_at DESC",
            (conv_id, sha256, db.current_user_id()),
        )
    finally:
        await conn.close()
    for row in rows:
        if f'"{SOURCE}"' in (row["metadata_json"] or ""):
            return await db.get_artifact(row["id"])
    return None


@router.post("/api/conversations/{conv_id}/saved-previews")
async def save_preview(conv_id: str, body: SavePreviewRequest):
    if not await db.get_conversation(conv_id):      # user-scoped: another profile's chat is "not found"
        raise HTTPException(404, "Conversation not found")
    data = (body.content or "").encode("utf-8")
    if not data.strip():
        raise HTTPException(400, "There is nothing to save")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "This is larger than 2 MB; use Download instead")
    name = save_name(body.filename, body.kind)
    sha256 = hashlib.sha256(data).hexdigest()
    existing = await _existing(conv_id, sha256)
    if existing:
        return {"artifact": existing, "already_saved": True}
    spot = output_paths.place(conv_id, name)
    with open(spot.path, "wb") as handle:
        handle.write(data)
    try:
        meta = artifact_file_metadata(spot.path)
        mime = db.artifact_mime_type_for_filename(spot.filename)
        kind = db.artifact_kind_for_filename(spot.filename, mime)
        artifact = await db.add_conversation_file(
            f"cf-{uuid.uuid4().hex[:8]}", conv_id, spot.filename, spot.url,
            message_id=body.message_id, kind=kind, mime_type=mime, storage_path=spot.path,
            size_bytes=meta["size_bytes"], sha256=meta["sha256"], exists_status="present", status="draft",
            content_text=extract_indexable_text(spot.path, kind, mime),
            metadata={"source_tool": SOURCE, "size_bytes": meta["size_bytes"], "sha256": meta["sha256"]},
        )
    except Exception:
        try: os.unlink(spot.path)
        except OSError: pass
        raise
    if not artifact:
        try: os.unlink(spot.path)
        except OSError: pass
        raise HTTPException(500, "The file could not be recorded")
    return {"artifact": artifact, "already_saved": False}
