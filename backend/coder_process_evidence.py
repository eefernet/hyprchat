"""Controller-owned observations of compiled applications executed by an audit.

The tracer runs outside bubblewrap. Its output is never writable by the audit.
Source mentions, failed execs, and merely opening an artifact are not launches.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import shutil

from coder_repository import file_hash

VERSION = 1
MAX_TRACE_BYTES = 32 * 1024 * 1024
_STRING = r'"(?:[^"\\]|\\.)*"'


class TraceUnavailable(RuntimeError):
    pass


def trace_command(command, path):
    tracer = shutil.which('strace')
    if not tracer:
        raise TraceUnavailable('Compiled audit execution requires strace on Codebox')
    return [tracer, '-f', '-q', '--decode-pids=pidns', '-yy', '-s', '65536', '-o', str(path), '-e',
            'trace=execve,execveat,clone,clone3,fork,vfork,chdir,fchdir,open,openat,openat2', *command]


def artifacts(root, packages):
    """Hash application artifacts after builds, before the read-only audit runs."""
    root = Path(root).resolve()
    found = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {'.git', '.venv', 'venv', 'node_modules', 'vendor', '.gradle'}
                   and not (Path(directory) / d).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            with path.open('rb') as handle:
                magic = handle.read(4)
            kind = 'native' if magic == b'\x7fELF' else (
                'jvm' if path.suffix in {'.jar', '.class'} and magic in {b'PK\x03\x04', b'\xca\xfe\xba\xbe'} else
                'dotnet' if path.suffix == '.dll' and magic[:2] == b'MZ' else '')
            if not kind:
                continue
            relative = path.relative_to(root).as_posix()
            owners = [p for p in packages if p == '.' or relative.startswith(p.rstrip('/') + '/')]
            owner = max(owners, key=len) if owners else '.'
            found[str(path)] = {'path': relative, 'sha256': file_hash(path), 'kind': kind, 'component': owner}
    return found


def _strings(text):
    values = []
    for token in re.findall(_STRING, text):
        try:
            values.append(ast.literal_eval(token))
        except (SyntaxError, ValueError):
            return []
    return values


def observe(path, root, cwd, known, revision):
    """Read successful execs and runtime artifact loads from the same process.

    CWD follows fork/chdir, including execs behind a shell. JVM/.NET launches
    must actually open a pre-hashed application artifact, not just name it.
    """
    path, root = Path(path), Path(root).resolve()
    if not path.is_file() or path.stat().st_size > MAX_TRACE_BYTES:
        raise TraceUnavailable('Compiled audit process trace is missing or exceeds 32 MiB')
    processes, pending, witnessed = {}, {}, False
    for line in path.read_text(errors='replace').splitlines():
        match = re.match(r'\s*(\d+)\s+(.*)', line)
        if not match:
            continue
        pid, text = match.groups()
        state = processes.setdefault(pid, {'cwd': str(cwd), 'launch': None, 'artifacts': [], 'ended': False})
        if '<unfinished ...>' in text:
            pending[pid] = text.split('<unfinished ...>', 1)[0]
            continue
        if text.startswith('<... '):
            prefix = pending.pop(pid, None)
            if prefix is None or ' resumed>' not in text:
                raise TraceUnavailable('Incomplete compiled audit process trace')
            text = prefix + text.split(' resumed>', 1)[1]
        if text.startswith('+++ '):
            state['ended'] = True
            state['termination'] = text
            continue
        call = re.match(r'(\w+)\((.*)\)\s+=\s+(-?\d+)', text)
        if not call:
            continue
        name, args, result = call.groups()
        result = int(result)
        if result < 0:
            continue
        words = _strings(args)
        if name in {'clone', 'clone3', 'fork', 'vfork'} and result > 0:
            translated = re.search(r"=\s+\d+\s+/\*\s+(\d+) in strace's PID NS \*/", text)
            if translated:
                result = int(translated.group(1))
            child = processes.setdefault(str(result), {'launch': state['launch'], 'artifacts': [], 'ended': False})
            child['cwd'] = state['cwd']
        elif name in {'chdir', 'fchdir'}:
            target = words[0] if words else next(iter(re.findall(r'<([^>]+)>', args)), '')
            if target:
                state['cwd'] = str((Path(state['cwd']) / target).resolve())
        elif name in {'execve', 'execveat'} and words:
            witnessed = True
            target = str((Path(state['cwd']) / words[0]).resolve())
            # exec replaces the process image. Only artifacts loaded by this image count.
            state.update(launch={'executable': target, 'arguments': words[1:]}, artifacts=[], ended=False)
            if target in known and known[target]['kind'] == 'native':
                state['artifacts'].append(known[target])
        elif name in {'open', 'openat', 'openat2'} and state['launch']:
            # -yy decodes the successful returned fd to its canonical file path.
            returned = re.search(r'=\s+\d+<([^>]+)>', text)
            if not returned:
                continue
            target = returned.group(1)
            artifact = known.get(target)
            runtime = Path(state['launch']['executable']).name
            if artifact and ((runtime == 'java' and artifact['kind'] == 'jvm') or
                             (runtime == 'dotnet' and artifact['kind'] == 'dotnet')):
                state['artifacts'].append(artifact)
    if not witnessed or pending:
        raise TraceUnavailable('Compiled audit trace did not record a complete process execution')
    observed = []
    for pid, state in processes.items():
        if not state['ended'] or not state['artifacts']:
            continue
        bound = {a['path']: a for a in state['artifacts']}
        for artifact in bound.values():
            target = root / artifact['path']
            if not target.is_file() or file_hash(target) != artifact['sha256']:
                raise TraceUnavailable('Executed application artifact changed during audit')
        observed.append({'pid': pid, **state['launch'], 'termination': state['termination'],
                         'artifacts': list(bound.values())})
    return {'version': VERSION, 'revision_id': revision, 'processes': observed}
