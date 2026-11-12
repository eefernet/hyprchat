"""Regressions from the first Qwen qualification, independent of model inference."""
import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace
import pytest

from coder_worker_runtime import WorkerStore
from context_policy import DEFAULTS, resolve


def operation(tmp_path, calls=4):
    store = WorkerStore(tmp_path)
    store.create('op', 'job', 'code', {'engine':'openhands159', 'settings':DEFAULTS,
        'calls_remaining':calls, 'seconds_remaining':100, '_active_started':time.time()})
    store.update('op', started=time.time())
    return store


def test_resume_preserves_usage_and_explicit_allowance_records_history(monkeypatch, tmp_path):
    from .test_daedalus_jobs import setup
    import coder_jobs
    from db import coder_jobs as jobs
    setup(monkeypatch,tmp_path)
    async def run():
        job=await jobs.create('conversation','task','build_from_prompt','','local','resume',engine='openhands159',policy_version=7)
        await jobs.save(job['id'],state='blocked',resume_state='coding',calls_used=17,seconds_used=123,repair_round=1,attempt=3)
        resumed=await coder_jobs.resume(job['id'])
        assert (resumed['calls_used'],resumed['seconds_used'],resumed['allowance'],resumed['repair_round'],resumed['attempt'])==(17,123,1,1,3)
        await jobs.save(job['id'],state='blocked')
        renewed=await coder_jobs.resume(job['id'],budget_mode='new_allowance')
        assert renewed['calls_used']==renewed['seconds_used']==0
        assert renewed['previous_allowances_calls']==17 and renewed['previous_allowances_seconds']==123
        assert renewed['allowance']==2 and renewed['repair_round']==1
        with pytest.raises(ValueError,match='budget_mode'):await coder_jobs.resume(job['id'],budget_mode='surprise')
    asyncio.run(run())


def test_cache_collection_protects_live_leases_and_retained_evidence(tmp_path):
    from coder_engine_cache import lease, collect
    store=WorkerStore(tmp_path)
    active=tmp_path/'project-environments'/'active'
    old=tmp_path/'project-environments'/'old';old.mkdir(parents=True);(old/'data').write_bytes(b'x'*1024)
    retained=tmp_path/'artifacts'/'accepted';retained.parent.mkdir();retained.write_bytes(b'keep')
    with lease(active):
        (active/'data').write_bytes(b'x'*1024)
        removed=collect(store,limit_bytes=0)
        assert [r['cache'] for r in removed]==['project-environments/old']
        assert (active/'data').exists() and retained.read_bytes()==b'keep'
    collect(store,limit_bytes=0)
    assert not active.exists() and retained.exists()


def test_cache_collection_bounds_full_store_scans(tmp_path, monkeypatch):
    import coder_worker_runtime
    from coder_engine_cache import collect
    store = WorkerStore(tmp_path)
    for index in range(128):
        cache = tmp_path / 'operation-caches' / str(index)
        cache.mkdir(parents=True)
        (cache / 'data').write_bytes(b'x' * 1024)
    retained = tmp_path / 'artifacts' / 'accepted'
    retained.parent.mkdir()
    retained.write_bytes(b'keep')
    measure = coder_worker_runtime.storage_usage
    full_scans = []
    def counted(root):
        if Path(root) == store.root:
            full_scans.append(root)
        return measure(root)
    monkeypatch.setattr(coder_worker_runtime, 'storage_usage', counted)
    removed = collect(store, limit_bytes=0)
    assert len(removed) == 128 and retained.read_bytes() == b'keep'
    assert len(full_scans) <= 2


@pytest.mark.parametrize('engine,protect,expected', [
    ('openhands159', False, 200), ('openhands159', True, 507), (None, False, 507)])
