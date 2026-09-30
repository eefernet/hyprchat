"""Policy-7 regressions exercise boundaries and real processes, without inference."""
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
from coder_policy7 import Experiment, acceptance, audit_checks, make_brief
from coder_patch_runtime import inference_bridge, select_files, readonly_command
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


def test_outcome_coverage_is_bound_to_exact_revision_and_check():
    outcomes = [{'id': 'o1', 'text': 'API works'}, {'id': 'o2', 'text': 'README and tests'}]
    verdict = {'scope_complete': True, 'outcomes': [{'id': o['id'], 'status': 'passed'} for o in outcomes]}
    check = {'id': 'x', 'execution_id': 'run:x', 'revision_id': 'current', 'passed': True,
             'coverage_observed': True, 'origin': 'independent', 'outcomes': ['o1'],
             'evidence_types': ['behavior'], 'execution_succeeded': True, 'source_bindings': [{'path': 'app.py', 'sha256': 'a'*64}]}
    assert not acceptance(outcomes, [check], verdict, 'current')['accepted']
    assert not acceptance(outcomes[:1], [check], verdict, 'different')['accepted']
    summary = acceptance(outcomes[:1], [check], verdict, 'current')
    assert not summary['accepted']  # generated audit cannot supply trusted behavior coverage
    assert summary['outcomes'][0]['missing_evidence'] == ['behavior']


def test_draft_obligations_cannot_disappear_from_new_brief():
    answer = {'outcomes': ['New feature'], 'batches': [{'task': 'Add it'}]}
    with pytest.raises(ValueError, match='inheritance disposition'):
        make_brief(answer, 'New feature', ['Original missing UI'])
    brief = make_brief({**answer, 'inheritance': [{'parent_id': 'o1', 'action': 'retain'}]},
                       'New feature', ['Original missing UI'])
    assert [o['text'] for o in brief['outcomes']] == ['Original missing UI', 'New feature']


def test_context_selection_keeps_complete_files(tmp_path):
    (tmp_path / 'small.py').write_text('VALUE=3')
    (tmp_path / 'huge.py').write_text('VALUE=3\n' * 10000)
    files, omitted = select_files(tmp_path, 'huge small', 100)
    assert files == ['small.py'] and omitted == ['huge.py']


def test_bridge_rejects_model_change_and_accounts_internal_retries(tmp_path):
    _, store = fixture(tmp_path, {})
    def chat(store, op, role, messages, **kwargs):
        _boundary(store, op, model_call=True)
        return 'patch'
    with inference_bridge(store, 'check', 'builder', chat) as (url, errors):
        body = {'model': 'other', 'messages': [], 'stream': False}
        assert requests.post(url + '/api/chat', json=body).status_code == 400
        body['model'] = 'local'
        assert requests.post(url + '/api/chat', json=body).json()['message']['content'] == 'patch'
        assert requests.post(url + '/api/chat', json=body).status_code == 400
    assert store.get('check')['calls'] == 1
    assert any('allowance' in error['message'] for error in errors)


@pytest.mark.skipif(not shutil.which('bwrap'), reason='Codebox sandbox test')
def test_readonly_audit_cannot_mutate_project(tmp_path):
    import subprocess
    source = tmp_path / 'subject.py'; source.write_text('VALUE=1')
    audit = tmp_path / 'audit'; audit.mkdir()
    command = [sys.executable, '-c', f'from pathlib import Path; Path({str(source)!r}).write_text("changed")']
    result = subprocess.run(readonly_command(command, [audit]), capture_output=True)
    assert result.returncode != 0
    assert source.read_text() == 'VALUE=1'


def test_repeated_narration_returns_candidate_and_counters_survive_restart(tmp_path):
    def chat(*args, **kwargs):
        return json.dumps({'outcomes': ['Fix ledger'], 'batches': [{'task': 'Fix ledger'}]})
    calls = []
    def editor(*args, **kwargs):
        calls.append('Now let me inspect the README')
        return {'changed': []}
    experiment = Experiment(tmp_path / 'job', 'Fix ledger', model='local', ollama_url='http://unused', editor=editor, chat=chat)
    result = experiment.run()
    assert result['state'] == 'candidate' and len(calls) == 2 and result['recovery_used']
    restored = Experiment(tmp_path / 'job', editor=editor, chat=chat)
    assert restored.job['recovery_used'] and restored.job['repair_round'] == 0
    assert Path(result['artifact']['path']).is_file()


def test_zero_tests_cannot_supply_coverage(tmp_path):
    repo, store = fixture(tmp_path, {'test_empty.py': 'print("passed")'})
    profile = {'profiles': [], 'services': [], 'checks': [{'id': 'test', 'command': 'python3 test_empty.py', 'phase': 'test', 'is_test': True}]}
    result = run_revision(store, 'check', repo, profile, project_id='project')
    assert not result['passed']
    assert result['checks'][0]['failure_kind'] == 'missing_coverage'


