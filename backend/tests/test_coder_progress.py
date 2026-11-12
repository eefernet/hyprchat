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
    assert result['current']['verb']=='create' and result['current']['tool']=='file_editor' and 'command' not in result['current']
    call=[r for r in rows if r['kind']=='tool_call' and r.get('path')=='app.js']
    assert len(call)==1 and call[0]['verb']=='create' and call[0]['tool']=='file_editor' and call[0]['id']=='op:3'
    assert [r['verb'] for r in rows if r['kind']=='tool_call']==['plan','create']
    assert [r['outcome'] for r in rows if r['kind']=='tool_result']==['succeeded']
    assert result['recent'][0]['id']=='op:3' and 'file_text' not in json.dumps(result)
    assert 'private' not in json.dumps(result) and 'secret' not in json.dumps(rows)
    result,rows=project(result,[agent(4,'ObservationEvent','file_editor',observation={'is_error':True})],'op','/work')
    assert result['current'] is None and rows[0]['outcome']=='failed'
    done=[r for r in rows if r['kind']=='tool_result']
    assert done==[{'id':'op:3:done','kind':'tool_result','ref':'op:3','at':4,'outcome':'failed','exit_code':None}]
    assert result['recent'][0]['id']=='op:3:done' and result['recent'][1]['id']=='op:3'


def test_commands_project_without_secrets():
    command='API_TOKEN=abc123 curl -H "Authorization: Bearer xyz" https://u:p@h/x --password hunter2'
    result,rows=project(empty(),[agent(1,'ActionEvent','terminal',{'command':command})],'op')
    assert result['current']['kind']=='command' and result['current']['verb']=='run' and result['current']['tool']=='terminal'
    assert result['current']['command'].startswith('curl') and rows[0]['command']==result['current']['command']
    shown=json.dumps(result)+json.dumps(rows)
    assert not any(s in shown for s in ('abc123','xyz','hunter2','u:p'))
    result,rows=project(result,[agent(2,'ObservationEvent','terminal',observation={'exit_code':3,'content':'secret output'})],'op')
    assert result['current'] is None and rows==[{'id':'op:1:done','kind':'tool_result','ref':'op:1','at':2,'outcome':'succeeded','exit_code':3}]
    assert 'secret output' not in json.dumps(result)


def test_check_started_and_check_pair():
    started={'seq':1,'at':1,'type':'check_started','data':{'id':'test:.','phase':'test','cwd':'.','command':'TOKEN=abc npm test','index':0}}
    result,rows=project(empty(),[started],'op',stage='checking')
    assert result['current']['verb']=='check' and result['current']['tool']=='check' and result['current']['kind']=='command'
    assert result['current']['command']=='npm test' and result['current']['check_id']=='test:.' and result['current']['phase']=='test'
    assert rows==[{'id':'op:1','kind':'tool_call','at':1,'stage':'checking','tool':'check','verb':'check','command':'npm test','check_id':'test:.','phase':'test'}]
    assert 'abc' not in json.dumps(result)
    other={'seq':2,'at':2,'type':'check','data':{'check':{'id':'other','passed':True}}}
    result,rows=project(result,[other],'op',stage='checking')
    assert result['current']['id']=='op:1' and rows==[]
    row={'id':'test:.','passed':False,'exit_code':1,'log_tail':'secret log'}
    result,rows=project(result,[{'seq':3,'at':3,'type':'check','data':{'check':row}}],'op',stage='checking')
    assert result['current'] is None and rows==[{'id':'op:1:done','kind':'tool_result','ref':'op:1','at':3,'outcome':'failed','exit_code':1}]
    assert 'secret log' not in json.dumps(result)+json.dumps(rows)
    result,rows=project(result,[dict(started,seq=4,at=4)],'op',stage='checking')
    result,rows=project(result,[{'seq':5,'at':5,'type':'check','data':{'id':'test:.','passed':True}}],'op',stage='checking')
    assert result['current'] is None and rows[0]['outcome']=='succeeded' and rows[0]['ref']=='op:4'


def test_every_started_check_gets_a_result_when_rows_arrive_at_the_end():
    def started(seq,identity): return {'seq':seq,'at':seq,'type':'check_started','data':{'id':identity,'phase':'test','cwd':'.','command':'run '+identity,'index':seq}}
    def row(seq,identity,passed): return {'seq':seq,'at':seq,'type':'check','data':{'check':{'id':identity,'passed':passed,'exit_code':0 if passed else 1}}}
    result,rows=project(empty(),[started(1,'build:.'),started(2,'test:.'),started(3,'probe:1')],'op',stage='checking')
    assert result['current']['check_id']=='probe:1' and len([r for r in rows if r['kind']=='tool_call'])==3
    result,rows=project(result,[row(4,'build:.',True),row(5,'test:.',False),row(6,'probe:1',True)],'op',stage='checking')
    assert [(r['ref'],r['outcome']) for r in rows]==[('op:1','succeeded'),('op:2','failed'),('op:3','succeeded')]
    assert result['current'] is None and result['check_calls']=={}
    result,rows=project(result,[row(7,'build:.',True)],'op',stage='checking')
    assert rows==[]