def test_source_upload_collects_only_inactive_modern_caches(tmp_path, monkeypatch, engine, protect, expected):
    import hashlib
    import io
    import tarfile
    from contextlib import nullcontext
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import coder_worker_runtime
    from coder_engine_cache import lease
    store = WorkerStore(tmp_path / 'worker')
    cache = store.root / 'project-environments' / 'old'
    cache.mkdir(parents=True)
    (cache / 'data').write_bytes(b'reconstructible')
    retained = store.root / 'artifacts' / 'accepted'
    retained.parent.mkdir()
    retained.write_bytes(b'retained')
    monkeypatch.setenv('DAEDALUS_STATE_DIR', str(store.root))
    monkeypatch.setattr(coder_worker_runtime.shutil, 'disk_usage',
                        lambda _: SimpleNamespace(free=0 if cache.exists() else 2 * 1024**2))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        member = tarfile.TarInfo('app.py')
        member.size = 4
        archive.addfile(member, io.BytesIO(b'pass'))
    body = buffer.getvalue()
    app = FastAPI()
    coder_worker_runtime.install_routes(app, tmp_path / 'projects', engine=engine)
    with lease(cache) if protect else nullcontext():
        with TestClient(app) as client:
            response = client.put('/jobs/cw3-abc/source/' + hashlib.sha256(body).hexdigest() + '?free_mb=1',
                                  content=body)
    assert response.status_code == expected
    assert cache.exists() == (expected == 507)
    assert retained.read_bytes() == b'retained'
    assert not list((store.root / 'sources').rglob('*.part'))


def test_evaluator_cache_lifecycle_preserves_reports_and_active_leases(tmp_path):
    from evals.engine_campaign import collect_evaluator_caches
    from coder_engine_cache import lease
    with pytest.raises(ValueError, match='marked candidate'):
        collect_evaluator_caches(tmp_path)
    (tmp_path / 'codex-candidate').touch()
    for name in ('completed', 'active'):
        root = tmp_path / name
        root.mkdir()
        (root / 'report.json').write_text('{"preserve":"original evidence"}')
        cache = root / 'evaluator-worker' / 'project-environments' / 'dependencies'
        cache.mkdir(parents=True)
        (cache / 'browser').write_bytes(b'cached')
        (root / 'evaluator-worker' / 'accepted-source').write_bytes(b'source')
    active = tmp_path / 'active/evaluator-worker/project-environments/dependencies'
    with lease(active):
        receipts = collect_evaluator_caches(tmp_path)
        assert [r['store'] for r in receipts] == ['completed/evaluator-worker']
        assert active.exists()
    for name in ('completed', 'active'):
        assert json.loads((tmp_path / name / 'report.json').read_text())['preserve'] == 'original evidence'
        assert (tmp_path / name / 'evaluator-worker/accepted-source').read_bytes() == b'source'


def test_fresh_evaluator_store_initializes_storage_collection(tmp_path):
    from coder_worker_runtime import _boundary
    from coder_engine_cache import lease
    store=operation(tmp_path)
    payload={**store.get('op')['payload'],'settings':{**DEFAULTS,'daedalus_storage_mb':1,'daedalus_min_free_mb':0}}
    store.update('op',payload=payload)
    old=tmp_path/'project-environments/old';old.mkdir(parents=True);(old/'data').write_bytes(b'x'*2*1024*1024)
    with lease(tmp_path/'project-environments/active') as active:
        (active/'data').write_bytes(b'active')
        _boundary(store,'op')
        assert not old.exists() and (active/'data').read_bytes()==b'active'
        assert store._storage_checked_at>0
    assert store.get('op')['calls']==0


def test_environment_preparation_does_not_consume_active_time(tmp_path):
    from coder_engine_environment import preparation
    from coder_worker_runtime import _boundary
    store=operation(tmp_path)
    with preparation(store,'op'):
        time.sleep(.02)
        _boundary(store,'op')
    p=store.get('op')['payload']
    assert p['_preparation_seconds']>=.02
    assert p['_active_started']>store.get('op')['started']
    assert '_preparation_deadline' not in p


def test_counting_failure_is_explicit(monkeypatch):
    from coder_engine_tokens import count_request, TokenCountingError
    monkeypatch.delenv('DAEDALUS_TOKENIZER_DIR',raising=False)
    with pytest.raises(TokenCountingError,match='not configured'):count_request({'messages':[{'role':'user','content':'hello'}]})


def test_transport_retry_charges_both_attempts_before_dispatch(tmp_path,monkeypatch):
    from coder_engine_transport import infer, TransportFailure
    monkeypatch.setattr('coder_engine_tokens.count_request',lambda _:20)
    store=operation(tmp_path);sent=[]
    def send(current):
        sent.append(current['id'])
        if len(sent)==1:raise TransportFailure(500,'malformed_tool_output','unexpected end of JSON',True)
        return {'done':True,'message':{'tool_calls':[{'function':{'name':'write','arguments':{'text':'ok'}}}]},'prompt_eval_count':20,'eval_count':15,'done_reason':'stop'}
    result=infer(store,'op',{'options':{},'messages':[]},'builder',resolve('builder',DEFAULTS),send)
    assert result['done'] and len(sent)==2 and store.get('op')['calls']==2


