import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coder_policy7_evidence import acceptance, decide
from coder_criteria import deliverables
from coder_project_runtime import failure_class
from context_policy import coding_allocation


def test_generated_audits_cannot_accept_unproved_tie_break():
    outcomes = [{'id': 'o1', 'text': 'Count words and break equal counts alphabetically', 'evidence_types': ['behavior']}]
    checks = [{'id': 'audit', 'origin': 'independent', 'passed': True, 'revision_id': 'r', 'execution_id': 'e',
               'evidence_types': ['behavior'], 'outcomes': ['o1'], 'coverage_observed': True,
               'execution_succeeded': True, 'source_bindings': [{'path': 'app.py'}]}]
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed'}]}
    summary = acceptance(outcomes, checks, verdict, 'r')
    assert not summary['accepted']
    assert summary['evidence_version'] == 3
    assert len(summary['criteria']) == 2
    assert all(c['status'] == 'unverified' for c in summary['criteria'])
    decision = decide({'checks': checks}, summary, verdict)
    assert decision['action'] == 'candidate'
    assert 'trusted' in decision['reason']


@pytest.mark.parametrize('row,expected', [
    ({'origin': 'project', 'exit_code': 1, 'failure_kind': 'missing_coverage'}, 'application_defect'),
    ({'origin': 'independent', 'exit_code': 1, 'failure_kind': 'missing_provenance'}, 'unresolved'),
    ({'origin': 'independent', 'exit_code': 0, 'assertion_failures': [{'line': 2}], 'failure_kind': 'missing_provenance'}, 'unresolved'),
    ({'origin': 'project', 'exit_code': 0, 'failure_kind': 'missing_coverage'}, 'missing_coverage'),
    ({'origin': 'project', 'exit_code': 1, 'environment_fault': True, 'failure_kind': 'missing_provenance'}, 'environment'),
])
def test_failure_precedence(row, expected):
    assert failure_class(row) == expected


@pytest.mark.skipif(not __import__('shutil').which('bwrap'), reason='Linux command evidence')
def test_failed_execution_labels_precede_missing_coverage_and_provenance(tmp_path):
    from tests.test_daedalus_policy7 import fixture
    from coder_project_runtime import run_revision
    repository, store = fixture(tmp_path, {
        'test_exit.js': 'throw new Error("test dependency missing");\n',
        'test_assert.py': 'assert False, "actual assertion failed"\n',
    })
    profile = {'profiles': [], 'services': [], 'checks': [
        {'id': 'exit', 'cwd': '.', 'phase': 'test', 'command': 'node test_exit.js', 'is_test': True},
        {'id': 'assert', 'cwd': '.', 'phase': 'test', 'command': 'python3 test_assert.py', 'is_test': True},
    ]}
    result = run_revision(store, 'check', repository, profile, project_id='failure-labels')
    rows = {row['id']: row for row in result['checks']}
    assert rows['exit']['failure_kind'] == 'command_failed'
    assert 'test dependency missing' in rows['exit']['log_tail']
    assert rows['assert']['failure_kind'] == 'assertion_failure'
    assert rows['assert']['assertion_failures'] and not rows['assert']['passed']


def test_rollback_candidate_keeps_exact_request_and_retained_parent_lineage():
    import asyncio
    from types import SimpleNamespace
    from coder_policy7_controller import step
    request = 'Keep the quoted user behavior; preserve its exact request.'
    job = {'id': 'job', 'state': 'checking', 'project_id': 'p', 'revision_id': 'failed',
           'worker_operation': 'check', 'verification_version': 3, 'user_task': request,
           'brief': {'outcomes': [{'id': 'active', 'text': 'Retained behavior', 'evidence_types': ['behavior']}],
                     'requirement_history': [{'parent_id': 'parent', 'action': 'retain', 'active_outcome_ids': ['active']}]}}
    saved = {}
    async def operate(*args, **kwargs):
        return {'restored_snapshot': {'revision': 'before'}, 'repair_regression': {'failed_revision': 'failed'}}, job
    async def save(identity, **changes):
        saved.update(changes); return {**job, **changes}
    async def record(*args): pass
    controller = SimpleNamespace(store=SimpleNamespace(save=save, record_check=record), _operate=operate)
    asyncio.run(step(job, controller))
    summary = saved['verification_summary']
    assert saved['state'] == 'candidate_packaging' and not summary['accepted']
    assert any(c['owner'] == 'request' and c['request_excerpt'] == 'Keep the quoted user behavior' for c in summary['criteria'])
    assert next(c for c in summary['criteria'] if c['owner'] == 'active')['parent_ids'] == ['parent']


