"""Project workflow regressions, with isolated storage and no model service."""
import asyncio
import json
from pathlib import Path

import pytest
import database as db
import workspace_service as ws
import library_search
import indexing_jobs
import job_registry


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path/'workspace.db'))
    asyncio.run(db.init_db())
    return tmp_path


def test_context_selection_membership_and_migration(isolated):
    async def run():
        await db.create_workspace('a','Alpha')
        await db.create_workspace('b','Beta')
        await db.update_workspace('a',memory_enabled=1,instructions='Alpha instruction')
        await db.create_conversation('c',workspace_context={'mode':'selected','workspace_id':'a'})
        assert [c['id'] for c in (await db.get_workspace('a'))['conversations']]==['c']
        assert (await db.get_conversation('c'))['effective_workspace']['id']=='a'
        await db.add_conv_to_workspace('b','c')
        assert (await ws.resolve('c'))['id']=='a'
        await db.update_conversation('c',workspace_context={'mode':'auto'})
        assert await ws.resolve('c') is None
        await db.remove_conv_from_workspace('b','c')
        assert (await ws.resolve('c'))['id']=='a'
        assert 'Alpha instruction' in (await db.build_workspace_memory_context(conversation_id='c'))['context']
        await db.update_conversation('c',workspace_context={'mode':'off'})
        assert not (await db.build_workspace_memory_context(conversation_id='c'))['context']
        await db.init_db()
        assert (await db.get_conversation('c'))['workspace_context']=={'mode':'off'}
        with pytest.raises(LookupError):
            await db.create_conversation('bad',workspace_context={'mode':'selected','workspace_id':'missing'})
        assert await db.get_conversation('bad') is None
    asyncio.run(run())


def test_workspace_search_includes_inherited_artifacts_and_linked_notes(isolated):
    async def run():
        await db.create_workspace('a','Alpha')
        await db.create_conversation('c')
        await db.add_artifact(artifact_id='artifact',conversation_id='c',filename='source.txt',url='/source.txt',content_text='cobaltnebula')
        await db.add_conv_to_workspace('a','c')
        await db.create_note('n',title='Project note',content='cobaltnebula',tags=['work'])
        await ws.link('a','notes','n')
        hits=(await library_search.search('cobaltnebula',workspace_id='a'))['results']
        assert {h['kind'] for h in hits}=={'artifact','note'}
        assert [n['id'] for n in await db.list_notes(workspace_id='a',tag='work')]==['n']
        await ws.link('a','notes','n',remove=True)
        assert {h['kind'] for h in (await library_search.search('cobaltnebula',workspace_id='a'))['results']}=={'artifact'}
    asyncio.run(run())


def test_owner_fences_for_links_and_context(isolated):
    async def run():
        await db.create_workspace('a','Alpha')
        await db.create_kb('kb','Private knowledge')
        await db.create_note('n',title='Private note')
        await db.create_conversation('c')
        user=await db.create_user('Other');token=db.set_current_user_id(user['id'])
        try:
            await db.create_workspace('b','Other workspace')
            with pytest.raises(LookupError):await ws.link('b','knowledge','kb')
            with pytest.raises(LookupError):await ws.link('b','notes','n')
            with pytest.raises(LookupError):await ws.related('a','notes')
            assert await ws.resolve('c') is None
            with pytest.raises(LookupError):await ws.validate_selection({'mode':'selected','workspace_id':'a'})
        finally:db.reset_current_user_id(token)
    asyncio.run(run())


def test_workspace_deletion_preserves_content_and_pauses_tasks(isolated):
    import task_queue
    async def run():
        await db.create_workspace('a','Alpha')
        await db.create_kb('kb','Knowledge')
        await db.create_note('n',title='Note')
        await ws.link('a','knowledge','kb');await ws.link('a','notes','n')
        task=await db.create_scheduled_task('task',title='Project task',workspace_id='a')
        assert task['conversation_id']
        assert (await db.get_conversation(task['conversation_id']))['effective_workspace']['id']=='a'
        queued=await task_queue.enqueue(task,trigger='manual')
        await db.delete_workspace('a')
        saved=await db.get_scheduled_task('task')
        assert not saved['enabled'] and not saved['workspace_id']
        assert (await db.get_task_run(queued))['status']=='cancelled'
        assert await db.get_note('n') and await db.get_kb('kb')
        conv=await db.get_conversation(task['conversation_id'])
        assert conv and conv['workspace_context']=={'mode':'off'}
    asyncio.run(run())


