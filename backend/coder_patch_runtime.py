"""Policy-7 experiment: a bounded Aider CLI operation, never a product dispatch.

All model requests go through the same local inference boundary as other v3
operations. The CLI cannot select a remote provider or enlarge Settings limits.
"""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import threading
import time

from context_policy import resolve, operation_settings, estimate_tokens
from coder_repository import safe_relative, validate_links

AIDER_VERSION = '0.86.2'


def readonly_command(command, writable):
    """Fail closed: audit code may write only controller-selected directories."""
    if not shutil.which('bwrap'):
        raise RuntimeError('Read-only audit execution requires bubblewrap on Codebox')
    args = ['bwrap', '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc',
            '--cap-drop', 'ALL', '--die-with-parent']
    for path in writable:
        args.extend(['--bind', str(path), str(path)])
    return [*args, '--', *command]


def tree_hashes(root, excludes=()):
    """Include every existing source file, including extensionless executables."""
    validate_links(root, excludes)
    result = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in excludes and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if name in excludes:
                continue
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def select_files(root, task, budget, excludes=(), preferred=()):
    """Select complete files. Never silently truncate a function or test."""
    hashes = tree_hashes(root, excludes)
    words = set(task.lower().replace('/', ' ').split())
    preferred = set(preferred)
    def rank(name):
        return (name not in preferred,
                not any(w in name.lower() for w in words if len(w) > 3),
                name.count('/'), name)
    selected, omitted, used = [], [], 0
    for name in sorted(hashes, key=rank):
        path = root / name
        if path.stat().st_size > budget * 4:
            omitted.append(name)
            continue
        try:
            content = path.read_text()
        except UnicodeError:
            continue
        cost = estimate_tokens(name + '\n' + content)
        if used + cost > budget:
            omitted.append(name)
            continue
        selected.append(name)
        used += cost
    return selected, omitted


EDITOR_OVERHEAD = 3000   # tokens Aider's own system prompt, examples and reply format take


