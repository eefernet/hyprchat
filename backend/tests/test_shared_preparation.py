import json
from pathlib import Path
import pytest

from coder_engine_contract import freeze, checks
from coder_generated import directories, ClassificationConflict
from coder_mount_policy import mounts
from coder_prepared_environment import classify


def test_mount_policy_exact_grants_and_conflicts(tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    dep = tmp_path / 'cache' / 'private'; dep.mkdir(parents=True)
    result = mounts([root, dep.parent], [root, dep])
    assert next(m for m in result if m['target'] == str(dep))['writable']
    with pytest.raises(ValueError, match='Read-only child'):
        mounts([dep], [dep.parent])
    with pytest.raises(ValueError, match='published'):
        mounts([], [dep], published=[dep])


@pytest.mark.parametrize('folder,name', [('todo_app', 'cli.py'), ('src', 'app.ts'), ('commands', 'main.go'), ('src/main/java', 'Main.java')])
def test_nested_deliverable_does_not_invent_root_file(tmp_path, folder, name):
    contract = freeze(f'Create the {folder}/ package with {name}')
    assert [c['path'] for c in contract['criteria']] == [folder + '/' + name]
    target = tmp_path / folder / name; target.parent.mkdir(parents=True); target.write_text('source')
    assert all(row['passed'] for row in checks(tmp_path, '', 'rev', contract))
    target.unlink()
    (tmp_path / name).write_text('wrong place')
    assert not checks(tmp_path, '', 'rev', contract)[0]['passed']


def test_literal_contract_provenance_and_ambiguity(tmp_path):
    contract = freeze('Create cli.py\nRun java -jar target/app.jar\nDo not create fake.py\nRead example.jar supplied as input.')
    assert {c['path'] for c in contract['criteria']} == {'cli.py', 'target/app.jar'}
    assert all(c['provenance'] == 'user_request' for c in contract['criteria'])
    uncertain = freeze('A package with cli.py')
    assert not uncertain['criteria'] and uncertain['unverified']
    inherited = freeze('Add README documentation', contract)
    assert {'cli.py', 'target/app.jar'} <= {c['path'] for c in inherited['criteria']}


def test_corroborated_environment_exclusions_and_authored_conflict(tmp_path):
    for name in ('.venv-dev', '.venv-docs', 'my-site'):
        (tmp_path / name).mkdir()
    (tmp_path / '.venv-dev/pyvenv.cfg').write_text('home = /usr/bin')
    (tmp_path / '.venv-dev/bin').mkdir()
    (tmp_path / '.venv-docs/README.md').write_text('authored docs')
    info = tmp_path / '.venv-cache-deps/thing.dist-info'; info.mkdir(parents=True)
    for name in ('RECORD', 'METADATA', 'INSTALLER'): (info / name).write_text('installed')
    assert directories(tmp_path) == {'.venv-dev', '.venv-cache-deps'}
    with pytest.raises(ClassificationConflict): directories(tmp_path, ['.venv-dev/user.py'])


def test_infrastructure_errors_are_not_package_errors():
    assert classify(1, 'Read-only file system: /work/.venv/lib/package.py', managed_paths=['/work/.venv'])['category'] == 'permission'
    assert classify(1, 'Permission denied: /arbitrary/file', managed_paths=['/work/.venv']) is None
    assert classify(1, 'npm ERR! E404 package does not exist') is None
    assert classify(1, 'Invalid pyproject.toml') is None
    assert classify(127, 'sh: 1: undeclared-test-runner: not found') is None
    assert classify(125, 'the application exited') is None
    assert classify(127, 'bash: line 1: composer: command not found')['category'] == 'runtime'
    assert classify(1, 'Disk quota exceeded')['category'] == 'storage'
    assert classify(124, '')['category'] == 'timeout'


def test_source_observation_excludes_generated_environment(tmp_path):
    from coder_source_activity import inventory
    (tmp_path / '.venv-dev/bin').mkdir(parents=True)
    (tmp_path / '.venv-dev/pyvenv.cfg').write_text('home = /usr/bin')
    (tmp_path / '.venv-dev/bin/dependency.py').write_text('not source')
    (tmp_path / 'main.py').write_text('source')
    assert set(inventory(tmp_path)) == {'main.py'}


def test_environment_binding_is_operation_specific(tmp_path):
    from coder_prepared_environment import binding_path, is_ready
    dep = tmp_path / 'private'; dep.mkdir()
    marker = binding_path(tmp_path, tmp_path / 'source', 'key'); marker.parent.mkdir()
    marker.write_text(json.dumps({'version': 2, 'dependencies': [str(dep)], 'operation_id': 'first'}))
    assert is_ready(tmp_path, tmp_path / 'source', 'key', 'first')
    assert not is_ready(tmp_path, tmp_path / 'source', 'key', 'second')


def test_ambiguous_deliverables_never_invent_success_or_spend_repair():
    from coder_engine_contract import apply_summary
    summary = {'revision_id': 'rev', 'checks_ok': True, 'checks_passed': True, 'failures': []}
    apply_summary(summary, freeze('Build a package with cli.py'))
    assert summary['checks_ok'] and not summary['checks_passed'] and not summary['failures']
    assert summary['criteria'][0]['status'] == 'unverified'
    assert summary['criteria'][0]['revision_id'] == 'rev'


def test_preparation_attempts_survive_new_operation_and_restart(tmp_path, monkeypatch):
    import time
    from .test_engine_reliability import operation
    from coder_worker_runtime import WorkerStore
    import coder_project_runtime as runtime
    monkeypatch.delenv('HEPHAESTUS_EXECUTOR', raising=False)
    store = operation(tmp_path / 'worker')
    root = tmp_path / 'source'; root.mkdir()
    folder = tmp_path / 'logs'; folder.mkdir()
    profile = {'cwd': '.', 'commands': {'setup': ['npm install']}, 'toolchains': []}
    invoked = []
    def fail(*args, **kwargs):
        invoked.append(True); args[5].write_text('No space left on device'); return 1
    monkeypatch.setattr(runtime, 'execute', fail)
    for name in ('op', 'second', 'third'):
        store = WorkerStore(store.root)
        if name != 'op':
            store.create(name, 'job', 'check', store.get('op')['payload'])
            store.update(name, started=time.time())
        rows = runtime.prepare_environment(store, name, root, profile, 'project', {}, folder)
        assert rows[-1]['environment_fault'] and not rows[-1]['passed']
    assert len(invoked) == 2
    assert rows[-1]['diagnostic']['category'] == 'preparation_limit'


def test_unknown_venv_keeps_authored_files(tmp_path, monkeypatch):
    from .test_engine_reliability import operation
    import coder_project_runtime as runtime
    monkeypatch.delenv('HEPHAESTUS_EXECUTOR', raising=False)
    root = tmp_path / 'source'; (root / '.venv').mkdir(parents=True)
    (root / '.venv/important.py').write_text('authored')
    folder = tmp_path / 'logs'; folder.mkdir()
    profile = {'cwd': '.', 'commands': {'setup': ['python3 -m venv .venv']}, 'toolchains': []}
    rows = runtime.prepare_environment(operation(tmp_path/'store'), 'op', root, profile, 'project', {}, folder)
    assert rows[-1]['diagnostic']['category'] == 'classification'
    assert (root / '.venv/important.py').read_text() == 'authored'


def test_generated_environment_links_not_exported(tmp_path):
    from coder_repository import Repository
    root = tmp_path / 'source'; root.mkdir()
    (root/'main.py').write_text('print(42)')
    env = root/'.venv-dev'; (env/'bin').mkdir(parents=True)
    (env/'pyvenv.cfg').write_text('home = /usr/bin')
    (env/'bin/python').symlink_to('/usr/bin/python3')
    repo = Repository(root, tmp_path/'index', ())
    repo.refresh()
    snapshot = repo.snapshot()
    assert list(repo.paths()) == ['main.py']
    assert repo.git('ls-tree', '-r', '--name-only', snapshot['revision']).decode().strip() == 'main.py'


def test_interrupted_preparation_cannot_restore_attempts(tmp_path, monkeypatch):
    import time
    from .test_engine_reliability import operation
    from coder_profiles import environment_key
    import coder_project_runtime as runtime
    monkeypatch.delenv('HEPHAESTUS_EXECUTOR', raising=False)
    store = operation(tmp_path/'store')
    root=tmp_path/'source';root.mkdir();folder=tmp_path/'logs';folder.mkdir()
    profile={'cwd':'.','commands':{'setup':['npm install']},'toolchains':[]}
    key=environment_key(root,profile)[0]
    ledger=store.root/'preparation-ledgers/job.json';ledger.parent.mkdir()
    ledger.write_text(json.dumps({'failures':{key:2},'seconds':10,'inflight':{'key':key,'operation_id':'op','started':time.time()-1}}))
    def unexpected(*a,**kw):raise AssertionError('Third preparation must not dispatch')
    monkeypatch.setattr(runtime,'execute',unexpected)
    rows=runtime.prepare_environment(store,'op',root,profile,'project',{},folder)
    assert rows[0]['diagnostic']['category']=='preparation_limit'


def test_scoped_file_list_ends_before_other_deliverables():
    c=freeze('Create the todo_app/ package with cli.py and storage.py, plus tests and README.md.')
    assert {r['path'] for r in c['criteria']} == {'todo_app/cli.py','todo_app/storage.py','README.md'}
    c=freeze('Include package.json, server.js, public/index.html, public/app.js, tests, and README.md.')
    assert {'package.json','server.js','public/index.html','public/app.js'} <= {r['path'] for r in c['criteria']}


@pytest.mark.parametrize('lock,manager', [(None, 'npm'), ('pnpm-lock.yaml', 'pnpm'), ('yarn.lock', 'yarn')])
def test_stdlib_node_start_is_managed_service(tmp_path, lock, manager):
    from coder_repository import Repository
    from coder_project_runtime import contract
    root = tmp_path / 'source'; root.mkdir()
    (root/'package.json').write_text(json.dumps({'scripts': {'start': 'node server.js'}}))
    (root/'server.js').write_text("require('node:http').createServer((q,r)=>r.end('ok')).listen(process.env.PORT, '127.0.0.1');")
    if lock: (root/lock).write_text('')
    repo = Repository(root, tmp_path/'index', ()); repo.refresh()
    profile = contract(repo)
    assert profile['services'][0]['command'] == f'PORT={{port}} HOST=127.0.0.1 {manager} run start'
    assert not any(c.get('phase') == 'launch' and c.get('command') for c in profile['checks'])
    (root/'server.js').write_text("console.log(process.env.PORT || '42');")
    repo.refresh()
    assert not contract(repo)['services']


def test_node_service_detection_does_not_follow_external_entrypoint(tmp_path):
    from coder_profiles import node_server_entrypoint
    source = tmp_path/'source'; source.mkdir()
    other = tmp_path/'outside.js'; other.write_text('server.listen(process.env.PORT)')
    (source/'server.js').symlink_to(other)
    assert not node_server_entrypoint(source, 'node server.js')
    assert not node_server_entrypoint(source, 'node ../outside.js')
    assert not node_server_entrypoint(source, 'node missing.js')


def test_explicit_readme_extension_is_not_substituted(tmp_path):
    (tmp_path/'README.txt').write_text('Wrong requested output')
    assert not checks(tmp_path, '', 'revision', freeze('Include README.md.'))[0]['passed']


def test_ui_driver_uses_controller_cache_without_changing_app_env(tmp_path, monkeypatch):
    import os, sys
    from types import SimpleNamespace
    from coder_ui_session import UISession
    driver = SimpleNamespace(chromium=SimpleNamespace(executable_path='/missing-controller-browser'))
    observed = []
    def start():
        observed.append(os.environ['PLAYWRIGHT_BROWSERS_PATH'])
        return driver
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=lambda: SimpleNamespace(start=start)))
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', '/project/browsers')
    monkeypatch.setenv('DAEDALUS_CONTROLLER_BROWSERS_PATH', '/controller/browsers')
    session = object.__new__(UISession)
    session.kind = None; session.behavior_version = 1; session.processes = []
    session.env = dict(os.environ); session.log_tail = lambda: ''; session.close_application = lambda _: None
    with pytest.raises(ValueError) as failure:
        session.perform({'action': 'launch', 'kind': 'web', 'url': 'file:///index.html'})
    assert observed == ['/controller/browsers']
    assert os.environ['PLAYWRIGHT_BROWSERS_PATH'] == session.env['PLAYWRIGHT_BROWSERS_PATH'] == '/project/browsers'
    assert failure.value.details['environment_fault']
    assert failure.value.details['category'] == 'environment'


