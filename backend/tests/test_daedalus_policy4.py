import copy
import json
import subprocess
import pytest

from coder_api_probe import command, source_bindings
from coder_browser_schema import validate_steps
from coder_contracts import normalize_probe, targeted_correction
from coder_probe_audit import audit_failures


def api(language='node', **case):
    return {'runner':'api','cwd':'.','target':{'language':language,'path':'subject.mjs' if language=='node' else 'subject.py','export':'subject'},
        'cases':[{'args':[],'expect':{'kind':'equal','value':0},'preserve_inputs':True,**case}]}


@pytest.mark.parametrize('actual,passed',[('2',True),('[2]',False),('0',False)])
def test_map_numeric_values_use_real_export(tmp_path,actual,passed):
    (tmp_path/'subject.mjs').write_text(f'export function subject() {{ return new Map([["fruit",{actual}]]); }}')
    probe=api(expect={'kind':'map_entries','value':[['fruit',2]]})
    result=subprocess.run(command(probe),shell=True,cwd=tmp_path,capture_output=True,text=True)
    assert (result.returncode==0)==passed,result.stderr
    assert len(source_bindings(tmp_path,probe)[0]['sha256'])==64


@pytest.mark.parametrize('language',['python','node'])
@pytest.mark.parametrize('actual,passed',[('false',True),('0',False),('null',False)])
def test_falsy_equality_is_type_strict(tmp_path,language,actual,passed):
    probe=api(language,expect={'kind':'equal','value':False})
    value={'false':'False','null':'None'}.get(actual,actual) if language=='python' else actual
    (tmp_path/probe['target']['path']).write_text(f'def subject(): return {value}' if language=='python' else f'export const subject=()=>{value};')
    result=subprocess.run(command(probe),shell=True,cwd=tmp_path,capture_output=True,text=True)
    assert (result.returncode==0)==passed,result.stderr


@pytest.mark.parametrize('language',['python','node'])
def test_input_mutation_and_expected_exceptions(tmp_path,language):
    probe=api(language,args=[[1]],expect={'kind':'raises','error_type':'ValueError' if language=='python' else 'TypeError'})
    (tmp_path/probe['target']['path']).write_text('def subject(items):\n    items.append(2)\n    raise ValueError("bad")' if language=='python' else 'export function subject(items) { items.push(2); throw new TypeError("bad"); }')
    assert subprocess.run(command(probe),shell=True,cwd=tmp_path,capture_output=True).returncode!=0
    probe['cases'][0]['preserve_inputs']=False
    assert subprocess.run(command(probe),shell=True,cwd=tmp_path,capture_output=True).returncode==0


def test_missing_source_binding_is_real_failure_not_fake_success(tmp_path):
    probe=api('python')
    with pytest.raises(FileNotFoundError,match='missing'): source_bindings(tmp_path,probe)
    (tmp_path/'subject.py').write_text('MISSING=object()')
    assert subprocess.run(command(probe),shell=True,cwd=tmp_path,capture_output=True).returncode!=0


def test_sentinel_and_json_special_keys(tmp_path):
    probe=api('python',expect={'kind':'export_identity','export':'MISSING'})
    (tmp_path/'subject.py').write_text('MISSING=object()\ndef subject(): return MISSING')
    assert subprocess.run(command(probe),shell=True,cwd=tmp_path,capture_output=True).returncode==0
    probe=api(args=[{'__proto__':'fruit'}],expect={'kind':'equal','value':'fruit'})
    (tmp_path/'subject.mjs').write_text("export const subject=value => Object.hasOwn(value,'__proto__') ? value.__proto__ : null;")
    assert subprocess.run(command(probe),shell=True,cwd=tmp_path,capture_output=True).returncode==0


