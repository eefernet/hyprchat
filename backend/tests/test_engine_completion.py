"""A stopped SDK cannot deliver unchanged green tests as a completed edit."""
import asyncio
import os
import sys
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from coder_engine_controller import step
from coder_engine_ops import summarize
from coder_engine_recovery import apply_execution_failure, execution_failure
from coder_engine_runtime import regression_context
from coder_presentation import presentation


def green(revision='same-source'):
    rows = [{'id': 'maven-tests', 'passed': True, 'revision_id': revision,
             'is_test': True, 'evidence_version': 3, 'execution_succeeded': True,
             'coverage_observed': True, 'assertions_executed': 4,
             'source_bindings': [{'path': 'Main.java', 'sha256': 'unchanged'}]}]
    return {'checks': rows, 'summary': summarize(rows, revision), 'execution_profile': {}}


@pytest.mark.parametrize('status', ['stuck', 'error', 'ConversationExecutionStatus.STUCK'])
def test_sdk_failure_overrides_green_unchanged_tests(status):
    result = green()
    apply_execution_failure(result, {'execution_status': status, 'agent_finished': False,
                                    'incomplete_reason': 'Agent stopped before verified completion'}, 'same-source')
    assert not result['summary']['checks_ok'] and not result['summary']['checks_passed']
    assert result['summary']['incomplete_reason']
    assert result['summary']['failures'][0]['id'] == 'sdk-execution'
    assert result['summary']['failures'][0]['revision_id'] == 'same-source'
    assert result['summary']['check_counts']['failed'] == 1
    assert result['checks'][0]['passed']  # Actual Maven observations remain intact.


def test_finished_dependency_deferral_can_be_verified_by_controller():
    patch = {'execution_status': 'conversationexecutionstatus.finished', 'agent_finished': False,
             'incomplete_reason': 'Declared dependencies changed; defer to controller-owned preparation.'}
    result = green()
    original = deepcopy(result)
    assert execution_failure(patch) is None
    apply_execution_failure(result, patch, 'same-source')
    assert result == original and result['summary']['checks_passed']


@pytest.mark.parametrize('recovers', [False, True])
def test_stall_recovery_shares_two_repairs_and_packages_truthful_metadata(recovers):
    job = {'id': 'j', 'state': 'coding', 'engine': 'openhands159', 'policy_version': 7,
           'revision_id': 'same-source', 'repair_round': 0, 'calls_used': 12, 'seconds_used': 20,
           'request_fingerprint': 'original-request', 'accepted_revision': 'accepted-source'}
    code_rounds, recorded, packaged = [], [], []

    async def save(_, **changes):
        job.update(deepcopy(changes))
        return deepcopy(job)

    async def record(_, op, revision, result):
        recorded.append(deepcopy(result))

    async def operate(current, kind, **kwargs):
        if kind == 'code':
            code_rounds.append(kwargs['repair_round'])
            if len(code_rounds) > 1:
                assert kwargs['repair_feedback'][0]['diagnostic']['category'] == 'sdk_stuck'
                assert kwargs['repair_feedback'][0]['revision_id'] == 'same-source'
            finished = recovers and len(code_rounds) == 2
            job['calls_used'] += 1
            job['seconds_used'] += 1
            return {'snapshot': {'revision': 'changed-source' if finished else 'same-source'},
                    'inventory': {'files': 2}, 'agent_finished': finished,
                    'execution_status': 'conversationexecutionstatus.finished' if finished else 'conversationexecutionstatus.stuck',
                    'incomplete_reason': '' if finished else 'Agent stopped before verified completion',
                    'hook_denials': kwargs['repair_round']}, deepcopy(job)
        if kind == 'check':
            return green(current['revision_id']), {**deepcopy(job), 'worker_operation': 'check-op'}
        assert kind == 'package'
        packaged.append(deepcopy(kwargs['verification_summary']))
        return {}, deepcopy(job)

    async def deliver(current, result, **kwargs):
        await save(job['id'], state='ready_for_review', candidate_artifact={
            'id': 'artifact', 'metadata': {'revision_id': current['revision_id'],
                'checks_passed': current['verification_summary']['checks_passed'],
                'verification_summary': current['verification_summary']}})
        return {}

    controller = SimpleNamespace(store=SimpleNamespace(save=save, record_check=record),
                                 _operate=operate, _deliver=deliver, _EVENTS=None)

    async def run():
        for _ in range(10):
            if await step(deepcopy(job), controller):
                return
        pytest.fail('Unbounded recovery')
    asyncio.run(run())
    assert code_rounds == ([0, 1] if recovers else [0, 1, 2])
    assert job['calls_used'] == 12 + len(code_rounds) and job['seconds_used'] == 20 + len(code_rounds)
    assert job['request_fingerprint'] == 'original-request' and job['accepted_revision'] == 'accepted-source'
    assert recorded[0]['summary']['incomplete_reason'] and not recorded[0]['summary']['checks_passed']
    assert len(packaged) == 1 and packaged[0]['checks_passed'] is recovers
    assert job['stop_limit'] == ('' if recovers else 'application_repairs')
    assert presentation(job)['phase'] == ('Checks passed — please review' if recovers else 'Build incomplete')
    if not recovers:
        assert 'incomplete' in job['blocker'] and 'Checks passed' not in job['blocker']
        assert not presentation(job)['actions']['resume']['enabled']


