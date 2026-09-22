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
    text = '\nPROJECT CONVENTIONS (the controller discovers and runs these itself after you return):\n' + EXECUTION_HELP
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
        text += 'FAILED CHECKS:\n' + '\n'.join(
            '- ' + str(d.get('id')) + ': ' + str(d.get('reason') or d.get('command') or d.get('classification') or '')[:400] for d in defects[:8]) + '\n'
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


def sdk_build(store, operation_id, repository, job, payload, *, runner=None):
    """Run the agent-loop builder and adapt its result to what edit_transition and
    repair_targets consume (changed, category, source_hashes, log_tail)."""
    from coder_patch_runtime import tree_hashes
    if runner is None:
        from coder_sdk_runtime import run_coder as runner
    excludes = tuple(payload['settings']['daedalus_exclude_dirs']) + ('.git', '.aider', '__pycache__')
    sanitize_workspace(repository.root, excludes)   # also frees a job an earlier round already wedged
    before = tree_hashes(repository.root, excludes)
    outcomes = (job.get('brief') or {}).get('outcomes', [])
    failures = [{k: d.get(k) for k in ('id', 'classification', 'origin', 'command', 'reason', 'log_tail')} for d in job.get('repair_feedback') or []]
    current = store.get(operation_id)
    # The agent runner reads its inputs from the stored payload; policy 7 nests them under policy7_job.
    store.update(operation_id, payload={**current['payload'], 'brief': job.get('brief'), 'builder_guidance': guidance(job),
        # One agent session per round: continuations resume it with their context intact.
        'milestone_id': f"build-r{job.get('repair_round', 0)}", 'round_id': job.get('repair_round', 0),
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
    for name in tree_hashes(repository.root, excludes).keys() - before.keys() - planned:
        path = repository.root / name
        try:
            with path.open('rb') as handle:
                runtime_state = handle.read(16) == b'SQLite format 3\x00'
        except OSError:
            continue
        if runtime_state or name.endswith(('-wal', '-shm', '-journal')):
            path.unlink(missing_ok=True)
    after = tree_hashes(repository.root, excludes)
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    changed = [p for p in changed if p in before or (repository.root / p).stat().st_size > 0]
    # Spent calls or a stalled agent end the pass at its checkpoint; neither is an inference fault.
    spent = 'model-call allowance' in error.lower()
    stalled = 'builder stalled' in error.lower()
    category = 'source_changed' if changed else 'stalled' if stalled else 'allowance' if spent else 'environment' if error else \
        'no_op' if outcome.get('agent_finished') else 'narration'
    result = {'builder': 'sdk', 'changed': changed, 'applied': changed, 'category': category, 'errors': [], 'source_hashes': after,
              'log_tail': error or outcome.get('incomplete_reason', ''), 'summary': f'{len(changed)} files changed',
              'allowance_spent': spent, 'stalled': stalled, 'agent_finished': bool(outcome.get('agent_finished')), 'execution_status': outcome.get('execution_status', ''),
              'session_id': outcome.get('session_id', ''), 'events': outcome.get('events', 0), 'removed': removed}
    store.event(operation_id, 'builder_returned', **{k: v for k, v in result.items() if k != 'source_hashes'})
    return result
