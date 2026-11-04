"""Runtime storage diagnostics for SQLite-backed HyprChat data paths."""
import os
import sqlite3
import uuid


async def execution_storage_status(http, base_url: str, owner: str) -> dict:
    """Read VM capacity without exposing worker metadata or invoking cleanup."""
    import httpx
    from hephaestus_scope import headers

    if not os.environ.get("HEPHAESTUS_TOKEN") or not base_url:
        return {"status": "not_configured", "message": "Hephaestus storage status is not configured. Check Connections."}
    if not owner:
        return {"status": "unavailable", "message": "Sign in to view Hephaestus storage."}
    try:
        response = await http.get(
            base_url.rstrip("/") + "/status", timeout=5,
            headers=headers(owner, project="storage-status"), follow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("runtime") != "hephaestus":
            return {"status": "unavailable", "message": "The execution endpoint did not identify itself as Hephaestus. Check Connections."}
        disk = data.get("disk", {})
        if not isinstance(disk, dict):
            raise ValueError("Invalid disk summary")
        total, used, free = (disk.get(key) for key in ("total", "used", "free"))
        if any(type(value) is not int for value in (total, used, free)) or not (
            total > 0 and 0 <= used <= total and 0 <= free <= total and used + free <= total
        ):
            raise ValueError("Invalid disk capacity")
        result={"status": "ok", "runtime": "hephaestus", "disk": {"total": total, "used": used, "free": free}}
        if data.get('storage_version') == 1:
            result['storage']=await vm_request(http,base_url,owner,'/storage')
            await sync_vm_retirements(result['storage'].get('operations',[]))
        return result
    except httpx.TimeoutException:
        return {"status": "unavailable", "message": "Hephaestus took too long to respond. Local files are still available. Retry in a moment."}
    except httpx.HTTPStatusError as error:
        return {"status": "unavailable", "message": f"Hephaestus storage status is unavailable (HTTP {error.response.status_code}). Check Connections."}
    except httpx.RequestError:
        return {"status": "unavailable", "message": "Cannot reach Hephaestus. Local files are still available. Check Connections."}
    except (ValueError, TypeError):
        return {"status": "unavailable", "message": "Hephaestus returned an unreadable disk summary. Retry or check the VM service."}


READONLY_STORAGE_PATTERNS = (
    "readonly database",
    "read-only database",
    "attempt to write a readonly database",
    "attempt to write a read-only database",
    "code: 1032",
    "permission denied",
    "read-only file system",
    "unable to open database file",
)


def is_readonly_storage_error(error) -> bool:
    text = str(error or "").lower()
    return any(pattern in text for pattern in READONLY_STORAGE_PATTERNS)


def readonly_storage_message() -> str:
    return (
        "RAG storage is not writable. Ensure the configured HyprChat data "
        "directory, hyprchat.db*, and chroma_db are owned by the service user "
        "and writable, then restart HyprChat."
    )


def _mode(path: str) -> str:
    try:
        return oct(os.stat(path).st_mode & 0o777)
    except OSError:
        return ""


def _path_summary(path: str) -> dict:
    exists = os.path.exists(path)
    summary = {
        "path": path,
        "exists": exists,
        "mode": _mode(path) if exists else "",
        "uid": None,
        "gid": None,
        "writable": False,
        "error": "",
    }
    try:
        st = os.stat(path)
        summary.update({
            "uid": st.st_uid,
            "gid": st.st_gid,
            "writable": os.access(path, os.W_OK),
        })
    except FileNotFoundError:
        parent = os.path.dirname(path) or "."
        summary["writable"] = os.access(parent, os.W_OK)
        summary["error"] = "missing"
    except OSError as exc:
        summary["error"] = str(exc)
    return summary


def _probe_directory_write(directory: str) -> tuple[bool, str]:
    if not os.path.isdir(directory):
        return False, "directory missing"
    probe = os.path.join(directory, f".hyprchat-write-test-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True, ""
    except OSError as exc:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except OSError:
            pass
        return False, str(exc)


def _scan_unwritable(path: str, limit: int = 25, max_entries: int = 250) -> list[dict]:
    if not os.path.isdir(path):
        return []
    entries: list[dict] = []
    checked = 0
    for root, dirs, files in os.walk(path):
        for name in [root] + [os.path.join(root, d) for d in dirs] + [os.path.join(root, f) for f in files]:
            checked += 1
            if not os.access(name, os.W_OK):
                entries.append(_path_summary(name))
                if len(entries) >= limit:
                    return entries
            if checked >= max_entries:
                return entries
    return entries


def directory_storage_status(path: str, label: str = "storage", scan_children: bool = False) -> dict:
    summary = _path_summary(path)
    ok, error = _probe_directory_write(path)
    unwritable = _scan_unwritable(path) if scan_children else []
    status = "ok" if summary["exists"] and ok and not unwritable else "error"
    return {
        "status": status,
        "label": label,
        **summary,
        "write_probe": "ok" if ok else "failed",
        "write_error": error,
        "unwritable_entries": unwritable,
    }


def sqlite_storage_status(database_path: str) -> dict:
    parent = os.path.dirname(database_path) or "."
    parent_status = directory_storage_status(parent, "database_dir")
    db_status = _path_summary(database_path)
    sidecars = [_path_summary(f"{database_path}{suffix}") for suffix in ("-wal", "-shm")]
    sqlite_error = ""
    sqlite_status = "ok"
    try:
        conn = sqlite3.connect(database_path, timeout=1.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        sqlite_error = str(exc)
        sqlite_status = "error" if is_readonly_storage_error(exc) else "degraded"
    except OSError as exc:
        sqlite_error = str(exc)
        sqlite_status = "error"
    status = sqlite_status if parent_status["status"] == "ok" else "error"
    return {
        "status": status,
        "label": "database",
        "path": database_path,
        "directory": parent_status,
        "database_file": db_status,
        "sidecars": sidecars,
        "write_probe": sqlite_status,
        "error": sqlite_error,
    }


def runtime_storage_status(database_path: str, chroma_dir: str) -> dict:
    database = sqlite_storage_status(database_path)
    rag_chroma = directory_storage_status(chroma_dir, "rag_chroma", scan_children=True)
    parts = [database, rag_chroma]
    statuses = {part["status"] for part in parts}
    if "error" in statuses:
        status = "error"
    elif "degraded" in statuses:
        status = "degraded"
    else:
        status = "ok"
    errors = []
    if database["status"] != "ok":
        errors.append(database.get("error") or "database storage is not writable")
    if rag_chroma["status"] != "ok":
        errors.append(rag_chroma.get("write_error") or "RAG Chroma storage is not writable")
    return {
        "status": status,
        "database": database,
        "rag_chroma": rag_chroma,
        "error": "; ".join(e for e in errors if e),
    }


async def vm_request(http, base_url, owner, path, body=None):
    from hephaestus_scope import headers
    request_headers=headers(owner, project='storage-maintenance', maintenance='storage-admin')
    if not request_headers:raise ValueError('Hephaestus maintenance is not configured')
    response=await http.request('POST' if body is not None else 'GET',base_url.rstrip('/')+path,
        json=body,headers=request_headers,timeout=120,follow_redirects=False)
    response.raise_for_status()
    return response.json()


async def archive_test_evidence(http, base_url, owner='default'):
    """Stream evidence to durable app storage and verify every archived member."""
    import asyncio
    import hashlib
    import tarfile
    from datetime import datetime, timezone
    from pathlib import Path
    import config
    from hephaestus_scope import headers
    records=(await vm_request(http,base_url,owner,'/storage/archives',{}))['archives']
    destination=Path(config.DATA_DIR)/'evaluation-archives/hephaestus'/ (datetime.now(timezone.utc).strftime('%Y-%m-%d')+'-storage-cleanup')
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    for record in records:
        import shutil
        if shutil.disk_usage(destination).free < record['bytes'] + 1024**3:
            raise OSError('Not enough HyprChat disk space to preserve the test evidence; VM cleanup remains blocked')
        if not __import__('re').fullmatch('[a-f0-9]{32}',record['id']):raise ValueError('Invalid evidence identity')
        target=destination/(record['id']+'.tar.gz');partial=target.with_suffix('.pending')
        if not target.exists():
            digest=hashlib.sha256();size=0
            try:
                async with http.stream('GET',base_url.rstrip('/')+'/storage/archive/'+record['id'],
                    headers=headers(owner,project='storage-maintenance',maintenance='storage-admin'),timeout=120) as response:
                    response.raise_for_status()
                    with partial.open('wb') as out:
                        async for chunk in response.aiter_bytes():
                            size+=len(chunk)
                            if size>record['bytes']:raise ValueError('Evidence archive size changed')
                            out.write(chunk);digest.update(chunk)
                        out.flush();os.fsync(out.fileno())
                if size!=record['bytes'] or digest.hexdigest()!=record['sha256']:raise ValueError('Evidence archive checksum mismatch')
                partial.chmod(0o600);partial.replace(target)
            finally:partial.unlink(missing_ok=True)
        def verify():
            import json
            digest=hashlib.sha256()
            with target.open('rb') as handle:
                for chunk in iter(lambda:handle.read(1024**2),b''):digest.update(chunk)
            if digest.hexdigest()!=record['sha256']:raise ValueError('Saved evidence checksum mismatch')
            with tarfile.open(target) as archive:
                manifest=json.load(archive.extractfile('manifest.json'))
                for row in manifest['files']:
                    digest=hashlib.sha256()
                    with archive.extractfile('evidence/'+row['path']) as handle:
                        for chunk in iter(lambda:handle.read(1024**2),b''):digest.update(chunk)
                    if digest.hexdigest()!=row['sha256']:raise ValueError('Evidence member checksum mismatch')
            receipt={**record,'archive':str(target),'verified':True}
            target.with_suffix('.json').write_text(json.dumps(receipt,indent=2))
        await asyncio.to_thread(verify)
        await vm_request(http,base_url,owner,'/storage/archive/'+record['id']+'/ack',
                         {'sha256':record['sha256'],'destination':str(target)})
    return len(records)


async def sync_vm_retirements(operations):
    import json
    from pathlib import Path
    import config
    import database as db
    completed=[op for op in operations if op.get('mode')=='reset' and op.get('status') in {'completed','blocked'} and op.get('retired_jobs')]
    if not completed:return
    registry=Path(config.DATA_DIR)/'retired-worker-workspaces.json'
    receipt=json.loads(registry.read_text()) if registry.exists() else {'projects':[],'jobs':[]}
    completed=[op for op in completed if op['id'] not in receipt.get('operations',[])]
    if not completed:return
    connection=await db.get_db()
    try:
        await connection.execute('BEGIN IMMEDIATE')
        for operation in completed:
            for job_id in operation['retired_jobs']:
                rows=await connection.execute_fetchall('SELECT job_json,project_id FROM coder_workflows WHERE id=?',(job_id,))
                for row in rows:
                    job=json.loads(row['job_json'] or '{}');job.update(worker_workspace_available=False,
                        worker_workspace_reason='The VM project data was explicitly reset. Restore the saved archive to continue.')
                    await connection.execute('UPDATE coder_workflows SET job_json=? WHERE id=?',(json.dumps(job),job_id))
                    projects=await connection.execute_fetchall('SELECT id,user_id,openhands_project_id FROM coding_projects WHERE id=?',(row['project_id'],))
                    for project in projects:
                        value=dict(project)
                        if value not in receipt['projects']:receipt['projects'].append(value)
        receipt['operations']=sorted(set(receipt.get('operations',[]))|{op['id'] for op in completed})
        tmp=registry.with_suffix('.pending');tmp.write_text(json.dumps(receipt));tmp.chmod(0o600)
        await connection.commit();tmp.replace(registry)
    finally:await connection.close()
