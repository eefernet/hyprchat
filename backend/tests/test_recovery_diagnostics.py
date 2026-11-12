"""Recorded production failures: exact test names, gateway faults, project browsers."""
import json
import socket
import time
from pathlib import Path
import pytest
from coder_engine_ops import summarize
from coder_prepared_environment import classify
from coder_required_tests import regressions
from coder_sandbox import network_failure

OLD = '.::Pocket Tasks > Clear button > should clear the Result display'
NEW = '.::Pocket Tasks > Clear button > should set Result to \'Ready\' when clearing'


def test_renamed_test_is_not_aliased_and_exact_identity_survives_restart(tmp_path):
    from coder_worker_runtime import WorkerStore
    lost = regressions([OLD], [{'is_test': True, 'cwd': '.', 'test_ids': {'ids': {NEW.split('::', 1)[1]: 'passed'}}}])
    assert lost == [{'id': OLD, 'status': 'missing'}]
    check = {'id': 'baseline-regressions', 'passed': False, 'revision_id': 'candidate',
             'baseline_revision': 'baseline', 'lost_tests': lost, 'reason': 'Previously passing tests regressed'}
    store = WorkerStore(tmp_path)
    store.create('op', 'job', 'check', {'repair_round': 2})
    store.update('op', result={'checks': [check], 'summary': summarize([check], 'candidate')})
    saved = WorkerStore(tmp_path).get('op')
    failure = saved['result']['summary']['failures'][0]
    assert failure['lost_tests'] == [{'id': OLD, 'package': '.', 'status': 'missing'}]
    assert failure['baseline_revision'] == 'baseline' and failure['revision_id'] == 'candidate'
    assert failure['omitted_test_count'] == 0
    assert 'retaining the original test name' in failure['reason']
    assert saved['payload']['repair_round'] == 2


@pytest.mark.parametrize('status', ['failed', 'skipped', 'missing'])
def test_regression_gate_keeps_deleted_skipped_and_unrelated_tests(status):
    tests = {} if status == 'missing' else {OLD.split('::', 1)[1]: status}
    rows = [{'is_test': True, 'passed': status == 'missing', 'cwd': '.', 'test_ids': {'ids': tests}}]
    assert regressions([OLD], rows) == [{'id': OLD, 'status': status}]


def test_bounded_feedback_prioritizes_exact_ids_and_discloses_omissions():
    lost = [{'id': '.::test_' + str(i) + 'x' * 100, 'status': 'missing'} for i in range(4000)]
    check = {'id': 'baseline-regressions', 'passed': False, 'revision_id': 'r', 'lost_tests': lost}
    report = summarize([{'id': 'other', 'passed': False, 'revision_id': 'r'}, check], 'r')
    packet = report['failures'][0]
    assert packet['id'] == 'baseline-regressions'
    assert packet['lost_tests'][0]['id'] == lost[0]['id']
    assert packet['omitted_test_count'] + len(packet['lost_tests']) == 4000
    assert len(check['lost_tests']) == 4000


@pytest.mark.parametrize('error,category,retry', [(socket.gaierror('DNS unavailable'), 'dns', True),
    (TimeoutError('connect timed out'), 'timeout', True), (ConnectionRefusedError('refused'), 'connection', True),
    (ValueError('Private destination'), 'network_policy', False)])
def test_gateway_classification_is_trusted_and_phase_bound(error, category, retry):
    trusted = network_failure(error, 'registry.npmjs.org', 443)
    assert trusted['category'] == category
    diagnostic = classify(1, '502 Sandbox network destination unavailable', network_diagnostics=[trusted])
    assert diagnostic['retryable'] is retry
    assert diagnostic['category'] == ('network' if retry else 'network_policy')
    assert classify(1, 'Application asserted expected 502, got 200', phase='test', network_diagnostics=[trusted]) is None
    assert classify(1, '502 Sandbox network destination unavailable') is None
    assert classify(1, 'npm ERR E404 package missing', network_diagnostics=[]) is None


