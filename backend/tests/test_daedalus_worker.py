import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coder_repository import Repository
from coder_worker_runtime import WorkerStore, launch, cancel, execute
from coder_sdk_runtime import compile_context, pin_task_text
from context_policy import DEFAULTS, resolve, estimate_tokens


def payload(**extra):
    return {'kind':'inspect','request_key':'one','task':'inspect','model':'coder:local','settings':DEFAULTS.copy(),
            'seconds_remaining':30,'calls_remaining':10,'projects_root':'','source_project_id':'',**extra}


@pytest.mark.parametrize('compact',[True,False])
def test_resumed_builder_keeps_current_repair_request_outside_summary(compact):
    policy=resolve(settings={**DEFAULTS,'openhands_num_ctx':8192,'generation_num_predict':512,
                             'daedalus_compaction':'on' if compact else 'off'})
    current='Repair the verified failure: test_lookup_zero fails. Preserve the stored zero; then rerun tests.'
    messages=[{'role':'system','content':'Coding rules'}, {'role':'user','content':'Original attempt: inspect lookup.'},
              {'role':'assistant','content':'Old investigation. '*2000 if compact else 'Old investigation.'},
              {'role':'user','content':current}, {'role':'assistant','content':'Checking source'},
              {'role':'assistant','tool_calls':[{'id':'read'}]}, {'role':'tool','tool_call_id':'read','content':'Current source'},
              {'role':'user','content':'Continue'}, {'role':'assistant','content':'Working'}]
    saved=json.dumps(messages)
    prepared=pin_task_text(messages,current)
    # SDK text-mode serialization happens after pinning and wraps the user
    # request with examples. Context compilation must preserve those examples.
    prepared[1]['content']='Tool protocol example.\n'+prepared[1]['content']+'\nEnd of protocol example.'
    result,changed=compile_context(prepared,[],policy,lambda older:'An incomplete summary omitting the repair.')
    assert changed is compact
    first=next(m['content'] for m in result if m['role']=='user')
    assert current in first and first.startswith('Tool protocol example.') and first.endswith('End of protocol example.')
    assert any(m.get('tool_call_id')=='read' for m in result)
    assert json.dumps(messages)==saved


@pytest.mark.parametrize('source,runner',[
    ('class TestLookup:\n    def test_zero(self):\n        assert 0 == 0\n','pytest'),
    ('import unittest\nclass TestLookup(unittest.TestCase):\n    def test_zero(self):\n        self.assertEqual(0,0)\n','unittest'),
])
def test_python_test_classes_select_their_actual_runner(tmp_path,source,runner):
    from coder_checks import discover
    root=tmp_path/'project';root.mkdir();(root/'test_lookup.py').write_text(source)
    repository=Repository(root,tmp_path/'index');repository.refresh()
    checks=discover(repository)['checks']
    assert runner in next(check['command'] for check in checks if check['id']=='.:tests')
    assert ('pip install pytest' in next(check['command'] for check in checks if check['id']=='.:install')) is (runner=='pytest')


def test_read_only_agent_can_correct_a_missing_source_path(monkeypatch,tmp_path):
    import coder_inference
    from coder_worker_runtime import _read_only
    root=tmp_path/'project';root.mkdir();(root/'api.py').write_text('value = 42\n')
    repository=Repository(root,tmp_path/'index');repository.refresh()
    store=WorkerStore(tmp_path/'worker')
    store.create('op','job','accept',payload(kind='accept'));store.update('op',started=time.time())
    prompts=[]
    answers=[{'inspect':{'operation':'read','path':'tests/api.py'}},
             {'inspect':{'operation':'read','path':'api.py'}}, {'accepted':True,'issues':[]}]
    def model(store,operation_id,role,messages,**kwargs):
        prompts.append(messages[0]['content'])
        return json.dumps(answers[len(prompts)-1])
    monkeypatch.setattr(coder_inference,'local_chat',model)
    result=_read_only(store,'op',repository,'Inspect current source.',{})
    assert result['accepted'] is True and len(prompts)==3
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


