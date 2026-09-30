"""Policy-7 hybrid builder for from-scratch projects.

One-shot Aider edits write blind: the model first meets a compiler or test error in
the final repair rounds. New projects instead use the agent-loop builder (terminal,
file editor, grep) so it can compile, run and fix while it works. Acceptance is
unchanged: policy 7's checks, independent audit, review and evidence gate decide.
Uploaded repairs and edits keep the Aider editor.
"""
from __future__ import annotations

import re

KEY_ERROR = re.compile(r'\berror\b|Error:|\bFAILED\b|Assertion|Segmentation fault|panicked|undefined reference|'
                       r'cannot find|expected .* found|SyntaxError|Traceback')
NOISE = re.compile(r'^g?make(\[\d+\])?:|npm error A complete log|^\[.*INFO.*\]')


def key_error_lines(defects, limit=14):
    """Runners bury the decisive line under build-tool summaries; surface it first."""
    lines = []
    for defect in defects or []:
        for line in (defect.get('log_tail') or '').splitlines():
            text = line.strip()
            if text and KEY_ERROR.search(text) and not NOISE.search(text):
                lines.append(text[:300])
    return list(dict.fromkeys(lines))[:limit]


def guidance(job):
    from coder_policy7 import EXECUTION_HELP
    text = ('Existing user tests are protected: ' + ', '.join(job.get('baseline_test_files', [])) + '. Add new tests in new files.\n' if job.get('baseline_test_files') else '')
    if job.get('repair_round'):
        text += ('\nPROJECT CONVENTIONS: unchanged from the build round (native test runners, .daedalus.json only for a non-native test '
                 'command, .daedalus-run.json only for HTTP services with {port}, Playwright tests under DAEDALUS_AUDIT_PYTHON).\n')
    else:
        text += '\nPROJECT CONVENTIONS (the controller discovers and runs these itself after you return):\n' + EXECUTION_HELP
    text += ('\nDo NOT write a .daedalus.json when the native test command already works (mvn test, ctest, dotnet test, go test, '
             'cargo test, npm test, pytest): the controller discovers it. Never wrap a native test suite in a Python script.'
             '\nWrite EVERY requested deliverable (application source, executable tests, README) in this session. A chat message ends '
             'nothing: keep using tools until all files exist and the tests pass, then call finish. '
             'Build and run the project and its tests yourself with the terminal before finishing; fix what fails. '
             'Keep the layout minimal: only files this project needs, one copy of each, no scratch or duplicate sources.\n')
    if job.get('build_continuations'):
        text += ('\nCONTINUE: your previous run stopped at a partial checkpoint. The files you already wrote are in the project. '
                 'Write the REMAINING files from the brief (application, tests, README), then build and run the tests. '
                 'Call finish only when every requested deliverable exists and its tests pass.\n')
    prerequisites = job.get('preflight_failures') or []
    if prerequisites:
        text += '\nSETUP/BUILD MUST SUCCEED BEFORE AUDIT. Correct these prerequisite failures on the existing checkpoint:\n'
        text += '\n'.join('- ' + str(d.get('command') or d.get('id')) + '\n' + str(d.get('log_tail') or d.get('reason') or '')[-1600:]
                          for d in prerequisites)
    from coder_skeletons import guidance as skeleton_guidance
    text += skeleton_guidance(job.get('skeleton'))
    defects = job.get('repair_feedback') or []
    if job.get('repair_round') and defects:
        keys = key_error_lines(defects)
        text += '\nREPAIR ROUND ' + str(job['repair_round']) + ': the controller\'s verification of your last checkpoint failed. Fix exactly this, then re-run the checks:\n'
        if keys:
            text += 'KEY ERROR LINES:\n' + '\n'.join(keys) + '\n'
        if all(d.get('origin') == 'independent' for d in defects):
            text += ('These failing checks are an independent audit written without running your program; its expected values can be '
                     'wrong. First reproduce the failing command yourself. If the application already does what the ORIGINAL REQUEST '
                     'says, change nothing and call finish immediately, stating the evidence; the audit will be corrected instead.\n')
        from coder_repair_packet import render
        packet = job.get('repair_packet') or []
        if packet:
            text += render(packet) + '\n'
        else:
            text += 'FAILED CHECKS:\n' + '\n'.join(
                '- ' + str(d.get('id')) + ': ' + str(d.get('reason') or d.get('command') or d.get('classification') or '')[:400] for d in defects[:8]) + '\n'
        text += ('This is an EXISTING CHECKPOINT with work in it: never delete or re-initialise it (rm -rf, cargo new, dotnet new, npm init, '
                 'git init, create-* generators are forbidden). Every check that passed before must still pass after your fix.\n')
    return text


