"""Policy-7 model operations. Executed only by the durable worker subprocess."""
from __future__ import annotations
import json
from pathlib import Path
import shutil

from coder_inference import local_chat
from coder_patch_runtime import edit, select_files, tree_hashes
from context_policy import resolve
from coder_scope import active_interfaces


def implementation_task(job):
    from coder_policy7 import EXECUTION_HELP
    batch = job['brief']['batches'][job.get('batch', 0)]
    targets = job.get('narrowed_targets') or (job.get('repair_targets') if job.get('repair_round') else None) or batch.get('files', [])
    task = batch['task'] if not job.get('repair_round') else 'Repair only these confirmed application defects and their regression tests.'
    if job.get('narrowed_targets'):
        targets = targets[:1]
        task += ('\nThis operation edits ONLY ' + targets[0] + '. Other components are reference for later operations. '
                 'Use the smallest SEARCH/REPLACE hunks that make the change; never re-emit unchanged code. '
                 'If this file already satisfies the request, reply exactly: NO CHANGE NEEDED.')
        if job.get('limit_retries'):
            task += ('\nYour previous reply for this file was cut off at the output limit. Its complete blocks were applied '
                     'and the file below already contains them. Make ONLY the remaining changes.')
    task += '\nFULL REQUEST (reference; restrict this edit to the current component):\n' + job['task']
    task += '\nCURRENT COMPONENT:\n' + ('Files implicated by the confirmed defects: ' + ', '.join(targets) if job.get('repair_round') else batch['task']) + '\n' + EXECUTION_HELP
    if job.get('repair_feedback'):
        # Compiler/test runners bury the decisive line under build-tool summaries; surface it first.
        from coder_policy7_builder import key_error_lines
        keys = key_error_lines(job['repair_feedback'])
        if keys:
            task += '\nKEY ERROR LINES (fix these exact problems):\n' + '\n'.join(keys)
        task += '\nCONFIRMED DEFECTS:\n' + json.dumps(job['repair_feedback'])
    failures = job.get('preflight_failures') or []
    if failures and not job.get('narrowed_targets'):
        from coder_repair_packet import repair_targets   # worker-shared; the controller module is backend-only
        targets = list(dict.fromkeys([*targets, *repair_targets(job, failures)]))
        task += ('\nTHE PROJECT SETUP/BUILD IS CURRENTLY FAILING after the previous step. Fix this first, with the smallest '
                 'edit to the owning manifest (for example never list Python standard-library modules such as sqlite3 in '
                 'requirements.txt), then do this step:\n' + json.dumps(failures))
    return task, targets


MAX_BUILD_CONTINUATIONS = 5
# In-operation static-fault nudges for the audit author: the second is spent only on mechanical slips
# (a syntax error, a rebuild, a missing assert). sweep3-c Node p2 spent BOTH audit corrections on one syntax error.
MAX_STATIC_NUDGES = 2
MECHANICAL_FAULT = ('syntax error', 'no assert statement', 'builds, tests or installs', 'writes a file into the project')


def request_clauses(task, limit=24):
    """Whole sentences of the request. Never split on 'and': "prints an error to stderr and exits with code 2" is one clause."""
    import re as _re
    clauses = [c.strip() for c in _re.split(r'[.;\n]+', task or '')]
    return [c for c in clauses if len(c) > 3][:limit]


def mechanical_fault(text):
    return any(marker in str(text) for marker in MECHANICAL_FAULT)
# Source a requested #id or --flag may live in, including server-side templates: a bare word in
# server.js used to pass EJS/Jinja projects by accident, so tightening the match needs these.
INTERFACE_SUFFIXES = {'.html', '.htm', '.js', '.jsx', '.ts', '.tsx', '.vue', '.svelte', '.py', '.mjs', '.cjs', '.css', '.java', '.kt',
    '.scala', '.cs', '.fs', '.vb', '.go', '.rs', '.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hh', '.rb', '.php', '.swift', '.sh',
    '.ejs', '.erb', '.hbs', '.handlebars', '.mustache', '.pug', '.jade', '.jinja', '.jinja2', '.j2', '.twig', '.cshtml', '.razor',
    '.astro', '.liquid', '.njk', '.scss', '.sass', '.less'}


def restore_protected(repository, baseline, paths):
    """Put files the request said to leave alone back to their baseline bytes after an edit.

    The preservation check still runs afterwards; restoring here keeps a stray edit from costing a
    repair round that an editor without git could not use anyway. Returns the restored paths."""
    from coder_repository import safe_relative
    restored = []
    for path in paths or []:
        try:
            original = repository.git('show', baseline + ':' + path)
            target = safe_relative(repository.root, path)
        except (RuntimeError, ValueError):
            continue  # absent at baseline: nothing to preserve, the check row reports it
        if not target.is_file() or target.read_bytes() != original:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original)
            restored.append(path)
    return restored


