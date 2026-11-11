"""Controller-owned dependency refresh and matching Playwright browser installs."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shlex
import time
import uuid


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


def browser_specs(cwd):
    """Use the project's declared package, including its browser revision metadata."""
    cwd = Path(cwd)
    specs = []
    for name in ('playwright', 'playwright-core'):
        package = cwd / 'node_modules' / name / 'package.json'
        if package.is_file():
            data = json.loads(package.read_text())
            core = cwd / 'node_modules/playwright-core'
            metadata = (core if core.exists() else package.parent) / 'browsers.json'
            script = ('(async()=>{const p=require(' + json.dumps(str(package.parent)) + ');'
                      'const b=await p.chromium.launch({headless:true,args:["--no-sandbox"]});'
                      'await b.close();console.log("DAEDALUS_BROWSER_VALIDATED");})().catch(e=>{console.error(e);process.exit(1)})')
            specs.append((f'node {shlex.quote(str(package.parent / "cli.js"))} install chromium',
                          'node -e ' + shlex.quote(script),
                          {'runtime': 'node', 'version': data['version'], 'package': name,
                           'browser_manifest': hashlib.sha256(metadata.read_bytes()).hexdigest()}))
            break
    python_packages = list((cwd / '.venv/lib').glob('python*/site-packages/playwright/driver/package/package.json'))
    if python_packages:
        data = json.loads(python_packages[0].read_text())
        python = shlex.quote(str(cwd / '.venv/bin/python'))
        script = ('from playwright.sync_api import sync_playwright; '
                  'p=sync_playwright().start(); b=p.chromium.launch(headless=True,args=["--no-sandbox"]); '
                  'b.close(); p.stop(); print("DAEDALUS_BROWSER_VALIDATED")')
        specs.append((python + ' -m playwright install chromium', python + ' -c ' + shlex.quote(script),
                      {'runtime': 'python', 'version': data['version'], 'package': 'playwright',
                       'browser_manifest': hashlib.sha256((python_packages[0].parent / 'browsers.json').read_bytes()).hexdigest()}))
    return specs


def browser_marker(cache, identity):
    return Path(cache) / (hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest() + '.json')


def valid_browser_marker(cache, identity):
    try:
        record = json.loads(browser_marker(cache, identity).read_text())
        if record.get('version') != 1 or record.get('identity') != identity or not record.get('executables'): return False
        permitted = Path(cache).resolve()
        if record.get('shared_entry'):
            shared = Path(record['shared_entry'])
            parent = Path(os.environ.get('HEPHAESTUS_ROOT', '/srv/hephaestus')) / 'caches/shared/browsers'
            if shared.parent != parent or shared.is_symlink() or len(shared.name) != 64: return False
            if not valid_browser_marker(shared, identity): return False
            permitted = shared.resolve()
        for item in record['executables']:
            path = Path(cache) / item['path']
            if not path.resolve().is_relative_to(permitted) or not path.is_file() or not os.access(path, os.X_OK): return False
            stat = path.stat()
            if [stat.st_size, stat.st_mtime_ns] != item['stat']: return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def browsers_ready(cwd, cache):
    try: return all(valid_browser_marker(cache, identity) for _, _, identity in browser_specs(cwd))
    except (OSError, ValueError, KeyError): return False


def bind_shared_browser(cache, shared, identity):
    """Keep the sandbox's browser path stable while publishing immutable downloads.

    The project cache is controller-writable and container-read-only. Existing
    terminals see these new directory entries without changing their environment.
    """
    from coder_environment_publish import atomic_json
    from hephaestus_shared_cache import published_browser
    cache, shared = Path(cache), Path(shared)
    if not published_browser(shared) or not valid_browser_marker(shared, identity):
        raise ValueError('Shared browser entry is not a validated publication')
    cache.mkdir(parents=True, exist_ok=True)
    marker = browser_marker(cache, identity)
    marker.unlink(missing_ok=True)
    record = json.loads(browser_marker(shared, identity).read_text())
    # Publish all driver components, including ffmpeg, without copying binaries.
    for directory in shared.iterdir():
        if directory.name.startswith('.') or not directory.is_dir(): continue
        if directory.is_symlink(): raise ValueError('Shared browser component must be immutable')
        target = cache / directory.name
        if target.is_dir() and not target.is_symlink():
            target.rename(cache / ('.retired-' + uuid.uuid4().hex))
        link = cache / ('.link-' + uuid.uuid4().hex)
        link.symlink_to(directory, target_is_directory=True)
        link.replace(target)
    atomic_json(marker, {**record, 'shared_entry': str(shared)})