def sanitize_workspace(root, excludes=()):
    """Remove what an agent with a terminal leaves behind that the checkpoint cannot hold.

    `python3 -m venv env` links env/bin/python3 to the host interpreter. Only .venv/venv are excluded,
    so the link guard raised outside this builder's try, the fallback snapshot raised the same way
    and every later operation did too: a permanent wedge with the round's work lost. A virtualenv is
    an environment, never a deliverable (the controller builds its own), and a link that leaves the
    workspace can never be packaged. Returns the removed paths, relative to the workspace.
    """
    import os, shutil
    root, removed = root.resolve(), []
    for directory, dirs, files in os.walk(root, followlinks=False):
        here = type(root)(directory)
        dirs[:] = [name for name in dirs if name not in excludes and name != '.git']
        for name in list(dirs):
            path = here / name
            if not path.is_symlink() and (path / 'pyvenv.cfg').is_file():
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path.relative_to(root).as_posix()); dirs.remove(name)
        for name in [*dirs, *files]:
            path = here / name
            if path.is_symlink():
                try:
                    inside = path.resolve().is_relative_to(root)
                except (OSError, RuntimeError):
                    inside = False
                if not inside:
                    path.unlink(missing_ok=True)
                    removed.append(path.relative_to(root).as_posix())
                    if name in dirs:
                        dirs.remove(name)
    return removed


# A service log the agent's trial run left behind (sweep3-web medium p1: the last patch changed only server.log).
RUNTIME_LOG = re.compile(r'^(?:[^/]+/)?(?:server|app|application|api|backend|frontend|vite|uvicorn|gunicorn|flask|django|node|npm-debug|'
                         r'yarn-error|dev|debug|error|errors|access|output|out|run|start|nohup|stdout|stderr)(?:[.-][\w-]*)?\.(?:log|out)$', re.I)
FIXTURE_DIRS = {'tests', 'test', 'fixtures', 'testdata', 'samples', 'sample', 'examples', 'data', 'docs'}


def runtime_log(name):
    """A runtime log at the project or package root; a log under a fixture directory is a deliverable and stays."""
    parts = name.split('/')
    if any(part.lower() in FIXTURE_DIRS for part in parts[:-1]):
        return False
    return bool(RUNTIME_LOG.match(name))


def logs_are_input(task):
    """Log files are the application's domain (a log summarizer): never remove any of them."""
    return bool(re.search(r'\.log\b|\blog\s*files?\b|<[^<>]*log[^<>]*>', task or '', re.I))


