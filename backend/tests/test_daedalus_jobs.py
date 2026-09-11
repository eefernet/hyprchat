import asyncio
import hashlib
import json

import httpx
import pytest

import config
import database as db
import coder_jobs as controller
from db import coder_jobs as store
from context_policy import DEFAULTS


def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'jobs.db'))
    monkeypatch.setattr(config, 'CONTEXT_SETTINGS', {**DEFAULTS, 'daedalus_v3_enabled': True})
    monkeypatch.setattr(controller, 'spawn', lambda *args: None)
    async def initialize():
        await db.init_db()
        await db.create_conversation('conversation', model='coder:local')
    asyncio.run(initialize())


def test_job_identity_owner_fencing_and_event_snapshot(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        first = await store.create('conversation','task','build_from_prompt','','coder:local','request')
        same = await store.create('conversation','task','build_from_prompt','','coder:local','request')
        assert first['id'] == same['id']
        with pytest.raises(ValueError, match='Idempotency'):
            await store.create('conversation','different','build_from_prompt','','coder:local','request')
        job = await store.save(first['id'], state='coding', milestone_index=2)
        events = await store.events(first['id'])
        assert job['event_sequence'] == events[-1]['seq']
        token = db.set_current_user_id('someone-else')
        try:
            assert await store.get(first['id']) is None
            assert await store.request_cancel(first['id']) is None
        finally:
            db.reset_current_user_id(token)
        assert (await store.get(first['id']))['state'] == 'coding'
        await store.request_cancel(first['id'])
        with pytest.raises(InterruptedError):
            await store.save(first['id'], state='completed', artifact={'id':'invalid'})
        await store.save(first['id'], state='cancelled')
        with pytest.raises(ValueError, match='terminal'):
            await store.save(first['id'], state='coding')
    asyncio.run(scenario())


def test_same_operation_reconnects_and_accounts_once(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    operations = {}
    def remote(request):
        if request.url.path.endswith('/events'):
            return httpx.Response(200,json=[])
        if request.method == 'POST':
            payload = json.loads(request.content)
            operations.setdefault(request.url.path, {'status':'succeeded', 'started':100, 'ended':110, 'calls':2,
                'result':{'milestones':[{'task':'implement', 'checks':[{'command':'true'}]}]}})
        return httpx.Response(200,json=operations[request.url.path])
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
            controller.configure(http,None)
            job=await store.create('conversation','task','build_from_prompt','','coder:local','request')
            job=await store.save(job['id'],state='planning')
            result,job=await controller._operate(job,'plan')
            _,again=await controller._operate(job,'plan')
            assert len(operations)==1
            assert again['calls_used']==2 and again['seconds_used']==10
            runs=await db.get_runs_by_conversation('conversation', limit=-1)
            assert len(runs)==1 and runs[0]['workflow_id']==job['id']
            assert runs[0]['status']=='succeeded'
    asyncio.run(scenario())


def test_interrupted_coding_blocks_at_checkpoint_and_requires_resume(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    calls=[]
    def remote(request):
        if request.url.path.endswith('/events'):return httpx.Response(200,json=[])
        calls.append(request.method)
        return httpx.Response(200,json={'status':'interrupted','started':100,'ended':130,'calls':4,
            'result':{'error':'Worker interrupted','snapshot':{'revision':'a'*40,'tree':'b'*40}}})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
            controller.configure(http,None)
            job=await store.create('conversation','task','build_from_prompt','','coder:local','request')
            await store.save(job['id'],state='coding',milestones=[{'task':'edit','checks':[{'command':'true'}]}])
            await controller.run(job['id'])
            stopped=await store.get(job['id'])
            assert stopped['state']=='blocked' and stopped['revision_id']=='a'*40
            assert stopped['calls_used']==4
            assert calls==['POST','GET']
            continued=await controller.resume(job['id'])
            assert continued['allowance']==2 and continued['calls_used']==0
            assert continued['resume_after_inspect']=='coding'
    asyncio.run(scenario())


def test_artifact_cannot_use_stale_acceptance(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    async def scenario():
        with pytest.raises(ValueError,match='accepted revision'):
            await controller._deliver({'revision_id':'new','acceptance':{'accepted':True,'revision_id':'old'}}, {'revision_id':'new'})
    asyncio.run(scenario())


def test_continue_after_failed_inspection_does_not_loop(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    async def scenario():
        job=await store.create('conversation','task','build_from_prompt','','coder:local','request')
        await store.save(job['id'],state='blocked',resume_state='inspecting',acceptance_attempt=3)
        continued=await controller.resume(job['id'])
        assert not continued['resume_after_inspect']
        assert continued['acceptance_attempt']==0
    asyncio.run(scenario())


def test_publication_is_atomic_idempotent_and_cancel_fenced(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    async def scenario():
        async def prepared(key):
            job=await store.create('conversation','task','build_from_prompt','','coder:local',key)
            return await store.save(job['id'],state='packaging',revision_id='revision',acceptance={'accepted':True,'revision_id':'revision'})
        first=await prepared('first')
        fields={'artifact_id':'test-accepted','filename':'project.tar.gz','url':'/api/downloads/project.tar.gz',
                'kind':'archive','status':'accepted','metadata':{'revision_id':'revision'}}
        artifact=await store.finish_artifact(first['id'],'revision',**fields)
        assert artifact['id']=='test-accepted'
        done=await store.get(first['id'])
        assert done['state']=='completed' and done['artifact']['id']==artifact['id']
        assert (await db.get_coding_project(done['project_id']))['openhands_project_id']==first['id']
        assert (await store.finish_artifact(first['id'],'revision',**fields))['id']==artifact['id']
        second=await prepared('second')
        await store.request_cancel(second['id'])
        with pytest.raises(InterruptedError):
            await store.finish_artifact(second['id'],'revision',**{**fields,'artifact_id':'cancelled-artifact'})
        assert await db.get_artifact('cancelled-artifact') is None
        assert (await store.get(second['id']))['artifact'] is None
    asyncio.run(scenario())


def test_disabling_flag_applies_to_new_jobs_and_keeps_active_job(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    monkeypatch.setattr(config,'CONTEXT_SETTINGS',{**DEFAULTS,'daedalus_v3_enabled':False})
    async def scenario():
        job=await store.create('conversation','task','build_from_prompt','','coder:local','request')
        assert await controller.uses_persistent_workflow('conversation')
        await store.save(job['id'],state='completed',answer='Q&A complete')
        assert not await controller.uses_persistent_workflow('conversation')
        assert await controller.route_tool('plan_project',{'task':'new task'},'conversation',None) is None
    asyncio.run(scenario())


def test_question_uses_chat_model_and_registered_worker_project(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    monkeypatch.setattr(config,'QA_MODEL','')
    async def scenario():
        await db.upsert_coding_project('registry-id','Project',conversation_id='conversation',openhands_project_id='worker-directory')
        job=await controller.create('conversation','Explain the source','ask_uploaded_project','registry-id',key='question')
        assert job['model']=='coder:local'
        assert job['project_id']=='registry-id' and job['source_project_id']=='worker-directory'
    asyncio.run(scenario())


def test_shutdown_after_publication_keeps_downloaded_artifact(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    monkeypatch.setattr(config,'SANDBOX_OUTPUTS_DIR',str(tmp_path/'artifacts'))
    async def scenario():
        original=store.finish_artifact
        async def committed_then_cancelled(*args,**kwargs):
            await original(*args,**kwargs)
            raise asyncio.CancelledError()
        monkeypatch.setattr(store,'finish_artifact',committed_then_cancelled)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=b'archive'))) as http:
            controller.configure(http,None)
            job=await store.create('conversation','task','build_from_prompt','','coder:local','request')
            job=await store.save(job['id'],state='packaging',revision_id='revision',acceptance={'accepted':True,'revision_id':'revision'})
            with pytest.raises(asyncio.CancelledError):
                await controller._deliver(job,{'revision_id':'revision','sha256':hashlib.sha256(b'archive').hexdigest()})
            completed=await store.get(job['id'])
            assert completed['state']=='completed'
            from pathlib import Path
            assert Path(completed['artifact']['storage_path']).read_bytes()==b'archive'
    asyncio.run(scenario())


def test_browser_criteria_use_the_discovered_preview_command():
    plan=[{'task':'Make greetings work','browser_flows':[{'cwd':'.','steps':[{'action':'click','selector':'#greet'}]}]}]
    discovered=[{'id':'browser','kind':'browser','cwd':'.','server_command':'python3 -m http.server {port} --bind 127.0.0.1'}]
    checks=controller._checks(plan,discovered=discovered)
    assert checks[0]['server_command']==discovered[0]['server_command']
    assert checks[0]['steps']==plan[0]['browser_flows'][0]['steps']


def test_stop_of_blocked_job_retries_worker_acknowledgement(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    spawned=[];monkeypatch.setattr(controller,'spawn',lambda *args:spawned.append(args))
    def offline(request):raise httpx.ConnectError('worker unavailable',request=request)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as http:
            controller.configure(http,None)
            job=await store.create('conversation','task','build_from_prompt','','coder:local','request')
            await store.save(job['id'],state='blocked',worker_operation='previous-operation')
            pending=await controller.cancel(job['id'])
            assert pending['state']=='cancelling' and pending['cancel_requested']
            assert spawned and spawned[0][0]==job['id']
    asyncio.run(scenario())


def test_continue_requires_acknowledgement_that_previous_writer_stopped(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    status='running'
    def remote(request):
        if status=='offline':raise httpx.ConnectError('offline',request=request)
        return httpx.Response(200,json={'status':status,'result':{}})
    async def scenario():
        nonlocal status
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
            controller.configure(http,None)
            job=await store.create('conversation','task','build_from_prompt','','coder:local','request')
            await store.save(job['id'],state='blocked',worker_operation='original-attempt',resume_state='coding')
            for value in ('running','offline'):
                status=value
                with pytest.raises(ValueError):await controller.resume(job['id'])
                current=await store.get(job['id'])
                assert current['worker_operation']=='original-attempt' and current['allowance']==1
            status='interrupted'
            assert (await controller.resume(job['id']))['allowance']==2
    asyncio.run(scenario())


def test_lost_worker_response_reconnects_without_releasing_job(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    calls=0
    async def fast_sleep(seconds):pass
    monkeypatch.setattr(controller.asyncio,'sleep',fast_sleep)
    def remote(request):
        nonlocal calls
        calls+=1
        if calls==1:raise httpx.ReadError('connection lost after submission',request=request)
        return httpx.Response(200,json={'status':'running'})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
            controller.configure(http,None)
            job=await store.create('conversation','task','build_from_prompt','','coder:local','request')
            await store.save(job['id'],state='coding',worker_operation='same-operation')
            result=await controller._worker_request(job['id'],'POST',controller.worker_url({'id':job['id'],'worker_operation':'same-operation'}),json={'request_key':'stable'},timeout=1)
            assert result.json()['status']=='running' and calls==2
            current=await store.get(job['id'])
            assert current['state']=='coding' and current['worker_operation']=='same-operation' and not current['blocker']
            assert [e['type'] for e in await store.events(job['id'])][-2:]==['worker_reconnecting','worker_reconnected']
    asyncio.run(scenario())


def test_restart_reattaches_active_operation_before_new_queued_jobs(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    async def scenario():
        queued=await store.create('conversation','queued','build_from_prompt','','coder:local','queued')
        running=await store.create('conversation','running','build_from_prompt','','coder:local','running')
        await store.save(running['id'],state='coding',worker_operation='active')
        assert [row['id'] for row in await store.recoverable()]==[running['id'],queued['id']]
    asyncio.run(scenario())


def test_controller_waits_for_stop_acknowledgement_before_returning(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    attempts=0
    async def interrupted(*args,**kwargs):raise InterruptedError('Stop requested')
    async def fast_sleep(seconds):pass
    async def cancelling(job_id):
        nonlocal attempts
        attempts+=1
        await store.request_cancel(job_id)
        return await store.save(job_id,state='cancelling' if attempts==1 else 'cancelled')
    monkeypatch.setattr(controller,'_operate',interrupted)
    monkeypatch.setattr(controller,'cancel',cancelling)
    monkeypatch.setattr(controller.asyncio,'sleep',fast_sleep)
    async def scenario():
        job=await store.create('conversation','task','build_from_prompt','','coder:local','request')
        await controller.run(job['id'])
        assert attempts==2 and (await store.get(job['id']))['state']=='cancelled'
    asyncio.run(scenario())


@pytest.mark.parametrize('accepted',[True,False])
def test_final_verified_revision_requires_acceptance_even_without_finish_tool(monkeypatch,tmp_path,accepted):
    setup(monkeypatch,tmp_path)
    stages=[]
    async def operate(job,kind,**kwargs):
        stages.append(kind)
        if kind=='check':return {'passed':True,'checks':[{'id':'tests','passed':True}]},job
        if kind=='accept':return {'accepted':accepted,'revision_id':'revision','issues':[{'summary':'Missing requested behavior'}]},job
        if kind=='package':raise ValueError('Stop before artifact download in this test')
        raise AssertionError(f'Unexpected stage: {kind}')
    monkeypatch.setattr(controller,'_operate',operate)
    async def scenario():
        job=await store.create('conversation','task','build_from_prompt','','coder:local','request')
        await store.save(job['id'],state='checking',revision_id='revision',builder_finished=False,
                         milestones=[{'task':'Implement and test the request'}],acceptance_attempt=2)
        await controller.run(job['id'])
        current=await store.get(job['id'])
        assert stages==(['check','accept','package'] if accepted else ['check','accept'])
        assert current['acceptance']['accepted'] is accepted and current['artifact'] is None
        assert current['completion_basis']=='independent_acceptance_required'
    asyncio.run(scenario())


def test_failed_checks_report_verification_failure_when_builder_also_did_not_finish(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    async def operate(job,kind,**kwargs):
        assert kind=='check'
        return {'passed':False,'checks':[{'id':'tests','passed':False}]},job
    monkeypatch.setattr(controller,'_operate',operate)
    async def scenario():
        job=await store.create('conversation','task','build_from_prompt','','coder:local','request')
        await store.save(job['id'],state='checking',revision_id='revision',builder_finished=False,
                         milestones=[{'task':'Implement and test'}],attempt=2)
        await controller.run(job['id'])
        current=await store.get(job['id'])
        assert current['state']=='blocked' and 'verification' in current['blocker']
        assert current['checks'][0]['passed'] is False
    asyncio.run(scenario())


def test_verified_intermediate_milestone_advances_without_finish_tool(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    stages=[]
    async def operate(job,kind,**kwargs):
        stages.append(kind)
        if kind=='check':return {'passed':True,'checks':[{'id':'tests','passed':True}]},job
        if kind=='code':
            assert kwargs['task']=='Add regression tests' and kwargs['milestone_id']=='1'
            return {'snapshot':{'revision':'tested'},'inventory':{},'agent_finished':False},job
        if kind=='accept':return {'accepted':True,'revision_id':'tested'},job
        if kind=='package':raise ValueError('Stop before artifact download')
        raise AssertionError(kind)
    monkeypatch.setattr(controller,'_operate',operate)
    async def scenario():
        job=await store.create('conversation','Fix lookup and add tests','build_from_prompt','','coder:local','request')
        await store.save(job['id'],state='checking',revision_id='fixed',builder_finished=False,
                         milestones=[{'task':'Fix lookup'},{'task':'Add regression tests'}])
        await controller.run(job['id'])
        current=await store.get(job['id'])
        assert stages==['check','code','check','accept','package']
        assert current['milestone_index']==1 and current['acceptance']['revision_id']=='tested'
        assert current['artifact'] is None
    asyncio.run(scenario())


@pytest.mark.parametrize('corrected',[True,False])
def test_invalid_browser_plan_replans_and_stops_repeated_errors(monkeypatch,tmp_path,corrected):
    setup(monkeypatch,tmp_path)
    invalid={'task':'Implement Python utility','browser_flows':[{'cwd':'.','steps':[]}]}
    stages=[]
    async def operate(job,kind,**kwargs):
        stages.append(kind)
        if kind=='plan':
            feedback=kwargs['evidence']['plan_feedback']
            assert 'No preview server' in feedback['error']
            assert feedback['previous_milestones']==[invalid]
            assert job['user_task']=='Create a Python utility with tests'
            return {'milestones':[{'task':'Implement Python utility'}] if corrected else [invalid]},job
        if kind=='code':return {'snapshot':{'revision':'tested'},'inventory':{},'agent_finished':True},job
        if kind=='check':return {'passed':True,'checks':[{'id':'tests','passed':True}]},job
        if kind=='accept':return {'accepted':True,'revision_id':'tested'},job
        if kind=='package':raise ValueError('Stop before artifact download')
        raise AssertionError(kind)
    monkeypatch.setattr(controller,'_operate',operate)
    async def scenario():
        job=await store.create('conversation','Create a Python utility with tests','build_from_prompt','','coder:local','request')
        await store.save(job['id'],state='checking',revision_id='existing',milestones=[invalid])
        await controller.run(job['id'])
        current=await store.get(job['id'])
        assert stages==(['plan','code','check','accept','package'] if corrected else ['plan','code'])
        assert current['artifact'] is None
        if not corrected:
            assert current['state']=='blocked' and current['resume_state']=='planning'
            continued=await controller.resume(job['id'])
            assert continued['resume_after_inspect']=='planning' and continued['plan_errors']==[]
            assert continued['plan_feedback']['error']==current['plan_feedback']['error']
    asyncio.run(scenario())


def test_queue_recovers_a_commit_after_its_creating_request_is_cancelled(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    monkeypatch.setattr(controller,'_CLOSING',False)
    monkeypatch.setattr(controller,'_RECONCILER',None)
    original_create=store.create
    async def scenario():
        tick,committed,found=asyncio.Event(),asyncio.Event(),asyncio.Event()
        dispatched=[]
        async def wait_for_tick(seconds):
            await tick.wait()
            tick.clear()
        async def commit_then_lose_request(*args,**kwargs):
            job=await original_create(*args,**kwargs)
            committed.set()
            await asyncio.Event().wait()
            return job
        def dispatch(identity,owner=None):
            dispatched.append(identity)
            found.set()
        monkeypatch.setattr(controller.asyncio,'sleep',wait_for_tick)
        monkeypatch.setattr(controller,'spawn',dispatch)
        monkeypatch.setattr(store,'create',commit_then_lose_request)
        await db.upsert_coding_project('registered','Existing project',conversation_id='conversation',openhands_project_id='worker-project')
        await controller.start()
        try:
            request=asyncio.create_task(controller.create('conversation','Update the project',mode='fix_uploaded_project',project_id='registered',model='coder:local',key='request'))
            await committed.wait()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):await request
            assert dispatched==[]
            tick.set()
            await asyncio.wait_for(found.wait(),timeout=2)
            assert len(dispatched)==1
            recovered=await store.get(dispatched[0])
            assert recovered['state']=='queued' and recovered['source_project_id']=='worker-project'
        finally:
            await controller.shutdown()
    asyncio.run(scenario())


def test_deadline_waits_for_worker_ack_and_records_final_usage(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    monkeypatch.setattr(config,'CONTEXT_SETTINGS',{**DEFAULTS,'daedalus_v3_enabled':True,'daedalus_job_seconds':1})
    polls=[];cancel_posts=[]
    ticks=iter([0,2])
    from types import SimpleNamespace
    monkeypatch.setattr(controller,'time',SimpleNamespace(monotonic=lambda:next(ticks),time=controller.time.time))
    async def fast_sleep(seconds):pass
    monkeypatch.setattr(controller.asyncio,'sleep',fast_sleep)
    def remote(request):
        if request.url.path.endswith('/events'):return httpx.Response(200,json=[])
        if request.url.path.endswith('/cancel'):
            cancel_posts.append(request.url.path)
            return httpx.Response(200,json={'status':'cancelling'})
        if request.method=='POST':return httpx.Response(202,json={})
        status=['running','cancelling','cancelled'][len(polls)]
        polls.append(status)
        return httpx.Response(200,json={'status':status,'started':100,'ended':105,'calls':4,
            'result':{'snapshot':{'revision':'a'*40,'tree':'b'*40}} if status=='cancelled' else {}})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
            controller.configure(http,None)
            job=await store.create('conversation','task','build_from_prompt','','coder:local','request')
            job=await store.save(job['id'],state='coding')
            with pytest.raises(TimeoutError,match='stopped at its checkpoint'):
                await controller._operate(job,'code')
            current=await store.get(job['id'])
            assert polls==['running','cancelling','cancelled'] and len(cancel_posts)==1
            assert current['calls_used']==4 and current['seconds_used']==5
            assert current['revision_id']=='a'*40 and not current['cancel_requested']
    asyncio.run(scenario())
