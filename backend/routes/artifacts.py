"""Artifact Studio API routes."""
from __future__ import annotations

import asyncio
import base64
import csv
import html
import io
import json
import os
import re
import shlex
import shutil
import uuid
import zipfile
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
import database as db
import rag
from artifact_files import (
    archive_contents_for_path,
    archive_entry_preview_for_path,
    artifact_file_metadata,
    artifact_path_for_row,
    artifact_text_preview_allowed,
    decode_preview_bytes,
    extract_indexable_text,
    language_hint,
    render_markdown_safe,
    sanitize_preview_html,
)
from artifact_service import annotate_artifact_staleness, delete_artifact_row_and_file

from .context import route_context


router = APIRouter()


class ArtifactUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    workspace_id: Optional[str] = None
    workspace_ids: Optional[list[str]] = None
    status: Optional[str] = None
    pinned: Optional[bool] = None
    tags: Optional[list[str]] = None
    parent_artifact_id: Optional[str] = None
    supersedes_artifact_id: Optional[str] = None


class ArtifactUseInChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    max_chars: int = 20000


class ArtifactAddToKBRequest(BaseModel):
    kb_id: str
    filename: Optional[str] = None
    max_chars: int = 500000


class ArtifactResearchRequest(BaseModel):
    query: Optional[str] = None
    title: Optional[str] = None
    focus: str = ""
    report_type: str = "analyst"
    depth: Optional[int] = None
    model: str = ""
    planner_model: str = ""
    auditor_model: str = ""
    kb_ids: list[str] = []
    max_chars: int = 50000


class ArtifactReviseRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    filename: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[list[str]] = None
    workspace_ids: Optional[list[str]] = None


class ArtifactBundleRequest(BaseModel):
    artifact_ids: list[str]
    title: Optional[str] = None
    filename: Optional[str] = None
    workspace_ids: Optional[list[str]] = None


class ArtifactMergeDuplicateRequest(BaseModel):
    duplicate_id: str