def sdk_build(store, operation_id, repository, job, payload, *, runner=None):
    """Run the agent-loop builder and adapt its result to what edit_transition and
    repair_targets consume (changed, category, source_hashes, log_tail)."""
    from coder_patch_runtime import tree_hashes
    if runner is None:
        from coder_sdk_runtime import run_coder as runner
    excludes = tuple(payload['settings']['daedalus_exclude_dirs']) + ('.git', '.aider', '__pycache__')
    sanitize_workspace(repository.root, excludes)   # also frees a job an earlier round already wedged
    before = tree_hashes(repository.root, excludes)
    skeleton_applied = []
    skeleton = job.get('skeleton')
    if skeleton and not skeleton.get('applied') and not job.get('repair_round') and not job.get('build_continuations') and not before:
        # A verified starting structure, written once into an EMPTY workspace so the build proceeds as an
        # edit of runnable files. The empty-tree guard keeps a re-dispatched operation from writing twice.
        from coder_skeletons import apply
        skeleton_applied = apply(repository.root, skeleton)
        store.event(operation_id, 'skeleton_applied', skeleton=skeleton['id'], version=skeleton.get('version'), files=skeleton_applied)
    outcomes = (job.get('brief') or {}).get('outcomes', [])
    failures = [{k: d.get(k) for k in ('id', 'classification', 'origin', 'command', 'reason', 'log_tail', 'request_excerpt', 'probe_input', 'exit_code')}
                for d in [*job.get('preflight_failures', []), *job.get('repair_feedback', [])]]
    if job.get('repair_packet'):
        failures = [{**entry, 'log_tail': ' | '.join(entry.get('trace') or [])} for entry in job['repair_packet']]
    current = store.get(operation_id)
    # The agent runner reads its inputs from the stored payload; policy 7 nests them under policy7_job.
    store.update(operation_id, payload={**current['payload'], 'brief': job.get('brief'), 'builder_guidance': guidance(job),
        # One agent session per round: continuations resume it with their context intact. The focused retry
        # starts a FRESH session: re-sending the whole repair task into the first attempt's session pushed
        # the request over the input budget in 15 of 15 archived repair retries (2026-09-25), so the retry
        # never ran and the job reported "no source changes".
        'milestone_id': f"build-r{job.get('repair_round', 0)}" + ('-retry' if job.get('recovery_used') else ''),
        'round_id': job.get('repair_round', 0),
        'focused_recovery': bool(job.get('recovery_used')), 'builder_until_finish': True,
        'ui_required': any(re.search(r'\bbrowser\b|\bweb\b|\bfrontend\b', o.get('text', ''), re.I) for o in outcomes),
        'evidence': {'brief': job.get('brief'), 'failures': failures, 'revision_id': payload['revision_id']}})
    outcome, error = {}, ''
    try:
        outcome = runner(store, operation_id, repository) or {}
    except (ValueError, RuntimeError) as failure:
        # Partial work is still a checkpoint; the controller's checks decide what it is worth.
        error = type(failure).__name__ + ': ' + str(failure)
    # Scaffolding tools (cargo new, git init, create-*) leave nested repositories that break the
    # controller's own checkpoint snapshot.
    import shutil
    for nested in sorted(repository.root.rglob('.git')):
        if nested.parent != repository.root and 'node_modules' not in nested.parts:
            shutil.rmtree(nested, ignore_errors=True) if nested.is_dir() else nested.unlink(missing_ok=True)
    removed = sanitize_workspace(repository.root, excludes)
    # The agent starts the app to try it, which leaves its database (with the agent's test rows) in the
    # project. That is runtime state, not a deliverable: the application recreates it on first start.
    planned = {name for batch in (job.get('brief') or {}).get('batches', []) for name in batch.get('files', [])}
    from coder_scope import effective_task
    keep_logs = logs_are_input(effective_task(job))
    for name in tree_hashes(repository.root, excludes).keys() - before.keys() - planned:
        path = repository.root / name
        try:
            with path.open('rb') as handle:
                runtime_state = handle.read(16) == b'SQLite format 3\x00'
        except OSError:
            continue
        # Server logs are runtime state too (the last patch of a medium build once changed only server.log).
        if runtime_state or name.endswith(('-wal', '-shm', '-journal')) or (not keep_logs and runtime_log(name)):
            path.unlink(missing_ok=True)
    after = tree_hashes(repository.root, excludes)
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    changed = [p for p in changed if p in before or (repository.root / p).stat().st_size > 0]
    # Spent calls or a stalled agent end the pass at its checkpoint; neither is an inference fault.
    spent = 'model-call allowance' in error.lower()
    stalled = 'builder stalled' in error.lower()
    over_budget = 'input budget' in error.lower()
    category = 'source_changed' if changed else 'stalled' if stalled else 'allowance' if spent else 'input_budget' if over_budget else \
        'environment' if error else 'no_op' if outcome.get('agent_finished') else 'narration'
    result = {'builder': 'sdk', 'changed': changed, 'applied': changed, 'category': category, 'errors': [], 'source_hashes': after,
              'log_tail': error or outcome.get('incomplete_reason', ''), 'summary': f'{len(changed)} files changed',
              'allowance_spent': spent, 'stalled': stalled, 'agent_finished': bool(outcome.get('agent_finished')), 'execution_status': outcome.get('execution_status', ''),
              'session_id': outcome.get('session_id', ''), 'events': outcome.get('events', 0), 'removed': removed,
              'skeleton_applied': bool(skeleton_applied), 'input_budget': over_budget}
    store.event(operation_id, 'builder_returned', **{k: v for k, v in result.items() if k != 'source_hashes'})
    return result