def prepare_browsers(store, operation_id, root, cwd, cache, env, folder):
    from coder_project_runtime import execute, CommandTimeout
    from coder_prepared_environment import classify
    from coder_environment_publish import atomic_json, require_capacity
    browser_cache = Path(env['PLAYWRIGHT_BROWSERS_PATH'])
    rows = []
    for command, validation, identity in browser_specs(cwd):
        if os.environ.get('HEPHAESTUS_SHARED_CACHE') == '1':
            from hephaestus_inventory import managed_root
            shared_root=managed_root(store.root)
            if shared_root:
                from hephaestus_shared_cache import browser
                from coder_sandbox import Sandbox
                import subprocess
                config=json.loads(Path('/etc/hephaestus/runtime.json').read_text())
                image=config['profiles'][os.environ.get('HEPHAESTUS_RUNTIME_PROFILE','core')]['image']
                def install(driver, destination):
                    clean={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8',
                           'PLAYWRIGHT_BROWSERS_PATH':str(destination),'PLAYWRIGHT_SKIP_BROWSER_GC':'1'}
                    import shlex
                    install_log=folder/'shared-browser-install.log'
                    code=execute(store,operation_id,'node '+shlex.quote(str(driver/'cli.js'))+' install chromium',
                                 driver.parent,clean,install_log,writable=[driver.parent])
                    if code:raise OSError('Shared browser preparation failed: '+install_log.read_text(errors='replace')[-1500:])
                try:
                    shared,reused=browser(shared_root,identity,image,install)
                    bind_shared_browser(browser_cache, shared, identity)
                    log=folder/('shared-browser-'+hashlib.sha256(str(identity).encode()).hexdigest()[:12]+'.log')
                    code=execute(store,operation_id,validation,cwd,dict(env),log,network_diagnostics=[])
                    rows.append({'id':'browser-setup:shared:'+identity['version'],'phase':'setup','origin':'environment',
                        'command':validation,'passed':code==0,'exit_code':code,'environment_reused':reused,
                        'browser_identity':identity,'log':str(log),'log_tail':log.read_text(errors='replace')[-4000:] if log.exists() else ''})
                    continue
                except (OSError,ValueError,subprocess.TimeoutExpired) as error:
                    rows.append({'id':'browser-setup:shared:'+identity['version'],'phase':'setup','origin':'environment',
                        'passed':False,'exit_code':1,'environment_fault':True,'browser_identity':identity,
                        'diagnostic':{'category':'browser_preparation','phase':'setup','retryable':True,'detail':str(error)[-2000:]}})
                    continue
        marker = browser_marker(browser_cache, identity)
        key = marker.stem
        log = folder / ('browser-setup-' + key[:12] + '.log')
        reused = valid_browser_marker(browser_cache, identity)
        network = []
        previous = json.loads(marker.read_text()) if reused else None
        marker.unlink(missing_ok=True)
        install_env = dict(env)
        generation = None
        if not reused:
            require_capacity(browser_cache, 1024**3)
            generation = browser_cache / ('.publish-browser-' + uuid.uuid4().hex)
            generation.mkdir()
            install_env['PLAYWRIGHT_BROWSERS_PATH'] = str(generation)
        # A receipt requires an actual launch by this project's driver, even
        # when installed files can be reused. Its ordinary sandbox has no
        # controller-only browser mount.
        run = validation if reused else command + ' && ' + validation
        try:
            code = execute(store, operation_id, run, cwd, install_env, log,
                           writable=([generation] if generation else []) + [Path(env['DAEDALUS_AUDIT_DIR'])], network_diagnostics=network)
        except CommandTimeout: code = 124
        tail = log.read_text(errors='replace')[-4000:] if log.exists() else ''
        diagnostic = classify(code, tail, network_diagnostics=network) if code else None
        if code and diagnostic is None:
            diagnostic = {'category': 'browser_preparation', 'phase': 'setup', 'retryable': False,
                          'exit_status': code, 'detail': tail}
        row = {'id': 'browser-setup:' + key[:12], 'phase': 'setup', 'origin': 'environment',
               'command': run, 'passed': code == 0, 'exit_code': code, 'environment_fault': code != 0,
               'diagnostic': diagnostic, 'environment_reused': reused, 'browser_identity': identity, 'log_tail': tail}
        if code == 0:
            if generation:
                # Publish new complete binaries without changing files that an
                # existing reader has open. Stale generations are lease-aware GC
                # data; a failed/interrupted download never replaces ready links.
                complete = browser_cache / ('.generation-' + uuid.uuid4().hex)
                generation.rename(complete)
                for directory in complete.iterdir():
                    if not directory.is_dir() or directory.name.startswith('.'): continue
                    target = browser_cache / directory.name
                    if target.is_dir() and not target.is_symlink():
                        target.rename(browser_cache / ('.retired-' + uuid.uuid4().hex))
                    link = browser_cache / ('.link-' + uuid.uuid4().hex)
                    link.symlink_to(directory, target_is_directory=True)
                    link.replace(target)
            executables = previous['executables'] if previous else []
            if not previous:
                for directory in browser_cache.iterdir():
                    if directory.name.startswith('.') or not directory.is_dir(): continue
                    for path in directory.rglob('*'):
                        if path.name in {'chrome', 'chrome-headless-shell', 'Chromium'} and path.is_file() and os.access(path, os.X_OK):
                            stat = path.stat()
                            executables.append({'path': str(path.relative_to(browser_cache)), 'stat': [stat.st_size, stat.st_mtime_ns]})
            if executables:
                atomic_json(marker, {'version': 1, 'identity': identity, 'executables': executables})
            else:
                row.update(passed=False, environment_fault=True, diagnostic={
                    'category': 'browser_preparation', 'phase': 'setup', 'retryable': False,
                    'detail': 'Browser validation returned without an installed project browser executable.'})
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
    with preparation(store, operation_id):
        for item in profile['profiles']:
            result.extend(prepare_environment(store, operation_id, repository.root, item, project_id, environment, folder))
            if any(row.get('environment_fault') for row in result): break
        if any(row.get('environment_fault') for row in result):
            diagnostic = next((row.get('diagnostic') for row in result if row.get('environment_fault') and row.get('diagnostic')), None) or {
                'category': 'environment', 'stage': 'dependency_preparation',
                'message': 'Dependency setup failed; your project files are saved.',
                'detail': next((row.get('log_tail', '') for row in result if row.get('environment_fault')), '')}
            store.update(operation_id, payload={**store.get(operation_id)['payload'], '_preparation_failure': diagnostic})
    passed = all(row.get('passed') for row in result)
    response = {'passed': passed, 'checks': result, 'profiles': profile['profiles'], 'environment_fingerprint': fingerprint,
                'guidance': ('Use these declared commands. Environment preparation does not verify application behavior.' if passed else
                             'Preparation failed. Do not use this incomplete environment. Inspect the diagnostic; infrastructure failures retain saved work.')}
    if passed:
        if any(row.get('browser_identity') for row in result) or browser_specs(repository.root):
            response['browser_path'] = environment.get('PLAYWRIGHT_BROWSERS_PATH')
            response['browser_prepared'] = True
            response['guidance'] += (' Chromium is already installed and validated for the declared Playwright package. '
                'Its managed browser cache is read-only. Run the tests now using normal Playwright browser resolution; '
                'do not run playwright install or npx playwright install inside this worker. '
                'Use prepare_environment again only after changing declared dependencies. '
                'Browser installation commands belong in the downloaded project setup instructions.')
        python = repository.root / '.venv/bin/python'
        # Project-relative: the controller replays commands on a fresh copy, where this workspace path does not exist.
        if python.is_file(): response['python'] = '.venv/bin/python'
    return response
