import copy
import json
import subprocess
import sys

import pytest

from coder_contracts import (request_sources,merge_requirements,normalize_probe,targeted_correction)
from coder_failures import classify_failure,malformed_native_call
from context_policy import DEFAULTS,validate_patch,resolve,operation_settings,thinking_options


def test_requirement_repair_keeps_valid_slots_and_rejects_omitted_failure():
    sources=request_sources('Preserve groupBy. Add countBy.\nAdd regression tests.')
    draft={};pending=[]
    good={'slot':1,'text':'Preserve the public API','kind':'preservation','source_refs':['request-1']}
    bad={'slot':2,'text':'Add counts','kind':'behavior','source_refs':['old-readme']}
    with pytest.raises(ValueError,match='supplied request IDs'):
        merge_requirements({'requirements':[good,bad]},sources,draft,pending)
    with pytest.raises(ValueError,match='Uncorrected'):
        merge_requirements({'requirements':[good]},sources,draft,pending)
    result=merge_requirements({'requirements':[{**good,'text':'Delete groupBy'}, {**bad,'source_refs':['request-1']}]},sources,draft,pending)
    assert result[0]['text']=='Preserve the public API'
    assert result[1]['source_quote']==sources['request-1']
    assert not pending


def test_probe_correction_preserves_identity_coverage_and_unaffected_data():
    requirement={'id':'r1','kind':'behavior','source_refs':['request-1']}
    check=normalize_probe({'runner':'python','program':'assert 1 == 2'},requirement,'check-r1')
    before=copy.deepcopy(check)
    same,reason=targeted_correction({'disposition':'code_defect'},check,requirement,{'request-1':'Add counts'})
    assert same==before and reason is None
    changed,reason=targeted_correction({'disposition':'probe_defect','reason':'Wrong fixture','source_refs':['invented-source'],
        'replacement':{'id':'injected','requirement_ids':['other'],'runner':'python','program':'assert 2 == 2'}},
        check,requirement,{'request-1':'Add counts'})
    assert changed['id']==check['id'] and changed['requirement_ids']==['r1'] and check==before
    assert reason['source_quote']=='Add counts'
    assert reason['source_refs']==requirement['source_refs']


def test_unexecuted_assertions_and_stale_acceptance_evidence_cannot_pass():
    from coder_contracts import attach_acceptance_evidence
    from coder_verification import validate_acceptance
    requirement={'id':'r1','kind':'behavior'}
    with pytest.raises(ValueError,match='top level'):
        normalize_probe({'runner':'python','program':'def test_value():\n    assert False'},requirement,'check-r1')
    answer={'accepted':True,'coverage':[{'requirement_id':'r1','status':'passed','reason':'Verified'}]}
    check={'id':'actual-check','revision_id':'current','requirement_ids':['r1'],'passed':True,'origin':'independent'}
    attach_acceptance_evidence(answer,[check],'current')
    assert answer['coverage'][0]['check_ids']==['actual-check']
    validate_acceptance(answer,[requirement],[check],'current',None)
    attach_acceptance_evidence(answer,[check],'different')
    with pytest.raises(ValueError):validate_acceptance(answer,[requirement],[check],'different',None)


@pytest.mark.parametrize('value,passed',[(2,True),(3,False)])
def test_independent_probe_rejects_broken_implementation(tmp_path,value,passed):
    from coder_verification import probe_checks
    (tmp_path/'counter.py').write_text(f'def count(items): return {value}\n')
    check=normalize_probe({'runner':'python','program':'from counter import count\nassert count([1,2]) == 2'},
        {'id':'r1','kind':'behavior'},'check-r1')
    result=subprocess.run(probe_checks([check])[0]['command'],shell=True,cwd=tmp_path,capture_output=True)
    assert (result.returncode==0)==passed


