"""Recorded verifier failure mechanisms and persistent policy-5 boundaries."""
import copy
import hashlib
import json
import time

import jsonschema
import pytest

from coder_check_schema import probe_schema
from coder_contracts import inspection_schema, normalize_probe, run_contract, targeted_correction
from coder_repository import Repository
from coder_worker_runtime import WorkerStore, _read_only, _run_check
from coder_verification import probe_checks
from context_policy import DEFAULTS


def file_probe(**changes):
    return {'runner':'file','cwd':'.','path':'README.md','expected_behavior':'README exists',
        'assertions':[{'kind':'nonempty'}], **changes}


def api_probe(**changes):
    return {'runner':'api','cwd':'.','expected_behavior':'Return the requested list',
        'target':{'language':'python','path':'subject.py','export':'subject'},
        'cases':[{'args':[], 'expect':{'kind':'equal','value':[1,2]},'preserve_inputs':True}], **changes}


def environment(tmp_path, **extra):
    project=tmp_path/'project';project.mkdir(exist_ok=True)
    repo=Repository(project,tmp_path/'repo');repo.refresh()
    store=WorkerStore(tmp_path/'worker')
    payload={'policy_version':5,'task':'Add README.','original_task':'Add README.',
        'requirements':[{'id':'r1','kind':'documentation','text':'Add README.','source_refs':['request-1']}],
        'settings':dict(DEFAULTS),'seconds_remaining':60,'calls_remaining':20, **extra}
    store.create('op','job','verify',payload);store.update('op',started=time.time())
    return repo,store,payload


@pytest.mark.parametrize('value', [None,False,0,2.5,'text',[1,False,None],{'rows':[[1,2]],'ok':True}])
def test_recursive_schema_preserves_real_json_values(value):
    probe=api_probe(cases=[{'args':[value], 'expect':{'kind':'equal','value':value}, 'preserve_inputs':True}])
    jsonschema.validate(probe,inspection_schema(probe_schema()))
    normalized=normalize_probe(probe,{'id':'r1','kind':'behavior'},'c',policy_version=5)
    assert normalized['cases'][0]['expect']['value']==value


@pytest.mark.parametrize('probe', [file_probe(target={}),api_probe(path='README.md'),
    {k:v for k,v in api_probe().items() if k!='cases'}, file_probe(assertions=[{'kind':'unchanged','sha256':'invented'}])])
def test_runner_schemas_reject_foreign_missing_and_model_hash_fields(probe):
    with pytest.raises(jsonschema.ValidationError): jsonschema.validate(probe,inspection_schema(probe_schema()))


def test_readme_binding_and_behavior_cannot_be_file_assertions():
    with pytest.raises(ValueError,match='README itself'):
        normalize_probe(file_probe(path='subject.py'),{'id':'r1','kind':'documentation','text':'Add README'},'c',5)
    with pytest.raises(ValueError,match='runtime behavior'):
        normalize_probe(file_probe(),{'id':'r1','kind':'behavior'},'c',5)


def test_controller_file_hashes_are_from_the_revision_and_baseline(tmp_path):
    repo,store,payload=environment(tmp_path)
    (repo.root/'README.md').write_text('Original usage')
    baseline=repo.snapshot()['revision']
    (repo.root/'README.md').write_text('Changed usage')
    revision=repo.snapshot(parent=baseline)['revision']
    (repo.root/'README.md').write_text('Original usage')  # mutable workspace cannot forge the evidence
    check=normalize_probe(file_probe(assertions=[{'kind':'unchanged'}]),{'id':'r1','kind':'preservation'},'c',5)
    payload.update(revision_id=revision,baseline_revision=baseline,checks=probe_checks([check]))
    result=_run_check(store,'op',repo,payload)
    assert not result['passed']
    evidence=result['checks'][0]
    assert evidence['source_bindings'][0]['sha256']==hashlib.sha256(b'Changed usage').hexdigest()
    assert evidence['assertions'][0]['baseline_sha256']==hashlib.sha256(b'Original usage').hexdigest()


@pytest.mark.parametrize('path',['../README.md','/tmp/README.md','a\\README.md'])
def test_file_paths_cannot_escape_project(path):
    with pytest.raises(ValueError): normalize_probe(file_probe(path=path),{'id':'r1','kind':'documentation'},'c',5)


