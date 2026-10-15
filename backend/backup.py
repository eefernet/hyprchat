"""Verified data snapshots and journaled, restart-recoverable restore activation.

The database and file trees form one restore unit. A prepared journal always
rolls back on startup; only a durable committed journal permits cleanup.
"""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
import tempfile
import subprocess
import sys
import threading
import time
import uuid
from glob import glob

import config

BACKUP_DIR = os.path.join(config.DATA_DIR, "backups")
RESTORE_PENDING_DIR = os.path.join(config.DATA_DIR, "restore-pending")
RESTORE_MARKER = os.path.join(config.DATA_DIR, ".restore-pending")
MANIFEST_NAME = "manifest.json"
_INCLUDE_DIRS = ["uploads", "knowledge_bases", "comfy_workflows", "tools", "artifacts", "sandbox/outputs"]
_INCLUDE_FILES = ["settings.json", "comfy_model_settings.json", "persona_image_profiles.json"]
_EXCLUDE_BASENAMES = {"connector_secrets.json", ".env"}
_EXCLUDE_SUFFIXES = (".key", ".pem", ".db-journal", ".db-wal", ".db-shm")
_LOCK = threading.RLock()
MAX_ARCHIVE_BYTES = 20 * 1024**3
MAX_ARCHIVE_MEMBERS = 200000
_SCRUB_STATEMENTS = (
    "UPDATE model_provider_credentials SET api_key=''",
    "UPDATE mcp_servers SET headers_json='{}'",
    "UPDATE mcp_servers SET env_json='{}'",
    "UPDATE openapi_connectors SET headers_json='{}'",
    "UPDATE openapi_connectors SET auth_json='{}'",
    "UPDATE caldav_accounts SET password=''",
    "UPDATE email_accounts SET password=''",
    "UPDATE scheduled_tasks SET webhook_token=''",
    "UPDATE scheduled_tasks SET enabled=0",
    "UPDATE task_runs SET status='interrupted',error='Restored backup; review before rerunning' WHERE status IN ('queued','running','cancelling')",
    "UPDATE notification_deliveries SET status='uncertain',error='Restored backup; review delivery at destination' WHERE status IN ('queued','retry_wait','quiet_hours','sending')",
    "UPDATE indexing_jobs SET status='interrupted',error='Restored backup; retry indexing explicitly' WHERE status IN ('queued','running')",
    "DELETE FROM user_sessions",
    "UPDATE users SET password_hash='!restore-password-required' WHERE password_hash!=''",
)
_SECRET = re.compile(r"password|secret|api[_-]?key|authorization|(^|[_-])token($|[_-])|private[_-]?key", re.I)