@pytest.mark.skipif(not shutil.which('bwrap'), reason='Codebox sandbox test')
def test_sentinel_import_and_list_assertion_run_against_real_source(tmp_path):
    repo, store = fixture(tmp_path, {'subject.py': 'MISSING=object()\ndef items(): return [1,2,3]\n'})
    audit = tmp_path / 'audit'; audit.mkdir()
    (audit / 'test_real.py').write_text('from subject import MISSING,items\nassert MISSING is MISSING\nassert items()==[1,2,3]\n')
    profile = {'profiles': [], 'services': [], 'checks': []}
    checks = [{'id': 'audit', 'command': 'python3 {audit}/test_real.py', 'origin': 'independent', 'is_test': True}]
    result = run_revision(store, 'check', repo, profile, project_id='project', audit_source=audit, checks=checks)
    assert result['passed'], result
    assert any(b['path'] == 'subject.py' for b in result['checks'][0]['source_bindings'])


def test_audit_metadata_cannot_embed_a_replacement_application(tmp_path):
    (tmp_path / 'audit.json').write_text(json.dumps({'checks': [{'command': 'python3 subject.py', 'outcomes': ['o1']}]}))
    with pytest.raises(ValueError, match='under'):
        audit_checks(tmp_path, [{'id': 'o1'}])


@pytest.mark.skipif(not Path('/opt/openhands-worker/aider-venv/bin/aider').exists(), reason='Installed Codebox Aider contract')
def test_real_pinned_cli_applies_patch_through_counted_text_bridge(tmp_path):
    from coder_patch_runtime import edit
    repo, store = fixture(tmp_path, {'subject.py': 'VALUE = 1\n'})
    def response(store, op, role, messages, **kwargs):
        _boundary(store, op, model_call=True)
        assert any('VALUE = 1' in str(m) for m in messages)
        return 'subject.py\n```python\n<<<<<<< SEARCH\nVALUE = 1\n=======\nVALUE = 2\n>>>>>>> REPLACE\n```'
    result = edit(store, 'check', repo.root, 'Set VALUE to 2', chat=response)
    assert repo.root.joinpath('subject.py').read_text() == 'VALUE = 2\n', result
    assert result['changed'] == ['subject.py']
    assert store.get('check')['calls'] == 1


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


def test_explicit_draft_fork_uses_archive_not_mutable_workspace(tmp_path):
    parent = Experiment(tmp_path / 'parent', 'Original requirement', model='local', ollama_url='http://unused', files={'app.py': 'VALUE=1'})
    parent.step()
    parent.save(state='candidate', brief={'outcomes': [{'id': 'o1', 'text': 'Original requirement'}]})
    parent.package()
    (parent.repo.root / 'app.py').write_text('VALUE=999')
    artifact = parent.job['artifact']
    child = Experiment.fork(parent, tmp_path / 'child', 'New feature', revision_id=artifact['revision_id'], artifact_sha256=artifact['sha256'])
    assert (child.repo.root / 'app.py').read_text() == 'VALUE=1'
    assert child.job['inherited'] == ['Original requirement']
    assert child.job['source_revision'] != child.job['expected_accepted_revision']
    with pytest.raises(ValueError, match='Exact'):
        Experiment.fork(parent, tmp_path / 'wrong', 'New feature', revision_id='stale', artifact_sha256=artifact['sha256'])


def test_stop_acknowledgement_resumes_with_persisted_repair_limits(tmp_path):
    experiment = Experiment(tmp_path / 'job', 'Fix app', model='local', ollama_url='http://unused')
    experiment.step()
    experiment.save(state='editing', repair_round=2, audit_corrections=1, recovery_used=True, operation='cancelled-op')
    experiment.store.create('cancelled-op', experiment.job['id'], 'code', {'request_key': 'x'})
    experiment.store.update('cancelled-op', status='cancelled', started=time.time(), ended=time.time(), calls=3)
    restarted = Experiment(tmp_path / 'job')
    restarted.resume()
    assert restarted.job['state'] == 'editing' and not restarted.job['operation']
    assert restarted.job['repair_round'] == 2 and restarted.job['audit_corrections'] == 1
    assert restarted.job['recovery_used'] and restarted.job['calls'] == 3


def test_saved_working_patch_with_unchanged_later_batch_reaches_checks(tmp_path):
    """Rehearsal: Python repair passed independently but no-change stopped review."""
    experiment = Experiment(tmp_path / 'job', 'Fix ledger and document it', model='local', ollama_url='http://unused',
        files={'ledger.py': 'VALUE=1'}, editor=lambda *a, **kw: {'changed': []})
    experiment.step()
    experiment.save(state='editing', brief={'outcomes': [{'id': 'o1', 'text': 'Fix ledger'}],
        'batches': [{'task': 'Fix ledger'}, {'task': 'Document it'}]}, batch=1,
        last_patch={'changed': ['ledger.py', 'README.md']})
    experiment.step()
    assert experiment.job['state'] == 'checking'
    assert not experiment.job['recovery_used'] and experiment.job['repair_round'] == 0


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


def test_candidate_does_not_reuse_verification_of_an_older_revision(tmp_path):
    experiment = Experiment(tmp_path / 'job', 'Make app work', model='local', ollama_url='http://unused', files={'app.py': 'VALUE=1'})
    experiment.step()
    experiment.save(state='candidate', brief={'outcomes': [{'id': 'o1', 'text': 'Works'}]},
        verification_summary={'revision_id': 'old', 'accepted': False, 'outcomes': [{'id': 'o1', 'status': 'passed'}]})
    experiment.package()
    assert experiment.job['verification_summary']['revision_id'] == experiment.job['revision_id']
    assert experiment.job['verification_summary']['outcomes'][0]['status'] == 'unverified'
