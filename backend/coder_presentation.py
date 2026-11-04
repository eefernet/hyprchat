"""Optional, conservative workflow presentation metadata (the job card renders it verbatim)."""

NOTHING_TO_CONTINUE = 'Nothing failed, so there is nothing left to continue. Accept it, or ask for changes in the chat.'


def nothing_to_continue(job):
    """A policy-7 candidate whose checks all passed, or that built cleanly with nothing left to evidence: Continue
    would only re-run the same checks on the same revision. A spent allowance (any stop_limit) can still continue."""
    summary = job.get('verification_summary') or {}
    if job.get('policy_version', 1) < 7 or job.get('state') != 'ready_for_review' or job.get('stop_limit'):
        return False
    if summary.get('revision_id') and summary['revision_id'] != job.get('revision_id'):
        return False
    if summary.get('rolled_back') or summary.get('incomplete_reason'):
        return False
    unexercised = (summary.get('checks_ok') and summary.get('exercised') is False
                   and all(not row.get('missing_evidence') for row in summary.get('outcomes') or []))
    unverified_ui = summary.get('unverified_ui_criteria') and summary.get('checks_ok') and not summary.get('failures')
    return bool(summary.get('checks_passed') or unexercised or unverified_ui)


NOT_VERIFIED = ('Daedalus ran the build, the project\'s own tests and the commands named in the request. It did not '
                'verify behaviour those do not exercise, interface details beyond page load and the first form '
                'submit, performance or security.')


def presentation(job):
    state = job['state']
    from coder_check_outcomes import current, counts
    rows = current(job.get('checks', []), job.get('revision_id')) if job.get('engine') == 'openhands159' else []
    inconclusive = bool(rows) and not any(row['outcome'] == 'passed' for row in rows)
    phases = {'queued': 'Planning', 'inspecting': 'Planning', 'planning': 'Planning',
        'baselining': 'Checking', 'coding': 'Repairing' if job.get('repair_round') else 'Building',
        'checking': 'Checking', 'auditing': 'Checking', 'reviewing': 'Reviewing', 'accepting': 'Reviewing',
        'visual_review': 'Reviewing', 'packaging': 'Saving checkpoint', 'candidate_packaging': 'Saving checkpoint',
        'cancelling': 'Stopping', 'cancelled': 'Stopped'}
    phase = phases.get(state, 'Needs attention')
    summary = job.get('verification_summary') or {}
    detail = ''
    if state == 'completed' and job.get('artifact') and job.get('artifact_status') == 'delivered':
        phase = 'Complete'
        accepted_by = job.get('accepted_by') or (job.get('artifact') or {}).get('metadata', {}).get('accepted_by') or ''
        if accepted_by == 'user':
            detail = 'Accepted by you'
        elif accepted_by == 'examples' or (job.get('acceptance') or {}).get('kind') == 'examples':
            count = summary.get('confirmed_examples') or 0
            detail = f'Your {count} agreed check{"s" if count != 1 else ""} passed'
    elif state == 'completed' and job.get('answer'):
        phase = 'Answered'   # a project question has no artifact and is not a problem
    if state == 'ready_for_review':
        metadata = (job.get('candidate_artifact') or {}).get('metadata', {})
        contract_gap = any(c.get('status') != 'passed' for c in summary.get('criteria', [])) if job.get('ui_contract_version') else False
        if summary.get('incomplete_reason'):
            phase = 'Build incomplete'
            detail = 'Generation stopped; saved work is available for review'
        elif not contract_gap and not inconclusive and (summary.get('checks_passed') or metadata.get('checks_passed')):
            phase = 'Checks passed — please review'
        elif summary.get('checks_ok') and not summary.get('exercised'):
            phase = 'Built, not exercised'
        else:
            phase = 'Ready for review' if metadata.get('runnable') else 'Build incomplete'
    from coder_policy7_evidence import DURABLE_LIMITS
    # Only spent repair/correction rounds end a request; a spent model-call allowance is renewed by Continue.
    reason = job.get('blocker') if job.get('stop_limit') in DURABLE_LIMITS else ''
    if state == 'cancelled' and job.get('policy_version', 1) < 7:
        reason = 'This older workflow cannot resume after Stop. Start a new request from its saved project.'
    if nothing_to_continue(job):
        reason = NOTHING_TO_CONTINUE
    candidate = job.get('candidate_artifact') or {}
    revision = (candidate.get('metadata') or {}).get('revision_id') or ''
    # cw3-59371f48: a rollback onto the empty starting tree of a new build offered Accept on an archive that held
    # only the review report. The starting tree, or a revision whose summary recorded no check row, has nothing to accept.
    empty = bool(revision) and job.get('mode') == 'build_from_prompt' and (revision == job.get('baseline_revision')
                                or (summary.get('revision_id') == revision and 'checks' in summary and not summary['checks']))
    accept_ready = state == 'ready_for_review' and bool(candidate.get('id')) and bool(revision) and not empty
    return {'phase': phase, 'detail': detail, 'project_name': job.get('project_name') or 'Daedalus project',
        **({'check_counts': counts(rows)} if job.get('engine') == 'openhands159' else {}),
        'not_verified': (('See the requirement evidence for failed or unverified UI outcomes. Passing checks cover only the observed interactions; other behavior needs your review.'
                         if job.get('ui_contract_version') else NOT_VERIFIED)
                        if job.get('policy_version', 1) >= 7 and state in {'completed', 'ready_for_review'} and not job.get('answer') else ''),
        'meaningful_update_at': job.get('meaningful_update_at') or job.get('updated_at'),
        'worker_contact_at': job.get('worker_contact_at'),
        'actions': {'resume': {'enabled': state in {'blocked', 'waiting_for_input', 'ready_for_review', 'cancelled'} and not reason,
                               'disabled_reason': reason or ''},
                    'cancel': {'enabled': state in phases and state not in {'cancelled', 'cancelling'},
                               'disabled_reason': 'Waiting for worker acknowledgement' if state == 'cancelling' else ''},
                    'accept': {'enabled': accept_ready,
                               'revision_id': revision if accept_ready else '',
                               'disabled_reason': '' if accept_ready else 'Nothing to accept yet: this version has no verified project files'
                               if empty else 'Nothing to accept yet'}}}