def edit_transition(job, result):
    """Splits consume the same logical repair round and survive Continue.

    A file-specific queue is always finished: a file that needs no change or
    cannot be patched is skipped, and the job stops only when a complete pass
    changed nothing.
    """
    narrowed = job.get('narrowed_targets', [])
    category, changed = result.get('category'), result.get('changed') or []
    batches = job['brief']['batches']
    if job.get('builder') == 'sdk':
        # The agent builder works on the whole brief: no batches or file queues. Its runs often end at a
        # partial checkpoint (one or two files). Checking a half-written project only burns repair rounds,
        # so it continues while it makes progress and has not finished, within a small bound.
        continued = job.get('build_continuations', 0)
        check = {'state': 'checking', 'narrowed_targets': [], 'batch': len(batches) - 1, 'preflight_pending': False,
                 'build_continuations': 0}
        if category == 'no_op' or changed and (result.get('agent_finished') or continued >= MAX_BUILD_CONTINUATIONS):
            return check
        if result.get('input_budget') and changed:
            # A saturated session overflowed AFTER real work (verify-kanban 2026-09-25: 14 calls, app.js written):
            # verify that checkpoint; the next round starts a fresh session with a bounded prompt.
            return check
        if result.get('input_budget'):
            # The request never reached the model. Once: retry in a fresh session (sdk_build keys it by
            # recovery_used). Twice: say so; "no source changes" hid this in every archived repair retry.
            if not job.get('recovery_used'):
                return {'state': 'editing', 'recovery_used': True}
            return {'state': 'candidate', 'stop_limit': 'input_budget',
                    'reason': 'The builder prompt for this round exceeds the configured input budget, so the model never ran. '
                              'Raise the Daedalus context window or lower the generation limit in Settings, then Continue.'}
        if result.get('allowance_spent') or result.get('stalled'):
            # More passes cannot help: verify what exists, or hand over the checkpoint when nothing was written.
            if changed or continued or job.get('repair_round'):
                return check
            return {'state': 'candidate', 'stop_limit': 'model_calls' if result.get('allowance_spent') else 'no_progress',
                    'reason': 'The model-call allowance for building was used before any source was written.' if result.get('allowance_spent')
                    else 'The builder only re-read files and wrote nothing, even after a specific correction.'}
        if changed:
            return {'state': 'editing', 'build_continuations': continued + 1, 'preflight_pending': True}
        if continued:
            return check  # earlier progress this round: let the checks judge the checkpoint
        feedback = job.get('repair_feedback') or []
        if job.get('repair_round') and feedback and all(d.get('origin') == 'independent' for d in feedback):
            # Only model-written audit checks fail and the builder, able to run the app, changed nothing: the
            # audit's expectation is the likelier fault. A second pass cost ~20 calls on a correct program;
            # the unchanged checkpoint sends decide() to an audit correction instead.
            return check
        if job.get('recovery_used'):
            return {'state': 'candidate', 'reason': 'The builder made no source changes after a focused retry.', 'stop_limit': 'no_progress'}
        return {'state': 'editing', 'recovery_used': True}

    def advance():
        next_batch = job.get('batch', 0) + 1
        finished = job.get('repair_round') or next_batch >= len(batches)
        return {'state': 'checking' if finished else 'editing', 'narrowed_targets': [], 'narrowed_changed': False,
                'limit_retries': 0, 'batch': min(next_batch, len(batches) - 1), 'preflight_pending': not finished}

    if narrowed:
        progressed = bool(changed) or bool(job.get('narrowed_changed'))
        # Complete blocks from a truncated reply were applied; ask for the remainder.
        if category == 'output_limit' and changed and job.get('limit_retries', 0) < 2:
            return {'state': 'editing', 'narrowed_targets': narrowed, 'narrowed_changed': True,
                    'limit_retries': job.get('limit_retries', 0) + 1}
        if len(narrowed) > 1:
            return {'state': 'editing', 'narrowed_targets': narrowed[1:], 'narrowed_changed': progressed, 'limit_retries': 0}
        if not progressed:
            return {'state': 'candidate', 'reason': 'The file-specific edits changed no source; the responses and checkpoint were saved.',
                    'stop_limit': 'no_progress'}
        return advance()
    if category == 'no_op' or changed and category not in {'output_limit', 'invalid_patch'}:
        return advance()
    # A later build batch may legitimately find its files already written. Inference
    # or editor faults are never skipped silently.
    if not changed and job.get('batch', 0) > 0 and not job.get('repair_round') and category not in {
            'output_limit', 'invalid_patch', 'environment'}:
        return advance()
    targets = (job.get('repair_targets') if job.get('repair_round') else None) or batches[job.get('batch', 0)].get('files', [])
    # Salvaged blocks are whole, so a truncated reply leaves its cut-off file unchanged
    # and queued. When every target changed, the checkpoint's checks decide what remains.
    remaining = [name for name in targets if name not in changed]
    if changed and not remaining:
        return advance()
    if remaining:
        return {'state': 'editing', 'narrowed_targets': remaining, 'narrowed_changed': bool(changed),
                'limit_retries': 0, 'recovery_used': True}
    if job.get('recovery_used'):
        return {'state': 'candidate', 'reason': 'The editor made no source changes after focused recovery.', 'stop_limit': 'no_progress'}
    return {'state': 'editing', 'recovery_used': True}


