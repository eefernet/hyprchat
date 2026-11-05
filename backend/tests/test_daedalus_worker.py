import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coder_repository import Repository
from coder_worker_runtime import WorkerStore, launch, cancel, execute
from context_policy import DEFAULTS, resolve, estimate_tokens


def payload(**extra):
    return {'engine':'openhands159','policy_version':7,'kind':'inspect','request_key':'one','task':'inspect','model':'coder:local','settings':DEFAULTS.copy(),
            'seconds_remaining':30,'calls_remaining':10,'projects_root':'','source_project_id':'',**extra}


def test_read_only_agent_can_correct_a_missing_source_path(monkeypatch,tmp_path):
    import coder_inference
    from coder_worker_runtime import _read_only
    root=tmp_path/'project';root.mkdir();(root/'api.py').write_text('value = 42\n')
    repository=Repository(root,tmp_path/'index');repository.refresh()
    store=WorkerStore(tmp_path/'worker')
    store.create('op','job','qa',payload(kind='qa'));store.update('op',started=time.time())
    prompts=[]
    answers=[{'inspect':{'operation':'read','path':'tests/api.py'}},
             {'inspect':{'operation':'read','path':'api.py'}}, {'answer':'api.py:1 defines value as 42.'}]
    def model(store,operation_id,role,messages,**kwargs):
        prompts.append(messages[0]['content'])
        return json.dumps(answers[len(prompts)-1])
    monkeypatch.setattr(coder_inference,'local_chat',model)
    result=_read_only(store,'op',repository,'Inspect current source.',{})
    assert result['answer']=='api.py:1 defines value as 42.' and len(prompts)==3
    assert 'FileNotFoundError' in prompts[1] and 'value = 42' in prompts[2]
    assert store.events('op')[0]['data']['result']['items'][0]['path']=='api.py'
    assert (root/'api.py').read_text()=='value = 42\n'


def test_root_tests_remain_visible_beyond_a_large_directory_page(tmp_path):
    from coder_worker_runtime import bounded_context
    root=tmp_path/'project';(root/'packages').mkdir(parents=True)
    for index in range(110):(root/'packages'/f'file-{index:03}.py').write_text('value = 1\n')
    (root/'test_api.py').write_text('value = 42\n')
    repository=Repository(root,tmp_path/'index');repository.refresh()
    context=bounded_context(repository,'Review the tests',resolve(settings=DEFAULTS))
    assert 'test_api.py' in {row['path'] for row in context['root_files']}
    assert 'test_api.py' not in {row['path'] for row in context['files']}
    assert 'test_api.py' in {row['path'] for row in repository.inventory(cursor=context['files_next_cursor'])['items']}


def wait(store, identity):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        operation=store.get(identity)
        if operation['status'] not in {'queued','starting','running','cancelling'}:return operation
        time.sleep(.05)
    pytest.fail('Worker failed to finish fixture')


def test_duplicate_worker_post_launches_only_one_process(tmp_path, monkeypatch):
    import hashlib
    import io
    import tarfile
    import uuid
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from coder_worker_runtime import install_routes
    from hephaestus_scope import headers, install_worker_auth

    store = WorkerStore(tmp_path/'state')
    monkeypatch.setenv('DAEDALUS_STATE_DIR', str(store.root))
    app = FastAPI()
    install_worker_auth(app)
    install_routes(app, tmp_path/'projects')
    client = TestClient(app)
    job = 'cw3-' + uuid.uuid4().hex
    signed = lambda: headers('duplicate-operation-fixture', project=job)
    source = tmp_path/'file.py'
    source.write_text('print(1)\n')
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        archive.add(source, arcname='file.py')
    digest = hashlib.sha256(stream.getvalue()).hexdigest()
    response = client.put(f'/jobs/{job}/source/{digest}', headers=signed(), content=stream.getvalue())
    assert response.is_success, response.text
    url = f'/jobs/{job}/operations/op'
    request = payload(source_job_id=job, source_archive_sha256=digest, project_id=job)
    first = client.post(url, json=request, headers=signed())
    second = client.post(url, json=request, headers=signed())
    assert first.status_code == second.status_code == 202
    assert first.json()['pid'] and second.json()['pid'] == first.json()['pid']
    finished = wait(store,'op')
    assert finished['status'] == 'succeeded', finished['result']
    assert finished['result']['inventory']['files'] == 1
    assert finished['calls'] == 0
    assert source.read_text() == 'print(1)\n'
    if os.environ.get('HEPHAESTUS_EXECUTOR') == 'docker':
        assert client.get(url, headers=headers('other-fixture', project=job)).status_code == 404


