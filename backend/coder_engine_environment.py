"""Controller-owned dependency refresh and matching Playwright browser installs."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shlex
import time


@contextmanager
def preparation(store, operation_id):
    operation = store.get(operation_id)
    payload = dict(operation['payload'])
    remaining = payload['settings'].get('daedalus_preparation_seconds', 600) - payload.get('_preparation_seconds', 0)
    if remaining <= 0: raise TimeoutError('Environment preparation allowance exhausted')
    started = time.time()
    store.update(operation_id, payload={**payload, '_preparation_active_started': started,
                                       '_preparation_deadline': started + remaining})
    store.event(operation_id, 'preparation_started')
    try: yield
    finally:
        current = dict(store.get(operation_id)['payload'])
        elapsed = time.time() - started
        current['_active_started'] = current.get('_active_started', operation['started']) + elapsed
        current['_preparation_seconds'] = current.get('_preparation_seconds', 0) + elapsed
        current.pop('_preparation_active_started', None); current.pop('_preparation_deadline', None)
        store.update(operation_id, payload=current)
        store.event(operation_id, 'environment_preparation', seconds=elapsed)


def prepare_browsers(store, operation_id, root, cwd, cache, env, folder):
    from coder_project_runtime import execute, CommandTimeout
    from coder_repository import safe_relative
    browser_cache = Path(env['PLAYWRIGHT_BROWSERS_PATH'])
    commands = []
    for name in ('playwright', 'playwright-core'):
        package = cwd / 'node_modules' / name / 'package.json'
        if package.is_file():
            data = json.loads(package.read_text())
            commands.append((f'node {shlex.quote(str(package.parent / "cli.js"))} install chromium',
                             {'runtime': 'node', 'version': data['version'], 'package': name}))
            break
    python_packages = list((cwd / '.venv/lib').glob('python*/site-packages/playwright/driver/package/package.json'))
    if python_packages:
        data = json.loads(python_packages[0].read_text())
        commands.append((f'{shlex.quote(str(cwd / ".venv/bin/python"))} -m playwright install chromium',
                         {'runtime': 'python', 'version': data['version'], 'package': 'playwright'}))
    rows = []
    for command, identity in commands:
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        marker = browser_cache / (key + '.json')
        log = folder / ('browser-setup-' + key[:12] + '.log')
        reused = marker.exists()
        try:
            code = 0 if reused else execute(store, operation_id, command, cwd, env, log,
                writable=[browser_cache, Path(env['DAEDALUS_AUDIT_DIR'])])
        except CommandTimeout: code = 124
        row = {'id': 'browser-setup:' + key[:12], 'phase': 'setup', 'origin': 'environment',
               'command': command, 'passed': code == 0, 'exit_code': code, 'environment_fault': code != 0,
               'environment_reused': reused, 'browser_identity': identity,
               'log_tail': log.read_text(errors='replace')[-4000:] if log.exists() else 'Browser environment reused'}
        if code == 0:
            marker.write_text(json.dumps(identity))
        rows.append(row)
    return rows


def refresh(store, operation_id, repository, manifests, environment, folder):
    from coder_profiles import MANIFESTS, LOCKFILES
    from coder_repository import safe_relative
    from coder_project_runtime import contract, prepare_environment
    if not isinstance(manifests, list) or len(manifests) > 30 or any(not isinstance(p, str) for p in manifests):
        raise ValueError('Provide at most thirty project-relative dependency manifest paths')
    for name in manifests:
        path = safe_relative(repository.root, name)
        if path.is_symlink() or not path.is_file() or path.name not in MANIFESTS | LOCKFILES:
            raise ValueError('Environment refresh accepts existing dependency manifests and lockfiles only')
    folder.mkdir(parents=True, exist_ok=True)
    project_id = store.get(operation_id)['payload'].get('project_id') or store.get(operation_id)['job_id']
    profile = contract(repository, managed_dependencies=True)
    result = []
    dependency_hashes = {}
    excluded = set(store.get(operation_id)['payload']['settings']['daedalus_exclude_dirs']) | {'vendor'}
    from coder_generated import directories, excluded as is_generated
    generated = directories(repository.root)
    for directory, folders, files in os.walk(repository.root, followlinks=False):
        folders[:] = [name for name in folders if name not in excluded and not is_generated((Path(directory) / name).relative_to(repository.root), generated) and not (Path(directory) / name).is_symlink()]
        for name in set(files) & (MANIFESTS | LOCKFILES):
            path = Path(directory) / name
            if not path.is_symlink():
                dependency_hashes[str(path.relative_to(repository.root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps({'profile': profile, 'files': dependency_hashes}, sort_keys=True).encode()).hexdigest()
    payload = store.get(operation_id)['payload']
    attempts = dict(payload.get('_environment_failures', {}))
    if attempts.get(fingerprint, 0) >= 2:
        raise ValueError('Two preparation attempts failed for these unchanged dependencies; inspect the reported environment failure')
    try:
        with preparation(store, operation_id):
            for attempt in range(2 - attempts.get(fingerprint, 0)):
                result = []
                for item in profile['profiles']:
                    result.extend(prepare_environment(store, operation_id, repository.root, item, project_id, environment, folder))
                    if any(row.get('environment_fault') for row in result): break
                if not any(row.get('environment_fault') for row in result): break
                attempts[fingerprint] = attempts.get(fingerprint, 0) + 1
                store.update(operation_id, payload={**store.get(operation_id)['payload'], '_environment_failures': attempts})
            if any(row.get('environment_fault') for row in result):
                diagnostic = next((row.get('diagnostic') for row in result if row.get('diagnostic')), None) or {
                    'category': 'environment', 'stage': 'dependency_preparation',
                    'message': 'Dependency setup failed; your project files are saved.',
                    'detail': next((row.get('log_tail', '') for row in result if row.get('environment_fault')), '')}
                store.update(operation_id, payload={**store.get(operation_id)['payload'], '_preparation_failure': diagnostic})
    except Exception:
        attempts[fingerprint] = attempts.get(fingerprint, 0) + 1
        store.update(operation_id, payload={**store.get(operation_id)['payload'], '_environment_failures': attempts})
        raise
    passed = all(row.get('passed') for row in result)
    if not passed and not any(row.get('environment_fault') for row in result):
        attempts[fingerprint] = attempts.get(fingerprint, 0) + 1
        store.update(operation_id, payload={**store.get(operation_id)['payload'], '_environment_failures': attempts})
    return {'passed': passed, 'checks': result,
            'profiles': profile['profiles'], 'browser_path': environment.get('PLAYWRIGHT_BROWSERS_PATH'),
            'python': str(repository.root / '.venv/bin/python'),
            'guidance': 'Use these declared commands. Environment preparation does not verify application behavior.'}
