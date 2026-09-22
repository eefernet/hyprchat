"""Policy 7 on the production controller's durable operations and publication."""
from __future__ import annotations

from pathlib import Path

from coder_policy7_evidence import acceptance, decide
from coder_policy7_ops import edit_transition


def operation_inputs(job):
    """Semantic inputs only: polling and accounting must not change identity."""
    fields = ('brief', 'batch', 'repair_round', 'audit_corrections', 'narrowed_targets', 'limit_retries', 'preflight_failures', 'builder', 'recovery_used', 'build_continuations',
              'repair_feedback', 'repair_targets', 'last_valid_audit', 'audit_dir', 'audit_checks',
              'audit_correction_pending', 'audit_feedback', 'execution_profile', 'execution_commands',
              'protected_files', 'baseline_revision', 'baseline_checks', 'checks',
              'inherited')
    return {key: job[key] for key in fields if key in job}


ECOSYSTEM_MANIFESTS = {'npm': ('package.json',), 'node': ('package.json',), 'npx': ('package.json',), 'vitest': ('package.json',),
    'jest': ('package.json',), 'python': ('requirements.txt', 'pyproject.toml'), 'python3': ('requirements.txt', 'pyproject.toml'),
    'pytest': ('requirements.txt', 'pyproject.toml'), 'pip': ('requirements.txt', 'pyproject.toml'), 'mvn': ('pom.xml',),
    'gradle': ('build.gradle', 'build.gradle.kts'), 'gradlew': ('build.gradle', 'build.gradle.kts'), 'cargo': ('Cargo.toml',),
    'go': ('go.mod',), 'cmake': ('CMakeLists.txt',), 'ctest': ('CMakeLists.txt',), 'make': ('Makefile', 'CMakeLists.txt'), 'dotnet': ()}


def repair_targets(job, defects):
    """Files a repair may edit, most implicated first: paths the failing log names, the failing
    command's own file arguments, traced bindings, then the owning ecosystem's manifest only."""
    import re, shlex
    known = set(job.get('last_patch', {}).get('source_hashes', {}))
    paths = []
    for defect in defects:
        package = defect.get('cwd', '.') or '.'
        prefix = '' if package == '.' else package.rstrip('/') + '/'
        log = defect.get('log_tail') or ''
        # Tracebacks and compiler errors name the file; execution copies live under .../project/.
        for match in re.findall(r'[\w./@+-]+\.[A-Za-z0-9]{1,6}', log):
            candidate = match.split('/project/', 1)[-1].lstrip('./')
            for name in (candidate, prefix + candidate):
                if name in known:
                    paths.append(name)
        try:
            words = shlex.split(defect.get('command') or '')
        except ValueError:
            words = []
        paths.extend(prefix + w for w in words if prefix + w in known)
        paths.extend(b['path'] for b in defect.get('test_files', []) + defect.get('source_bindings', []))
        if defect.get('id') == 'immutable-source':
            # The files a check rewrote, and the delivered tests that most likely did it.
            paths.extend(p for p in defect.get('paths', []) if p in known)
            paths.extend(b['path'] for c in job.get('checks', []) if c.get('is_test') and c.get('origin') == 'project'
                         for b in c.get('test_files', []))
            continue
        if str(defect.get('id', '')).startswith('execution-con'):
            paths.extend(name for name in ('.daedalus-run.json', '.daedalus.json') if name in known)
        runner = Path(words[0]).name.strip('"$') if words else ''
        manifests = list(ECOSYSTEM_MANIFESTS.get(runner, ()))
        if runner == 'dotnet':
            manifests = [name[len(prefix):] for name in known if name.startswith(prefix) and name.endswith(('.csproj', '.sln')) and '/' not in name[len(prefix):]]
        paths.extend(prefix + name for name in manifests if prefix + name in known)
        if prefix + '.daedalus.json' in known and ('DAEDALUS_AUDIT_PYTHON' in (defect.get('command') or '') or not words):
            paths.append(prefix + '.daedalus.json')
    # Missing deliverables have no bindings yet. Add only the planned files of that kind,
    # so one documentation gap does not queue every source file for a rewrite.
    planned = [p for batch in job['brief']['batches'] for p in batch.get('files', [])]
    is_test = lambda p: any('test' in part.lower() or 'spec' in part.lower() for part in p.split('/'))
    is_doc = lambda p: p.lower().endswith(('.md', '.rst', '.txt'))
    for defect in defects:
        kind = str(defect.get('id', '')).rsplit(':', 1)[-1] if defect.get('outcome') else ''
        if kind == 'documentation':
            paths.extend([p for p in planned if is_doc(p)] or ['README.md'])
        elif kind == 'tests':
            paths.extend(p for p in planned if is_test(p) or p.endswith('.daedalus.json'))
        elif kind == 'review':
            paths.extend(p for p in planned if not is_test(p))
    if not paths:
        paths.extend(planned)
    return list(dict.fromkeys(paths))


