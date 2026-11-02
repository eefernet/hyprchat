"""SDK stop hook: check the project when the builder says it is done, inside the same session.

The OpenHands SDK runs this script (a registered Stop hook) every time the agent's run would end: after a
``finish`` call and after a reply without a tool call. It reads a status file that the sandbox child keeps
up to date and answers on stdout with ``{"decision": "allow"}`` or ``{"decision": "deny", "reason": ...}``;
the SDK feeds a deny reason back to the agent as the next user message and the run continues.

* After ``finish``: run the project's own discovered build and test commands on the live workspace. Real
  failures deny the stop with the command, exit code and last lines (at most ``MAX_DENIALS`` per job).
* After a reply without a tool call: one short nudge to act or finish (at most ``MAX_NUDGES`` per job).
* Never blocks on the environment: a missing toolchain, no network, an uninstalled third-party module or
  declared Node package in a package whose read-only dependency cache predates its manifests
  (``stale_packages``) or a timeout set by the hook's own limits lets the stop through. The controller's full
  check on a fresh extraction stays the authority.
* Stops are allowed once fewer turns remain than a denial needs to fix and finish; a pass that still ends
  after a denied finish is reported as ``hook_denied_unresolved`` (``outcome``), not as unfinished work.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

MAX_DENIALS = 3
MAX_NUDGES = 2
TAIL_LINES = 40
RESERVE_TURNS = 5   # a denial needs turns to read, edit, re-run and finish; scaled up for long allowances
OUTPUT_BYTES = 64 * 1024
# Failures that say the sandbox cannot do this here, not that the program is wrong.
ENVIRONMENT = re.compile(
    r'Read-only file system|EROFS|Could not resolve host|Temporary failure in name resolution|Network is unreachable|'
    r'Failed to establish a new connection|Name or service not known|\bENOTFOUND\b|\bEAI_AGAIN\b|getaddrinfo|'
    r'Could not transfer artifact|Could not resolve dependencies|error: failed to download|failed to get .* as a dependency|'
    r'dial tcp|proxyconnect|Unable to load the service index|No module named pip|ensurepip is not available|'
    r'command not found|No such file or directory: .*\.venv|'
    # The hook builds .NET with --no-restore and cannot restore offline; the controller's check runs restore.
    r'NETSDK1004|NETSDK1064', re.I)
# Setup that configures inside the writable workspace (cmake, dotnet restore): nothing there can go stale. Any
# other setup may fill a cache the controller links read-only (.venv, node_modules, vendor), whatever its
# spelling ('npm i', bare 'yarn', 'uv sync'); listing the installing spellings missed configured setups.
WORKSPACE_SETUP = re.compile(r'^\s*(?:cmake|dotnet)\b')
# A stale cache shows up as a third-party module that is not installed. A path is the project's own file.
MISSING_DEPENDENCY = re.compile(
    r"No module named '?(?P<python>[\w.]+)|Cannot find package '|Cannot find module '(?![./]|file:)|"
    r"cannot load such file -- (?![./])|Could not find gem|vendor/autoload\.php")
# A resolver's own wording (Vite/Rollup/Vitest, esbuild, webpack, Node). A code frame or the source line Node
# echoes above a SyntaxError also quotes the import, but that is the program's fault, not the cache's.
UNRESOLVED = r"(?i:resolve import|could not resolve|can't resolve|cannot find (?:module|package)|module not found)"
NODE_SECTIONS = ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies')
NPM_NAME = re.compile(r'(?:@[\w~-][\w.~-]*/)?[\w~-][\w.~-]*')
SKIPPED = re.compile(r'DAEDALUS_AUDIT_PYTHON|playwright', re.I)
NUDGE = ('You replied without a tool call, so nothing was done. Continue the work with a tool, or call finish '
         'when every requested file exists and its tests pass.')


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def save(path, state):
    temporary = Path(str(path) + '.tmp')
    temporary.write_text(json.dumps(state))
    temporary.replace(path)


def telemetry(state, decision, **values):
    """Record the decision in the job's telemetry stream so the worker ledger shows hook activity."""
    target = state.get('telemetry')
    if not target:
        return
    try:
        with open(target, 'a') as stream:
            stream.write(json.dumps({'type': 'stop_hook', 'data': {'decision': decision, **values}}) + '\n')
    except OSError:
        pass


def environment_failure(code, tail):
    return code in (126, 127) or bool(ENVIRONMENT.search(tail or ''))


