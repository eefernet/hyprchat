import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coder_criteria import deliverables
from coder_project_runtime import failure_class
from context_policy import coding_allocation


@pytest.mark.parametrize('row,expected', [
    ({'origin': 'project', 'exit_code': 1, 'failure_kind': 'missing_coverage'}, 'application_defect'),
    ({'origin': 'independent', 'exit_code': 1, 'failure_kind': 'missing_provenance'}, 'unresolved'),
    ({'origin': 'independent', 'exit_code': 0, 'assertion_failures': [{'line': 2}], 'failure_kind': 'missing_provenance'}, 'unresolved'),
    ({'origin': 'project', 'exit_code': 0, 'failure_kind': 'missing_coverage'}, 'missing_coverage'),
    ({'origin': 'project', 'exit_code': 1, 'environment_fault': True, 'failure_kind': 'missing_provenance'}, 'environment'),
])
def test_failure_precedence(row, expected):
    assert failure_class(row) == expected


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


def test_scoped_audit_count_and_endpoint_punctuation():
    from coder_audit_hygiene import requested_endpoints, web_audit_faults
    assert ('GET', '/api/tasks') in requested_endpoints('Use GET /api/tasks. Then POST /api/tasks.')
    text = "# DAEDALUS_APP_URL\nmine = [r for r in rows if r['id'] == created['id']]\nassert len(mine) == 1\n"
    assert not web_audit_faults('test.py', text)


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


def test_retained_criteria_preserve_parent_identity():
    from coder_criteria import criteria
    rows = criteria('', [{'id':'o2','text':'Keep alphabetical ties','evidence_types':['behavior']}],
                    [{'parent_id':'o1','action':'retain','active_outcome_ids':['o2']}])
    assert rows[0]['parent_ids']==['o1'] and rows[0]['owners']==['o2']


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


def test_readme_server_recipe_requires_existing_source(tmp_path):
    from coder_profiles import documented_launch
    (tmp_path/'README.md').write_text('Run:\n```sh\npython3 -m uvicorn api:app --port 8000\n```\n')
    assert documented_launch(tmp_path) is None
    (tmp_path/'api.py').write_text('from fastapi import FastAPI\napp=FastAPI()\n')
    assert documented_launch(tmp_path)=='.venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port {port}'


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