@pytest.mark.parametrize('broken',[False,True])
def test_stored_none_is_distinct_from_missing_key(tmp_path,broken):
    body="value=rows.get(key)\n    return value if value is not None else MISSING" if broken else "return rows[key] if key in rows else MISSING"
    (tmp_path/'subject.py').write_text('MISSING=object()\ndef subject(rows,key):\n    '+body+'\n')
    probe=api('python')
    probe['cases']=[{'args':[{'x':value},'x'],'expect':{'kind':'equal','value':value},'preserve_inputs':True} for value in [0,False,'',None]]
    probe['cases'].append({'args':[{},'x'],'expect':{'kind':'export_identity','export':'MISSING'},'preserve_inputs':True})
    result=subprocess.run(command(probe),shell=True,cwd=tmp_path,capture_output=True,text=True)
    assert (result.returncode!=0)==broken,result.stderr


def test_raw_javascript_binds_import_and_rejects_shadow():
    req={'id':'r','kind':'behavior'}
    probe={'runner':'node','program':"import {subject as actual} from './subject.mjs'; import assert from 'node:assert/strict'; assert.equal(actual(),1);",
        'bindings':[{'path':'subject.mjs','export':'subject','local_name':'actual'}]}
    normalize_probe(probe,req,'check',policy_version=4)
    with pytest.raises(ValueError,match='shadows'):
        normalize_probe({**probe,'program':probe['program']+' actual=()=>1;'},req,'check',policy_version=4)


@pytest.mark.parametrize('program',[
    'from subject import MISSING\ndef subject(): return 1\nassert subject()==1',
    'from subject import subject\nsubject=lambda: 1\nassert subject()==1',
    'from subject import subject\ndef subject(): return 1\nassert subject()==1',
    'from subject import subject\nassert 1==1',
])
def test_copied_shadowed_or_unused_python_subject_is_rejected(program):
    with pytest.raises(ValueError,match='bindings'):
        normalize_probe({'runner':'python','program':program,'bindings':[{'path':'subject.py','export':'subject'}]},
            {'id':'r1','kind':'behavior'},'check-r1',policy_version=4)


@pytest.mark.parametrize('step,field',[
    ({'action':'evaluate','expression':'button.click()'},'action'),
    ({'action':'click','selector':'button','force':True},'force'),
    ({'action':'text','selector':'li','value':2},'value'),
    ({'action':'visible','selector':'button','value':'Delete'},'value'),
    ({'action':'fill','selector':'input'},'value'),
    ({'action':'click','selector':{'role':'button','hasText':'Delete'}},'selector'),
])
def test_browser_errors_identify_exact_field(step,field):
    with pytest.raises(ValueError,match=r'steps\[0\]\.'+field): validate_steps([step])


def test_policy3_compatibility_and_policy4_rationale():
    req={'id':'r1','kind':'behavior','source_refs':['request-1']};sources={'request-1':'Return a count'}
    check=normalize_probe({'runner':'python','program':'assert True'},req,'check-r1')
    assert targeted_correction({'disposition':'code_defect'},check,req,sources)[0]==check
    with pytest.raises(ValueError,match='reason'):
        targeted_correction({'disposition':'code_defect'},check,req,sources,policy_version=4)


def audit_fixture():
    probes=[{'id':'check-r'+str(n),'requirement_ids':['r'+str(n)],**api()} for n in (1,2)]
    job={'id':'job','state':'checking','revision_id':'revision-a','attempt':0,'calls_used':0,
         'verification_plan':{'checks':probes},'requirements':[{'id':'r1'},{'id':'r2'}]}
    result={'checks':[{'id':'requirement-check-r1','origin':'independent','passed':False,'error':'wrong value'}]}
    return job,result


@pytest.mark.asyncio
async def test_ambiguous_audits_survive_restart_and_do_not_consume_repairs():
    job,result=audit_fixture();calls=[]
    async def save(identity,**changes): job.update(changes);return copy.deepcopy(job)
    async def operate(current,kind,**extra):
        calls.append(extra);job['calls_used']+=1
        return {**job['verification_plan'],'audits':[{'check_id':'check-r1','disposition':'ambiguous','reason':'Cannot resolve'}]},copy.deepcopy(job)
    current,recheck=await audit_failures(job,result,operate,save)
    assert recheck and current['state']=='blocked' and current['attempt']==0 and len(calls)==2
    job.update(json.loads(json.dumps(current)))
    await audit_failures(job,result,operate,save)
    assert len(calls)==2 and len(job['probe_audits'])==2
    assert calls[0]['evidence']['audit_key']['round']==1 and calls[1]['evidence']['audit_key']['round']==2