def test_baseline_test_commands_and_identities_are_available_before_editing():
    job = {'id': 'j', 'state': 'baselining', 'revision_id': 'baseline'}
    async def save(_, **changes): job.update(changes)
    async def record(*args): pass
    async def operate(*args, **kwargs):
        return {'checks': [{'passed': True, 'is_test': True, 'cwd': '.',
                            'command': 'node --test test.js', 'test_runner': 'node --test test.js'},
                           {'passed': True, 'is_test': False, 'command': 'node --check calculator.js'}],
                'required_tests': ['.::test.js'], 'baseline_test_files': ['test.js']}, {'worker_operation': 'baseline-op'}
    controller = SimpleNamespace(store=SimpleNamespace(save=save, record_check=record), _operate=operate)
    asyncio.run(step(deepcopy(job), controller))
    assert job['state'] == 'coding'
    assert job['baseline_test_commands'] == [{'cwd': '.', 'command': 'node --test test.js',
                                              'test_runner': 'node --test test.js'}]
    context = regression_context({**job, 'baseline_revision': 'baseline'})
    assert '.::test.js' in context and 'node --test test.js' in context and 'baseline' in context
    assert 'node --check' not in context
    assert regression_context({}) == ''


def test_stop_hook_reports_lost_node_runner_within_existing_repairs(tmp_path, monkeypatch):
    from coder_engine_hook import evaluate
    (tmp_path / 'test.js').write_text('require("node:assert/strict").equal(2, 2)\n')
    # No subprocess is needed to establish the lost reporting command; normal
    # test execution and independent behavioral verification remain separate.
    monkeypatch.setattr('coder_profiles.discover', lambda *args: {'checks': []})
    state = {'root': str(tmp_path), 'finish_seen': True, 'denials': 1, 'nudges': 0,
             'command_seconds': 10, 'required_tests': ['.::test.js'],
             'baseline_test_commands': [{'cwd': '.', 'command': 'node --test test.js',
                                          'test_runner': 'node --test test.js'}]}
    deny, reason = evaluate(state)
    assert deny and 'node --test test.js' in reason and state['denials'] == 2
    assert not state['passed']
    deny, reason = evaluate(state)
    assert not deny and 'exhausted' in reason and state['denials'] == 2
    assert not state['passed']


@pytest.mark.skipif(sys.platform != 'linux' or os.getenv('HEPHAESTUS_EXECUTOR') != 'docker',
                    reason='Requires the Linux scoped broker and prepared project environment')
def test_same_count_pytest_replacement_is_a_regression_in_real_container(tmp_path):
    from coder_project_runtime import contract, run_revision
    from coder_repository import Repository
    from coder_required_tests import passing, regressions
    from coder_worker_runtime import WorkerStore
    from context_policy import DEFAULTS
    root=tmp_path/'source';root.mkdir()
    (root/'app.py').write_text('def double(value):\n    return value * 2\n')
    (root/'requirements.txt').write_text('pytest==8.4.2\n')
    test=root/'test_app.py'
    test.write_text('from app import double\ndef test_positive():\n    assert double(3) == 6\n'
                    'def test_negative():\n    assert double(-2) == -4\n')
    repo=Repository(root,tmp_path/'repo',DEFAULTS['daedalus_exclude_dirs'])
    store=WorkerStore(tmp_path/'worker')
    results=[]
    for index in range(2):
        if index:
            test.write_text(test.read_text().replace('test_negative', 'test_replacement'))
        repo.refresh();revision=repo.snapshot('Original' if not index else 'Same count, different name')['revision']
        operation='check-'+str(index)
        store.create(operation,'test-identities','check',{'settings':dict(DEFAULTS),'calls_remaining':0,
            'seconds_remaining':300,'revision_id':revision,'engine':'openhands159','project_id':'test-identities'})
        store.update(operation,status='running',started=time.time())
        try:
            result=run_revision(store,operation,repo,contract(repo,managed_dependencies=True),project_id='test-identities')
            results.append(result['checks'])
        finally:
            store.update(operation,status='succeeded',ended=time.time())
    for checks in results:
        rows=[row for row in checks if row.get('is_test')]
        assert rows and all(row['passed'] for row in rows),checks
        assert sum(row.get('test_count') or 0 for row in rows)==2
        assert len(passing(checks))==2
    assert regressions(passing(results[0]),results[1])==[{'id':'.::test_app.py::test_negative','status':'missing'}]
