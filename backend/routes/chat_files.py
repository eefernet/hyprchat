"""Chat data-file staging routes.

Uploaded data files (CSV/Excel/JSON) from the chat composer are pushed into
the Codebox sandbox at /root/chat_files/{conversation_id}/{upload_id}/{filename} so the
always-available execute_code tool can read the FULL file from disk. The
composer then references the sandbox path in the prompt instead of inlining
(and truncating) the file's text — which forced the model to re-type the data
into its tool calls.
"""
from __future__ import annotations

import asyncio
import re
import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import config
import database as db

from .context import route_context

router = APIRouter()

# 1MB of raw bytes per multipart POST — same transport as the coder-project
# upload: /upload-chunk writes bytes directly (no shell), so there is no
# MAX_ARG_STRLEN limit and no deny-list false positives on binary content.
_CHUNK = 1_000_000
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_CONV_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def safe_filename(name: str) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE_CHARS.sub("_", base).strip("._") or "file"
    if len(base) <= 120:
        return base
    stem, dot, suffix = base.rpartition(".")
    if dot and len(suffix) <= 16:
        return stem[:119 - len(suffix)] + dot + suffix
    return base[:120]


async def upload_bytes_to_codebox(http, remote_path: str, data: bytes) -> None:
    total = max(1, (len(data) + _CHUNK - 1) // _CHUNK)
    for i in range(total):
        chunk = data[i * _CHUNK : (i + 1) * _CHUNK]
        r = await http.post(
            f"{config.CODEBOX_URL}/upload-chunk",
            files={"file": (f"chunk-{i}", chunk, "application/octet-stream")},
            data={"path": remote_path, "truncate": "true" if i == 0 else "false"},
            timeout=60,
        )
        if r.status_code != 200:
            raise HTTPException(
                502,
                f"Sandbox upload failed on chunk {i + 1}/{total}: "
                f"HTTP {r.status_code} — {r.text[:200]}",
            )


@router.post("/api/chat-files/stage")
async def stage_chat_file(file: UploadFile = File(...), conversation_id: str = Form(...)):
    conv_id = (conversation_id or "").strip()
    if not _CONV_ID_RE.match(conv_id):
        raise HTTPException(400, "Invalid conversation_id")
    if not await db.get_conversation(conv_id):
        raise HTTPException(404, "Conversation not found")

    limit = config.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    data = await file.read(limit + 1)
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > limit:
        raise HTTPException(413, f"File too large (max {config.MAX_UPLOAD_SIZE_MB}MB)")

    name = safe_filename(file.filename or "")
    remote_path = f"/root/chat_files/{conv_id}/{uuid.uuid4().hex}/{name}"
    ctx = route_context()
    try:
        await upload_bytes_to_codebox(ctx.http, remote_path, data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Sandbox unreachable: {str(e)[:200]}")

    # Warm the sandbox data stack (pandas/openpyxl) in the background so it's
    # installed by the time the model's execute_code call arrives.
    from tooling import codebox_tools
    warm = codebox_tools.ensure_data_stack(ctx.http)
    if ctx.track_bg:
        ctx.track_bg(warm)
    else:
        asyncio.ensure_future(warm)

    print(f"[CHAT-FILES] staged {name} ({len(data)} bytes) -> {remote_path}")
    return {"name": name, "sandbox_path": remote_path, "size_bytes": len(data)}
