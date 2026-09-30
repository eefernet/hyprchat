"""Policy 7 on the production controller's durable operations and publication."""
from __future__ import annotations

from pathlib import Path

from coder_policy7_evidence import acceptance, decide, blocking
from coder_policy7_ops import edit_transition, retained_deliverables
from coder_repair_packet import packet, repair_targets, ECOSYSTEM_MANIFESTS  # noqa: F401


def operation_inputs(job):
    """Semantic inputs only: polling and accounting must not change identity."""
    fields = ('brief', 'batch', 'repair_round', 'audit_corrections', 'narrowed_targets', 'limit_retries', 'preflight_failures', 'preflight_retries', 'builder', 'recovery_used', 'build_continuations',
              'repair_feedback', 'repair_targets', 'last_valid_audit', 'audit_dir', 'audit_checks',
              'audit_correction_pending', 'audit_feedback', 'execution_profile', 'execution_commands',
              'protected_files', 'baseline_test_files', 'baseline_revision', 'baseline_checks', 'checks',
              'inherited', 'scope_clarifications', 'stage_allocation', 'repair_checkpoint', 'repair_checks', 'repair_deliverables', 'repair_documentation',
              'skeleton', 'repair_packet')
    return {key: job[key] for key in fields if key in job}


# repair_targets lives in coder_repair_packet (worker-shared); re-exported here for existing callers.


def regression_entries(regression):
    """The failed revision's rows for the regressed ids, carried FIRST into the next repair packet."""
    failed = str(regression.get('failed_revision') or '')[:12]
    rows = {c.get('id'): c for c in regression.get('checks', []) if isinstance(c, dict)}
    entries = []
    for ident in regression.get('regressed', []):
        ident = str(ident)
        if ident.startswith(('deliverable:', 'documentation:')):
            what = ident.split(':', 1)[1]
            reason = (f'Your previous repair (revision {failed}) removed the delivered '
                      + ('documentation for ' + what if ident.startswith('documentation:') else 'file ' + what)
                      + '; that revision is kept under refs/daedalus/failed-repairs and the checkpoint was restored. '
                      'Repair the items below WITHOUT removing it again.')
            row = {'id': ident, 'origin': 'controller', 'command': 'compare delivered files', 'cwd': '.'}
        else:
            reason = (f'Your previous repair (revision {failed}) broke this retained passing test; that revision is kept under '
                      "refs/daedalus/failed-repairs and the checkpoint was restored. Repair the items below WITHOUT changing this suite's result.")
            row = rows.get(ident) or {'id': ident, 'origin': 'project', 'command': ident, 'cwd': '.'}
        entries.append({**row, 'id': ident, 'passed': False, 'classification': 'application_defect', 'priority': True,
                        'reason': reason, 'log_tail': (row.get('log_tail') or '')[-1600:]})
    return entries



