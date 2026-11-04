"""Bounded in-session feedback. Final evidence always comes from the supervisor."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

MAX_REPAIRS = 2


def checklist_reminder(state):
    if state.get('plan_finalization_version') == 1 and any(row.get('status') != 'done' for row in state.get('plan', [])):
        from coder_plan_finalization import request
        try: allowed = request(state['bridge_url'], 'begin').get('allowed')
        except Exception: allowed = False
        if allowed:
            state['finalizing'] = True
            return True, 'Before finishing, reconcile the existing task_tracker plan. Mark completed steps done; keep incomplete steps unresolved. Only task_tracker and finish are allowed, within two model turns and 60 seconds of the existing allowance. This reminder does not grant a coding repair.'
    return False, ''


def defer_verification(state, reason):
    state['passed'] = False
    state['deferred_reason'] = reason
    # Finish is still normal when checks need controller-owned preparation.
    # Bookkeeping may run, but it cannot claim those checks succeeded.
    deny, reminder = checklist_reminder(state)
    return deny, reminder if deny else reason


def evaluate(state):
    if not state.get('finish_seen'):
        state['nudges'] += 1
        return state['nudges'] <= 2, 'Use a tool to continue the work, or call finish when implementation and checks are done.'
    if state.get('finalizing'):
        from coder_plan_finalization import request
        try: request(state['bridge_url'], 'end')
        except Exception: pass  # Independent verification still runs.
        return False, state.get('deferred_reason', '')
    from coder_repository import Repository, safe_relative
    from coder_profiles import discover
    from context_policy import DEFAULTS
    root = Path(state['root'])
    with tempfile.TemporaryDirectory(prefix='daedalus-stop-') as temporary:
        repository = Repository(root, Path(temporary) / 'inventory', DEFAULTS['daedalus_exclude_dirs'])
        inventory = repository.refresh()
        failures = [] if inventory['files'] else ['The project contains no deliverable files.']
        profile = discover(repository, state.get('execution_commands'))
        cache_root = os.environ.get('DAEDALUS_ENVIRONMENT_ROOT')
        if cache_root:
            from coder_profiles import environment_key
            for package in profile.get('profiles', []):
                if not package.get('commands', {}).get('setup'):
                    continue
                key, _ = environment_key(safe_relative(root, package['cwd']), package)
                from coder_prepared_environment import is_ready
                ready = is_ready(cache_root, safe_relative(root, package['cwd']), key, os.environ.get('DAEDALUS_PREPARATION_OPERATION')) if os.environ.get('DAEDALUS_RUNTIME_POLICY') == '1' else (Path(cache_root) / key / 'environment-ready.json').is_file()
                if not ready:
                    return defer_verification(state, 'Declared dependencies changed or preparation failed; defer to controller-owned preparation without consuming a code-repair cycle.')
        for check in profile['checks']:
            if check.get('phase') not in {'build', 'test', 'tests', 'lint', 'typecheck'} and not check.get('is_test'):
                continue
            cwd = safe_relative(root, check.get('cwd', '.'))
            if '.venv/bin/python' in check['command'] and not (cwd / '.venv/bin/python').exists():
                # New files can introduce a profile after the child's preflight.
                # Missing controller dependencies are not defects to ask the model
                # to repair. Final checks provision their own fresh environment.
                return defer_verification(state, 'Environment preparation is required; defer checks to the independent runner.')
            with tempfile.TemporaryFile() as log:
                process = subprocess.Popen(['bash', '-c', check['command']], cwd=cwd,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    code = process.wait(timeout=state['command_seconds'])
                except subprocess.TimeoutExpired:
                    from coder_patch_runtime import stop_process
                    stop_process(process)
                    code = -1
                finally:
                    from coder_patch_runtime import stop_process
                    stop_process(process)
                log.seek(0, 2); length = log.tell(); log.seek(max(0, length - 2500))
                tail = log.read().decode(errors='replace')
            if code:
                from coder_prepared_environment import classify
                diagnostic = classify(code, tail, phase=check.get('phase', 'test'), managed_paths=[root / '.venv', root / 'node_modules', root / 'vendor'])
                if diagnostic:
                    state['environment_failure'] = diagnostic
                    return defer_verification(state, 'Environment checks failed; defer to controller preparation without spending a code repair.')
                failures.append(f"{check['id']}: {check['command']} exited {code}\n{tail}")
        state['passed'] = not failures
        if failures:
            if state['denials'] < MAX_REPAIRS:
                state['denials'] += 1
                return True, '\n'.join(failures)[:7000]
            return False, 'The two repair cycles are exhausted. Fresh verification will report the remaining failures.'
        return checklist_reminder(state)


def main(path):
    state = json.loads(Path(path).read_text())
    try:
        deny, reason = evaluate(state)
    except Exception as error:
        state['passed'] = False
        # Hook errors never turn into successful completion, even though the SDK
        # allows nonblocking hook errors. The supervisor independently rechecks.
        deny, reason = False, 'Stop checks unavailable: ' + str(error)
    state['reason'] = reason
    temporary = Path(path).with_suffix('.tmp')
    temporary.write_text(json.dumps(state)); temporary.replace(path)
    print(json.dumps({'decision': 'deny' if deny else 'allow', 'reason': reason}))
    if deny:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
