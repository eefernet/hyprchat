"""Process-held leases and bounded collection of reconstructible worker caches."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import time


def scope(project_id):
    return hashlib.sha256(project_id.encode()).hexdigest()


@contextmanager
def lease(directory, *, exclusive=False, blocking=True):
    directory = Path(directory)
    locks = directory.parent.parent / 'cache-leases'
    locks.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(str(directory.absolute()).encode()).hexdigest() + '.lock'
    with (locks / name).open('a') as handle:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(handle, mode | (0 if blocking else fcntl.LOCK_NB))
        if not exclusive:
            directory.mkdir(parents=True, exist_ok=True)
            os.utime(directory, None)
        try: yield directory
        finally: fcntl.flock(handle, fcntl.LOCK_UN)


def collect(store, *, required_bytes=0, limit_bytes=None, reserve_bytes=0):
    """Never visits sources, snapshots, artifacts, logs, or SDK conversations."""
    from coder_worker_runtime import storage_usage
    removed = []
    candidates = []
    # A full store walk per cache makes recovery quadratic in retained jobs.
    # Account for removals incrementally; enforce_storage performs the final
    # fresh measurement, including any concurrent writes to leased caches.
    used = storage_usage(store.root) if limit_bytes is not None else 0
    for category in ('project-environments', 'operation-caches'):
        parent = store.root / category
        if parent.exists():
            candidates.extend(p for p in parent.iterdir() if p.is_dir() and not p.is_symlink())
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime):
        enough = (limit_bytes is None or used + required_bytes <= limit_bytes)
        enough = enough and shutil.disk_usage(store.root).free >= reserve_bytes + required_bytes
        if enough: break
        try:
            with lease(path, exclusive=True, blocking=False):
                size = storage_usage(path)
                shutil.rmtree(path)
                used = max(0, used - size)
                removed.append({'cache': str(path.relative_to(store.root)), 'bytes': size, 'at': time.time()})
        except (BlockingIOError, FileNotFoundError):
            continue
    return removed