@pytest.mark.parametrize('broken',[False,True])
def test_upload_journey_checks_real_cli_artifact(tmp_path,broken):
    import hashlib
    import tarfile
    from evals.run_coder_journeys import CLI_SOURCE,cli_golden
    source=tmp_path/'ledger.py'
    source.write_text(CLI_SOURCE if broken else CLI_SOURCE.replace('        next(rows,None)  # bug: skips the first expense\n',''))
    archive=tmp_path/'project.tar.gz'
    with tarfile.open(archive,'w:gz') as bundle:bundle.add(source,arcname='ledger.py')
    artifact={'storage_path':str(archive),'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
    if broken:
        with pytest.raises(AssertionError):cli_golden(artifact,tmp_path/'downloaded',0)
    else:assert cli_golden(artifact,tmp_path/'downloaded',0)['passed']


def test_stage_output_policy_preserves_old_jobs():
    patch=validate_patch({'daedalus_role_outputs':{'reviewer':16384},'daedalus_role_thinking':{'reviewer':'on'}},DEFAULTS)
    payload={'settings':{**DEFAULTS,**patch},'policy_version':3}
    assert resolve('reviewer',operation_settings(payload)).num_predict==16384
    assert resolve('reviewer',operation_settings({**payload,'policy_version':2})).num_predict==4096
    assert thinking_options('reviewer',payload,{'capabilities':[]})==({},'unsupported')
    assert thinking_options('reviewer',payload,{'capabilities':['thinking']})[0]=={'think':True}
    with pytest.raises(ValueError,match='cannot fit'):
        validate_patch({'daedalus_role_outputs':{'reviewer':32768}},DEFAULTS)


def test_text_history_preserves_batched_calls_and_observations():
    from coder_sdk_runtime import text_tool_history
    calls=[{'id':str(i),'function':{'name':'terminal','arguments':json.dumps({'command':str(i)})}} for i in range(3)]
    history=[{'role':'assistant','content':'Original explanation','tool_calls':calls},
             *[{'role':'tool','tool_call_id':str(i),'content':'result '+str(i)} for i in range(3)]]
    original=copy.deepcopy(history);converted=text_tool_history(history)
    assert history==original and len(converted)==6
    assert [row['tool_calls'][0] for row in converted[:3]]==calls
    assert [row['content'] for row in converted[:3]]==['Original explanation','','']
    assert converted[3:]==history[1:]


def test_failure_categories_do_not_retry_application_or_network_errors():
    malformed='Ollama_chatException XML syntax error on line 7'
    assert malformed_native_call(malformed)
    assert not malformed_native_call('Connection refused')
    assert not malformed_native_call('Application XML parser failed')
    assert classify_failure('Response reached the configured completion allowance','verify')['category']=='output_limit'
    assert classify_failure('Preview failed to start','code')['category']=='preview_startup'
    assert classify_failure(malformed,'code',recovery_attempted=True)['recovery_attempted']


def test_rollout_needs_two_distinct_complete_runs_and_journeys():
    from evals.rollout_gate import rollout_verdict
    from evals.coder_fixtures import FIXTURES
    rows=[{'fixture':f['id'],'job_id':'first-'+f['id'],'model':'local','passed':True,'false_acceptance':False,
        **({'followups':{'passed':True,'original_artifact_preserved':True,
            'requests':[{'evidence':{'passed':True}} for _ in range(2)]}} if f['id']=='grouping' else {})} for f in FIXTURES]
    common={'settings':dict(DEFAULTS),'models':{'local':{'digest':'abc'}},'source_hashes':{'evals/coder_fixtures.py':'fixture'}}
    first={**common,'workflow_version':3,'policy_version':3,'results':rows}
    second={**first,'results':[{**r,'job_id':r['job_id'].replace('first','second')} for r in rows]}
    baseline={**common,'workflow_version':2,'results':rows}
    journeys={**common,'original_artifacts_preserved':True,'results':[
        {'scenario':s,'step':i,'evidence':{'passed':True}} for s,n in [('web',3),('upload',2)] for i in range(n)]}
    assert rollout_verdict(first,second,baseline,journeys)['eligible']
    assert not rollout_verdict(first,first,baseline,journeys)['eligible']
    assert not rollout_verdict(first,second,baseline,{**journeys,'results':journeys['results'][:1]})['eligible']
    bad=copy.deepcopy(second);bad['results'][0]['false_acceptance']=True
    assert not rollout_verdict(first,bad,baseline,journeys)['eligible']