def execute_operation(store, operation_id, repository):
    """Use the worker's existing operation ledger, cancellation and checkpoint."""
    from coder_project_runtime import contract, run_revision
    from coder_policy7_evidence import acceptance
    operation = store.get(operation_id)
    payload, kind = operation['payload'], operation['kind']
    root = store.root / 'jobs' / operation['job_id']
    job = {**payload, **payload.get('policy7_job', {}), 'task': payload['original_task'],
           'settings': payload['settings']}
    job.setdefault('inherited', [])
    ops = Operations(store, operation_id, repository, root, job)
    if kind in {'plan', 'verify', 'accept'}:
        snapshot = repository.snapshot('Read-only operation', parent=payload['revision_id'])
        if snapshot['revision'] != payload['revision_id']:
            raise ValueError('Project source changed after verification')
        return {'plan': ops.plan, 'verify': ops.author, 'accept': ops.review}[kind](operation_id)
    if kind == 'code':
        if job.get('builder') == 'sdk':
            # New projects need a compile/test feedback loop while building; acceptance is unchanged.
            from coder_policy7_builder import sdk_build
            result = sdk_build(store, operation_id, repository, job, payload)
        else:
            task, targets = implementation_task(job)
            if job.get('baseline_test_files'):
                task += '\nPreserve existing user tests unchanged: ' + ', '.join(job['baseline_test_files']) + '. Add new tests in new files.'
            result = edit(store, operation_id, repository.root, task, preferred=targets)
        baseline_tests = job.get('baseline_test_files', [])
        restored_tests = []
        if baseline_tests:
            # Preserve the attempted edits before restoring tests the current request
            # did not authorize changing; old repair evidence is never rewritten.
            attempted = repository.snapshot('Editor checkpoint before baseline test protection', parent=payload['revision_id'])
            restored_tests = restore_protected(repository, job.get('baseline_revision') or payload['revision_id'], baseline_tests)
            if restored_tests:
                repository.git('update-ref', 'refs/daedalus/protected-test-edits/' + attempted['revision'], attempted['revision'])
                result['unauthorized_test_edits'] = {'paths': restored_tests, 'revision_id': attempted['revision']}
                store.event(operation_id, 'baseline_tests_restored', **result['unauthorized_test_edits'])
        restored = restore_protected(repository, job.get('baseline_revision') or payload['revision_id'], job.get('protected_files'))
        restored = list(dict.fromkeys([*restored, *restored_tests]))
        if restored:
            result['source_hashes'] = tree_hashes(repository.root, payload['settings']['daedalus_exclude_dirs'])
            store.event(operation_id, 'protected_restored', paths=restored)
            result['changed'] = [name for name in result.get('changed') or [] if name not in restored]
            result['protected_restored'] = restored
        result.update(snapshot=repository.snapshot('Coding checkpoint', parent=payload['revision_id']),
                      inventory=repository.refresh())
        return result
    if kind == 'check':
        try:
            profile = contract(repository, job.get('execution_commands'))
        except (ValueError, TypeError, KeyError) as error:
            return {'checks': [{'id': 'execution-contract', 'origin': 'project', 'passed': False,
                'classification': 'application_defect', 'log_tail': str(error) + ' (in .daedalus-run.json or .daedalus.json). '
                    'A command-line program or library needs NO .daedalus-run.json: delete it. That file declares long-running '
                    'HTTP services only, and each service command must contain the literal {port}.',
                'reason': 'The project execution configuration is invalid: ' + str(error),
                'revision_id': payload['revision_id'], 'execution_id': operation_id + ':contract'}], 'execution_profile': {}}
        if payload.get('preflight'):
            profile = {**profile, 'checks': [c for c in profile['checks'] if c.get('phase') in {'setup', 'build', 'lint', 'typecheck'}]}
        result = run_revision(store, operation_id, repository, profile, project_id=payload['project_id'],
            audit_source=Path(job['audit_dir']) if payload.get('audit') else None,
            checks=job.get('audit_checks') if payload.get('audit') else None)
        result['execution_profile'] = profile
        if not payload.get('audit') and not payload.get('baseline') and not payload.get('preflight'):
            regressed = repair_regressions(job, result['checks'], tree_hashes(repository.root, repository.excludes))
            regressed.extend(missing_repair_deliverables(job, repository))
            if regressed:
                result['repair_regression'] = {'failed_revision': payload['revision_id'], 'checks': result['checks'], 'regressed': regressed}
                result['restored_snapshot'] = restore_repair(repository, job['repair_checkpoint'], payload['revision_id'])
                # The controller may repair again from the restored tree; its targets must reflect THAT tree.
                result['source_hashes'] = tree_hashes(repository.root, repository.excludes)
                return result
        if not payload.get('baseline') and not payload.get('preflight'):
            import hashlib
            from coder_repository import file_hash, safe_relative
            for path in job.get('protected_files', []):
                try:
                    original = repository.git('show', job['baseline_revision'] + ':' + path)
                    current = safe_relative(repository.root, path)
                    expected = hashlib.sha256(original).hexdigest()
                    passed = current.is_file() and file_hash(current) == expected
                except (ValueError, OSError, RuntimeError):
                    # git raises RuntimeError for a path absent at baseline: that row fails, not the whole check.
                    expected, passed = '', False
                result['checks'].append({'id': 'protected:' + path, 'passed': passed, 'origin': 'controller',
                    'reason': '' if passed else 'The request says to leave ' + path + ' unchanged, but it differs from the original (or did '
                        'not exist then). Restore it exactly and make the requested change in other files.',
                    'classification': 'passed' if passed else 'application_defect', 'evidence_types': ['preservation'],
                    'outcomes': [o['id'] for o in job['brief']['outcomes'] if 'preservation' in o.get('evidence_types', [])],
                    'file_bindings': [{'path': path, 'sha256': expected}], 'revision_id': payload['revision_id'],
                    'execution_id': operation_id + ':protected:' + path})
            # Requested controls must exist in the delivered source, whatever the model's own tests say.
            import re as _re
            from coder_policy7_evidence import interface_files, requested_interfaces
            wanted = active_interfaces(job)
            if wanted['selectors'] or wanted['flags']:
                is_test = lambda name: any('test' in part.lower() or 'spec' in part.lower() for part in Path(name).parts)
                sources = {}
                for name in tree_hashes(repository.root, ('.git', '.venv', 'venv', 'node_modules', '__pycache__', 'dist', 'build')):
                    path = repository.root / name
                    if not is_test(name) and path.suffix.lower() in INTERFACE_SUFFIXES and path.stat().st_size < 2_000_000:
                        sources[name] = path.read_text(errors='replace')
                for kind, token in [*(('selector', v) for v in wanted['selectors']), *(('flag', v) for v in wanted['flags'])]:
                    found = interface_files(kind, token, sources)
                    label = ('#' + token) if kind == 'selector' else token
                    result['checks'].append({'id': 'interface:' + label, 'passed': bool(found), 'origin': 'controller',
                        'classification': 'passed' if found else 'unverified_interface', 'evidence_types': [],
                        'interface_status': 'declared_unverified' if found else 'unresolved',
                        'outcomes': [], 'revision_id': payload['revision_id'], 'execution_id': operation_id + ':interface:' + label,
                        'log_tail': '' if found else 'The request names ' + label + ' but no delivered source file defines or handles it. '
                            'Implement exactly that ' + ('element id' if kind == 'selector' else 'command-line flag') + '; do not substitute another control.',
                        'reason': '' if found else 'Requested interface ' + label + ' could not be resolved from source; runtime proof is required'})
            # Values a page script reads from a FormData by names the delivered HTML never assigns (the September
            # Kanban acceptance): a controller row on the immutable execution copy, never the editor's tree.
            from coder_frontend_guard import form_fields_row
            fields = form_fields_row(result['workspace'], payload['revision_id'], operation_id)
            if fields:
                result['checks'].append(fields)
            # Diagnostic probes from the literal request: the requested command must run, a .NET solution
            # must carry a real test project, a skeleton's placeholders must have been replaced. Never evidence.
            if not payload.get('audit'):
                from coder_request_probes import command_rows, solution_test_rows
                from coder_scope import effective_task
                result['checks'].extend(solution_test_rows(result['workspace'], payload['revision_id'], operation_id))
                result['checks'].extend(command_rows(store, operation_id, result['workspace'], profile, effective_task(job),
                                                     payload['revision_id'], project_id=payload['project_id']))
                if (job.get('skeleton') or {}).get('applied'):
                    from coder_skeletons import placeholder_row
                    placeholder = placeholder_row(result['workspace'], job['skeleton'], payload['revision_id'], operation_id)
                    if placeholder:
                        result['checks'].append(placeholder)
            # A user-owned test that failed before the edit and passes now, byte-identical,
            # demonstrates the repair without any model-authored audit.
            from coder_policy7_evidence import repair_demonstrated
            before = {b.get('id'): b for b in job.get('baseline_checks', [])}
            for row in result['checks']:
                old = before.get(row.get('id'))
                row['repair_demonstrated'] = repair_demonstrated(row, old)
                row['test_origin'] = ('baseline_user_unchanged' if old and row.get('test_files') and row['test_files'] == old.get('test_files')
                                      else 'current_builder' if row.get('origin') == 'project' else 'generated_audit')
            # Absent documentation is an application defect, never an audit fault.
            from coder_policy7 import documented
            packages = [p.get('cwd', '.') for p in profile.get('profiles', []) if isinstance(p, dict)]
            result['missing_deliverables'] = [o['id'] for o in job.get('brief', {}).get('outcomes', [])
                if 'documentation' in o.get('evidence_types', []) and not documented(repository.root, o.get('component', '.'), packages)]
        return result
    raise ValueError('Unsupported policy-7 operation: ' + kind)