@pytest.mark.asyncio
async def test_corrections_are_per_check_and_recheck_same_revision():
    job,result=audit_fixture();job['probe_replacements']={'check-r2':2}
    async def save(identity,**changes):job.update(changes);return copy.deepcopy(job)
    async def operate(current,kind,**extra):
        probes=copy.deepcopy(current['verification_plan']['checks']);probes[0]['cases'][0]['expect']['value']=2
        return {'checks':probes,'audits':[{'check_id':'check-r1','disposition':'probe_defect','reason':'Wrong expected value'}]},current
    current,recheck=await audit_failures(job,result,operate,save)
    assert recheck and current['state']=='checking' and current['revision_id']=='revision-a' and current['attempt']==0
    assert current['probe_replacements']=={'check-r2':2,'check-r1':1}
    assert current['probe_audits'][0]['previous_probe']['cases'][0]['expect']['value']==0


@pytest.mark.asyncio
async def test_new_revision_requires_fresh_audit_cached_verdict_does_not():
    job,result=audit_fixture();calls=[]
    async def save(identity,**changes):job.update(changes);return copy.deepcopy(job)
    async def operate(current,kind,**extra):
        calls.append(extra)
        return {**current['verification_plan'],'audits':[{'check_id':'check-r1','disposition':'code_defect','reason':'Wrong return'}]},current
    await audit_failures(job,result,operate,save)
    await audit_failures(job,result,operate,save)
    assert len(calls)==1
    job['revision_id']='revision-b';job['attempt']=1
    await audit_failures(job,result,operate,save)
    assert len(calls)==2
    assert calls[-1]['evidence']['audit_key']['revision_id']=='revision-b'


def test_reliability_profile_retains_settings_owned_limits():
    from evals.run_coder_benchmark import evaluation_profile
    profile=evaluation_profile('reliability')
    assert profile['openhands_num_ctx']==65536
    assert profile['daedalus_role_outputs']['reviewer']==16384
    assert profile['daedalus_role_outputs']['builder']==8192
    assert profile['daedalus_role_thinking']['reviewer']=='on'
    assert profile['daedalus_role_thinking']['builder']=='off'
    assert profile['daedalus_job_seconds']==3600 and profile['daedalus_model_calls']==120


def test_full_evaluation_requires_every_focused_case_and_edit():
    from evals.rollout_gate import FOCUSED_FIXTURES,focused_verdict
    rows=[{'fixture':name,'passed':True,'false_acceptance':False} for name in FOCUSED_FIXTURES]
    grouping=next(row for row in rows if row['fixture']=='grouping')
    grouping['followups']={'passed':True,'original_artifact_preserved':True,'requests':[{'evidence':{'passed':True}} for _ in range(2)]}
    assert focused_verdict({'results':rows})['eligible']
    assert not focused_verdict({'results':rows[:-1]})['eligible']
    grouping['followups']['requests'][0]['false_acceptance']=True
    assert not focused_verdict({'results':rows})['eligible']


@pytest.mark.asyncio
async def test_resume_retains_audit_limits_in_database(tmp_path,monkeypatch):
    import coder_jobs as controller
    import database as db
    import config
    from context_policy import DEFAULTS
    from db import coder_jobs as store
    monkeypatch.setattr(db,'DATABASE_PATH',str(tmp_path/'jobs.db'))
    monkeypatch.setattr(config,'CONTEXT_SETTINGS',{**DEFAULTS,'daedalus_v3_enabled':True})
    monkeypatch.setattr(controller,'spawn',lambda *a:None)
    await db.init_db();await db.create_conversation('conv',model='local')
    job=await controller.create('conv','Build a counter',model='local',key='new')
    assert job['policy_version']==6
    await store.save(job['id'],policy_version=4)  # Existing jobs keep their original policy.
    history=[{'check_id':'check-r1','round':2,'disposition':'ambiguous'}]
    await store.save(job['id'],state='blocked',resume_state='checking',probe_audits=history,probe_replacements={'check-r1':2})
    resumed=await controller.resume(job['id'])
    assert resumed['probe_audits']==history and resumed['probe_replacements']=={'check-r1':2}
    assert resumed['policy_version']==4


