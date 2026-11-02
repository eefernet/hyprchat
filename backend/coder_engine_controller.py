"""Small durable workflow around one SDK conversation and independent checks."""
from __future__ import annotations


async def step(job, controller):
    store, identity = controller.store, job['id']
    state = job['state']

    async def save(**changes):
        return await store.save(identity, **changes)

    async def operate(kind, **extra):
        fields = ('project_id', 'ui_checks', 'execution_commands', 'protected_files', 'baseline_test_files', 'baseline_revision',
                  'required_tests', 'repair_feedback', 'repair_round', 'ui_contract_version', 'ui_contract', 'ui_contract_context', 'ui_contract_bindings')
        return await controller._operate(job, kind, **{k: job[k] for k in fields if k in job}, **extra)

    if state in {'queued', 'inspecting'}:
        result, _ = await operate('inspect')
        resume = job.get('resume_after_inspect')
        next_state = resume if resume in {'coding', 'checking', 'candidate_packaging'} else (
            'baselining' if result['inventory']['files'] else 'coding')
        await save(state=next_state, inventory=result['inventory'], workspace=result['workspace'],
                   revision_id=result['snapshot']['revision'],
                   baseline_revision=job.get('baseline_revision') or result['snapshot']['revision'],
                   verification=result.get('verification', {}), resume_after_inspect='')
    elif state == 'baselining':
        result, current = await operate('check', baseline=True)
        await store.record_check(identity, current['worker_operation'], job['revision_id'], result)
        await save(state='coding', baseline_checks=result['checks'],
                   required_tests=result.get('required_tests', []), baseline_test_files=result.get('baseline_test_files', []),
                   ui_contract_context=result.get('ui_contract_context', job.get('ui_contract_context', [])),
                   ui_contract_bindings=result.get('ui_contract_bindings', job.get('ui_contract_bindings', {})))
    elif state == 'coding':
        result, _ = await operate('code')
        await save(state='checking', revision_id=result['snapshot']['revision'], inventory=result['inventory'],
                   last_patch=result, model_digest=result.get('model_digest', job.get('model_digest', '')),
                   repair_round=max(job.get('repair_round', 0), min(2, result.get('hook_denials', 0))))
    elif state == 'checking':
        result, current = await operate('check')
        await store.record_check(identity, current['worker_operation'], job['revision_id'], result)
        summary = result['summary']
        if job.get('recovery_failure'):
            from coder_engine_recovery import incomplete
            summary = incomplete(summary, job['recovery_failure'])
        common = dict(checks=result['checks'], verification_summary=summary,
                      verified_outputs=result.get('verified_outputs', []),
                      execution_profile=result['execution_profile'], verification=result['execution_profile'],
                      verification_version=3, acceptance=None)
        if job.get('ui_contract_version'):
            common['ui_contract_context'] = result.get('ui_contract_context', job.get('ui_contract_context', []))
        failures = summary['failures']
        environment_fault = any(c.get('environment_fault') for c in result['checks'])
        if job.get('recovery_failure'):
            await save(state='candidate_packaging', delivery_status='review_candidate',
                       blocker='Generation stopped. Review the retained incomplete candidate and its revision-specific checks.',
                       resume_state='coding',
                       stop_limit='application_repairs' if job.get('repair_round', 0) >= 2 else '', **common)
        elif environment_fault and job.get('preparation_retries', 0) < 1:
            await save(state='checking', preparation_retries=job.get('preparation_retries', 0) + 1,
                       attempt=job.get('attempt', 0) + 1, **common)
        elif failures and job.get('repair_round', 0) < 2 and not environment_fault:
            await save(state='coding', repair_round=job.get('repair_round', 0) + 1,
                       repair_feedback=failures, **common)
        else:
            reason = ('Checks passed on this revision. Review it, then Accept or request changes.' if summary['checks_passed'] else
                      'Some checks failed; review the retained candidate.' if failures else
                      'Tests ran, but existing test files changed. Review the changed expectations before accepting.' if summary.get('changed_baseline_tests') else
                      'Some requested UI outcomes remain unverified. Review the requirement evidence before accepting.' if summary.get('unverified_ui_criteria') else
                      'Built, but requested interface behavior has not been independently exercised. Review the candidate.' if summary.get('unverified_interfaces') else
                      'Built, but no executed behavioral test established correctness. Review the candidate.')
            await save(state='candidate_packaging', delivery_status='review_candidate', blocker=reason,
                       resume_state='checking', stop_limit='application_repairs' if failures and job.get('repair_round', 0) >= 2 else '', **common)
    elif state == 'candidate_packaging':
        result, current = await operate('package', candidate=True,
            verified_outputs=job.get('verified_outputs', []),
            verification_summary=job.get('verification_summary', {}),
            run_instructions=job.get('execution_profile', {}).get('profiles', []))
        artifact = await controller._deliver(current, result, candidate=True)
        if controller._EVENTS:
            await controller._EVENTS.emit(job['conversation_id'], 'file_ready', {**artifact, 'workflow_id': identity})
        return True
    else:
        raise ValueError('Unknown modern engine state: ' + state)
    return False
