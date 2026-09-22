"""Policy 6 controller: one implementation, native checks, independent review.

Older policies stay in coder_jobs.run. All transitions use its existing durable
operations, ownership scope, cancellation and atomic delivery fences.
"""
from __future__ import annotations

from pathlib import Path
import re

from coder_review import verification_summary, scope_brief
from coder_verification import digest

POLICY_VERSION = 6
MAX_REPAIRS = 2
MAX_CHECK_CORRECTIONS = 2


def failure_key(check):
    evidence = '\n'.join(str(check.get(k, '')) for k in ('log_tail', 'error', 'summary'))
    for path in (check.get('audit_dir'), str(Path(check['log']).parent) if check.get('log') else None):
        if path:
            evidence = evidence.replace(path, '<verification>')
    evidence = re.sub(r'\bin \d+(?:\.\d+)?\s*(?:s|seconds?)\b', 'in <duration>', evidence)
    evidence = re.sub(r'127\.0\.0\.1:\d+', '127.0.0.1:<port>', evidence)
    return digest({'id':check['id'], 'command':check.get('command'), 'exit_code':check.get('exit_code'),
                   'failure_kind':check.get('failure_kind'), 'evidence':evidence})


def classify_checks(checks, baseline):
    known = {c['id']: c for c in baseline}
    result = []
    for check in checks:
        old = known.get(check['id'], {})
        same_failure = old and not old.get('passed') and failure_key(old) == failure_key(check)
        category = ('passed' if check.get('passed') else 'environment' if check.get('environment_fault') else
                    'unverified' if check.get('failure_kind') == 'missing_coverage' else
                    'existing_failure' if same_failure else 'regression' if old.get('passed') else 'application_defect')
        result.append({**check, 'classification': category})
    return result


async def candidate(job, store, reason):
    summary = verification_summary(job, job.get('review') or {})
    return await store.save(job['id'], state='candidate_packaging', event='review_candidate',
        verification_summary=summary, delivery_status='review_candidate', acceptance=None, blocker=reason,
        resume_state='checking')


