"""Quota allocation and reference-aware cache collection for the execution VM."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

GIB = 1024**3


def ensure_quota(root, directory, limit=10*GIB):
    root, directory = Path(root).resolve(), Path(directory).resolve()
    if not directory.is_relative_to(root) or directory == root:
        raise ValueError('Quota must name a managed child directory')
    if any(c in str(directory) for c in '\n\r"\' '): raise ValueError('Unsafe quota path')
    registry = root/'control/quotas'; registry.mkdir(parents=True, exist_ok=True)
    with (registry/'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = registry/'registry.json'
        records = json.loads(path.read_text()) if path.exists() else {}
        if str(directory) in records:
            return records[str(directory)]
        # Allocation is serialized and collision-free; IDs are not truncated path hashes.
        identity = max([1000, *records.values()])+1
        directory.mkdir(parents=True, exist_ok=True)
        subprocess.run(['xfs_quota','-x','-c',f'project -s -p {directory} {identity}',str(root)],check=True,capture_output=True)
        subprocess.run(['xfs_quota','-x','-c',f'limit -p bhard={limit} bsoft={limit} {identity}',str(root)],check=True,capture_output=True)
        records[str(directory)] = identity
        temporary = path.with_suffix('.tmp'); temporary.write_text(json.dumps(records)); temporary.replace(path)
        return identity


def collect(root, *, reserve=10*GIB):
    """Only known reconstructible worker caches, protected by their existing flock leases."""
    from coder_engine_cache import lease
    root = Path(root).resolve()
    removed = []
    usage = shutil.disk_usage(root)
    pressured = usage.used/usage.total >= .8
    if not pressured and usage.free >= reserve: return removed
    candidates = []
    for store in (root/'state').glob('*'):
        if store.is_symlink() or not store.is_dir(): continue
        pinned=set()
        for record in (store/'previews').glob('*/session.json'):
            try:
                session=json.loads(record.read_text())
                if session.get('lifecycle')=='pinned' and session.get('status')!='deleted':
                    pinned.update(Path(value).resolve() for value in session.get('cache_roots',[]))
            except (OSError,ValueError): continue
        for category in ('project-environments','operation-caches'):
            parent = store/category
            if parent.is_dir() and not parent.is_symlink():
                candidates.extend(p for p in parent.iterdir() if p.is_dir() and not p.is_symlink() and p.resolve() not in pinned)
    for path in sorted(candidates, key=lambda p:p.stat().st_mtime):
        usage = shutil.disk_usage(root)
        if usage.free >= reserve and (not pressured or usage.used/usage.total <= .7): break
        try:
            with lease(path, exclusive=True, blocking=False):
                shutil.rmtree(path)
                removed.append(str(path.relative_to(root)))
        except (BlockingIOError,FileNotFoundError): continue
    return removed


def maintenance(root):
    """Expire completed generic scratch, then apply pressure-based cache collection."""
    from coder_engine_cache import lease
    root=Path(root).resolve(); removed=[]; cutoff=time.time()-86400
    # Root/project files, revisions, artifacts and pinned application data are excluded.
    for workspace in (root/'projects').glob('*'):
        if workspace.is_symlink() or not workspace.is_dir(): continue
        try:
            with lease(workspace,exclusive=True,blocking=False):
                for path in (workspace/'tmp/codebox_workspace').glob('*'):
                    if path.is_dir() and not path.is_symlink() and path.stat().st_mtime<cutoff:
                        shutil.rmtree(path);removed.append(str(path.relative_to(root)))
        except (BlockingIOError,FileNotFoundError): continue
    removed.extend(collect(root))
    usage=shutil.disk_usage(root)
    if usage.free<10*GIB or usage.used/usage.total>=.8:
        # Docker itself protects layers in use; retained releases keep their tagged images.
        subprocess.run(['docker','builder','prune','-f','--filter','until=24h','--keep-storage','4GB'],
                       capture_output=True,timeout=90,check=True)
        subprocess.run(['docker','image','prune','-f','--filter','until=24h'],
                       capture_output=True,timeout=90,check=True)
    return removed


def require_space(root, required=0):
    collect(root, reserve=10*GIB+required)
    if shutil.disk_usage(root).free < 10*GIB+required:
        raise OSError('Hephaestus storage reserve reached; delete unused projects or application data before preparing more work')


def quota_root(root, path):
    relative=Path(path).resolve().relative_to(Path(root).resolve())
    parts=relative.parts
    if parts[0]=='projects':
        count=3 if len(parts)>2 and parts[1] in {'legacy','modern'} else 2
        return Path(root).joinpath(*parts[:count]),10*GIB
    if parts[0]=='runs':
        count=3 if len(parts)>2 and parts[1] in {'legacy','modern','generic'} else 2
        return Path(root).joinpath(*parts[:count]),2*GIB
    if parts[0]=='state' and len(parts)>=4:
        return Path(root).joinpath(*parts[:4]),(10 if parts[2] in {'jobs','previews','execution','project-environments'} else 2)*GIB
    if parts[0]=='applications' and len(parts)>=2: return Path(root).joinpath(*parts[:2]),10*GIB
    raise ValueError('Writable path has no bounded project or operation scope')


if __name__=='__main__':
    print(json.dumps({'removed':maintenance(os.environ.get('HEPHAESTUS_ROOT','/srv/hephaestus'))}))
