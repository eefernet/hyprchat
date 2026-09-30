"""Optional, conservative workflow presentation metadata."""
def presentation(job):
    state = job['state']
    phases = {'queued': 'Planning', 'inspecting': 'Planning', 'planning': 'Planning',
        'baselining': 'Checking', 'coding': 'Repairing' if job.get('repair_round') else 'Building',
        'checking': 'Checking', 'auditing': 'Checking', 'reviewing': 'Reviewing', 'accepting': 'Reviewing',
        'visual_review': 'Reviewing', 'packaging': 'Saving checkpoint', 'candidate_packaging': 'Saving checkpoint',
        'cancelling': 'Stopping', 'cancelled': 'Stopped'}
    phase = phases.get(state, 'Needs attention')
    if state == 'completed' and job.get('artifact') and job.get('artifact_status') == 'delivered':
        phase = 'Complete'
    elif state == 'completed' and job.get('answer'):
        phase = 'Answered'   # a project question has no artifact and is not a problem
    if state == 'ready_for_review':
        phase = 'Ready for review' if (job.get('candidate_artifact') or {}).get('metadata', {}).get('runnable') else 'Build incomplete'
    from coder_policy7_evidence import DURABLE_LIMITS
    # Only spent repair/correction rounds end a request; a spent model-call allowance is renewed by Continue.
    reason = job.get('blocker') if job.get('stop_limit') in DURABLE_LIMITS else ''
    if state == 'cancelled' and job.get('policy_version', 1) < 7:
        reason = 'This older workflow cannot resume after Stop. Start a new request from its saved project.'
    return {'phase': phase, 'project_name': job.get('project_name') or 'Daedalus project',
        'meaningful_update_at': job.get('meaningful_update_at') or job.get('updated_at'),
        'worker_contact_at': job.get('worker_contact_at'),
        'actions': {'resume': {'enabled': state in {'blocked', 'waiting_for_input', 'ready_for_review', 'cancelled'} and not reason,
                               'disabled_reason': reason or ''},
                    'cancel': {'enabled': state in phases and state not in {'cancelled', 'cancelling'},
                               'disabled_reason': 'Waiting for worker acknowledgement' if state == 'cancelling' else ''}}}