def _hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _json_write(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        json.dump(value, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _scrub_json(value):
    if isinstance(value, dict):
        return {k: "" if _SECRET.search(k) else _scrub_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_json(v) for v in value]
    if isinstance(value, str):
        # URL userinfo and common query credentials are configuration secrets too.
        value = re.sub(r"(https?://)[^/@\s]+@", r"\1", value)
        value = re.sub(r"([?&](?:token|key|api_key|password|secret)=)[^&#\s]*", r"\1", value, flags=re.I)
    return value


def _scrub_secrets(db_path):
    scrubbed = []
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        for stmt in _SCRUB_STATEMENTS:
            try:
                conn.execute(stmt)
                scrubbed.append(stmt.split(" SET ")[0])
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e) and "no such column" not in str(e):
                    raise
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "user_prefs" in tables:
            for rowid, key, value in conn.execute("SELECT rowid,key,value_json FROM user_prefs").fetchall():
                cleaned = "" if _SECRET.search(key) else _scrub_json(json.loads(value))
                conn.execute("UPDATE user_prefs SET value_json=? WHERE rowid=?", (json.dumps(cleaned), rowid))
        # MCP command arguments and URLs may contain credentials; scrub config
        # strings conservatively without changing user-authored content tables.
        for table in ("mcp_servers", "openapi_connectors", "model_provider_credentials", "caldav_accounts"):
            if table not in tables:
                continue
            columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            for column in columns & {"url", "base_url"}:
                for rowid, value in conn.execute(f'SELECT rowid,"{column}" FROM "{table}"').fetchall():
                    conn.execute(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?', (_scrub_json(value), rowid))
            if "args_json" in columns:
                conn.execute(f"UPDATE {table} SET args_json='[]'")
        conn.commit()
        # Deleted secrets must not survive in SQLite free pages.
        conn.execute("VACUUM")
    return scrubbed


def _allowed(name):
    return name in {"hyprchat.db", MANIFEST_NAME, *_INCLUDE_FILES} or any(
        name == root or name.startswith(root + "/") for root in _INCLUDE_DIRS)


def _excluded(path):
    return any(p in _EXCLUDE_BASENAMES or p.startswith(".env.") or p.endswith(_EXCLUDE_SUFFIXES)
               for p in Path(path).parts)


def _content_path(name):
    return Path({"uploads": config.UPLOAD_DIR, "knowledge_bases": config.KB_DIR,
                 "tools": config.TOOLS_DIR, "artifacts": config.ARTIFACT_DIR,
                 "sandbox/outputs": config.SANDBOX_OUTPUTS_DIR,
                 "settings.json": config.SETTINGS_PATH}.get(name, str(Path(config.DATA_DIR) / name)))


def _validate_migrations(stage):
    # Validate schema evolution on a separate DB; the hashed stage stays immutable.
    with tempfile.TemporaryDirectory(prefix="restore-migrations-", dir=config.DATA_DIR) as scratch:
        path = Path(scratch) / "hyprchat.db"
        shutil.copy2(Path(stage) / "hyprchat.db", path)
        env = {**os.environ, "HYPRCHAT_DATA_DIR": scratch, "DATABASE_PATH": str(path),
               "PYTHONPATH": str(Path(__file__).resolve().parent)}
        result = subprocess.run([sys.executable, "-c", "import asyncio,database; asyncio.run(database.init_db())"],
                                env=env, cwd=Path(__file__).resolve().parent, capture_output=True, timeout=120)
        if result.returncode:
            raise ValueError("Backup cannot be migrated by this application version")
        with closing(sqlite3.connect(path)) as conn, conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup failed integrity check after migration")


def _capture_references(db_path, stage):
    """Include configured external storage too; only the snapshot paths change."""
    from durable_storage import source_for
    missing = []
    with closing(sqlite3.connect(db_path)) as conn, conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(artifacts)")}
        if not {"id", "storage_path", "url", "filename", "sha256"} <= columns:
            return missing
        for aid, path, url, filename, expected in conn.execute("SELECT id,storage_path,url,filename,sha256 FROM artifacts").fetchall():
            source = source_for(path, url, filename)
            if source is None:
                missing.append(aid)
                continue
            sha = _hash(source)
            if expected and expected != sha:
                raise ValueError("Referenced artifact does not match its recorded hash")
            relative = Path("artifacts") / sha / source.name
            dest = stage / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            if _hash(dest) != sha:
                raise ValueError("Referenced artifact changed during backup")
            conn.execute("UPDATE artifacts SET storage_path=? WHERE id=?", (str(Path(config.DATA_DIR) / relative), aid))
    return missing


def _sync_tree(root):
    root = Path(root)
    for path in [root, *root.rglob("*")] if root.is_dir() else [root]:
        if path.is_file() or path.is_dir():
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


def create_backup_archive():
    with _LOCK:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="stage-", dir=BACKUP_DIR))
        out = Path(BACKUP_DIR) / f"hyprchat-backup-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.tar.gz"
        try:
            with closing(sqlite3.connect(config.DATABASE_PATH)) as live, closing(sqlite3.connect(stage / "hyprchat.db")) as snapshot:
                live.backup(snapshot)
            scrubbed = _scrub_secrets(stage / "hyprchat.db")
            for root in _INCLUDE_DIRS:
                source = _content_path(root)
                if not source.exists():
                    continue
                if stage.is_relative_to(source.resolve()):
                    raise ValueError("Content storage overlaps backup staging")
                for path in source.rglob("*"):
                    relative = Path(root) / path.relative_to(source)
                    if path.is_symlink():
                        raise ValueError("Backup content contains a symbolic link")
                    if path.is_file() and not _excluded(relative):
                        dest = stage / relative
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, dest)
            for name in _INCLUDE_FILES:
                source = _content_path(name)
                if source.exists():
                    _json_write(stage / name, _scrub_json(json.loads(source.read_text())))
            missing = _capture_references(stage / "hyprchat.db", stage)
            files = {str(p.relative_to(stage)): {"size": p.stat().st_size, "sha256": _hash(p)}
                     for p in stage.rglob("*") if p.is_file()}
            manifest = {"version": 2, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "hyprchat_db": "hyprchat.db", "source_data_dir": config.DATA_DIR,
                        "source_roots": {name: str(_content_path(name)) for name in _INCLUDE_DIRS},
                        "secrets_scrubbed": scrubbed, "files": files, "missing_artifact_ids": missing,
                        "included_dirs": [d for d in _INCLUDE_DIRS if (stage / d).exists()],
                        "note": "Chroma is derived; rebuild indexes after restore. Protected profiles retain local passwords or require an administrator reset."}
            _json_write(stage / MANIFEST_NAME, manifest)
            with tarfile.open(out, "w:gz") as tar:
                for p in sorted(stage.rglob("*")):
                    if p.is_file():
                        tar.add(p, arcname=str(p.relative_to(stage)), recursive=False)
            return str(out)
        except BaseException:
            out.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(stage)


