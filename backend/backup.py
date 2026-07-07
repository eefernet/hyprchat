"""Backup / restore for HyprChat data.

Homelab-scale: one tar.gz of the data directory with a consistent SQLite copy
(VACUUM INTO — safe under the single-writer/one-worker deployment) and secrets
scrubbed from the staged DB copy. ChromaDB is deliberately excluded — it is
derived state, rebuilt via Knowledge Bases → Reindex All (plus the automatic
FTS backfill) after a restore.

Restore is two-phase because SQLite is live in-process: the uploaded archive is
extracted to a staging dir and a marker file is written; `apply_pending_restore`
runs synchronously at lifespan startup BEFORE `database.init_db()`, so an
older-schema backup is migrated by the normal idempotent migrations right after.
"""
import json
import os
import shutil
import sqlite3
import tarfile
import time
from glob import glob

import config

BACKUP_DIR = os.path.join(config.DATA_DIR, "backups")
RESTORE_PENDING_DIR = os.path.join(config.DATA_DIR, "restore-pending")
RESTORE_MARKER = os.path.join(config.DATA_DIR, ".restore-pending")
MANIFEST_NAME = "manifest.json"

_INCLUDE_DIRS = ["uploads", "knowledge_bases", "comfy_workflows", "tools"]
_INCLUDE_FILES = ["settings.json", "comfy_model_settings.json", "persona_image_profiles.json"]
# Never in an archive and never overwritten by a restore
_EXCLUDE_BASENAMES = {"connector_secrets.json"}
_EXCLUDE_SUFFIXES = (".key", ".pem", ".db-journal", ".db-wal", ".db-shm")

# Blanked in the STAGED copy only (live DB untouched). Provider keys are
# re-enterable in Settings; connector header/env JSON may embed credentials.
_SCRUB_STATEMENTS = (
    "UPDATE model_provider_credentials SET api_key=''",
    "UPDATE mcp_servers SET headers_json='{}'",
    "UPDATE mcp_servers SET env_json='{}'",
    "UPDATE openapi_connectors SET headers_json='{}'",
    "UPDATE openapi_connectors SET auth_json='{}'",
)


def _scrub_secrets(db_path: str) -> list[str]:
    scrubbed = []
    conn = sqlite3.connect(db_path)
    try:
        for stmt in _SCRUB_STATEMENTS:
            try:
                conn.execute(stmt)
                scrubbed.append(stmt.split(" SET ", 1)[0].replace("UPDATE ", "") + "." +
                                stmt.split(" SET ", 1)[1].split("=", 1)[0])
            except sqlite3.Error:
                pass  # table/column may not exist on this schema — fine
        conn.commit()
    finally:
        conn.close()
    return scrubbed


def _tar_filter(info: tarfile.TarInfo):
    base = os.path.basename(info.name)
    if base in _EXCLUDE_BASENAMES or base.endswith(_EXCLUDE_SUFFIXES):
        return None
    return info