def test_explanation_only_replacement_is_not_a_correction():
    requirement={'id':'r1','kind':'behavior','source_refs':['request-1']}
    old=normalize_probe(api_probe(),requirement,'c',5)
    diagnosis={'disposition':'probe_defect',**{k:'Evidence' for k in ('reason','request_basis','source_basis','failure_basis')}}
    with pytest.raises(ValueError,match='executable assertions'):
        targeted_correction({**diagnosis,'replacement':api_probe(expected_behavior='Corrected explanation')},
            old,requirement,{'request-1':'Return a list'},5)
    replacement=api_probe();replacement['cases'][0]['expect']['value']=[3,4]
    assert targeted_correction({**diagnosis,'replacement':replacement},old,requirement,{'request-1':'Return a list'},5)[0]['cases']==replacement['cases']


@pytest.mark.parametrize('runner,program,explained', [
    ('python', 'assert actual == [1, 2], "old message"', '\"Corrected check\"\nassert actual == [1, 2], "new message" # fixed'),
    ('node', 'assert.deepStrictEqual(actual, [1, 2]);', '// Corrected check\nassert.deepStrictEqual( actual, [1, 2] ); /* fixed */'),
    ('shell', 'test "$actual" = expected', '# Corrected check\ntest "$actual" = expected # fixed'),
])
def test_raw_explanations_do_not_change_executable_identity(runner, program, explained):
    from coder_check_schema import executable_identity
    check={'runner':runner,'cwd':'.','program':program,'bindings':[]}
    assert executable_identity(check)==executable_identity({**check,'program':explained})


@pytest.mark.parametrize('runner,program,replacement', [
    ('node', 'assert.equal(actual, "https://old");', 'assert.equal(actual, "https://new");'),
    ('node', 'assert.match(actual, /[/*]old/);', 'assert.match(actual, /[/*]new/);'),
    ('node', 'assert.equal(actual, `/*old*/`);', 'assert.equal(actual, `/*new*/`);'),
    ('shell', 'test "$actual" = "#old"', 'test "$actual" = "#new"'),
])
def test_comment_markers_in_fixture_data_remain_executable(runner, program, replacement):
    from coder_check_schema import executable_identity
    check={'runner':runner,'cwd':'.','program':program,'bindings':[]}
    assert executable_identity(check)!=executable_identity({**check,'program':replacement})


def test_raw_replacement_review_compares_actual_assertions():
    from coder_contracts import review_raw_probe
    class Store:
        def event(self, *args, **kwargs): pass
    previous={'id':'c','requirement_ids':['r'],'program':'assert old'}
    proposed={**previous,'program':'assert old # fixed'}
    def read_only(store, op, repo, instruction, context, validate, schema):
        assert context['previous_probe']==previous and context['candidate_probe']==proposed
        assert 'diagnostic messages' in instruction and 'unrelated assertions' in instruction
        verdict={'valid':False,'reason':'The failed assertion remains unchanged'}
        validate(verdict)
        return verdict
    verdict=review_raw_probe(Store(),'op',None,read_only,proposed,
        {'id':'r','kind':'behavior','source_refs':['request']},{'request':'Check the output'},
        return_negative=True,previous=previous)
    assert not verdict['valid']


