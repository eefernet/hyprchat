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


def setup(monkeypatch,tmp_path):
    monkeypatch.setenv("DAEDALUS_ENGINE_URL","http://worker:8587")
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
        with pytest.raises(ValueError,match="no delivered build"):
            await controller.create("conversation","Change it",mode="edit_project",model="coder:local")
        await store.save(build["id"],state="packaging",revision_id="a"*40,acceptance={"accepted":True,"revision_id":"a"*40},verification_summary={'evidence_version':4,'accepted':True,'revision_id':'a'*40})
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
        await store.save(newer["id"],state="packaging",revision_id="b"*40,acceptance={"accepted":True,"revision_id":"b"*40},verification_summary={'evidence_version':4,'accepted':True,'revision_id':'b'*40})
        await store.finish_artifact(newer["id"],"b"*40,artifact_id="next",filename="next.tar.gz",kind="archive",url="/next",status="accepted")
        with pytest.raises(ValueError,match="newer version"):
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
    return {"engine":"openhands159","settings":dict(DEFAULTS),"kind":"inspect","request_key":"request","policy_version":2,
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


def test_image_accounting_and_thinking_capabilities(monkeypatch,tmp_path):
    import requests
    import coder_inference
    worker=WorkerStore(tmp_path/'state')
    worker.create('op','job','check',worker_payload(model='vision:local',ollama_url='local'))
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


def test_fixed_preview_port_fails_before_launch(tmp_path,monkeypatch):
    import coder_browser
    monkeypatch.setattr(coder_browser.subprocess,'Popen',lambda *a,**k:pytest.fail('Invalid preview launched'))
    with pytest.raises(ValueError,match='must use .*port'):
        coder_browser.browser_check(tmp_path,{'server_command':'python3 -m http.server 8080'},tmp_path/'evidence',600)


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

from coder_verification import test_count as count_tests
