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


def cleanup_outputs(cutoff=None):
    """Preserve unresolved legacy and active output files for explicit review.

    New durable files never enter age-based cleanup. Until temporary-output
    leases are recorded, even an unreferenced output may belong to an active
    external worker or a legacy message URL, so no automatic unlink is safe.
    """
    root = Path(config.SANDBOX_OUTPUTS_DIR)
    count = sum(1 for p in root.iterdir() if p.is_file()) if root.exists() else 0
    return {"deleted": 0, "freed_bytes": 0, "preserved": count,
            "reason": "Legacy and active outputs preserved pending ownership review"}