async def step(job, controller):
    store, identity = controller.store, job['id']
    state = job['state']
    async def operate(kind, **extra):
        return await controller._operate(job, kind, policy7_job=operation_inputs(job),
            project_id=job['project_id'], baseline_checks=job.get('baseline_checks', []), **extra)
    async def save(**changes):
        return await store.save(identity, **changes)
    async def candidate(reason, limit=''):
        summary = acceptance(job.get('brief', {}).get('outcomes', []), job.get('checks', []),
                             job.get('review') or {}, job['revision_id'])
        summary['accepted'] = False
        await save(state='candidate_packaging', verification_summary=summary, acceptance=None,
                   delivery_status='review_candidate', blocker=reason, stop_limit=limit, resume_state='checking')
    if state in {'queued', 'inspecting'}:
        await save(state='inspecting')
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
        # From-scratch projects use the agent-loop builder; uploaded repairs and edits keep Aider.
        builder = 'sdk' if job['mode'] == 'build_from_prompt' else 'aider'
        # A new brief starts at its first batch: a carried index can exceed the new plan's batches.
        await save(state='coding', **result, batch=0, narrowed_targets=[], narrowed_changed=False, limit_retries=0,
                   builder=builder, project_name=result['brief']['project_name'])
    elif state == 'coding':
        # The agent builder may not spend the calls the audit and review need (a live build used all 120).
        settings = controller.context_policy.runtime_settings() if job.get('builder') == 'sdk' else {}
        reserve = controller.verification_reserve(settings) if settings else 0
        produced = job.get('build_continuations') or (job.get('last_patch') or {}).get('changed')
        if reserve and produced and not job.get('repair_round') and settings['daedalus_model_calls'] - job.get('calls_used', 0) - reserve <= 0:
            # The builder's share ended exactly at a pass boundary. Dispatching another code operation
            # would raise and park an UNVERIFIED candidate; the reserve exists to check this checkpoint.
            await save(state='checking', preflight_pending=False, build_continuations=0, narrowed_targets=[])
            return False
        result, _ = await operate('code', reserve_calls=reserve)
        decision = edit_transition(job, result)
        next_state = decision.pop('state')
        revision = result['snapshot']['revision']
        changes = dict(revision_id=revision, inventory=result['inventory'], last_patch=result,
            unchanged_source=revision == job['revision_id'], acceptance=None, **decision)
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
        if job.get('preflight_pending'):
            # A broken setup/build is handed to the very next batch instead of riding along
            # unseen until the final repair rounds.
            failures = [{'id': c['id'], 'command': c.get('command'), 'cwd': c.get('cwd', '.'),
                         'log_tail': (c.get('log_tail') or '')[-900:]} for c in result['checks'] if not c.get('passed')]
            await save(state='coding', preflight_pending=False, preflight_failures=failures,
                       preflight_history=[*job.get('preflight_history', []), result])
        else:
            checks = result['checks']
            if job.get('audit_error'):
                checks = [*checks, {'id': 'audit-metadata', 'passed': False, 'origin': 'independent',
                                   'classification': 'audit_defect', 'log_tail': job['audit_error']}]
            await save(state='auditing' if job.get('audit_checks') else 'accepting' if job.get('audit_attempts') else 'reviewing',
                project_checks=result['checks'], checks=checks, execution_profile=result['execution_profile'],
                missing_deliverables=result.get('missing_deliverables', []),
                verification=result['execution_profile'])
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
        await save(state='visual_review' if job.get('visual_policy', {}).get('enabled') else 'accepting',
            checks=[*job.get('project_checks', []), *result['checks']])
    elif state == 'visual_review':
        result, _ = await operate('visual', evidence={'checks': job.get('checks', [])})
        await save(state='accepting', visual_review=result)
    elif state == 'accepting':
        verdict, _ = await operate('accept')
        summary = acceptance(job['brief']['outcomes'], job['checks'], verdict, job['revision_id'])
        job = await save(review=verdict, verification_summary=summary, reviews=[*job.get('reviews', []), verdict])
        if summary['accepted']:
            await save(state='packaging', acceptance={'accepted': True, 'revision_id': job['revision_id'],
                       'coverage': summary['outcomes']})
        else:
            decision = decide(job, summary, verdict)
            if decision['action'] == 'repair':
                await save(state='coding', repair_round=job.get('repair_round', 0) + 1, batch=0, editor_stopped='', recovery_used=False, build_continuations=0,
                    narrowed_changed=False, limit_retries=0,
                    repair_feedback=decision['feedback'], repair_targets=repair_targets(job, decision['feedback']),
                    failure_signature=decision['signature'], narrowed_targets=[])
            elif decision['action'] == 'audit':
                # The author must be told which outcomes are uncovered, not only which checks failed.
                feedback = [{k: f.get(k) for k in ('id', 'reason', 'log_tail') if f.get(k)} for f in decision.get('feedback', [])]
                await save(state='reviewing', audit_corrections=job.get('audit_corrections', 0) + 1,
                           audit_correction_pending=True, audit_feedback=feedback)
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