def _safe_members(tar):
    seen, size = set(), 0
    for m in tar:
        if m.issym() or m.islnk():
            raise ValueError("Links are not allowed in backup archives")
        if str(Path(m.name)) != m.name or m.name.startswith("/") or ".." in m.name.split("/") or "\\" in m.name or not (m.isfile() or m.isdir()):
            raise ValueError("Unsafe archive member path or type")
        if m.name in seen or (m.name != "sandbox" and not _allowed(m.name)) or _excluded(m.name):
            raise ValueError("Unsafe or duplicate archive member")
        seen.add(m.name)
        size += m.size
        if size > MAX_ARCHIVE_BYTES or len(seen) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("Backup exceeds extraction limits")
        yield m


def _validate(stage):
    stage = Path(stage)
    if not (stage / MANIFEST_NAME).is_file():
        raise ValueError("archive has no manifest.json")
    manifest = json.loads((stage / MANIFEST_NAME).read_text())
    if not isinstance(manifest, dict):
        raise ValueError("Invalid backup manifest")
    if manifest.get("version") not in (1, 2) or manifest.get("hyprchat_db", "hyprchat.db") != "hyprchat.db":
        raise ValueError("Unsupported backup manifest")
    if not (stage / "hyprchat.db").is_file():
        raise ValueError("backup is missing its database file")
    if manifest["version"] == 2:
        if not isinstance(manifest.get("files"), dict):
            raise ValueError("Invalid backup file inventory")
        actual = {str(p.relative_to(stage)) for p in stage.rglob("*") if p.is_file() and p != stage / MANIFEST_NAME}
        if actual != set(manifest.get("files", {})):
            raise ValueError("Backup manifest file inventory mismatch")
        for name, record in manifest["files"].items():
            path = stage / name
            if not _allowed(name) or path.stat().st_size != record["size"] or _hash(path) != record["sha256"]:
                raise ValueError("Backup size/hash verification failed")
    else:
        manifest["warnings"] = ["Legacy backup has no content hashes and may omit artifacts, previews and sandbox outputs."]
    try:
        with closing(sqlite3.connect(f"file:{stage / 'hyprchat.db'}?mode=ro&immutable=1", uri=True)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("staged database failed integrity_check")
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='conversations'").fetchone():
                raise ValueError("Backup is not a HyprChat database")
    except sqlite3.Error as e:
        raise ValueError("staged database unreadable") from e
    if manifest.get("missing_artifact_ids"):
        manifest.setdefault("warnings", []).append(f"{len(manifest['missing_artifact_ids'])} referenced artifacts were already missing at backup time")
    return manifest


def stage_restore(archive_path):
    with _LOCK:
        if Path(RESTORE_MARKER).exists():
            raise ValueError("A restore is already staged; cancel it first")
        stage = Path(tempfile.mkdtemp(prefix="restore-check-", dir=config.DATA_DIR))
        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                for member in _safe_members(tar):
                    target = stage / member.name
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with tar.extractfile(member) as incoming, target.open("xb") as outgoing:
                            shutil.copyfileobj(incoming, outgoing)
            manifest = _validate(stage)
            _validate_migrations(stage)
            if Path(RESTORE_PENDING_DIR).exists():
                shutil.rmtree(RESTORE_PENDING_DIR)
            os.replace(stage, RESTORE_PENDING_DIR)
            _json_write(RESTORE_MARKER, {"staged_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "manifest": manifest})
            return manifest
        except (tarfile.TarError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise ValueError("Invalid backup archive structure") from error
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def _journal_path():
    return Path(config.DATA_DIR) / ".restore-journal.json"


def _remove(path):
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _rollback(journal):
    # Copy rather than consume rollback originals: interrupted rollback is repeatable.
    for item in reversed(journal["entries"]):
        target = Path(item["target"])
        _remove(target)
        if item["existed"]:
            source = Path(journal["rollback"]) / item["name"]
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    Path(RESTORE_MARKER).unlink(missing_ok=True)
    _journal_path().unlink()


def _prune_pre_restore_copies(keep=2):
    for p in sorted(Path(config.DATABASE_PATH).parent.glob(Path(config.DATABASE_PATH).name + ".pre-restore-*"), reverse=True)[keep:]:
        p.unlink()


def apply_pending_restore():
    with _LOCK:
        journal_path = _journal_path()
        if journal_path.exists():
            journal = json.loads(journal_path.read_text())
            if journal["state"] != "committed":
                _rollback(journal)  # failure propagates: never start on mixed state
                return False
            Path(RESTORE_MARKER).unlink(missing_ok=True)
            journal_path.unlink()
            return True
        if not Path(RESTORE_MARKER).exists():
            return False
        manifest = _validate(RESTORE_PENDING_DIR)
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        rollback = Path(config.DATA_DIR) / ("restore-rollback-" + stamp)
        rollback.mkdir()
        entries = []
        stage = Path(RESTORE_PENDING_DIR)
        # Each selected tree is replaced as a unit; stale live files never merge.
        for name in [*_INCLUDE_DIRS, *_INCLUDE_FILES, "hyprchat.db"]:
            if not (stage / name).exists():
                continue
            target = Path(config.DATABASE_PATH) if name == "hyprchat.db" else _content_path(name)
            item = {"name": name, "target": str(target), "existed": target.exists()}
            entries.append(item)
            saved = rollback / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if name == "hyprchat.db":
                    with closing(sqlite3.connect(target)) as live, closing(sqlite3.connect(saved)) as copy:
                        live.backup(copy)
                    shutil.copy2(saved, str(target) + ".pre-restore-" + stamp)
                elif target.is_dir():
                    shutil.copytree(target, saved)
                else:
                    shutil.copy2(target, saved)
        # Keep the validated stage immutable even if preparation is interrupted.
        activation_db = rollback / "activation.db"
        shutil.copy2(stage / "hyprchat.db", activation_db)
        # Retain locally configured passwords without putting them in the archive.
        with closing(sqlite3.connect(activation_db)) as restored, restored:
            restored.execute("PRAGMA journal_mode=DELETE")
            if restored.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone():
                with closing(sqlite3.connect(config.DATABASE_PATH)) as live:
                    if live.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone():
                        for uid, password in live.execute("SELECT id,password_hash FROM users"):
                            restored.execute("UPDATE users SET password_hash=? WHERE id=?", (password, uid))
            old_root = manifest.get("source_data_dir")
            if isinstance(old_root, str) and old_root:
                roots = {old_root: str(config.DATA_DIR)}
                for name in _INCLUDE_DIRS:
                    roots[str(Path(old_root) / name)] = str(_content_path(name))
                    original = (manifest.get('source_roots') or {}).get(name)
                    if isinstance(original,str) and original:
                        roots[original] = str(_content_path(name))
                tables = {row[0] for row in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table, column in [('artifacts','storage_path'),('kb_files','filepath'),('indexing_jobs','filepath')]:
                    if table not in tables: continue
                    for identity, path in restored.execute(f'SELECT id,{column} FROM {table}').fetchall():
                        if not isinstance(path,str): continue
                        for original,target in sorted(roots.items(),key=lambda pair:-len(pair[0])):
                            if path.startswith(original.rstrip('/')+'/'):
                                restored.execute(f'UPDATE {table} SET {column}=? WHERE id=?',(target.rstrip('/')+path[len(original.rstrip('/')):],identity))
                                break
        _sync_tree(rollback)
        journal = {"state": "prepared", "rollback": str(rollback), "entries": entries}
        _json_write(journal_path, journal)
        try:
            for suffix in ("-wal", "-shm", "-journal"):
                Path(config.DATABASE_PATH + suffix).unlink(missing_ok=True)
            for item in entries:
                target = Path(item["target"])
                source = activation_db if item["name"] == "hyprchat.db" else stage / item["name"]
                _remove(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    shutil.copy2(source, target)
            for item in entries:
                _sync_tree(item["target"])
            journal["state"] = "committed"
            _json_write(journal_path, journal)
        except Exception:
            _rollback(journal)
            raise
        Path(RESTORE_MARKER).unlink(missing_ok=True)
        journal_path.unlink()
        shutil.rmtree(stage)
        return True


def cancel_staged_restore():
    with _LOCK:
        if _journal_path().exists():
            raise ValueError("Restore activation needs startup recovery")
        Path(RESTORE_MARKER).unlink()
        shutil.rmtree(RESTORE_PENDING_DIR, ignore_errors=True)


def backup_status():
    pending = Path(RESTORE_MARKER).exists()
    info = json.loads(Path(RESTORE_MARKER).read_text()) if pending else {}
    return {"restore_pending": pending, "staged_at": info.get("staged_at", ""),
            "warnings": info.get("manifest", {}).get("warnings", []),
            "recovery_required": _journal_path().exists(),
            "leftover_archives": [os.path.basename(a) for a in sorted(glob(os.path.join(BACKUP_DIR, "hyprchat-backup-*.tar.gz")))]}