def create_backup_archive() -> str:
    """Build the archive; returns its path. Sync — call via asyncio.to_thread."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stage = os.path.join(BACKUP_DIR, f"stage-{ts}")
    os.makedirs(stage, exist_ok=True)
    out_path = os.path.join(BACKUP_DIR, f"hyprchat-backup-{ts}.tar.gz")
    try:
        staged_db = os.path.join(stage, "hyprchat.db")
        conn = sqlite3.connect(config.DATABASE_PATH)
        try:
            conn.execute("VACUUM INTO ?", (staged_db,))
        finally:
            conn.close()
        scrubbed = _scrub_secrets(staged_db)

        manifest = {
            "version": 1,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "hyprchat_db": "hyprchat.db",
            "secrets_scrubbed": scrubbed,
            "included_dirs": [d for d in _INCLUDE_DIRS if os.path.isdir(os.path.join(config.DATA_DIR, d))],
            "note": "chroma_db is excluded (derived) — after restore, restart the service and run Knowledge Bases → Reindex All.",
        }
        manifest_path = os.path.join(stage, MANIFEST_NAME)
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        with tarfile.open(out_path, "w:gz") as tar:
            tar.add(staged_db, arcname="hyprchat.db")
            tar.add(manifest_path, arcname=MANIFEST_NAME)
            for d in _INCLUDE_DIRS:
                p = os.path.join(config.DATA_DIR, d)
                if os.path.isdir(p):
                    tar.add(p, arcname=d, filter=_tar_filter)
            for name in _INCLUDE_FILES:
                p = os.path.join(config.DATA_DIR, name)
                if os.path.isfile(p):
                    tar.add(p, arcname=name)
        return out_path
    except Exception:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _safe_members(tar: tarfile.TarFile):
    for m in tar.getmembers():
        if m.name.startswith("/") or ".." in m.name.split("/"):
            raise ValueError(f"Unsafe archive member path: {m.name}")
        if m.issym() or m.islnk():
            raise ValueError(f"Links are not allowed in backup archives: {m.name}")
        yield m


def stage_restore(archive_path: str) -> dict:
    """Extract + validate an uploaded backup; write the pending marker.

    Never touches live data — apply happens at next startup."""
    if os.path.isdir(RESTORE_PENDING_DIR):
        shutil.rmtree(RESTORE_PENDING_DIR)
    os.makedirs(RESTORE_PENDING_DIR, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(RESTORE_PENDING_DIR, members=_safe_members(tar))
    manifest_path = os.path.join(RESTORE_PENDING_DIR, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        shutil.rmtree(RESTORE_PENDING_DIR, ignore_errors=True)
        raise ValueError("archive has no manifest.json — not a HyprChat backup")
    with open(manifest_path) as f:
        manifest = json.load(f)
    db_name = manifest.get("hyprchat_db") or "hyprchat.db"
    staged_db = os.path.join(RESTORE_PENDING_DIR, db_name)
    if not os.path.isfile(staged_db):
        shutil.rmtree(RESTORE_PENDING_DIR, ignore_errors=True)
        raise ValueError("backup is missing its database file")
    # Basic integrity probe of the staged DB
    try:
        conn = sqlite3.connect(staged_db)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        if not ok or ok[0] != "ok":
            raise ValueError("staged database failed integrity_check")
    except sqlite3.Error as e:
        shutil.rmtree(RESTORE_PENDING_DIR, ignore_errors=True)
        raise ValueError(f"staged database unreadable: {e}")
    with open(RESTORE_MARKER, "w") as f:
        json.dump({"staged_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "manifest": manifest}, f)
    return manifest


def _prune_pre_restore_copies(keep: int = 2) -> None:
    """Keep only the newest N pre-restore DB safety copies. Each restore left
    a full DB copy behind forever; repeated restores filled the data dir."""
    try:
        base = os.path.basename(config.DATABASE_PATH) + ".pre-restore-"
        data_dir = os.path.dirname(config.DATABASE_PATH)
        copies = sorted(
            (e for e in os.listdir(data_dir) if e.startswith(base)),
            reverse=True,  # timestamp suffix sorts lexicographically
        )
        for stale in copies[keep:]:
            try:
                os.remove(os.path.join(data_dir, stale))
                print(f"[Backup] Pruned old pre-restore copy {stale}")
            except OSError as e:
                print(f"[Backup] Could not prune {stale}: {e}")
    except Exception as e:
        print(f"[Backup] pre-restore prune skipped: {e}")


def apply_pending_restore() -> bool:
    """Apply a staged restore. Called at lifespan start BEFORE init_db.

    The live DB is kept as hyprchat.db.pre-restore-<ts>. connector_secrets.json
    is never touched. Failure logs loudly and continues with existing data."""
    if not os.path.isfile(RESTORE_MARKER):
        return False
    print("[Backup] Pending restore found — applying...")
    try:
        with open(os.path.join(RESTORE_PENDING_DIR, MANIFEST_NAME)) as f:
            manifest = json.load(f)
        db_name = manifest.get("hyprchat_db") or "hyprchat.db"
        staged_db = os.path.join(RESTORE_PENDING_DIR, db_name)
        ts = time.strftime("%Y%m%d-%H%M%S")
        if os.path.isfile(config.DATABASE_PATH):
            shutil.copy2(config.DATABASE_PATH, config.DATABASE_PATH + f".pre-restore-{ts}")
        _prune_pre_restore_copies(keep=2)
        for suffix in ("-journal", "-wal", "-shm"):
            p = config.DATABASE_PATH + suffix
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(staged_db, config.DATABASE_PATH)
        for entry in os.listdir(RESTORE_PENDING_DIR):
            if entry in (MANIFEST_NAME, db_name) or entry in _EXCLUDE_BASENAMES:
                continue
            src = os.path.join(RESTORE_PENDING_DIR, entry)
            dst = os.path.join(config.DATA_DIR, entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        os.remove(RESTORE_MARKER)
        shutil.rmtree(RESTORE_PENDING_DIR, ignore_errors=True)
        print(f"[Backup] Restore applied. Previous DB kept as hyprchat.db.pre-restore-{ts}. "
              "Run Knowledge Bases → Reindex All to rebuild RAG.")
        return True
    except Exception as e:
        print(f"[Backup] RESTORE FAILED — continuing with existing data: {e}")
        try:
            os.remove(RESTORE_MARKER)
        except OSError:
            pass
        return False


def backup_status() -> dict:
    pending = os.path.isfile(RESTORE_MARKER)
    info = {}
    if pending:
        try:
            with open(RESTORE_MARKER) as f:
                info = json.load(f)
        except Exception:
            info = {}
    archives = sorted(glob(os.path.join(BACKUP_DIR, "hyprchat-backup-*.tar.gz")))
    return {
        "restore_pending": pending,
        "staged_at": info.get("staged_at", ""),
        "leftover_archives": [os.path.basename(a) for a in archives],
    }