def test_transport_never_retries_past_budget_or_after_success(tmp_path,monkeypatch):
    from coder_engine_transport import infer, TransportFailure
    monkeypatch.setattr('coder_engine_tokens.count_request',lambda _:20)
    store=operation(tmp_path,calls=1)
    def fail(_):raise TransportFailure(500,'malformed_tool_output','incomplete',True)
    with pytest.raises(TimeoutError,match='Model-call'):infer(store,'op',{'options':{}},'builder',resolve('builder',DEFAULTS),fail)
    assert store.get('op')['calls']==1


def test_transport_overflow_has_no_inference_or_budget_charge(tmp_path,monkeypatch):
    from coder_engine_transport import infer, TransportFailure
    monkeypatch.setattr('coder_engine_tokens.count_request',lambda _:100000)
    store=operation(tmp_path)
    with pytest.raises(TransportFailure) as caught:
        infer(store,'op',{'options':{}},'builder',resolve('builder',DEFAULTS),lambda _:pytest.fail('must not send'))
    assert caught.value.status==413 and store.get('op')['calls']==0


def test_output_exhaustion_is_bounded_and_keeps_available_usage(tmp_path,monkeypatch):
    from coder_engine_transport import infer, TransportFailure
    monkeypatch.setattr('coder_engine_tokens.count_request',lambda _:20)
    store=operation(tmp_path)
    response={'done':True,'done_reason':'length','message':{'content':'','thinking':'still reasoning'},
              'prompt_eval_count':20,'eval_count':4096}
    with pytest.raises(TransportFailure) as caught:
        infer(store,'op',{'options':{}},'builder',resolve('builder',DEFAULTS),lambda _:response)
    assert caught.value.category=='output_limit' and store.get('op')['calls']==2
    failures=[event['data'] for event in store.events('op') if event['type']=='model_failure']
    assert len(failures)==2 and [row['retry'] for row in failures]==[True,False]
    assert all(row['usage_available'] and row['usage']['eval_count']==4096 and row['usage']['done_reason']=='length' for row in failures)


def test_stop_hook_defers_unprepared_dependencies_without_code_repair(tmp_path,monkeypatch):
    from coder_engine_hook import evaluate
    import coder_profiles
    (tmp_path/'main.py').write_text('print("application")')
    package={'cwd':'.','commands':{'setup':['install declared dependencies']}}
    monkeypatch.setattr(coder_profiles,'discover',lambda *args:{'profiles':[package], 'checks':[
        {'id':'test','phase':'test','command':'echo actual-test-failure; exit 1'}]})
    monkeypatch.setattr(coder_profiles,'environment_key',lambda *args:('dependency-key',{}))
    cache=tmp_path/'cache';monkeypatch.setenv('DAEDALUS_ENVIRONMENT_ROOT',str(cache))
    state={'root':str(tmp_path),'finish_seen':True,'denials':0,'nudges':0,'command_seconds':10}
    deny,reason=evaluate(state)
    assert not deny and not state['passed'] and state['denials']==0 and 'preparation' in reason
    prepared=cache/'dependency-key';prepared.mkdir(parents=True);(prepared/'environment-ready.json').write_text('{}')
    deny,reason=evaluate(state)
    assert deny and state['denials']==1 and 'actual-test-failure' in reason


def test_requested_jar_is_a_deliverable():
    from coder_engine_contract import required_files
    assert 'target/app.jar' in required_files('Build an app. Run java -jar target/app.jar Ada.')
    assert 'example.jar' not in required_files('Read example.jar supplied as input.')


def test_standalone_java_has_compile_check(tmp_path):
    from coder_profiles import discover
    from coder_repository import Repository
    root=tmp_path/'source';root.mkdir();(root/'Counter.java').write_text('class Counter { public static void main(String[] a){} }')
    repo=Repository(root,tmp_path/'repo',DEFAULTS['daedalus_exclude_dirs'])
    profile=discover(repo)
    assert any('javac -d build/classes' in row['command'] for row in profile['checks'])


