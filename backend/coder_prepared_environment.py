"""Operation-owned writable environments, immutable reusable dependency entries."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import time

from coder_environment_publish import atomic_json, PreparationFailure, require_capacity, tree_manifest
from coder_mount_policy import MountPolicyError


def configure(store, operation_id, env, project_id=None):
    from coder_engine_cache import scope
    from coder_mount_policy import runtime_environment
    operation = store.get(operation_id)
    base = store.root / 'project-environments' / scope(project_id or operation['payload'].get('project_id') or operation['job_id'])
    private = base / 'operations' / operation_id
    private.mkdir(parents=True, exist_ok=True)
    env.update(DAEDALUS_ENVIRONMENT_ROOT=str(base), DAEDALUS_RUNTIME_POLICY='1',
               DAEDALUS_PRIVATE_DEPENDENCIES=json.dumps([str(private)]), DAEDALUS_PREPARATION_OPERATION=operation_id)
    env.update(runtime_environment(private / 'tool-caches'))
    return private


def binding_path(base, root, key):
    return Path(base) / 'bindings-v2' / (hashlib.sha256((str(Path(root).resolve()) + ':' + key).encode()).hexdigest() + '.json')


def is_ready(base, root, key, operation_id=None):
    marker = binding_path(base, root, key)
    if not marker.is_file(): return False
    record = json.loads(marker.read_text())
    from coder_engine_environment import browsers_ready
    browser_path = Path(base) / 'browsers'
    return record.get('version') == 2 and record.get('browser_path', str(browser_path)) == str(browser_path) and browsers_ready(Path(root), browser_path) and (operation_id is None or record.get('operation_id') == operation_id) and all(Path(p).is_dir() for p in record.get('dependencies', []))


def classify(code, tail, *, phase='setup', managed_paths=(), network_diagnostics=()):
    """Only known infrastructure signals override ordinary application errors."""
    if code and phase == 'setup' and network_diagnostics:
        trusted = [d for d in network_diagnostics if d.get('source') == 'sandbox_gateway']
        if trusted:
            selected = next((d for d in trusted if not d.get('retryable')), trusted[-1])
            return {'category': 'network_policy' if not selected.get('retryable') else 'network',
                    'phase': phase, 'exit_status': code, 'retryable': bool(selected.get('retryable')),
                    'detail': tail[-4000:], 'proxy_diagnostics': trusted[-32:]}
    match = re.search(r'DAEDALUS_PREPARATION_FAILURE:(\{[^\n]+\})', tail)
    detail = json.loads(match.group(1)) if match else None
    category = (detail or {}).get('category')
    if not category and code == 124 and (phase == 'setup' or 'deadline' in tail.lower()): category = 'timeout'
    if not category and code in {125, 126, 127}:
        from coder_project_runtime import HOST_TOOLS
        missing = re.findall(r'(?:sh|bash): (?:\d+: |line \d+: )?([\w.@/+-]+): (?:command )?not found', tail)
        if any(Path(name).name in HOST_TOOLS | {'composer', 'bundle'} for name in missing): category = 'runtime'
        elif re.search(r'Runtime (?:profile|broker)|Mount source|Invalid .*mount|Read-only child', tail): category = 'mount'
    if not category and re.search(r'No space left on device|Disk quota exceeded|ENOSPC|EDQUOT', tail, re.I): category = 'storage'
    if not category and re.search(r'ENOTFOUND|EAI_AGAIN|Temporary failure|Network is unreachable|Connection refused', tail, re.I): category = 'network'
    if not category and any(str(p) in tail for p in managed_paths) and re.search(r'Read-only file system|Permission denied|EROFS|EACCES', tail, re.I): category = 'permission'
    return {'category': category, 'phase': phase, 'exit_status': code, 'detail': tail[-4000:], 'retryable': category in {'network', 'timeout'}, **(detail or {})} if category else None


def clone(source, destination):
    """No hard links: mutations to a private environment cannot alter its base."""
    import subprocess
    manifest, size = tree_manifest(source)
    require_capacity(destination.parent, size)
    destination.mkdir()
    try:
        result = subprocess.run(['cp', '-a', '--reflink=auto', str(source) + '/.', str(destination)], capture_output=True)
        if result.returncode:
            shutil.rmtree(destination)
            shutil.copytree(source, destination, symlinks=True)
        if tree_manifest(destination)[0] != manifest:
            raise PreparationFailure('publication', 'Dependency copy failed validation.')
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def reusable_node(cwd):
    try: package = json.loads((cwd / 'package.json').read_text())
    except (OSError, ValueError): return False
    dependencies = {**package.get('dependencies', {}), **package.get('devDependencies', {})}
    return (not package.get('workspaces') and not set(package.get('scripts', {})) & {'preinstall', 'install', 'postinstall', 'prepare'}
            and not any(str(v).startswith(('file:', 'link:', 'workspace:')) for v in dependencies.values()))


def prepare(store, op_id, root, profile, project_id, env, folder):
    from coder_profiles import environment_key
    from coder_repository import safe_relative
    from coder_project_runtime import execute, uninstrumented, CommandTimeout
    from coder_engine_environment import prepare_browsers
    root, folder = Path(root), Path(folder)
    cwd = safe_relative(root, profile['cwd'])
    private = configure(store, op_id, env, project_id)
    key, versions = environment_key(cwd, profile)
    base = Path(env['DAEDALUS_ENVIRONMENT_ROOT'])
    entry = base / key
    marker = binding_path(base, cwd, key)
    generation = private / key / hashlib.sha256(str(cwd).encode()).hexdigest()[:16]
    generation.mkdir(parents=True, exist_ok=True)
    env.setdefault('DAEDALUS_PROJECT_ROOT', str(root))
    env.setdefault('DAEDALUS_AUDIT_DIR', str(folder / 'setup-audit'))
    Path(env['DAEDALUS_AUDIT_DIR']).mkdir(parents=True, exist_ok=True)
    env['PLAYWRIGHT_BROWSERS_PATH'] = str(base / 'browsers')
    env['PLAYWRIGHT_SKIP_BROWSER_GC'] = '1'
    Path(env['PLAYWRIGHT_BROWSERS_PATH']).mkdir(parents=True, exist_ok=True)
    if is_ready(base, cwd, key, op_id):
        return [{'id': profile['cwd'] + ':setup:' + str(i), 'cwd': profile['cwd'], 'phase': 'setup',
                 'origin': 'environment', 'command': cmd, 'passed': True, 'exit_code': 0,
                 'environment_reused': True, 'toolchains': versions} for i, cmd in enumerate(profile['commands'].get('setup', []))]
    ledger_path = store.root / 'preparation-ledgers' / (store.get(op_id)['job_id'] + '.json')
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {'failures': {}, 'seconds': 0}
    inflight = ledger.pop('inflight', None)
    if inflight:
        previous = store.get(inflight['operation_id'])
        last_seen = previous.get('ended') or previous['payload'].get('_usage_checkpoint', {}).get('at', inflight['started'])
        ledger['seconds'] += max(0, last_seen - inflight['started'])
    limit = store.get(op_id)['payload']['settings'].get('daedalus_preparation_seconds', 600)
    if ledger['failures'].get(key, 0) >= 2 or ledger['seconds'] >= limit:
        return [{'id': profile['cwd'] + ':setup:limit', 'phase': 'setup', 'passed': False,
                 'environment_fault': True, 'classification': 'environment',
                 'diagnostic': {'category': 'preparation_limit', 'phase': 'setup', 'retryable': False, 'attempt': ledger['failures'].get(key, 0)},
                 'log_tail': 'Preparation allowance exhausted; dependencies need controller attention.'}]
    started = time.monotonic()
    # Reserve the attempt before dispatch. A killed supervisor cannot silently
    # restore a preparation attempt when a new operation reopens this ledger.
    ledger['failures'][key] = ledger['failures'].get(key, 0) + 1
    ledger['inflight'] = {'key': key, 'operation_id': op_id, 'started': time.time()}
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(ledger_path, ledger)
    operation_payload = dict(store.get(op_id)['payload'])
    operation_payload['_preparation_deadline'] = min(operation_payload.get('_preparation_deadline', float('inf')), time.time() + limit - ledger['seconds'])
    store.update(op_id, payload=operation_payload)
    rows, dependencies = [], []
    marker.unlink(missing_ok=True)
    # Only controller-owned links may be switched. Real directories are retained
    # until the completed replacement is ready, then handled by the publisher.
    linked = {}
    try:
        for name in ('.venv', 'node_modules', 'vendor'):
            target = cwd / name
            if target.is_symlink():
                if not target.resolve().is_relative_to(base.resolve()):
                    raise PreparationFailure('unsafe_symlink', 'Dependency link leaves its project environment.', path=str(target))
                linked[name] = os.readlink(target)
                target.unlink()
        shared_downloads=[]
        if os.environ.get('HEPHAESTUS_SHARED_CACHE') == '1':
            from hephaestus_shared_cache import project_downloads
            from hephaestus_inventory import managed_root
            shared_root=managed_root(store.root)
            if shared_root:
                try:
                    shared_downloads=project_downloads(cwd,shared_root)
                    store.event(op_id,'shared_downloads',count=len(shared_downloads),reused=sum(d['reused'] for d in shared_downloads))
                except (OSError,ValueError) as error:
                    # Cache optimization cannot turn a supported private install into a failure.
                    store.event(op_id,'shared_downloads_unavailable',reason=str(error)[:300])
        wheels=[str(Path(d['path']).parent) for d in shared_downloads if d['kind']=='python']
        if wheels:env['PIP_FIND_LINKS']=' '.join(wheels)
        for i, original in enumerate(profile['commands'].get('setup', [])):
            command = original
            if command.startswith('python3 -m venv .venv'):
                venv = generation / '.venv'
                if not venv.exists(): venv.mkdir()
                target = cwd / '.venv'
                if target.exists():
                    if not (target / 'pyvenv.cfg').is_file():
                        raise PreparationFailure('classification', 'The .venv directory contains unrecognized authored files; it was preserved.')
                    # Do not relocate an existing venv or erase user files.
                    backup = cwd / '.daedalus-original-venv'
                    if backup.exists(): raise PreparationFailure('publication', 'An earlier environment switch needs recovery.')
                    target.rename(backup)
                target.symlink_to(venv, target_is_directory=True)
                command = command.replace('python3 -m venv .venv', 'python3 -m venv ' + shlex.quote(str(venv)), 1)
                dependencies.append(str(venv))
            if wheels and '.venv/bin/python -m pip install' in command:
                wheel_paths=[shlex.quote(d['path']) for d in shared_downloads if d['kind']=='python']
                command=command.replace('.venv/bin/python -m pip install', '.venv/bin/python -m pip install --no-deps '+' '.join(wheel_paths)+' && .venv/bin/python -m pip install',1)
            installed = entry / 'installed-v2' / 'node_modules'
            if installed.exists() and not (installed.parent / 'ready.json').exists():
                # The key lock excludes readers until publication completes.
                shutil.rmtree(installed)
            can_reuse = original.startswith(('npm ci', 'npm install', 'pnpm install', 'yarn install')) and reusable_node(cwd)
            reused = can_reuse and installed.is_dir() and (installed.parent / 'ready.json').is_file()
            if reused:
                if tree_manifest(installed)[0] != json.loads((installed.parent / 'ready.json').read_text())['tree']:
                    raise PreparationFailure('publication', 'Published dependency validation failed.')
                private_node = generation / 'node_modules'
                if private_node.exists() and tree_manifest(private_node)[0] != tree_manifest(installed)[0]:
                    # This operation has no readiness binding; a killed clone
                    # must never become a usable environment on restart.
                    shutil.rmtree(private_node)
                if not private_node.exists(): clone(installed, private_node)
                if not (cwd / 'node_modules').exists(): (cwd / 'node_modules').symlink_to(private_node, target_is_directory=True)
                else: reused = False
            if not reused and original.startswith(('npm ci','npm install')):
                archives=[d['path'] for d in shared_downloads if d['kind']=='node']
                if archives:command='npm cache add --ignore-scripts '+ ' '.join(shlex.quote(p) for p in archives)+' && '+command+' --prefer-offline'
            log = folder / ('setup-v2-' + key[:12] + '-' + str(i) + '.log')
            run_env = uninstrumented(dict(env))
            downloads = entry / 'downloads-v2'
            downloads.mkdir(parents=True, exist_ok=True)
            run_env['PIP_CACHE_DIR'] = str(downloads / 'pip')
            run_env['DAEDALUS_PROBE_WRITES'] = json.dumps([str(private), str(cwd), env['DAEDALUS_AUDIT_DIR']])
            network_diagnostics = []
            try:
                code = 0 if reused else execute(store, op_id, command, cwd, run_env, log,
                         writable=[root, private, downloads, Path(env['DAEDALUS_AUDIT_DIR'])], network_diagnostics=network_diagnostics)
            except CommandTimeout: code = 124
            tail = log.read_text(errors='replace')[-4000:] if log.exists() else 'Dependency environment reused'
            diagnostic = classify(code, tail, managed_paths=[private, cwd / '.venv', cwd / 'node_modules', cwd / 'vendor'], network_diagnostics=network_diagnostics) if code else None
            if diagnostic: diagnostic.update(command=original, managed_path=str(generation))
            rows.append({'id': profile['cwd'] + ':setup:' + str(i), 'cwd': profile['cwd'], 'phase': 'setup',
                         'origin': 'environment', 'command': original, 'passed': code == 0, 'exit_code': code,
                         'log_tail': tail, 'log': str(log), 'environment_reused': reused, 'toolchains': versions,
                         'environment_fault': bool(diagnostic), 'diagnostic': diagnostic})
            if code: break
            for name in ('node_modules', 'vendor'):
                target, destination = cwd / name, generation / name
                if name == 'vendor' or (name == 'node_modules' and not reusable_node(cwd)):
                    # Composer's generated autoloaders and workspace links bind
                    # to this source copy. Keep them at their installed location.
                    if target.is_dir(): dependencies.append(str(target))
                    continue
                if target.is_dir() and not target.is_symlink() and not destination.exists():
                    from coder_environment_publish import publish
                    publish(target, destination)
                if destination.is_dir(): dependencies.append(str(destination))
            if can_reuse and not installed.exists() and (generation / 'node_modules').is_dir():
                installed.parent.mkdir(parents=True, exist_ok=True)
                try:
                    clone(generation / 'node_modules', installed)
                    atomic_json(installed.parent / 'ready.json', {'version': 2, 'tree': tree_manifest(installed)[0]})
                except PreparationFailure as error:
                    # Workspace-bound links are usable privately, never reusable.
                    if error.diagnostic['category'] != 'unsafe_symlink': raise
        if all(row['passed'] for row in rows):
            from coder_environment_publish import publication_lock
            with publication_lock(Path(env['PLAYWRIGHT_BROWSERS_PATH'])):
                rows.extend(prepare_browsers(store, op_id, root, cwd, entry, env, folder))
        if all(row['passed'] for row in rows):
            marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(marker, {'version': 2, 'source': str(cwd), 'dependencies': dependencies, 'operation_id': op_id, 'browser_path': env['PLAYWRIGHT_BROWSERS_PATH']})
            ledger['failures'][key] = 0
            backup = cwd / '.daedalus-original-venv'
            if backup.is_dir(): shutil.rmtree(backup)
        else:
            if not any(row.get('environment_fault') for row in rows): ledger['failures'][key] = max(0, ledger['failures'][key] - 1)
    except (OSError, MountPolicyError) as error:
        from coder_environment_publish import failure
        if isinstance(error, MountPolicyError):
            raise PreparationFailure('mount', 'Dependency mount policy is inconsistent; project files are saved.', detail=str(error)) from error
        raise failure(error) from error
    finally:
        ledger['seconds'] += time.monotonic() - started
        ledger.pop('inflight', None)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(ledger_path, ledger)
        if not marker.exists():
            for name, previous in linked.items():
                target = cwd / name
                if not target.exists() and not target.is_symlink(): target.symlink_to(previous, target_is_directory=True)
    for row in rows:
        if row.get('diagnostic'):
            row['diagnostic'].update(attempt=ledger['failures'].get(key, 0), attempt_limit=2, environment_fingerprint=key)
    return rows
