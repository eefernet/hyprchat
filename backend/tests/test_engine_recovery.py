"""Behavior-v1 recovery invariants, independent of generated application claims."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import pytest

from coder_engine_transport import infer, TransportFailure
from context_policy import DEFAULTS, resolve
from .test_engine_reliability import operation


def modern(tmp_path, monkeypatch):
    monkeypatch.setattr('coder_engine_tokens.count_request', lambda b: 20 + len(b.get('messages', [])))
    store = operation(tmp_path)
    store.update('op', payload={**store.get('op')['payload'], 'engine_behavior_version':1})
    return store


def body():
    return {'options':{}, 'messages':[{'role':'user','content':'Use write.'}],
            'tools':[{'type':'function','function':{'name':'write'}}]}


def response(name=None, reason='stop'):
    return {'done':True, 'done_reason':reason, 'prompt_eval_count':21, 'eval_count':16384 if reason=='length' else 12,
            'message':{'content':'done', 'tool_calls':[] if name is None else [{'function':{'name':name,'arguments':{}}}]}}


@pytest.mark.parametrize('bad,category', [(response(),'missing_required_tool'),
    (response('undeclared'),'malformed_tool_output'),(response('write','length'),'output_limit')])
def test_regeneration_discards_bad_response_and_charges_same_allowance(tmp_path,monkeypatch,bad,category):
    store=modern(tmp_path,monkeypatch); requests=[]; original=body()
    def send(current, request):
        requests.append(deepcopy(request))
        return bad if len(requests)==1 else response('write')
    assert infer(store,'op',original,'builder',resolve('builder',DEFAULTS),send)==response('write')
    assert original==body() and len(requests)==2 and store.get('op')['calls']==2
    assert len(requests[1]['messages'])==2 and 'Controller response recovery' in requests[1]['messages'][-1]['content']
    events=store.events('op'); failure=next(e['data'] for e in events if e['type']=='model_failure')
    assert failure['category']==category and failure['upstream_status']==200 and failure['bridge_status']==502
    calls=[e['data'] for e in events if e['type']=='model_call']
    assert [c['prompt_tokens_estimate'] for c in calls]==[21,22]
    assert calls[0]['request_id']==calls[1]['request_id'] and calls[0]['request_sha256']!=calls[1]['request_sha256']
    success=next(e['data'] for e in events if e['type']=='model_response')
    assert success['recovered'] and not success['first_attempt_passed']


def test_shared_retry_limit_across_failure_categories(tmp_path,monkeypatch):
    store=modern(tmp_path,monkeypatch); answers=iter([response(),response('write','length')])
    with pytest.raises(TransportFailure) as caught:
        infer(store,'op',body(),'builder',resolve('builder',DEFAULTS),lambda *_:next(answers))
    assert caught.value.category=='output_limit' and store.get('op')['calls']==2


def test_augmented_retry_overflow_is_not_sent(tmp_path,monkeypatch):
    store=modern(tmp_path,monkeypatch)
    monkeypatch.setattr('coder_engine_tokens.count_request',lambda b:100000 if len(b['messages'])>1 else 20)
    with pytest.raises(TransportFailure) as caught:
        infer(store,'op',body(),'builder',resolve('builder',DEFAULTS),lambda *_:response())
    assert caught.value.category=='context_overflow' and store.get('op')['calls']==1


def test_text_phase_and_cancel_do_not_regenerate(tmp_path,monkeypatch):
    store=modern(tmp_path,monkeypatch)
    assert infer(store,'op',body(),'compaction',resolve('builder',DEFAULTS),lambda *_:response())==response()
    def cancel(*_): raise InterruptedError('Stop')
    with pytest.raises(InterruptedError):infer(store,'op',body(),'builder',resolve('builder',DEFAULTS),cancel)
    assert store.get('op')['calls']==2


def controls():
    return [({'id':'desktop/1','name':'Celsius','role':'text','showing':True,'editable':True,'activatable':False,'text':''},None),
            ({'id':'desktop/2','name':'Convert','role':'push button','showing':True,'editable':False,'activatable':True},None)]


@pytest.mark.parametrize('locator,category', [({'action':'fill','name':'celsius_input','role':'lineedit'},'target_not_found'),
    ({'action':'fill','name':'Celsius','role':'lineedit'},'target_not_found'),
    ({'action':'fill','name':'Convert'},'unsupported_action'),({'action':'press','name':'Celsius'},'unsupported_action'),
    ({'action':'fill','selector':'desktop/1','name':'Wrong'},'target_not_found')])
def test_desktop_diagnostic_has_observed_locators(locator,category):
    from coder_accessibility import resolve_target, AccessibleFailure
    with pytest.raises(AccessibleFailure) as caught:resolve_target({**locator,'_behavior_version':1},controls())
    d=caught.value.details
    assert d['category']==category and d['requested']==locator
    assert {'name':'Celsius','role':'text'} in [c.get('locator') for c in d['candidates']]
    assert next(c for c in d['candidates'] if c['name']=='Celsius')['editable'] is True


def test_duplicate_targets_are_not_arbitrarily_selected():
    from coder_accessibility import resolve_target, AccessibleFailure
    nodes=controls();nodes.append(({**nodes[0][0],'id':'desktop/3'},None))
    with pytest.raises(AccessibleFailure) as caught:resolve_target({'action':'value','name':'Celsius','_behavior_version':1},nodes)
    assert caught.value.details['category']=='ambiguous_target'
    assert not any(c.get('locator') for c in caught.value.details['candidates'] if c['name']=='Celsius')


def test_failed_launch_cleans_session_and_old_node_ids_are_rejected(tmp_path,monkeypatch):
    from coder_ui_session import UISession, UIFailure
    session=object.__new__(UISession)
    session.behavior_version=1;session.session_id='old';session.kind=None;session.processes=[]
    session.application_processes=[];session.browser=session.playwright=session.page=None;session.env={}
    session.directory=tmp_path
    def fail(request):session.kind='desktop';raise RuntimeError('startup traceback')
    monkeypatch.setattr(session,'_perform',fail)
    with pytest.raises(UIFailure) as caught:session.perform({'action':'launch','kind':'desktop','command':'broken'})
    assert caught.value.details['category']=='application_startup' and session.kind is None
    def good(request):session.kind='desktop';return {'nodes':[]}
    monkeypatch.setattr(session,'_perform',good)
    launched=session.perform({'action':'launch','kind':'desktop','command':'fixed'})
    assert launched['session_id']!='old'
    with pytest.raises(UIFailure) as caught:session.perform({'action':'value','selector':'desktop/1','session_id':'old'})
    assert caught.value.details['category']=='stale_session'


def test_persisted_desktop_flows_reject_session_references():
    from coder_ui_checks import validate
    flow=[{'kind':'desktop','command':'app','steps':[{'action':'text','selector':'desktop/1','equals':'42'}]}]
    assert validate(flow)==flow
    with pytest.raises(ValueError,match='session-local'):validate(flow,1)


@pytest.mark.parametrize('check_error,package_error', [(False,False),(True,False),(False,True)])
def test_generation_checkpoint_recovery_never_codes_or_accepts(monkeypatch,tmp_path,check_error,package_error):
    from .test_daedalus_jobs import setup
    from db import coder_jobs as jobs
    from coder_engine_recovery import recover_candidate
    import coder_jobs
    setup(monkeypatch,tmp_path);calls=[]
    async def operate(job,kind,**kwargs):
        calls.append(kind)
        if kind=='check':
            if check_error:raise TimeoutError('Verification deadline')
            return {'checks':[], 'summary':{'revision_id':'new','checks_ok':True,'checks_passed':True,'failures':[]},'execution_profile':{}},job
        assert kind=='package' and kwargs['candidate']
        if package_error:raise RuntimeError('Artifact storage unavailable')
        assert kwargs['verification_summary']['revision_id']=='new'
        assert not kwargs['verification_summary']['checks_passed']
        return {},job
    async def deliver(job,result,**kwargs):
        await jobs.save(job['id'],state='ready_for_review',candidate_revision='new')
        return {}
    monkeypatch.setattr(coder_jobs,'_operate',operate);monkeypatch.setattr(coder_jobs,'_deliver',deliver)
    monkeypatch.setattr(coder_jobs,'_EVENTS',None)
    async def run():
        job=await jobs.create('conversation','task','build_from_prompt','','local','recover',engine='openhands159',policy_version=8)
        job=await jobs.save(job['id'],state='coding',engine_behavior_version=1,revision_id='new',worker_operation='op',
            repair_round=2,accepted_revision='accepted',checks=[{'revision_id':'old','passed':True}],
            failure={'category':'output_limit'},failed_checkpoint={'operation_id':'op','revision_id':'new','inventory':{'files':1}})
        assert await recover_candidate(job,coder_jobs,RuntimeError('generation failed'))
        saved=await jobs.get(job['id'])
        assert calls==['check','package'] and saved['accepted_revision']=='accepted' and saved['repair_round']==2
        assert saved['stop_limit']=='application_repairs' and saved['verification_summary']['incomplete_reason']
        assert saved['checks']==[] and saved['state']==('blocked' if package_error else 'ready_for_review')
        with pytest.raises(ValueError):await coder_jobs.resume(job['id'],budget_mode='new_allowance')
    asyncio.run(run())


@pytest.mark.parametrize('recorded_version',[None,0])
def test_old_job_creation_replay_keeps_behavior_after_upgrade(monkeypatch,tmp_path,recorded_version):
    from .test_daedalus_jobs import setup
    from db import coder_jobs as jobs
    setup(monkeypatch,tmp_path)
    async def run():
        options={'execution_commands':{}}
        if recorded_version is not None:options['engine_behavior_version']=recorded_version
        old=await jobs.create('conversation','task','build_from_prompt','','local','upgrade-replay',engine='openhands159',policy_version=7,request_options=options)
        again=await jobs.create('conversation','task','build_from_prompt','','local','upgrade-replay',engine='openhands159',policy_version=7,request_options={**options,'engine_behavior_version':1})
        assert old['id']==again['id'] and again['engine_behavior_version']==0
        with pytest.raises(ValueError,match='Idempotency'):
            await jobs.create('conversation','changed task','build_from_prompt','','local','upgrade-replay',engine='openhands159',policy_version=7,request_options={**options,'engine_behavior_version':1})
    asyncio.run(run())