def test_gui_fill_filters_label_but_rejects_two_editable_targets(monkeypatch):
    import sys
    from coder_accessibility import perform
    class State:
        def __init__(self,edit):self.edit=edit
        def contains(self,flag):return self.edit if flag=='editable' else False
    class Node:
        def __init__(self,name,edit=False,children=()):self.name,self.edit,self.children=name,edit,list(children);self.childCount=len(children);self.value=''
        def getRoleName(self):return 'text' if self.edit else 'label'
        def getState(self):return State(self.edit)
        def getChildAtIndex(self,i):return self.children[i]
        def queryText(self):raise NotImplementedError
        def queryEditableText(self):return self
        def setTextContents(self,value):self.value=value;return True
    label=Node('Item');field=Node('Item',True);root=Node('',children=[label,field])
    monkeypatch.setitem(sys.modules,'pyatspi',SimpleNamespace(Registry=SimpleNamespace(getDesktop=lambda _:root),STATE_SHOWING='showing',STATE_EDITABLE='editable'))
    perform({'action':'fill','name':'Item','value':'hello'})
    assert field.value=='hello' and label.value==''
    root.children.append(Node('Item',True));root.childCount=3
    with pytest.raises(ValueError,match='found 2'):perform({'action':'fill','name':'Item','value':'hello'})


def test_file_browser_flow_is_scoped_to_project():
    from coder_ui_checks import validate
    flow={'kind':'web','file_path':'index.html','steps':[{'action':'click','selector':'button'},{'action':'text','selector':'output','equals':'1'}]}
    assert validate([flow])==[flow]
    for name in ('../secret.html','/etc/secret.html','app.py'):
        with pytest.raises(ValueError,match='project-relative'):validate([{**flow,'file_path':name}])
    with pytest.raises(ValueError,match="flow 0, step 1: unsupported action 'assert'"):
        validate([{**flow,'steps':[{'action':'click','selector':'button'},{'action':'assert','equals':'1'}]}])
    with pytest.raises(ValueError,match='put equals or contains directly'):
        validate([{**flow,'steps':[{'action':'click','selector':'button'}]}])


def test_project_ui_loading_alone_cannot_pass(tmp_path,monkeypatch):
    from contextlib import contextmanager
    from coder_ui_checks import run
    from coder_engine_ops import summarize
    store=operation(tmp_path)
    store.update('op',payload={**store.get('op')['payload'],'revision_id':'revision'})
    @contextmanager
    def fake_session(*args):yield lambda request:{'text':'hello'}
    monkeypatch.setattr('coder_ui_checks.session',fake_session)
    rows=run(store,'op',None,[{'kind':'web','file_path':'index.html','steps':[{'action':'text','selector':'body','equals':'hello'}]}],origin='project_interaction')
    assert not rows[0]['passed'] and not summarize(rows,'revision',['web'])['checks_passed']


def test_web_snapshot_equality_reports_element_text_action(tmp_path,monkeypatch):
    from contextlib import contextmanager
    from coder_ui_checks import run
    store=operation(tmp_path);store.update('op',payload={**store.get('op')['payload'],'revision_id':'revision'})
    requests=[]
    @contextmanager
    def fake_session(*args):
        def action(request):requests.append(request);return {'accessibility':'count 1'}
        yield action
    monkeypatch.setattr('coder_ui_checks.session',fake_session)
    rows=run(store,'op',None,[{'kind':'web','file_path':'index.html','steps':[
        {'action':'click','selector':'button'},{'action':'observe','selector':'#count','equals':'1'}]}])
    assert not rows[0]['passed'] and 'action text' in rows[0]['reason']
    assert not any(request['action']=='observe' for request in requests)


def test_substitute_html_fixture_cannot_verify_requested_application(tmp_path,monkeypatch):
    from coder_ui_checks import run
    from coder_engine_ops import summarize
    store=operation(tmp_path);store.update('op',payload={**store.get('op')['payload'],
        'revision_id':'revision','task':'Create index.html with an Increment button and a working counter.'})
    project=tmp_path/'project';project.mkdir();(project/'index.html').write_text('<button>Broken application</button>')
    repository=SimpleNamespace(root=project)
    monkeypatch.setattr('coder_ui_checks.session',lambda *args:pytest.fail('Substitute page must not supply evidence'))
    rows=run(store,'op',repository,[{'kind':'web','file_path':'test-fixture.html','steps':[
        {'action':'click','selector':'button'},{'action':'text','selector':'output','equals':'1'}]}],origin='project_interaction')
    assert not rows[0]['passed'] and 'application entrypoint index.html' in rows[0]['reason']
    assert not summarize(rows,'revision',['web'])['checks_passed']