def test_worker_checks_bind_immutable_revision_and_reject_empty_tests(tmp_path):
    import time
    from coder_repository import Repository
    from coder_worker_runtime import WorkerStore,_run_check
    from coder_verification import probe_checks
    from context_policy import DEFAULTS
    project=tmp_path/'project';project.mkdir();(project/'subject.py').write_text('def subject(): return 0')
    repository=Repository(project,tmp_path/'repo');repository.refresh()
    revision=repository.snapshot('baseline')
    store=WorkerStore(tmp_path/'worker')
    payload={'settings':dict(DEFAULTS),'policy_version':4,'seconds_remaining':60,'calls_remaining':5,
        'revision_id':revision['revision'],'checks':probe_checks([{'id':'test','requirement_ids':['r1'],**api('python')}])}
    store.create('op','job','check',payload);store.update('op',started=time.time())
    (project/'subject.py').write_text('def subject(): return 999')
    result=_run_check(store,'op',repository,payload)
    assert result['passed']
    assert result['checks'][0]['source_bindings'][0]['sha256']==__import__('hashlib').sha256(b'def subject(): return 0').hexdigest()
    payload['checks']=[{'id':'empty','command':'python3 -m unittest discover','is_test':True}]
    result=_run_check(store,'op',repository,payload)
    assert not result['passed'] and result['checks'][0]['test_count']==0


@pytest.mark.parametrize('valid',[True,False])
def test_raw_documentation_review_preserves_its_scope_and_negative_verdict(tmp_path,monkeypatch,valid):
    import time
    import coder_inference
    from coder_contracts import run_contract
    from coder_repository import Repository
    from coder_worker_runtime import WorkerStore,_read_only
    from coder_verification import probe_checks
    from context_policy import DEFAULTS
    project=tmp_path/'project';project.mkdir()
    repository=Repository(project,tmp_path/'repo');repository.refresh()
    store=WorkerStore(tmp_path/'worker')
    requirement={'id':'r6','text':'Add README.','kind':'documentation','source_refs':['request-1']}
    task='Create groups.mjs exporting groupBy(items,key). Preserve inputs. Add runnable Node tests and README.'
    store.create('verify-doc','job','verify',{'policy_version':4,'task':task,'original_task':task,'requirements':[requirement],
        'settings':dict(DEFAULTS),'seconds_remaining':60,'calls_remaining':10})
    store.update('verify-doc',started=time.time())
    probe={'runner':'shell','cwd':'.','expected_behavior':'README exists and is nonempty',
        'program':'test -s README.md','bindings':[{'path':'README.md'}]}
    replies=iter([probe,{'valid':valid,'reason':'Documentation coverage is sufficient' if valid else 'The probe does not check the assigned requirement'}])
    prompts=[]
    def chat(*args,**kwargs):
        prompts.append(args[3][0]['content'])
        return json.dumps(next(replies))
    monkeypatch.setattr(coder_inference,'local_chat',chat)
    if not valid:
        with pytest.raises(ValueError,match='Invalid raw probe: The probe'):
            run_contract(store,'verify-doc',repository,_read_only)
    else:
        result=run_contract(store,'verify-doc',repository,_read_only)
        check=result['checks'][0]
        assert check['requirement_ids']==['r6']
        command=probe_checks([check])[0]['command']
        assert subprocess.run(command,shell=True,cwd=project).returncode!=0
        (project/'README.md').write_text('How to use groupBy\n')
        assert subprocess.run(command,shell=True,cwd=project).returncode==0
        # The prospective documentation check does not assert an API exists.
        assert not (project/'groups.mjs').exists()
    assert len(prompts)==2
    reviews=[event for event in store.events('verify-doc') if event['type']=='raw_probe_review']
    assert len(reviews)==1 and reviews[0]['data']['valid'] is valid