def test_model_call_never_clobbers_pending_command():
    events=[agent(1,'ActionEvent','terminal',{'command':'npm test'}),agent(2,'ObservationEvent','terminal',observation={'exit_code':-1}),{'seq':3,'at':3,'type':'model_call','data':{'request_id':'r'}}]
    result,rows=project(empty(),events,'op')
    assert result['current']['kind']=='command' and result['current']['command']=='npm test' and result['current']['id']=='op:1'
    result,rows=project(result,[{'seq':4,'at':4,'type':'model_response','data':{}},agent(5,'ActionEvent','terminal',{'command':''},call='poll')],'op')
    assert result['current']['id']=='op:1' and result['current']['command']=='npm test' and result['current']['call_id']=='poll' and rows==[]
    result,rows=project(empty(),[{'seq':1,'at':1,'type':'model_call','data':{}}],'op')
    assert result['current']['kind']=='model' and result['current']['verb']=='think' and result['current']['tool']=='model' and rows==[]
    result,rows=project(result,[{'seq':2,'at':2,'type':'model_call','data':{}}],'op')
    assert result['current']['id']=='op:2'
    result,rows=project(result,[{'seq':3,'at':3,'type':'model_response','data':{}}],'op')
    assert result['current'] is None and rows==[]
    result,rows=project(empty(),[{'seq':1,'at':1,'type':'model_call','data':{}},agent(2,'ActionEvent','file_editor',{'command':'view','path':'a.py'})],'op')
    assert result['current']['kind']=='tool' and result['current']['verb']=='read'
    result,rows=project(result,[{'seq':3,'at':3,'type':'model_failure','data':{}}],'op')
    assert result['current']['verb']=='read'
    result,rows=project(result,[{'seq':4,'type':'finished','data':{'status':'cancelled'}}],'op')
    assert result['current'] is None and rows==[{'id':'op:2:done','kind':'tool_result','ref':'op:2','at':None,'outcome':'interrupted','exit_code':None}]


def test_recent_is_bounded_to_20():
    events=[]
    for i in range(30):
        events.append(agent(2*i+1,'ActionEvent','file_editor',{'command':'create','path':f'/work/f{i}.js','file_text':'body'},call=f'c{i}'))
        events.append(agent(2*i+2,'ObservationEvent','file_editor',observation={},call=f'c{i}'))
    result,rows=project(empty(),events,'op','/work')
    assert len(rows)==60 and len(result['recent'])==20
    assert result['recent'][0]=={'id':'op:59:done','kind':'tool_result','ref':'op:59','at':60,'outcome':'succeeded','exit_code':None}
    assert result['recent'][1]['id']=='op:59' and result['recent'][1]['path']=='f29.js' and result['recent'][-1]['id']=='op:41'
    assert result['files']==[] and 'body' not in json.dumps(result)
    previous=result
    result,rows=project(previous,[agent(61,'ActionEvent','terminal',{'command':'npm test'},call='t')],'op','/work')
    assert len(result['recent'])==20 and result['recent'][0]['id']=='op:61' and result['recent'][1]['id']=='op:59:done'
    result,rows=project({k:v for k,v in previous.items() if k!='recent'},[agent(61,'ActionEvent','terminal',{'command':'npm test'},call='t')],'op','/work')
    assert result['recent']==[rows[0]]


def test_view_sets_read_path_without_file_record():
    result,rows=project(empty(),[agent(1,'ActionEvent','file_editor',{'command':'view','path':'/work/server.js'})],'op','/work')
    assert result['current']['verb']=='read' and result['current']['path']=='server.js' and result['current']['kind']=='tool'
    assert rows==[{'id':'op:1','kind':'tool_call','at':1,'stage':'coding','tool':'file_editor','verb':'read','path':'server.js'}]
    result,rows=project(result,[agent(2,'ObservationEvent','file_editor',observation={'content':'secret source'})],'op','/work')
    assert result['current'] is None and result['files']==[] and [r['kind'] for r in rows]==['tool_result']
    assert 'secret source' not in json.dumps(result)+json.dumps(rows)


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
