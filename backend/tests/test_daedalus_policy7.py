"""Shared verification regressions exercise boundaries and real processes, without inference."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import time

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coder_project_runtime import contract, failure_class, run_revision
from coder_repository import Repository
from coder_worker_runtime import WorkerStore, _boundary
from context_policy import DEFAULTS


def fixture(tmp_path, files):
    root = tmp_path / 'source'; root.mkdir()
    for name, text in files.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
    repo = Repository(root, tmp_path / 'repo', DEFAULTS['daedalus_exclude_dirs'])
    revision = repo.snapshot()['revision']
    store = WorkerStore(tmp_path / 'worker')
    store.create('check', 'job', 'check', {'settings': dict(DEFAULTS), 'revision_id': revision,
        'calls_remaining': 1, 'seconds_remaining': 60, 'model': 'local', 'policy_version': 7})
    store.update('check', started=time.time())
    return repo, store


def test_profiles_order_builds_before_launch_and_validate_dependencies(tmp_path):
    repo, _ = fixture(tmp_path, {'package.json': '{"scripts":{"build":"true"}}',
        'frontend/package.json': '{"scripts":{"build":"true"}}',
        '.daedalus-run.json': json.dumps({'dependencies': {'.': ['frontend']}, 'services': [
            {'id': 'app', 'command': 'python3 -m http.server {port} --bind 127.0.0.1'}]})})
    profile = contract(repo)
    assert [p['cwd'] for p in profile['profiles']] == ['frontend', '.']
    assert all(c.get('phase') != 'launch' for c in profile['checks'])
    (repo.root / '.daedalus-run.json').write_text(json.dumps({'dependencies': {'.': ['frontend'], 'frontend': ['.']}}))
    with pytest.raises(ValueError, match='Cyclic'):
        contract(repo)


def test_static_tooling_and_user_commands_survive(tmp_path):
    repo, _ = fixture(tmp_path, {'index.html': 'Hello', 'package.json': '{"scripts":{"test":"node --test"}}', 'check.py': 'assert True'})
    result = contract(repo, {'packages': {'.': {'test': 'python3 check.py'}}})
    assert result['services'][0]['command'].startswith('python3 -m http.server')
    assert next(c for c in result['checks'] if c['phase'] == 'test')['command'] == 'python3 check.py'


def test_audit_path_errors_never_become_application_repairs():
    row = {'passed': False, 'origin': 'independent', 'log_tail': "python: can't open file '/audit/ledger.py': No such file or directory"}
    assert failure_class(row) == 'audit_defect'
    assert failure_class({**row, 'log_tail': 'AssertionError: 0.2 != 0.3'}) == 'unresolved'
    assert failure_class({**row, 'log_tail': "OSError: [Errno 30] Read-only file system: 'fixture.txt'"}) == 'audit_defect'


def test_zero_tests_cannot_supply_coverage(tmp_path):
    repo, store = fixture(tmp_path, {'test_empty.py': 'print("passed")'})
    profile = {'profiles': [], 'services': [], 'checks': [{'id': 'test', 'command': 'python3 test_empty.py', 'phase': 'test', 'is_test': True}]}
    result = run_revision(store, 'check', repo, profile, project_id='project')
    assert not result['passed']
    assert result['checks'][0]['failure_kind'] == 'missing_coverage'


@pytest.mark.skipif(not (shutil.which('bwrap') or os.environ.get('HEPHAESTUS_EXECUTOR')=='docker'), reason='Linux isolated execution required')
def test_sentinel_import_and_list_assertion_run_against_real_source(tmp_path):
    repo, store = fixture(tmp_path, {'subject.py': 'MISSING=object()\ndef items(): return [1,2,3]\n'})
    audit = tmp_path / 'audit'; audit.mkdir()
    (audit / 'test_real.py').write_text('from subject import MISSING,items\nassert MISSING is MISSING\nassert items()==[1,2,3]\n')
    profile = {'profiles': [], 'services': [], 'checks': []}
    checks = [{'id': 'audit', 'command': 'python3 {audit}/test_real.py', 'origin': 'independent', 'is_test': True}]
    result = run_revision(store, 'check', repo, profile, project_id='project', audit_source=audit, checks=checks)
    assert result['passed'], result
    assert any(b['path'] == 'subject.py' for b in result['checks'][0]['source_bindings'])


def test_shared_dependency_cache_reuses_project_across_jobs(tmp_path):
    from coder_project_runtime import prepare_environment
    repo, store = fixture(tmp_path, {})
    profile = {'cwd': '.', 'toolchains': [], 'commands': {'setup': ['mkdir -p node_modules']}}
    logs = tmp_path / 'logs'; logs.mkdir()
    first = prepare_environment(store, 'check', repo.root, profile, 'same-project', dict(os.environ), logs)
    other = tmp_path / 'second'; other.mkdir()
    second = prepare_environment(store, 'check', other, profile, 'same-project', dict(os.environ), logs)
    assert first[0]['passed'] and not first[0]['environment_reused']
    assert second[0]['passed'] and second[0]['environment_reused']
    assert (other / 'node_modules').is_symlink()


def test_cancel_stops_command_process_group(tmp_path):
    import threading
    from coder_project_runtime import execute
    repo, store = fixture(tmp_path, {})
    timer = threading.Timer(.3, lambda: store.update('check', cancel_requested=1))
    timer.start()
    try:
        with pytest.raises(InterruptedError):
            execute(store, 'check', 'sleep 30', repo.root, dict(os.environ), tmp_path / 'process.log')
    finally:
        timer.cancel()
    assert store.get('check')['cancel_requested']


def test_mutation_of_extensionless_source_is_rejected(tmp_path):
    repo, store = fixture(tmp_path, {'bin/tool': 'original'})
    profile = {'profiles': [], 'services': [], 'checks': [{'id': 'malicious', 'command': 'echo changed > bin/tool', 'phase': 'build'}]}
    result = run_revision(store, 'check', repo, profile, project_id='project')
    assert not result['passed']
    assert result['checks'][-1]['failure_kind'] == 'source_mutation'
    assert (repo.root / 'bin/tool').read_text() == 'original'


def test_cli_launch_is_a_bounded_command_not_a_server(tmp_path):
    repo, store = fixture(tmp_path, {'hello.py': 'print("hello")'})
    profile = contract(repo, {'packages': {'.': {'launch': 'python3 hello.py'}}})
    assert profile['services'] == []
    launch = [c for c in profile['checks'] if c.get('phase') == 'launch']
    assert launch[0]['command'] == 'python3 hello.py'
    result = run_revision(store, 'check', repo, {'profiles': [], 'checks': launch, 'services': []}, project_id='project')
    assert result['checks'][0]['execution_succeeded']


def test_setup_never_runs_under_assertion_tracing(tmp_path, monkeypatch):
    import coder_project_runtime as runtime
    repo, store = fixture(tmp_path, {})
    observed = []
    def execute(store, op, command, cwd, env, log, **kwargs):
        observed.append(env)
        log.write_text('setup completed')
        return 0
    monkeypatch.setattr(runtime, 'execute', execute)
    logs = tmp_path / 'logs'; logs.mkdir()
    runtime.prepare_environment(store, 'check', repo.root,
        {'cwd': '.', 'toolchains': [], 'commands': {'setup': ['true']}}, 'project',
        {'PATH': os.environ['PATH'], 'PYTHONPATH': '/instrumentation', 'NODE_V8_COVERAGE': '/coverage',
         'DAEDALUS_PROVENANCE': '/bindings'}, logs)
    assert observed and not ({'PYTHONPATH', 'NODE_V8_COVERAGE', 'DAEDALUS_PROVENANCE'} & observed[0].keys())