@router.get("/api/artifacts")
async def list_artifacts_ep(
    conversation_id: Optional[str] = Query(None),
    workspace_id: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None),
    workflow_id: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    pinned: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(80, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    artifacts = await db.list_artifacts(
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        run_id=run_id,
        workflow_id=workflow_id,
        kind=kind,
        status=status,
        pinned=pinned,
        tag=tag,
        q=q,
        date_from=date_from,
        date_to=date_to,
        source=source,
        limit=limit,
        offset=offset,
    )
    return await annotate_artifact_staleness(artifacts)


@router.get("/api/artifacts/{artifact_id}")
async def get_artifact_ep(artifact_id: str):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    await annotate_artifact_staleness([artifact] + (artifact.get("versions") or []))
    return artifact


@router.get("/api/artifacts/{artifact_id}/download")
async def download_artifact_ep(artifact_id: str):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, safe_name = artifact_path_for_row(artifact)
    if not filepath:
        raise HTTPException(404, "Artifact file is missing")
    download_name = os.path.basename(artifact.get("filename") or "") or safe_name
    return FileResponse(filepath, filename=download_name)


@router.get("/api/artifacts/{artifact_id}/duplicates")
async def get_artifact_duplicates_ep(artifact_id: str):
    duplicates = await db.get_artifact_duplicates(artifact_id)
    if duplicates is None:
        raise HTTPException(404, "Artifact not found")
    return {"artifact_id": artifact_id, "duplicates": duplicates}


@router.get("/api/artifacts/{artifact_id}/timeline")
async def get_artifact_timeline_ep(artifact_id: str):
    timeline = await db.get_artifact_timeline(artifact_id)
    if timeline is None:
        raise HTTPException(404, "Artifact not found")
    return {"artifact_id": artifact_id, "events": timeline}


@router.post("/api/artifacts/{artifact_id}/merge-duplicate")
async def merge_artifact_duplicate_ep(artifact_id: str, req: ArtifactMergeDuplicateRequest):
    try:
        merged = await db.merge_duplicate_artifact(artifact_id, req.duplicate_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not merged:
        raise HTTPException(404, "Artifact not found")
    return merged


@router.patch("/api/artifacts/{artifact_id}")
async def update_artifact_ep(artifact_id: str, req: ArtifactUpdate):
    artifact = await db.update_artifact(
        artifact_id,
        **{k: v for k, v in req.dict(exclude_unset=True).items()},
    )
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    return artifact


@router.delete("/api/artifacts")
async def delete_all_artifacts_ep():
    targets: list[dict] = []
    offset = 0
    while True:
        page = await db.list_artifacts(limit=200, offset=offset)
        targets.extend(page)
        if len(page) < 200:
            break
        offset += 200
    deleted_artifacts = 0
    deleted_files = 0
    removed_attachments = 0
    for artifact in targets:
        result = await delete_artifact_row_and_file(artifact)
        if result["deleted"]:
            deleted_artifacts += 1
        if result["deleted_file"]:
            deleted_files += 1
        try:
            cf_id = (artifact.get("metadata") or {}).get("conversation_file_id") or ""
            if cf_id and await db.delete_conversation_file(cf_id):
                removed_attachments += 1
        except Exception:
            pass
    if deleted_artifacts:
        try:
            await db.vacuum_database()
        except Exception as e:
            print(f"[ARTIFACT] bulk vacuum failed: {e}")
    return {
        "status": "deleted",
        "deleted": deleted_artifacts,
        "deleted_files": deleted_files,
        "removed_attachments": removed_attachments,
    }


@router.delete("/api/artifacts/{artifact_id}")
async def delete_artifact_ep(artifact_id: str):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    result = await delete_artifact_row_and_file(artifact)
    if not result["deleted"]:
        raise HTTPException(404, "Artifact not found")
    return {"status": "deleted", "deleted_file": result["deleted_file"]}


@router.get("/api/artifacts/{artifact_id}/preview")
async def preview_artifact_ep(artifact_id: str, max_chars: int = Query(200000, ge=1000, le=1000000)):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, safe_name = artifact_path_for_row(artifact)
    if not filepath:
        updated = await db.update_artifact_file_metadata(artifact_id, exists_status="missing")
        return {
            **(updated or artifact),
            "id": artifact_id,
            "filename": safe_name or artifact.get("filename") or "",
            "kind": artifact.get("kind") or "file",
            "mime_type": artifact.get("mime_type") or "",
            "exists_status": "missing",
            "preview_type": "missing",
            "error": "File not found. It may have been cleaned up.",
        }

    kind = (artifact.get("kind") or "file").lower()
    lower_name = (safe_name or artifact.get("filename") or "").lower()
    file_meta = artifact_file_metadata(filepath)
    if kind == "archive":
        try:
            return {
                **artifact,
                "preview": archive_contents_for_path(filepath, safe_name),
                "preview_type": "archive",
                "file_size": file_meta["size_bytes"],
                "download_url": artifact.get("url"),
            }
        except HTTPException as e:
            return JSONResponse({"error": e.detail, "preview_type": "archive"}, status_code=e.status_code)
        except Exception as e:
            return JSONResponse({"error": str(e), "preview_type": "archive"}, status_code=500)
    if kind in {"image", "pdf"}:
        return {
            **artifact,
            "preview_type": kind,
            "file_size": file_meta["size_bytes"],
            "sha256": file_meta["sha256"],
            "download_url": artifact.get("url"),
        }
    if not artifact_text_preview_allowed(artifact):
        return {
            **artifact,
            "preview_type": "metadata",
            "file_size": file_meta["size_bytes"],
            "sha256": file_meta["sha256"],
            "message": "Binary preview is not available for this artifact type.",
        }
    try:
        def _read_preview_bytes():
            with open(filepath, "rb") as f:
                return f.read(min(max_chars * 4, 4 * 1024 * 1024))

        raw = await asyncio.to_thread(_read_preview_bytes)
        content = decode_preview_bytes(raw, {"content-type": artifact.get("mime_type") or ""})[:max_chars]
        truncated = os.path.getsize(filepath) > len(raw) or len(content) >= max_chars
        if lower_name.endswith((".csv", ".tsv")):
            delimiter = "\t" if lower_name.endswith(".tsv") else ","
            reader = csv.reader(io.StringIO(content), delimiter=delimiter)
            parsed = list(reader)
            columns = parsed[0] if parsed else []
            rows = parsed[1:201] if len(parsed) > 1 else []
            return {
                **artifact,
                "preview_type": "table",
                "table_type": "csv" if delimiter == "," else "tsv",
                "columns": columns,
                "rows": rows,
                "row_count": max(0, len(parsed) - 1),
                "truncated": truncated or len(parsed) > 201,
                "file_size": file_meta["size_bytes"],
            }
        if lower_name.endswith((".json", ".jsonl", ".ndjson")):
            parsed = None
            parse_error = ""
            try:
                if lower_name.endswith((".jsonl", ".ndjson")):
                    parsed = [json.loads(line) for line in content.splitlines() if line.strip()][:200]
                else:
                    parsed = json.loads(content)
            except Exception as e:
                parse_error = str(e)
            return {
                **artifact,
                "preview_type": "json",
                "content": content,
                "json": parsed,
                "parse_error": parse_error,
                "truncated": truncated,
                "file_size": file_meta["size_bytes"],
            }
        if kind == "markdown" or lower_name.endswith((".md", ".markdown", ".mdx")):
            return {
                **artifact,
                "preview_type": "markdown",
                "content": content,
                "html": render_markdown_safe(content),
                "truncated": truncated,
                "file_size": file_meta["size_bytes"],
            }
        if kind == "html" or lower_name.endswith((".html", ".htm")):
            title_match = re.search(r"<title[^>]*>(.*?)</title>", content, flags=re.I | re.S)
            return {
                **artifact,
                "preview_type": "html",
                "content": sanitize_preview_html(content, artifact.get("url") or ""),
                "metadata": {"title": html.unescape(re.sub(r"\s+", " ", title_match.group(1)).strip()) if title_match else ""},
                "download_url": artifact.get("url"),
                "truncated": truncated,
                "file_size": file_meta["size_bytes"],
            }
        return {
            **artifact,
            "preview_type": "text",
            "content": content,
            "language": language_hint(safe_name, kind),
            "truncated": truncated,
            "file_size": file_meta["size_bytes"],
        }
    except Exception as e:
        return JSONResponse({"error": f"Failed to read preview: {e}"}, status_code=500)


@router.get("/api/artifacts/{artifact_id}/archive-entry")
async def preview_artifact_archive_entry_ep(artifact_id: str, path: str = Query(..., min_length=1)):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    if (artifact.get("kind") or "").lower() != "archive":
        raise HTTPException(400, "Artifact is not an archive")
    filepath, safe_name = artifact_path_for_row(artifact)
    if not filepath:
        await db.update_artifact_file_metadata(artifact_id, exists_status="missing")
        raise HTTPException(404, "Artifact file is missing")
    try:
        return archive_entry_preview_for_path(filepath, safe_name or artifact.get("filename") or "", path)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to preview archive entry: {e}")


@router.post("/api/artifacts/{artifact_id}/check-file")
async def check_artifact_file_ep(artifact_id: str):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, _safe_name = artifact_path_for_row(artifact)
    if not filepath:
        updated = await db.update_artifact_file_metadata(artifact_id, exists_status="missing")
        return updated or {**artifact, "exists_status": "missing"}
    meta = artifact_file_metadata(filepath)
    content_text = extract_indexable_text(filepath, artifact.get("kind") or "", artifact.get("mime_type") or "")
    updated = await db.update_artifact_file_metadata(
        artifact_id,
        storage_path=filepath,
        size_bytes=meta["size_bytes"],
        sha256=meta["sha256"],
        exists_status="present",
        content_text=content_text,
    )
    return updated


@router.post("/api/artifacts/{artifact_id}/use-in-chat")
async def use_artifact_in_chat_ep(artifact_id: str, req: ArtifactUseInChatRequest):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, _safe_name = artifact_path_for_row(artifact)
    content = (artifact.get("content_text") or "").strip()
    if not content and filepath:
        content = extract_indexable_text(filepath, artifact.get("kind") or "", artifact.get("mime_type") or "", max_chars=req.max_chars)
    if not content:
        content = f"[Artifact: {artifact.get('title') or artifact.get('filename')} ({artifact.get('kind') or 'file'})]"
    content = content[: max(1000, min(req.max_chars or 20000, 100000))]
    attachment = {
        "name": artifact.get("title") or artifact.get("filename") or artifact_id,
        "content": content,
        "type": "text",
        "artifact_id": artifact_id,
        "url": artifact.get("url"),
    }
    return {
        "artifact_id": artifact_id,
        "conversation_id": req.conversation_id,
        "attachment": attachment,
        "context": f"Artifact: {attachment['name']}\n{content}",
    }


@router.post("/api/artifacts/{artifact_id}/add-to-kb")
async def add_artifact_to_kb_ep(artifact_id: str, req: ArtifactAddToKBRequest):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    owned = await db.get_kb(req.kb_id)
    if not owned:
        raise HTTPException(404, "KB not found")
    filepath, _safe_name = artifact_path_for_row(artifact)
    content = (artifact.get("content_text") or "").strip()
    if not content and filepath:
        content = extract_indexable_text(filepath, artifact.get("kind") or "", artifact.get("mime_type") or "", max_chars=req.max_chars)
    if not content or (artifact.get("kind") or "").lower() in {"image", "pdf"}:
        raise HTTPException(400, "Artifact does not have safe text content to import")
    kb_dir = os.path.join(config.KB_DIR, req.kb_id)
    os.makedirs(kb_dir, exist_ok=True)
    base_name = os.path.basename(req.filename or artifact.get("filename") or f"{artifact_id}.txt")
    if not os.path.splitext(base_name)[1]:
        base_name += ".txt"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", base_name)[:180] or f"{artifact_id}.txt"
    if not safe_name.lower().endswith((".txt", ".md", ".csv", ".json", ".jsonl", ".html", ".htm", ".py", ".js", ".ts", ".css", ".sh", ".yaml", ".yml", ".toml", ".xml")):
        safe_name += ".txt"
    dest = os.path.abspath(os.path.join(kb_dir, safe_name))
    if not dest.startswith(os.path.abspath(kb_dir) + os.sep):
        raise HTTPException(400, "Invalid KB filename")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(content[: max(1000, min(req.max_chars or 500000, 1000000))])
    file_id = await db.add_kb_file(req.kb_id, safe_name, dest, os.path.getsize(dest), "text/plain")

    async def _bg_index_artifact_import():
        try:
            await rag.index_file(req.kb_id, safe_name, dest)
        except Exception as e:
            print(f"[RAG] Artifact KB import indexing failed for {safe_name}: {e}")

    route_context().track_bg(_bg_index_artifact_import())
    await db.add_artifact_event(
        artifact_id,
        "added_to_kb",
        f"Added to knowledge base {owned.get('name') or req.kb_id}",
        {"kb_id": req.kb_id, "kb_name": owned.get("name"), "file_id": file_id, "filename": safe_name},
    )
    return {"artifact_id": artifact_id, "kb_id": req.kb_id, "file_id": file_id, "filename": safe_name, "indexing": True}


@router.post("/api/artifacts/{artifact_id}/send-to-research")
async def send_artifact_to_research_ep(artifact_id: str, req: ArtifactResearchRequest):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, _safe_name = artifact_path_for_row(artifact)
    content = (artifact.get("content_text") or "").strip()
    if not content and filepath:
        content = extract_indexable_text(filepath, artifact.get("kind") or "", artifact.get("mime_type") or "", max_chars=req.max_chars)
    title = req.title or f"Research: {artifact.get('title') or artifact.get('filename')}"
    query = req.query or f"Analyze this artifact: {artifact.get('title') or artifact.get('filename')}"
    report = await route_context().create_research_report({
        "title": title,
        "query": query,
        "focus": req.focus or f"Use artifact {artifact_id} as primary context.",
        "report_type": req.report_type or "analyst",
        "depth": req.depth,
        "model": req.model,
        "planner_model": req.planner_model,
        "auditor_model": req.auditor_model,
        "kb_ids": req.kb_ids,
        "inputs": [{
            "type": "artifact",
            "artifact_id": artifact_id,
            "title": artifact.get("title") or artifact.get("filename"),
            "filename": artifact.get("filename"),
            "content": content[: max(1000, min(req.max_chars or 50000, 200000))],
        }],
    })
    await db.add_artifact_event(
        artifact_id,
        "sent_to_research",
        f"Sent to research report {report.get('title') or report.get('id') or ''}".strip(),
        {"report_id": report.get("id"), "title": report.get("title"), "query": query},
    )
    return report


@router.post("/api/artifacts/{artifact_id}/fork-to-codebox")
async def fork_artifact_to_codebox_ep(artifact_id: str):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, safe_name = artifact_path_for_row(artifact)
    if not filepath:
        raise HTTPException(404, "Artifact file is missing")
    size = os.path.getsize(filepath)
    if size > config.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, f"Artifact exceeds {config.MAX_UPLOAD_SIZE_MB}MB Codebox copy limit")

    def _read_b64():
        with open(filepath, "rb") as f:
            return base64.b64encode(f.read()).decode()

    b64 = await asyncio.to_thread(_read_b64)
    remote_dir = f"/root/artifacts/{shlex.quote(artifact_id)}"
    remote_path = f"/root/artifacts/{artifact_id}/{safe_name}"
    cmd = f"mkdir -p {remote_dir} && printf '%s' {shlex.quote(b64)} | base64 -d > {shlex.quote(remote_path)} && ls -lah {shlex.quote(remote_path)}"
    try:
        r = await route_context().http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 60}, timeout=70)
        data = r.json()
        if r.status_code >= 400 or data.get("exit_code", 0) != 0:
            raise HTTPException(502, data.get("stderr") or "Codebox copy failed")
        result = {
            "artifact_id": artifact_id,
            "path": remote_path,
            "project_path": f"/root/artifacts/{artifact_id}",
            "filename": safe_name,
            "size_bytes": size,
            "stdout": data.get("stdout", "")[:1000],
        }
        await db.add_artifact_event(
            artifact_id,
            "forked_to_codebox",
            f"Copied to Codebox at {remote_path}",
            {"path": remote_path, "project_path": result["project_path"], "size_bytes": size},
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Codebox copy failed: {e}")


@router.post("/api/artifacts/{artifact_id}/revise")
async def revise_artifact_ep(artifact_id: str, req: ArtifactReviseRequest):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, safe_name = artifact_path_for_row(artifact)
    os.makedirs(config.SANDBOX_OUTPUTS_DIR, exist_ok=True)
    filename = os.path.basename(req.filename or safe_name or artifact.get("filename") or f"{artifact_id}.txt")
    stem, ext = os.path.splitext(filename)
    if not ext:
        ext = ".txt"
    new_name = f"{stem}-revision-{uuid.uuid4().hex[:6]}{ext}"
    dest = os.path.abspath(os.path.join(config.SANDBOX_OUTPUTS_DIR, new_name))
    if not dest.startswith(os.path.abspath(config.SANDBOX_OUTPUTS_DIR) + os.sep):
        raise HTTPException(400, "Invalid revision filename")
    if req.content is not None:
        with open(dest, "w", encoding="utf-8") as f:
            f.write(req.content)
    elif filepath:
        shutil.copy2(filepath, dest)
    else:
        raise HTTPException(404, "Original artifact file is missing; provide content to revise")
    mime_type = db.artifact_mime_type_for_filename(new_name)
    kind = db.artifact_kind_for_filename(new_name, mime_type)
    meta = artifact_file_metadata(dest)
    content_text = extract_indexable_text(dest, kind, mime_type)
    parent_id = artifact.get("parent_artifact_id") or artifact["id"]
    new_artifact = await db.add_artifact(
        conversation_id=artifact.get("conversation_id"),
        message_id=artifact.get("message_id"),
        workspace_ids=req.workspace_ids if req.workspace_ids is not None else artifact.get("workspace_ids", []),
        run_id=artifact.get("run_id"),
        workflow_id=artifact.get("workflow_id"),
        filename=new_name,
        url=f"/api/downloads/{new_name}",
        kind=kind,
        mime_type=mime_type,
        title=req.title or f"{artifact.get('title') or artifact.get('filename')} revision",
        description=req.description if req.description is not None else artifact.get("description", ""),
        storage_path=dest,
        size_bytes=meta["size_bytes"],
        sha256=meta["sha256"],
        exists_status="present",
        parent_artifact_id=parent_id,
        supersedes_artifact_id=artifact["id"],
        content_text=content_text,
        tags=req.tags if req.tags is not None else artifact.get("tags", []),
        metadata={**(artifact.get("metadata") or {}), "source_tool": "artifact_revise", "revised_from": artifact["id"]},
    )
    await db.add_artifact_event(
        artifact["id"],
        "revised",
        f"Created revision {new_artifact.get('id')}",
        {"related_artifact_id": new_artifact.get("id"), "filename": new_name},
    )
    await db.add_artifact_event(
        new_artifact["id"],
        "supersedes",
        f"Supersedes {artifact['id']}",
        {"related_artifact_id": artifact["id"], "parent_artifact_id": parent_id},
    )
    return new_artifact


@router.post("/api/artifacts/bundle")
async def bundle_artifacts_ep(req: ArtifactBundleRequest):
    ids = []
    seen = set()
    for artifact_id in req.artifact_ids or []:
        if artifact_id and artifact_id not in seen:
            seen.add(artifact_id)
            ids.append(artifact_id)
    if not ids:
        raise HTTPException(400, "No artifacts selected")
    artifacts = []
    for artifact_id in ids[:100]:
        artifact = await db.get_artifact(artifact_id)
        if not artifact:
            raise HTTPException(404, f"Artifact not found: {artifact_id}")
        filepath, safe_name = artifact_path_for_row(artifact)
        if not filepath:
            raise HTTPException(404, f"Artifact file is missing: {artifact.get('filename') or artifact_id}")
        artifacts.append((artifact, filepath, safe_name or os.path.basename(filepath)))
    os.makedirs(config.SANDBOX_OUTPUTS_DIR, exist_ok=True)
    raw_name = os.path.basename(req.filename or (req.title or f"artifact-bundle-{uuid.uuid4().hex[:6]}.zip"))
    if not raw_name.lower().endswith(".zip"):
        raw_name += ".zip"
    safe_bundle = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name)[:180] or f"artifact-bundle-{uuid.uuid4().hex[:6]}.zip"
    dest = os.path.abspath(os.path.join(config.SANDBOX_OUTPUTS_DIR, safe_bundle))
    if not dest.startswith(os.path.abspath(config.SANDBOX_OUTPUTS_DIR) + os.sep):
        raise HTTPException(400, "Invalid bundle filename")
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        used_names = set()
        for artifact, filepath, safe_name in artifacts:
            arcname = safe_name
            if arcname in used_names:
                stem, ext = os.path.splitext(arcname)
                arcname = f"{stem}-{artifact['id']}{ext}"
            used_names.add(arcname)
            zf.write(filepath, arcname)
    meta = artifact_file_metadata(dest)
    content_text = extract_indexable_text(dest, "archive", "application/zip")
    workspace_ids = req.workspace_ids
    if workspace_ids is None:
        ws_seen = set()
        workspace_ids = []
        for artifact, _path, _safe in artifacts:
            for ws_id in artifact.get("workspace_ids", []):
                if ws_id not in ws_seen:
                    ws_seen.add(ws_id)
                    workspace_ids.append(ws_id)
    bundle = await db.add_artifact(
        filename=safe_bundle,
        url=f"/api/downloads/{safe_bundle}",
        kind="archive",
        mime_type="application/zip",
        title=req.title or "Artifact bundle",
        description=f"Bundle of {len(artifacts)} artifact(s).",
        storage_path=dest,
        size_bytes=meta["size_bytes"],
        sha256=meta["sha256"],
        exists_status="present",
        status="draft",
        workspace_ids=workspace_ids,
        content_text=content_text,
        tags=["bundle"],
        metadata={"source_tool": "artifact_bundle", "artifact_ids": ids[:100]},
    )
    await db.add_artifact_event(
        bundle["id"],
        "bundled_from",
        f"Bundled {len(artifacts)} artifact(s)",
        {"related_artifact_ids": ids[:100]},
    )
    for artifact, _path, _safe in artifacts:
        await db.add_artifact_event(
            artifact["id"],
            "included_in_bundle",
            f"Included in bundle {bundle.get('id')}",
            {"related_artifact_id": bundle.get("id"), "filename": safe_bundle},
        )
    return bundle