def edit_targets(root, task, input_budget, excludes=(), preferred=()):
    """Editable files and read-only reference files for one Aider call.

    select_files sizes everything to a third of the context, and only NON-existent targets used to be
    re-added: an existing 30 KB target was silently dropped, Aider received no editable file, replied
    with prose and the job ended "the editor made no source changes". A target is what the edit is
    FOR, so it goes in whenever it fits the whole input budget, and reference files make room.
    """
    selected, _ = select_files(root, task, input_budget // 3, excludes, preferred)
    if not preferred:
        return selected, []
    files = [name for name in selected if name in preferred]
    reference = [name for name in selected if name not in preferred]
    cost = lambda name: estimate_tokens(name + '\n' + (root / name).read_text(errors='replace'))
    room = input_budget - min(estimate_tokens(task), input_budget // 3) - EDITOR_OVERHEAD - sum(cost(name) for name in files)
    oversized = []
    for name in preferred:
        path = safe_relative(root, name)
        if name in files:
            continue
        if not path.exists():
            # Pass missing targets to Aider without creating files in the controller.
            # Empty placeholders are not implementation progress.
            path.parent.mkdir(parents=True, exist_ok=True)
            files.append(name)
        elif path.is_file():
            needed = cost(name)
            if needed <= room:
                files.append(name); room -= needed
            else:
                oversized.append((name, path.stat().st_size, needed))
    if oversized and not files:
        name, size, needed = oversized[0]
        # ValueError blocks the job with this text and leaves Continue available after a Settings change;
        # a "no source changes" result would have spent a durable no_progress limit on a file never sent.
        raise ValueError(f'{name} ({size:,} bytes, about {needed:,} tokens) does not fit the editing context of {input_budget:,} input '
                         'tokens. Raise the Daedalus context in Settings, then Continue.')
    used = sum(cost(name) for name in files if (root / name).is_file())
    kept, spare = [], input_budget - min(estimate_tokens(task), input_budget // 3) - EDITOR_OVERHEAD - used
    for name in reference:
        needed = cost(name)
        if needed <= min(spare, input_budget // 3):
            kept.append(name); spare -= needed
    return files, kept


@contextmanager
def inference_bridge(store, operation_id, role, chat=None):
    """Loopback Ollama-compatible facade; no remote/cloud or tool dispatch."""
    from coder_inference import local_chat, require_local_model
    operation = store.get(operation_id)
    payload = operation['payload']
    details = require_local_model(payload['ollama_url'], payload['model']) if chat is None else {'capabilities': ['completion']}
    chat = chat or local_chat
    lock = threading.Lock()
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size <= 8 * 1024 * 1024:
                    raise ValueError('Invalid request size')
                request = json.loads(self.rfile.read(size))
                selected = request.get('model', request.get('name'))
                aliases = {payload['model']}
                if ':' not in payload['model']:
                    aliases.add(payload['model'] + ':latest')
                if selected not in aliases:
                    raise ValueError(f'Editor attempted to select another model: {selected!r} at {self.path}')
                if self.path == '/api/show':
                    self.reply(200, details)
                    return
                if self.path != '/api/chat' or request.get('tools') or request.get('stream'):
                    raise ValueError('Only non-streaming text chat is supported by this editor adapter')
                with lock:
                    log_dir = store.root / 'inference' / operation_id
                    log_dir.mkdir(parents=True, exist_ok=True)
                    log_path = log_dir / (str(len(list(log_dir.glob('*.json'))) + 1) + '.json')
                    log_path.write_text(json.dumps({'request': request}))
                    result = chat(store, operation_id, role, request['messages'], temperature=0)
                    log_path.write_text(json.dumps({'request': request, 'response': result}))
                self.reply(200, {'model': payload['model'], 'message': {'role': 'assistant', 'content': result},
                                 'done': True, 'done_reason': 'stop'})
            except Exception as error:
                partial = getattr(error, 'response', None)
                failure = {'category': getattr(error, 'failure_category', 'inference'),
                           'message': f'{type(error).__name__}: {error}', 'response': partial}
                marker = '>>>>>>> REPLACE'
                # A response cut at the output limit still holds complete blocks. Hand
                # those to the editor's own parser instead of discarding valid work.
                if failure['category'] == 'output_limit' and isinstance(partial, str) and marker in partial:
                    complete = partial[:partial.rindex(marker) + len(marker)] + '\n```\n'
                    failure.update(salvaged=True, salvaged_blocks=complete.count(marker))
                    errors.append(failure)
                    log_path.write_text(json.dumps({'request': request, 'failure': failure, 'response': complete}))
                    store.event(operation_id, 'editor_output_salvaged', blocks=failure['salvaged_blocks'])
                    self.reply(200, {'model': payload['model'], 'message': {'role': 'assistant', 'content': complete},
                                     'done': True, 'done_reason': 'stop'})
                    return
                errors.append(failure)
                if 'log_path' in locals():
                    log_path.write_text(json.dumps({'request': request, 'failure': errors[-1]}))
                store.event(operation_id, 'editor_inference_error', error=errors[-1])
                self.reply(400, {'error': errors[-1]})

        def do_GET(self):
            if self.path == '/api/tags':
                self.reply(200, {'models': [{'name': payload['model'], 'model': payload['model']}]})
            else:
                self.reply(404, {'error': 'Unsupported editor endpoint'})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', errors
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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


def edit(store, operation_id, root, task, *, read_root=None, preferred=(), binary=None, chat=None):
    """One focused edit. Actual patches survive CLI errors for controller checks."""
    from coder_worker_runtime import _boundary
    operation = _boundary(store, operation_id)
    payload = operation['payload']
    role = 'reviewer' if read_root else 'builder'
    policy = resolve(role, operation_settings(payload))
    binary = binary or '/opt/openhands-worker/aider-venv/bin/aider'
    version = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=20)
    if version.returncode or version.stdout.strip() != 'aider ' + AIDER_VERSION:
        raise ValueError(f'Policy-7 proof requires aider {AIDER_VERSION}')
    excludes = tuple(payload['settings']['daedalus_exclude_dirs']) + ('.git', '.aider', '__pycache__')
    before = tree_hashes(root, excludes)
    readonly_before = tree_hashes(read_root, excludes) if read_root else None
    folder = store.root / 'patches' / operation_id
    folder.mkdir(parents=True, exist_ok=True)
    files, reference_files = edit_targets(root, task, policy.input_budget, excludes, preferred)
    omitted = [name for name in select_files(root, task, policy.input_budget // 3, excludes, preferred)[1]
               if name not in files and name not in reference_files]
    reads = select_files(read_root, task, policy.input_budget // 3, excludes)[0] if read_root else []
    prompt = ('Apply the requested changes now using Aider SEARCH/REPLACE blocks. '
              'Every edit must name its file, then a fenced block containing <<<<<<< SEARCH, the exact old lines, '
              '=======, the replacement, and >>>>>>> REPLACE. For a new or empty file use an empty SEARCH section. '
              'Plain code fences without SEARCH/REPLACE markers do not edit files. '
              'Do not stop with a plan or a next-step announcement. '
              'Do not run commands; the controller runs checks. Keep the complete original requirements. '
              'Do not copy application logic into tests.\n' + task)
    if omitted:
        prompt += '\nOther indexed paths (not included as source):\n' + '\n'.join(omitted)
    if estimate_tokens(prompt) > policy.input_budget // 3:
        raise ValueError('Edit instructions exceed context; narrow the implementation batch')
    if (folder / 'aider.log').exists():
        # A second attempt inside one operation keeps the first attempt's evidence.
        for name in ('aider.log', 'request.md', 'chat.md'):
            if (folder / name).exists():
                (folder / name).replace(folder / ('previous-' + name))
    prompt_file = folder / 'request.md'
    prompt_file.write_text(prompt)
    settings_file = folder / 'model-settings.yml'
    settings_file.write_text(json.dumps([{'name': 'ollama_chat/' + payload['model'], 'edit_format': 'diff',
        'use_repo_map': False, 'extra_params': {'num_ctx': policy.num_ctx, 'num_predict': policy.num_predict, 'temperature': 0}}]))
    # Empty explicit config/env prevent a project upload from changing model routing.
    config = folder / 'empty.yml'; config.write_text('{}\n')
    env_file = folder / 'empty.env'; env_file.write_text('')
    command = [binary, '--model', 'ollama_chat/' + payload['model'], '--edit-format', 'diff',
        '--message-file', str(prompt_file), '--model-settings-file', str(settings_file),
        '--config', str(config), '--env-file', str(env_file), '--yes-always', '--no-stream',
        '--no-git', '--no-auto-commits', '--no-dirty-commits', '--no-auto-test', '--no-auto-lint',
        '--no-suggest-shell-commands', '--no-detect-urls', '--no-check-update', '--no-show-release-notes',
        '--no-analytics', '--map-tokens', '0', '--no-pretty',
        '--chat-history-file', str(folder / 'chat.md'), '--input-history-file', str(folder / 'input.txt')]
    for path in reads:
        command.extend(['--read', str(safe_relative(read_root, path))])
    for path in reference_files:
        command.extend(['--read', str(safe_relative(root, path))])
    command.extend(str(safe_relative(root, f)) for f in files)
    store.event(operation_id, 'patch_started', files=files, read_files=reads, omitted=omitted, version=AIDER_VERSION)
    log = folder / 'aider.log'
    with inference_bridge(store, operation_id, role, chat) as (url, errors):
        environment = {k: v for k, v in os.environ.items() if not k.startswith(('AIDER_', 'OLLAMA_', 'OPENAI_', 'ANTHROPIC_'))}
        environment.update(OLLAMA_API_BASE=url, AIDER_ANALYTICS_DISABLE='true', NO_COLOR='1')
        if read_root:
            home = folder / 'home'; home.mkdir(exist_ok=True)
            temporary = folder / 'tmp'; temporary.mkdir(exist_ok=True)
            environment['HOME'] = str(home)
            environment['TMPDIR'] = str(temporary)
            # Only the explicit audit test/metadata targets are writable. A model
            # cannot create a shadow copy of a referenced application beside them.
            audit_targets = [safe_relative(root, name) for name in preferred]
            for path in audit_targets:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(exist_ok=True)
            command = readonly_command(command, [*audit_targets, folder])
        with log.open('w') as output:
            process = subprocess.Popen(command, cwd=root, env=environment, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                while process.poll() is None:
                    _boundary(store, operation_id)
                    if any(not e.get('salvaged') for e in errors):
                        stop_process(process)
                        break
                    time.sleep(.1)
            finally:
                stop_process(process)
    after = tree_hashes(root, excludes)
    if read_root and tree_hashes(read_root, excludes) != readonly_before:
        raise ValueError('Audit editor changed read-only project source')
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    changed = [p for p in changed if p in before or (root / p).stat().st_size > 0]
    text = log.read_text(errors='replace')
    tail = text[-6000:]
    # The editor's own report separates "applied, bytes identical" from prose-only replies.
    applied = sorted(set(re.findall(r'^Applied edit to (.+)$', text, re.M)))
    category = ('output_limit' if any(e.get('category') == 'output_limit' for e in errors) else
                'invalid_patch' if 'SEARCH block failed' in text or 'did not match' in text else
                'source_changed' if changed else 'no_op' if applied else
                'environment' if process.returncode or errors else 'narration')
    result = {'changed': changed, 'applied': applied, 'exit_code': process.returncode, 'log': str(log), 'errors': errors,
              'category': category, 'log_tail': tail,
              'source_hashes': after, 'summary': f'{len(changed)} files changed', 'version': AIDER_VERSION}
    store.event(operation_id, 'patch_returned', **result)
    return result