async def step(job, controller):
    store, operate = controller.store, controller._operate
    identity, state = job['id'], job['state']
    if job.get('brief'):
        brief = scope_brief(job['brief'], job)
        if brief != job['brief']:
            job = await store.save(identity, brief=brief, event='brief_policy_normalized')
    common = {'brief': job.get('brief', {}), 'protected_files': job.get('protected_files', []),
              'execution_commands': job.get('execution_commands', {}), 'verification': job.get('verification', {}),
              'proposed_commands': job.get('proposed_commands', {}),
              'planning_fallback': job.get('planning_fallback', False),
              'profiles': job.get('verification', {}).get('profiles', [])}
    if state in {'queued', 'inspecting'}:
        job = await store.save(identity, state='inspecting')
        result, job = await operate(job, 'inspect', **common, pinned_revision=job.get('candidate_revision', ''))
        revision = result['snapshot']['revision']
        if job.get('candidate_revision') and revision != job['candidate_revision']:
            raise ValueError('Candidate source changed; its immutable revision must be restored before continuing')
        next_state = job.get('resume_after_inspect') or ('baselining' if result['inventory']['files'] and job['mode'] != 'ask_uploaded_project' else 'planning')
        if job.get('brief') and job.get('revision_id') and revision != job['revision_id']:
            next_state = 'checking'
        await store.save(identity, state=next_state, inventory=result['inventory'], workspace=result['workspace'],
            revision_id=revision, baseline_revision=job.get('baseline_revision') or revision,
            verification=result.get('verification', {}), resume_after_inspect='', candidate_revision='')
    elif state == 'baselining':
        checks = job.get('verification', {}).get('checks', [])
        result = {'checks': []}
        if checks:
            result, job = await operate(job, 'check', **common, checks=checks, baseline=True)
            await store.record_check(identity, job['worker_operation'], job['revision_id'], result)
        # Environment faults remain visible. They do not prevent implementation.
        await store.save(identity, state='coding' if job.get('brief') else 'planning', baseline_checks=result['checks'])
    elif state == 'planning':
        if job['mode'] == 'ask_uploaded_project':
            result, job = await operate(job, 'qa')
            await store.save(identity, state='completed', answer=result.get('answer', ''))
            return True
        result, job = await operate(job, 'plan', **common, evidence={'inventory': job.get('inventory', {}),
            'verification': job.get('verification', {}), 'baseline_checks': job.get('baseline_checks', [])})
        baseline_changed = result.get('verification') and result['verification'] != job.get('verification')
        await store.save(identity, state='baselining' if baseline_changed else 'coding', event='plan_returned', **result, repair_round=0,
            requirements=[], milestones=[], builder_round=0)
    elif state == 'coding':
        before = job['revision_id']
        result, job = await operate(job, 'code', **common, task=job['user_task'],
            ui_required=bool(job.get('verification', {}).get('web_packages')) or any('browser' in o['text'].lower() or 'web' in o['text'].lower() for o in job['brief']['outcomes']),
            milestone_id=f"round-{job.get('builder_round', 0)}", round_id=job.get('builder_round', 0),
            focused_recovery=job.get('focused_recovery', False),
            evidence={'brief': job['brief'], 'plan_text': job.get('plan_text', []),
                'revision_id': before, 'progress': job.get('progress_summary', ''),
                'failures': [c for c in job.get('checks', []) if not c.get('passed')],
                'diagnosis': job.get('review', {}).get('summary', '')})
        await store.save(identity, state='checking', revision_id=result['snapshot']['revision'],
            inventory=result['inventory'], verification=result.get('verification', {}), acceptance=None,
            visual_review={'status': 'pending'}, builder_finished=result.get('agent_finished', False),
            builder_note=result.get('incomplete_reason', ''), unchanged_source=before == result['snapshot']['revision'],
            progress_summary=result.get('summary') or result.get('incomplete_reason', ''), focused_recovery=False)
    elif state == 'checking':
        protected = [{'id':'protected:' + path, 'kind':'file', 'path':path, 'origin':'controller',
                      'assertions':[{'kind':'unchanged'}], 'evidence_types':['preservation'],
                      'outcomes':[o['id'] for o in job['brief']['outcomes'] if 'preservation' in o['evidence_types']]}
                     for path in job.get('protected_files', [])]
        result, job = await operate(job, 'check', **common, checks=[*job.get('verification', {}).get('checks', []), *protected])
        await store.record_check(identity, job['worker_operation'], job['revision_id'], result)
        checks = classify_checks(result['checks'], job.get('baseline_checks', []))
        await store.save(identity, state='auditing' if job.get('audit_plan') is not None else 'reviewing',
            project_checks=checks, checks=checks)
    elif state == 'reviewing':
        result, job = await operate(job, 'verify', **common, evidence={'brief': job['brief'],
            'original_request': job['user_task'], 'baseline_checks': job.get('baseline_checks', []),
            'checks': job.get('checks', []), 'verification': job.get('verification', {})})
        await store.save(identity, state='auditing', audit_plan=result['checks'], review_notes=result.get('notes', []),
                         brief=result.get('brief', job['brief']))
    elif state == 'auditing':
        # Setup/build are rerun in this fresh copy, preserving only dependency environments.
        setup = [c for c in job.get('verification', {}).get('checks', []) if c.get('phase') in {'setup', 'build'}]
        result, job = await operate(job, 'check', **common, checks=[*setup, *job.get('audit_plan', [])], audit=True,
                                   correction_round=job.get('check_corrections', 0))
        await store.record_check(identity, job['worker_operation'], job['revision_id'], result)
        audit_results = [c for c in result['checks'] if c.get('origin') == 'independent' or not c.get('passed')]
        await store.save(identity, state='visual_review' if job.get('visual_policy', {}).get('enabled') else 'accepting',
                         checks=[*job.get('project_checks', []), *audit_results])
    elif state == 'visual_review':
        result, job = await operate(job, 'visual', **common, ui_required=bool(job.get('verification', {}).get('web_packages')),
                                    evidence={'checks': job.get('checks', [])})
        await store.save(identity, state='accepting', visual_review=result)
    elif state == 'accepting':
        review, job = await operate(job, 'accept', **common, audit_plan=job.get('audit_plan', []),
            evidence={'brief': job['brief'], 'checks': job.get('checks', []),
                'baseline_checks': job.get('baseline_checks', []), 'audit_plan': job.get('audit_plan', []),
                'review_notes': job.get('review_notes', []), 'planning_fallback': job.get('planning_fallback', False)})
        summary = verification_summary(job, review)
        history = [*job.get('reviews', []), review]
        job = await store.save(identity, review=review, reviews=history, verification_summary=summary)
        if summary['accepted']:
            await store.save(identity, state='packaging', acceptance={'accepted': True, 'revision_id': job['revision_id'],
                'summary': review['summary'], 'coverage': summary['outcomes']})
        elif review['disposition'] == 'check_defect' and review.get('replacement_checks') and job.get('check_corrections', 0) < MAX_CHECK_CORRECTIONS:
            await store.save(identity, state='auditing', check_corrections=job.get('check_corrections', 0) + 1,
                check_history=[*job.get('check_history', []), {'revision_id': job['revision_id'], 'original': job['audit_plan'],
                    'replacement': review['replacement_checks'], 'diagnosis': review}], audit_plan=review['replacement_checks'])
        elif review['disposition'] == 'application_defect' and not any(c.get('environment_fault') or c.get('failure_kind') == 'source_mutation' for c in job.get('checks', [])):
            signature = digest({'failures': sorted(failure_key(c) for c in job.get('checks', []) if not c.get('passed')),
                                'outcomes': sorted(o['id'] for o in summary['outcomes'] if o['status'] != 'passed')})
            repeated = signature == job.get('failure_signature') or job.get('unchanged_source', False)
            if job.get('repair_round', 0) >= MAX_REPAIRS or repeated and job.get('recovery_used'):
                await candidate(job, store, 'Repair limit reached; review the saved candidate and execution evidence.')
            else:
                round_number = job.get('repair_round', 0) + 1
                await store.save(identity, state='coding', repair_round=round_number, builder_round=round_number,
                    failure_signature=signature, focused_recovery=repeated,
                    recovery_used=job.get('recovery_used', False) or repeated)
        else:
            await candidate(job, store, review.get('summary') or 'Some requested outcomes still require review.')
    elif state in {'packaging', 'candidate_packaging'}:
        is_candidate = state == 'candidate_packaging'
        result, job = await operate(job, 'package', candidate=is_candidate, verification_summary=job.get('verification_summary', {}),
                                    run_instructions=job.get('verification', {}).get('profiles', []))
        artifact = await controller._deliver(job, result, candidate=is_candidate)
        if controller._EVENTS:
            await controller._EVENTS.emit(job['conversation_id'], 'file_ready', {**artifact, 'workflow_id': identity})
        return True
    else:
        raise ValueError(f'Unknown policy-6 state: {state}')
    return False
