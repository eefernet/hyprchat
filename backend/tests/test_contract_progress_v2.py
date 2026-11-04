"""Literal provenance, truthful evidence and bounded checklist bookkeeping."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import time
import pytest
from coder_ui_contract import freeze, original_source
from coder_engine_ops import summarize
from coder_plan_finalization import change, state, FinalizationEnded
from .test_engine_recovery import modern, response
from coder_engine_transport import infer
from context_policy import DEFAULTS, resolve

FOLLOWUP = '''Change the Clear button so that clicking “Clear” sets the “Result” display to exactly “Ready” instead of leaving it empty.

Clear must still empty the Task input and validation error while preserving all saved tasks and completion states. Keep adding, completing, deleting, and reload persistence working.

Update the tests for this changed requirement without removing unrelated checks. Verify a real transition: add a task so Result says “Task added,” then click Clear and confirm Result says exactly “Ready.”

Run the regression checks and deliver the updated candidate and preview. Clearly report anything unverified.'''

def test_exact_original_followup():
    contract = freeze(FOLLOWUP)
    supported = [c for c in contract['criteria'] if c['supported']]
    assert {(c['action']['name'], c['target'].get('name'), c['property'], c['expected']) for c in supported} == {
        ('Clear','Result','text','Ready'), ('Clear','Task','value','')}
    assert len({c['id'] for c in contract['criteria']}) == len(contract['criteria'])
    assert not any('tests' in c['request_excerpt'] or 'Verify a real' in c['request_excerpt'] for c in contract['criteria'])
    assert contract['parser_version'] == 3

@pytest.mark.parametrize('quote', [('“','”'),('"','"'),("'","'"),('‘','’'),('`','`')])
@pytest.mark.parametrize('value', ['', '0.00', ' Ready. not!  now ', 'A; B and C', 'never'])
def test_quote_contents_are_exact(quote,value):
    a,b=quote
    rows=freeze(f'- Clicking Clear must now set the Result display to exactly {a}{value}{b}.')['criteria']
    assert len(rows)==1 and rows[0]['supported'] and rows[0]['expected']==value

@pytest.mark.parametrize('text', ['Adding a nonblank task appends it and clears the input.',
    'Reset', 'Clicking Clear does not clear the input.', 'Clicking Clear clears the input if it is invalid.',
    'Clicking Clear clears the input or the result.', 'Clicking Clear sets the result to its default.'])
def test_unsupported_is_not_invented(text):
    assert not any(c['supported'] for c in freeze(text)['criteria'])

def test_independent_clauses_and_preservation():
    rows=freeze('Do not change the Save button; Clicking Clear clears the input without removing or changing saved tasks.')['criteria']
    assert sum(c['supported'] for c in rows)==1
    assert next(c for c in rows if c['supported'])['expected']==''
    assert len(rows)==3

def test_replacement_retains_history_and_other_requirements():
    old=freeze('Clicking Clear clears the Result display and Task input.')
    new=freeze(FOLLOWUP,{'ui_contract':old})
    assert any(c['expected']=='' and c['target'].get('name')=='Result' for c in new['history'])
    assert next(c for c in new['criteria'] if c.get('target',{}).get('name')=='Result')['expected']=='Ready'
    assert old['history']==[]

def test_upgrade_uses_original_history_only():
    original={'id':'old','mode':'edit_project','original_user_request':'Clicking Clear sets the Result display to “Ready”.',
        'user_task':'Clicking Clear sets the Result display to “Wrong”.', 'ui_contract':freeze('Clicking Clear clears the result.')}
    original['ui_contract'].pop('parser_version')
    saved=deepcopy(original)
    result=asyncio.run(original_source(original,lambda _: None))
    assert result['ui_contract']['criteria'][0]['expected']=='Ready' and original==saved
    assert result['ui_contract']['legacy_contracts'][0]['contract']==saved['ui_contract']
    original.pop('original_user_request')
    result=asyncio.run(original_source(original,lambda _: None))
    assert all(not c['supported'] for c in result['ui_contract']['criteria'])


def test_new_build_freezes_clear_before_any_generated_tests():
    request = 'Build a web app. Clicking Clear must empty the Task input, Result display, and validation error without removing or changing saved tasks.'
    specification = freeze(request, mode='build_from_prompt')
    supported = [c for c in specification['criteria'] if c['supported']]
    assert {(c['target']['name'], c['property'], c['expected']) for c in supported} == {
        ('Task', 'value', ''), ('Result', 'text', '')}
    assert all(c['provenance']['kind'] == 'user_request' for c in supported)
    # Unsupported preservation/error wording remains visible, never a pass.
    assert any(not c['supported'] for c in specification['criteria'])


def test_v2_initial_contract_is_rebuilt_only_for_new_followup():
    root = {'id': 'build', 'mode': 'build_from_prompt',
        'original_user_request': 'Clicking Clear clears the Task input and Result display.',
        'ui_contract': {'version': 1, 'parser_version': 2, 'criteria': [], 'history': []}}
    edit = {'id': 'edit', 'source_job_id': 'build', 'mode': 'edit_project',
        'original_user_request': 'Clicking Clear sets the Result display to exactly “Ready”.',
        'ui_contract': {'version': 1, 'parser_version': 2, 'criteria': [], 'history': []}}
    saved = deepcopy([root, edit])
    async def load(identity):
        assert identity == 'build'
        return root
    upgraded = asyncio.run(original_source(edit, load))
    assert [root, edit] == saved
    criteria = upgraded['ui_contract']['criteria']
    assert {(c['target']['name'], c['expected']) for c in criteria} == {('Task', ''), ('Result', 'Ready')}
    assert any(c.get('expected') == '' for c in upgraded['ui_contract']['history'])
    assert len(upgraded['ui_contract']['legacy_contracts']) == 2
    async def forbidden(_):
        raise AssertionError('Current frozen contracts must not be reparsed')
    assert asyncio.run(original_source(upgraded, forbidden)) is upgraded

def test_advisory_never_exercises_and_latest_current_result_wins():
    row={'id':'workflow:x','revision_id':'r','passed':True,'workflow_status':'advisory',
         'execution_succeeded':True,'evidence_version':3,'coverage_observed':True,'source_bindings':['app']}
    saved=deepcopy(row)
    result=summarize([row], 'r')
    assert not result['exercised'] and not result['checks_passed'] and not result['failures']
    assert result['check_counts']=={'passed':0,'advisory':1,'failed':0,'skipped':0,'unverified':0}
    assert row==saved
    failed={**row,'workflow_status':'failed','passed':False}
    result=summarize([row,failed,{**row,'revision_id':'stale'}],'r')
    assert result['check_counts']['failed']==1 and result['check_counts']['advisory']==0


def test_historical_presentation_counts_without_changing_evidence():
    from coder_presentation import presentation
    job={'engine':'openhands159','state':'ready_for_review','revision_id':'r',
         'verification_summary':{'checks_passed':True},
         'checks':[{'id':'probe','revision_id':'r','workflow_status':'advisory','passed':True}]}
    saved=deepcopy(job)
    result=presentation(job)
    assert result['phase']!='Checks passed — please review'
    assert result['check_counts']['advisory']==1 and result['check_counts']['passed']==0
    assert job==saved


def test_exercised_candidate_with_unverified_requirements_is_ready_for_review():
    from coder_presentation import presentation
    job = {'engine': 'openhands159', 'state': 'ready_for_review', 'revision_id': 'current',
        'ui_contract_version': 1, 'candidate_artifact': {'id': 'bundle', 'metadata': {'revision_id': 'current'}},
        'verification_summary': {'revision_id': 'current', 'checks_ok': True, 'exercised': True,
            'checks_passed': False, 'failures': [], 'criteria': [{'status': 'unverified'}]}}
    saved = deepcopy(job)
    assert presentation(job)['phase'] == 'Ready for review'
    assert presentation(job)['actions']['accept']['enabled']
    assert job == saved
    job['verification_summary']['revision_id'] = 'stale'
    assert presentation(job)['phase'] == 'Build incomplete'
    job['verification_summary'].update(revision_id='current', failures=[{'id': 'build'}], checks_ok=False)
    assert presentation(job)['phase'] == 'Build incomplete'

def finalizer(tmp_path,monkeypatch):
    store=modern(tmp_path,monkeypatch)
    store.update('op',payload={**store.get('op')['payload'],'plan_finalization_version':1})
    store.event('op','model_response',tool_names=['finish'],authority='supervisor')
    return store

def test_once_per_request_survives_new_operation(tmp_path,monkeypatch):
    store=finalizer(tmp_path,monkeypatch)
    assert change(store,'op','begin')['allowed']
    assert not change(store,'op','begin')['allowed']
    operation=store.get('op')
    store.create('next',operation['job_id'],'code',operation['payload'])
    store.update('next',status='running',started=time.time())
    assert not change(store,'next','begin')['allowed']
    change(store,'op','acknowledge',[{'title':'Implemented','status':'done'}])
    assert state(store,'op')['acknowledged_plan'][0]['status']=='done'
    count=len(store.events('op'))
    change(store,'op','acknowledge',[{'title':'Implemented','status':'done'}])
    assert len(store.events('op'))==count

def test_finalization_two_turns_and_no_other_tool_dispatch(tmp_path,monkeypatch):
    store=finalizer(tmp_path,monkeypatch);change(store,'op','begin')
    body={'options':{},'messages':[], 'tools':[{'function':{'name':name}} for name in ('task_tracker','finish','terminal')]}
    seen=[]
    def send(_,request):
        seen.append(request)
        return response('task_tracker')
    for _ in range(2):infer(store,'op',body,'builder',resolve('builder',DEFAULTS),send)
    with pytest.raises(FinalizationEnded):infer(store,'op',body,'builder',resolve('builder',DEFAULTS),send)
    assert store.get('op')['calls']==2 and len(seen)==2
    assert all(t['function']['name']!='terminal' for t in seen[0]['tools'])


def test_finalization_rejects_file_action_and_deadline(tmp_path,monkeypatch):
    store=finalizer(tmp_path,monkeypatch);change(store,'op','begin')
    body={'options':{},'messages':[], 'tools':[{'function':{'name':'finish'}},{'function':{'name':'terminal'}}]}
    with pytest.raises(FinalizationEnded):
        infer(store,'op',body,'builder',resolve('builder',DEFAULTS),lambda *_:response('terminal'))
    assert store.get('op')['calls']==1
    payload=store.get('op')['payload'];payload['plan_finalization']['deadline']=time.time()-1;store.update('op',payload=payload)
    with pytest.raises(FinalizationEnded):change(store,'op','turn')

def test_request_provenance_idempotency_and_restart(monkeypatch,tmp_path):
    import coder_jobs
    from db import coder_jobs as jobs
    from .test_daedalus_versions import setup
    setup(monkeypatch,tmp_path);monkeypatch.setenv('DAEDALUS_ENGINE_URL','http://candidate:8589')
    async def scenario():
        first=await coder_jobs.create('conversation','Clicking Clear sets the result to "Wrong".',model='local',engine='openhands159',key='original',original_request=FOLLOWUP)
        assert first['ui_preflight_version']==1
        assert first['original_user_request']==FOLLOWUP and first['ui_contract']['parser_version']==3
        assert {c['expected'] for c in first['ui_contract']['criteria'] if c['supported']} == {'Ready', ''}
        # Literal storage remains authoritative even when model task disagrees.
        assert first['ui_contract']['entrypoint_task']==FOLLOWUP
        again=await coder_jobs.create('conversation',first['user_task'],model='local',engine='openhands159',key='original',original_request=FOLLOWUP)
        assert first['ui_contract']==again['ui_contract'] and first['id']==again['id']
        old=await jobs.create('conversation','old','build_from_prompt','','local','legacy-parser',engine='openhands159',policy_version=7,
            request_options={'original_user_request':'old','ui_contract_version':1,'ui_contract_context':[],'ui_contract':{'version':1,'criteria':[]},'engine_behavior_version':1})
        repeat=await jobs.create('conversation','old','build_from_prompt','','local','legacy-parser',engine='openhands159',policy_version=7,
            request_options={'original_user_request':'old','ui_contract_version':1,'ui_contract_context':[],'ui_contract':freeze('old'),'engine_behavior_version':1,'plan_finalization_version':1,'ui_preflight_version':1})
        assert repeat['ui_contract']==old['ui_contract'] and not repeat.get('plan_finalization_version') and not repeat.get('ui_preflight_version')
    asyncio.run(scenario())

def test_hook_reminder_does_not_spend_repair_and_missing_plan_skips(tmp_path,monkeypatch):
    from coder_engine_hook import evaluate
    root=tmp_path/'project';root.mkdir();(root/'README.md').write_text('hello')
    calls=[];monkeypatch.setattr('coder_plan_finalization.request',lambda *args:calls.append(args) or {'allowed':True})
    state={'root':str(root),'finish_seen':True,'denials':1,'nudges':0,'command_seconds':1,'bridge_url':'test','plan_finalization_version':1,'plan':[{'title':'work','status':'todo'}]}
    assert evaluate(state)[0] and state['denials']==1 and state['finalizing']
    assert not evaluate(state)[0] and state['denials']==1 and len(calls)==2
    state.pop('finalizing');state['plan']=[]
    assert not evaluate(state)[0] and len(calls)==2


def test_finalization_cancel_and_insufficient_allowance(tmp_path,monkeypatch):
    store=finalizer(tmp_path,monkeypatch)
    payload=store.get('op')['payload'];payload['seconds_remaining']=10;store.update('op',payload=payload)
    assert not change(store,'op','begin')['allowed']
    store.update('op',cancel_requested=True)
    with pytest.raises(InterruptedError):change(store,'op','begin')


def test_headings_conflicting_values_and_reaffirmation():
    contract=freeze('## UI behavior\n**Tests**\nClicking Clear sets the result to "Ready". Clicking Clear sets the result to "Done".')
    assert len(contract['criteria'])==2 and all(not c['supported'] for c in contract['criteria'])
    first=freeze('Clicking Clear clears the Task input.')
    new=freeze('Clear must still empty the Task input.',{'ui_contract':first})
    assert new['criteria']==first['criteria']
    assert new['history'][0]['kind']=='reaffirmed' and 'superseded_by' not in new['history'][0]

def test_checklist_regeneration_is_charged_inside_two_turn_limit(tmp_path,monkeypatch):
    store=finalizer(tmp_path,monkeypatch);change(store,'op','begin')
    body={'options':{},'messages':[], 'tools':[{'function':{'name':'task_tracker'}},{'function':{'name':'finish'}}]}
    answers=iter([response(),response('task_tracker'),response('finish')])
    for _ in range(2):infer(store,'op',body,'builder',resolve('builder',DEFAULTS),lambda *_:next(answers))
    assert state(store,'op')['turns']==2 and store.get('op')['calls']==3
    assert len([e for e in store.events('op') if e['type']=='response_recovery'])==1


def test_reminder_cannot_begin_before_normal_finish(tmp_path,monkeypatch):
    store=modern(tmp_path,monkeypatch)
    store.update('op',payload={**store.get('op')['payload'],'plan_finalization_version':1})
    assert change(store,'op','begin')['status']=='no_normal_finish'
    assert not state(store,'op')


def test_numbered_markdown_bullet_is_one_clause():
    rows=freeze('1. Clicking Clear must empty the Task input.')['criteria']
    assert len(rows)==1 and rows[0]['supported'] and rows[0]['expected']==''


def test_rewritten_task_cannot_choose_target_identity():
    spec=freeze('Clicking Clear clears the result.',{'user_task':'Create output id fabricated.'})
    assert 'source_selector' not in spec['criteria'][0]['target']


def test_input_setup_reuses_only_observed_fill_on_exact_control():
    from coder_ui_contract import seed_inputs
    node={'selector':'#task','editable':True,'name':'Task'}
    calls=[]
    def action(request):
        calls.append(request)
        return {'value':'known task'}
    setup=[{'action':'fill','selector':'#task','value':'known task'}, {'action':'click','selector':'#add'}]
    assert seed_inputs(action,[node],[node],[''],'',setup)==['known task']
    assert setup[-1]=={'action':'fill','selector':'#task','value':'known task'}
    assert len(calls)==2


def test_input_setup_does_not_invent_data_or_select_ambiguous_control():
    from coder_ui_contract import seed_inputs
    node={'selector':'#task','editable':True,'name':'Task'}
    def forbidden(request):
        pytest.fail('Ambiguous or unknown input must not be filled')
    assert seed_inputs(forbidden,[node],[node],[''],'',[])==['']
    duplicate={**node,'selector':'#second'}
    assert seed_inputs(forbidden,[node,duplicate],[node],[''],'',[{'action':'fill','name':'Task','value':'known task'}])==['']


def test_deferred_verification_does_not_skip_checklist_or_claim_checks_passed(monkeypatch):
    from coder_engine_hook import defer_verification, evaluate
    calls=[]
    monkeypatch.setattr('coder_plan_finalization.request',lambda *args:calls.append(args) or {'allowed':True})
    state={'finish_seen':True,'denials':0,'plan_finalization_version':1,'bridge_url':'test',
           'plan':[{'title':'Owner review','status':'todo'}]}
    assert defer_verification(state,'Independent preparation required')[0]
    assert state['passed'] is False and state['denials']==0
    assert evaluate(state)==(False,'Independent preparation required')
    assert calls==[('test','begin'),('test','end')]
