"""Qualification rejects stale verification, budget failures and weak families."""
import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('engine_report',Path(__file__).parents[1]/'qualify_engine_report.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def report():
    import hashlib
    trials=[]
    for case in module.CASES:
        for rep in range(1,4):
            key = f"{case['id']}-{rep}"
            jobs = []
            for turn in range(2):
                identity = f'{key}-{turn}'
                rev = hashlib.sha1(identity.encode()).hexdigest()
                digest = hashlib.sha256(identity.encode()).hexdigest()
                jobs.append({'id':identity,'revision_id':rev,'verification_summary':{'revision_id':rev,'checks_ok':True},
                             'checks':[{'passed':True,'revision_id':rev}],'candidate_artifact':{'sha256':digest},
                             'held_out':{'passed':True,'archive_sha256':digest,'checks':[{'passed':True}]},
                             'exact_acceptance_verified':True})
            trials.append({'family':case['family'],'case':case['id'],'repeat':rep,'key':key,'passed':True,'finished':1,'jobs':jobs})
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


def test_failed_build_without_archive_remains_a_measured_failure(monkeypatch):
    import engine_identity
    monkeypatch.setattr(engine_identity, 'validate', lambda *args: None)
    data = report()
    data['freeze'] = {'prompt_bank_version': 2, 'environment': {
        'judge_identity': {'sha256': 'judge'}, 'judge_adapter_sha256': 'adapter'}}
    for trial in data['trials']:
        for job in trial['jobs']:
            job['held_out']['judge_identity_sha256'] = 'judge'
    failed = data['trials'][0]
    failed.update(passed=False, error='Build failed before an archive was produced')
    del failed['jobs'][0]['held_out']
    result = module.assess(data)
    assert not result['invalid_report']
    assert result['failures'][0]['trial'] == failed['key']
    failed['jobs'][0]['held_out'] = {'judge_identity_sha256': 'stale'}
    assert module.assess(data)['invalid_report']
    del failed['jobs'][0]['held_out']
    failed['passed'] = True
    assert module.assess(data)['invalid_report']


def test_stuck_sdk_cannot_pass_strict_grading_on_existing_green_checks():
    data=report()
    data['trials'][0]['jobs'][1]['last_patch']={'agent_finished':False,'execution_status':'stuck'}
    grade=module.assess(data)
    assert grade['families']['core']['successes']==23
    assert 'worker stopped before completion' in grade['failures'][0]['reasons'][0]
    # A dependency deferral on a finished SDK turn can be resolved by the
    # subsequent successful controller-owned preparation and verification.
    data['trials'][0]['jobs'][1]['last_patch'].update(execution_status='conversationexecutionstatus.finished')
    assert module.assess(data)['families']['core']['successes']==24


def test_controller_contract_failures_are_strict_failures_even_if_private_behavior_passed():
    data=report()
    data['trials'][24]['jobs'][1]['verification_summary']['criteria']=[{'status':'failed','id':'ui'}]
    data['trials'][25]['jobs'][1]['verification_summary']['criteria']=[{'status':'unverified','id':'unsupported'}]
    result=module.assess(data)
    assert result['families']['web']['successes']==11
    assert result['failures'][0]['trial']==data['trials'][24]['key']
    assert 'UI outcome failed' in result['failures'][0]['reasons'][0]
    data=report();data['trials'].pop()
    assert not module.assess(data)['rollout_behavior_gate_passed']


def test_reliability_requires_observed_interfaces_without_regrading_baseline():
    data=report()
    assert module.assess(data)['families']['core']['successes']==24
    data['freeze']={'cohort':'reliability'}
    for trial, case in zip(data['trials'], [c for c in module.RELIABILITY for _ in range(3)]):
        trial.update(case=case['id'], key=f"{case['id']}-{trial['repeat']}")
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


def test_strict_grader_rejects_corrupted_slots_and_evidence():
    from copy import deepcopy
    for kind in ('duplicate_slot','repeated_job','failed_private','missing_private','wrong_hash',
                 'empty_controller','empty_private','missing_revision','wrong_parent','top_level'):
        data = report()
        trial = data['trials'][0]
        job = trial['jobs'][0]
        if kind == 'duplicate_slot':
            data['trials'][1] = deepcopy(trial)
        elif kind == 'repeated_job':
            trial['jobs'][1]['id'] = job['id']
        elif kind == 'failed_private':
            job['held_out']['checks'][0]['passed'] = False
        elif kind == 'missing_private':
            del job['held_out']
        elif kind == 'wrong_hash':
            job['held_out']['archive_sha256'] = '0'*64
        elif kind == 'empty_controller':
            job['checks'] = []
        elif kind == 'empty_private':
            job['held_out']['checks'] = []
        elif kind == 'missing_revision':
            del job['revision_id']
            del job['verification_summary']['revision_id']
        elif kind == 'wrong_parent':
            trial['jobs'][1]['source_revision_id'] = '0'*40
        else:
            data['behavioral_gate_passed'] = False
        result = module.assess(data)
        assert result['invalid_report'] and not result['rollout_behavior_gate_passed'], kind


def test_exact_family_floors_and_explicit_user_entrypoint():
    data = report()
    for family, misses in [('core',4),('web',2),('desktop',2)]:
        for trial in [t for t in data['trials'] if t['family']==family][:misses]:
            trial.update(passed=False, jobs=[], error='Application did not deliver')
    assert module.assess(data)['rollout_behavior_gate_passed']
    assert not module.assess(data, require_composer=True)['rollout_behavior_gate_passed']


def test_user_workflow_requires_verbatim_messages_exact_download_and_parent():
    data = report()
    data.update(entrypoint='daedalus-composer', model='local', model_digest='f'*64)
    for trial in data['trials']:
        # This fixture has no bank version and represents a historical v1 report.
        case = next(c for c in module.CASES_V1 if c['id']==trial['case'])
        for i, job in enumerate(trial['jobs']):
            prompt = case['followup'] if i else case['task']
            job.update(original_user_request=prompt, conversation_id=trial['key'], project_id=trial['key'],
                       source_revision_id=trial['jobs'][0]['revision_id'] if i else '',
                       mode='edit_project' if i else 'build_from_prompt', chat_request_key=str(i+1),
                       model=data['model'],model_digest=data['model_digest'])
            job['verification_summary']['exercised']=True
            job['checks'][0].update(coverage_observed=True,execution_succeeded=True,source_bindings=['app'])
            if case['family']=='core':
                probes=module.private_probes(case, bool(i))
                job['held_out']['checks']=[{'id':f'probe-{n}','passed':True,'expected':p,'exit':p.get('code',0),
                    'stdout':p.get('stdout',module.json.dumps(p.get('json'))),'stderr':'error' if p.get('stderr_nonempty') else ''}
                    for n,p in enumerate(probes)]
            else:
                steps=case['steps']+(case['followup_steps'] if i else [])
                job['held_out']['checks']=[{'id':f'step-{n}','passed':True,'expected':step[k],'comparison':k,
                    'actual':[] if k=='absent_name' else step[k]} for n,step in enumerate(steps)
                    for k in ('equals','contains','absent_name') if k in step]
            job['composer']={'prompt':prompt,'message_id':i+1,'workflow_ids':[job['id']],
                             'download_sha256':job['candidate_artifact']['sha256']}
    assert module.assess(data,require_composer=True)['user_workflow_gate_passed']
    data['trials'][0]['jobs'][1]['composer']['prompt']='evaluator-supplied substitute'
    result=module.assess(data,require_composer=True)
    assert not result['user_workflow_gate_passed'] and result['invalid_report']


def test_false_ui_verification_requires_same_action_target_revision_and_archive():
    case=next(c for c in module.DEVELOPMENT if c['id']=='dev-web')
    steps=case['steps']+case['followup_steps']
    index=next(i for i,s in enumerate(steps) if s.get('selector')=='#amount' and s.get('equals')=='')
    evidence={'revision_id':'abc','targets':[{'selector':'#amount'}]}
    criterion={'id':'clear-fields','status':'passed','revision_id':'abc','property':'value','expected':'',
               'action':{'name':'Reset'},'evidence':[evidence]}
    row={'id':f'step-{index}','passed':False,'comparison':'equals','expected':'','actual':'20'}
    job={'id':'test','revision_id':'abc','candidate_artifact':{'sha256':'digest'},
         'verification_summary':{'criteria':[criterion]},'held_out':{'archive_sha256':'digest','checks':[row]}}
    assert module.conflicting_ui_evidence(job,case,True)[0]['actual']=='20'
    for field,other in [('revision_id','other'),('action',{'name':'Calculate'}),('expected','Ready'),
                        ('evidence',[{'revision_id':'abc','targets':[{'selector':'#percent'}]}])]:
        original=criterion[field]
        criterion[field]=other
        assert not module.conflicting_ui_evidence(job,case,True)
        criterion[field]=original
    job['held_out']['archive_sha256']='other'
    assert not module.conflicting_ui_evidence(job,case,True)


def test_paired_interval_keeps_repeated_trials_together():
    trials=[{'case':c,'family':'web','key':f'{c}-{i}'} for c in ('form','list') for i in range(3)]
    wins={t['key'] for t in trials if t['case']=='form'}
    losses={t['key'] for t in trials if t['case']=='list'}
    assert module.paired_interval(trials,wins,losses)==[-100,100]
    assert module.paired_interval(trials,set(),set())==[0,0]