def test_copy_never_follows_external_symlink(tmp_path):
    store=WorkerStore(tmp_path/'state')
    root=tmp_path/'projects';(root/'source').mkdir(parents=True)
    secret=tmp_path/'outside';secret.write_text('outside-data')
    (root/'source'/'link').symlink_to(secret)
    store.create('op','job','inspect',payload(projects_root=str(root),source_project_id='source'))
    store.update('op',started=time.time())
    execute(store,'op')
    assert store.get('op')['status']=='blocked'
    assert 'escapes project' in store.get('op')['result']['error']
    assert not (store.root/'jobs'/'job'/'workspace'/'link').exists()


def test_cancellation_stops_worker_and_its_command(tmp_path):
    import psutil
    store=WorkerStore(tmp_path/'state')
    workspace=store.root/'jobs'/'job'/'workspace';workspace.mkdir(parents=True)
    (workspace/'test_wait.py').write_text('import unittest,subprocess\nclass Waiting(unittest.TestCase):\n    def test_wait(self):\n        print("WAITING_FOR_CANCEL",flush=True)\n        subprocess.run(["sleep","60"],check=True)\n')
    repository=Repository(workspace,store.root/'jobs'/'job'/'repository')
    revision=repository.snapshot()['revision']
    store.create('op','job','check',payload(kind='check',revision_id=revision,seconds_remaining=90,
        _execution_scope={'owner':'cancellation-fixture','project':'cancellation-fixture'},
        execution_commands={'packages':{'.':{'test':'python3 -m unittest -v test_wait'}}}))
    launch(store,'op')
    parent=psutil.Process(store.get('op')['pid'])
    deadline=time.monotonic()+60
    children=[]
    container=''
    while time.monotonic()<deadline:
        assert parent.is_running(),store.get('op')['result']
        children=parent.children(recursive=True)
        if os.environ.get('HEPHAESTUS_EXECUTOR')=='docker':
            started=any('WAITING_FOR_CANCEL' in p.read_text(errors='replace') for p in store.root.rglob('*.log'))
            if started:
                client=next(c for c in children if any('hephaestus_client.py' in part for part in c.cmdline()))
                request=json.loads(Path(client.cmdline()[-1]).read_text())
                container='hephaestus-'+request['id']
                observed=json.loads(subprocess.check_output(['docker','inspect',container]))[0]
                assert observed['State']['Running']
                children.extend(psutil.Process(observed['State']['Pid']).children(recursive=True))
                break
        elif any(child.name()=='sleep' for child in children):break
        time.sleep(.05)
    else:pytest.fail('The real check command did not start: '+str(store.get('op')['result']))
    assert children
    result=cancel(store,'op')
    assert result['status']=='cancelled'
    assert not any(child.is_running() and child.status()!=psutil.STATUS_ZOMBIE for child in children)
    if container:
        inspected=subprocess.run(['docker','inspect',container],capture_output=True,text=True)
        assert inspected.returncode or not json.loads(inspected.stdout)[0]['State']['Running']


