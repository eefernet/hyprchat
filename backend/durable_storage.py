"""Immutable local delivery storage. Copies precede DB publication; never move originals.

Legacy outputs are deliberately retained until an operator resolves their ownership.
An age threshold alone is not evidence that a file is disposable.
"""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit

import config


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def publish(path: str, *, expected_sha256: str = "") -> str:
    """Return a verified immutable copy, with atomic publication on the same disk."""
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Artifact source must be a regular file")
    sha = digest(source)
    if expected_sha256 and sha != expected_sha256:
        raise ValueError("Artifact content does not match its recorded hash")
    target = Path(config.ARTIFACT_DIR) / sha / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_symlink() or digest(target) != sha:
            raise ValueError("Durable artifact failed integrity verification")
        return str(target)
    fd, temporary = tempfile.mkstemp(prefix=".publish-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, out)
            out.flush()
            os.fsync(out.fileno())
        if digest(temporary) != sha:
            raise ValueError("Artifact changed during publication")
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(target)


def source_for(storage_path, url, filename):
    if storage_path:
        path = Path(storage_path)
        roots = [config.ARTIFACT_DIR, config.SANDBOX_OUTPUTS_DIR, config.UPLOAD_DIR, config.KB_DIR]
        if any(path.resolve().is_relative_to(Path(root).resolve()) for root in roots):
            return path if path.is_file() and not path.is_symlink() else None
        return None
    name = Path(unquote(urlsplit(url or "").path)).name or Path(filename or "").name
    for root in [config.SANDBOX_OUTPUTS_DIR, config.UPLOAD_DIR]:
        candidate = Path(root) / name
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def retain(storage_path, url, filename, sha=""):
    source = source_for(storage_path, url, filename)
    return publish(str(source), expected_sha256=sha or "") if source else storage_path


def migrate_references():
    """Restart-safe backfill; commit each verified reference, retaining old copies."""
    result = {"migrated": 0, "unresolved": 0}
    with sqlite3.connect(config.DATABASE_PATH) as conn:
        rows = conn.execute("SELECT id,storage_path,url,filename,sha256 FROM artifacts").fetchall()
        for aid, path, url, name, sha in rows:
            try:
                retained = retain(path, url, name, sha)
                if not retained or not Path(retained).is_file():
                    result["unresolved"] += 1
                elif retained != path:
                    conn.execute("UPDATE artifacts SET storage_path=? WHERE id=? AND storage_path IS ?",
                                 (retained, aid, path))
                    conn.commit()
                    result["migrated"] += 1
            except (OSError, ValueError):
                result["unresolved"] += 1
    return result


LEASE_SCHEMA = '''CREATE TABLE IF NOT EXISTS temporary_outputs (
    name TEXT PRIMARY KEY, user_id TEXT NOT NULL, job_id TEXT NOT NULL,
    state TEXT NOT NULL, created REAL NOT NULL, released REAL,
    sha256 TEXT NOT NULL DEFAULT '')'''


@contextmanager
def temporary_output(user_id, job_id):
    """Owned staging file. Only an explicit release makes a leftover disposable.

    A process crash leaves an active lease, which cleanup conservatively retains.
    Final deliverables are never leased or automatically reclaimed.
    """
    root = Path(config.SANDBOX_OUTPUTS_DIR) / '.hc-temporary'
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError('Temporary storage must not be a symlink')
    name = uuid.uuid4().hex + '.tmp'
    path = root / name
    with sqlite3.connect(config.DATABASE_PATH) as conn:
        conn.execute(LEASE_SCHEMA)
        conn.execute('INSERT INTO temporary_outputs(name,user_id,job_id,state,created) VALUES(?,?,?,?,?)',
                     (name, user_id, job_id, 'active', time.time()))
    try:
        yield path
    finally:
        with sqlite3.connect(config.DATABASE_PATH) as conn:
            if path.exists() and not path.is_symlink():
                conn.execute("UPDATE temporary_outputs SET state='released',released=?,sha256=? WHERE name=?",
                             (time.time(), digest(path), name))
            elif not path.exists():
                conn.execute('DELETE FROM temporary_outputs WHERE name=?', (name,))


def _referenced(conn, path):
    # Check all owners. A lease never overrules a durable reference.
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('artifacts', 'conversation_files', 'messages'):
        if table not in tables:
            continue
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        for column in columns & {'storage_path', 'url', 'filename', 'content', 'metadata'}:
            if conn.execute(f'SELECT 1 FROM {table} WHERE instr({column},?)>0 LIMIT 1', (path.name,)).fetchone():
                return True
    return False


def _identity(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def cleanup_outputs(cutoff=None, *, dry_run=False):
    root = Path(config.SANDBOX_OUTPUTS_DIR)
    result = {'deleted':0, 'freed_bytes':0, 'preserved':0, 'retained_bytes':0,
              'reclaimable_bytes':0, 'reclaimable_files':0, 'reasons':{},
              'reason':'Saved files, active jobs and unresolved legacy outputs are retained'}
    if not root.exists() or root.is_symlink():
        return result
    with sqlite3.connect(config.DATABASE_PATH) as conn:
        # Serialize rechecking and deletion with lease publication/release.
        conn.execute('BEGIN' if dry_run else 'BEGIN IMMEDIATE')
        has_leases = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='temporary_outputs'").fetchone()
        leases = {r[0]: r for r in conn.execute('SELECT name,state,released,sha256 FROM temporary_outputs')} if has_leases else {}
        paths = list(root.iterdir())
        temporary_root = root / '.hc-temporary'
        if temporary_root.is_dir() and not temporary_root.is_symlink():
            paths += list(temporary_root.iterdir())
        for path in paths:
            if path.is_dir() and not path.is_symlink():
                continue
            try:
                before = path.lstat()
                size = before.st_size
                lease = leases.get(path.name) if path.parent == temporary_root else None
                reason = 'unresolved legacy output'
                eligible = False
                if path.is_symlink():
                    reason = 'symlink retained'
                elif lease:
                    if lease[1] != 'released':
                        reason = 'active or interrupted job'
                    elif cutoff is not None and (lease[2] or 0) > cutoff:
                        reason = 'within retention period'
                    elif _referenced(conn, path):
                        reason = 'referenced by saved work'
                    elif not lease[3] or digest(path) != lease[3] or _identity(path.lstat()) != _identity(before):
                        reason = 'content changed since release'
                    else:
                        eligible = True
                if eligible:
                    result['reclaimable_bytes'] += size
                    result['reclaimable_files'] += 1
                    if not dry_run:
                        path.unlink()
                        conn.execute('DELETE FROM temporary_outputs WHERE name=?', (path.name,))
                        result['deleted'] += 1
                        result['freed_bytes'] += size
                        continue
                    reason = 'released temporary output'
                result['preserved'] += 1
                result['retained_bytes'] += size
                result['reasons'][reason] = result['reasons'].get(reason, 0) + 1
            except OSError:
                result['reasons']['unreadable or changed file'] = result['reasons'].get('unreadable or changed file', 0) + 1
    return result