@pytest.mark.parametrize('persistent,denied,expected', [(False, False, 2), (True, False, 2), (True, True, 1)])
def test_shared_preparation_retry_uses_durable_ledger(tmp_path, monkeypatch, persistent, denied, expected):
    from .test_engine_reliability import operation
    from coder_worker_runtime import WorkerStore
    import coder_project_runtime as runtime
    monkeypatch.delenv('HEPHAESTUS_EXECUTOR', raising=False)
    store = operation(tmp_path/'store'); root = tmp_path/'source'; root.mkdir(); logs=tmp_path/'logs';logs.mkdir()
    profile = {'cwd': '.', 'commands': {'setup': ['npm install']}, 'toolchains': []}
    calls = []
    def execute(*args, **kwargs):
        calls.append(True)
        fail = persistent or len(calls) == 1
        args[5].write_text('502 Sandbox network destination unavailable' if fail else 'installed')
        if fail:
            kwargs['network_diagnostics'].append(network_failure(ValueError('Private destination') if denied else TimeoutError('connect'), 'registry.npmjs.org', 443))
        return int(fail)
    monkeypatch.setattr(runtime, 'execute', execute)
    rows = runtime.prepare_environment(store, 'op', root, profile, 'project', {}, logs)
    assert len(calls) == expected
    assert rows[-1]['passed'] is (not persistent)
    assert store.get('op')['calls'] == 0
    if persistent and not denied:
        store = WorkerStore(store.root)
        store.create('again', 'job', 'check', store.get('op')['payload']);store.update('again', started=time.time())
        rows = runtime.prepare_environment(store, 'again', root, profile, 'project', {}, logs)
        assert rows[0]['diagnostic']['category'] == 'preparation_limit'
        assert len(calls) == 2


def test_failed_preparation_exposes_no_usable_paths(tmp_path, monkeypatch):
    from .test_engine_reliability import operation
    from coder_repository import Repository
    from coder_engine_environment import refresh
    import coder_project_runtime as runtime
    root=tmp_path/'source';root.mkdir();(root/'package.json').write_text('{"dependencies":{"missing-package":"1"}}')
    repo=Repository(root,tmp_path/'index',());repo.refresh()
    monkeypatch.setattr(runtime, 'prepare_environment', lambda *a: [{'passed': False, 'log_tail': 'Invalid package'}])
    result=refresh(operation(tmp_path/'store'), 'op', repo, ['package.json'], {'PLAYWRIGHT_BROWSERS_PATH':'/incomplete'}, tmp_path/'logs')
    assert not result['passed'] and 'browser_path' not in result and 'python' not in result


def test_successful_browser_preparation_explains_readonly_ready_environment(tmp_path, monkeypatch):
    from .test_engine_reliability import operation
    from coder_repository import Repository
    import coder_engine_environment as environment
    import coder_project_runtime as runtime
    root=tmp_path/'source';root.mkdir();(root/'package.json').write_text('{"devDependencies":{"playwright":"1.64.1"}}')
    repo=Repository(root,tmp_path/'index',());repo.refresh()
    monkeypatch.setattr(runtime, 'prepare_environment', lambda *a: [{'passed': True, 'browser_identity': {'version': '1.64.1'}}])
    result=environment.refresh(operation(tmp_path/'store'), 'op', repo, ['package.json'], {'PLAYWRIGHT_BROWSERS_PATH':'/prepared'}, tmp_path/'logs')
    assert result['passed'] and result['browser_prepared'] and result['browser_path']=='/prepared'
    assert 'read-only' in result['guidance'] and 'Run the tests now' in result['guidance']
    assert 'do not run playwright install' in result['guidance']