def test_unfinished_inference_stream_is_not_a_completion(monkeypatch,tmp_path):
    import requests
    import coder_inference
    store=WorkerStore(tmp_path/'state')
    store.create('op','job','qa',payload(kind='qa',ollama_url='http://ollama'))
    store.update('op',started=time.time())
    monkeypatch.setattr(coder_inference,'ensure_context',lambda *args:None)
    monkeypatch.setattr(coder_inference,'require_local_model',lambda *args:{'capabilities':[]})
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def raise_for_status(self):pass
        def iter_lines(self):yield b'{"message":{"content":"partial"}}'
    monkeypatch.setattr(requests,'post',lambda *a,**kw:Response())
    with pytest.raises(RuntimeError,match='without a completion'):
        coder_inference.local_chat(store,'op','qa',[{'role':'user','content':'Explain the source'}])
    assert store.get('op')['calls']==1


def test_active_worker_inherits_changed_global_compaction(monkeypatch,tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from coder_worker_runtime import install_routes
    monkeypatch.setenv('DAEDALUS_STATE_DIR',str(tmp_path/'worker'))
    app=FastAPI();store=install_routes(app,tmp_path/'projects')
    request=payload(settings={**DEFAULTS,'daedalus_compaction':'inherit','context_compaction':'off'})
    store.create('op','job','inspect',request)
    with TestClient(app) as client:
        response=client.patch('/jobs/job/operations/op/settings',json={'context_compaction':'on','openhands_num_ctx':196608})
        assert response.status_code==200
        policy=resolve(settings=store.get('op')['payload']['settings'])
        assert policy.compaction and policy.num_ctx==196608
        assert client.patch('/jobs/job/operations/op/settings',json={'openhands_num_ctx':-1}).status_code==422


def test_large_check_evidence_is_paged_without_losing_access():
    from coder_worker_runtime import evidence_catalog,evidence_page
    checks=[{'id':f'package-{i}:test','passed':True,'log':f'/checks/{i}.log','log_tail':'large output '*1000} for i in range(1000)]
    evidence={'checks':checks,'revision_id':'exact-revision'}
    catalog=evidence_catalog(evidence,1500)
    assert estimate_tokens(catalog)<=1500
    assert catalog['checks']['total']==1000
    assert catalog['checks']['next_cursor']<1000
    page=evidence_page(evidence,'checks',990)
    assert page['items'][-1]['value']['id']=='package-999:test'
    assert page['next_cursor'] is None
    assert evidence['checks'][0]['log_tail']


def test_failed_checks_keep_actionable_diagnostics_in_context():
    from coder_worker_runtime import evidence_catalog
    evidence={'issues':[{'id':'test','passed':False,'log':'/checks/test.log','log_tail':'ModuleNotFoundError: missing_test'}]}
    assert evidence_catalog(evidence,1000)['issues']['items'][0]['log_tail']=='ModuleNotFoundError: missing_test'


def test_worker_connections_close_after_each_transaction(tmp_path):
    import sqlite3
    store=WorkerStore(tmp_path/'worker')
    with store.connect() as connection:
        assert connection.execute('SELECT 1').fetchone()[0]==1
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):
        connection.execute('SELECT 1')


def test_reconcile_does_not_overwrite_concurrent_completion(monkeypatch,tmp_path):
    import coder_worker_runtime as runtime
    store=WorkerStore(tmp_path/'worker');store.create('op','job','inspect',payload())
    store.update('op',status='running',started=time.time()-30)
    def finished(operation):
        store.update('op',status='succeeded',ended=time.time(),result={'complete':True})
        return False
    monkeypatch.setattr(runtime,'_alive',finished)
    result=runtime.reconcile(store,'op')
    assert result['status']=='succeeded' and result['result']=={'complete':True}


def test_text_model_json_fallback_accepts_prose_and_embedded_fences():
    from coder_worker_runtime import _json
    assert _json('I will inspect the files.\n```json\n{"inspect":{"operation":"files"}}\n```')['inspect']['operation']=='files'
    assert _json('Here is the plan: {"milestones":[{"task":"implement"}]}\nContinue with this plan.')['milestones'][0]['task']=='implement'
    with pytest.raises(ValueError):_json('No structured response was returned')
