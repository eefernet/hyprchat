"""Qualification rejects stale verification, budget failures and weak families."""
import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('engine_report',Path(__file__).parents[1]/'qualify_engine_report.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def report():
    trials=[]
    for family,count in [('core',24),('web',12),('desktop',12)]:
        for index in range(count):
            trials.append({'family':family,'key':f'{family}-{index}','passed':True,'finished':1,
                'jobs':[{'id':f'{family}-{index}-{turn}','revision_id':'r',
                         'verification_summary':{'revision_id':'r','checks_ok':True,'checks_passed':False},
                         'exact_acceptance_verified':True} for turn in range(2)]})
    return {'complete':True,'behavioral_gate_passed':True,'trials':trials}


def test_every_family_must_qualify_even_when_overall_rate_is_high():
    data=report();assert module.assess(data)['rollout_behavior_gate_passed']
    for trial in data['trials'][-3:]:trial['passed']=False
    result=module.assess(data)
    assert result['families']['desktop']['rate']==.75
    assert not result['rollout_behavior_gate_passed']


def test_external_behavior_cannot_excuse_stale_checks_or_spent_allowance():
    data=report()
    data['trials'][0]['jobs'][1]['verification_summary']['revision_id']='old'
    data['trials'][1]['jobs'][0]['stop_limit']='model_calls'
    result=module.assess(data)
    assert result['families']['core']['successes']==22
    assert len(result['failures'])==2


def test_incomplete_campaign_cannot_qualify():
    data=report();data['complete']=False
    assert not module.assess(data)['rollout_behavior_gate_passed']


def test_controller_contract_failures_are_strict_failures_even_if_private_behavior_passed():
    data=report()
    data['trials'][24]['jobs'][1]['verification_summary']['criteria']=[{'status':'failed','id':'ui'}]
    data['trials'][25]['jobs'][1]['verification_summary']['criteria']=[{'status':'unverified','id':'unsupported'}]
    result=module.assess(data)
    assert result['families']['web']['successes']==11
    assert result['failures'][0]['trial']=='web-0'
    assert 'UI outcome failed' in result['failures'][0]['reasons'][0]
    data=report();data['trials'].pop()
    assert not module.assess(data)['rollout_behavior_gate_passed']


def test_reliability_requires_observed_interfaces_without_regrading_baseline():
    data=report()
    assert module.assess(data)['families']['core']['successes']==24
    data['freeze']={'cohort':'reliability'}
    for trial in data['trials']:
        for job in trial['jobs']:
            job['verification_summary'].update(exercised=True,unverified_interfaces=[])
    assert module.assess(data)['rollout_behavior_gate_passed']
    data['trials'][0]['jobs'][0]['verification_summary']['exercised']=False
    data['trials'][24]['jobs'][0]['verification_summary']['unverified_interfaces']=['web']
    result=module.assess(data)
    assert result['families']['core']['successes']==23
    assert result['families']['web']['successes']==11
    assert len(result['failures'])==2


def test_new_behavior_requires_supported_ui_checks_and_rejects_incomplete_candidates():
    data=report()
    job=data['trials'][24]['jobs'][0];job['engine_behavior_version']=1
    job['verification_summary']['criteria']=[{'supported':True,'status':'unverified'}]
    data['trials'][25]['jobs'][0]['verification_summary']['incomplete_reason']='Generation stopped: output_limit'
    result=module.assess(data)
    assert result['families']['web']['successes']==10
    assert 'supported UI outcome not verified' in result['failures'][0]['reasons'][0]
    assert 'incomplete candidate' in result['failures'][1]['reasons'][0]