def test_knowledge_status_and_job_filter_are_persisted(isolated,monkeypatch):
    import rag
    async def index(*args):return {'chunks':3}
    monkeypatch.setattr(rag,'index_file',index)
    async def run():
        await db.create_workspace('a','Alpha');await db.create_kb('kb','Knowledge')
        path=isolated/'source.txt';path.write_text('source content')
        await db.add_kb_file('kb','source.txt',str(path),14,'text/plain')
        await ws.link('a','knowledge','kb')
        assert not (await ws.knowledge_status(await db.get_kb('kb')))['ready']
        identity=await indexing_jobs.submit('kb','source.txt',path)
        assert len((await job_registry.list_jobs(workspace_id='a',status='active'))['jobs'])==1
        await indexing_jobs.execute(identity)
        assert (await ws.knowledge_status(await db.get_kb('kb')))['ready']
        assert not (await job_registry.list_jobs(workspace_id='missing'))['jobs']
    asyncio.run(run())


def test_snapshot_submits_indexing_and_retains_coverage(isolated,monkeypatch):
    import main
    import config
    monkeypatch.setattr(config,'KB_DIR',str(isolated/'knowledge'))
    # Retain queued state; the normal runner is covered separately above.
    monkeypatch.setattr(main,'_track_bg',lambda coro:coro.close())
    async def run():
        await db.create_workspace('a','Alpha');await db.create_conversation('c')
        await db.add_message('c','user','cobaltnebula '*300)
        await db.add_conv_to_workspace('a','c')
        snapshot=await main.create_kb_from_workspace('a',{'name':'Snapshot'})
        assert snapshot['snapshot']['truncated']
        assert snapshot['snapshot']['conversation_ids']==['c']
        job=await indexing_jobs.get(snapshot['indexing_job_id'])
        assert job['status']=='queued' and Path(job['filepath']).is_file()
        assert 'cobaltnebula' in Path(job['filepath']).read_text()
        assert (await ws.related('a','knowledge'))['items'][0]['snapshot']==snapshot['snapshot']
    asyncio.run(run())


def test_note_direct_read_and_schedule_preview(isolated):
    from routes.notes import get_note
    from routes.scheduler import preview_task,TaskCreate
    async def run():
        await db.create_note('old',title='Older note')
        note=await get_note('old')
        assert note['id']=='old' and note['workspace_ids']==[]
        preview=await preview_task(TaskCreate(title='Morning',schedule_kind='daily',schedule_json={'time':'08:30'},timezone='America/Phoenix'))
        assert len(preview['occurrences'])==3 and preview['timezone']=='America/Phoenix'
        from datetime import datetime
        assert all(datetime.fromisoformat(t).hour==15 for t in preview['occurrences'])
    asyncio.run(run())


@pytest.mark.parametrize('mode,ghost,expect_kb', [('selected',False,True),('auto',False,True),('off',False,False),('selected',True,False)])
def test_chat_retrieves_workspace_knowledge_without_persona(isolated,monkeypatch,mode,ghost,expect_kb):
    import httpx
    import rag
    import reasoning
    import context_inspector
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from agents import chat
    from main import ChatRequest
    monkeypatch.setattr(chat,'_suggest_workspace_memories',AsyncMock())
    query=AsyncMock(return_value=[{'kb_id':'kb','filename':'manual.txt','text':'cobaltnebula uses port 4872','score':1}])
    monkeypatch.setattr(rag,'hybrid_query',query)
    reasoning._cache.clear()
    payloads=[]
    def handle(request):
        if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'fixture','digest':'fixture'}]})
        if request.url.path=='/api/show':return httpx.Response(200,json={'thinking':{'values':[False,'low','medium','high']},'capabilities':['completion']})
        assert request.url.path=='/api/chat'
        payloads.append(json.loads(request.content))
        return httpx.Response(200,text=json.dumps({'message':{'role':'assistant','content':'The project answer.'},'done':True,'eval_count':4,'prompt_eval_count':12})+'\n')
    async def run():
        await db.create_workspace('a','Alpha');await db.create_kb('kb','Knowledge')
        await ws.link('a','knowledge','kb')
        await db.create_conversation('c',workspace_context={'mode':'selected','workspace_id':'a'})
        (isolated/'manual.txt').write_text('cobaltnebula uses port 4872')
        await db.add_kb_file('kb','manual.txt',str(isolated/'manual.txt'),32,'text/plain')
        await db.update_conversation('c',workspace_context={'mode':mode,**({'workspace_id':'a'} if mode=='selected' else {})})
        req=ChatRequest(conversation_id='c',model='fixture',messages=[{'role':'user','content':'cobaltnebula port?'}],use_memories=False,ephemeral=ghost,effort_mode='auto',reasoning_level='low',reasoning_model='fixture')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            events=[json.loads(e[5:]) async for e in chat.chat_stream_generate(req,http,SimpleNamespace(emit=AsyncMock()),{}, {})]
        assert not [e for e in events if e.get('type')=='error']
        assert query.await_count==int(expect_kb)
        assert ('port 4872' in json.dumps(payloads))==expect_kb
        conv=await db.get_conversation('c')
        if ghost:
            assert not conv['messages']
        else:
            context=(await context_inspector.inspect(conv['messages'][-1]['id']))['rounds'][0]
            assert context['knowledge_base_ids']==(['kb'] if expect_kb else [])
            assert bool(context['memories']['workspace']['workspace'])==expect_kb
    asyncio.run(run())