def test_duplicate_worker_post_launches_only_one_process(tmp_path):
    store=WorkerStore(tmp_path/'state')
    root=tmp_path/'projects';(root/'source').mkdir(parents=True)
    (root/'source'/'file.py').write_text('print(1)\n')
    request=payload(projects_root=str(root),source_project_id='source')
    assert store.create('op','job','inspect',request)
    launch(store,'op');pid=store.get('op')['pid']
    assert not store.create('op','job','inspect',request)
    launch(store,'op');assert store.get('op')['pid']==pid
    finished=wait(store,'op')
    assert finished['status']=='succeeded',finished['result']
    assert finished['result']['inventory']['files']==1
    assert finished['calls']==0
    assert (root/'source'/'file.py').read_text()=='print(1)\n'


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
    (workspace/'file.py').write_text('print(1)\n')
    repository=Repository(workspace,store.root/'jobs'/'job'/'repository')
    revision=repository.snapshot()['revision']
    store.create('op','job','check',payload(kind='check',revision_id=revision,checks=[{'id':'slow','command':'sleep 60'}]))
    launch(store,'op')
    parent=psutil.Process(store.get('op')['pid'])
    deadline=time.monotonic()+10
    children=[]
    while time.monotonic()<deadline:
        children=parent.children(recursive=True)
        if any(child.name()=='sleep' for child in children):break
        time.sleep(.05)
    assert children
    result=cancel(store,'op')
    assert result['status']=='cancelled'
    assert not any(child.is_running() and child.status()!=psutil.STATUS_ZOMBIE for child in children)


def test_compaction_preserves_tool_pairs_and_original_request():
    policy=resolve(settings={**DEFAULTS,'openhands_num_ctx':6000,'generation_num_predict':256,'daedalus_compaction':'on'})
    messages=[{'role':'system','content':'Instructions'},{'role':'user','content':'Authoritative original request'}]
    messages += [{'role':'assistant','content':'old history '*1800},{'role':'user','content':'old correction'}]
    messages += [{'role':'assistant','content':'working'}, {'role':'assistant','tool_calls':[{'id':'call','function':{'name':'read','arguments':'{}'}}]},
                 {'role':'tool','tool_call_id':'call','content':'source evidence'},{'role':'assistant','content':'continue'}]
    rebuilt,compacted=compile_context(messages,[{'name':'read'}],policy,lambda older:'Old decisions and unresolved issues')
    assert compacted
    assert rebuilt[:2]==messages[:2]
    assert rebuilt[-4:]==messages[-4:]
    assert estimate_tokens({'messages':rebuilt,'tools':[{'name':'read'}]})<=policy.input_budget
    off=resolve(settings={**DEFAULTS,'openhands_num_ctx':6000,'generation_num_predict':256,'daedalus_compaction':'off'})
    with pytest.raises(ValueError,match='disabled'):
        compile_context(messages,[{'name':'read'}],off,lambda _:pytest.fail('Compaction was disabled'))


def test_unfinished_inference_stream_is_not_a_completion(monkeypatch,tmp_path):
    import requests
    import coder_inference
    store=WorkerStore(tmp_path/'state')
    store.create('op','job','plan',payload(kind='plan',ollama_url='http://ollama'))
    store.update('op',started=time.time())
    monkeypatch.setattr(coder_inference,'ensure_context',lambda *args:None)
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def raise_for_status(self):pass
        def iter_lines(self):yield b'{"message":{"content":"partial"}}'
    monkeypatch.setattr(requests,'post',lambda *a,**kw:Response())
    with pytest.raises(RuntimeError,match='without a completion'):
        coder_inference.local_chat(store,'op','architect',[{'role':'user','content':'plan'}])
    assert store.get('op')['calls']==1