def test_ui_controller_runtime_fault_does_not_become_application_failure(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    import coder_ui_checks as ui
    @contextmanager
    def session(*args):
        def action(request):
            raise ui.UIObservationFailure({'error': 'Browser unavailable', 'category': 'environment', 'environment_fault': True})
        yield action
    monkeypatch.setattr(ui, 'session', session)
    store = SimpleNamespace(get=lambda _: {'payload': {'revision_id': 'rev', 'engine_behavior_version': 1}}, event=lambda *a, **k: None)
    rows = ui.run(store, 'op', None, [{'kind': 'web', 'file_path': 'index.html', 'steps': [
        {'action': 'click', 'name': 'Go'}, {'action': 'text', 'selector': '#result', 'equals': 'Ready'}]}])
    assert rows[0]['environment_fault'] and not rows[0]['passed']
    assert rows[0]['classification'] == 'environment' and rows[0]['revision_id'] == 'rev'


def test_live_usage_totals_survive_stage_transition_and_replay(tmp_path, monkeypatch):
    import asyncio
    from .test_daedalus_jobs import setup7
    from db import coder_jobs as store
    setup7(monkeypatch, tmp_path)
    async def scenario():
        job = await store.create('conversation', 'task', 'build_from_prompt', '', 'local', 'usage-event', policy_version=7)
        await store.save(job['id'], worker_operation='op', operation_accounted=False, calls_used=2, seconds_used=5)
        operation = {'status': 'succeeded', 'started': 10, 'ended': 13, 'calls': 4}
        await store.account_operation(job['id'], 'op', 'code', operation)
        await store.account_operation(job['id'], 'op', 'code', operation)
        events = [event for event in await store.events(job['id'], 0) if event['type'] == 'operation_accounted']
        assert len(events) == 1
        data = events[0]['data']
        assert data['calls_used'] == 6 and data['seconds_used'] == 8
        assert data['operation_calls'] == data['operation_seconds'] == 0
    asyncio.run(scenario())


def test_inherited_ui_contract_retains_preparation_diagnostic(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    import coder_ui_checks as ui
    from coder_ui_contract import run
    calls = []
    @contextmanager
    def session(*args):
        calls.append(True)
        raise ui.UIObservationFailure({'error': 'Environment unavailable', 'environment_fault': True})
        yield  # contextmanager interface
    monkeypatch.setattr(ui, 'session', session)
    monkeypatch.setattr(ui, 'bind_project_entrypoint', lambda *args: None)
    store = SimpleNamespace(get=lambda _: {'payload': {'revision_id': 'rev'}}, event=lambda *a, **k: None)
    rows = run(store, 'op', None, {'criteria': [{'id': 'criterion', 'supported': True}]}, [{'flow': {}}, {'flow': {}}])
    assert len(calls) == 1
    assert rows[0]['environment_fault'] and rows[0]['status'] == 'unverified'
    assert rows[0]['revision_id'] == 'rev'