class Operations:
    def __init__(self, store, operation_id, repository, job_root, job, *, chat=None, editor=None):
        self.store, self.operation_id, self.repo = store, operation_id, repository
        self.root, self.job, self.chat, self.editor = Path(job_root), job, chat or local_chat, editor or edit

    def author(self, op):
        from coder_policy7 import audit_checks, audit_commands, normalize_audit, static_audit_faults, mechanical_audit_rewrites
        from coder_policy7_evidence import needs_behavior_audit, requested_interfaces
        job = self.job
        directory = self.root / ('audit-' + op)
        directory.mkdir(exist_ok=True)
        previous = job.get('last_valid_audit')
        if previous and Path(previous).is_dir() and Path(previous) != directory:
            shutil.copytree(previous, directory, dirs_exist_ok=True)
        before, commands = tree_hashes(directory), audit_commands(directory)
        instruction = (
            'AUDIT TASK. The application has ALREADY been edited by someone else; you are NOT fixing or changing it. '
            'Your whole reply is two new files created with empty-SEARCH blocks: test_behavior.py and audit.json. '
            'Do not announce a plan or ask to examine files; the source is already supplied read-only. '
            'Use absolute paths under DAEDALUS_AUDIT_DIR for temporary fixtures; never derive project paths from __file__. '
            'Use subprocess argv lists with cwd set to DAEDALUS_PROJECT_ROOT. For Go exit-code checks use the built executable; go run wraps exit status. If no executable is available, report missing launch evidence rather than claiming its exit code. '
            'Generated browser audits must use the supplied DAEDALUS_APP_URL. Delivered project tests should also support a standalone local-launch fallback. '
            'Write independent executable tests in the writable audit directory. The actual project is READ ONLY. '
            'Explicit new-file targets are audit.json and test_behavior.py. Inspect the supplied source. '
            'Never copy/reimplement the app or put project modules in the audit. Python clients may test a Node HTTP API; '
            'never import JavaScript as Python. Import Python projects using python3. For HTTP/browser clients use '
            '\"$DAEDALUS_AUDIT_PYTHON\" which has requests and Playwright. Use os.environ[\"DAEDALUS_APP_URL\"] '
            'and os.environ[\"DAEDALUS_PROJECT_ROOT\"]; never guess a port or derive project paths from __file__. '
            'Do not create services; the controller starts them. Use plain assert statements against actual application results. '
            'Use Python Playwright for browser checks, never Selenium or a DOM script executed by Node. '
            'Launch the application with ITS OWN runtime, never sys.executable unless the application itself is Python '
            '(node <file>.js for Node, the built executable for Go). '
            'For a compiled program (Java, C, C++, C#, Go, Rust) the controller has ALREADY built it: launch the built artifact with '
            'subprocess.run from os.environ[\"DAEDALUS_PROJECT_ROOT\"] and assert on its real stdout/exit code, e.g. '
            '[\"java\",\"-cp\",\"target/classes\",\"<MainClass>\",...], [\"build/<exe>\",...], [\"dotnet\",\"<App>/bin/Debug/net8.0/<App>.dll\",...], '
            '[\"target/debug/<bin>\",...] or [\"./<built-go-program>\",...]. Never rebuild and never write inside the project. '
            'Include a __main__ entrypoint so python executes the assertions. Map each tested outcome ID in metadata; '
            'o1 is only an example, include every outcome actually covered by that check. '
            'Write audit.json metadata {\"checks\":[{\"command\":\"\\\"$DAEDALUS_AUDIT_PYTHON\\\" {audit}/test_behavior.py\",'
            '\"outcomes\":[\"o1\"],\"evidence_types\":[\"behavior\"],\"cwd\":\".\"}]}. '
            'Documentation uses {\"kind\":\"file\",\"path\":\"README.md\",\"outcomes\":[\"o2\"],'
            '\"evidence_types\":[\"documentation\"],\"assertions\":[{\"kind\":\"nonempty\"}]}. '
            'An independent audit cannot substitute for requested tests DELIVERED IN THE PROJECT. '
            'For every request clause that names an input class (invalid, malformed, missing, empty, unknown, boundary) write an '
            'executed assertion that feeds exactly that class of input to the requested invocation and asserts the stated exit code, '
            'stderr and stdout. '
            'Correct faulty checks while retaining valid checks and all requested outcomes.\n' + json.dumps({
                'original_request': job['task'], 'request_clauses': request_clauses(job['task']), 'outcomes_to_audit': [
                    {**row, 'evidence_types': [kind for kind in row['evidence_types'] if kind in {'behavior', 'documentation'}]}
                    for row in job['brief']['outcomes'] if set(row['evidence_types']) & {'behavior', 'documentation'}],
                'delivered_tests': 'Controller executes project tests separately; do not register them as audit commands.',
                'requested_interfaces_the_audit_must_use_exactly': active_interfaces(job),
                'correction_required': ([{**f, 'instruction': 'Add an executed assertion for this outcome and list its ID in audit.json outcomes'
                        if str(f.get('id', '')).endswith(':behavior') else 'Fix this audit check'} for f in job.get('audit_feedback', [])]
                    if job.get('audit_correction_pending') else []),
                'execution_profile': job.get('execution_profile'),
                'previous_failures': [{k: c.get(k) for k in ('id', 'classification', 'command', 'log_tail', 'audit_fixture_error', 'subprocess_diagnostics')}
                                      for c in job.get('checks', []) if not c.get('passed')]}))
        patch = self.editor(self.store, op, directory, instruction, read_root=self.repo.root,
                            preferred=['audit.json', 'test_behavior.py'])
        needs_behavior = needs_behavior_audit(job['brief']['outcomes'], job.get('checks', []))
        script = directory / 'test_behavior.py'
        if needs_behavior and not patch.get('changed') and not (script.is_file() and script.read_text().strip()):
            # A reply that only announces work is a stall, not an audit. One immediate
            # nudge inside this operation; it spends model calls, never a correction round.
            self.store.event(op, 'audit_author_stalled', category=patch.get('category')) if self.store else None
            patch = self.editor(self.store, op, directory, 'Your previous reply wrote no files. Reply now with ONLY the two '
                'SEARCH/REPLACE blocks (empty SEARCH) that create test_behavior.py and audit.json.\n' + instruction,
                read_root=self.repo.root, preferred=['audit.json', 'test_behavior.py'])
        mechanical_audit_rewrites(directory, self.repo.root)
        for nudge in range(MAX_STATIC_NUDGES):
            faults = static_audit_faults(directory, self.repo.root, self.job.get('task', '')) if script.is_file() and script.read_text().strip() else []
            if not faults or (nudge and not all(mechanical_fault(f) for f in faults)):
                break
            # Visible before execution: precise nudges inside this operation, never a correction round.
            if self.store:
                self.store.event(op, 'audit_static_faults', faults=faults, nudge=nudge + 1)
            patch = self.editor(self.store, op, directory, 'Fix these defects in the audit files you just wrote, keeping everything else:\n- ' +
                '\n- '.join(faults) + '\n' + instruction, read_root=self.repo.root, preferred=['audit.json', 'test_behavior.py'])
            mechanical_audit_rewrites(directory, self.repo.root)
        import re as _re
        labels = [v for v in (active_interfaces(job).get('labels') or []) if v]
        text = script.read_text(errors='replace') if script.is_file() else ''
        if labels and _re.search(r'DAEDALUS_APP_URL|playwright', text, _re.I) and not any(label in text for label in labels):
            # A browser audit that never touches the controls the request names by label cannot exercise them
            # (sweep3 medium: get_by_label("Project name") found nothing; no audit had looked). A nudge, not a fault.
            if self.store:
                self.store.event(op, 'audit_labels_unused', labels=labels)
            patch = self.editor(self.store, op, directory, 'Your browser audit never uses the accessible labels the request names (' +
                ', '.join(labels) + '). Locate those controls with page.get_by_label(<label>, exact=True) and the requested buttons with '
                'page.get_by_role("button", name=<text>, exact=True), keeping everything else:\n' + instruction,
                read_root=self.repo.root, preferred=['audit.json', 'test_behavior.py'])
        packages = [p.get('cwd', '.') for p in (job.get('execution_profile') or {}).get('profiles', []) if isinstance(p, dict)]
        try:
            notes = normalize_audit(directory, job['brief']['outcomes'], self.repo.root, packages=packages)
        except (ValueError, OSError) as error:
            notes = ['Audit normalization skipped: ' + str(error)]
        after = tree_hashes(directory)
        changed = any(before.get(p) != h for p, h in after.items() if p != 'audit.json') or commands != audit_commands(directory)
        try:
            mechanical_audit_rewrites(directory, self.repo.root)
            remaining = static_audit_faults(directory, self.repo.root, self.job.get('task', ''))
            if remaining:
                raise ValueError('Audit is not executable evidence: ' + ' | '.join(remaining))
            checks = audit_checks(directory, job['brief']['outcomes'], self.repo.root)
            if needs_behavior and not any(c.get('kind') != 'file' for c in checks):
                raise ValueError('Audit wrote no executable behavior test (test_behavior.py is missing or empty)')
            if needs_behavior:
                # A named control that no audit file touches has not been independently exercised.
                text = ''.join(p.read_text(errors='replace') for p in sorted(directory.rglob('*')) if p.is_file() and p.name != 'audit.json')
                wanted = active_interfaces(job)
                # Flags are verified in source by the controller; a browser/API audit need not invoke the CLI.
                unused = ['#' + v for v in wanted['selectors'] if v not in text]
                if unused:
                    raise ValueError('Audit never exercises requested interface(s): ' + ', '.join(unused) +
                                     '. Use exactly these names from the request; do not substitute other controls.')
            if job.get('audit_correction_pending') and not changed:
                # The attempt is spent, but valid retained checks are never thrown away.
                notes = [*notes, 'Audit correction changed no executable checks; the last valid audit was kept']
            return {'audit_dir': str(directory), 'last_valid_audit': str(directory), 'audit_checks': checks, 'patch': patch,
                    'audit_notes': notes}
        except (ValueError, OSError, SyntaxError) as error:
            return {'audit_dir': str(directory), 'audit_checks': [], 'audit_error': str(error), 'patch': patch, 'audit_notes': notes}

    def source_context(self, policy):
        files, omitted = select_files(self.repo.root, self.job['task'], policy.input_budget // 3,
                                      self.job['settings']['daedalus_exclude_dirs'])
        return {'files': {name: (self.repo.root / name).read_text() for name in files}, 'omitted': omitted}

    def plan(self, op):
        from coder_policy7 import make_brief, json_object
        from coder_scope import ScopeClarification, parents, protected_conflict, review_scope, plan_schema
        from coder_policy7_evidence import protected_paths
        clarification = '\n'.join(n['text'] for n in self.job.get('scope_clarifications', []))
        reaffirmed = protected_paths(clarification, self.job.get('protected_files', []))
        conflict = protected_conflict(self.job['task'], [p for p in self.job.get('protected_files', []) if p not in reaffirmed])
        if conflict:
            return {'scope_question': conflict}
        instruction = ('Create a compact work brief. Return JSON {"outcomes":[{"text":"requested result","component":".","evidence_types":["behavior"]}],'
            '"batches":[{"task":"focused implementation task","files":["suggested path"]}]}. '
            'Size the plan to the task. A command-line tool, library, small web page or edit is ONE batch of at most five files. '
            'Only a genuinely multi-component application (for example a separate backend and frontend) gets up to four dependency-ordered batches. '
            'Never plan more files than the request needs: no duplicate sources, sample data, scratch or report files. '
            'Give explicit file targets, including new files, for each batch. List EVERY file needed to install, build and run, '
            'using ONLY the manifests of the project\'s own ecosystem (Python: requirements.txt/pyproject.toml; Node/React: package.json, and '
            'index.html + vite.config.js for a Vite frontend; Java: pom.xml; C/C++: CMakeLists.txt; C#: .sln + .csproj files; Go: go.mod; Rust: Cargo.toml). '
            'NEVER add another ecosystem\'s manifest (no package.json in a C++, C#, Go, Rust, Java or Python project). '
            'A command-line program needs NO .daedalus-run.json: that file declares long-running web services only. Evidence types are nonexclusive: behavior, documentation, tests, preservation. '
            'Include project_name with a short plain-language name for the application. '
            'A README and executable test requirement needs documentation AND tests. component is its actual package path or dot for the whole app. '
            'Include all requested behavior, tests and documentation. Browser regression tests should be Python Playwright files with a .daedalus.json test command; do not run DOM tests with Node. '
            'Do not write code or executable probes.\n')
        context = self.source_context(resolve('architect', self.job['settings']))
        schema = None
        if self.job['inherited']:
            schema = plan_schema(self.job['task'], self.job['inherited'])
            context['parent_outcomes'] = parents(self.job['inherited'])
            instruction += ('For this follow-up, include inheritance as an object keyed by the supplied parent IDs: '
                '{"o1":{"action":"retain","request_quote":"","replacement_outcomes":[],"question":""}}. '
                'Give exactly one entry per parent outcome. Actions: retain unrelated requirements, replace explicitly changed ones, '
                'remove explicitly removed ones, or clarify an ambiguous conflict. request_quote must be an exact phrase from the '
                'CURRENT request for replace/remove. replacement_outcomes are one-based indexes into your NEW outcomes list. '
                'Put only NEW or REPLACEMENT outcomes in outcomes: retained outcomes are added by the controller. '
                'A replacement must preserve unaffected parts of the old requirement. Do not retain a requirement the user explicitly replaces. '
                'If the desired replacement is unclear, use clarify and ask one specific question.\n')
        retained, scope_text = [], []
        for attempt in range(2):
            content = instruction + self.job['task'] + '\n' + json.dumps(context)
            if self.job['inherited']:
                content = (instruction + json.dumps(context) + '\nCURRENT USER REQUEST (authoritative; latest clarification takes precedence):\n' +
                    self.job['task'] + '\nSource files and parent outcomes describe earlier behavior, not the current request. '
                    'Never quote earlier requirements as user authorization for a change.')
            try:
                raw = self.chat(self.store, op, 'architect', [{'role': 'user', 'content': content}], **({'schema': schema} if schema else {}))
            except ValueError as error:
                if schema and 'rejected response format' in str(error).lower():
                    schema = None
                    retained.append(str(error))
                    continue
                raise
            retained.append(raw)
            try:
                brief = make_brief(json_object(raw), self.job['task'], self.job['inherited'])
                if brief['requirement_history']:
                    try:
                        brief['scope_review'] = review_scope(brief, self.job['task'],
                            lambda prompt, schema: self.chat(self.store, op, 'reviewer', [{'role': 'user', 'content': prompt}], schema=schema), scope_text)
                    except ScopeClarification as error:
                        if attempt == 0:
                            instruction += ('\nThe scope review found a problem in the proposed reconciliation: ' + str(error) +
                                '. Correct the plan using the current request and latest clarification. Preserve unrelated clauses; '
                                'do not retain old behavior the request explicitly replaces. If the user intent is still ambiguous, '
                                'use action clarify and ask a specific question.')
                            continue
                        raise
                protected = self.protected_files()
                if not (protected or self.job.get('protected')):
                    for requirement in brief['outcomes']:
                        requirement['evidence_types'] = [k for k in requirement['evidence_types'] if k != 'preservation'] or ['behavior']
                return {'brief': brief, 'plan_text': retained, 'scope_review_text': scope_text,
                        'protected_files': protected, 'grounded_protected_files': self.grounded, 'baseline_test_files': baseline_test_files(self.job)}
            except ScopeClarification as error:
                return {'scope_question': str(error), 'plan_text': retained, 'scope_review_text': scope_text}
            except (ValueError, TypeError) as error:
                instruction += '\nCorrect this brief format: ' + str(error)
        if self.job['inherited']:
            return {'scope_question': 'The follow-up could not be reconciled with the earlier requirements. Clarify which behavior should change and which should remain.',
                    'plan_text': retained, 'scope_review_text': scope_text}
        return {'brief': make_brief({'outcomes': [self.job['task']], 'batches': [{'task': self.job['task']}]},
                                   self.job['task'], self.job['inherited']), 'plan_text': retained, 'planning_fallback': True,
                'protected_files': self.protected_files(), 'grounded_protected_files': self.grounded, 'baseline_test_files': baseline_test_files(self.job)}

    def protected_files(self):
        """API-supplied files plus those the request text says to leave alone (existing at baseline only)."""
        from coder_policy7_evidence import protected_paths
        baseline = self.job.get('baseline_revision') or self.job.get('revision_id') or ''
        try:
            existing = self.repo.git('ls-tree', '-r', '--name-only', '-z', baseline).decode('utf-8', 'replace').split('\0') if baseline else []
        except RuntimeError:
            existing = []
        # Kept apart from the API-supplied list: "do not modify README.md" binds this request, not the next one.
        self.grounded = protected_paths(self.job['task'], [p for p in existing if p])
        return list(dict.fromkeys([*self.job.get('protected_files', []), *self.grounded]))

    def review(self, op):
        from coder_policy7 import json_object
        from coder_policy7_evidence import acceptance, normalize_verdict, blocking
        policy = resolve('reviewer', self.job['settings'])
        outcomes = self.job['brief']['outcomes']
        checks = [{k: c.get(k) for k in ('id', 'passed', 'classification', 'command', 'log_tail', 'outcomes',
            'source_bindings', 'service_bindings', 'assertions_executed', 'assertion_failures', 'audit_fixture_error', 'subprocess_diagnostics', 'evidence_types', 'test_files', 'test_origin')} for c in self.job['checks'] if c.get('passed') or blocking(c)]
        for check in checks:
            check['log_tail'] = (check.get('log_tail') or '')[-1600:] if not check['passed'] else ''
        audit_root = Path(self.job.get('audit_dir', self.root / 'missing-audit'))
        audit_root.mkdir(exist_ok=True)
        audit_files, omitted = select_files(audit_root, self.job['task'], policy.input_budget // 6)
        prompt = ('Independently assess the ORIGINAL request and actual revision. Diagnose each failed check separately. '
            'Audit import, path, syntax or launch failures are audit/environment issues, not application defects. '
            'Return JSON {"scope_complete":true|false,"outcomes":[{"id":"o1","status":"passed|failed|unverified",'
            '"reason":"evidence"}],"diagnoses":[{"check_id":"id","kind":"application_defect|audit_defect|environment|unresolved",'
            '"reason":"request and executable evidence","outcomes":["o1"]}]}. Give exactly one status for EVERY outcome ID. '
            'Judge ONLY checks: they ran against this revision. before_this_work lists how checks behaved BEFORE the '
            'edit and is history, never evidence against the revision; a check listed there as failing that passes in '
            'checks has been FIXED. Baseline failures that still fail may be repaired only when they are explicitly '
            'in the current request; cite their outcome IDs in diagnoses[].outcomes. Otherwise leave them as existing failures. '
            'A passing check is insufficient if it copies application logic or checks another component. '
            'advisory_checks are informational lint/typecheck or optional visual skips. Never diagnose them as blocking defects.\n')
        gaps = acceptance(outcomes, self.job['checks'], {}, self.job['revision_id'])['outcomes']
        current = {c['id']: c.get('passed') for c in self.job['checks']}
        baseline = self.job.get('baseline_checks', self.job.get('baseline', []))
        data = {'original_request': self.job['task'], 'outcomes': outcomes, 'checks': checks,
                'requirement_history': self.job['brief'].get('requirement_history', []),
                'advisory_checks': [{k: c.get(k) for k in ('id', 'phase', 'log_tail')} for c in self.job['checks']
                                    if not c.get('passed') and not blocking(c)],
                'source': self.source_context(policy),
                'audit_source': {p: (audit_root / p).read_text() for p in audit_files}, 'omitted_audit_files': omitted,
                'evidence_gaps': gaps,
                'before_this_work': [{'id': c.get('id'), 'command': c.get('command'), 'failed_before': True,
                                      'now': 'FIXED: passes in checks' if current.get(c.get('id')) else 'still failing or not run'}
                                     for c in baseline if not c.get('passed')]}
        diff = self.repo.git('diff', '--no-ext-diff', self.job['baseline_revision'], self.job['revision_id']).decode(errors='replace')
        limit = policy.input_budget // 6
        data['source_diff'] = {'text': diff[:limit], 'truncated': len(diff) > limit}
        schema = {'type': 'object', 'required': ['scope_complete', 'outcomes', 'diagnoses'], 'properties': {
            'scope_complete': {'type': 'boolean'},
            'outcomes': {'type': 'array', 'items': {'type': 'object', 'required': ['id', 'status', 'reason'], 'properties': {
                'id': {'type': 'string', 'enum': [o['id'] for o in outcomes]},
                'status': {'type': 'string', 'enum': ['passed', 'failed', 'unverified']}, 'reason': {'type': 'string'}}}},
            'diagnoses': {'type': 'array', 'items': {'type': 'object', 'required': ['check_id', 'kind', 'reason'], 'properties': {
                'check_id': {'type': 'string'}, 'reason': {'type': 'string'},
                'kind': {'type': 'string', 'enum': ['application_defect', 'audit_defect', 'environment', 'unresolved']},
                'outcomes': {'type': 'array', 'items': {'type': 'string'}}}}}}}
        from coder_inference import InputBudgetError
        try:
            base = pack_review(prompt, data, policy.input_budget)
        except InputBudgetError as error:
            return {'scope_complete': False, 'outcomes': [{'id': o['id'], 'status': 'unverified', 'reason': str(error)} for o in outcomes],
                    'diagnoses': [], 'review_error': {'category': 'input_budget', 'message': str(error)}}
        correction, retained, verdict, challenged = '', [], None, False
        # Same bounded format recovery as planning; a contradiction with executed
        # evidence earns one grounded re-ask, never an automatic pass.
        for attempt in range(4):
            try:
                raw = self.chat(self.store, op, 'reviewer', [{'role': 'user', 'content': base + correction}], schema=schema)
                retained.append(raw)
                verdict = normalize_verdict(json_object(raw), outcomes)
            except (ValueError, TypeError) as error:
                if getattr(error, 'failure_category', '') == 'input_budget':
                    return {'scope_complete': False, 'outcomes': [{'id': o['id'], 'status': 'unverified', 'reason': str(error)} for o in outcomes],
                            'diagnoses': [], 'review_error': {'category': 'input_budget', 'message': str(error)}}
                if getattr(error, 'failure_category', '') == 'output_limit':
                    correction = '\nYour previous review exceeded the output limit. Keep every reason under 25 words.'
                else:
                    correction = '\nCorrect this review format: ' + str(error)[:300]
                if 'rejected response format' in str(error):
                    schema = None
                verdict = None
                continue
            failing = {name for c in self.job['checks'] if blocking(c) for name in c.get('outcomes') or []}
            unscoped = any(blocking(c) and not c.get('outcomes') for c in self.job['checks'])
            disputed = [row['id'] for row, gap in zip(verdict['outcomes'], gaps) if row['status'] != 'passed'
                        and not gap['missing_evidence'] and row['id'] not in failing]
            if disputed and not unscoped and not challenged:
                challenged = True
                correction = ('\nRe-examine outcomes ' + ', '.join(disputed) + '. Every required check for them PASSED against '
                    'this revision with executed assertions, and no current check for them fails. Failures listed under '
                    'before_this_work happened before the edit. Keep status failed only for a defect you can cite in the '
                    'current source or audit_source; otherwise mark them passed.')
                continue
            break
        if verdict is None:
            raise ValueError('Reviewer returned no valid verdict after format correction')
        return {**verdict, 'review_text': retained}


def pack_review(prompt, data, budget):
    """Fit the actual serialized input, retaining request/outcomes/failing checks."""
    from context_policy import estimate_tokens
    from coder_inference import InputBudgetError
    import copy
    data = copy.deepcopy(data)
    def fits():
        text = prompt + json.dumps(data)
        return estimate_tokens({'messages': [{'role': 'user', 'content': text}]}) <= max(0, budget - 512)
    # Lowest-priority context goes first; corrective text has a reserved allowance.
    for key in ('source_diff', 'source', 'audit_source', 'advisory_checks', 'before_this_work'):
        if fits(): break
        data.pop(key, None)
    if not fits():
        data['checks'] = [c for c in data['checks'] if not c.get('passed')]
        for c in data['checks']:
            c['log_tail'] = (c.get('log_tail') or '')[-600:]
    if not fits():
        raise InputBudgetError('Authoritative review requirements and failures exceed the configured input budget')
    return prompt + json.dumps(data)


def placeholder_test_hashes(job):
    """sha256 of the skeleton's replace-marked files as written: a placeholder suite the model replaced is progress."""
    import hashlib
    spec = job.get('skeleton') or {}
    if not spec.get('id'):
        return set()
    try:
        from coder_skeletons import files_for
        rendered = files_for(spec['id'], spec.get('names') or {})
    except (KeyError, ImportError):
        return set()
    return {hashlib.sha256(rendered[path].encode()).hexdigest() for path in spec.get('replace', []) if path in rendered}


def retained_suite_regression(old, current, placeholder_hashes=(), source_hashes=None):
    """Whether a retained passing project suite regressed: the SAME suite must now fail or run fewer tests.

    sweep3-c Kanban p1, q35coder, verify3 p2 and the medium runs (2026-09-26): the model replaced the skeleton's
    placeholder test with real CRUD tests that failed; matching rows by id alone called that a regression, restored
    the checkpoint (discarding a revision where the form and persistence already worked) and parked the job. A
    rewritten suite is a failing-test defect for decide(), never a regression; a deleted or renamed suite is a
    missing-tests defect. This mirrors repair_demonstrated's same-suite rule.
    """
    def fewer(now):
        return (old.get('test_count') is not None and now.get('test_count') is not None
                and now['test_count'] < old['test_count'])
    files = {(f['path'], f['sha256']) for f in old.get('test_files', []) if isinstance(f, dict) and f.get('sha256')}
    if files and all(digest in placeholder_hashes for _, digest in files):
        return False
    if not files:
        # Compiled runners trace no test files: the id rule stands.
        return current is None or not current.get('passed') or fewer(current)
    if current is None:
        # Deselected: every file of the suite is still present and unchanged, yet its row is gone.
        return source_hashes is None or all(source_hashes.get(path) == digest for path, digest in files)
    now = {(f['path'], f['sha256']) for f in current.get('test_files', []) if isinstance(f, dict) and f.get('sha256')}
    if now != files:
        return False
    return not current.get('passed') or fewer(current)


def repair_regressions(job, current, source_hashes=None):
    """Retained project suites must pass at the project-check stage.

    Generated audits and controller probes run in later stages. Their absence
    here means they are pending, not that the repair removed or broke them.
    """
    if not job.get('repair_checkpoint'):
        return []
    checks = {c['id']: c for c in current}
    placeholders = placeholder_test_hashes(job)
    return [old['id'] for old in job.get('repair_checks', []) if old.get('passed') and old.get('is_test')
            and old.get('origin', 'project') == 'project'
            and retained_suite_regression(old, checks.get(old['id']), placeholders, source_hashes)]


def missing_repair_deliverables(job, repository):
    """Protect named delivered files and still-required documentation during repair.

    A follow-up starts a fresh repair checkpoint after scope reconciliation; removed
    parent requirements are not copied into this list.
    """
    if not job.get('repair_checkpoint'):
        return []
    current = tree_hashes(repository.root, repository.excludes)
    missing = ['deliverable:' + name for name in job.get('repair_deliverables', []) if name not in current]
    from coder_policy7 import documented
    packages = [p.get('cwd', '.') for p in (job.get('execution_profile') or {}).get('profiles', []) if isinstance(p, dict)]
    for component in job.get('repair_documentation', []):
        if not documented(repository.root, component, packages):
            missing.append('documentation:' + component)
    return missing


def retained_deliverables(job):
    import re
    from coder_scope import effective_task
    known = (job.get('last_patch') or {}).get('source_hashes', {})
    text = effective_task(job)
    named = [name for name in known if re.search(r'(?<![\w./-])' + re.escape(name) + r'(?![\w./-])', text)]
    docs = [o.get('component', '.') for o in job.get('brief', {}).get('outcomes', [])
            if 'documentation' in o.get('evidence_types', [])]
    # Only preserve documentation that was present before this repair.
    documented = {o.get('component', '.') for o in (job.get('verification_summary') or {}).get('outcomes', [])
                  if 'documentation' not in o.get('missing_evidence', [])}
    return {'repair_deliverables': named, 'repair_documentation': [c for c in docs if c in documented]}


def restore_repair(repository, checkpoint, failed_revision):
    """Both immutable commits remain reachable, while the working tree is restored."""
    import re
    if any(not re.fullmatch(r'[a-f0-9]{40,64}', value) for value in (checkpoint, failed_revision)):
        raise ValueError('Invalid repair checkpoint')
    repository.git('update-ref', 'refs/daedalus/failed-repairs/' + failed_revision, failed_revision)
    # Inventory refresh rebuilds the scratch index. Restore its failed tree
    # first so checkout knows which newly added files must be removed.
    repository.git('read-tree', failed_revision)
    repository.git('read-tree', '--reset', '-u', checkpoint)
    restored = repository.snapshot('Restore passing pre-repair checkpoint', parent=checkpoint)
    if restored['revision'] != checkpoint:
        raise ValueError('Repair rollback did not reproduce the saved checkpoint')
    return restored


def baseline_test_files(job):
    """Existing user tests need explicit current-request authorization to change."""
    import re
    from coder_scope import effective_task
    task = effective_task(job)
    names = {b['path'] for row in job.get('baseline_checks', job.get('baseline', []))
             for b in row.get('test_files', [])}
    protected = []
    for name in sorted(names):
        target = r'(?:[`"\']?' + re.escape(name) + r'[`"\']?|(?:the\s+)?(?:existing\s+)?(?:unit\s+)?tests?)'
        pattern = r'\b(?:edit|modify|update|rewrite|remove|delete|replace|fix)\s+' + target + r'(?![\w./-])'
        authorized = any(not re.search(r"(?:not|never|don't)\s*$", task[max(0,m.start()-20):m.start()], re.I)
                         for m in re.finditer(pattern, task, re.I))
        if not authorized: protected.append(name)
    return protected