def test_verification_cache_resumes_and_rejects_modified_source(tmp_path):
    from coder_worker_runtime import _run_check,read_check_log
    store=WorkerStore(tmp_path/'state')
    root=store.root/'jobs'/'job'/'workspace';root.mkdir(parents=True)
    (root/'module.py').write_text('answer = 42\n')
    repo=Repository(root,store.root/'jobs'/'job'/'repository')
    revision=repo.snapshot()['revision']
    checks=[{'id':'good','command':'echo verified'}, {'id':'bad','command':'exit 1'}]
    request=payload(kind='check',revision_id=revision,checks=checks)
    store.create('op','job','check',request);store.update('op',started=time.time())
    first=_run_check(store,'op',repo,request)
    second=_run_check(store,'op',repo,request)
    assert second['checks'][0]['reused'] and not second['checks'][1].get('reused')
    log=first['checks'][0]['log']
    page=read_check_log(store,store.get('op'),log,0,3)
    assert page['content']=='ver' and page['next_offset']==3
    assert read_check_log(store,store.get('op'),log,3)['content']=='ified\n'
    with pytest.raises(ValueError):read_check_log(store,store.get('op'),str(tmp_path/'secret.log'))
    request['checks']=[{'id':'rewrite','command':"printf 'answer = 0\\n' > module.py"}]
    dirty=_run_check(store,'op',repo,request)
    assert not dirty['passed'] and dirty['checks'][-1]['id']=='verification-source-integrity'
    assert dirty['checks'][-1]['paths']==['module.py']
    request['checks']=[{'id':'restored','command':"test \"$(cat module.py)\" = 'answer = 42'"}]
    assert _run_check(store,'op',repo,request)['passed']
    assert (root/'module.py').read_text()=='answer = 42\n'


def test_architect_checks_are_parsed_without_executing_them(tmp_path):
    from coder_checks import validate_plan
    marker=tmp_path/'must-not-exist'
    def plan(command):return {'milestones':[{'task':'implement','checks':[{'id':'check','command':command}]}]}
    validate_plan(plan(f'touch {marker}'))
    assert not marker.exists()
    with pytest.raises(ValueError,match='shell syntax'):
        validate_plan(plan('python -c "print(1)'))
    with pytest.raises(ValueError,match='Python syntax'):
        validate_plan(plan('python -c "x=1; try: x/0; except: pass"'))
    validate_plan(plan("python -c 'from module import result; assert result == 42'"))


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


def test_architect_can_return_criteria_without_inventing_test_commands():
    from coder_checks import validate_plan
    validate_plan({'milestones':[{'task':'Implement and test the requested behavior','criteria':['Preserve input values']} ]})


def test_narration_continues_to_a_real_finish_within_turn_allowance():
    from types import SimpleNamespace
    from coder_sdk_runtime import drive_to_finish
    events=[]
    class Conversation:
        state=SimpleNamespace(execution_status='FINISHED')
        calls=0
        messages=[]
        def run(self):
            self.calls+=1
            events.append({'kind':'MessageEvent'} if self.calls==1 else {'kind':'ActionEvent','tool_name':'finish'})
        def send_message(self,message):self.messages.append(message)
    conversation=Conversation()
    assert drive_to_finish(conversation,events,lambda:conversation.calls,lambda:3)
    assert conversation.calls==2 and len(conversation.messages)==1
    assert conversation.max_iteration_per_run==2
    events.clear();conversation=Conversation()
    assert not drive_to_finish(conversation,events,lambda:conversation.calls,lambda:1)
    assert conversation.calls==1 and not any(event.get('tool_name')=='finish' for event in events)


def test_text_model_json_fallback_accepts_prose_and_embedded_fences():
    from coder_worker_runtime import _json
    assert _json('I will inspect the files.\n```json\n{"inspect":{"operation":"files"}}\n```')['inspect']['operation']=='files'
    assert _json('Here is the plan: {"milestones":[{"task":"implement"}]}\nContinue with this plan.')['milestones'][0]['task']=='implement'
    with pytest.raises(ValueError):_json('No structured response was returned')


