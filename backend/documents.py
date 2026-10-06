"""Owned document artifacts and cancellable Codebox jobs."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shlex
import uuid
from pathlib import Path

from fastapi import HTTPException

import cancel_registry
import config
import database as db
from artifact_files import artifact_path_for_row
from document_formats import OFFICE_EXTS, LEGACY_EXTS, MAX_BYTES, extract_text, inspect_document

INSTALL = "/opt/hyprchat-documents"
JOB_ROOT = "/root/hyprchat-documents/jobs"
DOCUMENT_KINDS = {"document", "presentation", "spreadsheet"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
_tasks: dict[str, asyncio.Task] = {}
_events = None


def configure(event_bus):
    global _events
    _events = event_bus


async def shutdown():
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def stop_worker(http, run_id):
    """A cancellation request is not proof that the remote process stopped."""
    async def confirm():
        state = await worker(http, "cancel", run_id)
        while state.get("status") not in {"succeeded", "failed", "cancelled"}:
            await asyncio.sleep(.2)
            state = await worker(http, "status", run_id)
    try:
        await asyncio.wait_for(confirm(), timeout=8)
        return ""
    except Exception:
        return "Worker stop is unconfirmed. Retry Stop to check again."


def enabled():
    try:
        settings = json.loads(Path(config.SETTINGS_PATH).read_text())
    except (OSError, ValueError):
        settings = {}
    return settings.get("document_tools_enabled", config.DEFAULT_SETTINGS.get("document_tools_enabled", False)) is True


def require_enabled():
    if not enabled():
        raise HTTPException(503, "Documents is disabled. Enable it in Settings after installing the document worker.")


async def worker(http, command, *args):
    cmd = " ".join(shlex.quote(str(x)) for x in [f"{INSTALL}/venv/bin/python", f"{INSTALL}/document_worker.py", command, *args])
    response = await http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 25}, timeout=35)
    response.raise_for_status()
    result = response.json()
    if result.get("exit_code", 0) != 0:
        raise RuntimeError("Document worker unavailable; verify installation on Codebox")
    try:
        return json.loads(result.get("stdout") or "{}")
    except ValueError as e:
        raise RuntimeError("Invalid document worker response") from e


async def owned_source(artifact_id):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Document not found")
    path, _ = artifact_path_for_row(artifact)
    if not path:
        raise HTTPException(404, "Document file is missing")
    return artifact, Path(path)


async def store_file(data, filename, *, conversation_id, artifact_id=None, run_id=None,
                     source=None, role="document", metadata=None, content_text="", _connection=None):
    from routes.chat_files import safe_filename
    filename = safe_filename(filename)
    aid = artifact_id or "art-" + uuid.uuid4().hex[:20]
    storage = Path(config.SANDBOX_OUTPUTS_DIR) / ("hc-document-" + uuid.uuid4().hex + Path(filename).suffix.lower())
    storage.parent.mkdir(parents=True, exist_ok=True)
    temporary = storage.with_suffix(storage.suffix + ".tmp")
    await asyncio.to_thread(temporary.write_bytes, data)
    os.replace(temporary, storage)
    try:
        artifact = await db.add_artifact(
            artifact_id=aid, conversation_id=conversation_id, run_id=run_id,
            filename=filename, url=f"/api/documents/files/{aid}", storage_path=str(storage),
            size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), exists_status="present", status="draft",
            parent_artifact_id=(source or {}).get("id"),
            supersedes_artifact_id=(source or {}).get("id") if role == "revision" else None,
            content_text=content_text,
            metadata={"document_managed": True, "document_role": role, **(metadata or {})},
            _connection=_connection,
        )
        if not artifact:
            raise HTTPException(404, "Conversation or source artifact not found")
        return artifact
    except BaseException:
        storage.unlink(missing_ok=True)
        raise


async def upload_document(http, file, conversation_id):
    require_enabled()
    if not await db.get_conversation(conversation_id):
        raise HTTPException(404, "Conversation not found")
    name = file.filename or ""
    ext = Path(name).suffix.lower()
    if ext not in OFFICE_EXTS | LEGACY_EXTS | IMAGE_EXTS:
        raise HTTPException(415, "Upload DOCX, PPTX, XLSX, DOC, PPT, XLS, PNG, or JPEG; encrypted/macro-enabled files are unsupported")
    data = await file.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, "File must contain between 1 byte and 50 MB")
    if ext in LEGACY_EXTS and not data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        raise HTTPException(415, "Legacy Office file has an invalid signature")
    if ext in IMAGE_EXTS and not (data.startswith(b"\x89PNG\r\n\x1a\n") if ext == ".png" else data.startswith(b"\xff\xd8\xff")):
        raise HTTPException(415, "Image signature does not match its extension")
    # Inspect before publishing a database row. Originals use opaque, unique storage.
    import tempfile
    text, inspection = "", {}
    with tempfile.NamedTemporaryFile(suffix=ext) as temp:
        temp.write(data)
        temp.flush()
        if ext in OFFICE_EXTS:
            try:
                text = await asyncio.to_thread(extract_text, temp.name, 200000)
                inspection = await asyncio.to_thread(inspect_document, temp.name, limit=100)
            except Exception as e:
                raise HTTPException(422, str(e)) from e
    artifact = await store_file(data, name, conversation_id=conversation_id, role="original", content_text=text,
                                metadata={"inspection": inspection, "warnings": inspection.get("warnings", [])})
    result = {"artifact_id": artifact["id"], "name": artifact["filename"], "sha256": artifact["sha256"],
              "size_bytes": len(data), "url": artifact["url"]}
    if ext in {".xlsx", ".xls"}:
        # Retain the existing execute_code analysis capability without retyping data.
        from routes.chat_files import upload_bytes_to_codebox, safe_filename
        path = f"/root/chat_files/{conversation_id}/{uuid.uuid4().hex}/{safe_filename(name)}"
        try:
            await upload_bytes_to_codebox(http, path, data)
            result["sandbox_path"] = path
        except Exception:
            result["warning"] = "Spreadsheet analysis staging unavailable; the document original is retained."
    return result


def _image_ids(value):
    if isinstance(value, dict):
        found = {value["image_artifact_id"]} if isinstance(value.get("image_artifact_id"), str) else set()
        return found.union(*(_image_ids(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_image_ids(v) for v in value))
    return set()


async def validate_request(parameters):
    require_enabled()
    action = parameters.get("action")
    if action not in {"read", "create", "edit", "convert", "preview"}:
        raise HTTPException(422, "Unknown document action")
    conv_id = parameters.get("conversation_id", "")
    if not await db.get_conversation(conv_id):
        raise HTTPException(404, "A saved conversation is required")
    if len(json.dumps(parameters)) > 2 * 1024 * 1024:
        raise HTTPException(413, "Document specification exceeds 2 MB")
    source, path = None, None
    if action != "create":
        source, path = await owned_source(parameters.get("artifact_id", ""))
        if path.suffix.lower() not in OFFICE_EXTS | LEGACY_EXTS:
            raise HTTPException(415, "Source is not a supported Office document")
        if action == "edit":
            current = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
            if parameters.get("expected_sha256") != current:
                raise HTTPException(409, "Source changed or was not inspected; read the document before editing")
    elif parameters.get("format") not in {"docx", "pptx", "xlsx"}:
        raise HTTPException(422, "Create format must be docx, pptx, or xlsx")
    if action == "create":
        from document_validation import validate_content, markdown_content, EXAMPLES
        try:
            if "markdown" in parameters:
                if parameters["format"] != "docx" or parameters.get("content"):
                    raise ValueError("markdown requires format docx and no content object")
                parameters["content"] = markdown_content(parameters.pop("markdown"))
            else:
                parameters["content"] = validate_content(parameters["format"], parameters.get("content"))
        except (ValueError, TypeError, RecursionError) as e:
            raise HTTPException(422, f"{e}. Example content: {json.dumps(EXAMPLES[parameters['format']])}") from e
    assets = {}
    for aid in _image_ids(parameters):
        artifact, asset = await owned_source(aid)
        if asset.suffix.lower() not in IMAGE_EXTS:
            raise HTTPException(415, "Image assets must be PNG or JPEG")
        assets[aid] = asset
    return source, path, assets


async def submit(http, parameters, *, wait=False, track_bg=None):
    source, path, assets = await validate_request(parameters)
    run_id = "doc-" + uuid.uuid4().hex
    await db.create_run(run_id, parameters["conversation_id"], "documents", status="queued")
    cancel_registry.register(run_id)
    coro = run_job(http, run_id, parameters, source, path, assets)
    task = asyncio.create_task(coro)
    _tasks[run_id] = task
    task.add_done_callback(lambda _task: _tasks.pop(run_id, None))
    if wait:
        return await task
    return {"run_id": run_id, "status": "queued"}


async def run_job(http, run_id, parameters, source, source_path, assets):
    from routes.chat_files import upload_bytes_to_codebox, safe_filename
    conv_id, action = parameters["conversation_id"], parameters["action"]
    async def emit(event, data):
        payload = {"run_id": run_id, "tool": "document_" + action, "run_role": "documents", "document_action": action, "icon": "file", **data}
        await db.append_run_event(run_id, {"type": event, **payload})
        if _events is not None:
            await _events.emit(conv_id, event, payload)
    async def check_cancel():
        row = await db.get_run(run_id)
        if cancel_registry.is_cancelled(run_id) or not row or row.get("status") in {"cancelling", "cancelled", "failed"}:
            raise cancel_registry.RunCancelled(run_id)
    published = []
    committed = False
    try:
        conn = await db.get_db()
        try:
            await conn.execute("UPDATE runs SET status='running',result_envelope=? WHERE id=? AND status='queued'", (json.dumps({"action": action}), run_id))
            await conn.commit()
        finally:
            await conn.close()
        await check_cancel()
        await emit("tool_start", {"status": "Processing document…"})
        request = {k: v for k, v in parameters.items() if k in {"action", "format", "content", "operations", "offset", "limit", "sheet", "range"}}
        if source_path:
            request["source"] = "source" + source_path.suffix.lower()
            original_bytes = await asyncio.to_thread(source_path.read_bytes)
            source = {**source, "sha256": hashlib.sha256(original_bytes).hexdigest()}
            if action == "edit" and parameters.get("expected_sha256") != source["sha256"]:
                raise ValueError("Source changed while staging; read it again before editing")
            await upload_bytes_to_codebox(http, f"{JOB_ROOT}/{run_id}/{request['source']}", original_bytes)
        request["assets"] = {}
        for aid, path in assets.items():
            name = "asset-" + uuid.uuid4().hex + path.suffix.lower()
            request["assets"][aid] = name
            await upload_bytes_to_codebox(http, f"{JOB_ROOT}/{run_id}/{name}", await asyncio.to_thread(path.read_bytes))
        await upload_bytes_to_codebox(http, f"{JOB_ROOT}/{run_id}/request.json", json.dumps(request).encode())
        await check_cancel()
        await worker(http, "start", run_id)
        deadline = asyncio.get_running_loop().time() + 630
        while True:
            await check_cancel()
            state = await worker(http, "status", run_id)
            if state.get("status") in {"succeeded", "failed", "cancelled"}:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Document job timed out")
            await asyncio.sleep(1)
        if state["status"] != "succeeded":
            raise ValueError(state.get("error", "Document processing failed"))
        await check_cancel()
        report = state.get("report", {})
        content_text = report.pop("text", "")
        warnings = list(dict.fromkeys(state.get("warnings", []) + report.get("warnings", []) + report.get("inspection", {}).get("warnings", [])))
        inspection = report.get("inspection", report if "items" in report else {})
        primary = source
        pending = []
        for output in sorted(state.get("outputs", []), key=lambda o: o["role"] == "preview"):
            await check_cancel()
            size = output["size_bytes"]
            if not 0 < size <= MAX_BYTES:
                raise ValueError("Invalid document output size")
            data = bytearray()
            while len(data) < size:
                await check_cancel()
                chunk = await worker(http, "chunk", run_id, output["path"], len(data))
                raw = base64.b64decode(chunk["data"], validate=True)
                if not raw or len(data) + len(raw) > size:
                    raise ValueError("Incomplete document transfer")
                data.extend(raw)
            role = output["role"]
            title = safe_filename(parameters.get("filename") or (source or {}).get("filename") or "Document")
            title = Path(title).stem + ("-preview" if role == "preview" else "-converted" if role in {"normalized", "export"} else "") + Path(output["path"]).suffix
            pending.append((bytes(data), title, role))
        # Artifact rows, revision links, preview metadata and run success commit
        # together. Restart/cancellation cannot expose a half-published revision.
        conn = await db.get_db()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            await check_cancel()
            for data, title, role in pending:
                output_text = (data.decode("utf-8-sig", errors="replace")[:200000]
                               if Path(title).suffix.lower() in {".csv", ".txt", ".md"} else content_text)
                artifact = await store_file(data, title, conversation_id=conv_id, run_id=run_id,
                                            source=primary if role == "preview" else source,
                                            role="revision" if role == "document" and action == "edit" else role,
                                            content_text=output_text if role != "preview" else "", _connection=conn,
                                            metadata={"warnings": warnings, "inspection": inspection if role != "preview" else {},
                                                      "validation": "structure checked; visual layout requires preview inspection"})
                published.append(artifact)
                if role in {"document", "normalized"}:
                    primary = artifact
                if role == "preview" and primary:
                    meta = dict(primary.get("metadata") or {})
                    meta.update({"document_preview_id": artifact["id"], "warnings": warnings, "inspection": inspection})
                    await conn.execute("UPDATE artifacts SET metadata_json=? WHERE id=? AND user_id=?", (json.dumps(meta), primary["id"], db._scope_user()))
            await check_cancel()
            result = {"status": "succeeded", "run_id": run_id, "action": action, "report": report,
                      "warnings": warnings, "source_sha256": (source or {}).get("sha256"),
                      "artifacts": [{"artifact_id": a["id"], "filename": a["filename"], "url": a["url"],
                                     "sha256": a["sha256"], "role": a["metadata"]["document_role"]} for a in published]}
            await conn.execute("UPDATE runs SET status='succeeded',result_envelope=?,ended_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(result), run_id))
            if cancel_registry.is_cancelled(run_id):
                raise cancel_registry.RunCancelled(run_id)
            await conn.commit()
            committed = True
        finally:
            if not committed:
                await conn.rollback()
            await conn.close()
        for a in published:
            await emit("file_ready", {"artifact_id": a["id"], "filename": a["filename"], "url": a["url"],
                                      "kind": a["kind"], "mime_type": a["mime_type"], "parent_artifact_id": a.get("parent_artifact_id"),
                                      "supersedes_artifact_id": a.get("supersedes_artifact_id"), "document_role": a["metadata"]["document_role"]})
        await emit("tool_end", {"run_status": "succeeded", "status": "Document operation finished", "detail": json.dumps({"warnings": warnings})})
        return result
    except BaseException as e:
        if committed:
            # A disconnected SSE consumer cannot revoke an already committed file.
            return result
        cancelled = isinstance(e, (cancel_registry.RunCancelled, asyncio.CancelledError))
        if cancelled:
            await db.update_run(run_id, status="cancelling")
        stop_error = await stop_worker(http, run_id)
        # Publication is rolled back when the owning job did not complete.
        for artifact in reversed(published):
            Path(artifact["storage_path"]).unlink(missing_ok=True)
        result = {"status": ("cancelling" if stop_error else "cancelled") if cancelled else "failed", "run_id": run_id, "action": action, "error": "; ".join(filter(None, [str(e)[:1000] or "Document job cancelled", stop_error]))}
        await db.update_run(run_id, status=result["status"], result_envelope=result, ended=result["status"] != "cancelling")
        await emit("tool_error" if not cancelled else "tool_end", {"status": result["error"], "run_status": result["status"]})
        if isinstance(e, asyncio.CancelledError):
            raise
        return result
    finally:
        cancel_registry.cleanup(run_id)


async def tool(name, args, *, http, conv_id):
    try:
        result = await submit(http, {**args, "action": name.removeprefix("document_"), "conversation_id": conv_id}, wait=True)
        # Preserve valid JSON and an explicit continuation cursor when bounding
        # model context; never cut the serialized object mid-string.
        for report in (result.get("report", {}), result.get("report", {}).get("inspection", {})):
            items = report.get("items", [])
            chars, kept = 0, []
            for item in items:
                row = dict(item)
                for key in ("text", "value", "formula"):
                    if isinstance(row.get(key), str) and len(row[key]) > 12000:
                        row[key] = row[key][:12000]
                        row["text_truncated"] = True
                cost = len(json.dumps(row))
                if kept and chars + cost > 40000:
                    break
                kept.append(row)
                chars += cost
            if "items" in report:
                report["items"] = kept
            if len(kept) < len(items):
                report["next_offset"] = int(args.get("offset", 0)) + len(kept)
        return json.dumps(result, ensure_ascii=False)
    except HTTPException as e:
        if _events is not None:
            await _events.emit(conv_id, "tool_error", {"tool": name, "run_role": "documents", "run_status": "failed", "status": f"Document failed: {e.detail}"})
        return json.dumps({"status": "failed", "error": e.detail, "error_code": "invalid_arguments" if e.status_code == 422 else "request_failed"})


async def preview_payload(artifact):
    meta = artifact.get("metadata") or {}
    preview = await db.get_artifact(meta["document_preview_id"]) if meta.get("document_preview_id") else None
    if preview and not ((path := artifact_path_for_row(preview)[0]) and Path(path).is_file()):
        preview = None
    return {**artifact, "preview_type": "office", "inspection": meta.get("inspection", {}),
            "warnings": meta.get("warnings", []), "download_url": artifact["url"],
            "render_url": preview["url"] if preview else None}


async def has_documents(conversation_id):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT 1 FROM conversations c WHERE c.id=? AND c.user_id=? AND "
            "(EXISTS(SELECT 1 FROM artifacts a WHERE a.conversation_id=c.id AND a.user_id=c.user_id AND a.kind IN ('document','presentation','spreadsheet')) "
            "OR EXISTS(SELECT 1 FROM runs r WHERE r.conversation_id=c.id AND r.role='documents')) LIMIT 1",
            (conversation_id, db._scope_user()),
        )
        return bool(rows)
    finally:
        await conn.close()


async def cancel_run(http, run_id):
    row = await db.get_run(run_id)
    if not row or row.get("role") != "documents":
        raise HTTPException(404, "Document job not found")
    if row["status"] not in {"queued", "pending", "running", "cancelling"}:
        return {"run_id": run_id, "status": row["status"]}
    conn = await db.get_db()
    try:
        await conn.execute("UPDATE runs SET status='cancelling' WHERE id=? AND status IN ('queued','pending','running')", (run_id,))
        await conn.commit()
    finally:
        await conn.close()
    row = await db.get_run(run_id)
    if row["status"] != "cancelling":
        return {"run_id": run_id, "status": row["status"]}
    cancel_registry.signal(run_id)
    error = await stop_worker(http, run_id)
    # An active owner finishes rollback before reporting cancellation.
    if not error and run_id not in _tasks:
        conn = await db.get_db()
        try:
            await conn.execute("UPDATE runs SET status='cancelled',ended_at=CURRENT_TIMESTAMP WHERE id=? AND status='cancelling'", (run_id,))
            await conn.commit()
        finally:
            await conn.close()
    return {"run_id": run_id, "status": (await db.get_run(run_id))["status"], "error": error}