def test_browser_url_and_desktop_command_are_bound_to_named_entrypoint(tmp_path):
    from coder_ui_checks import bind_project_entrypoint, validate
    repository=SimpleNamespace(root=tmp_path)
    with pytest.raises(ValueError,match='application entrypoint index.html'):
        bind_project_entrypoint(repository,{'kind':'web','url':'http://127.0.0.1:8000/test-fixture.html'},'Create index.html.')
    with pytest.raises(ValueError,match='file_path'):
        validate([{'kind':'web','url':'file:///tmp/fixture.html','command':'true','steps':[{'action':'observe','contains':'fake'}]}])
    with pytest.raises(ValueError,match='application entrypoint app.py'):
        bind_project_entrypoint(repository,{'kind':'desktop','command':'python3 fake.py'},'Create app.py using PyQt6.')
    bind_project_entrypoint(repository,{'kind':'desktop','command':'/usr/bin/python3 app.py'},'Create app.py using PyQt6.')
    bind_project_entrypoint(repository,{'kind':'desktop','command':'java -cp build/classes Counter'},'Create Counter.java using Swing.')


@pytest.mark.parametrize('wrong_key',['launch','launch_command'])
def test_ui_manifest_feedback_names_the_required_command_field(wrong_key):
    from coder_ui_checks import validate
    flow={'kind':'desktop',wrong_key:'python3 app.py','steps':[
        {'action':'click','name':'Convert'}, {'action':'text','role':'label','equals':'212.0'}]}
    with pytest.raises(ValueError) as caught:validate([flow])
    assert 'missing required "command" field' in str(caught.value)
    assert wrong_key in str(caught.value)
    fixed={**flow,'command':flow.pop(wrong_key)}
    assert validate([fixed])==[fixed]


def test_ui_manifest_kind_and_command_shape_feedback_is_specific():
    from coder_ui_checks import validate
    flow={'kind':'qt','command':'python3 app.py','steps':[{'action':'observe','contains':'Ready'}]}
    with pytest.raises(ValueError,match='kind must be'):validate([flow])
    with pytest.raises(ValueError,match='shell command string'):
        validate([{**flow,'kind':'desktop','command':['python3','app.py']}])


@pytest.mark.skipif(__import__('os').getenv('DAEDALUS_LIVE_ENV') != '1',reason='Dedicated Linux dependency preparation')
def test_project_playwright_installs_its_own_browser_revision_and_reuses(tmp_path):
    import subprocess,os
    from coder_project_runtime import prepare_environment, sandbox_paths
    from coder_profiles import discover
    from coder_repository import Repository
    from coder_sandbox import Sandbox
    root=tmp_path/'source';root.mkdir()
    (root/'package.json').write_text(json.dumps({'devDependencies':{'playwright':'1.64.1'}}))
    repo=Repository(root,tmp_path/'repo',DEFAULTS['daedalus_exclude_dirs']);repo.refresh()
    profile=discover(repo)['profiles'][0]
    store=operation(tmp_path/'worker');store.update('op',payload={**store.get('op')['payload'],'seconds_remaining':400})
    env={'PATH':os.environ['PATH']};folder=tmp_path/'logs';folder.mkdir()
    rows=prepare_environment(store,'op',root,profile,'project',env,folder)
    assert all(row['passed'] for row in rows),rows
    assert (root/'node_modules').is_symlink()
    browser=next(row for row in rows if 'browser_identity' in row)
    assert browser['browser_identity']['version']=='1.64.1'
    script="const {chromium}=require('playwright');(async()=>{const b=await chromium.launch({headless:true,args:['--no-sandbox']});const p=await b.newPage();await p.setContent('<p>ready</p>');if(await p.textContent('p')!=='ready')throw Error('bad');await b.close()})()"
    with Sandbox(['node','-e',script],cwd=root,writable=[folder],readonly=sandbox_paths(root,env),environment=env) as box:
        run=subprocess.run(box.args,env=box.env,capture_output=True,text=True,timeout=40)
    assert run.returncode==0,run.stderr
    again=prepare_environment(store,'op',root,profile,'project',env,folder)
    assert all(row['passed'] and row.get('environment_reused') for row in again)