def test_native_model_text_tool_fallback_requires_complete_known_unfenced_call():
    from coder_sdk_runtime import text_tool_calls
    tools=[{'function':{'name':'glob'}}]
    envelope='<function name="glob">{"pattern":"**/*.py","summary":"Inspect source"}</function>'
    content,calls=text_tool_calls('Inspecting source.\n'+envelope,tools)
    assert content.strip()=='Inspecting source.'
    assert calls[0]['function']['name']=='glob'
    assert json.loads(calls[0]['function']['arguments'])['pattern']=='**/*.py'
    assert text_tool_calls(envelope.replace('name="','call="'),tools)[1][0]['function']['name']=='glob'
    for text in ('```xml\n'+envelope+'\n```',envelope.replace('glob','unknown'),envelope[:-11],envelope.replace('{"pattern"','{broken')):
        assert text_tool_calls(text,tools)==(text,[])
    stopped=envelope.removesuffix('</function>')
    assert text_tool_calls(stopped,tools)==(stopped,[])
    assert text_tool_calls(stopped,tools,allow_stopped=True)[1][0]['function']['name']=='glob'
    for incomplete in (stopped[:-1],stopped.replace('glob','unknown'),'```xml\n'+stopped):
        assert text_tool_calls(incomplete,tools,allow_stopped=True)==(incomplete,[])


def test_compaction_reuses_only_an_unchanged_history_prefix():
    from coder_sdk_runtime import checkpoint_delta
    old=[{'role':'assistant','content':'old decision'}]
    checkpoint={'history':old,'summary':'The earlier decision'}
    new={'role':'tool','content':'new result'}
    assert checkpoint_delta([*old,new],checkpoint)=={'checkpoint':'The earlier decision','new_evidence':[new]}
    assert checkpoint_delta(old,checkpoint)['new_evidence']==[]
    changed=[{'role':'assistant','content':'different decision'},new]
    assert checkpoint_delta(changed,checkpoint)==changed


def test_native_fallback_counts_only_consecutive_missing_calls():
    from types import SimpleNamespace
    from coder_sdk_runtime import native_narration_streak
    response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None))])
    assert native_narration_streak(response,[{'function':{'name':'read'}}],True,1)==2
    assert native_narration_streak(response,[],True,1)==0
    assert native_narration_streak(response,[{}],False,1)==0
    response.choices[0].message.tool_calls=[{'name':'read'}]
    assert native_narration_streak(response,[{}],True,1)==0


def test_dependency_free_javascript_runs_its_regression_tests(tmp_path):
    from coder_checks import discover
    from coder_repository import Repository
    root=tmp_path/'project';root.mkdir()
    (root/'groups.mjs').write_text('export const groupBy = () => new Map();')
    (root/'test_groups.mjs').write_text("import assert from 'node:assert/strict'; assert.fail('regression');")
    repository=Repository(root,tmp_path/'index');repository.refresh()
    checks=discover(repository)['checks']
    assert any(check['command']=='node --test test_groups.mjs' for check in checks)


def test_verification_discovers_both_languages_in_one_package(tmp_path):
    from coder_checks import discover
    from coder_repository import Repository
    root=tmp_path/'project';root.mkdir()
    (root/'package.json').write_text('{"scripts":{"test":"node --test"}}')
    (root/'requirements.txt').write_text('')
    (root/'test_api.py').write_text('import unittest\nclass TestAPI(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
    repository=Repository(root,tmp_path/'index');repository.refresh()
    commands=[check['command'] for check in discover(repository)['checks']]
    assert 'npm run test' in commands
    assert any('unittest discover' in command for command in commands)


def test_benchmark_does_not_mix_source_settings_or_dependency_versions():
    from evals.run_coder_benchmark import validate_resume
    expected={'settings':{'num_ctx':32768},'source_hashes':{'worker.py':'hash'},'environment':{'sdk':'pinned'}}
    validate_resume({**expected,'results':[]},expected)
    for key in expected:
        with pytest.raises(ValueError,match='fresh --state'):
            validate_resume({**expected,key:{}},expected)