def test_task_workspace_snapshot_and_full_result_survive_changes(isolated):
    import task_queue
    async def run():
        await db.create_workspace('a','Alpha');await db.create_workspace('b','Beta')
        task=await db.create_scheduled_task('task',title='Task',workspace_id='a')
        run_id=await task_queue.enqueue(task,trigger='manual')
        await db.update_scheduled_task('task',{'workspace_id':'b','conversation_id':''})
        job=await job_registry.get('scheduled:'+run_id)
        assert job['details']['workspace_id']=='a'
        assert job['details']['conversation_id']==task['conversation_id']
        await db.finish_task_run(run_id,'succeeded',result_text='x'*25000+' final answer')
        run=(await db.list_task_runs('task'))[0]
        assert run['result_text'].endswith(' final answer') and len(run['result_text'])>25000
        assert run['conversation_id']==task['conversation_id']
        assert len((await job_registry.list_jobs(workspace_id='a'))['jobs'])==1
        await db.init_db()
        assert (await job_registry.get('scheduled:'+run_id))['details']['workspace_id']=='a'
    asyncio.run(run())


def test_partial_index_and_missing_source_never_report_ready(isolated,monkeypatch):
    import rag
    from fastapi import HTTPException
    from routes.workspaces import index_file
    async def partial(*args):return {'chunks':1,'warnings':['One page could not be extracted']}
    monkeypatch.setattr(rag,'index_file',partial)
    async def run():
        await db.create_kb('kb','Knowledge')
        path=isolated/'source.txt';path.write_text('fixture')
        file_id=await db.add_kb_file('kb','source.txt',str(path),7,'text/plain')
        identity=await indexing_jobs.submit('kb','source.txt',path)
        await indexing_jobs.execute(identity)
        status=await ws.knowledge_status(await db.get_kb('kb'))
        assert not status['ready'] and status['files'][0]['indexing']['status']=='partial'
        path.unlink()
        with pytest.raises(HTTPException) as exc:await index_file('kb',file_id)
        assert exc.value.status_code==409
    asyncio.run(run())


def test_workspace_routes_reject_cross_profile_content_and_invalid_task_context(isolated):
    import httpx
    import main
    async def run():
        await db.create_workspace('a','Alpha');await db.create_note('n',title='Private')
        other=await db.create_user('Other')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://fixture') as http:
            assert (await http.post('/api/tasks',json={'title':'Invalid','task_type':'check_in','schedule_kind':'daily','schedule_json':{'time':'08:00'},'workspace_id':'a'})).status_code==400
            headers={'X-HyprChat-User':other['id']}
            for path in ['/api/workspaces/a/content/notes','/api/notes/n']:
                assert (await http.get(path,headers=headers)).status_code==404
            assert (await http.post('/api/conversations',headers=headers,json={'workspace_context':{'mode':'selected','workspace_id':'a'}})).status_code==400
            changed=await http.patch('/api/notes/n',json={'kind':'todo','tags':['review'],'workspace_ids':['a']})
            assert changed.status_code==200 and changed.json()['kind']=='todo'
            assert changed.json()['workspace_ids']==['a']
    asyncio.run(run())


def test_workspace_memory_tool_honors_selection_and_opt_out(isolated):
    from tools import exec_tool
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    async def run():
        await db.create_workspace('a','Alpha');await db.create_workspace('b','Beta')
        await db.update_workspace('a',memory_enabled=1);await db.update_workspace('b',memory_enabled=1)
        await db.create_conversation('c',workspace_context={'mode':'selected','workspace_id':'b'})
        await db.add_conv_to_workspace('a','c')
        events=SimpleNamespace(emit=AsyncMock())
        result=await exec_tool(None,events,'save_memory',{'content':'The project uses local port 4872.','scope':'workspace'},'c')
        assert not result.startswith('ERROR')
        assert len(await db.list_workspace_memories('b',status='all'))==1
        assert not await db.list_workspace_memories('a',status='all')
        await db.update_conversation('c',workspace_context={'mode':'off'})
        result=await exec_tool(None,events,'save_memory',{'content':'Remember another project fact.','scope':'workspace'},'c')
        assert result.startswith('ERROR') and not await db.list_global_memories(status='all')
    asyncio.run(run())