@pytest.mark.parametrize('repair_round', [0, 1])
def test_finished_builder_with_failed_setup_recovers_before_any_audit(repair_round):
    import asyncio
    from types import SimpleNamespace
    from coder_policy7_controller import step
    from coder_policy7_builder import guidance
    from context_policy import DEFAULTS
    job = {'id': 'job', 'state': 'checking', 'project_id': 'p', 'revision_id': 'current',
           'worker_operation': 'check', 'verification_version': 3, 'task': 'Build the requested application.',
           'repair_round': repair_round, 'calls_used': 10, 'stage_allocation': {'verification_reserve': 30},
           'last_patch': {'agent_finished': True}, 'brief': {'outcomes': [{'id': 'o', 'text': 'Requested behavior'}]}}
    failure = {'id': '.:setup:0', 'phase': 'setup', 'passed': False, 'exit_code': 1,
               'classification': 'application_defect', 'command': 'pip install -r requirements.txt',
               'log_tail': 'No matching distribution found for sqlite3'}
    calls = []
    async def operate(current, kind, **kwargs):
        calls.append(kind); return {'checks': [failure], 'execution_profile': {}}, current
    async def save(identity, **changes):
        job.update(changes); return dict(job)
    async def record(*args): pass
    controller = SimpleNamespace(store=SimpleNamespace(save=save, record_check=record), _operate=operate,
                                 context_policy=SimpleNamespace(runtime_settings=lambda: dict(DEFAULTS)))
    asyncio.run(step(dict(job), controller))
    assert calls == ['check'] and job['state'] == 'coding'
    assert job['repair_round'] == repair_round and job['preflight_retries'] == 1
    assert 'No matching distribution found for sqlite3' in guidance(job)
    job.update(state='checking', preflight_retries=5)
    asyncio.run(step(dict(job), controller))
    assert calls == ['check', 'check'] and job['state'] == 'candidate_packaging'
    assert job['stop_limit'] == 'no_progress' and not job['verification_summary']['accepted']
    assert job['repair_round'] == repair_round


def test_requested_deliverables_and_negation():
    assert deliverables('Add browser regression checks and a README.') == {'tests', 'documentation'}
    assert deliverables('No tests. Include documentation.') == {'documentation'}
    assert deliverables('Build a calculator without tests or documentation.') == set()
    assert deliverables('Without network access, include tests and documentation.') == {'tests', 'documentation'}
    assert deliverables('No tests and add a README.') == {'documentation'}
    assert deliverables('Tests are not required; document usage.') == {'documentation'}


@pytest.mark.parametrize('configured,remaining,reserve', [(120, 120, 30), (120, 29, 14), (8, 3, 1), (120, 1, 0), (1, 0, 0)])
def test_verification_reserve_remains_usable(configured, remaining, reserve):
    result = coding_allocation(configured, remaining)
    assert result['verification_reserve'] == reserve
    assert result['coding_calls'] + reserve == remaining