def missing_dependency(tail, cwd):
    """The tail names an uninstalled third-party module. A Python name that is a file or folder of the
    package is the project's own import going wrong, which a fresh environment would not fix."""
    match = MISSING_DEPENDENCY.search(tail or '')
    top = (match.group('python') or '').split('.')[0] if match else ''
    return bool(match) and not (top and ((cwd / (top + '.py')).exists() or (cwd / top).is_dir()))


def uninstalled_dependencies(cwd, root):
    """Names package.json declares that no node_modules from cwd up to root holds (Node resolves upward)."""
    try:
        manifest = json.loads((cwd / 'package.json').read_text())
    except (OSError, ValueError):
        return set()
    sections = [manifest.get(s) for s in NODE_SECTIONS] if isinstance(manifest, dict) else []
    declared = {name for section in sections if isinstance(section, dict) for name in section if NPM_NAME.fullmatch(name)}
    folders = [cwd, *(p for p in cwd.parents if p == root or root in p.parents)]
    return {name for name in declared if not any((f / 'node_modules' / name / 'package.json').is_file() for f in folders)}


def names_uninstalled(tail, cwd, root):
    """A resolver message in the tail quotes a declared, uninstalled package ("axios", 'axios/lib') on its own
    line, in whatever wording the bundler uses (Vite, Vitest, esbuild, webpack). An undeclared or misspelled
    import is not matched: a fresh setup would not install it either. Nor is a syntax error whose code frame
    merely quotes that import."""
    return any(re.search(UNRESOLVED + r'[^\n]*?(["\'])' + re.escape(name) + r'(?:\1|/)', tail or '')
               for name in uninstalled_dependencies(cwd, root))


def stop(process):
    """End the command's whole process group, leader exited or not, without an unbounded wait."""
    for sent, grace in ((signal.SIGTERM, 2), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, sent)
        except OSError:
            return   # nothing left in the group
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


def run(command, cwd, timeout):
    """Run one command in its own process group; (exit code, output tail, timed out).

    Output goes to a file and only the leader is awaited, as in the controller's execute(): a background
    process the command leaves behind neither holds the result open nor outlives the hook."""
    with tempfile.TemporaryFile() as output:
        started = subprocess.Popen(command, shell=True, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True, env=os.environ.copy())
        deadline = time.monotonic() + timeout
        try:
            while started.poll() is None and time.monotonic() < deadline:
                time.sleep(.1)
            code = started.poll()
        finally:
            stop(started)
        output.seek(0, os.SEEK_END)
        output.seek(max(0, output.tell() - OUTPUT_BYTES))
        text = output.read().decode(errors='replace')
    tail = '\n'.join(text.splitlines()[-TAIL_LINES:])
    return (124 if code is None else code), tail, code is None


def discovered(state):
    """The project contract, discovered the same way the controller discovers it."""
    from coder_project_runtime import contract
    from coder_repository import Repository
    repository = Repository(Path(state['root']), Path(state['inventory']), state.get('excludes') or ())
    return contract(repository, state.get('execution_commands') or None)


def project_checks(profile):
    """Build and test commands."""
    return [c for c in profile.get('checks', []) if c.get('phase') in {'build', 'test'} and c.get('command')
            and not SKIPPED.search(c['command'])]


def environment_keys(state, profile=None):
    """{package cwd: dependency-environment key} for each package with a setup that may install into a controller
    cache, discovered or configured: the controller caches and links back whatever any setup leaves there.

    Cargo, Go, Maven, Gradle and Make have no setup, and CMake and .NET set up inside the writable workspace, so
    none of them has a prepared environment to go stale. Counting too many is safe: a stale package only leaves
    missing-dependency failures to the controller."""
    from coder_profiles import environment_key
    profile = profile or discovered(state)
    return {p['cwd']: environment_key(Path(state['root']) / p['cwd'], p)[0] for p in profile.get('profiles', [])
            if any(not WORKSPACE_SETUP.search(c) for c in p.get('commands', {}).get('setup', []))}


def stale_packages(state, profile):
    """Packages whose dependency environment moved since the operation began, or that appeared since.

    Their .venv / node_modules / vendor is the controller's read-only cache for the old manifests and the agent
    cannot install into it. Their checks still run; only a missing third-party module ('No module named pytest',
    "Cannot find module 'supertest'", or a bundler naming a declared package node_modules lacks) is left to the
    controller, which re-runs setup. Assertion and compile failures still deny. A state without recorded keys
    is not judged."""
    recorded = state.get('environment_keys')
    if not isinstance(recorded, dict):
        return set()
    return {cwd for cwd, key in environment_keys(state, profile).items() if recorded.get(cwd) != key}


