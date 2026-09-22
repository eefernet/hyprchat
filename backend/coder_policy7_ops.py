"""Policy-7 model operations. Executed only by the durable worker subprocess."""
from __future__ import annotations
import json
from pathlib import Path
import shutil

from coder_inference import local_chat
from coder_patch_runtime import edit, select_files, tree_hashes
from context_policy import resolve


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
    if failures and not job.get('repair_round') and not job.get('narrowed_targets'):
        manifests = []
        for failure in failures:
            package = failure.get('cwd', '.')
            for name in ('requirements.txt', 'pyproject.toml', 'package.json', 'vite.config.js', '.daedalus.json', '.daedalus-run.json'):
                if name in (failure.get('command') or '') + (failure.get('log_tail') or '') or name in {'requirements.txt', 'package.json'}:
                    manifests.append(name if package == '.' else package.rstrip('/') + '/' + name)
        targets = list(dict.fromkeys([*targets, *manifests]))
        task += ('\nTHE PROJECT SETUP/BUILD IS CURRENTLY FAILING after the previous step. Fix this first, with the smallest '
                 'edit to the owning manifest (for example never list Python standard-library modules such as sqlite3 in '
                 'requirements.txt), then do this step:\n' + json.dumps(failures))
    return task, targets


MAX_BUILD_CONTINUATIONS = 5
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
        if result.get('allowance_spent') or result.get('stalled'):
            # More passes cannot help: verify what exists, or hand over the checkpoint when nothing was written.
            if changed or continued or job.get('repair_round'):
                return check
            return {'state': 'candidate', 'stop_limit': 'model_calls' if result.get('allowance_spent') else 'no_progress',
                    'reason': 'The model-call allowance for building was used before any source was written.' if result.get('allowance_spent')
                    else 'The builder only re-read files and wrote nothing, even after a specific correction.'}
        if changed:
            return {'state': 'editing', 'build_continuations': continued + 1}
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
            result = edit(store, operation_id, repository.root, task, preferred=targets)
        restored = restore_protected(repository, job.get('baseline_revision') or payload['revision_id'], job.get('protected_files'))
        if restored:
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
            wanted = requested_interfaces(job['task'])
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
                        'classification': 'passed' if found else 'application_defect', 'evidence_types': [],
                        'outcomes': [], 'revision_id': payload['revision_id'], 'execution_id': operation_id + ':interface:' + label,
                        'log_tail': '' if found else 'The request names ' + label + ' but no delivered source file defines or handles it. '
                            'Implement exactly that ' + ('element id' if kind == 'selector' else 'command-line flag') + '; do not substitute another control.',
                        'reason': '' if found else 'Requested interface ' + label + ' is absent from the application source'})
            # Values a page script reads from a FormData by names the delivered HTML never assigns (the September
            # Kanban acceptance): a controller row on the immutable execution copy, never the editor's tree.
            from coder_frontend_guard import form_fields_row
            fields = form_fields_row(result['workspace'], payload['revision_id'], operation_id)
            if fields:
                result['checks'].append(fields)
            # A user-owned test that failed before the edit and passes now, byte-identical,
            # demonstrates the repair without any model-authored audit.
            from coder_policy7_evidence import repair_demonstrated
            before = {b.get('id'): b for b in job.get('baseline_checks', [])}
            for row in result['checks']:
                row['repair_demonstrated'] = repair_demonstrated(row, before.get(row.get('id')))
            # Absent documentation is an application defect, never an audit fault.
            from coder_policy7 import documentation_files
            result['missing_deliverables'] = [o['id'] for o in job.get('brief', {}).get('outcomes', [])
                if 'documentation' in o.get('evidence_types', []) and not documentation_files(repository.root, o.get('component', '.'))]
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
            'Write independent executable tests in the writable audit directory. The actual project is READ ONLY. '
            'Explicit new-file targets are audit.json and test_behavior.py. Inspect the supplied source. '
            'Never copy/reimplement the app or put project modules in the audit. Python clients may test a Node HTTP API; '
            'never import JavaScript as Python. Import Python projects using python3. For HTTP/browser clients use '
            '\"$DAEDALUS_AUDIT_PYTHON\" which has requests and Playwright. Use os.environ[\"DAEDALUS_APP_URL\"] '
            'and os.environ[\"DAEDALUS_PROJECT_ROOT\"]; never guess a port or derive project paths from __file__. '
            'Do not create services; the controller starts them. Use plain assert statements against actual application results. '
            'Use Python Playwright for browser checks, never Selenium or a DOM script executed by Node. '
            'Launch the application with ITS OWN runtime, never sys.executable unless the application itself is Python '
            '(node <file>.js for Node, go run . for Go). '
            'For a compiled program (Java, C, C++, C#, Go, Rust) the controller has ALREADY built it: launch the built artifact with '
            'subprocess.run from os.environ[\"DAEDALUS_PROJECT_ROOT\"] and assert on its real stdout/exit code, e.g. '
            '[\"java\",\"-cp\",\"target/classes\",\"<MainClass>\",...], [\"build/<exe>\",...], [\"dotnet\",\"<App>/bin/Debug/net8.0/<App>.dll\",...], '
            '[\"target/debug/<bin>\",...] or [\"go\",\"run\",\".\",...]. Never rebuild and never write inside the project. '
            'Include a __main__ entrypoint so python executes the assertions. Map each tested outcome ID in metadata; '
            'o1 is only an example, include every outcome actually covered by that check. '
            'Write audit.json metadata {\"checks\":[{\"command\":\"\\\"$DAEDALUS_AUDIT_PYTHON\\\" {audit}/test_behavior.py\",'
            '\"outcomes\":[\"o1\"],\"evidence_types\":[\"behavior\"],\"cwd\":\".\"}]}. '
            'Documentation uses {\"kind\":\"file\",\"path\":\"README.md\",\"outcomes\":[\"o2\"],'
            '\"evidence_types\":[\"documentation\"],\"assertions\":[{\"kind\":\"nonempty\"}]}. '
            'An independent audit cannot substitute for requested tests DELIVERED IN THE PROJECT. '
            'Correct faulty checks while retaining valid checks and all requested outcomes.\n' + json.dumps({
                'original_request': job['task'], 'outcomes_to_audit': [
                    {**row, 'evidence_types': [kind for kind in row['evidence_types'] if kind in {'behavior', 'documentation'}]}
                    for row in job['brief']['outcomes'] if set(row['evidence_types']) & {'behavior', 'documentation'}],
                'delivered_tests': 'Controller executes project tests separately; do not register them as audit commands.',
                'requested_interfaces_the_audit_must_use_exactly': requested_interfaces(job['task']),
                'correction_required': ([{**f, 'instruction': 'Add an executed assertion for this outcome and list its ID in audit.json outcomes'
                        if str(f.get('id', '')).endswith(':behavior') else 'Fix this audit check'} for f in job.get('audit_feedback', [])]
                    if job.get('audit_correction_pending') else []),
                'execution_profile': job.get('execution_profile'),
                'previous_failures': [{k: c.get(k) for k in ('id', 'classification', 'command', 'log_tail')}
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
        faults = static_audit_faults(directory, self.repo.root, self.job.get('task', '')) if script.is_file() and script.read_text().strip() else []
        if faults:
            # Visible before execution: one precise nudge inside this operation, never a correction round.
            if self.store:
                self.store.event(op, 'audit_static_faults', faults=faults)
            patch = self.editor(self.store, op, directory, 'Fix these defects in the audit files you just wrote, keeping everything else:\n- ' +
                '\n- '.join(faults) + '\n' + instruction, read_root=self.repo.root, preferred=['audit.json', 'test_behavior.py'])
        try:
            notes = normalize_audit(directory, job['brief']['outcomes'], self.repo.root)
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
                wanted = requested_interfaces(job['task'])
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
        instruction = ('Create a compact work brief. Return JSON {"outcomes":[{"text":"requested result","component":".","evidence_types":["behavior"]}],'
            '"batches":[{"task":"focused implementation task","files":["suggested path"]}]}. '
            'Size the plan to the task. A command-line tool, library, small web page or edit is ONE batch of at most five files. '
            'Only a genuinely multi-component application (for example a separate backend and frontend) gets up to four dependency-ordered batches. '
            'Never plan more files than the request needs: no duplicate sources, sample data, scratch or report files. '
            'Give explicit file targets, including new files, for each batch. List EVERY file needed to install, build and run, at most three files per batch, '
            'using ONLY the manifests of the project\'s own ecosystem (Python: requirements.txt/pyproject.toml; Node/React: package.json, and '
            'index.html + vite.config.js for a Vite frontend; Java: pom.xml; C/C++: CMakeLists.txt; C#: .sln + .csproj files; Go: go.mod; Rust: Cargo.toml). '
            'NEVER add another ecosystem\'s manifest (no package.json in a C++, C#, Go, Rust, Java or Python project). '
            'A command-line program needs NO .daedalus-run.json: that file declares long-running web services only. Evidence types are nonexclusive: behavior, documentation, tests, preservation. '
            'Include project_name with a short plain-language name for the application. '
            'A README and executable test requirement needs documentation AND tests. component is its actual package path or dot for the whole app. '
            'Include all requested behavior, tests and documentation. Browser regression tests should be Python Playwright files with a .daedalus.json test command; do not run DOM tests with Node. '
            'Do not write code or executable probes.\n')
        context = self.source_context(resolve('architect', self.job['settings']))
        retained = []
        for attempt in range(2):
            raw = self.chat(self.store, op, 'architect', [{'role': 'user', 'content': instruction + self.job['task'] + '\n' + json.dumps(context)}])
            retained.append(raw)
            try:
                brief = make_brief(json_object(raw), self.job['task'], self.job['inherited'])
                protected = self.protected_files()
                if not (protected or self.job.get('protected')):
                    for requirement in brief['outcomes']:
                        requirement['evidence_types'] = [k for k in requirement['evidence_types'] if k != 'preservation'] or ['behavior']
                return {'brief': brief, 'plan_text': retained, 'protected_files': protected, 'grounded_protected_files': self.grounded}
            except (ValueError, TypeError) as error:
                instruction += '\nCorrect this brief format: ' + str(error)
        return {'brief': make_brief({'outcomes': [self.job['task']], 'batches': [{'task': self.job['task']}]},
                                   self.job['task'], self.job['inherited']), 'plan_text': retained, 'planning_fallback': True,
                'protected_files': self.protected_files(), 'grounded_protected_files': self.grounded}

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
        from coder_policy7_evidence import acceptance, normalize_verdict
        policy = resolve('reviewer', self.job['settings'])
        outcomes = self.job['brief']['outcomes']
        checks = [{k: c.get(k) for k in ('id', 'passed', 'classification', 'command', 'log_tail', 'outcomes',
            'source_bindings', 'service_bindings', 'assertions_executed', 'evidence_types', 'test_files')} for c in self.job['checks']]
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
            'A passing check is insufficient if it copies application logic or checks another component.\n')
        gaps = acceptance(outcomes, self.job['checks'], {}, self.job['revision_id'])['outcomes']
        current = {c['id']: c.get('passed') for c in self.job['checks']}
        baseline = self.job.get('baseline_checks', self.job.get('baseline', []))
        data = {'original_request': self.job['task'], 'outcomes': outcomes, 'checks': checks,
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
        base, correction, retained, verdict, challenged = prompt + json.dumps(data), '', [], None, False
        # Same bounded format recovery as planning; a contradiction with executed
        # evidence earns one grounded re-ask, never an automatic pass.
        for attempt in range(4):
            try:
                raw = self.chat(self.store, op, 'reviewer', [{'role': 'user', 'content': base + correction}], schema=schema)
                retained.append(raw)
                verdict = normalize_verdict(json_object(raw), outcomes)
            except (ValueError, TypeError) as error:
                if getattr(error, 'failure_category', '') == 'output_limit':
                    correction = '\nYour previous review exceeded the output limit. Keep every reason under 25 words.'
                else:
                    correction = '\nCorrect this review format: ' + str(error)[:300]
                if 'rejected response format' in str(error):
                    schema = None
                verdict = None
                continue
            failing = {name for c in self.job['checks'] if not c.get('passed') for name in c.get('outcomes') or []}
            unscoped = any(not c.get('passed') and not c.get('outcomes') for c in self.job['checks'])
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