def test_caught_python_assertion_is_retained(tmp_path):
    from coder_native_checks import PYTHON_TRACE
    from coder_assertion_outcomes import PYTHON_OUTCOMES, read_outcomes
    root, audit, hooks, evidence = [tmp_path / x for x in ('project', 'audit', 'hooks', 'evidence')]
    for path in (root, audit, hooks, evidence): path.mkdir()
    (hooks / 'sitecustomize.py').write_text(PYTHON_TRACE + PYTHON_OUTCOMES)
    (audit / 'check.py').write_text('actual = 2\ntry:\n assert actual == 3\nexcept AssertionError:\n pass\n')
    env = {**os.environ, 'PYTHONPATH': str(hooks), 'DAEDALUS_PROJECT_ROOT': str(root), 'DAEDALUS_AUDIT_DIR': str(audit), 'DAEDALUS_PROVENANCE': str(evidence)}
    result = subprocess.run([sys.executable, str(audit / 'check.py')], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    failures, _ = read_outcomes(evidence)
    assert failures and failures[0]['line'] == 3
    assert failures[0]['values']['actual'] == 2


def test_supported_python_expected_exception(tmp_path):
    from coder_native_checks import PYTHON_TRACE
    from coder_assertion_outcomes import PYTHON_OUTCOMES, read_outcomes
    (tmp_path / 'sitecustomize.py').write_text(PYTHON_TRACE + PYTHON_OUTCOMES)
    (tmp_path / 'check.py').write_text('import unittest\nwith unittest.TestCase().assertRaises(AssertionError):\n assert False\n')
    env = {**os.environ, 'PYTHONPATH': str(tmp_path), 'DAEDALUS_PROJECT_ROOT': str(tmp_path), 'DAEDALUS_AUDIT_DIR': str(tmp_path), 'DAEDALUS_PROVENANCE': str(tmp_path)}
    result = subprocess.run([sys.executable, str(tmp_path / 'check.py')], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not read_outcomes(tmp_path)[0]


@pytest.mark.parametrize('absolute', [False, True])
def test_audit_subprocess_fixture_diagnostics_survive_cleanup(tmp_path, absolute):
    from coder_native_checks import PYTHON_TRACE
    from coder_assertion_outcomes import PYTHON_OUTCOMES, read_subprocesses, audit_fixture_path_error
    root, audit, hooks, evidence, temp = [tmp_path / x for x in ('project', 'audit', 'hooks', 'evidence', 'temp')]
    for path in (root, audit, hooks, evidence, temp): path.mkdir()
    (hooks / 'sitecustomize.py').write_text(PYTHON_TRACE + PYTHON_OUTCOMES)
    (root / 'app.py').write_text('import pathlib,sys\nprint(pathlib.Path(sys.argv[1]).read_text())\n')
    (audit / 'check.py').write_text('''import pathlib,subprocess,sys,tempfile
with tempfile.TemporaryDirectory() as directory:
 fixture=pathlib.Path(directory)/'input.txt'
 fixture.write_text('fixture content')
 result=subprocess.run([sys.executable,'app.py',str(fixture) if ABSOLUTE else fixture.name],capture_output=True,text=True)
assert result.returncode==0,result.stderr
'''.replace('ABSOLUTE', repr(absolute)))
    env = {**os.environ, 'PYTHONPATH': str(hooks), 'TMPDIR': str(temp), 'DAEDALUS_PROJECT_ROOT': str(root),
           'DAEDALUS_AUDIT_DIR': str(audit), 'DAEDALUS_PROVENANCE': str(evidence)}
    result = subprocess.run([sys.executable, str(audit / 'check.py')], cwd=root, env=env, capture_output=True)
    rows = read_subprocesses(evidence)
    assert len(rows) == 1, result.stderr
    assert rows[0]['cwd'] == str(root)
    assert rows[0]['argv'][1] == 'app.py'
    error = audit_fixture_path_error(rows)
    if absolute:
        assert result.returncode == 0 and rows[0]['stdout'] == 'fixture content\n'
        assert error is None
    else:
        assert result.returncode != 0 and rows[0]['returncode'] != 0
        assert 'No such file' in rows[0]['stderr']
        assert error['argument'] == 'input.txt'
        assert not Path(error['fixture']).exists()


@pytest.mark.skipif(not __import__('shutil').which('bwrap'), reason='Linux audit execution')
def test_relative_fixture_failure_routes_to_audit_correction(tmp_path):
    from tests.test_daedalus_policy7 import fixture
    from coder_project_runtime import run_revision
    repo, store = fixture(tmp_path, {'app.py': 'import pathlib,sys\nprint(pathlib.Path(sys.argv[1]).read_text())\n'})
    audit = tmp_path / 'audit'; audit.mkdir()
    (audit / 'test_behavior.py').write_text('''import pathlib,subprocess,tempfile
with tempfile.TemporaryDirectory() as directory:
 fixture=pathlib.Path(directory)/'input.txt'
 fixture.write_text('data')
 result=subprocess.run(['python3','app.py','input.txt'],capture_output=True,text=True)
assert result.returncode==0,result.stderr
''')
    checks = [{'id': 'audit', 'command': 'python3 {audit}/test_behavior.py', 'origin': 'independent', 'is_test': True}]
    result = run_revision(store, 'check', repo, {'profiles': [], 'services': [], 'checks': []},
                          project_id='fixture-diagnostics', audit_source=audit, checks=checks)
    row = result['checks'][0]
    assert not row['passed'] and row['classification'] == 'audit_defect', row
    assert row['audit_fixture_error']['argument'] == 'input.txt'
    assert 'No such file' in row['subprocess_diagnostics'][0]['stderr']


@pytest.mark.skipif(not __import__('shutil').which('bwrap'), reason='Linux audit execution')
def test_nonfinite_assertion_diagnostics_preserve_worker_api_and_stop(monkeypatch, tmp_path):
    from tests.test_daedalus_policy7 import fixture
    from coder_project_runtime import run_revision
    from coder_worker_runtime import install_routes
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    repo, store = fixture(tmp_path, {'app.py': 'VALUE=1\n'})
    audit = tmp_path / 'audit'; audit.mkdir()
    (audit / 'check.py').write_text('''from app import VALUE
actual=float('nan')
positive=float('inf')
negative=float('-inf')
enormous=1<<20000
text='undecodable:'+chr(0xD800)
try:
 assert actual==VALUE
except AssertionError:
 pass
''')
    checks = [{'id': 'audit', 'command': 'python3 {audit}/check.py', 'origin': 'independent', 'is_test': True}]
    result = run_revision(store, 'check', repo, {'profiles': [], 'services': [], 'checks': []},
                          project_id='nonfinite', audit_source=audit, checks=checks)
    row = result['checks'][0]
    assert not row['passed'] and row['failure_kind'] == 'assertion_failure'
    values = row['assertion_failures'][0]['values']
    assert values['actual'] == 'nan' and values['positive'] == 'inf' and values['negative'] == '-inf'
    assert values['enormous'] == '<integer: 20001 bits>'
    assert values['text'] == 'undecodable:\\ud800'
    store.update('check', status='succeeded', result=result)
    monkeypatch.setenv('DAEDALUS_STATE_DIR', str(tmp_path / 'worker'))
    app = FastAPI(); install_routes(app, tmp_path / 'projects')
    with TestClient(app, raise_server_exceptions=False) as client:
        for path in ('/jobs/job/operations/check', '/jobs/job/operations/check/events'):
            response = client.get(path)
            assert response.status_code == 200, response.text
            json.dumps(response.json(), allow_nan=False)
        assert client.post('/jobs/job/operations/check/cancel').status_code == 200


def test_foreign_outcome_numbers_cannot_break_json_responses(tmp_path):
    from coder_assertion_outcomes import read_outcomes, read_subprocesses
    (tmp_path / 'outcomes-python-1.json').write_text(r'''{"version":3,
"failures":[{"values":{"actual":NaN,"expected":1e400,"text":"\ud800"}}],
"subprocesses":[{"time_ns":1,"returncode":1,"extra":Infinity}]}''')
    failures, _ = read_outcomes(tmp_path)
    processes = read_subprocesses(tmp_path)
    assert failures and processes
    assert failures[0]['values']['actual'] == 'NaN'
    json.dumps({'failures': failures, 'subprocesses': processes}, allow_nan=False, ensure_ascii=False).encode('utf-8')


def test_node_caught_assertion_and_expected_exception(tmp_path):
    import shutil
    if not shutil.which('node'): pytest.skip('Node unavailable')
    from coder_assertion_outcomes import NODE_OUTCOMES, read_outcomes
    preload = tmp_path / 'outcomes.cjs'; preload.write_text(NODE_OUTCOMES)
    script = tmp_path / 'test.cjs'
    script.write_text("const assert = require('node:assert'); assert.throws(() => assert.equal(1,2)); try { assert.strictEqual(2,3); } catch {}")
    result = subprocess.run(['node', '--require', str(preload), str(script)], env={**os.environ, 'DAEDALUS_PROVENANCE': str(tmp_path)}, capture_output=True)
    assert result.returncode == 0, result.stderr
    failures, observed = read_outcomes(tmp_path)
    assert observed >= 2
    assert len(failures) == 1 and failures[0]['actual'] == 2


def test_repair_regression_retains_both_revisions_and_restores_tests(tmp_path):
    from coder_policy7_ops import repair_regressions, restore_repair
    from coder_repository import Repository
    root = tmp_path / 'source'; root.mkdir()
    (root / 'app.rs').write_text('fn behavior() {}')
    (root / 'tests.rs').write_text('// fourteen retained cases')
    repo = Repository(root, tmp_path / 'revisions')
    before = repo.snapshot()['revision']
    checks = [{'id': 'cargo:test', 'passed': True, 'is_test': True, 'test_count': 14}]
    (root / 'tests.rs').unlink()
    (root / 'app.rs').write_text('fn main() { println!("Hello"); }')
    (root / 'starter.rs').write_text('fn placeholder() {}')
    failed = repo.snapshot(parent=before)['revision']
    repo.refresh()  # Normal verification inventories the failed checkpoint.
    assert repair_regressions({'repair_checkpoint': before, 'repair_checks': checks}, []) == ['cargo:test']
    restored = restore_repair(repo, before, failed)
    assert restored['revision'] == before
    assert (root / 'tests.rs').exists()
    assert not (root / 'starter.rs').exists()
    assert b'Hello' in repo.git('show', failed + ':app.rs')
    assert b'placeholder' in repo.git('show', failed + ':starter.rs')
    assert repo.git('rev-parse', 'refs/daedalus/failed-repairs/' + failed).decode().strip() == failed


def test_project_regression_guard_does_not_require_later_stage_checks():
    from coder_policy7_ops import repair_regressions
    project = {'id': 'project-test', 'origin': 'project', 'is_test': True, 'passed': True, 'test_count': 3}
    job = {'repair_checkpoint': 'before', 'repair_checks': [project,
        {'id': 'audit-1', 'origin': 'independent', 'is_test': True, 'passed': True},
        {'id': 'browser-probe', 'origin': 'controller', 'is_test': True, 'passed': True}]}
    assert repair_regressions(job, [project]) == []
    assert repair_regressions(job, []) == ['project-test']
    assert repair_regressions(job, [{**project, 'passed': False}]) == ['project-test']
    assert repair_regressions(job, [{**project, 'test_count': 2}]) == ['project-test']


@pytest.mark.skipif(not __import__('shutil').which('bwrap'), reason='Linux command evidence')
def test_repaired_project_keeps_revision_and_reaches_pending_audit(tmp_path):
    import asyncio, time
    from types import SimpleNamespace
    from tests.test_daedalus_policy7 import fixture
    from coder_policy7_ops import execute_operation
    from coder_policy7_controller import step
    repo, store = fixture(tmp_path, {'app.py': 'VALUE=1\n', 'test_app.py': 'import app\nassert app.VALUE == 2\n'})
    before = store.get('check')['payload']['revision_id']
    (repo.root / 'app.py').write_text('VALUE=2\n')
    repaired = repo.snapshot(parent=before)['revision']
    job = {'id': 'job', 'state': 'checking', 'project_id': 'p', 'revision_id': repaired,
           'repair_checkpoint': before, 'brief': {'outcomes': []},
           'execution_commands': {'packages': {'.': {'test': 'python3 test_app.py'}}},
           'repair_checks': [{'id': 'audit-1', 'origin': 'independent', 'passed': True, 'is_test': True}],
           'audit_checks': [{'id': 'audit-1'}]}
    payload = {**store.get('check')['payload'], 'revision_id': repaired, 'original_task': 'Fix VALUE',
               'project_id': 'p', 'policy7_job': job}
    store.create('repaired-check', 'job', 'check', payload)
    store.update('repaired-check', started=time.time(), status='running')
    result = execute_operation(store, 'repaired-check', repo)
    assert 'restored_snapshot' not in result
    assert any(c.get('is_test') and c.get('passed') for c in result['checks'])
    assert repo.snapshot(parent=repaired)['revision'] == repaired
    saved = {}
    async def operate(*args, **kwargs):
        return result, {**job, 'worker_operation': 'repaired-check'}
    async def save(identity, **changes):
        saved.update(changes)
        return {**job, **changes}
    async def record(*args): pass
    controller = SimpleNamespace(store=SimpleNamespace(save=save, record_check=record), _operate=operate)
    asyncio.run(step(job, controller))
    assert saved['state'] == 'auditing'
    assert 'verification_summary' not in saved  # No stale audit is counted as current proof.


def test_each_defect_retains_an_application_owner():
    from coder_policy7_controller import repair_targets
    job = {'last_patch': {'source_hashes': {'app.go': 'x', 'app_test.go': 'y', 'go.mod': 'z'}},
           'brief': {'batches': [{'files': ['app.go', 'app_test.go', 'go.mod']}]}}
    defects = [{'id': 'test', 'command': 'go test', 'test_files': [{'path': 'app_test.go'}]}, {'id': 'interface:--sort'}]
    assert 'app.go' in repair_targets(job, defects)


def test_review_packing_reserves_correction_and_keeps_requirements():
    from coder_policy7_ops import pack_review
    from context_policy import estimate_tokens
    data = {'original_request': 'Tie counts alphabetically.', 'outcomes': [{'id': 'o1'}],
            'checks': [{'id': 'bad', 'passed': False, 'log_tail': 'actual wrong'}],
            'source': {'files': {'huge.cs': 'noise ' * 50000}}}
    text = pack_review('Review: ', data, 1500)
    assert 'Tie counts alphabetically.' in text and 'actual wrong' in text
    assert estimate_tokens({'messages': [{'role': 'user', 'content': text}]}) <= 988


def test_go_flag_declarations_and_comments():
    from coder_policy7_evidence import interface_files
    assert interface_files('flag', '--limit', {'main.go': 'flag.Int("limit", 10, "count")'}) == ['main.go']
    assert not interface_files('flag', '--limit', {'main.go': '// --limit is supported'})


def test_scoped_audit_count_and_endpoint_punctuation():
    from coder_audit_hygiene import requested_endpoints, web_audit_faults
    assert ('GET', '/api/tasks') in requested_endpoints('Use GET /api/tasks. Then POST /api/tasks.')
    text = "# DAEDALUS_APP_URL\nmine = [r for r in rows if r['id'] == created['id']]\nassert len(mine) == 1\n"
    assert not web_audit_faults('test.py', text)


def test_prepared_dependency_links_never_enter_revision_archives(tmp_path):
    import tarfile
    from coder_repository import Repository
    source = tmp_path / 'source'; source.mkdir()
    cache = tmp_path / 'cache'; cache.mkdir()
    (source / 'app.py').write_text('print("application")\n')
    for name in ('.venv', 'node_modules', 'vendor'):
        target = cache / name; target.mkdir()
        (target / 'dependency').write_text('private cache')
        (source / name).symlink_to(target, target_is_directory=True)
    repository = Repository(source, tmp_path / 'state', excludes=('.venv', 'node_modules', 'vendor'))
    repository.refresh()
    revision = repository.snapshot()['revision']
    archive = tmp_path / 'revision.tar.gz'; repository.archive(revision, archive)
    with tarfile.open(archive) as bundle:
        assert bundle.getnames() == ['app.py']
        bundle.extractall(tmp_path / 'execution', filter='data')
    assert (tmp_path / 'execution/app.py').read_text() == 'print("application")\n'
    from coder_policy7_ops import missing_repair_deliverables
    job = {'repair_checkpoint': revision, 'repair_deliverables': ['app.py']}
    assert missing_repair_deliverables(job, repository) == []
    (source / 'app.py').unlink()
    assert missing_repair_deliverables(job, repository) == ['deliverable:app.py']


@pytest.mark.skipif(not __import__('shutil').which('bwrap'), reason='Linux Node command isolation')
def test_node_build_omits_instrumentation_but_test_observes_assertions(tmp_path):
    from tests.test_daedalus_policy7 import fixture
    from coder_project_runtime import run_revision
    repository, store = fixture(tmp_path, {
        'app.js': 'module.exports = () => 42;\n',
        'test.js': 'const assert = require("node:assert"); const answer = require("./app.js"); assert.equal(answer(), 42);\n',
    })
    profile = {'profiles': [], 'services': [], 'checks': [
        {'id': 'syntax', 'cwd': '.', 'phase': 'build', 'command': 'node --check app.js'},
        {'id': 'tests', 'cwd': '.', 'phase': 'test', 'command': 'node test.js', 'is_test': True},
    ]}
    result = run_revision(store, 'check', repository, profile, project_id='node-build')
    rows = {row['id']: row for row in result['checks']}
    assert rows['syntax']['passed'], rows['syntax']['log_tail']
    assert rows['tests']['passed'], rows['tests']['log_tail']
    assert rows['tests']['coverage_observed'] and rows['tests']['source_bindings']


def test_generated_dotnet_outputs_are_excluded_but_authored_bin_is_kept(tmp_path):
    from coder_patch_runtime import tree_hashes
    from coder_repository import Repository
    root = tmp_path / 'project'; root.mkdir()
    (root / 'App.csproj').write_text('<Project/>')
    (root / 'obj').mkdir(); (root / 'obj/project.assets.json').write_text('{}')
    (root / 'obj/App.AssemblyInfo.cs').write_text('// generated')
    (root / 'bin').mkdir(); (root / 'bin/tool.py').write_text('print(1)')
    repo = Repository(root, tmp_path / 'repo')
    revision = repo.snapshot()['revision']
    assert 'bin/tool.py' in tree_hashes(root)
    assert not any(name.startswith('obj/') for name in tree_hashes(root))
    assert b'obj/' not in repo.git('ls-tree', '-r', '--name-only', revision)


def test_swallowed_unittest_assertion_is_retained(tmp_path):
    from coder_native_checks import PYTHON_TRACE
    from coder_assertion_outcomes import PYTHON_OUTCOMES, read_outcomes
    (tmp_path / 'sitecustomize.py').write_text(PYTHON_TRACE + PYTHON_OUTCOMES)
    (tmp_path / 'check.py').write_text('import unittest\ntry:\n unittest.TestCase().assertEqual(1,2)\nexcept AssertionError:\n pass\n')
    env = {**os.environ, 'PYTHONPATH': str(tmp_path), 'DAEDALUS_PROJECT_ROOT': str(tmp_path), 'DAEDALUS_AUDIT_DIR': str(tmp_path), 'DAEDALUS_PROVENANCE': str(tmp_path)}
    result = subprocess.run([sys.executable, str(tmp_path / 'check.py')], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert read_outcomes(tmp_path)[0]


def test_node_async_expected_failure_does_not_mask_unrelated_failure(tmp_path):
    import shutil
    if not shutil.which('node'): pytest.skip('Node unavailable')
    from coder_assertion_outcomes import NODE_OUTCOMES, read_outcomes
    preload = tmp_path / 'outcomes.cjs'; preload.write_text(NODE_OUTCOMES)
    script = tmp_path / 'test.cjs'
    script.write_text("""const a = require('node:assert');
(async () => {
  const anticipated = a.rejects(async () => { await Promise.resolve(); a.equal(1,2); });
  try { a.equal(3,4); } catch {}
  await anticipated;
})().catch(() => process.exitCode = 1);
""")
    result = subprocess.run(['node', '--require', str(preload), str(script)], env={**os.environ, 'DAEDALUS_PROVENANCE': str(tmp_path)}, capture_output=True)
    assert result.returncode == 0, result.stderr
    failures, _ = read_outcomes(tmp_path)
    assert len(failures) == 1 and failures[0]['actual'] == 3


def test_named_deliverable_disappearance_is_a_repair_regression(tmp_path):
    from coder_policy7_ops import missing_repair_deliverables
    from coder_repository import Repository
    root = tmp_path / 'source'; root.mkdir()
    (root / 'README.md').write_text('Instructions for the delivered program.')
    repository = Repository(root, tmp_path / 'revisions')
    checkpoint = repository.snapshot()['revision']
    job = {'repair_checkpoint': checkpoint, 'repair_deliverables': ['README.md'], 'repair_documentation': ['.']}
    assert missing_repair_deliverables(job, repository) == []
    (root / 'README.md').unlink()
    assert missing_repair_deliverables(job, repository) == ['deliverable:README.md', 'documentation:.']


def test_old_evidence_waits_for_writer_ack_then_rechecks_exact_checkpoint():
    import asyncio
    from types import SimpleNamespace
    from coder_policy7_controller import step
    job = {'id':'j', 'state':'accepting', 'verification_version':2, 'brief': {'outcomes':[]},
           'worker_operation':'old-op','operation_accounted':False, 'revision_id':'old', 'calls_used':4}
    calls, writes = [], []
    operation = {'status':'running'}
    async def request(identity, method, url, **kw):
        calls.append(method)
        return SimpleNamespace(json=lambda: dict(operation))
    async def save(identity, **changes):
        writes.append(changes); job.update(changes); return dict(job)
    async def record(*args): pass
    controller = SimpleNamespace(store=SimpleNamespace(save=save,record_revision=record), worker_url=lambda job:'/old', _worker_request=request)
    asyncio.run(step(job, controller))
    assert calls == ['GET','POST'] and not writes
    operation.update(status='cancelled', calls=2, started=10, ended=12, result={'snapshot':{'revision':'checkpoint'}})
    asyncio.run(step(job, controller))
    assert job['state']=='checking' and job['revision_id']=='checkpoint'
    assert job['verification_version']==3 and job['calls_used']==6 and not job['worker_operation']
    assert job['acceptance'] is None and job['checks']==[]


@pytest.mark.skipif(not __import__('shutil').which('bwrap'), reason='Linux managed service isolation')
def test_form_probe_data_is_not_visible_to_project_tests(tmp_path):
    from tests.test_daedalus_policy7 import fixture
    from coder_project_runtime import run_revision
    files = {
        'app.py': "import os,pathlib\ndef count():\n p=pathlib.Path(os.environ['APP_DB_PATH']); return int(p.read_text()) if p.exists() else 0\n",
        'test_state.py': 'from app import count\nassert count() == 0\n',
        'server.py': '''import os,pathlib
from http.server import BaseHTTPRequestHandler,HTTPServer
from app import count
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  body=b'<form><input type="text" name="title" required><button type="submit">Create</button></form><script>document.querySelector("form").onsubmit=async e=>{e.preventDefault();await fetch("/items",{method:"POST",body:"{}"})}</script>'
  self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
 def do_POST(self):
  pathlib.Path(os.environ['APP_DB_PATH']).write_text(str(count()+1))
  self.send_response(201);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{}')
HTTPServer(('127.0.0.1',int(os.environ['PORT'])),H).serve_forever()
'''}
    repo, store = fixture(tmp_path, files)
    profile = {'profiles': [], 'checks': [{'id':'clean-state', 'cwd':'.', 'phase':'test','is_test':True,'origin':'project','command':'python3 test_state.py'}],
               'services':[{'id':'app','cwd':'.','command':'PORT={port} python3 server.py'}]}
    result = run_revision(store,'check',repo,profile,project_id='probe-isolation')
    form = next(c for c in result['checks'] if c['id'] == 'form:app')
    assert form['passed'] and any(w['status'] == 201 for w in form['form_probe']['writes']), form
    test = next(c for c in result['checks'] if c['id']=='clean-state')
    assert test['exit_code']==0, test


def test_retained_criteria_preserve_parent_identity():
    from coder_criteria import criteria
    rows = criteria('', [{'id':'o2','text':'Keep alphabetical ties','evidence_types':['behavior']}],
                    [{'parent_id':'o1','action':'retain','active_outcome_ids':['o2']}])
    assert rows[0]['parent_ids']==['o1'] and rows[0]['owners']==['o2']


def test_planner_keeps_authored_bin_deliverables():
    from coder_policy7 import make_brief
    brief = make_brief({'outcomes':['Deliver command'], 'batches':[{'task':'Write command','files':['bin/tool.py']}]},'Deliver bin/tool.py')
    assert brief['batches'][0]['files']==['bin/tool.py']


def test_first_coding_operation_uses_persisted_allocation_identity():
    import asyncio
    from types import SimpleNamespace
    from coder_policy7_controller import step
    initial={'id':'j','state':'coding','project_id':'p','revision_id':'r','brief':{'outcomes':[],'batches':[]}}
    saved=dict(initial); operations=[]
    async def save(identity,**changes):
        saved.update(changes);return dict(saved)
    async def operate(job,kind,**kwargs):
        operations.append(kwargs);raise ConnectionError('simulate disconnect after request creation')
    controller=SimpleNamespace(store=SimpleNamespace(save=save),_operate=operate,
        context_policy=SimpleNamespace(runtime_settings=lambda:{'daedalus_model_calls':120}))
    for job in (initial,saved):
        with pytest.raises(ConnectionError):asyncio.run(step(dict(job),controller))
    assert operations[0]==operations[1]
    assert operations[0]['policy7_job']['stage_allocation']['verification_reserve']==30


def test_cancel_before_dispatch_fences_a_delayed_start(tmp_path):
    from coder_worker_runtime import WorkerStore,launch
    from context_policy import DEFAULTS
    store=WorkerStore(tmp_path/'worker')
    row=store.cancel_before_dispatch('op','job')
    assert row['status']=='cancelled'
    assert store.create('op','job','code',{'settings':DEFAULTS,'request_key':'late'}) is False
    launch(store,'op')
    assert store.get('op')['pid'] is None and store.get('op')['calls']==0
    with pytest.raises(ValueError):store.create('op','other-job','code',{})
    with pytest.raises(ValueError):store.cancel_before_dispatch('op','other-job')


def test_named_unittest_is_an_explicit_test_obligation():
    assert 'tests' in deliverables('Add a unittest test_calc.py and README.')


def test_cancel_and_completion_account_usage_once(monkeypatch,tmp_path):
    import asyncio
    from tests.test_daedalus_jobs import setup7
    from db import coder_jobs as store
    setup7(monkeypatch,tmp_path)
    async def scenario():
        job=await store.create('conversation','task','build_from_prompt','','local','account',policy_version=7)
        await store.save(job['id'],worker_operation='op',operation_accounted=False,calls_used=2)
        operation={'status':'cancelled','started':10,'ended':13,'calls':4}
        await asyncio.gather(*(store.account_operation(job['id'],'op','code',operation) for _ in range(4)))
        current=await store.get(job['id'])
        assert current['calls_used']==6 and current['seconds_used']==3
        assert len(current['stage_usage'])==1
    asyncio.run(scenario())


def test_baseline_test_changes_require_current_authorization():
    from coder_policy7_ops import baseline_test_files
    baseline=[{'test_files':[{'path':'tests/check.py','sha256':'a'}]}]
    for task in ('Fix the application total.', 'Do not modify tests/check.py.', 'Add new regression tests.'):
        assert baseline_test_files({'task':task,'baseline_checks':baseline})==['tests/check.py']
    for task in ('Update tests/check.py to cover JSON.', 'Update the existing tests for the changed behavior.'):
        assert baseline_test_files({'task':task,'baseline_checks':baseline})==[]


def test_readme_server_recipe_requires_existing_source(tmp_path):
    from coder_profiles import documented_launch
    (tmp_path/'README.md').write_text('Run:\n```sh\npython3 -m uvicorn api:app --port 8000\n```\n')
    assert documented_launch(tmp_path) is None
    (tmp_path/'api.py').write_text('from fastapi import FastAPI\napp=FastAPI()\n')
    assert documented_launch(tmp_path)=='.venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port {port}'


def test_editor_cannot_weaken_baseline_test_without_authorization(monkeypatch,tmp_path):
    from tests.test_daedalus_policy7 import fixture
    from coder_policy7_ops import execute_operation
    import coder_policy7_ops
    import time
    files={'app.py':'VALUE=1\n','test_app.py':'from app import VALUE\nassert VALUE==2\n'}
    repo,store=fixture(tmp_path,files)
    revision=store.get('check')['payload']['revision_id']
    payload={**store.get('check')['payload'],'original_task':'Fix VALUE','project_id':'p',
        'policy7_job':{'task':'Fix VALUE','brief':{'batches':[{'task':'Fix VALUE','files':['app.py']}],'outcomes':[]},
                      'baseline_revision':revision,'baseline_test_files':['test_app.py']}}
    store.create('edit','job','code',payload);store.update('edit',started=time.time(),status='running')
    def edit(*args,**kwargs):
        (repo.root/'test_app.py').write_text('assert True\n')
        (repo.root/'app.py').write_text('VALUE=2\n')
        return {'changed':['test_app.py','app.py']}
    monkeypatch.setattr(coder_policy7_ops,'edit',edit)
    result=execute_operation(store,'edit',repo)
    assert (repo.root/'test_app.py').read_text()==files['test_app.py']
    attempted=result['unauthorized_test_edits']['revision_id']
    assert repo.git('show',attempted+':test_app.py')==b'assert True\n'
    assert result['changed']==['app.py']


@pytest.mark.skipif(not __import__('shutil').which('bwrap'), reason='Linux managed service isolation')
def test_cooperating_services_can_start_after_thirty_seconds(tmp_path):
    import time,urllib.request
    from tests.test_daedalus_policy7 import fixture
    from coder_project_runtime import services
    code='''import os,urllib.request
from http.server import BaseHTTPRequestHandler,HTTPServer
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  body=b'backend'
  if os.environ.get('DAEDALUS_BACKEND_URL'):
   body=b'frontend:'+urllib.request.urlopen(os.environ['DAEDALUS_BACKEND_URL']).read()
  self.send_response(200);self.end_headers();self.wfile.write(body)
HTTPServer(('127.0.0.1',int(os.environ['PORT'])),H).serve_forever()
'''
    repo,store=fixture(tmp_path,{'server.py':code})
    payload=store.get('check')['payload'];payload['settings'].update(daedalus_browser_startup_seconds=45,daedalus_command_seconds=50)
    store.update('check',payload=payload)
    folder=tmp_path/'services';folder.mkdir()
    definitions=[{'id':'backend','command':'sleep 31; PORT={port} python3 server.py'},
                 {'id':'frontend','command':'PORT={port} python3 server.py'}]
    start=time.monotonic()
    with services(store,'check',repo.root,definitions,{},folder) as (env,rows):
        assert all(row['passed'] for row in rows), rows
        assert time.monotonic()-start>=31
        with urllib.request.urlopen(env['DAEDALUS_FRONTEND_URL'],timeout=5) as response:
            assert response.read()==b'frontend:backend'


def test_simultaneous_stop_requests_return_one_accounted_result(monkeypatch,tmp_path):
    import asyncio,httpx
    from tests.test_daedalus_jobs import setup7
    import coder_jobs as controller
    from db import coder_jobs as store
    setup7(monkeypatch,tmp_path)
    async def scenario():
        job=await store.create('conversation','task','build_from_prompt','','local','stop',policy_version=7)
        await store.save(job['id'],state='coding',worker_operation='op',operation_kind='code',operation_accounted=False)
        async def remote(request):
            return httpx.Response(200,json={'status':'cancelled','calls':4,'started':10,'ended':13,'result':{}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
            controller.configure(http,None)
            responses=await asyncio.gather(*(controller.cancel(job['id']) for _ in range(2)))
        assert all(row['state']=='cancelled' for row in responses)
        current=await store.get(job['id']);assert current['calls_used']==4 and len(current['stage_usage'])==1
    asyncio.run(scenario())
