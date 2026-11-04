"""Diagnostic UI preflight in the existing builder sandbox, never acceptance."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import threading
import uuid

import requests

from coder_ui_checks import UIFlowValidationError, UIObservationFailure, interfaces, replay, validate


class _Cancelled(BaseException):
    """Do not let HTTP libraries catch a Stop signal as a retryable OSError."""


def inspect(root, directory, commands=None):
    from coder_repository import Repository, safe_relative
    from coder_profiles import discover, environment_key
    from coder_prepared_environment import binding_path, is_ready
    from context_policy import DEFAULTS
    repository = Repository(root, Path(directory) / 'inventory', DEFAULTS['daedalus_exclude_dirs'])
    inventory = repository.refresh()
    if inventory.get('errors'): raise ValueError('Cannot inventory UI preflight source: ' + str(inventory['errors'])[:1000])
    profile = discover(repository, commands or {})
    required = interfaces(repository, profile)
    manifest = Path(root) / '.daedalus-ui.json'
    flows = []
    if manifest.exists():
        if manifest.is_symlink() or manifest.stat().st_size > 65536:
            raise UIFlowValidationError([{'reason': 'UI manifest must be a regular project file of at most 64 KiB.'}])
        try: flows = json.loads(manifest.read_text())
        except (ValueError, UnicodeError) as error:
            raise UIFlowValidationError([{'reason': 'UI manifest is not valid JSON: ' + str(error)[:500]}]) from error
        validate(flows, 1, preflight_version=1)
    if required and not flows:
        raise UIFlowValidationError([{'reason': 'Missing UI flows for ' + ', '.join(required) + '. Create .daedalus-ui.json with real control interactions and text/value assertions, then use check_flows before Finish.'}])
    with repository.connect() as db:
        files = [dict(row) for row in db.execute('SELECT path,sha256,size FROM files ORDER BY path')]
    environment = {}; ready = True
    cache = os.environ.get('DAEDALUS_ENVIRONMENT_ROOT')
    for package in profile.get('profiles', []):
        if not package.get('commands', {}).get('setup'): continue
        cwd = safe_relative(Path(root), package['cwd'])
        key, versions = environment_key(cwd, package)
        marker = binding_path(cache, cwd, key) if cache else None
        environment[package['cwd']] = {'key': key, 'versions': versions,
            'binding': marker.read_text() if marker and marker.is_file() else ''}
        ready &= bool(cache and is_ready(cache, cwd, key, os.environ.get('DAEDALUS_PREPARATION_OPERATION')))
    identity = {'files': files, 'environment': environment, 'commands': commands or {},
                'paths': {k: os.environ.get(k, '') for k in ('PATH', 'VIRTUAL_ENV', 'PLAYWRIGHT_BROWSERS_PATH', 'DAEDALUS_CONTROLLER_BROWSERS_PATH')}}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {'flows': flows, 'files': files, 'profile': profile, 'ready': ready, 'fingerprint': fingerprint}


def emit(directory, operation_id, status, **values):
    packet = {'operation_id': operation_id, 'status': status, **values}
    path = os.environ.get('DAEDALUS_UI_TELEMETRY')
    if path:
        with open(path, 'a') as stream:
            stream.write(json.dumps({'type': 'ui_preflight', 'data': packet}) + '\n')
    return packet


def close_interactive(directory, operation_id):
    """Reset only a session owned by this operation; never kill a recorded PID."""
    ready = Path(directory) / 'ui' / 'ready.json'
    if not ready.exists(): return
    info = json.loads(ready.read_text())
    if info.get('operation_id') != operation_id: return
    with requests.Session() as client:
        client.trust_env = False
        try:
            r = client.post('http://127.0.0.1:' + str(int(info['port'])),
                json={'action': 'reset', '_operation_id': operation_id}, timeout=5)
            r.raise_for_status()
        except requests.ConnectionError:
            pass  # The previous SDK tool already closed its process.


def copy_source(root, destination, spec):
    """Copy source, share read-only managed environments, never duplicate dependencies."""
    from coder_repository import safe_relative
    from coder_environment_publish import require_capacity
    root, destination = Path(root), Path(destination)
    require_capacity(destination.parent, sum(row['size'] for row in spec['files']))
    for row in spec['files']:
        source = safe_relative(root, row['path']); target = destination / row['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for package in spec['profile'].get('profiles', []):
        relative = package['cwd']
        original = safe_relative(root, relative); copied = destination / relative
        copied.mkdir(parents=True, exist_ok=True)
        for name in ('.venv', 'node_modules', 'vendor'):
            dependency = original / name
            if dependency.is_dir() and not (copied / name).exists():
                (copied / name).symlink_to(dependency.resolve(), target_is_directory=True)


def build_copy(root, runtime, spec, deadline):
    """Recreate source-bound build outputs; never install or relocate an environment."""
    from coder_execution_utils import stop_process
    from coder_repository import safe_relative
    for index, check in enumerate(spec['profile'].get('checks', [])):
        if check.get('phase') != 'build': continue
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('UI preflight deadline reached before building the application')
        log = runtime / f'build-{index}.log'
        with log.open('wb') as stream:
            process = subprocess.Popen(['bash', '-c', check['command']], cwd=safe_relative(root, check.get('cwd', '.')),
                env={**os.environ, 'DAEDALUS_PROJECT_ROOT': str(root)}, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            try: code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error: raise TimeoutError('UI preflight build deadline reached') from error
            finally: stop_process(process)
        if code:
            with log.open('rb') as stream:
                stream.seek(0, 2); stream.seek(max(0, stream.tell()-4000)); tail = stream.read().decode(errors='replace')
            raise UIObservationFailure({'error': 'UI preflight application build failed', 'category': 'application_build',
                'command': check['command'], 'exit_status': code, 'log_tail': tail})


@contextmanager
def local_session(root, runtime, deadline, operation_id):
    from coder_ui_session import stop_session
    runtime.mkdir(parents=True)
    env = {**os.environ, 'DAEDALUS_UI_OPERATION': operation_id,
           'HOME': str(runtime / 'home'), 'XDG_CONFIG_HOME': str(runtime / 'config'),
           'XDG_DATA_HOME': str(runtime / 'data'), 'DAEDALUS_PROJECT_ROOT': str(root),
           'PYTHONPATH': os.pathsep.join([str(root), str(root / 'src'), os.environ.get('PYTHONPATH', '')])}
    for name in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'): Path(env[name]).mkdir()
    script = Path(__file__).with_name('coder_ui_session.py')
    with (runtime / 'session.log').open('wb') as log:
        process = subprocess.Popen(['dbus-run-session', '--', sys.executable, str(script), '--directory', str(runtime),
            '--seconds', str(max(1, int(deadline - time.monotonic())))], cwd=root, env=env,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    client = requests.Session(); client.trust_env = False
    try:
        ready = runtime / 'ready.json'
        while not ready.exists():
            if process.poll() is not None: raise RuntimeError('UI preflight session failed: ' + (runtime / 'session.log').read_text(errors='replace')[-2000:])
            if time.monotonic() >= deadline: raise TimeoutError('UI preflight deadline reached during startup')
            time.sleep(.1)
        port = json.loads(ready.read_text())['port']
        def action(request):
            from coder_repository import safe_relative
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError('UI preflight deadline reached')
            if request.get('action') == 'launch' and request.get('file_path'):
                file = safe_relative(root, request['file_path'])
                if not file.is_file(): raise ValueError('Browser entrypoint does not exist')
                request = {**request, 'url': file.as_uri(), 'command': ''}
            try: response = client.post(f'http://127.0.0.1:{port}', json=request, timeout=min(45, remaining))
            except requests.Timeout as error: raise TimeoutError('UI preflight action timed out') from error
            data = response.json()
            if response.status_code != 200: raise UIObservationFailure(data)
            return data
        yield action
    finally:
        client.close(); stop_session(process)


def run(root, directory, operation_id, *, commands=None, timeout=60):
    directory = Path(directory); folder = directory / 'ui-preflight'; folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / 'receipt.json'; deadline = time.monotonic() + max(.1, timeout)
    emit(directory, operation_id, 'running')
    result = {'passed': False, 'diagnostic_only': True, 'flows': []}
    previous_signals = {}
    if threading.current_thread() is threading.main_thread():
        def interrupted(signum, frame): raise _Cancelled('UI preflight cancelled')
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_signals[signum] = signal.signal(signum, interrupted)
    try:
        spec = inspect(root, folder, commands)
        result['fingerprint'] = spec['fingerprint']
        if not spec['flows']:
            result.update(passed=True, status='not_required')
        elif not spec['ready']:
            result.update(status='deferred', reason='Dependencies need controller preparation; independent verification remains required.')
        else:
            identity = {'operation_id': operation_id, 'instance': os.environ.get('DAEDALUS_UI_INSTANCE', ''), 'fingerprint': spec['fingerprint']}
            saved = json.loads(receipt.read_text()) if receipt.exists() else {}
            if identity['instance'] and saved.get('identity') == identity and saved.get('result', {}).get('passed'):
                result = {**saved['result'], 'reused': True}
            else:
                receipt.unlink(missing_ok=True)
                close_interactive(directory, operation_id)
                for index, flow in enumerate(spec['flows']):
                    # Each flow starts with clean disk, HOME, and browser storage.
                    with tempfile.TemporaryDirectory(prefix='flow-', dir=folder) as temporary:
                        root_copy = Path(temporary) / 'project'; root_copy.mkdir()
                        copy_source(root, root_copy, spec)
                        try:
                            build_copy(root_copy, Path(temporary), spec, deadline)
                            with local_session(root_copy, Path(temporary) / 'runtime', deadline, operation_id) as action:
                                outcome = replay(flow, action, strict=True, deadline=deadline)
                        except (InterruptedError, TimeoutError): raise
                        except Exception as failure:
                            outcome = {'passed': False, 'reason': str(failure)[:4000], 'diagnostic': getattr(failure, 'details', {})}
                        result['flows'].append({'flow_index': index, **{k: outcome[k] for k in ('passed', 'reason', 'diagnostic')}})
                    if time.monotonic() >= deadline: raise TimeoutError('UI preflight deadline reached')
                result['passed'] = all(row['passed'] for row in result['flows'])
                result['status'] = 'passed' if result['passed'] else 'failed'
                if any(r['diagnostic'].get('environment_fault') for r in result['flows']): result['status'] = 'deferred'
                if result['passed']:
                    after = inspect(root, folder, commands)
                    if after['fingerprint'] != spec['fingerprint']:
                        result.update(passed=False, status='incomplete', reason='Source or dependencies changed during UI preflight; run check_flows again.')
                    else:
                        from coder_environment_publish import atomic_json
                        atomic_json(receipt, {'identity': identity, 'result': result})
    except UIFlowValidationError as error:
        result.update(status='failed', errors=error.errors, reason=str(error)[:7000])
    except (InterruptedError, _Cancelled) as error:
        receipt.unlink(missing_ok=True)
        emit(directory, operation_id, 'interrupted', passed=False, diagnostic_only=True)
        raise InterruptedError('UI preflight cancelled') from error
    except Exception as error:
        receipt.unlink(missing_ok=True)
        from coder_prepared_environment import classify
        diagnostic = getattr(error, 'diagnostic', None) or classify(1, str(error), phase='ui_preflight', managed_paths=[folder])
        result.update(status='deferred' if diagnostic else 'incomplete', reason=str(error)[:4000],
            diagnostic=diagnostic, category=(diagnostic or {}).get('category', 'timeout' if isinstance(error, TimeoutError) else 'preflight_unavailable'))
    finally:
        for signum, handler in previous_signals.items(): signal.signal(signum, handler)
    emit(directory, operation_id, **result)
    return result
