import asyncio
import json
from pathlib import Path
from coder_progress import empty, project
from coder_source_activity import Observer


def agent(seq,kind,tool,action=None,observation=None,call='call',identity=None):
    return {'seq':seq,'at':seq,'type':'agent','data':{'event':{'kind':kind,'id':identity or str(seq),'tool_call_id':call,'tool_name':tool,
        'action':action,'observation':observation,'thought':'private','reasoning_content':'secret'}}}


def test_plan_allowlist_and_action_pairing():
    events=[agent(1,'ActionEvent','task_tracker',{'command':'plan'}),agent(2,'ObservationEvent','task_tracker',observation={'task_list':[{'title':'Build UI','status':'in_progress','notes':'private notes'}]}),
        agent(3,'ActionEvent','file_editor',{'command':'create','path':'/work/app.js','file_text':'secret source'})]
    result,rows=project(empty(),events,'op','/work')
    assert result['plan']==[{'id':'0','title':'Build UI','status':'in_progress'}]
    assert result['current']['path']=='app.js' and result['current']['action']=='create'
    assert 'private' not in json.dumps(result) and 'secret' not in json.dumps(rows)
    result,rows=project(result,[agent(4,'ObservationEvent','file_editor',observation={'is_error':True})],'op','/work')
    assert result['current'] is None and rows[0]['outcome']=='failed'


def test_terminal_poll_retains_identity_and_finish_clears():
    result,_=project(empty(),[agent(1,'ActionEvent','terminal',{'command':'opaque'}),agent(2,'ObservationEvent','terminal',observation={'exit_code':-1})],'op')
    identity=result['current']['id']
    result,_=project(result,[agent(3,'ActionEvent','terminal',{'command':''},call='poll'),agent(4,'ObservationEvent','terminal',observation={'exit_code':0},call='poll')],'op')
    assert result['current'] is None
    result,_=project(empty(),[agent(1,'ActionEvent','terminal',{'command':'opaque'}),agent(2,'ObservationEvent','terminal',observation={'exit_code':-1}),agent(3,'ActionEvent','terminal',{'command':''},call='poll')],'op')
    assert result['current']['id']==identity
    result,_=project(result,[{'seq':5,'type':'finished','data':{'status':'cancelled'}}],'op')
    assert result['current'] is None


def test_observer_detects_shell_changes_without_guessing_commands(tmp_path):
    work=tmp_path/'work';work.mkdir(); events=[]
    observer=Observer(work,tmp_path/'state.json',lambda **row:events.append(row))
    observer.event({'id':'a','kind':'ActionEvent','tool_name':'terminal','action':{'command':'do anything'}})
    (work/'actual.py').write_text('source')
    (work/'node_modules').mkdir();(work/'node_modules/dependency').write_text('excluded')
    observer.event({'id':'b','kind':'ObservationEvent','tool_name':'terminal','observation':{'exit_code':0}})
    assert [(r['path'],r['action']) for r in events]==[('actual.py','create')]
    observer.event({'id':'c','kind':'ActionEvent','tool_name':'terminal','action':{'command':'opaque'}})
    (work/'actual.py').unlink()
    observer.event({'id':'d','kind':'ObservationEvent','tool_name':'terminal','observation':{'exit_code':0}})
    assert events[-1]['action']=='delete'


def test_history_bound_and_unsafe_paths():
    batch=[{'seq':i,'at':i,'type':'source_activity','data':{'id':str(i),'path':f'f{i}.py','action':'edit'}} for i in range(60)]
    batch += [{'seq':61,'type':'source_activity','data':{'path':'../secret','action':'edit'}}]
    result,rows=project(empty(),batch,'op')
    assert len(result['files'])==50 and len(rows)==60 and result['files'][0]['path']=='f59.py'


def test_atomic_progress_replay_pagination_and_owner_scope(monkeypatch,tmp_path):
    from .test_daedalus_jobs import setup
    from db import coder_jobs as jobs
    import database as db
    setup(monkeypatch,tmp_path)
    async def run():
        job=await jobs.create('conversation','task','build_from_prompt','','local','progress',engine='openhands159',policy_version=7)
        job=await jobs.save(job['id'],state='coding',worker_operation='op',worker_event_cursor=0)
        batch=[{'seq':i,'at':i,'type':'source_activity','data':{'id':str(i),'path':f'f{i}.py','action':'edit'}} for i in range(1,56)]
        for _ in range(2):job=await jobs.save(job['id'],event='progress',worker_events=batch,worker_operation='op')
        assert job['worker_event_cursor']==55 and len(job['progress']['files'])==50
        page=await jobs.activity(job['id'],limit=30)
        assert len(page['items'])==30
        next_page=await jobs.activity(job['id'],before=page['next_cursor'],limit=30)
        assert len(next_page['items'])==25 and next_page['next_cursor'] is None
        assert not set(r['id'] for r in page['items']) & set(r['id'] for r in next_page['items'])
        token=db.set_current_user_id('other-owner')
        try:
            try:await jobs.activity(job['id'])
            except LookupError:pass
            else:raise AssertionError('cross-owner activity leaked')
        finally:db.reset_current_user_id(token)
    asyncio.run(run())


def test_interrupted_shell_changes_survive_restart(tmp_path):
    from coder_source_activity import finish_interrupted
    work=tmp_path/'work';work.mkdir();events=[];state=tmp_path/'state.json'
    observer=Observer(work,state,lambda **row:events.append(row))
    observer.event({'id':'command','kind':'ActionEvent','tool_name':'terminal','action':{'command':'opaque'}})
    (work/'saved.py').write_text('partial work')
    finish_interrupted(work,state,lambda **row:events.append(row))
    finish_interrupted(work,state,lambda **row:events.append(row))
    assert len(events)==1 and events[0]['outcome']=='interrupted' and events[0]['path']=='saved.py'
