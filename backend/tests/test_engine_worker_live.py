"""Deterministic fault injection through the real modern SDK and worker transport.

The local Ollama-compatible fixture is explicitly not a model-quality evaluation.
It lets cancellation, budgets, replay and restart hit reproducible boundaries.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

import pytest
import requests

WORKER=os.getenv('DAEDALUS_TEST_WORKER','')
pytestmark=pytest.mark.skipif(WORKER!='http://127.0.0.1:8587',reason='Only the isolated candidate worker')
ROOT=Path(os.getenv('DAEDALUS_TEST_ROOT','/opt/daedalus-openhands159-20261028'))
MODEL='qwen3.6-35b-a3b-gguf:latest'


@contextmanager
def model(mode):
    received=threading.Event(); release=threading.Event(); calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):self.handle_request({})
        def do_POST(self):self.handle_request(json.loads(self.rfile.read(int(self.headers.get('content-length','0'))) or '{}'))
        def handle_request(self,body):
            if self.path=='/api/show':result={'capabilities':['completion','tools'],'template':(ROOT/'tokenizer/chat_template.jinja').read_text() if (ROOT/'tokenizer/chat_template.jinja').exists() else '{{ .Messages }}','parameters':'','model_info':{}}
            elif self.path=='/api/tags':result={'models':[{'name':MODEL,'digest':__import__('coder_engine_tokens').MODEL_DIGEST if (ROOT/'tokenizer').exists() else 'f'*64}]}
            elif self.path=='/api/ps':result={'models':[{'name':MODEL,'size':100,'size_vram':100,'context_length':65536}]}
            elif self.path=='/api/chat':
                calls.append(body);received.set()
                if mode=='inference':release.wait(40)
                if mode=='compaction' and not body.get('tools'):
                    result={'model':MODEL,'message':{'role':'assistant','content':'Earlier commands printed diagnostics. Continue and finish the task.'},
                            'done':True,'done_reason':'stop','prompt_eval_count':1000,'eval_count':30}
                elif mode=='stuck' and not release.is_set():
                    name='task_tracker'
                    command=({'command':'plan','task_list':[{'title':'Implement the requested edit','status':'todo','notes':''}]}
                             if len(calls)==1 else {'command':'view'})
                elif mode=='environment_refresh' and len(calls)<=4:
                    if len(calls) in {1,3}:
                        version='six==1.16.0' if len(calls)==1 else 'six==1.17.0'
                        command={'command':"printf '%s\\n' "+version+" > requirements.txt",'timeout':10}
                        name=next(t['function']['name'] for t in body['tools'] if 'terminal' in t['function']['name'].lower())
                    else:
                        command={'manifests':['requirements.txt']}
                        name=next(t['function']['name'] for t in body['tools'] if 'prepare_environment' in t['function']['name'])
                elif mode=='environment_refresh' and len(calls)==5:
                    command={'command':".venv/bin/python -c 'import six; assert six.__version__ == \"1.17.0\"; open(\"environment-refreshed\", \"w\").write(six.__version__)'",'timeout':10}
                    name=next(t['function']['name'] for t in body['tools'] if 'terminal' in t['function']['name'].lower())
                elif mode=='ui_browser' and len(calls)<=4:
                    if len(calls)==1:
                        import base64
                        html='<button id="inc" onclick="count.textContent=Number(count.textContent)+1">Increment</button><output id="count">0</output>'
                        encoded=base64.b64encode(html.encode()).decode()
                        command={'command':"python3 -c \"import base64;open('index.html','wb').write(base64.b64decode('"+encoded+"'))\"",'timeout':10}
                        name=next(t['function']['name'] for t in body['tools'] if 'terminal' in t['function']['name'].lower())
                    else:
                        name=next(t['function']['name'] for t in body['tools'] if t['function']['name']=='ui')
                        command=({'action':'launch','application_type':'web','command':'python3 -m http.server 8000','url':'http://127.0.0.1:8000'} if len(calls)==2 else
                                 {'action':'click','selector':'#inc'} if len(calls)==3 else {'action':'text','selector':'#count'})
                elif len(calls)==1 and mode!='finish' or mode=='missing-tool' and len(calls)==2 or mode=='budget' or mode=='compaction' and len(calls)<10:
                    command={'command':'printf x >> action-count'+(' ; sleep 30' if mode=='command' else ' ; sleep 10' if mode=='restart' else ''),'timeout':60}
                    if mode=='compaction':command={'command':"python3 -c 'print(\"diagnostic-word \"*5000)'",'timeout':20}
                    name=next(t['function']['name'] for t in body['tools'] if 'terminal' in t['function']['name'].lower())
                else:
                    command={'message':'The requested operation is finished.'}
                    name=next(t['function']['name'] for t in body['tools'] if 'finish' in t['function']['name'].lower())
                if not (mode=='compaction' and not body.get('tools')):
                    result={'model':MODEL,'created_at':'2026-10-28T00:00:00Z','message':{'role':'assistant','content':'',
                        'tool_calls':[{'function':{'name':name,'arguments':command}}]},'done':True,'done_reason':'stop',
                        'prompt_eval_count':30000 if mode=='compaction' else 100,'eval_count':20}
            else:result={'done':True}
            status=200
            if self.path=='/api/chat' and mode=='server-error':
                status=500; result={'error':'Fixture tool-call parser rejected a truncated response'}
            if self.path=='/api/chat' and mode=='generation-failure' and len(calls)>1:
                result={'model':MODEL,'message':{'role':'assistant','content':'discard truncated output'},
                        'done':True,'done_reason':'length','prompt_eval_count':100,'eval_count':16384}
            if self.path=='/api/chat' and mode=='missing-tool' and len(calls)==1:
                result={'model':MODEL,'message':{'role':'assistant','content':'I have finished.'},
                        'done':True,'done_reason':'stop','prompt_eval_count':100,'eval_count':12}
            raw=json.dumps(result).encode()
            try:
                self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            except OSError:pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}',received,release,calls
    finally:release.set();server.shutdown();server.server_close();thread.join()


class Job:
    def __init__(self,url,calls=10):
        from context_policy import DEFAULTS
        self.id='cw3-'+uuid.uuid4().hex[:24];self.sequence=0
        self.base={'engine':'openhands159','engine_behavior_version':1,'policy_version':7,'settings':{**DEFAULTS,'daedalus_compaction':'off'},
                   'model':MODEL,'ollama_url':url,'seconds_remaining':180,'calls_remaining':calls,
                   'task':'Perform the requested fixture operation.','original_task':'Perform the requested fixture operation.',
                   'revision_id':''}
        self.operate('inspect',wait=True)
    def operate(self,kind,wait=False):
        self.sequence+=1;self.url=WORKER+f'/jobs/{self.id}/operations/{self.id}-{self.sequence}'
        self.payload={**self.base,'kind':kind,'request_key':self.id+'-'+str(self.sequence)}
        response=requests.post(self.url,json=self.payload,timeout=15);response.raise_for_status()
        return self.wait() if wait else response.json()
    def wait(self):
        deadline=time.monotonic()+100
        while True:
            try:response=requests.get(self.url,timeout=5);response.raise_for_status();operation=response.json()
            except requests.RequestException:
                if time.monotonic()>deadline:raise
                time.sleep(.2);continue
            if operation['status'] not in {'queued','starting','running','cancelling'}:
                if operation['result'].get('snapshot'):self.base['revision_id']=operation['result']['snapshot']['revision']
                return operation
            if time.monotonic()>deadline:raise TimeoutError(operation)
            time.sleep(.2)
    @property
    def marker(self):return ROOT/'state/jobs'/self.id/'workspace/action-count'
    def wait_marker(self):
        deadline=time.monotonic()+40
        while not self.marker.exists():
            if time.monotonic()>deadline:raise TimeoutError(requests.get(self.url).json())
            time.sleep(.1)


def test_local_sampling_options_reach_the_actual_transport():
    with model('finish') as (url, received, release, calls):
        job=Job(url); job.operate('code', wait=True)
        assert calls
        assert all(call['options']['temperature'] == .2 for call in calls)


def test_sdk_browser_launches_with_long_durable_runtime_and_closes():
    """The real SDK UI executor must work under operation-caches, not just QA /tmp."""
    with model('ui_browser') as (url, received, release, calls):
        job=Job(url)
        try:
            result=job.operate('code',wait=True)
            assert result['status']=='succeeded',result
            events=requests.get(job.url+'/events',timeout=10).json()
            observations=[e.get('data',{}).get('event',{}).get('observation',{}) for e in events
                          if e.get('data',{}).get('event',{}).get('tool_name')=='ui']
            observations=[o for o in observations if o]
            assert len(observations)==3 and all(not o.get('is_error') for o in observations),observations
            assert any(json.loads(c['text']).get('text')=='1' for o in observations for c in o.get('content',[])),observations
            assert len(calls)==5,calls
        finally:
            requests.post(job.url+'/cancel',timeout=20).raise_for_status()


def test_inference_server_failure_preserves_its_useful_reason():
    with model('server-error') as (url, received, release, calls):
        job=Job(url); result=job.operate('code', wait=True)
        assert result['status']=='failed'
        assert 'Fixture tool-call parser rejected a truncated response' in result['result']['error']


@pytest.mark.parametrize('boundary',['inference','command'])
def test_stop_acknowledges_and_replay_never_dispatches_again(boundary):
    with model(boundary) as (url,received,release,calls):
        job=Job(url);job.operate('code')
        assert received.wait(40)
        if boundary=='command':job.wait_marker()
        response=requests.post(job.url+'/cancel',timeout=20);response.raise_for_status()
        stopped=job.wait();assert stopped['status']=='cancelled'
        before=len(calls);content=job.marker.read_text() if job.marker.exists() else ''
        requests.post(job.url,json=job.payload,timeout=10).raise_for_status()
        release.set();time.sleep(.7)
        assert len(calls)==before
        assert (job.marker.read_text() if job.marker.exists() else '')==content
        events=requests.get(job.url+'/events',params={'after':0},timeout=5).json()
        assert len({e['seq'] for e in events})==len(events)
        assert requests.get(job.url+'/events',params={'after':events[-1]['seq']},timeout=5).json()==[]
        assert stopped['result'].get('snapshot')


def test_model_call_budget_is_authoritative():
    with model('budget') as (url,received,release,calls):
        job=Job(url,calls=2);job.operate('code');stopped=job.wait()
        assert stopped['status'] in {'blocked','failed'}
        assert stopped['calls']==2 and len(calls)==2
        assert job.marker.read_text()=='xx'
        assert stopped['result'].get('snapshot')


def test_continue_restores_repair_count_without_replaying_interrupted_action():
    with model('command') as (url,received,release,calls):
        job=Job(url);job.operate('code');job.wait_marker()
        state=ROOT/'state/agent-runtime'/job.id/'bookkeeping/engine/stop.json'
        saved=json.loads(state.read_text());saved['denials']=2
        state.write_text(json.dumps(saved))
        requests.post(job.url+'/cancel',timeout=20).raise_for_status()
        assert job.wait()['status']=='cancelled'
        assert job.marker.read_text()=='x'
    with model('finish') as (url,received,release,calls):
        job.base['ollama_url']=url
        resumed=job.operate('code',wait=True)
        assert resumed['status']=='succeeded',resumed['result']
        assert resumed['result']['hook_denials']==2
        assert job.marker.read_text()=='x', 'The interrupted terminal action was dispatched twice'


def test_worker_restart_reattaches_without_repeating_command():
    import psutil
    assert (ROOT/'codex-candidate').exists()
    with model('restart') as (url,received,release,calls):
        job=Job(url);job.operate('code');job.wait_marker()
        workers=[process for process in psutil.process_iter(['cmdline','cwd'])
                 if 'coder_engine_worker:app' in (process.info['cmdline'] or [])
                 and process.info['cwd']==str(ROOT/'backend')]
        assert len(workers)==1, 'Restart only the single isolated candidate worker'
        worker=workers[0]
        unit=os.getenv('DAEDALUS_TEST_SERVICE','daedalus-qualification-159.service')
        assert unit in {'daedalus-qualification-159.service','daedalus-reliability-159.service'}
        result=subprocess.run(['systemctl','show',unit,'-p','LoadState','-p','ActiveState',
                               '-p','MainPID','-p','WorkingDirectory','-p','KillMode'],
                              capture_output=True,text=True,timeout=10)
        properties=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        if properties.get('LoadState')=='loaded':
            assert properties['ActiveState']=='active', properties
            assert int(properties['MainPID'])==worker.pid, properties
            assert properties['WorkingDirectory']==str(ROOT/'backend'), properties
            assert properties['KillMode']=='process', 'The detached operation must survive HTTP restart'
            # A manual Popen would compete with systemd's automatic restart and
            # could accidentally run the replacement outside its resource limits.
            subprocess.run(['systemctl','restart',unit],check=True,timeout=30)
        else:
            assert properties.get('LoadState')=='not-found', (result.stdout,result.stderr)
            cgroup=Path(f'/proc/{worker.pid}/cgroup').read_text()
            assert 'daedalus-' not in cgroup, 'An unrecognized managed worker must not be replaced manually'
            worker.terminate();worker.wait(15)
            with (ROOT/'worker.log').open('ab') as log:
                subprocess.Popen([str(ROOT/'venv/bin/python'),'-m','uvicorn','coder_engine_worker:app','--host','127.0.0.1','--port','8587'],
                    cwd=ROOT/'backend',env={**os.environ,'DAEDALUS_STATE_DIR':str(ROOT/'state'),'DAEDALUS_PROJECTS_DIR':str(ROOT/'projects')},
                    stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        result=job.wait()
        assert result['status']=='succeeded',result['result']
        assert job.marker.read_text()=='x'
        requests.post(job.url,json=job.payload,timeout=10).raise_for_status()
        assert len(calls)==2

@pytest.mark.parametrize('fixture', ['printed-success','empty-suite','mock-only','failing-assertion','build-only'])
def test_verification_faults_never_claim_checks_passed(fixture):
    from coder_worker_runtime import WorkerStore, operation_repo
    with model('unused') as (url,received,release,calls):
        job=Job(url)
        ledger=WorkerStore(ROOT/'state'); operation=ledger.get(job.id+'-1');repository=operation_repo(ledger,operation)
        root=repository.root
        (root/'app.py').write_text('def answer():\n    return 42\n')
        if fixture=='printed-success':
            (root/'test_app.py').write_text('print("Ran 99 tests in 0.001s\\nOK")\n')
        elif fixture=='empty-suite':
            (root/'test_app.py').write_text('import unittest\n')
        elif fixture=='mock-only':
            (root/'test_app.py').write_text('import unittest\nclass Test(unittest.TestCase):\n def test_fake(self):self.assertEqual(42,42)\n')
        elif fixture=='failing-assertion':
            (root/'test_app.py').write_text('import unittest,app\nclass Test(unittest.TestCase):\n def test_answer(self):self.assertEqual(app.answer(),0)\n')
        job.base['revision_id']=repository.snapshot('Verification fault fixture')['revision']
        result=job.operate('check',wait=True)
        assert result['status']=='succeeded',result['result']
        assert not result['result']['summary']['checks_passed'],result['result']
        assert not result['result']['summary']['accepted']
        assert not calls


def test_sdk_compaction_uses_shared_budget_and_stage_context():
    with model('compaction') as (url,received,release,calls):
        job=Job(url,calls=16)
        job.base['settings'].update(daedalus_compaction='on',context_compaction_threshold=20,
            daedalus_role_outputs={'compaction':512})
        job.operate('code');operation=job.wait()
        summaries=[request for request in calls if not request.get('tools')]
        assert summaries,'SDK did not compact the deliberately oversized history'
        assert all(request['options']['num_predict']==512 for request in summaries)
        assert operation['calls']==len(calls)<=16
        assert not operation['result'].get('accepted')


def test_dependencies_refresh_twice_inside_one_sdk_conversation():
    with model('environment_refresh') as (url, received, release, calls):
        job=Job(url); result=job.operate('code',wait=True)
        assert result['status']=='succeeded', result
        assert (job.marker.parent/'environment-refreshed').read_text()=='1.17.0'
        assert len(calls)==6
        assert result['payload']['_preparation_seconds']>0
        assert result['result'].get('hook_denials',0)==0


@pytest.mark.parametrize('kind',['web','desktop'])
def test_managed_preview_has_rfb_session_and_independent_stop(kind):
    import asyncio
    import websockets
    with model('finish') as (url, received, release, calls):
        job=Job(url)
        root=job.marker.parent
        if kind=='web':
            (root/'index.html').write_text('<button onclick="this.textContent=\'clicked\'">Click</button>')
            command='python3 -m http.server 8000'
        else:
            (root/'app.py').write_text('from PyQt6.QtWidgets import QApplication,QPushButton\na=QApplication([])\nb=QPushButton("Click")\nb.clicked.connect(lambda:b.setText("clicked"))\nb.show()\na.exec()\n')
            command='/usr/bin/python3 app.py'
        from coder_repository import Repository
        from context_policy import DEFAULTS
        repository=Repository(root,ROOT/'state/jobs'/job.id/'repository',DEFAULTS['daedalus_exclude_dirs'])
        revision=repository.snapshot('Preview fixture')['revision']
        response=requests.post(WORKER+f'/jobs/{job.id}/previews',json={'revision':revision,'kind':kind,'command':command,'toolkit':'qt' if kind=='desktop' else ''},timeout=15)
        response.raise_for_status(); state=response.json()
        endpoint=WORKER+f'/jobs/{job.id}/previews/'+state['id']
        try:
            deadline=time.monotonic()+45
            while state['status']=='starting' and time.monotonic()<deadline:
                time.sleep(.3);state=requests.get(endpoint,timeout=5).json()
            assert state['status']=='running',state
            async def handshake():
                async with websockets.connect(endpoint.replace('http:','ws:')+'/stream?token='+state['token']) as socket:
                    data=await asyncio.wait_for(socket.recv(),5)
                    assert data.startswith(b'RFB ')
            asyncio.run(handshake())
            assert requests.get(WORKER+'/health',timeout=5).status_code==200
        finally:
            requests.delete(endpoint,timeout=15).raise_for_status()
        assert requests.get(endpoint,timeout=5).json()['status']=='closed'


def test_missing_required_tool_recovers_once_before_dispatch():
    with model('missing-tool') as (url,received,release,calls):
        job=Job(url);result=job.operate('code',wait=True)
        assert result['status']=='succeeded',result
        events=requests.get(job.url+'/events',timeout=10).json()
        failures=[e['data'] for e in events if e['type']=='model_failure']
        assert len(failures)==1 and failures[0]['category']=='missing_required_tool'
        assert failures[0]['upstream_status']==200 and failures[0]['bridge_status']==502
        assert len(calls)==3 and job.marker.read_text()=='x'  # Retry writes once; the next turn explicitly finishes.
        assert 'Controller response recovery' in calls[1]['messages'][-1]['content']


def test_generation_failure_retains_checkpoint_without_replaying_action():
    with model('generation-failure') as (url,received,release,calls):
        job=Job(url);result=job.operate('code',wait=True)
        assert result['status']=='failed' and result['result']['failure']['category']=='output_limit',result
        assert result['calls']==3 and job.marker.read_text()=='x'
        checkpoint=result['result']['snapshot']['revision']
        assert result['result']['inventory']['files'] and checkpoint==job.base['revision_id']
        job.base.update(verification_summary={'revision_id':checkpoint,'checks_passed':False,'incomplete_reason':'output_limit'})
        packaged=job.operate('package',wait=True)
        assert packaged['status']=='succeeded' and len(calls)==3,packaged


def test_controller_publishes_failed_generation_without_replacing_accepted_revision(tmp_path,monkeypatch):
    import asyncio
    import httpx
    import config
    import database as db
    import coder_jobs as controller
    from db import coder_jobs as jobs, coder_promotion
    from .test_daedalus_jobs import setup
    setup(monkeypatch,tmp_path)
    monkeypatch.setenv('DAEDALUS_ENGINE_URL',WORKER)
    monkeypatch.setattr(config,'SANDBOX_OUTPUTS_DIR',str(tmp_path/'outputs'))
    monkeypatch.setattr(config,'ARTIFACT_DIR',str(tmp_path/'artifacts'))
    fixture_key=uuid.uuid4().hex  # The live worker retains operation identities between pytest runs.
    async def scenario():
        async with httpx.AsyncClient(timeout=60) as http:
            controller.configure(http,None)
            with model('build') as (url,_,__,calls):
                monkeypatch.setattr(config,'OLLAMA_URL',url)
                initial=await controller.create('conversation','Create action-count with initial content.',model=MODEL,key=fixture_key+'-initial',engine='openhands159')
                await controller.run(initial['id']);initial=await jobs.get(initial['id'])
                assert initial.get('candidate_artifact'),initial
                await coder_promotion.accept(initial['project_id'],initial['revision_id'])
                accepted=await db.get_coding_project(initial['project_id'])
            with model('generation-failure') as (url,_,__,calls):
                monkeypatch.setattr(config,'OLLAMA_URL',url)
                followup=await controller.create('conversation','Append to action-count.',mode='edit_project',project_id=initial['project_id'],model=MODEL,key=fixture_key+'-failure',engine='openhands159')
                await controller.run(followup['id']);saved=await jobs.get(followup['id'])
                assert saved['state']=='ready_for_review' and saved.get('candidate_artifact'),saved
                summary=saved['verification_summary']
                assert summary['incomplete_reason'] and not summary['checks_passed']
                assert summary['revision_id']==saved['revision_id']!=initial['revision_id']
                assert saved['failure']['category']=='output_limit' and len(calls)==3
                assert saved['candidate_artifact']['metadata']['revision_id']==saved['revision_id']
                assert Path(saved['candidate_artifact']['storage_path']).is_file()
                current=await db.get_coding_project(initial['project_id'])
                assert current['accepted_revision_id']==accepted['accepted_revision_id']
                assert current['working_revision_id']==saved['revision_id']
    asyncio.run(scenario())