async def step(job, controller):
    store, identity = controller.store, job['id']
    state = job['state']
    async def operate(kind, **extra):
        return await controller._operate(job, kind, policy7_job=operation_inputs(job),
            project_id=job['project_id'], baseline_checks=job.get('baseline_checks', []),
            visual_required=job.get('visual_required', False), verification_version=3, **extra)
    async def save(**changes):
        return await store.save(identity, **changes)
    async def candidate(reason, limit=''):
        summary = acceptance(job.get('brief', {}).get('outcomes', []), job.get('checks', []),
                             job.get('review') or {}, job['revision_id'], request=job.get('user_task', job.get('task', '')),
                                 history=job.get('brief', {}).get('requirement_history', []))
        summary['accepted'] = False
        await save(state='candidate_packaging', verification_summary=summary, acceptance=None,
                   delivery_status='review_candidate', blocker=reason, stop_limit=limit, resume_state='checking')
    if job.get('verification_version') in {1, 2} and job.get('brief'):
        # A running writer must acknowledge cancellation before its checkpoint is
        # rechecked. Never dispatch a second operation by changing its identity.
        operation = None
        if job.get('worker_operation') and not job.get('operation_accounted'):
            url = controller.worker_url(job)
            operation = (await controller._worker_request(identity, 'GET', url, timeout=15)).json()
            if operation['status'] in {'queued', 'starting', 'running', 'cancelling'}:
                await controller._worker_request(identity, 'POST', url + '/cancel', timeout=15)
                import asyncio
                await asyncio.sleep(1)
                return False
        changes = {}
        if operation:
            snapshot = (operation.get('result') or {}).get('snapshot')
            if snapshot:
                await store.record_revision(identity, snapshot)
                changes['revision_id'] = snapshot['revision']
            elapsed = max(0, (operation.get('ended') or 0) - (operation.get('started') or operation.get('ended') or 0))
            changes.update(calls_used=job.get('calls_used', 0) + operation.get('calls', 0),
                           seconds_used=job.get('seconds_used', 0) + elapsed, operation_accounted=True)
        await save(state='checking', verification_version=3, acceptance=None, review=None,
            checks=[], project_checks=[], verification_summary=None, visual_review={'status': 'pending'},
            worker_operation='', operation_key='', **changes)
        return False
    if state in {'queued', 'inspecting'}:
        await save(state='inspecting', verification_version=3)
        result, _ = await operate('inspect', pinned_revision=job.get('candidate_revision', ''))
        revision = result['snapshot']['revision']
        if job.get('candidate_revision') and revision != job['candidate_revision']:
            raise ValueError('Candidate revision could not be restored')
        target = job.get('resume_after_inspect')
        if target == 'packaging' and (job.get('acceptance') or {}).get('revision_id') != revision:
            # Continue after a failed publication keeps its verdict, but only for the revision it judged.
            target = 'checking'
        await save(state=target or ('baselining' if result['inventory']['files'] and job['mode'] != 'ask_uploaded_project' else 'planning'),
            workspace=result['workspace'], inventory=result['inventory'], revision_id=revision,
            baseline_revision=job.get('baseline_revision') or revision, candidate_revision='', resume_after_inspect='')
    elif state == 'baselining':
        result, current = await operate('check', baseline=True)
        await store.record_check(identity, current['worker_operation'], job['revision_id'], result)
        await save(state='planning', baseline_checks=result['checks'], execution_profile=result['execution_profile'])
    elif state == 'planning':
        if job['mode'] == 'ask_uploaded_project':
            result, _ = await operate('qa')
            await save(state='completed', answer=result.get('answer', ''))
            return True
        result, _ = await operate('plan')
        if result.get('scope_question'):
            await save(state='waiting_for_input', resume_state='planning', scope_question=result['scope_question'],
                       blocker=result['scope_question'], plan_text=result.get('plan_text', []), scope_review_text=result.get('scope_review_text', []))
            return True
        # From-scratch projects use the agent-loop builder; uploaded repairs and edits keep Aider.
        builder = 'sdk' if job['mode'] == 'build_from_prompt' else 'aider'
        from coder_policy7_visual import required
        from coder_scope import effective_task
        from coder_skeletons import select_skeleton, describe
        # A verified starting structure for the stacks that fail from scratch; None keeps the general builder.
        chosen = select_skeleton(effective_task(job), result['brief'], job) if builder == 'sdk' and not job.get('skeleton') else None
        skeleton = describe(chosen, effective_task(job), result['brief'].get('project_name', '')) if chosen else job.get('skeleton')
        # A new brief starts at its first batch: a carried index can exceed the new plan's batches.
        await save(state='coding', **result, batch=0, narrowed_targets=[], narrowed_changed=False, limit_retries=0,
                   builder=builder, project_name=result['brief']['project_name'], scope_question='',
                   visual_required=required(effective_task(job)), skeleton=skeleton)
    elif state == 'coding':
        # The agent builder may not spend the calls the audit and review need (a live build used all 120).
        settings = controller.context_policy.runtime_settings()
        from context_policy import coding_allocation
        round_key = f"{job.get('allowance', 1)}:{job.get('repair_round', 0)}"
        allocation = job.get('stage_allocation') or {}
        if settings and allocation.get('round_key') != round_key:
            allocation = {**coding_allocation(settings['daedalus_model_calls'],
                settings['daedalus_model_calls'] - job.get('calls_used', 0)), 'round_key': round_key}
            job = await save(stage_allocation=allocation)
        reserve = allocation.get('verification_reserve', 0) if settings else 0
        produced = job.get('revision_id') and (job.get('build_continuations') or (job.get('last_patch') or {}).get('changed') or job.get('repair_round'))
        remaining = settings['daedalus_model_calls'] - job.get('calls_used', 0)
        if remaining < 2 and not produced:
            await candidate('Insufficient model calls remain to code and verify; continue with a new allowance.')
            return False
        if produced and (remaining < 2 or remaining - reserve <= 0):
            # The builder's share ended exactly at a pass boundary. Dispatching another code operation
            # would raise and park an UNVERIFIED candidate; the reserve exists to check this checkpoint.
            await save(state='checking', preflight_pending=False, build_continuations=0, narrowed_targets=[])
            return False
        result, _ = await operate('code', reserve_calls=reserve)
        decision = edit_transition(job, result)
        next_state = decision.pop('state')
        revision = result['snapshot']['revision']
        changes = dict(revision_id=revision, inventory=result['inventory'], last_patch=result,
            unchanged_source=revision == job['revision_id'], acceptance=None, visual_review={'status': 'pending'}, **decision)
        if result.get('skeleton_applied') and job.get('skeleton'):
            changes['skeleton'] = {**job['skeleton'], 'applied': True, 'applied_revision': revision}
        if next_state == 'candidate':
            # Partial edits may already satisfy the request. Check the immutable
            # checkpoint before deciding; this does not authorize another edit.
            changes.pop('stop_limit', None)
            await save(state='checking', **changes, editor_stopped=decision['reason'])
        else:
            # Commit the source revision and next stage together. A restart between
            # separate writes must never repeat a completed edit on its new source.
            await save(state='checking' if decision.get('preflight_pending') else 'coding' if next_state == 'editing' else next_state,
                       **changes)
    elif state == 'checking':
        result, current = await operate('check', preflight=job.get('preflight_pending', False))
        await store.record_check(identity, current['worker_operation'], job['revision_id'], result)
        if result.get('restored_snapshot'):
            restored = result['restored_snapshot']['revision']
            regression = result.get('repair_regression') or {}
            history = [*job.get('repair_regressions', []), regression]
            settings = controller.context_policy.runtime_settings() if getattr(controller, 'context_policy', None) else None
            remaining = settings['daedalus_model_calls'] - job.get('calls_used', 0) if settings else 0
            reserve = (job.get('stage_allocation') or {}).get('verification_reserve', 0)
            from coder_policy7_evidence import MAX_REPAIRS
            if settings and job.get('repair_round', 0) < MAX_REPAIRS and remaining >= 2 and remaining - reserve > 0:
                # sweep3-c Kanban (2026-09-26): parking here threw away the rounds that were left. The restored
                # checkpoint gets another repair whose packet names the regression FIRST; MAX_REPAIRS and the
                # verification reserve still bound it. Targets must reflect the RESTORED tree, not the failed one.
                feedback = [*regression_entries(regression), *job.get('repair_feedback', [])]
                last_patch = {**(job.get('last_patch') or {})}
                if result.get('source_hashes'):
                    last_patch['source_hashes'] = result['source_hashes']
                job = await save(revision_id=restored, checks=job.get('repair_checks', []), acceptance=None,
                                 last_patch=last_patch, unchanged_source=False)
                await save(state='coding', repair_round=job.get('repair_round', 0) + 1, batch=0, editor_stopped='', recovery_used=False,
                    build_continuations=0, narrowed_changed=False, limit_retries=0, narrowed_targets=[], blocker='',
                    repair_feedback=feedback, repair_targets=repair_targets(job, feedback),
                    repair_packet=packet(job, feedback, targets=lambda defect: repair_targets(job, [defect])),
                    repair_regressions=history)
                return False
            summary = acceptance(job['brief']['outcomes'], job.get('repair_checks', []), {}, restored,
                                 request=job.get('user_task', job.get('task', '')),
                                 history=job['brief'].get('requirement_history', []))
            await save(state='candidate_packaging', revision_id=restored, acceptance=None,
                checks=job.get('repair_checks', []), verification_summary=summary,
                delivery_status='review_candidate', blocker='Repair regressed a retained passing test. The earlier checkpoint was restored for review.',
                repair_regressions=history, resume_state='checking')
            return False
        from coder_policy7_phases import layer, progress
        # Foundation failures (setup, build, a service that does not start, a page that throws on load,
        # a requested command that cannot start) go back to the builder BEFORE any audit is authored.
        prerequisites = [c for c in result['checks'] if blocking(c) and layer(c) == 1]
        if prerequisites:
            # A finish event does not establish that delivered dependencies or
            # compilation work. Recover within this coding round, before audit.
            failures = [{k: c.get(k) for k in ('id', 'command', 'cwd', 'phase', 'classification', 'reason', 'request_excerpt', 'probe_input', 'exit_code')}
                        | {'log_tail': (c.get('log_tail') or '')[-1600:]} for c in prerequisites]
            job = await save(checks=result['checks'], project_checks=result['checks'],
                             preflight_pending=False, preflight_failures=failures,
                             execution_profile=result.get('execution_profile', {}),
                             preflight_history=[*job.get('preflight_history', []), result],
                             stage_progress=progress(result['checks']))
            settings = controller.context_policy.runtime_settings()
            remaining = settings['daedalus_model_calls'] - job.get('calls_used', 0)
            reserve = (job.get('stage_allocation') or {}).get('verification_reserve', 0)
            from coder_policy7_ops import MAX_BUILD_CONTINUATIONS
            if any(c.get('environment_fault') or c.get('classification') == 'environment' for c in prerequisites):
                await candidate('Project setup/build is blocked by the execution environment; audit was not started.')
            elif job.get('preflight_retries', 0) >= MAX_BUILD_CONTINUATIONS:
                await candidate('Setup/build still fails after bounded prerequisite recovery; audit was not started.', 'no_progress')
            elif remaining < 2 or remaining <= reserve:
                await candidate('The coding allowance ended while setup/build still fails; audit was not started.', 'model_calls')
            else:
                await save(state='coding', preflight_retries=job.get('preflight_retries', 0) + 1,
                           narrowed_targets=[], narrowed_changed=False, editor_stopped='')
            return False
        if job.get('preflight_pending'):
            # A broken setup/build is handed to the very next batch instead of riding along
            # unseen until the final repair rounds.
            failures = [{'id': c['id'], 'command': c.get('command'), 'cwd': c.get('cwd', '.'),
                         'log_tail': (c.get('log_tail') or '')[-900:]} for c in result['checks'] if blocking(c)]
            await save(state='coding', preflight_pending=False, preflight_failures=failures,
                       preflight_history=[*job.get('preflight_history', []), result])
        else:
            checks = result['checks']
            if job.get('audit_error'):
                checks = [*checks, {'id': 'audit-metadata', 'passed': False, 'origin': 'independent',
                                   'classification': 'audit_defect', 'log_tail': job['audit_error']}]
            await save(state='auditing' if job.get('audit_checks') else 'accepting' if job.get('audit_attempts') else 'reviewing',
                project_checks=result['checks'], checks=checks, preflight_failures=[], execution_profile=result['execution_profile'],
                stage_progress=progress(checks),
                ui_required=result.get('ui_required', bool(result['execution_profile'].get('web_packages'))),
                missing_deliverables=result.get('missing_deliverables', []),
                verification=result['execution_profile'], verification_version=3)
    elif state == 'reviewing':
        result, _ = await operate('verify')
        history = [*job.get('audit_history', []), {'revision_id': job['revision_id'], **result}]
        if result.get('audit_error'):
            await save(state='accepting', **result, audit_attempts=job.get('audit_attempts', 0) + 1, audit_history=history, checks=[*job.get('project_checks', []),
                {'id': 'audit-metadata', 'passed': False, 'origin': 'independent',
                 'classification': 'audit_defect', 'log_tail': result['audit_error']}])
        else:
            await save(state='auditing', **result, audit_attempts=job.get('audit_attempts', 0) + 1,
                       audit_history=history, audit_error='', audit_correction_pending=False)
    elif state == 'auditing':
        result, current = await operate('check', audit=True)
        await store.record_check(identity, current['worker_operation'], job['revision_id'], result)
        await save(state='visual_review' if job.get('visual_policy', {}).get('enabled') or job.get('visual_required') else 'accepting',
            ui_required=job.get('ui_required', False) or result.get('ui_required', False),
            checks=[*job.get('project_checks', []), *result['checks']])
    elif state == 'visual_review':
        result, _ = await operate('visual', evidence={'checks': job.get('checks', [])})
        checks = [*job.get('checks', []), *result.get('measurements', [])]
        if job.get('visual_required') and result.get('status') != 'passed' and result.get('status') != 'failed':
            await save(state='waiting_for_input', resume_state='visual_review', visual_review=result,
                       blocker='Visual verification was explicitly requested. Enable an installed vision model and continue: ' + result.get('reason', 'Visual evidence is unavailable'))
            return True
        await save(state='accepting', visual_review=result, checks=checks)
    elif state == 'accepting':
        if (job.get('visual_policy', {}).get('enabled') or job.get('visual_required')) and (job.get('visual_review') or {}).get('status', 'pending') == 'pending':
            await save(state='visual_review')
            return False
        verdict, _ = await operate('accept')
        summary = acceptance(job['brief']['outcomes'], job['checks'], verdict, job['revision_id'], request=job.get('user_task', job.get('task', '')), history=job['brief'].get('requirement_history', []))
        from coder_policy7_phases import progress
        job = await save(review=verdict, verification_summary=summary, reviews=[*job.get('reviews', []), verdict],
                         stage_progress=progress(job.get('checks', [])))
        if summary['accepted']:
            await save(state='packaging', acceptance={'accepted': True, 'revision_id': job['revision_id'],
                       'coverage': summary['outcomes']})
        else:
            decision = decide(job, summary, verdict)
            if decision['action'] == 'repair':
                await save(state='coding', repair_round=job.get('repair_round', 0) + 1, batch=0, editor_stopped='', recovery_used=False, build_continuations=0,
                    narrowed_changed=False, limit_retries=0,
                    repair_checkpoint=job['revision_id'], repair_checks=job['checks'], **retained_deliverables(job),
                    repair_feedback=decision['feedback'], repair_targets=repair_targets(job, decision['feedback']),
                    repair_packet=packet(job, decision['feedback'], targets=lambda defect: repair_targets(job, [defect])),
                    failure_signature=decision['signature'],
                    failed_result_history=[*job.get('failed_result_history', []), decision['signature']], narrowed_targets=[])
            elif decision['action'] == 'audit':
                # The author must be told which outcomes are uncovered, not only which checks failed.
                feedback = [{k: f.get(k) for k in ('id', 'reason', 'log_tail', 'audit_fixture_error', 'subprocess_diagnostics') if f.get(k)} for f in decision.get('feedback', [])]
                await save(state='reviewing', audit_corrections=job.get('audit_corrections', 0) + 1,
                           audit_correction_pending=True, audit_feedback=feedback,
                           no_progress_diagnosed=job.get('no_progress_diagnosed', False) or decision.get('no_progress_diagnosis', False))
            elif decision['action'] == 'execute':
                if job.get('executed_revision') == job['revision_id']:
                    # The same revision was already re-checked for this reason; another lap only buys another review.
                    await candidate('The delivered tests were already executed against this revision and still produced no test evidence.')
                else:
                    await save(state='checking', executed_revision=job['revision_id'])
            else:
                await candidate(decision['reason'], decision.get('limit', ''))
    elif state in {'packaging', 'candidate_packaging'}:
        is_candidate = state == 'candidate_packaging'
        result, current = await operate('package', candidate=is_candidate,
            verification_summary=job.get('verification_summary', {}), run_instructions=job.get('execution_profile', {}).get('profiles', []))
        artifact = await controller._deliver(current, result, candidate=is_candidate)
        if controller._EVENTS:
            await controller._EVENTS.emit(job['conversation_id'], 'file_ready', {**artifact, 'workflow_id': identity})
        return True
    else:
        raise ValueError('Unknown policy-7 state: ' + state)
    return False
