"""Execution and workspace utilities shared by the modern controller and checks."""
from pathlib import Path
import hashlib
import os
import re
import shutil
import signal
import subprocess
from coder_repository import safe_relative, validate_links


def readonly_command(command, writable):
    """Fail closed: audit code may write only controller-selected directories."""
    if not shutil.which('bwrap'):
        raise RuntimeError('Read-only audit execution requires bubblewrap on Hephaestus')
    args = ['bwrap', '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc',
            '--cap-drop', 'ALL', '--die-with-parent']
    for path in writable:
        args.extend(['--bind', str(path), str(path)])
    return [*args, '--', *command]


def tree_hashes(root, excludes=()):
    """Include every existing source file, including extensionless executables."""
    validate_links(root, excludes)
    from coder_generated import directories
    generated = directories(root)
    result = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in excludes and (Path(directory) / d).relative_to(root).as_posix() not in generated and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if name in excludes:
                continue
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def stop_process(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def restore_protected(repository, baseline, paths):
    """Put files the request said to leave alone back to their baseline bytes after an edit.

    The preservation check still runs afterwards; restoring here keeps a stray edit from costing a
    repair round that an editor without git could not use anyway. Returns the restored paths."""
    from coder_repository import safe_relative
    restored = []
    for path in paths or []:
        try:
            original = repository.git('show', baseline + ':' + path)
            target = safe_relative(repository.root, path)
        except (RuntimeError, ValueError):
            continue  # absent at baseline: nothing to preserve, the check row reports it
        if not target.is_file() or target.read_bytes() != original:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original)
            restored.append(path)
    return restored


KEY_ERROR = re.compile(r'\berror\b|Error:|\bFAILED\b|Assertion|Segmentation fault|panicked|undefined reference|'
                       r'cannot find|expected .* found|SyntaxError|Traceback')


NOISE = re.compile(r'^g?make(\[\d+\])?:|npm error A complete log|^\[.*INFO.*\]')


def key_error_lines(defects, limit=14):
    """Runners bury the decisive line under build-tool summaries; surface it first."""
    lines = []
    for defect in defects or []:
        for line in (defect.get('log_tail') or '').splitlines():
            text = line.strip()
            if text and KEY_ERROR.search(text) and not NOISE.search(text):
                lines.append(text[:300])
    return list(dict.fromkeys(lines))[:limit]


def sanitize_workspace(root, excludes=()):
    """Remove what an agent with a terminal leaves behind that the checkpoint cannot hold.

    `python3 -m venv env` links env/bin/python3 to the host interpreter. Only .venv/venv are excluded,
    so the link guard raised outside this builder's try, the fallback snapshot raised the same way
    and every later operation did too: a permanent wedge with the round's work lost. A virtualenv is
    an environment, never a deliverable (the controller builds its own), and a link that leaves the
    workspace can never be packaged. Returns the removed paths, relative to the workspace.
    """
    import os, shutil
    root, removed = root.resolve(), []
    for directory, dirs, files in os.walk(root, followlinks=False):
        here = type(root)(directory)
        dirs[:] = [name for name in dirs if name not in excludes and name != '.git']
        for name in list(dirs):
            path = here / name
            if not path.is_symlink() and (path / 'pyvenv.cfg').is_file():
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path.relative_to(root).as_posix()); dirs.remove(name)
        for name in [*dirs, *files]:
            path = here / name
            if path.is_symlink():
                try:
                    inside = path.resolve().is_relative_to(root)
                except (OSError, RuntimeError):
                    inside = False
                if not inside:
                    path.unlink(missing_ok=True)
                    removed.append(path.relative_to(root).as_posix())
                    if name in dirs:
                        dirs.remove(name)
    return removed


RUNTIME_LOG = re.compile(r'^(?:[^/]+/)?(?:server|app|application|api|backend|frontend|vite|uvicorn|gunicorn|flask|django|node|npm-debug|'
                         r'yarn-error|dev|debug|error|errors|access|output|out|run|start|nohup|stdout|stderr)(?:[.-][\w-]*)?\.(?:log|out)$', re.I)


FIXTURE_DIRS = {'tests', 'test', 'fixtures', 'testdata', 'samples', 'sample', 'examples', 'data', 'docs'}


def runtime_log(name):
    """A runtime log at the project or package root; a log under a fixture directory is a deliverable and stays."""
    parts = name.split('/')
    if any(part.lower() in FIXTURE_DIRS for part in parts[:-1]):
        return False
    return bool(RUNTIME_LOG.match(name))


def logs_are_input(task):
    """Log files are the application's domain (a log summarizer): never remove any of them."""
    return bool(re.search(r'\.log\b|\blog\s*files?\b|<[^<>]*log[^<>]*>', task or '', re.I))
