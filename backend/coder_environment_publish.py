"""Crash-recoverable publication across filesystems and XFS project quotas.

Callers hold the project cache lease and key lock for the entire preparation.
Published dependencies are never overwritten. Only temporary copies owned by this
publisher are removed; collection of cache entries still requires the GC lease.
"""
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import uuid


class PreparationFailure(OSError):
    def __init__(self, category, message, **details):
        super().__init__(message)
        self.diagnostic = {'category': category, 'stage': 'dependency_preparation',
                           'message': message, **details}


def failure(error, source=None, destination=None):
    if isinstance(error, PreparationFailure): return error
    category = ('storage' if error.errno in {errno.ENOSPC, errno.EDQUOT} else
                'permission' if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS} else 'publication')
    return PreparationFailure(category, 'Dependency setup failed; your project files are saved.',
        errno=error.errno, detail=str(error)[:1000], source=str(source or ''), destination=str(destination or ''))


@contextmanager
def publication_lock(cache):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / '.preparation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            # A killed publisher cannot hold this lock. Completed trees and source
            # backups are deliberately excluded from this cleanup.
            for path in cache.glob('.publish-*'):
                if path.is_dir() and not path.is_symlink(): shutil.rmtree(path)
            yield
        finally: fcntl.flock(lock, fcntl.LOCK_UN)


def tree_manifest(root):
    root = Path(root)
    rows, size = {}, 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if path.is_symlink():
                link = os.readlink(path)
                resolved = path.resolve()
                # Python venv interpreter links are allowed to trusted runtime
                # executables; arbitrary external dependency links are not.
                system_python = (path.parent.name == 'bin' and name.startswith('python') and
                                 any(resolved.is_relative_to(p) for p in (Path('/usr/bin'), Path('/usr/local/bin'))))
                if not resolved.is_relative_to(root.resolve()) and not system_python:
                    raise PreparationFailure('unsafe_symlink', 'Dependency contains a link outside its permitted roots.', path=relative)
                if resolved.is_relative_to(root.resolve()):
                    link = '@root/' + str(resolved.relative_to(root.resolve()))
                rows[relative] = ['link', mode, link]
            elif path.is_dir(): rows[relative] = ['directory', mode]
            elif path.is_file():
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024*1024), b''): digest.update(block)
                rows[relative] = ['file', mode, info.st_size, digest.hexdigest()]
                size += max(info.st_size, info.st_blocks * 512)
            else: raise PreparationFailure('unsupported_file', 'Dependency contains a special file.', path=relative)
    return rows, size


def require_capacity(destination, required):
    # Copy is transiently duplicated, so account for the full tree plus metadata.
    required += max(16 * 1024**2, required // 20)
    managed = Path(os.environ.get('HEPHAESTUS_ROOT', '/srv/hephaestus'))
    reserve = 10 * 1024**3 if destination.is_relative_to(managed) else 16 * 1024**2
    capacity_root = managed if destination.is_relative_to(managed) else destination
    if shutil.disk_usage(capacity_root).free < required + reserve:
        raise PreparationFailure('storage', 'Not enough free space to prepare dependencies; project files are saved.', required_bytes=required)
    registry = managed / 'control/quotas/registry.json'
    if destination.is_relative_to(managed) and registry.is_file():
        records = json.loads(registry.read_text())
        matches = [(Path(path), identity) for path, identity in records.items() if destination.is_relative_to(Path(path))]
        if not matches: raise PreparationFailure('storage', 'Dependency cache has no registered storage quota.')
        _, identity = max(matches, key=lambda pair: len(pair[0].parts))
        try:
            result = subprocess.run(['xfs_quota', '-x', '-c', f'quota -p -b -N -v {identity}', str(managed)],
                                    capture_output=True, text=True, check=True, timeout=10)
        except subprocess.SubprocessError as error:
            raise PreparationFailure('storage', 'Could not inspect the dependency-cache quota.', detail=str(error)[:500]) from error
        fields = next((line.split() for line in result.stdout.splitlines() if line.split() and line.split()[0].startswith('/')), None)
        if not fields or len(fields) < 4:
            raise PreparationFailure('storage', 'Could not determine remaining dependency-cache quota.')
        used, _, hard = map(int, fields[1:4])
        if hard and (hard - used) * 1024 < required:
            raise PreparationFailure('storage', 'Dependency-cache quota is full; project files are saved.', required_bytes=required, available_bytes=max(0, hard-used)*1024)


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream)
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


def publish(source, destination):
    source, destination = Path(source), Path(destination)
    backup = source.with_name('.daedalus-publish-' + source.name)
    marker = destination.parent / ('.published-' + source.name + '.json')
    temporary = destination.parent / ('.publish-' + uuid.uuid4().hex)
    link = source.with_name('.daedalus-link-' + source.name)
    try:
        # Finish an interrupted workspace switch before attempting more installs.
        if backup.exists() and not source.exists() and not source.is_symlink(): backup.rename(source)
        if source.is_symlink():
            if source.resolve() != destination.resolve():
                raise PreparationFailure('publication', 'Workspace dependency points at a different environment.')
            return
        if destination.exists():
            if not marker.is_file():
                # Recover rename-before-marker only when the original has moved.
                if source.exists():
                    raise PreparationFailure('publication', 'An incomplete cache entry requires cleanup before reuse.')
                manifest, _ = tree_manifest(destination)
                atomic_json(marker, {'version': 1, 'tree': manifest})
            manifest, _ = tree_manifest(destination)
            if manifest != json.loads(marker.read_text())['tree']:
                raise PreparationFailure('publication', 'Dependency cache validation failed; the existing cache was preserved.')
            if source.exists() and tree_manifest(source)[0] != manifest:
                raise PreparationFailure('publication', 'Dependency cache differs from the workspace; the original files were preserved.')
        else:
            manifest, size = tree_manifest(source)
            try:
                if any(row[0] == 'link' and os.path.isabs(os.readlink(source / name)) and row[2].startswith('@root/') for name, row in manifest.items()):
                    raise OSError(errno.EXDEV, 'Internal absolute links require rebasing')
                source.rename(destination)
            except OSError as error:
                if error.errno != errno.EXDEV: raise
                require_capacity(destination.parent, size)
                shutil.copytree(source, temporary, symlinks=True, copy_function=shutil.copy2)
                # Rebase absolute internal links, without traversing external ones.
                for relative, entry in manifest.items():
                    if entry[0] == 'link' and entry[2].startswith('@root/'):
                        path = temporary / relative
                        path.unlink(); path.symlink_to(os.path.relpath(temporary / entry[2][6:], path.parent))
                if tree_manifest(temporary)[0] != manifest:
                    raise PreparationFailure('publication', 'Copied dependency files did not match the source.')
                temporary.rename(destination)
            atomic_json(marker, {'version': 1, 'tree': tree_manifest(destination)[0]})
        link.unlink(missing_ok=True)
        link.symlink_to(destination, target_is_directory=True)
        if source.exists():
            if backup.exists(): shutil.rmtree(backup)
            source.rename(backup)
        try: link.replace(source)
        except BaseException:
            if backup.exists() and not source.exists(): backup.rename(source)
            raise
        if backup.exists(): shutil.rmtree(backup)
    except OSError as error:
        raise failure(error, source, destination) from error
    finally:
        link.unlink(missing_ok=True)
        if temporary.exists(): shutil.rmtree(temporary)
