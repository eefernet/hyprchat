"""Regressions from the first real policy-6 pilot, with no inference."""
import copy
from pathlib import Path

import pytest

from coder_review import author, judge, normalize_checks, scope_brief
from coder_repository import Repository
from coder_worker_runtime import WorkerStore
from coder_sdk_runtime import drive_to_checkpoint
from context_policy import DEFAULTS


def fixture(tmp_path):
    root=tmp_path/'project';root.mkdir()
    (root/'subject.py').write_text('VALUE = 1\n')
    repo=Repository(root,tmp_path/'repo',DEFAULTS['daedalus_exclude_dirs']);repo.refresh()
    revision=repo.snapshot()['revision']
    payload={'revision_id':revision,'brief':{'outcomes':[{
        'id':'o1','text':'Implement the application with README and tests',
        'evidence_types':['behavior','documentation','tests']}], 'constraints':[], 'components':[], 'approach':[], 'validation':[]},
        'evidence':{'checks':[]},'visual_policy':{'enabled':False},'protected_files':[]}
    store=WorkerStore(tmp_path/'worker');store.create('review','job','accept',payload)
    return repo,store,payload


def test_optional_evidence_cannot_become_an_impossible_release_gate():
    brief={'outcomes':[
        {'id':'o1','text':'Build app','evidence_types':['behavior','documentation','tests','preservation']},
        {'id':'o2','text':'Responsive UI','evidence_types':['behavior','visual']}]}
    original=copy.deepcopy(brief)
    scoped=scope_brief(brief,{'visual_policy':{'enabled':False},'protected_files':[]})
    assert scoped['outcomes'][0]['evidence_types']==['behavior','documentation','tests']
    assert scoped['outcomes'][1]['evidence_types']==['behavior']
    assert scope_brief(brief,{'visual_policy':{'enabled':True},'protected_files':['notes.txt']})==brief
    assert brief==original


def test_hardcoded_selenium_audit_cannot_execute_against_an_unrelated_server(tmp_path):
    repo,store,payload=fixture(tmp_path)
    check={'outcomes':['o1'],'evidence_types':['behavior'],'files':{'test_behavior.py':
        'from selenium import webdriver\ndriver=webdriver.Chrome()\ndriver.get("http://localhost:8000")\nassert driver.title=="Expense Tracker"\n'},
        'command':'python3 "$DAEDALUS_AUDIT_DIR/test_behavior.py"'}
    with pytest.raises(ValueError,match='fixed localhost port'):
        normalize_checks({'checks':[check]},repo,payload)


def test_audit_author_must_cover_documentation_as_well_as_behavior(tmp_path):
    repo,store,payload=fixture(tmp_path)
    answer={'checks':[{'outcomes':['o1'],'evidence_types':['behavior'],'files':{'test_behavior.py':
        'from subject import VALUE\nassert VALUE == 1\n'},'command':'python3 "$DAEDALUS_AUDIT_DIR/test_behavior.py"'}]}
    def response(*args,validate,**kwargs):
        validate(answer);return answer
    with pytest.raises(ValueError,match='o1:documentation'):
        author(store,'review',repo,response)
    answer['checks'].append({'kind':'file','outcomes':['o1'],'evidence_types':['documentation'],
        'path':'README.md','assertions':[{'kind':'nonempty'}]})
    result=author(store,'review',repo,response)
    assert len(result['checks'])==2  # Missing README is executable failure, not a format error.