def test_browser_readiness_requires_matching_package_and_live_executable(tmp_path):
    from coder_engine_environment import browser_specs, browser_marker, browsers_ready
    package=tmp_path/'node_modules/playwright-core';package.mkdir(parents=True)
    (package/'package.json').write_text('{"version":"1.64.1"}')
    (package/'browsers.json').write_text('{"browsers":[{"name":"chromium","revision":"1248"}]}')
    cache=tmp_path/'browsers';cache.mkdir()
    identity=browser_specs(tmp_path)[0][2]
    marker=browser_marker(cache,identity);marker.write_text(json.dumps(identity))
    assert not browsers_ready(tmp_path,cache)
    binary=cache/'chromium-1248/chrome';binary.parent.mkdir();binary.write_text('binary');binary.chmod(0o755)
    stat=binary.stat()
    marker.write_text(json.dumps({'version':1,'identity':identity,'executables':[{'path':'chromium-1248/chrome','stat':[stat.st_size,stat.st_mtime_ns]}]}))
    assert browsers_ready(tmp_path,cache)
    (package/'browsers.json').write_text('{"browsers":[{"name":"chromium","revision":"1234"}]}')
    assert not browsers_ready(tmp_path,cache)
    (package/'browsers.json').write_text('{"browsers":[{"name":"chromium","revision":"1248"}]}')
    binary.unlink()
    assert not browsers_ready(tmp_path,cache)


def test_controller_browser_dependency_has_actionable_project_feedback():
    failure=summarize([{'id':'node:test','passed':False,'revision_id':'r','log_tail':
        'Executable missing: /srv/hephaestus/control/browsers/1.62.0/chromium-1234/chrome'}], 'r')['failures'][0]
    assert 'without executablePath' in failure['reason']
    assert not failure.get('environment_fault')


def test_browser_generation_mount_and_stale_replacement_preserve_old_files(tmp_path, monkeypatch):
    import coder_project_runtime as runtime
    from coder_engine_environment import prepare_browsers, browsers_ready, browser_specs, browser_marker
    from coder_mount_policy import mounts
    source=tmp_path/'project';package=source/'node_modules/playwright-core';package.mkdir(parents=True)
    (package/'package.json').write_text('{"version":"1.64.1"}')
    (package/'browsers.json').write_text('{"browsers":[{"name":"chromium","revision":"1248"}]}')
    cache=tmp_path/'environment/browsers';cache.mkdir(parents=True);audit=tmp_path/'audit';audit.mkdir()
    env={'PLAYWRIGHT_BROWSERS_PATH':str(cache),'DAEDALUS_ENVIRONMENT_ROOT':str(cache.parent),'DAEDALUS_AUDIT_DIR':str(audit)}
    launches=[]
    def launch(store, op, command, cwd, environment, log, writable, **kwargs):
        reads=runtime.sandbox_paths(source,environment)
        mounts(reads,writable)  # the former writable-parent/read-only-child conflict
        browser=Path(environment['PLAYWRIGHT_BROWSERS_PATH'])/'chromium-1248/chrome'
        if not browser.exists():
            browser.parent.mkdir();browser.write_text('complete browser');browser.chmod(0o755)
        launches.append(browser.resolve());log.write_text('actual validation stand-in');return 0
    monkeypatch.setattr(runtime,'execute',launch)
    rows=prepare_browsers(None,'op',source,source,cache,env,audit)
    assert rows[0]['passed'] and browsers_ready(source,cache)
    first=(cache/'chromium-1248/chrome').resolve()
    first.write_text('corrupt executable')
    assert not browsers_ready(source,cache)
    rows=prepare_browsers(None,'op',source,source,cache,env,audit)
    assert rows[0]['passed'] and browsers_ready(source,cache)
    assert first.read_text()=='corrupt executable'  # an existing reader's file was not changed/deleted
    assert (cache/'chromium-1248/chrome').read_text()=='complete browser'
    assert (cache/'chromium-1248/chrome').resolve()!=first


