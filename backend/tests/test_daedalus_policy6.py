"""Policy-6 regressions with saved model responses; no inference or downloads."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

import coder_jobs as controller
import database as db
from db import coder_jobs as jobs
from coder_loop import classify_checks, step
from coder_profiles import discover, python_test_kind
from coder_repository import Repository
from coder_review import normalize_brief, normalize_checks, corrected_checks, verification_summary, plan
from coder_sdk_runtime import drive_to_checkpoint, tool_mode_key
from coder_worker_runtime import WorkerStore, _run_check, execute
from context_policy import DEFAULTS
import config


def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'jobs.db'))
    monkeypatch.setattr(config, 'CONTEXT_SETTINGS', {**DEFAULTS, 'daedalus_v3_enabled': True})
    monkeypatch.setattr(controller, 'spawn', lambda *args: None)
    async def initialize():
        await db.init_db()
        await db.create_conversation('conversation', model='coder:local')
    asyncio.run(initialize())

RESPONSES = json.loads((Path(__file__).parent / 'fixtures/daedalus_policy6.json').read_text())


def repository(tmp_path, files):
    root = tmp_path / 'project'; root.mkdir(parents=True)
    for name, contents in files.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(contents)
    repo = Repository(root, tmp_path / 'repo', DEFAULTS['daedalus_exclude_dirs'])
    repo.refresh()
    return repo


def native_environment(tmp_path, files, checks):
    repo = repository(tmp_path, files)
    revision = repo.snapshot()['revision']
    store = WorkerStore(tmp_path / 'worker')
    payload = {'policy_version':6, 'settings':dict(DEFAULTS), 'revision_id':revision, 'baseline_revision':revision,
               'seconds_remaining':60, 'calls_remaining':10, 'checks':checks}
    store.create('op','job','check',payload); store.update('op',started=time.time())
    return repo, store, payload


def test_static_preview_survives_test_tooling_manifest(tmp_path):
    repo = repository(tmp_path, {'index.html':'<html>Hello</html>', 'package.json':json.dumps({'scripts':{'test':'node --test'}})})
    result = discover(repo)
    assert result['web_packages'] == ['.']
    assert any(c.get('server_command','').startswith('python3 -m http.server') for c in result['checks'])
    assert any(c.get('command')=='npm run test' for c in result['checks'])


def test_python_scripts_unittest_pytest_and_demonstrations_are_distinct(tmp_path):
    repo = repository(tmp_path, {'test_script.py':'assert 1 == 1\n', 'test_unit.py':'import unittest\nclass T(unittest.TestCase):\n def test_x(self): pass\n',
        'test_pytest.py':'def test_answer():\n assert True\n', 'demo.py':'assert False\n', 'test_demo.py':'print("passed")\n'})
    commands = [c['command'] for c in discover(repo)['checks'] if c.get('is_test')]
    assert '.venv/bin/python test_script.py' in commands
    assert '.venv/bin/python -m unittest -v test_unit.py' in commands
    assert '.venv/bin/python -m pytest -q test_pytest.py' in commands
    assert not any('demo' in c for c in commands)


@pytest.mark.parametrize('files,command', [
    ({'app.csproj':'<Project/>'}, 'dotnet build'), ({'CMakeLists.txt':'project(App)'}, 'cmake --build'),
    ({'Makefile':'test:\n\ttrue\n'}, 'make test'), ({'composer.json':'{"scripts":{"test":"phpunit"}}'}, 'composer run test'),
    ({'Gemfile':'source "https://rubygems.org"', 'spec/example_spec.rb':''}, 'bundle exec rspec'),
    ({'Cargo.toml':''}, 'cargo test'), ({'go.mod':'module example'}, 'go test'), ({'pom.xml':'<project/>'}, 'mvn test'),
    ({'build.gradle':''}, 'gradle test')])
def test_cross_language_profiles(tmp_path, files, command):
    result = discover(repository(tmp_path, files))
    assert any(command in c.get('command','') for c in result['checks'])
    assert result['profiles'][0]['toolchains']


def test_command_precedence_and_validated_generic_fallback(tmp_path):
    repo = repository(tmp_path, {'package.json':'{"scripts":{"test":"node --test"}}', 'suite.sh':'exit 0',
        '.daedalus.json':'{"packages":{".":{"test":"bash suite.sh"}}}'})
    assert next(c for c in discover(repo)['checks'] if c.get('is_test'))['command']=='bash suite.sh'
    explicit = {'packages':{'.':{'test':'bash suite.sh explicit'}}}
    assert next(c for c in discover(repo, explicit)['checks'] if c.get('is_test'))['command']=='bash suite.sh explicit'
    proposals = {'packages':{'.':{'lint':'bash suite.sh'}}}
    assert any(c['command']=='bash suite.sh' and c.get('phase')=='lint' for c in discover(repo, proposals=proposals)['checks'])
    with pytest.raises(ValueError, match='existing repository file'):
        discover(repo, proposals={'packages':{'.':{'lint':'madeup --check'}}})


def test_multiple_evidence_types_and_no_api_file_freeze(tmp_path):
    brief = normalize_brief(RESPONSES['brief'])
    assert brief['outcomes'][0]['evidence_types']==['behavior','documentation','tests']
    repo = repository(tmp_path, {'subject.py':'MISSING = object()\n'})
    frozen = {'kind':'file','path':'subject.py','assertions':[{'kind':'unchanged'}], 'outcomes':['o1'],'evidence_types':['preservation']}
    with pytest.raises(ValueError,match='explicitly protected'):
        normalize_checks({'checks':[frozen]},repo,{'brief':brief})
    assert normalize_checks({'checks':[frozen]},repo,{'brief':brief,'protected_files':['subject.py']})[0]['id']=='audit-1'


def test_native_constant_list_provenance_and_isolation(tmp_path):
    files = {'subject.py':'MISSING = object()\ndef values(): return [1, 2]\n', 'README.md':'Usage'}
    repo, store, payload = native_environment(tmp_path, files, [])
    payload['brief'] = normalize_brief(RESPONSES['brief'])
    checks = normalize_checks(RESPONSES['author'], repo, payload)
    payload['checks'] = checks
    result = _run_check(store, 'op', repo, payload)
    assert result['passed'], result
    native = result['checks'][0]
    assert native['assertions_executed']==2 and native['coverage_observed']
    assert native['source_bindings'][0]['sha256']==hashlib.sha256(files['subject.py'].encode()).hexdigest()
    assert not (repo.root/'check_api.py').exists()
    assert not Path(native['audit_dir']).is_relative_to(repo.root)


@pytest.mark.parametrize('program', ['print("passed")\n', 'def test_nothing():\n assert True\n', 'import unittest\nunittest.main()\n'])
def test_zero_unexecuted_and_printed_tests_do_not_count(tmp_path, program):
    checks = [{'id':'test','command':'python3 test_script.py','is_test':True,'origin':'project'}]
    repo, store, payload = native_environment(tmp_path, {'test_script.py':program}, checks)
    result = _run_check(store,'op',repo,payload)
    assert not result['passed'] and not result['checks'][0]['coverage_observed']


def test_configured_test_failure_is_preserved(tmp_path):
    repo, store, payload = native_environment(tmp_path, {'test_script.py':'assert False\n'}, [
        {'id':'test','command':'python3 test_script.py','is_test':True,'origin':'project'}])
    result = _run_check(store,'op',repo,payload)
    assert not result['passed'] and result['checks'][0]['exit_code']!=0


def test_verification_source_mutation_cannot_establish_success(tmp_path):
    repo, store, payload = native_environment(tmp_path, {'subject.py':'answer=1\n'}, [
        {'id':'bad','command':"printf 'answer=2\\n' > subject.py",'origin':'project'}])
    result = _run_check(store,'op',repo,payload)
    assert not result['passed'] and result['checks'][0]['failure_kind']=='source_mutation'
    assert (repo.root/'subject.py').read_text()=='answer=1\n'


def test_missing_toolchain_is_environment_fault(tmp_path):
    repo, store, payload = native_environment(tmp_path, {'subject.py':''}, [
        {'id':'missing','command':'daedalus_toolchain_not_installed --test','origin':'project'}])
    result = _run_check(store,'op',repo,payload)
    assert result['environment_fault'] and not result['passed']


def test_baseline_failure_categories():
    baseline=[{'id':'old','passed':False,'command':'test','exit_code':1,'log_tail':'assert x'}, {'id':'new','passed':True}]
    checks=[baseline[0], {'id':'new','passed':False}, {'id':'env','passed':False,'environment_fault':True}]
    assert [c['classification'] for c in classify_checks(checks,baseline)]==['existing_failure','regression','environment']


def test_explanation_only_correction_rejected_and_real_fix_retained(tmp_path):
    repo = repository(tmp_path, {'subject.py':'MISSING=object()\ndef values(): return [1,2]\n'})
    payload={'brief':normalize_brief(RESPONSES['brief'])}
    previous=normalize_checks({'checks':[RESPONSES['author']['checks'][0]]},repo,payload)
    replacement=copy.deepcopy(previous[0]); replacement['files']['check_api.py']+='\n# corrected explanation\n'
    review={'corrections':[{'check_id':'audit-1','reason':'bad assertion','replacement':replacement}]}
    with pytest.raises(ValueError,match='Explanation-only'):
        corrected_checks(review,previous,repo,payload)
    replacement['files']['check_api.py']=replacement['files']['check_api.py'].replace('[1, 2]','[2, 3]')
    assert corrected_checks(review,previous,repo,payload)[0]['id']=='audit-1'
    assert '[1, 2]' in previous[0]['files']['check_api.py']


def test_brief_recovery_falls_back_to_original_request(tmp_path):
    repo, store, payload=native_environment(tmp_path, {'subject.py':''}, [])
    payload.update(original_task='Original request',task='Original request')
    store.update('op',payload=payload)
    store.event('op','response_format_recovery',rejected_response='Retained partial work brief')
    def failed(*args, **kwargs): raise ValueError('format recovery exhausted')
    result=plan(store,'op',repo,failed)
    assert result['planning_fallback'] and result['brief']['outcomes'][0]['text']=='Original request'
    assert result['plan_text']==['Retained partial work brief']


def test_builder_stops_on_checkpoint_and_narration_recovers_only_once():
    events=[]; calls=[0]; recovery=[False]
    class Conversation:
        def run(self): calls[0]+=1
        def send_message(self,message): assert 'finish' not in message
    result=drive_to_checkpoint(Conversation(),events,lambda:calls[0],lambda:20,lambda:'tree',lambda:recovery[0],lambda:recovery.__setitem__(0,True))
    assert not result and calls[0]==2 and recovery[0]
    # Interruptions retain the consumed recovery in the same round.
    drive_to_checkpoint(Conversation(),events,lambda:calls[0],lambda:20,lambda:'tree',lambda:recovery[0],lambda:None)
    assert calls[0]==3
    class Useful(Conversation):
        def run(self):
            calls[0]+=1; events.append({'kind':'ActionEvent','tool_name':'terminal'})
    drive_to_checkpoint(Useful(),events,lambda:calls[0],lambda:20,lambda:str(calls[0]),lambda:False,lambda:None)
    assert calls[0]==4


def test_tool_mode_cache_identity_tracks_digest_runtime_and_tools():
    base=tool_mode_key('digest','sdk',{'browser':False})
    assert len({base,tool_mode_key('changed','sdk',{'browser':False}),tool_mode_key('digest','new-sdk',{'browser':False}),tool_mode_key('digest','sdk',{'browser':True})})==4


def test_controller_does_not_accept_unsupported_positive_model_claims():
    job={'revision_id':'r','brief':normalize_brief(RESPONSES['brief']), 'checks':[]}
    summary=verification_summary(job,RESPONSES['accepted'])
    assert not summary['accepted'] and summary['unverified']==['o1']


def test_candidate_publication_continue_limits_stop_and_head_protection(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    async def scenario():
        job=await controller.create('conversation','task',model='coder:local',key='candidate')
        assert job['policy_version']==6
        revision='a'*40
        job=await jobs.save(job['id'],state='candidate_packaging',revision_id=revision,
            repair_round=2,check_corrections=2,recovery_used=True,
            verification_summary={'revision_id':revision,'accepted':False},resume_state='checking')
        fields={'artifact_id':'candidate-test','filename':'candidate.tar.gz','url':'/candidate.tar.gz',
                'kind':'archive','status':'review_candidate','metadata':{'revision_id':revision}}
        artifact=await jobs.finish_candidate(job['id'],revision,**fields)
        saved=await jobs.get(job['id'])
        assert saved['state']=='ready_for_review' and saved['artifact'] is None
        assert (await db.get_coding_project(job['project_id']))['accepted_revision_id']==''
        assert await jobs.finish_candidate(job['id'],revision,**fields)==artifact
        assert not any(j['id']==job['id'] for j in await jobs.recoverable())
        with pytest.raises(ValueError,match='exact immutable'):
            await controller.resume(job['id'])
        resumed=await controller.resume(job['id'],candidate_revision=revision)
        assert resumed['repair_round']==2 and resumed['check_corrections']==2 and resumed['recovery_used']
        assert resumed['candidate_revision']==revision
        await jobs.save(job['id'],state='candidate_packaging')
        await jobs.request_cancel(job['id'])
        with pytest.raises(InterruptedError):
            await jobs.finish_candidate(job['id'],revision,**fields)
        token=db.set_current_user_id('other')
        try:
            with pytest.raises(LookupError): await jobs.finish_candidate(job['id'],revision,**fields)
        finally: db.reset_current_user_id(token)
    asyncio.run(scenario())


def test_two_repair_rounds_survive_repeated_continue(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    calls=[]
    async def operate(job,kind,**extra):
        calls.append((kind,extra.get('round_id')))
        if kind=='accept': return {**RESPONSES['application_defect'],'revision_id':job['revision_id']},job
        if kind=='package': return {'revision_id':job['revision_id']},job
        raise AssertionError(kind)
    async def deliver(job,result,**kwargs):
        await jobs.save(job['id'],state='ready_for_review',candidate_artifact={'metadata':{'revision_id':job['revision_id']}})
        return {}
    monkeypatch.setattr(controller,'_operate',operate); monkeypatch.setattr(controller,'_deliver',deliver)
    async def scenario():
        job=await controller.create('conversation','task',model='coder:local')
        job=await jobs.save(job['id'],state='accepting',revision_id='b'*40,brief=normalize_brief(RESPONSES['brief']))
        await step(job,controller)
        job=await jobs.get(job['id']); assert job['repair_round']==1
        job=await jobs.save(job['id'],state='accepting')
        await step(job,controller)
        job=await jobs.get(job['id']); assert job['repair_round']==2 and job['recovery_used']
        job=await jobs.save(job['id'],state='accepting')
        await controller.run(job['id'])
        candidate=await jobs.get(job['id']); assert candidate['state']=='ready_for_review'
        resumed=await controller.resume(job['id'],candidate_revision='b'*40)
        assert resumed['repair_round']==2
        await jobs.save(job['id'],state='accepting')
        await controller.run(job['id'])
        assert (await jobs.get(job['id']))['state']=='ready_for_review'
        assert not any(kind=='code' for kind,_ in calls)
    asyncio.run(scenario())


def test_saved_response_build_edits_upload_repair_feature_journeys(monkeypatch,tmp_path):
    """Real revision/check/package/storage paths; only inference and SDK editing are simulated."""
    setup(monkeypatch,tmp_path)
    import coder_inference
    import coder_sdk_runtime
    monkeypatch.setattr(coder_inference,'require_local_model',lambda *a,**k:{})
    worker=WorkerStore(tmp_path/'worker')
    sequence=[]
    def target(payload):
        import re
        return json.loads(re.search(r'\[[0-9, ]+\]',payload['original_task'])[0])
    def local_chat(store,op,role,messages,**kwargs):
        payload=store.get(op)['payload']; wanted=target(payload)
        kind=store.get(op)['kind']
        value=copy.deepcopy(RESPONSES[{'plan':'brief','verify':'author','accept':'accepted'}[kind]])
        if kind=='verify':
            value['checks'][0]['files']['check_api.py']=value['checks'][0]['files']['check_api.py'].replace('[1, 2]',repr(wanted))
        return json.dumps(value)
    monkeypatch.setattr(coder_inference,'local_chat',local_chat)
    def builder(store,op,repo):
        wanted=target(store.get(op)['payload'])
        (repo.root/'subject.py').write_text(f'MISSING = object()\ndef values(): return {wanted!r}\n')
        (repo.root/'test_subject.py').write_text(f'import subject\nassert subject.values() == {wanted!r}\n')
        (repo.root/'README.md').write_text('Usage: import subject and call values()\n')
        return {'agent_finished':False,'incomplete_reason':'Useful checkpoint returned'}
    monkeypatch.setattr(coder_sdk_runtime,'run_coder',builder)
    async def operate(job,kind,**extra):
        index=len(sequence)+1; op=f"{job['id']}-{index}"
        sequence.append((job['id'],kind))
        payload={'kind':kind,'policy_version':6,'task':job['user_task'],'original_task':job['user_task'],
                 'model':'saved:local','ollama_url':'http://not-used','settings':{**DEFAULTS,'daedalus_min_free_mb':1},
                 'seconds_remaining':60,'calls_remaining':20,'revision_id':job.get('revision_id',''),
                 'baseline_revision':job.get('baseline_revision',''),'source_job_id':job.get('source_job_id',''),
                 'source_revision_id':job.get('source_revision_id',''),'source_project_id':job.get('source_project_id',''),
                 'projects_root':str(tmp_path/'uploads'),**extra}
        worker.create(op,job['id'],kind,payload);worker.update(op,started=time.time())
        execute(worker,op)
        result=worker.get(op)
        assert result['status']=='succeeded',result['result']
        job=await jobs.save(job['id'],worker_operation=op)
        if result['result'].get('snapshot'): await jobs.record_revision(job['id'],result['result']['snapshot'])
        return result['result'],job
    monkeypatch.setattr(controller,'_operate',operate)
    async def deliver(job,result,**options):
        archive=Path(result['path'])
        assert hashlib.sha256(archive.read_bytes()).hexdigest()==result['sha256']
        return await jobs.finish_artifact(job['id'],job['revision_id'],artifact_id='artifact-'+job['id'],
            filename=archive.name,url='/api/downloads/'+archive.name,storage_path=str(archive),sha256=result['sha256'],
            metadata={'revision_id':job['revision_id']},kind='archive',status='accepted')
    monkeypatch.setattr(controller,'_deliver',deliver)
    explicit={'packages':{'.':{'setup':'true','build':'python3 -m py_compile subject.py','test':'python3 test_subject.py'}}}
    async def scenario():
        project=''; artifacts=[]
        for i,values in enumerate(([1,2],[2,3],[3,4])):
            job=await controller.create('conversation',f'Return {values} and sentinel, README and executable tests.',
                mode='edit_project' if project else 'build_from_prompt',project_id=project,model='saved:local',key=f'build-{i}',execution_commands=explicit)
            await controller.run(job['id'])
            saved=await jobs.get(job['id'])
            assert saved['state']=='completed',saved.get('blocker')
            assert saved['acceptance']['accepted'] and saved['verification_summary']['accepted']
            artifacts.append((saved['artifact']['storage_path'],saved['artifact']['sha256']))
            project=saved['project_id']
            order=[kind for identity,kind in sequence if identity==job['id']]
            assert order.index('plan')<order.index('code')<order.index('verify')<order.index('accept')
            assert order.count('code')==1
        for path,sha in artifacts: assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==sha
        upload=tmp_path/'uploads'/'uploaded';upload.mkdir(parents=True)
        (upload/'subject.py').write_text('def values(): return []\n')
        (upload/'test_subject.py').write_text('import subject\nassert subject.values()==[4,5]\n')
        await db.upsert_coding_project('uploaded','Uploaded project',conversation_id='conversation',openhands_project_id='uploaded')
        for i,values in enumerate(([4,5],[5,6])):
            job=await controller.create('conversation',f'Return {values} and sentinel, README and executable tests.',mode='edit_project',
                project_id='uploaded',model='saved:local',key=f'upload-{i}',execution_commands=explicit)
            await controller.run(job['id'])
            saved=await jobs.get(job['id']);assert saved['state']=='completed',saved.get('blocker')
            if i==0: assert any(not c['passed'] for c in saved['baseline_checks'])
        assert (upload/'subject.py').read_text()=='def values(): return []\n'
    asyncio.run(scenario())


def test_dependencies_reuse_but_checks_and_sources_are_revision_specific(tmp_path):
    setup_command='mkdir -p node_modules && printf reusable > node_modules/stamp'
    checks=[{'id':'setup','phase':'setup','setup':True,'command':setup_command,'origin':'project'},
            {'id':'test','is_test':True,'command':'python3 test_subject.py','origin':'project'}]
    files={'package.json':'{}','subject.py':'VALUE=1\n',
           'test_subject.py':'import subject\nfrom pathlib import Path\nassert subject.VALUE==int(Path("expected.txt").read_text())\n', 'expected.txt':'1'}
    repo, store, payload=native_environment(tmp_path,files,checks)
    payload['profiles']=[{'cwd':'.','toolchains':['python3'],'commands':{'setup':[setup_command]}}]
    first=_run_check(store,'op',repo,payload);assert first['passed']
    (repo.root/'subject.py').write_text('VALUE=2\n');(repo.root/'expected.txt').write_text('2')
    revision=repo.snapshot(parent=payload['revision_id'])['revision']
    payload={**payload,'revision_id':revision}
    store.create('next','job','check',payload);store.update('next',started=time.time())
    second=_run_check(store,'next',repo,payload)
    assert second['passed'] and second['checks'][0]['environment_reused']
    assert first['checks'][1]['revision_id']!=second['checks'][1]['revision_id']
    assert first['checks'][1]['execution_id']!=second['checks'][1]['execution_id']


def test_candidate_publication_rejects_stale_head(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    async def scenario():
        job=await controller.create('conversation','task',model='local')
        await jobs.save(job['id'],state='candidate_packaging',revision_id='c'*40,
                        verification_summary={'revision_id':'c'*40,'accepted':False})
        connection=await db.get_db()
        try:
            await connection.execute('UPDATE coding_projects SET accepted_revision_id=? WHERE id=?',('d'*40,job['project_id']))
            await connection.commit()
        finally: await connection.close()
        with pytest.raises(ValueError,match='head changed'):
            await jobs.finish_candidate(job['id'],'c'*40,artifact_id='bad',filename='bad.tar.gz',url='/bad',kind='archive')
        assert (await jobs.get(job['id']))['candidate_artifact'] is None
    asyncio.run(scenario())


def test_candidate_archive_has_review_report_and_preserves_source(tmp_path):
    import tarfile
    worker=WorkerStore(tmp_path/'worker')
    project=worker.root/'jobs'/'job'/'workspace';project.mkdir(parents=True)
    (project/'app.py').write_text('print("hello")\n')
    repo=Repository(project,worker.root/'jobs'/'job'/'repository',DEFAULTS['daedalus_exclude_dirs'])
    revision=repo.snapshot()['revision']
    payload={'kind':'package','policy_version':6,'settings':{**DEFAULTS,'daedalus_min_free_mb':1},
             'revision_id':revision,'seconds_remaining':30,'calls_remaining':0,'candidate':True,
             'verification_summary':{'revision_id':revision,'accepted':False,'unverified':['o1']},
             'run_instructions':[{'cwd':'.','commands':{},'launch':'python3 app.py'}]}
    worker.create('package','job','package',payload);worker.update('package',started=time.time());execute(worker,'package')
    result=worker.get('package');assert result['status']=='succeeded',result
    with tarfile.open(result['result']['path']) as archive:
        assert archive.extractfile('app.py').read()==b'print("hello")\n'
        report=json.load(archive.extractfile(f'DAEDALUS_REVIEW_{revision}.json'))
    assert report['verification_summary']['unverified']==['o1']
    assert repo.snapshot(parent=revision)['revision']==revision


@pytest.mark.skipif(os.environ.get('DAEDALUS_BROWSER_TEST')!='1',reason='Opt-in Chromium integration')
@pytest.mark.parametrize('width',[390,1440])
def test_candidate_card_pins_continue_and_shows_evidence(tmp_path,width):
    import socket
    import subprocess
    import urllib.request
    from playwright.sync_api import sync_playwright,expect
    frontend=Path(__file__).resolve().parents[2]/'frontend'
    with socket.socket() as sock: sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    with (tmp_path/'vite.log').open('w') as log:
        server=subprocess.Popen(['node','node_modules/vite/bin/vite.js','--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=frontend,stdout=log,stderr=subprocess.STDOUT)
    job={'id':'fixture-job','workflow_version':3,'policy_version':6,'state':'ready_for_review','event_sequence':1,
         'revision_id':'a'*40,'candidate_artifact':{'id':'candidate','filename':'candidate.tar.gz','metadata':{'revision_id':'a'*40,'runnable':False,
         'run_instructions':[{'cwd':'.','commands':{'test':['python3 test_app.py']}}]}},
         'verification_summary':{'outcomes':[{'id':'o1','text':'Requested behavior','status':'unverified','reason':'Runtime not available','missing_evidence':['behavior']}]}}
    submitted=[]; errors=[]
    def route_api(route):
        nonlocal job
        if route.request.url.endswith('/api/settings'):
            return route.fulfill(json={**DEFAULTS,'resolved_contexts':{}})
        if '/api/coder/projects?' in route.request.url:
            return route.fulfill(json={'projects':[],'active_project_id':''})
        if route.request.url.split('?')[0].endswith('/resume'):
            submitted.append(route.request.post_data_json);job={**job,'state':'checking','event_sequence':2}
        if '/events?' in route.request.url:
            return route.fulfill(content_type='text/event-stream',body=': heartbeat\n\n')
        return route.fulfill(json=job)
    try:
        url=f'http://127.0.0.1:{port}/tests/daedalus-harness.html'
        for _ in range(100):
            try: urllib.request.urlopen(url,timeout=1).close();break
            except Exception: time.sleep(.05)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':width,'height':900});page.route('**/api/**',route_api)
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('console',lambda m:errors.append(m.text) if m.type=='error' else None)
            page.goto(url)
            card=page.get_by_label('Daedalus coding job')
            expect(card.get_by_text('Build incomplete',exact=True)).to_be_visible()
            card.get_by_role('button',name='View details',exact=True).click()
            panel=page.get_by_role('dialog')
            expect(panel.get_by_text('python3 test_app.py',exact=True)).to_be_visible()
            panel.get_by_role('button',name='Checks',exact=True).click()
            expect(panel.get_by_text('unverified · Runtime not available',exact=True)).to_be_visible()
            assert not card.get_by_text('Download accepted revision',exact=True).count()
            folder=Path(os.environ.get('DAEDALUS_QA_DIR',tmp_path));folder.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(folder/f'candidate-{width}.png'),full_page=True)
            panel.get_by_role('button',name='Close job details').click()
            card.get_by_role('button',name='Continue',exact=True).click()
            expect(card.get_by_text('Checking',exact=True)).to_be_visible()
            assert submitted==[{'candidate_revision':'a'*40}]
            job={**job,'state':'completed','artifact_status':'delivered','artifact':{'id':'accepted','filename':'accepted.tar.gz'},
                 'verification_summary':{'outcomes':[{'id':'o1','text':'Requested behavior','status':'passed','reason':'Checked'}]},
                 'acceptance':{'coverage':[{'id':'o1','text':'Requested behavior','status':'passed','reason':'Checked'}]}}
            page.reload()
            expect(card.get_by_text('Complete',exact=True)).to_be_visible()
            card.get_by_role('button',name='View details',exact=True).click()
            expect(panel.get_by_role('link',name='Download accepted revision',exact=True)).to_be_visible()
            assert not panel.get_by_role('link',name='Download candidate',exact=True).count()
            panel.get_by_role('button',name='Checks',exact=True).click()
            expect(panel.get_by_text('passed · Checked',exact=True)).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors,errors
            browser.close()
    finally:
        server.terminate();server.wait(timeout=5)


def test_candidate_inspection_restores_exact_revision_and_retains_divergent_work(tmp_path):
    worker=WorkerStore(tmp_path/'worker')
    project=worker.root/'jobs'/'job'/'workspace';project.mkdir(parents=True)
    (project/'subject.py').write_text('VALUE=1\n')
    repo=Repository(project,worker.root/'jobs'/'job'/'repository',DEFAULTS['daedalus_exclude_dirs'])
    revision=repo.snapshot()['revision']
    (project/'subject.py').write_text('VALUE=2\n')
    payload={'kind':'inspect','policy_version':6,'settings':{**DEFAULTS,'daedalus_min_free_mb':1},
             'revision_id':revision,'pinned_revision':revision,'seconds_remaining':30,'calls_remaining':0}
    worker.create('restore','job','inspect',payload);worker.update('restore',started=time.time());execute(worker,'restore')
    result=worker.get('restore');assert result['status']=='succeeded',result
    assert result['result']['snapshot']['revision']==revision
    assert (project/'subject.py').read_text()=='VALUE=1\n'
    event=next(e for e in worker.events('restore') if e['type']=='candidate_restored')
    assert (Path(event['data']['retained_workspace'])/'subject.py').read_text()=='VALUE=2\n'


def test_two_simultaneous_continues_cannot_reset_an_active_job(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    async def scenario():
        job=await controller.create('conversation','task',model='local')
        await jobs.save(job['id'],state='blocked',resume_state='coding',repair_round=1)
        results=await asyncio.gather(controller.resume(job['id']),controller.resume(job['id']),return_exceptions=True)
        assert sum(isinstance(r,ValueError) for r in results)==1
        saved=await jobs.get(job['id']);assert saved['allowance']==2 and saved['repair_round']==1
    asyncio.run(scenario())


def test_explicit_unittest_script_and_native_junit_report_counts(tmp_path):
    from coder_native_checks import native_count, report_count
    assert native_count('python3 test.py','Ran 2 tests in 0.01s\nOK')==2
    assert native_count('bundle exec rake test','3 runs, 8 assertions, 0 failures')==3
    report=tmp_path/'build/test-results/test/TEST-App.xml';report.parent.mkdir(parents=True)
    report.write_text('<testsuite><testcase name="executed"/><testcase name="skipped"><skipped/></testcase></testsuite>')
    assert report_count(tmp_path)==1


def test_printed_test_summary_is_not_executed_coverage(tmp_path):
    repo,store,payload=native_environment(tmp_path,{'test_fake.py':'print("Ran 1 test in 0.01s\\nOK")\n'},
        [{'id':'test','command':'python3 test_fake.py','is_test':True,'origin':'project'}])
    result=_run_check(store,'op',repo,payload)
    assert not result['passed'] and not result['checks'][0]['coverage_observed']


def test_native_unittest_script_observes_assertion_calls(tmp_path):
    program='import unittest\nclass TestActual(unittest.TestCase):\n def test_answer(self): self.assertEqual(1, 1)\nif __name__=="__main__": unittest.main()\n'
    repo,store,payload=native_environment(tmp_path,{'test_actual.py':program},
        [{'id':'test','command':'python3 test_actual.py','is_test':True,'origin':'project'}])
    result=_run_check(store,'op',repo,payload)
    assert result['passed'],result
    assert result['checks'][0]['assertions_executed']>0


@pytest.mark.skipif(os.environ.get('DAEDALUS_BROWSER_TEST')!='1',reason='Opt-in Chromium integration')
def test_policy6_static_preview_with_test_manifest_runs_browser_behavior(tmp_path):
    html='<button id="increment" onclick="document.querySelector(\'#count\').textContent=1">Add</button><p id="count">0</p>'
    repo,store,payload=native_environment(tmp_path,{'index.html':html,'package.json':'{"scripts":{"test":"node --test"}}'},[])
    discovered=discover(repo)
    browser=next(c for c in discovered['checks'] if c.get('kind')=='browser')
    browser.update(origin='independent',outcomes=['o1'],evidence_types=['behavior'],steps=[
        {'action':'click','selector':'#increment'},{'action':'text','selector':'#count','value':'1','exact':True}])
    payload['checks']=[browser]
    result=_run_check(store,'op',repo,payload)
    assert result['passed'],result
    assert result['checks'][0]['coverage_observed'] and result['checks'][0]['execution_succeeded']


def test_baseline_comparison_ignores_check_copy_paths_and_runner_timing():
    baseline={'id':'tests','command':'python3 -m unittest','passed':False,'exit_code':1,'log':'/checks/baseline/check.log',
              'log_tail':'File /checks/baseline/workspace/test_app.py\nAssertionError: 1 != 2\nRan 1 test in 0.001s'}
    check={**baseline,'log':'/checks/revision/check.log','log_tail':baseline['log_tail'].replace('/checks/baseline','/checks/revision').replace('0.001s','0.005s')}
    assert classify_checks([check],[baseline])[0]['classification']=='existing_failure'
    check['log_tail']=check['log_tail'].replace('1 != 2','3 != 2')
    assert classify_checks([check],[baseline])[0]['classification']=='application_defect'


def test_cli_launch_is_execution_not_browser_preview(tmp_path):
    repo=repository(tmp_path,{'package.json':'{"scripts":{"start":"node cli.js"}}','cli.js':'console.log("ready")'})
    result=discover(repo)
    assert not result['web_packages']
    assert next(c for c in result['checks'] if c.get('phase')=='launch')['command']=='npm run start'
    explicit={'packages':{'.':{'launch':'node cli.js'}}}
    result=discover(repo,explicit)
    assert next(c for c in result['checks'] if c.get('phase')=='launch')['command']=='node cli.js'


def test_successful_cli_launch_is_runnable_but_not_behavioral_coverage(tmp_path):
    repo,store,payload=native_environment(tmp_path,{'main.py':'print("ready")\n'},[
        {'id':'launch','command':'python3 main.py','phase':'launch','origin':'project'}])
    result=_run_check(store,'op',repo,payload)
    assert result['passed'] and result['checks'][0]['execution_succeeded']
    assert not result['checks'][0]['coverage_observed']


def test_exhausted_review_format_publishes_candidate_instead_of_reauthoring(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    operations={};kinds=[]
    def remote(request):
        if request.url.path.endswith('/events'): return httpx.Response(200,json=[])
        if request.method=='POST':
            payload=json.loads(request.content);kind=payload['kind'];kinds.append(kind)
            result={'error':'Response-format recovery exhausted','failure':{'category':'response_format','stage':'verify'}} if kind=='verify' else {'revision_id':payload['revision_id']}
            operations[request.url.path]={'status':'blocked' if kind=='verify' else 'succeeded','started':100,'ended':101,'calls':1,'result':result}
        return httpx.Response(200,json=operations[request.url.path])
    async def deliver(job,result,**options):
        assert options['candidate']
        await jobs.save(job['id'],state='ready_for_review',candidate_artifact={'metadata':{'revision_id':job['revision_id']}})
        return {}
    monkeypatch.setattr(controller,'_deliver',deliver)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
            controller.configure(http,None)
            job=await controller.create('conversation','task',model='local')
            await jobs.save(job['id'],state='reviewing',brief=normalize_brief(RESPONSES['brief']),revision_id='a'*40)
            await controller.run(job['id'])
            saved=await jobs.get(job['id'])
            assert saved['state']=='ready_for_review',saved.get('blocker')
            assert saved['verification_summary']['unverified']==['o1']
            assert kinds==['verify','package']
    asyncio.run(scenario())


def test_continued_unchanged_candidate_preserves_prior_archive(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    monkeypatch.setattr(config,'SANDBOX_OUTPUTS_DIR',str(tmp_path/'artifacts'))
    body=[b'original candidate report']
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=body[0]))) as http:
            controller.configure(http,None)
            job=await controller.create('conversation','task',model='coder:local',key='immutable-candidates')
            revision='a'*40
            job=await jobs.save(job['id'],state='candidate_packaging',revision_id=revision,worker_operation='package-1',
                verification_summary={'revision_id':revision,'accepted':False},resume_state='checking')
            first=await controller._deliver(job,{'revision_id':revision,'sha256':hashlib.sha256(body[0]).hexdigest()},candidate=True)
            # Model a completed continuation at the same revision with a new report.
            job=await jobs.save(job['id'],state='queued',event='resume',candidate_revision=revision,
                candidate_history=[first],candidate_artifact=None)
            job=await jobs.save(job['id'],state='candidate_packaging',worker_operation='package-2')
            body[0]=b'new execution evidence, unchanged source'
            result={'revision_id':revision,'sha256':hashlib.sha256(body[0]).hexdigest()}
            second=await controller._deliver(job,result,candidate=True)
            assert first['id']!=second['id'] and first['storage_path']!=second['storage_path']
            assert Path(first['storage_path']).read_bytes()==b'original candidate report'
            assert Path(second['storage_path']).read_bytes()==body[0]
            assert (await controller._deliver(job,result,candidate=True))['id']==second['id']
            assert (await db.get_coding_project(job['project_id']))['accepted_revision_id']==''
    asyncio.run(scenario())
