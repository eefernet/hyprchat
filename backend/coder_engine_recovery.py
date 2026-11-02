"""Publish stopped generation checkpoints without granting more inference."""
from copy import deepcopy

GENERATION_FAILURES = frozenset({'output_limit', 'missing_required_tool', 'malformed_response',
    'malformed_tool_output', 'incomplete_response', 'context_overflow', 'upstream_service', 'network', 'timeout'})


def incomplete(summary, failure):
    result = deepcopy(summary)
    result.update(checks_passed=False, accepted=False,
                  incomplete_reason='Generation stopped: ' + failure.get('category', 'generation_failure'),
                  generation_failure=deepcopy(failure))
    return result


def unchecked(job, reason):
    from coder_engine_ops import summarize
    summary = summarize([], job['revision_id'])
    if job.get('ui_contract_version'):
        from coder_ui_contract import apply_summary
        criteria = [{**deepcopy(c), 'revision_id': job['revision_id'], 'status': 'unverified',
                     'reason': reason, 'evidence': []} for c in job.get('ui_contract', {}).get('criteria', [])]
        apply_summary(summary, criteria)
    return incomplete(summary, job['recovery_failure'])


async def recover_candidate(job, controller, error):
    """Called after a failed operation has stopped and usage is accounted for."""
    store = controller.store
    failure = job.get('recovery_failure') or job.get('failure') or {}
    checkpoint = job.get('failed_checkpoint') or {}
    continuing = bool(job.get('recovery_failure'))
    if job.get('cancel_requested') or failure.get('category') not in GENERATION_FAILURES:
        return False
    if not continuing and (job['state'] != 'coding' or
            checkpoint.get('operation_id') != job.get('worker_operation') or
            checkpoint.get('revision_id') != job.get('revision_id') or
            not checkpoint.get('inventory', {}).get('files')):
        return False
    if continuing and job['state'] != 'checking':
        return False  # Never turn a failed publication into an automatic loop.
    from coder_engine_controller import step
    if not continuing:
        job = await store.save(job['id'], state='checking', recovery_failure=deepcopy(failure),
            event='generation_checkpoint_retained', checks=[], verification_summary=None,
            verified_outputs=[], acceptance=None, delivery_status='building',
            stop_limit='application_repairs' if job.get('repair_round', 0) >= 2 else '',
            resume_state='coding', blocker='Generation stopped; checking the retained checkpoint without further inference.')
        try:
            await step(job, controller)
            job = await store.get(job['id'])
        except InterruptedError:
            raise
        except Exception as check_error:
            error = check_error
            continuing = True
            job = await store.get(job['id'])
    if continuing:
        job = await store.save(job['id'], state='candidate_packaging', checks=[],
            verification_summary=unchecked(job, 'Checkpoint verification did not finish: ' + str(error)),
            verified_outputs=[], execution_profile={}, verification_version=3,
            blocker='Generation stopped; this checkpoint has not been fully checked. ' + str(error))
    try:
        await step(job, controller)
    except InterruptedError:
        raise
    except Exception as packaging_error:
        exhausted = 'execution allowance exhausted' in str(packaging_error).lower()
        await store.save(job['id'], state='blocked', resume_state='candidate_packaging',
            stop_limit='application_repairs' if job.get('repair_round',0)>=2 else 'job_seconds' if exhausted else job.get('stop_limit',''),
            failure=failure, blocker='Checkpoint ' + job['revision_id'] +
            ' is retained, but publication could not finish: ' + str(packaging_error))
    return True
