"""Evidence archival that holds the global admission lock for moments, not for the whole compression.

Before: one exclusive hold covered two full tree walks, gzip compression and hashing of every file, per
test environment. Any job or judge that arrived meanwhile was refused. Now:

1. Under exclusive admission (try once, as before): confirm the environment is idle. Release admission.
2. Under the resource lock only: fingerprint, compress to a pending file, hash, fingerprint again.
3. Under exclusive admission again (bounded wait): confirm it is still idle and unchanged, then publish.

Job admission does not look at resource locks, so step 3's re-check is what makes this safe: any work
admitted during step 2 is either still active or has changed the fingerprint, and both abort the publish.
Cleanup does take the resource lock, so it cannot delete an environment being archived.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile

from hephaestus_admission import AdmissionBusy, admitted
from hephaestus_inventory import atomic, lock, read

COMMIT_WAIT = 30.0


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 ** 2), b''): digest.update(chunk)
    return digest.hexdigest()


def _compress(path, row, target, excluded):
    files = []
    with tarfile.open(target, 'w:gz') as archive:
        for directory, dirs, names in os.walk(path, followlinks=False):
            dirs[:] = [name for name in dirs if name not in excluded
                       and not (Path(directory) / name).is_symlink() and not (Path(directory) / name / 'pyvenv.cfg').exists()]
            for name in names:
                file = Path(directory) / name
                if file.is_symlink() or not file.is_file(): continue
                relative = str(file.relative_to(path))
                files.append({'path': relative, 'bytes': file.stat().st_size, 'sha256': _sha(file)})
                archive.add(file, arcname='evidence/' + relative, recursive=False)
        body = json.dumps({'resource': row['id'], 'source': row['path'], 'files': files}, indent=2).encode()
        member = tarfile.TarInfo('manifest.json'); member.size = len(body); archive.addfile(member, io.BytesIO(body))
    target.chmod(0o600)


def archive_environment(root, row, path, outbox, *, idle, fingerprint, identity, excluded, commit_wait=COMMIT_WAIT, between=None):
    """Archive one inactive test environment. Returns its record, or None when it must be tried again later.

    `idle()` says whether nothing is running in the environment. `between` is a test hook that runs while
    the pending archive exists and no admission lock is held.
    """
    target, metadata = outbox / (row['id'] + '.tar.gz'), outbox / (row['id'] + '.json')
    pending, sidecar = target.with_suffix('.pending'), target.with_suffix('.pending.json')
    try:
        with lock(root, 'resource:' + row['id'], blocking=False):
            with lock(root, 'admission', blocking=False):        # start only when the VM is quiet, as before
                if not idle(): return None
            before = fingerprint(path)
            record = read(metadata)
            if target.exists() and record.get('fingerprint') == before:
                return record                                    # already published for exactly this evidence
            staged = read(sidecar)
            if not (pending.exists() and staged.get('fingerprint') == before and staged.get('sha256') == _sha(pending)):
                sidecar.unlink(missing_ok=True)
                _compress(path, row, pending, excluded)
                staged = {'fingerprint': before, 'sha256': _sha(pending), 'bytes': pending.stat().st_size}
                atomic(sidecar, staged)
            if between: between()
            try:
                with admitted(root, exclusive=True, seconds=commit_wait):
                    if not idle(): return None                   # work arrived meanwhile; the pending file is retried
                    if fingerprint(path) != before:
                        pending.unlink(missing_ok=True); sidecar.unlink(missing_ok=True)
                        raise RuntimeError('Test evidence changed during archival')
                    pending.replace(target)
                    record = {'id': row['id'], 'source': row['path'], 'identity': identity(path), 'fingerprint': before,
                              'sha256': staged['sha256'], 'bytes': staged['bytes']}
                    atomic(metadata, record)
                    sidecar.unlink(missing_ok=True)
                    return record
            except AdmissionBusy:
                return None                                      # keep the pending archive for the next pass
    except BlockingIOError:
        return None