def test_incomplete_delivery_cannot_be_labeled_environment_without_evidence(tmp_path):
    repo,store,payload=fixture(tmp_path)
    payload['evidence']['checks']=[{'id':'audit-1','passed':False,'environment_fault':False,
        'revision_id':payload['revision_id'],'log_tail':"ModuleNotFoundError: No module named 'fastapi'"}]
    store.update('review',payload=payload)
    answer={'outcomes':[{'id':'o1','status':'failed','reason':'FastAPI could not be imported'}],
        'disposition':'environment','summary':'Missing FastAPI dependency','corrections':[]}
    def response(*args,validate,**kwargs):
        validate(answer);return copy.deepcopy(answer)
    with pytest.raises(ValueError,match='No failing environment check'):
        judge(store,'review',repo,response)
    answer['disposition']='application_defect'
    assert judge(store,'review',repo,response)['disposition']=='application_defect'
    payload['evidence']['checks'][0]['environment_fault']=True
    store.update('review',payload=payload);answer['disposition']='environment'
    assert judge(store,'review',repo,response)['disposition']=='environment'


def test_accepted_review_without_requested_tests_is_corrected_before_publication(tmp_path):
    repo,store,payload=fixture(tmp_path)
    answer={'outcomes':[{'id':'o1','status':'passed','reason':'Source looks correct'}],
        'disposition':'accepted','summary':'Ready','corrections':[]}
    def response(*args,validate,**kwargs):
        validate(answer);return answer
    with pytest.raises(ValueError,match='Acceptance lacks passing evidence'):
        judge(store,'review',repo,response)


@pytest.mark.parametrize('partial_edit',[False,True])
def test_inspection_or_next_step_narration_gets_one_recovery_before_checkpoint(partial_edit):
    events=[];calls=[0];tree=['original'];recovery=[False];prompts=[]
    class Conversation:
        def run(self):
            calls[0]+=1
            if calls[0]==1:
                events.append({'kind':'ActionEvent','tool_name':'file_editor','action':{'command':'view'}})
                if partial_edit:tree[0]='backend only'
                events.append({'kind':'MessageEvent','llm_message':{'role':'assistant','content':[
                    {'type':'text','text':'Now let me create the frontend files:'}]}})
            else:
                tree[0]='implemented'
                events.append({'kind':'ActionEvent','tool_name':'file_editor','action':{'command':'create'}})
                events.append({'kind':'MessageEvent','llm_message':{'role':'assistant','content':[
                    {'type':'text','text':'Implementation and tests are ready for review.'}]}})
        def send_message(self,text):prompts.append(text)
    drive_to_checkpoint(Conversation(),events,lambda:calls[0],lambda:20,lambda:tree[0],
                        lambda:recovery[0],lambda:recovery.__setitem__(0,True))
    assert tree[0]=='implemented' and calls[0]==2 and len(prompts)==1 and recovery[0]


def test_audit_name_error_is_corrected_before_application_repair(tmp_path):
    from coder_review import audit_program_error
    repo,store,payload=fixture(tmp_path)
    audit=tmp_path/'audit'
    check={'id':'audit-1','origin':'independent','passed':False,'audit_dir':str(audit),
        'log_tail':f'Traceback (most recent call last):\n  File "{audit}/test_behavior.py", line 7, in test\n    root = os.environ["DAEDALUS_PROJECT_ROOT"]\nNameError: name \'os\' is not defined\n'}
    assert audit_program_error(check)
    project_error={**check,'log_tail':check['log_tail'].replace(str(audit/'test_behavior.py'),str(repo.root/'subject.py'))}
    assert not audit_program_error(project_error)
    assert not audit_program_error({**check,'log_tail':check['log_tail'].replace('NameError:', 'AssertionError:')})
    payload['evidence']['checks']=[check,{'id':'project-test','passed':False}]
    store.update('review',payload=payload)
    answer={'outcomes':[{'id':'o1','status':'failed','reason':'Project tests fail'}],
        'disposition':'application_defect','summary':'Repair application','corrections':[]}
    def response(*args,validate,**kwargs):
        validate(answer);return answer
    with pytest.raises(ValueError,match='Correct the independent audit program'):
        judge(store,'review',repo,response)
    answer['disposition']='ambiguous'
    assert judge(store,'review',repo,response)['disposition']=='ambiguous'
