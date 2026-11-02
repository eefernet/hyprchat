"""Trusted operations for the modern engine; SDK completion is never acceptance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def prepare(store, operation_id):
    from coder_inference import require_local_model, ensure_context
    from context_policy import resolve, operation_settings
    import requests
    operation = store.get(operation_id)
    payload = operation['payload']
    details = require_local_model(payload['ollama_url'], payload['model'])
    response = requests.get(payload['ollama_url'].rstrip('/') + '/api/tags', timeout=15)
    response.raise_for_status()
    entry = next((m for m in response.json().get('models', []) if m['name'] in
                  {payload['model'], payload['model'] + ':latest'}), None)
    if not entry:
        raise ValueError('The selected model is not installed')
    if payload.get('model_digest') and payload['model_digest'] != entry['digest']:
        raise ValueError('Model digest changed; start a new job to use the replacement model')
    from coder_engine_tokens import validate_identity
    validate_identity(entry['digest'], details.get('template', ''))
    metadata = {'model': payload['model'], 'digest': entry['digest'],
                'template_sha256': hashlib.sha256(details.get('template', '').encode()).hexdigest(),
                'parameters': details.get('parameters', ''), 'capabilities': details.get('capabilities', []),
                'settings': payload['settings']}
    store.update(operation_id, payload={**payload, 'model_digest': entry['digest']})
    store.event(operation_id, 'model_identity', **metadata)
    policy=resolve('builder', operation_settings(payload))
    ensure_context(payload['ollama_url'], payload['model'], policy,
                   payload['settings'].get('daedalus_preparation_seconds', 600))
    response=requests.get(payload['ollama_url'].rstrip('/')+'/api/ps',timeout=15)
    response.raise_for_status()
    resident=next((m for m in response.json().get('models',[]) if m.get('name')==entry['name']),None)
    store.event(operation_id,'model_ready',model=entry['name'],digest=entry['digest'],
                residency=resident,context=policy.as_dict(),temperature=0.2,seed=None)


def execute(store, operation_id, repository):
    from coder_project_runtime import contract, run_revision
    from coder_policy7_ops import restore_protected
    from coder_required_tests import passing, regressions
    operation = store.get(operation_id)
    payload, kind = operation['payload'], operation['kind']
    if kind == 'code':
        from coder_sandbox_agent import run_isolated
        from coder_policy7_builder import sanitize_workspace
        result = run_isolated(store, operation_id, repository)
        sanitize_workspace(repository.root, repository.excludes)
        paths = list(dict.fromkeys(payload.get('protected_files', [])))
        restored = restore_protected(repository, payload.get('baseline_revision') or payload['revision_id'], paths)
        result.update(protected_restored=restored, inventory=repository.refresh(),
                      snapshot=repository.snapshot('Modern engine checkpoint', parent=payload['revision_id']),
                      model_digest=payload.get('model_digest', ''))
        if not result.get('agent_finished') and store.get(operation_id)['calls'] >= payload['calls_remaining']:
            store.update(operation_id, result=result)
            raise TimeoutError('Model-call allowance exhausted; the unverified checkpoint is retained')
        return result
    if kind != 'check':
        raise ValueError('Unsupported modern engine operation: ' + kind)
    observed = repository.snapshot('Verification source fence', parent=payload['revision_id'])
    if observed['revision'] != payload['revision_id']:
        raise ValueError('Source changed before verification')
    profile = contract(repository, payload.get('execution_commands'), managed_dependencies=True)
    result = run_revision(store, operation_id, repository, profile,
                          project_id=payload.get('project_id') or operation['job_id'])
    for check in result['checks']:
        # A guessed initial-page selector is diagnostic. Conditional controls
        # may only appear after an interaction; confirmed UI flows remain hard
        # checks and are still required to verify the interface.
        if (str(check.get('id', '')).startswith(('interface:labels:', 'workflow:'))
                and not check.get('environment_fault')):
            check['optional'] = True
    from coder_engine_contract import checks as deliverable_checks
    result['checks'].extend(deliverable_checks(result['workspace'],payload.get('task',''),payload['revision_id']))
    from coder_engine_delivery import products
    result['verified_outputs'] = products(result['workspace'], payload.get('task', ''), payload['revision_id'])
    from coder_ui_checks import interfaces, run as check_interactions
    required_interfaces = interfaces(repository, profile)
    if payload.get('ui_checks'):
        result['checks'].extend(check_interactions(store, operation_id, repository, payload['ui_checks']))
    ui_manifest = repository.root / '.daedalus-ui.json'
    if ui_manifest.is_file():
        try:
            if ui_manifest.stat().st_size > 65536: raise ValueError('Project UI checks exceed 64 KiB')
            flows = json.loads(ui_manifest.read_text())
            result['checks'].extend(check_interactions(store, operation_id, repository, flows, origin='project_interaction'))
        except (ValueError, TypeError) as error:
            result['checks'].append({'id': 'project-ui-manifest', 'passed': False, 'revision_id': payload['revision_id'],
                'classification': 'application_defect', 'reason': str(error)})
    result['execution_profile'] = profile
    result['required_tests'] = passing(result['checks'])
    if not payload.get('baseline'):
        lost = regressions(payload.get('required_tests', []), result['checks'], untested=result.get('untested'))
        if lost:
            result['checks'].append({'id': 'baseline-regressions', 'passed': False,
                'classification': 'application_defect', 'reason': 'Previously passing tests regressed',
                'lost_tests': lost, 'revision_id': payload['revision_id']})
    files = {p.get('path') for c in result['checks'] for p in c.get('test_files', []) if isinstance(p, dict) and p.get('path')}
    result['baseline_test_files'] = sorted(files)
    result['summary'] = summarize(result['checks'], payload['revision_id'], required_interfaces)
    changed_tests = []
    if not payload.get('baseline') and payload.get('baseline_revision'):
        from coder_repository import safe_relative
        for path in payload.get('baseline_test_files', []):
            current = safe_relative(repository.root, path)
            original = repository.git('show', payload['baseline_revision'] + ':' + path)
            if not current.is_file() or current.read_bytes() != original:
                changed_tests.append(path)
    if changed_tests:
        # Extending a feature can legitimately invalidate an old negative test.
        # Keep named-test regression checks, expose the changed expectations,
        # and require owner review instead of silently approving weaker tests.
        result['summary'].update(checks_passed=False, changed_baseline_tests=changed_tests)
    if payload.get('ui_contract_version') == 1:
        from coder_ui_contract import recipes, bindings, run as run_contract, apply_summary
        context = list(payload.get('ui_contract_context', []))
        for recipe in recipes(result['checks']):
            if recipe not in context:
                context.append(recipe)
        result['ui_contract_context'] = context
        if payload.get('baseline'):
            result['ui_contract_bindings'] = bindings(payload.get('ui_contract', {}), result['checks'])
        else:
            criteria = run_contract(store, operation_id, repository, payload.get('ui_contract', {}), context, payload.get('ui_contract_bindings'))
            apply_summary(result['summary'], criteria)
    return result


def summarize(checks, revision, required_interfaces=()):
    """Evidence is revision-bound and cannot treat an empty test run as exercise."""
    failed = [c for c in checks if not c.get('passed') and not c.get('optional')]
    exercised = any(c.get('passed') and c.get('revision_id') == revision and
        c.get('evidence_version') == 3 and c.get('coverage_observed') and
        c.get('execution_succeeded') and (c.get('source_bindings') or c.get('service_bindings'))
        for c in checks)
    same_revision = bool(checks) and all(c.get('revision_id') == revision for c in checks)
    exercised_interfaces = {interface for c in checks if c.get('passed') and c.get('execution_succeeded') and
        c.get('coverage_observed') and c.get('revision_id') == revision and c.get('origin') in {'confirmed_interaction', 'project_interaction'}
        for interface in c.get('test_interfaces', [])}
    missing_interfaces = sorted(set(required_interfaces) - exercised_interfaces)
    feedback = []
    for check in failed:
        row = {k: check.get(k) for k in ('id', 'command', 'reason', 'log_tail', 'classification', 'diagnostic')}
        if not row['reason'] and row['classification'] == 'missing_provenance':
            row['reason'] = ('Assertions ran, but verification did not observe them exercising application code or its running service. '
                             'Test the actual imported application or launch it and assert its behavior; duplicated logic and mocks alone do not verify the application. '
                             'For a web browser test, navigate to the controller-injected DAEDALUS_APP_URL '
                             '(static web) or DAEDALUS_<SERVICE_ID>_URL (declared service). Read that environment '
                             'variable before starting a fallback server, and start the fallback only when it is absent. '
                             'A test-owned server or hardcoded localhost URL is outside the observed endpoint, even '
                             'if its page and assertions work. For file:// interactions use file_path in .daedalus-ui.json. '
                             'Declare all test dependencies and a reproducible command.')
        elif not row['reason'] and row['classification'] == 'missing_coverage':
            row['reason'] = 'The command exited successfully, but no executed test assertions were observed. Run a supported test suite with real assertions against the application.'
        feedback.append(row)
    for interface in missing_interfaces:
        feedback.append({'id': 'unverified-interface:' + interface, 'classification': 'missing_interaction',
                         'reason': 'No controller-observed interaction test exercised the requested ' + interface +
                         ' application. Declare .daedalus-ui.json with a launch command or HTML file_path, '
                         'click/fill actions on real controls, and subsequent expected text/observe results.'})
    return {'revision_id': revision, 'accepted': False, 'checks_ok': same_revision and not failed,
            'checks_passed': same_revision and not failed and exercised and not missing_interfaces, 'exercised': exercised,
            'unverified_interfaces': missing_interfaces,
            'outcomes': [], 'failures': feedback, 'verification_version': 1}
