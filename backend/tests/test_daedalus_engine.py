"""Modern-engine boundaries: routing, revision evidence, budgets and compatibility."""
import asyncio
import time

import pytest

import coder_jobs
from coder_engine_ops import summarize
from coder_worker_runtime import WorkerStore, _boundary
from context_policy import DEFAULTS, validate_patch
from db import coder_jobs as jobs
from .test_daedalus_jobs import setup


def test_engine_is_frozen_and_idempotency_cannot_change_it(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    monkeypatch.setenv('DAEDALUS_ENGINE_URL', 'http://candidate:8587')
    async def scenario():
        modern = await coder_jobs.create('conversation', 'Build a calculator', model='local', key='engine', engine='openhands159')
        assert modern['engine_version'] == '1.59.0'
        assert coder_jobs.worker_url(modern).startswith('http://candidate:8587/')
        with pytest.raises(ValueError, match='retired'):
            await coder_jobs.create('conversation', 'Build a calculator', model='local', key='engine', engine='legacy')
        again = await jobs.get(modern['id'])
        assert again['engine'] == 'openhands159'
        with pytest.raises(ValueError, match='retired'):
            coder_jobs.worker_url({'id': 'old', 'worker_operation': 'op'})
    asyncio.run(scenario())


def test_modern_engine_requires_explicit_worker_configuration(monkeypatch):
    monkeypatch.delenv('DAEDALUS_ENGINE_URL', raising=False)
    with pytest.raises(ValueError, match='not configured'):
        coder_jobs.worker_base({'engine': 'openhands159'})
    with pytest.raises(ValueError, match='retired'):
        validate_patch({'daedalus_engine': 'whatever'}, DEFAULTS)


def test_evidence_never_uses_empty_or_stale_test_results():
    assert not summarize([], 'new')['checks_passed']
    assert not summarize([{'passed': True, 'revision_id': 'new', 'test_count': 0}], 'new')['checks_passed']
    assert not summarize([{'passed': True, 'revision_id': 'new', 'test_count': None}], 'new')['checks_passed']
    assert not summarize([{'passed': True, 'revision_id': 'new', 'test_count': 99}], 'new')['checks_passed']
    assert not summarize([{'passed': True, 'revision_id': 'old', 'test_count': 9}], 'new')['checks_passed']
    rows = [{'passed': True, 'revision_id': 'new', 'test_count': 9, 'evidence_version': 3,
             'coverage_observed': True, 'execution_succeeded': True,
             'source_bindings': [{'path': 'calc.py', 'sha256': 'abc'}]},
            {'passed': False, 'revision_id': 'new', 'id': 'regression'}]
    assert not summarize(rows, 'new')['checks_passed']
    result = summarize(rows[:1], 'new')
    assert result['checks_passed'] and not result['accepted']


def test_stop_hook_bounds_repairs_and_reports_concrete_failure(tmp_path, monkeypatch):
    from coder_engine_hook import evaluate
    import coder_profiles
    (tmp_path/'main.py').write_text('print("unfinished")\n')
    monkeypatch.setattr(coder_profiles, 'discover', lambda *args: {'checks':[
        {'id':'behavior', 'phase':'test', 'command':'echo missing-output; exit 1'}]})
    state={'root':str(tmp_path), 'finish_seen':True, 'denials':0,
           'nudges':0, 'command_seconds':10}
    for expected in (1,2):
        deny, reason=evaluate(state)
        assert deny and state['denials']==expected and 'missing-output' in reason
        assert not state['passed']
    deny, reason=evaluate(state)
    assert not deny and state['denials']==2 and 'exhausted' in reason
    assert not state['passed']


def test_verification_feedback_explains_missing_application_evidence():
    result = summarize([{'id':'tests', 'passed':False, 'revision_id':'r',
                         'classification':'missing_provenance', 'log_tail':'All tests passed'}], 'r', ['web'])
    assert not result['checks_passed']
    assert 'actual imported application' in result['failures'][0]['reason']
    assert 'browser test' in result['failures'][0]['reason']
    assert 'DAEDALUS_APP_URL' in result['failures'][0]['reason']
    assert 'fallback only when it is absent' in result['failures'][0]['reason']
    diagnostic = {'id':'interface:labels:app', 'passed':False, 'revision_id':'r', 'optional':True}
    assert not summarize([diagnostic], 'r', ['web'])['checks_passed']
    confirmed = {'id':'ui:0', 'passed':False, 'revision_id':'r', 'origin':'confirmed_interaction'}
    assert not summarize([diagnostic, confirmed], 'r', ['web'])['checks_ok']


def test_interrupted_conversation_retains_its_repair_allowance(tmp_path):
    from coder_engine_runtime import repair_count
    state=tmp_path/'stop.json'
    assert repair_count(state,0)==0
    state.write_text('{"denials":2}')
    assert repair_count(state,0)==2
    state.write_text('{"denials":1}')
    assert repair_count(state,2)==2
    state.write_text('{"denials":-1}')
    with pytest.raises(ValueError,match='repair allowance'):repair_count(state,0)


def test_failed_operation_records_used_hook_repairs(monkeypatch,tmp_path):
    import httpx
    setup(monkeypatch,tmp_path)
    monkeypatch.setenv('DAEDALUS_ENGINE_URL','http://candidate:8587')
    def response(request):
        if request.url.path.endswith('/events'):return httpx.Response(200,json=[])
        return httpx.Response(200,json={'status':'blocked','started':100,'ended':101,'calls':1,
            'result':{'hook_denials':2,'error':'Model-call allowance exhausted'}})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
            coder_jobs.configure(http,None)
            job=await jobs.create('conversation','task','build_from_prompt','','local','failed-repairs',engine='openhands159',policy_version=7)
            job=await jobs.save(job['id'],state='coding',revision_id='a'*40)
            with pytest.raises(RuntimeError,match='Model-call'):
                await coder_jobs._operate(job,'code')
            assert (await jobs.get(job['id']))['repair_round']==2
    asyncio.run(scenario())


def test_preparation_is_separate_but_active_time_and_calls_are_bounded(tmp_path):
    store = WorkerStore(tmp_path)
    store.create('op', 'job', 'code', {'seconds_remaining': 10, 'calls_remaining': 1, '_active_started': time.time()})
    store.update('op', started=time.time()-100)
    _boundary(store, 'op', model_call=True)
    with pytest.raises(TimeoutError, match='Model-call'):
        _boundary(store, 'op', model_call=True)
    payload = store.get('op')['payload']; payload['_active_started'] = time.time()-11
    store.update('op', payload=payload)
    with pytest.raises(TimeoutError, match='Execution'):
        _boundary(store, 'op')


def test_preparation_usage_is_accounted_once(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        job = await jobs.create('conversation', 'task', 'build_from_prompt', '', 'local', 'usage')
        await jobs.save(job['id'], state='coding', worker_operation='op')
        operation = {'status': 'succeeded', 'started': 100, 'ended': 150, 'calls': 3,
                     'payload': {'_preparation_seconds': 20}}
        await jobs.account_operation(job['id'], 'op', 'code', operation)
        result = await jobs.account_operation(job['id'], 'op', 'code', operation)
        assert result['seconds_used'] == 30 and result['calls_used'] == 3
    asyncio.run(scenario())


def test_literal_deliverables_do_not_turn_input_examples_into_files(tmp_path):
    from coder_engine_contract import required_files, checks
    task='Create totals.py. Read input.csv supplied by the caller. Include a README with usage.'
    assert required_files(task)==['README','totals.py']
    (tmp_path/'totals.py').write_text('print(42)\n')
    rows=checks(tmp_path,task,'r')
    assert not next(r for r in rows if r['id']=='deliverable:README')['passed']
    (tmp_path/'README.md').write_text('Run python3 totals.py\n')
    assert all(row['passed'] for row in checks(tmp_path,task,'r'))


def test_call_budget_delivers_an_explicitly_unverified_modern_candidate(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    import coder_engine_controller
    calls=[]
    async def fake_step(job,controller):
        calls.append(job['state'])
        if job['state']=='coding':raise RuntimeError('Model-call allowance exhausted')
        assert job['state']=='candidate_packaging'
        assert not job['verification_summary']['checks_passed']
        assert not job['verification_summary']['accepted']
        return True
    monkeypatch.setattr(coder_engine_controller,'step',fake_step)
    async def scenario():
        job=await jobs.create('conversation','task','build_from_prompt','','local','budget-candidate',engine='openhands159',policy_version=7)
        await jobs.save(job['id'],state='coding',revision_id='a'*40)
        await coder_jobs.run(job['id'])
        assert calls==['coding','candidate_packaging']
    asyncio.run(scenario())
