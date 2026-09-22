"""Project continuity, evidence gating, and optional vision contracts."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tarfile
import time
import shlex
import sys

import pytest

import coder_jobs as controller
import config
import database as db
from context_policy import DEFAULTS
from db import coder_jobs as store
from coder_worker_runtime import WorkerStore,execute
from coder_verification import validate_probes,validate_acceptance,test_count as count_tests


def setup(monkeypatch,tmp_path):
    # These fixtures exercise the persisted pre-policy-6 controller contract.
    monkeypatch.setattr(controller, 'POLICY_VERSION', 5)
    monkeypatch.setattr(db,"DATABASE_PATH",str(tmp_path/"jobs.db"))
    monkeypatch.setattr(config,"CONTEXT_SETTINGS",{**DEFAULTS,"daedalus_v3_enabled":True})
    monkeypatch.setattr(controller,"spawn",lambda *a:None)


def test_project_follows_accepted_head_and_fences_old_jobs(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    async def scenario():
        await db.init_db(); await db.create_conversation("conversation",model="coder:local")
        build=await controller.create("conversation","Build a calculator",model="coder:local",key="one")
        assert build["project_id"].startswith("cp-") and build["project_id"]!=build["id"]
        assert not build["visual_policy"]["enabled"]
        assert build["visual_policy"]["model_inherited"]
        with pytest.raises(ValueError,match="no accepted revision"):
            await controller.create("conversation","Change it",mode="edit_project",model="coder:local")
        await store.save(build["id"],state="packaging",revision_id="a"*40,acceptance={"accepted":True,"revision_id":"a"*40})
        artifact=await store.finish_artifact(build["id"],"a"*40,artifact_id="first",filename="first.tar.gz",kind="archive",url="/first",status="accepted")
        project=await db.get_coding_project_by_conv("conversation")
        assert project["id"]==build["project_id"] and project["accepted_artifact_id"]==artifact["id"]
        await db.create_conversation("second",model="coder:local")
        await db.select_coding_project("second",project["id"])
        edit=await controller.create("second","Add subtraction",mode="edit_project",model="coder:local",key="two")
        assert edit["source_job_id"]==build["id"] and edit["parent_artifact_id"]==artifact["id"]
        with pytest.raises(ValueError,match="active job"):
            await controller.create("conversation","Add multiply",mode="edit_project",model="coder:local",key="three")
        await store.save(edit["id"],state="blocked",resume_state="coding")
        newer=await controller.create("conversation","Add multiply",mode="edit_project",model="coder:local",key="three")
        await store.save(newer["id"],state="packaging",revision_id="b"*40,acceptance={"accepted":True,"revision_id":"b"*40})
        await store.finish_artifact(newer["id"],"b"*40,artifact_id="next",filename="next.tar.gz",kind="archive",url="/next",status="accepted")
        with pytest.raises(ValueError,match="newer accepted revision"):
            await controller.resume(edit["id"])
        assert (await db.get_coding_project_by_conv("second"))["accepted_revision_id"]=="b"*40
        assert (await store.get(build["id"]))["artifact"]["id"]=="first"
        assert len(await store.project_history(project["id"]))==3
        token=db.set_current_user_id("stranger")
        try:
            assert await db.get_coding_project(project["id"]) is None
            with pytest.raises(LookupError): await store.project_history(project["id"])
        finally: db.reset_current_user_id(token)
    asyncio.run(scenario())


def worker_payload(**extra):
    return {"settings":dict(DEFAULTS),"kind":"inspect","request_key":"request","policy_version":2,
        "task":"task","original_task":"task","seconds_remaining":60,"calls_remaining":10,**extra}


def test_restore_accepted_source_without_old_worker_workspace(tmp_path):
    worker=WorkerStore(tmp_path/"state")
    source=tmp_path/"accepted";source.mkdir();(source/"answer.py").write_text("answer=42\n")
    bundle=tmp_path/"accepted.tar.gz"
    with tarfile.open(bundle,"w:gz") as archive: archive.add(source/"answer.py",arcname="answer.py")
    sha=hashlib.sha256(bundle.read_bytes()).hexdigest()
    destination=worker.root/"sources"/"cw3-abc"/(sha+".tar.gz");destination.parent.mkdir(parents=True);destination.write_bytes(bundle.read_bytes())
    worker.create("operation","cw3-abc","inspect",worker_payload(source_job_id="cw3-def",source_revision_id="a"*40,source_archive_sha256=sha))
    worker.update("operation",started=time.time());execute(worker,"operation")
    result=worker.get("operation")
    assert result["status"]=="succeeded",result
    assert (worker.root/"jobs"/"cw3-abc"/"workspace"/"answer.py").read_text()=="answer=42\n"


@pytest.mark.parametrize("ui,enabled,reason",[(False,True,"browser UI"),(True,False,"disabled")])
def test_optional_vision_does_not_contact_models(tmp_path,monkeypatch,ui,enabled,reason):
    from coder_visual import review
    import coder_inference
    monkeypatch.setattr(coder_inference,"require_local_model",lambda *a:pytest.fail("Vision must not call a model"))
    worker=WorkerStore(tmp_path/"state")
    worker.create("op","job","visual",worker_payload(ui_required=ui,visual_policy={"enabled":enabled}))
    result=review(worker,"op")
    assert result["status"]=="skipped" and reason in result["reason"]


def test_text_only_model_never_receives_screenshots(tmp_path,monkeypatch):
    from coder_visual import review
    import coder_inference
    monkeypatch.setattr(coder_inference,"require_local_model",lambda *a:{"capabilities":["completion","tools"]})
    monkeypatch.setattr(coder_inference,"local_chat",lambda *a,**k:pytest.fail("Do not send screenshots to a text model"))
    worker=WorkerStore(tmp_path/"state")
    worker.create("op","job","visual",worker_payload(ui_required=True,model="text:local",ollama_url="local",revision_id="a",
        visual_policy={"enabled":True},evidence={"checks":[{"revision_id":"a","screenshots":[{}]}]}))
    assert "does not support vision" in review(worker,"op")["reason"]


def test_independent_probes_require_real_assertions():
    requirements=[{"id":"api","kind":"behavior"}]
    probe={"id":"test","requirement_ids":["api"],"runner":"node","program":"console.assert(false)"}
    with pytest.raises(ValueError,match="throwing"): validate_probes({"checks":[probe]},requirements)
    validate_probes({"checks":[{**probe,"program":"import assert from 'node:assert/strict'; assert.equal(1, 1);"}]},requirements)
    with pytest.raises(ValueError,match="Invalid data fixture"):
        validate_probes({"checks":[{**probe,"program":"const items = [{__proto__: 'value'}]; if (!items[0].__proto__) throw Error();"}]},requirements)
    validate_probes({"checks":[{**probe,"program":"import assert from 'node:assert/strict'; const value = {['__proto__']: 'data'}; assert.equal(value.__proto__, 'data');"}]},requirements)
    with pytest.raises(ValueError,match="every nonvisual"): validate_probes({"checks":[]},requirements)
    assert count_tests("python -m unittest discover","Ran 0 tests in 0.000s\nOK")==0
    assert count_tests("pytest -q","no tests ran in 0.02s")==0


def test_acceptance_cannot_invent_or_reuse_evidence():
    requirements=[{"id":"api","kind":"behavior"}]
    answer={"accepted":True,"coverage":[{"requirement_id":"api","status":"passed","reason":"Test passed","check_ids":["probe"]}]}
    check={"id":"probe","revision_id":"a","passed":True,"origin":"independent","requirement_ids":["api"]}
    validate_acceptance(answer,requirements,[check],"a",None)
    with pytest.raises(ValueError,match="stale"):validate_acceptance(answer,requirements,[check],"b",None)
    with pytest.raises(ValueError,match="passing independent"):validate_acceptance(answer,requirements,[{**check,"passed":False}],"a",None)
    with pytest.raises(ValueError,match="explicitly requested visual"):
        validate_acceptance(answer,[{"id":"api","kind":"visual"}],[check],"a",None,visual={"status":"skipped"})


def test_probe_correction_cannot_drop_requirements():
    from coder_verification import validate_probe_correction
    old={"checks":[{"id":"probe","runner":"python","program":"assert 1 == 1","requirement_ids":["r"]}]}
    new={"checks":[{**old["checks"][0],"program":"assert 2 == 2"}]}
    with pytest.raises(ValueError,match="explain its defect"):
        validate_probe_correction(new,[{"id":"r","kind":"behavior"}],old,"Preserve the input")
    new["corrections"]=[{"check_id":"probe","reason":"Wrong fixture construction","source_quote":"Preserve the input"}]
    validate_probe_correction(new,[{"id":"r","kind":"behavior"}],old,"Preserve the input")
    with pytest.raises(ValueError):validate_probe_correction({"checks":[]},[{"id":"r","kind":"behavior"}],old,"Preserve the input")


def test_project_tests_and_language_experiments_preserve_meaning():
    from coder_worker_runtime import javascript_experiment
    from coder_verification import probe_checks
    probes={"checks":[{"id":"tests","requirement_ids":["tests"],"runner":"project_tests"}]}
    validate_probes(probes,[{"id":"tests","kind":"tests"}])
    assert probe_checks(probes['checks'])[0]['kind']=='project_tests'
    with pytest.raises(ValueError,match='only verify requested tests'):
        validate_probes(probes,[{"id":"tests","kind":"behavior"}])
    result=javascript_experiment("({colon:Object.keys({__proto__:'value'}),computed:Object.keys({['__proto__']:'value'}),process:typeof process,require:typeof require})",5,4000)
    assert result['experiment']['value']=={'colon':[],'computed':['__proto__'],'process':'undefined','require':'undefined'}


def test_repair_evidence_fits_once_with_tool_pairs_preserved():
    from coder_sdk_runtime import pin_task_text,compile_context
    from context_policy import resolve,DEFAULTS,estimate_tokens
    policy=resolve(settings={**DEFAULTS,'openhands_num_ctx':6000,'generation_num_predict':256,'daedalus_compaction':'on'})
    repair='Current assertion failure and original requirements. '*200
    messages=[{'role':'system','content':'Use project tools. '*60},
              {'role':'user','content':'Original request'},
              {'role':'assistant','content':'Old execution evidence. '*1200},
              {'role':'user','content':'Check the original attempt'},
              {'role':'assistant','content':'Saved a source checkpoint'},
              {'role':'user','content':[{'type':'text','text':repair}]},
              {'role':'assistant','tool_calls':[{'id':'read-current','function':{'name':'read','arguments':'{}'}}]},
              {'role':'tool','tool_call_id':'read-current','content':'Current file contents'}]
    with pytest.raises(ValueError,match='exceed.*context'):
        compile_context(pin_task_text(messages,repair),[],policy,lambda _: 'Prior execution')
    compiled,compacted=compile_context(pin_task_text(messages,repair,deduplicate=True),[],policy,lambda _: 'Prior execution')
    assert compacted and estimate_tokens({'messages':compiled,'tools':[]})<=policy.input_budget
    assert compiled[1]['content']==repair and compiled[-1]['tool_call_id']=='read-current'
    assert compiled[-2]['tool_calls'][0]['id']=='read-current'


@pytest.mark.parametrize('inherited',[False,True])
def test_visual_batches_use_only_selected_local_model(tmp_path,monkeypatch,inherited):
    from coder_visual import review
    from coder_evidence import register,resolve
    import coder_inference
    worker=WorkerStore(tmp_path/"state")
    directory=worker.root/"checks"/"job";directory.mkdir(parents=True)
    screenshots=[]
    for index in range(3):
        path=directory/f"{index}.png";path.write_bytes(b"image fixture"+bytes([index]))
        screenshots.append({"viewport":"desktop","evidence":register(worker,"job",path,revision="a",kind="screenshot")})
    with pytest.raises(OSError):resolve(worker,"different-job",screenshots[0]["evidence"]["id"])
    calls=[]
    monkeypatch.setattr(coder_inference,"require_local_model",lambda *a:{"capabilities":["vision"]})
    def chat(store,op,role,messages,**kwargs):
        calls.append((role,messages,kwargs));return '{"findings":[]}'
    monkeypatch.setattr(coder_inference,"local_chat",chat)
    worker.create("op","job","visual",worker_payload(ui_required=True,model="text:local",ollama_url="local",revision_id="a",
        settings={**DEFAULTS,'daedalus_visual_model':'vision:local' if inherited else 'different:local'},
        visual_policy={"enabled":True,"model":"old:local" if inherited else "vision:local",'model_inherited':inherited},
        evidence={"checks":[{"revision_id":"a","screenshots":screenshots}]}))
    assert review(worker,"op")["status"]=="passed"
    assert [len(call[1][0]["images"]) for call in calls]==[2,1]
    assert all(call[2]["model"]=="vision:local" for call in calls)


def test_explicit_visual_requirement_waits_without_editing(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    async def operation(job,kind,**extra):
        assert kind=="visual"
        return {"status":"skipped","reason":"disabled"},job
    monkeypatch.setattr(controller,"_operate",operation)
    async def scenario():
        await db.init_db();await db.create_conversation("conversation",model="coder:local")
        job=await controller.create("conversation","Visually verify the layout",model="coder:local")
        await store.save(job["id"],state="visual_review",requirements=[{"id":"r","kind":"visual"}],ui_required=True)
        await controller.run(job["id"])
        current=await store.get(job["id"])
        assert current["state"]=="waiting_for_input" and current["resume_state"]=="visual_review"
        await store.save(job['id'],visual_attempt=3,probe_corrections=3,probe_diagnoses=['previous'])
        continued=await controller.resume(job['id'],visual_review=True)
        assert continued['visual_policy']['enabled'] and continued['visual_attempt']==0
        assert continued['probe_corrections']==0 and continued['probe_diagnoses']==[]
    asyncio.run(scenario())


def test_controller_runs_independent_browser_probe_and_optional_vision(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path);operations=[]
    async def operation(job,kind,**extra):
        operations.append(kind)
        if kind=='inspect':result={'inventory':{'files':0},'snapshot':{'revision':'a'*40},'workspace':'fixture'}
        elif kind=='plan':result={'requirements':[{'id':'r','kind':'behavior','text':'Build UI','source_quote':'Build UI'}],
            'ui_required':True,'milestones':[{'id':'m','task':'Build UI','requirement_ids':['r']}]}
        elif kind=='verify':result={'checks':[{'id':'probe','runner':'browser','requirement_ids':['r'],
            'steps':[{'action':'text','selector':'h1','value':'Hello'}]}]}
        elif kind=='code':result={'snapshot':{'revision':'b'*40},'inventory':{'files':1},'agent_finished':True,
            'verification':{'checks':[{'id':'preview','kind':'browser','server_command':'python3 -m http.server {port} --bind 127.0.0.1','steps':[]}]}}
        elif kind=='check':
            independent=next(c for c in extra['checks'] if c.get('origin')=='independent')
            assert independent['server_command'].startswith('python3 -m http.server')
            result={'passed':True,'checks':[{**c,'passed':True,'revision_id':job['revision_id']} for c in extra['checks']]}
        elif kind=='visual':result={'status':'skipped','reason':'disabled'}
        elif kind=='accept':result={'accepted':True,'revision_id':job['revision_id']}
        elif kind=='package':result={'revision_id':job['revision_id']}
        else:raise AssertionError(kind)
        return result,job
    async def deliver(job,result):return await store.save(job['id'],state='completed',artifact={'id':'fixture'})
    monkeypatch.setattr(controller,'_operate',operation);monkeypatch.setattr(controller,'_deliver',deliver)
    async def scenario():
        await db.init_db();await db.create_conversation('conversation',model='coder:local')
        job=await controller.create('conversation','Build UI',model='coder:local')
        await controller.run(job['id'])
        assert (await store.get(job['id']))['state']=='completed'
        assert operations==['inspect','plan','verify','code','check','visual','accept','package']
    asyncio.run(scenario())


def test_image_accounting_and_thinking_capabilities(monkeypatch,tmp_path):
    import requests
    import coder_inference
    worker=WorkerStore(tmp_path/'state')
    worker.create('op','job','visual',worker_payload(model='vision:local',ollama_url='local'))
    worker.update('op',started=time.time())
    monkeypatch.setattr(coder_inference,'require_local_model',lambda *args:{'capabilities':['vision','thinking']})
    monkeypatch.setattr(coder_inference,'ensure_context',lambda *args:None)
    sent=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def raise_for_status(self):pass
        def iter_lines(self):yield b'{"message":{"content":"ok"},"done":true,"prompt_eval_count":2100,"eval_count":1}'
    def post(url,**kwargs):sent.append(kwargs['json']);return Response()
    monkeypatch.setattr(requests,'post',post)
    assert coder_inference.local_chat(worker,'op','visual',[{'role':'user','content':'Review','images':['A'*200000]}])=='ok'
    assert sent[0]['think'] is False and worker.get('op')['calls']==1
    call=next(e for e in worker.events('op') if e['type']=='model_call')
    assert call['data']['image_count']==1 and call['data']['prompt_tokens_estimate']<3000


def test_worker_source_upload_and_evidence_are_bounded(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from coder_worker_runtime import install_routes
    monkeypatch.setenv("DAEDALUS_STATE_DIR",str(tmp_path/"state"))
    app=FastAPI();install_routes(app,tmp_path/"projects")
    archive=tmp_path/"source.tar.gz"
    source=tmp_path/"source.py";source.write_text('print("hello")')
    with tarfile.open(archive,"w:gz") as bundle:bundle.add(source,arcname="source.py")
    data=archive.read_bytes();sha=hashlib.sha256(data).hexdigest()
    with TestClient(app) as client:
        url=f"/jobs/cw3-abc/source/{sha}"
        assert client.get(url).status_code==404
        assert client.put(url,content=data).status_code==200
        assert client.get(url).status_code==200
        assert client.put('/jobs/cw3-abc/source/'+'0'*64,content=data).status_code==422
        assert client.get('/jobs/cw3-abc/evidence/'+'a'*64).status_code==404


@pytest.mark.parametrize('command,marker',[
    (shlex.quote(sys.executable)+' -c '+shlex.quote('import unittest; unittest.TextTestRunner().run(unittest.TestSuite())'),'test_count'),
    ('node --test test.mjs','assertion_failure'),
])
def test_success_exit_does_not_hide_empty_tests_or_failed_assertions(tmp_path,command,marker):
    from coder_repository import Repository
    from coder_worker_runtime import _run_check
    worker=WorkerStore(tmp_path/'state');root=tmp_path/'project';root.mkdir()
    (root/'test_empty.py').write_text('# no tests\n')
    (root/'test.mjs').write_text('console.assert(false, "wrong behavior");\n')
    repo=Repository(root,tmp_path/'repo');revision=repo.snapshot()['revision']
    payload=worker_payload(revision_id=revision,checks=[{'id':'tests','command':command,'is_test':True}])
    worker.create('op','job','check',payload);worker.update('op',started=time.time())
    result=_run_check(worker,'op',repo,payload)
    assert not result['passed'] and result['checks'][0]['exit_code']==0
    assert result['checks'][0][marker]==(0 if marker=='test_count' else True)


def test_fixed_preview_port_fails_before_launch(tmp_path,monkeypatch):
    import coder_browser
    monkeypatch.setattr(coder_browser.subprocess,'Popen',lambda *a,**k:pytest.fail('Invalid preview launched'))
    with pytest.raises(ValueError,match='must use .*port'):
        coder_browser.browser_check(tmp_path,{'server_command':'python3 -m http.server 8080'},tmp_path/'evidence',600)


def test_promotion_needs_all_distinct_fixtures_and_followup_success():
    from evals.run_coder_benchmark import promotion_result
    from evals.coder_fixtures import FIXTURES
    rows=[{'fixture':f['id'],'passed':True,'false_acceptance':False} for f in FIXTURES]
    assert not promotion_result(rows,rows)['promotable']
    next(r for r in rows if r['fixture']=='grouping')['followups']={'passed':True,
        'original_artifact_preserved':True,'requests':[{'evidence':{'passed':True}} for _ in range(2)]}
    assert promotion_result(rows,rows)['promotable']
    assert not promotion_result(rows,[rows[0]]*12)['promotable']
    assert not promotion_result([rows[0]]*12,rows)['promotable']
    assert not promotion_result(rows,[])['promotable']
    for passed in (9,10,11):
        failed=[{**r,'passed':i<passed} for i,r in enumerate(rows)]
        assert not promotion_result(failed,rows)['promotable']


def test_baseline_matches_legacy_workflow_model_fixture_and_allowances():
    from evals.run_coder_benchmark import matching_baseline
    current={'settings':dict(DEFAULTS),'models':{'local':{'digest':'model-sha'}},'source_hashes':{'evals/coder_fixtures.py':'fixture-sha'}}
    baseline={**current,'workflow_version':2}
    assert matching_baseline(baseline,current)
    assert not matching_baseline({**baseline,'workflow_version':3},current)
    assert not matching_baseline({**baseline,'models':{'local':{'digest':'different'}}},current)
    assert not matching_baseline({**baseline,'source_hashes':{}},current)
    assert not matching_baseline({**baseline,'settings':{**DEFAULTS,'generation_num_predict':8192}},current)


@pytest.mark.skipif(os.environ.get("DAEDALUS_BROWSER_TEST")!="1",reason="Opt-in real browser")
def test_browser_failure_keeps_trace_and_desktop_mobile_behavior(tmp_path):
    from coder_browser import browser_check
    (tmp_path/"index.html").write_text('''<!doctype html><link rel="icon" href="data:,"><title>Fixture</title>
        <button onclick="localStorage.setItem('count','1');document.querySelector('output').textContent='1'">Add</button>
        <output></output><span id="empty"></span><script>document.querySelector('output').textContent=localStorage.getItem('count')||'0'</script>''')
    check={"server_command":"python3 -m http.server {port} --bind 127.0.0.1","path":"index.html","steps":[
        {"action":"exists","selector":"#empty"},
        {"action":"click","selector":{"role":"button","name":"Add"}},
        {"action":"reload"},{"action":"text","selector":"output","value":"1"}]}
    result=browser_check(tmp_path,check,tmp_path/"evidence",30,step_timeout=1)
    assert result["passed"],result
    assert [s["viewport"] for s in result["screenshots"]]==["desktop","mobile"]
    assert len(result["traces"])==2 and all(Path(t["path"]).is_file() for t in result["traces"])
    result=browser_check(tmp_path,{**check,"steps":[{"action":"visible","selector":"#missing"}]},tmp_path/"failed",15,step_timeout=1)
    assert not result["passed"] and result["screenshots"] and result["traces"] and result["timeline"][-1]["status"]=="failed"