def failures(state, budget_seconds, skipped=None):
    found, deadline = [], time.monotonic() + budget_seconds
    skipped = [] if skipped is None else skipped
    configured = int(state.get('command_seconds') or 300)
    per_command = max(30, min(configured, 300))
    profile = discovered(state)
    stale = stale_packages(state, profile)
    for check in project_checks(profile):
        remaining = deadline - time.monotonic()
        if remaining < 10:
            break
        cwd = Path(state['root']) / check.get('cwd', '.')
        limit = min(per_command, remaining)
        code, tail, timed_out = run(check['command'], cwd, limit)
        if code == 0 or environment_failure(code, tail):
            continue
        if check.get('cwd', '.') in stale and (missing_dependency(tail, cwd)
                                               or names_uninstalled(tail, cwd, Path(state['root']))):
            skipped.append(f"{check['command']} (dependency environment changed)")
            continue
        if timed_out and limit < configured:
            # Cut short by the hook's own cap or remaining budget, not the command limit the controller applies.
            skipped.append(f"{check['command']} (stopped at {int(limit)} s)")
            continue
        found.append({'command': check['command'], 'cwd': check.get('cwd', '.'), 'phase': check.get('phase'),
                      'exit_code': code, 'timed_out': timed_out, 'tail': tail[-1800:]})
        if check.get('phase') == 'build':
            break   # tests of a project that does not build only repeat the build error
        if len(found) >= 3:
            break
    return found


def report(found):
    lines = ['Not done yet: the controller ran this project\'s own checks and they failed. Fix the cause, run the '
             'same commands yourself until they pass, then call finish. Do not delete or weaken tests to pass.']
    for row in found:
        where = '' if row['cwd'] in ('', '.') else f" (in {row['cwd']})"
        verdict = 'timed out' if row['timed_out'] else f"exit {row['exit_code']}"
        lines += ['', f"$ {row['command']}{where}  ->  {verdict}", row['tail']]
    return '\n'.join(lines)[:6000]


def decide(state, budget_seconds):
    """(decision, reason, state updates). Pure apart from running the project's commands."""
    turn_limit = int(state.get('turn_limit') or 0)
    if turn_limit - int(state.get('turns') or 0) < max(RESERVE_TURNS, turn_limit // 4):
        return 'allow', 'turn allowance nearly spent', {}
    if not state.get('finish_seen'):
        if int(state.get('nudges') or 0) >= MAX_NUDGES:
            return 'allow', 'prose stop; nudges spent', {}
        return 'deny', NUDGE, {'nudges': int(state.get('nudges') or 0) + 1}
    if int(state.get('denials') or 0) >= MAX_DENIALS:
        return 'allow', 'finish; denials spent', {}
    skipped = []
    try:
        found = failures(state, budget_seconds, skipped)
    except Exception as error:   # discovery or the hook itself: never trap the agent
        return 'allow', f'checks unavailable: {type(error).__name__}: {error}'[:300], {}
    if not found:
        return 'allow', 'checks passed' + ('; left to the controller: ' + '; '.join(skipped) if skipped else ''), {}
    return 'deny', report(found), {'denials': int(state.get('denials') or 0) + 1, 'finish_seen': False,
                                   'last_failures': [{k: row[k] for k in ('command', 'cwd', 'exit_code')} for row in found]}


def outcome(state, denials_before, finished):
    """The hook's part of the run result. A pass that ended after a denied finish with no finish allowed
    later (the turn ceiling came first) is unresolved: the agent did call finish, so it is not unfinished work."""
    denials = int(state.get('denials') or 0)
    return {'hook_denials': denials, 'hook_nudges': int(state.get('nudges') or 0),
            'hook_denied_unresolved': denials > int(denials_before or 0) and not finished}


def main(argv):
    status = Path(argv[1])
    state = load(status)
    budget = max(30, int(state.get('hook_seconds') or 600) - 30)
    decision, reason, updates = decide(state, budget)
    if updates:
        save(status, {**state, **updates})
    telemetry(state, decision, reason=reason[:500], denials=updates.get('denials', state.get('denials', 0)),
              nudges=updates.get('nudges', state.get('nudges', 0)))
    print(json.dumps({'decision': decision, **({'reason': reason} if decision == 'deny' else {})}))
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main(sys.argv))