def test_modern_output_limits_do_not_use_legacy_compatibility_settings():
    from context_policy import operation_settings
    settings={**DEFAULTS,'daedalus_role_outputs':{'builder':8192}}
    assert resolve('builder',operation_settings({'engine':'openhands159','settings':settings})).num_predict==8192
    assert resolve('builder',operation_settings({'engine':'legacy','settings':settings})).num_predict==4096


@pytest.mark.parametrize('response',[[],{'message':[]},{'message':{'tool_calls':{}},'done':True},
    {'message':{'tool_calls':[{'function':'invalid'}]},'done':True}])
def test_malformed_transport_shapes_are_bounded(tmp_path,monkeypatch,response):
    from coder_engine_transport import infer, TransportFailure
    monkeypatch.setattr('coder_engine_tokens.count_request',lambda _:20)
    store=operation(tmp_path)
    with pytest.raises(TransportFailure):
        infer(store,'op',{'options':{}},'builder',resolve('builder',DEFAULTS),lambda _:response)
    assert store.get('op')['calls']==2


@pytest.mark.parametrize('workspace_type', [str, Path])
def test_packaged_outputs_are_hash_bound_and_in_delivered_archive(tmp_path, workspace_type):
    import tarfile
    from coder_engine_delivery import products,append_products
    source=tmp_path/'verification';(source/'target').mkdir(parents=True)
    jar=source/'target/app.jar';jar.write_bytes(b'jar-contents')
    rows=products(workspace_type(source),'Package as an executable target/app.jar','revision')
    assert len(rows)==1
    archive=tmp_path/'candidate.tar.gz'
    with tarfile.open(archive,'w:gz'):pass
    append_products(archive,rows,'revision',tmp_path)
    with tarfile.open(archive) as bundle:assert bundle.extractfile('target/app.jar').read()==b'jar-contents'
    jar.write_bytes(b'changed')
    with pytest.raises(ValueError,match='changed'):append_products(archive,rows,'revision',tmp_path)
    with pytest.raises(ValueError,match='another revision'):append_products(archive,rows,'other',tmp_path)


def test_campaign_pauses_infrastructure_without_resetting_trial_usage():
    from evals.engine_campaign import interruption,exception_interruption,paused
    job={'failure':{'category':'worker_interrupted'},'calls_used':17,'seconds_used':123}
    reason=interruption(job)
    trial={'key':'case-1','jobs':[job]}; report={}
    paused(report,trial,reason)
    assert 'finished' not in trial and report['paused']['budget_mode']=='preserve'
    assert job['calls_used']==17 and job['seconds_used']==123
    for category in ('malformed_tool_output','resource_limit','application_check'):
        assert not interruption({'failure':{'category':category}})
    for message in ('Worker free disk space is below the configured reserve',
                    'Worker storage allowance exceeded by retained data or active caches',
                    "HTTPStatusError: Server error '507 Insufficient Storage' for url '/source/archive'",
                    'Artifact storage is below the configured free-space reserve'):
        reason=exception_interruption(ValueError(message))
        assert reason
        paused(report,trial,reason)
        assert 'finished' not in trial and job['calls_used']==17 and job['seconds_used']==123
    assert exception_interruption(AssertionError('Held-out behavior failed')) is None


def test_interrupted_worker_usage_excludes_downtime_and_preserves_calls(tmp_path,monkeypatch):
    from coder_worker_runtime import _boundary,reconcile
    from context_policy import operation_active_seconds
    store=operation(tmp_path)
    now=time.time()
    payload={**store.get('op')['payload'],'_active_started':now-20,'seconds_remaining':1000}
    store.update('op',status='running',started=now-30,payload=payload,calls=3)
    _boundary(store,'op')
    assert 20 <= store.get('op')['payload']['_usage_checkpoint']['active_seconds'] < 22
    monkeypatch.setattr('coder_worker_runtime._alive',lambda _:False)
    monkeypatch.setattr('coder_worker_runtime.time.time',lambda:now+3600)
    lost=reconcile(store,'op')
    assert lost['status']=='interrupted' and lost['calls']==3
    assert 20 <= operation_active_seconds(lost) < 22
    assert operation_active_seconds({'started':10,'ended':100,'status':'cancelled',
        'payload':{'engine':'openhands159','_preparation_seconds':20,'_preparation_active_started':90}})==60