def test_exact_observed_visible_button_caption_keeps_ambiguity_strict():
    from coder_ui_contract import _matches, Unverified
    clear={'selector':'#clear','tag':'button','role':'button','activatable':True,'name':'Clear input','text':'Clear'}
    assert _matches([clear],{'name':'Clear'},activation=True)==[clear]
    with pytest.raises(Unverified,match='found 2'):
        _matches([clear,{**clear,'selector':'#other','name':'Clear results'}],{'name':'Clear'},activation=True)
    with pytest.raises(Unverified,match='found 0'):
        _matches([{**clear,'text':'Clear all'}],{'name':'Clear'},activation=True)
    with pytest.raises(Unverified,match='found 0'):
        _matches([{**clear,'activatable':False}],{'name':'Clear'},activation=True)
    with pytest.raises(Unverified,match='found 0'):
        _matches([{**clear,'showing':False}],{'name':'Clear'},activation=True)


def test_shared_browser_binding_keeps_stable_path_and_validates_targets(tmp_path, monkeypatch):
    from coder_engine_environment import bind_shared_browser, browser_marker, valid_browser_marker
    from hephaestus_inventory import register
    monkeypatch.setenv('HEPHAESTUS_ROOT', str(tmp_path))
    monkeypatch.setenv('HEPHAESTUS_SHARED_CACHE', '1')
    shared = tmp_path / 'caches/shared/browsers' / ('a' * 64)
    binary = shared / 'chromium-1248/chrome'
    binary.parent.mkdir(parents=True); binary.write_text('binary'); binary.chmod(0o755)
    identity = {'version': '1.64.1'}
    stat = binary.stat()
    browser_marker(shared, identity).write_text(json.dumps({'version': 1, 'identity': identity,
        'executables': [{'path': 'chromium-1248/chrome', 'stat': [stat.st_size, stat.st_mtime_ns]}]}))
    register(tmp_path, shared, 'shared_cache')
    cache = tmp_path / 'state/project/browsers'
    cache.mkdir(parents=True)
    bind_shared_browser(cache, shared, identity)
    assert (cache / 'chromium-1248/chrome').read_text() == 'binary'
    assert valid_browser_marker(cache, identity)
    bind_shared_browser(cache, shared, identity)
    assert valid_browser_marker(cache, identity)
    (cache / 'chromium-1248').unlink()
    elsewhere = tmp_path / 'other'; elsewhere.mkdir()
    (elsewhere / 'chrome').write_text('binary'); (elsewhere / 'chrome').chmod(0o755)
    (cache / 'chromium-1248').symlink_to(elsewhere)
    assert not valid_browser_marker(cache, identity)
    binary.unlink()
    with pytest.raises(ValueError, match='validated'):
        bind_shared_browser(cache, shared, identity)


@pytest.mark.parametrize('source', [
    "const {chromium}=require('playwright'); const shared='/srv/hephaestus/caches/shared/browsers';",
    "const config={launchOptions:{executablePath:'/srv/hephaestus/control/browsers/chrome'}};",
    "browser = chromium.launch(executable_path='/root/.cache/ms-playwright/chrome')",
])
def test_worker_browser_paths_fail_delivery_and_repair_feedback(tmp_path, source):
    from coder_engine_delivery import portability_checks
    from coder_repository import Repository
    root=tmp_path/'source';root.mkdir();(root/'playwright.config.js').write_text(source)
    repo=Repository(root,tmp_path/'index',());repo.refresh()
    checks=portability_checks(repo,'candidate')
    summary=summarize(checks,'candidate')
    assert not summary['checks_ok'] and summary['check_counts']['failed']==1
    failure=summary['failures'][0]
    assert failure['classification']=='nonportable_browser'
    assert failure['diagnostic']['locations']==[{'path':'playwright.config.js','line':1}]
    assert 'Preserve test names and assertions' in failure['reason']


def test_portable_browser_resolution_is_not_an_extra_passing_check(tmp_path):
    from coder_engine_delivery import portability_checks
    from coder_repository import Repository
    root=tmp_path/'source';root.mkdir()
    (root/'test.js').write_text("const {chromium}=require('playwright'); chromium.launch();")
    (root/'README.md').write_text('Avoid /srv/hephaestus/control/browsers/chrome paths for Playwright.')
    repo=Repository(root,tmp_path/'index',());repo.refresh()
    assert portability_checks(repo,'r')==[]