def test_recorded_pagination_wrappers_fail_and_plain_arrays_pass(tmp_path):
    # policy4-r2, cw3-a88d1b5e56d9719b40515c56: the diagnosis described
    # removing this object wrapper but returned it unchanged three times.
    from coder_api_probe import command
    import subprocess
    (tmp_path/'pagination.py').write_text('def paginate(items,page,page_size):\n    return list(items[(page-1)*page_size:page*page_size])\n')
    probe=api_probe(target={'language':'python','path':'pagination.py','export':'paginate'},
        cases=[{'args':[[1,2,3,4,5],1,2],'expect':{'kind':'equal','value':{'items':[1,2]}},'preserve_inputs':False}])
    result=subprocess.run(command(probe),shell=True,cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode and 'Type mismatch' in result.stderr
    probe['cases'][0]['expect']['value']=[1,2]
    assert subprocess.run(command(probe),shell=True,cwd=tmp_path,capture_output=True).returncode==0


def test_recorded_tags_readme_cannot_bind_to_python_subject():
    # policy4-r2, cw3-4f89167407a501ac5871501f: an uncorrected binding.
    probe={'runner':'shell','cwd':'.','expected_behavior':'README.md exists and contains documentation for the normalize_tags function',
        'program':"test -f README.md && grep -qi 'normalize_tags' README.md; exit $?",'bindings':[{'path':'normalizer.py'}]}
    with pytest.raises(ValueError,match='Documentation must use'):
        normalize_probe(probe,{'id':'r1','kind':'documentation','text':'Add README'},'c',5)


def test_format_modes_and_limits_survive_restart_and_continue(tmp_path,monkeypatch):
    import coder_inference
    repo,store,payload=environment(tmp_path)
    modes=[]
    def chat(s,op,*args,**kwargs):
        modes.append(s.get(op)['payload']['structured_mode'])
        return json.dumps(file_probe(path='subject.py'))  # syntactically valid, wrong README binding
    monkeypatch.setattr(coder_inference,'local_chat',chat)
    for op in ('op','continued'):
        if op!='op':
            store=WorkerStore(store.root);store.create(op,'job','verify',payload);store.update(op,started=time.time())
        with pytest.raises(ValueError,match='recovery exhausted'):
            run_contract(store,op,repo,_read_only)
    assert modes==['schema','schema','json','text']
    events=[e for e in store.events('op') if e['type']=='response_format_recovery']
    assert len(events)==4 and events[-1]['data']['next_mode']=='exhausted'


def test_diagnosis_saved_before_replacement_and_survives_interruption(tmp_path,monkeypatch):
    import coder_inference
    req={'id':'r1','kind':'behavior','text':'Return a list','source_refs':['request-1']}
    old=normalize_probe(api_probe(),req,'check-r1',5)
    repo,store,payload=environment(tmp_path,task='Return a list',original_task='Return a list',requirements=[req],
        diagnose_probes={'checks':[old]},evidence={'audit_key':{'check_id':'check-r1','round':1,'failure_hash':'failure'},
            'failed_checks':[{'id':'requirement-check-r1','passed':False,'log':'first.log'}]})
    calls=[]
    diagnosis={'disposition':'probe_defect',**{k:'The expected array has an extra wrapper' for k in ('reason','request_basis','source_basis','failure_basis')}}
    replacement=api_probe();replacement['cases'][0]['expect']['value']=[3,4]
    def chat(s,op,*args,**kwargs):
        calls.append(s.get(op)['payload']['structured_mode'])
        if len(calls)==1: return json.dumps(diagnosis)
        assert any(e['type']=='probe_diagnosis' for e in s.events('op'))
        if len(calls)==2: raise RuntimeError('worker interruption')
        return json.dumps(replacement)
    monkeypatch.setattr(coder_inference,'local_chat',chat)
    with pytest.raises(RuntimeError): run_contract(store,'op',repo,_read_only)
    store=WorkerStore(store.root)
    continued=copy.deepcopy(payload)
    continued['evidence']['failed_checks'][0].update(log='continued.log',reused=True,seconds=0)
    store.create('continued','job','verify',continued);store.update('continued',started=time.time())
    result=run_contract(store,'continued',repo,_read_only)
    assert len(result['audits'])==1 and result['checks'][0]['cases']==replacement['cases']
    assert len(calls)==3


def test_partial_requirement_progress_does_not_reset_format_limits(tmp_path,monkeypatch):
    import coder_inference
    repo,store,payload=environment(tmp_path)
    store.create('plan','job','plan',payload);store.update('plan',started=time.time())
    modes=[]
    def chat(s,op,*args,**kwargs):
        modes.append(s.get(op)['payload']['structured_mode'])
        if len(modes)==2: raise RuntimeError('worker interruption')
        return json.dumps({'requirements':[
            {'slot':1,'text':'Add README.','kind':'documentation','source_refs':['request-1']},
            {'slot':2,'text':'Invalid requirement','kind':'invented','source_refs':['request-1']}]})
    monkeypatch.setattr(coder_inference,'local_chat',chat)
    with pytest.raises(RuntimeError): run_contract(store,'plan',repo,_read_only)
    draft=json.loads(next((store.root/'jobs/job').glob('contract-*.json')).read_text())
    assert draft['valid_requirements']
    store=WorkerStore(store.root)
    store.create('continued','job','plan',payload);store.update('continued',started=time.time())
    with pytest.raises(ValueError,match='recovery exhausted'): run_contract(store,'continued',repo,_read_only)
    assert modes==['schema','schema','json','text']
    assert len(list((store.root/'jobs/job').glob('contract-*.json')))==1


def test_recovery_identity_keeps_distinct_audits_and_revisions():
    from coder_response_recovery import contract_identity
    operation={'kind':'verify','payload':{'task':'Task','revision_id':'a',
        'evidence':{'audit_key':{'check_id':'r1','round':1},'failed_checks':[{'log':'old','reused':False}]}}}
    continued=copy.deepcopy(operation)
    continued['payload']['evidence']['failed_checks']=[{'log':'new','reused':True}]
    assert contract_identity(operation)==contract_identity(continued)
    continued['payload']['evidence']['audit_key']['round']=2
    assert contract_identity(operation)!=contract_identity(continued)
    continued=copy.deepcopy(operation);continued['payload']['revision_id']='b'
    assert contract_identity(operation)!=contract_identity(continued)


@pytest.mark.parametrize('truncations',[2,4])
def test_truncated_schema_responses_recover_without_raising_output_limit(tmp_path,monkeypatch,truncations):
    import coder_inference
    import requests
    repo,store,payload=environment(tmp_path,model='local',ollama_url='http://local',
        settings={**DEFAULTS,'daedalus_role_outputs':{'reviewer':32}})
    monkeypatch.setattr(coder_inference,'require_local_model',lambda *a,**k:{'capabilities':['completion']})
    monkeypatch.setattr(coder_inference,'ensure_context',lambda *a,**k:None)
    calls=[]
    class Response:
        status_code=200
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def raise_for_status(self): pass
        def iter_lines(self):
            truncated=len(calls)<=truncations
            yield json.dumps({'message':{'content':'{"runner":' if truncated else json.dumps(file_probe())},
                'done':True,'done_reason':'length' if truncated else 'stop','prompt_eval_count':10,'eval_count':32}).encode()
    def post(url,**kwargs):
        assert url=='http://local/api/chat'
        calls.append(kwargs['json'])
        return Response()
    monkeypatch.setattr(requests,'post',post)
    if truncations==4:
        with pytest.raises(ValueError,match='recovery exhausted'): run_contract(store,'op',repo,_read_only)
    else:
        assert run_contract(store,'op',repo,_read_only)['checks'][0]['runner']=='file'
    modes=[v.get('format') if isinstance(v.get('format'),str) or 'format' not in v else 'schema' for v in calls]
    assert modes==(['schema','schema','json',None] if truncations==4 else ['schema','schema','json'])
    assert {v['options']['num_predict'] for v in calls}=={32}
    events=[e['data'] for e in store.events('op') if e['type']=='response_format_recovery']
    assert len(events)==truncations and all(e['failure_category']=='output_limit' for e in events)
    assert all(e['rejected_response']=='{"runner":' for e in events)


def test_negative_raw_review_is_persisted_and_gets_two_semantic_revisions(tmp_path,monkeypatch):
    import coder_inference
    repo,store,payload=environment(tmp_path,task='Run CLI',original_task='Run CLI',
        requirements=[{'id':'r1','kind':'behavior','text':'Run CLI','source_refs':['request-1']}])
    calls=[]
    def chat(*args,**kwargs):
        prompt=args[3][0]['content'];calls.append(prompt)
        if 'Independently audit this raw probe' in prompt:
            return json.dumps({'valid':False,'reason':'Does not exercise the requested failure'})
        index=sum('Independently audit this raw probe' not in p for p in calls)
        if index>1: assert 'Does not exercise the requested failure' in prompt
        return json.dumps({'runner':'shell','cwd':'.','program':f'python3 cli.py {index}',
            'expected_behavior':'Execute CLI','bindings':[{'path':'cli.py'}]})
    monkeypatch.setattr(coder_inference,'local_chat',chat)
    for _ in range(2):
        with pytest.raises(ValueError,match='two semantic revisions exhausted'): run_contract(store,'op',repo,_read_only)
    assert len(calls)==6
    events=store.events('op')
    assert len([e for e in events if e['type']=='probe_rejected'])==3
    assert not any(e['type']=='response_format_recovery' for e in events)


def test_project_test_aliases_share_execution_but_independent_probes_do_not(tmp_path):
    repo,store,payload=environment(tmp_path)
    (repo.root/'README.md').write_text('Source')
    revision=repo.snapshot()['revision']
    command="echo run >> counter; printf 'Ran 1 test in 0.001s\nOK\n'"
    base={'id':'project-test','command':command,'cwd':'.','is_test':True,'test_runner':'unittest'}
    payload.update(revision_id=revision,checks=[base,
        {**base,'id':'alias-1','execution_alias':'project-test','requirement_ids':['r1']},
        {**base,'id':'alias-2','execution_alias':'project-test','requirement_ids':['r2']},
        {**base,'id':'independent','origin':'independent'}])
    result=_run_check(store,'op',repo,payload)
    assert result['passed']
    assert [c.get('reused_from') for c in result['checks']]==[None,'project-test','project-test',None]
    assert (store.root/'checks'/'job'/revision/'workspace'/'counter').read_text().splitlines()==['run','run']
