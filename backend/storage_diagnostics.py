"""Runtime storage diagnostics for SQLite-backed HyprChat data paths."""
import os
import sqlite3
import uuid


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
