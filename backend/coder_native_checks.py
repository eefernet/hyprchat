"""Ordinary native checks on disposable revision copies, with audit provenance."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import signal
import subprocess
import tarfile
import time

from coder_profiles import environment_key
from coder_repository import safe_relative, file_hash
from coder_verification import test_count
from context_policy import resolve

# Python imports (including constants) establish binding without requiring a call.
# Executed assert lines establish coverage for executable scripts, not printed prose.
PYTHON_TRACE = '''import atexit, ast, hashlib, json, os, pathlib, sys
root = pathlib.Path(os.environ['DAEDALUS_PROJECT_ROOT']).resolve()
audit = pathlib.Path(os.environ['DAEDALUS_AUDIT_DIR']).resolve()
lines, assertions, observed = {}, set(), set()
def trace(frame, event, arg):
    if event == 'call' and frame.f_globals.get('__name__') == 'unittest.case' and frame.f_code.co_name.startswith('assert'):
        caller = frame.f_back
        assertions.add((caller.f_code.co_filename, caller.f_lineno))
    if event == 'line':
        p = pathlib.Path(frame.f_code.co_filename).resolve()
        if p.is_relative_to(root) or p.is_relative_to(audit):
            if '.venv' not in p.parts and 'node_modules' not in p.parts:
                observed.add(str(p))
                if str(p) not in lines:
                    try: lines[str(p)] = {n.lineno for n in ast.walk(ast.parse(p.read_bytes())) if isinstance(n, ast.Assert)}
                    except Exception: lines[str(p)] = set()
                if frame.f_lineno in lines[str(p)]: assertions.add((str(p), frame.f_lineno))
    return trace
sys.settrace(trace)
def save():
    sys.settrace(None)
    imports = []
    for p in observed:
        path = pathlib.Path(p)
        if path.is_relative_to(root) and path.is_file():
            imports.append({'path':str(path.relative_to(root)), 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    target = pathlib.Path(os.environ['DAEDALUS_PROVENANCE']) / ('python-' + str(os.getpid()) + '.json')
    target.write_text(json.dumps({'imports':imports, 'assertions':len(assertions)}))
atexit.register(save)
'''


def native_count(command, log):
    count = test_count(command, log)
    if count is not None:
        return count
    for pattern in (r'Ran (\d+) tests?\b', r'Total tests:\s*(\d+)', r'Total:\s*(\d+)', r'(\d+) (?:tests?|runs?),\s*\d+ assertions?',
                    r'(\d+) examples?,\s*\d+ failures?', r'Tests:\s*(\d+)', r'out of (\d+)', r'OK \((\d+) tests?'):
        matches = re.findall(pattern, log)
        if matches:
            return int(matches[-1])
    if re.search(r'No tests (?:were found|found|ran)|no test files|0 passing', log, re.I):
        return 0
    if 'go test' in command:
        return len(re.findall(r'^--- PASS:', log, re.M))
    return None


def compiled_count(command, log):
    """Executed tests from a native compiled runner's OWN summary line, or None.

    native_count() also accepts generic phrases ("Total: 5", "out of 10") for reporting. As
    EVIDENCE that compiled tests ran, only the runner's format counts: `make` otherwise turned
    `@echo "Total: 5"` - or the application's own output - into five executed tests.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    runner = Path(words[0]).name if words else ''
    if runner == 'mvn':
        found = re.findall(r'Tests run: (\d+), Failures: \d+, Errors: \d+', log)
        return int(found[-1]) if found else None
    if runner == 'cargo':
        found = re.findall(r'test result: \w+\. (\d+) passed', log)
        return sum(map(int, found)) if found else None
    if runner == 'go':
        if re.search(r'^(?:ok|PASS|FAIL|---|===|\?)\s', log, re.M):
            return len(re.findall(r'^\s*--- PASS:', log, re.M))
        return None
    if runner == 'dotnet':
        found = (re.findall(r'^\s*(?:Passed|Failed)!\s+-\s+Failed:\s+\d+,\s+Passed:\s+(\d+)', log, re.M)
                 or re.findall(r'Test summary: total: \d+, failed: \d+, succeeded: (\d+)', log)      # .NET 9 terminal logger
                 or re.findall(r'Total tests:\s*(\d+)', log))
        return sum(map(int, found)) if found else None
    if runner == 'ctest':
        # `make` is deliberately absent: a Makefile can echo any summary line (EXECUTION_HELP: "a bare Makefile is not enough").
        found = re.findall(r'\d+% tests passed, \d+ tests failed out of (\d+)', log)
        return int(found[-1]) if found else None
    return None


def native_runner(command):
    """A printed test summary alone is insufficient for an arbitrary script."""
    words = shlex.split(command)
    if not words:
        return False
    first = Path(words[0]).name
    if first in {'python', 'python3'}:
        return len(words) > 2 and words[1] == '-m' and words[2] in {'pytest', 'unittest'}
    if first == 'node':
        return '--test' in words
    return first in {'pytest', 'vitest', 'jest', 'cargo', 'go', 'mvn', 'gradle', 'gradlew',
                     'dotnet', 'ctest', 'phpunit', 'bundle', 'composer', 'make'}


def report_count(root, after=0):
    """Native JUnit reports cover runners such as Gradle with terse stdout."""
    import xml.etree.ElementTree as ET
    reports = [p for p in [*root.glob('build/test-results/**/TEST-*.xml'), *root.glob('target/surefire-reports/TEST-*.xml')]
               if p.stat().st_mtime >= after]
    if not reports:
        return None
    count = 0
    for path in reports:
        try:
            tree = ET.parse(path)
            count += sum(1 for case in tree.iter('testcase') if case.find('skipped') is None)
        except (OSError, ET.ParseError):
            return None
    return count


def provenance(directory, root):
    imports, assertions = {}, 0
    for path in directory.glob('python-*.json'):
        # A child killed mid-write leaves a truncated file: lose its assertions (fail closed), not the whole check.
        try:
            data = json.loads(path.read_text())
            found, count = {p['path']: p for p in data['imports']}, int(data['assertions'])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        imports.update(found)
        assertions += count
    for path in directory.glob('coverage-*.json'):
        try:
            data = json.loads(path.read_text())
            for row in data.get('result', []):
                from urllib.parse import unquote, urlparse
                source = Path(unquote(urlparse(row['url']).path))
                if not source.is_file() or 'node_modules' in source.parts:
                    continue
                if source.is_relative_to(root):
                    imports[str(source.relative_to(root))] = {'path': str(source.relative_to(root)), 'sha256': file_hash(source)}
                elif not source.is_relative_to(root.parent / 'audit'):
                    continue
                # Native Node runners supply counts. Plain scripts need a real assert
                # inside an executed V8 range; console.assert is never sufficient.
                text = source.read_text(errors='replace')
                ranges = [r for fn in row.get('functions', []) for r in fn.get('ranges', [])]
                for match in re.finditer(r'(?<![\w.])assert(?:\.(?:equal|strictEqual|deepEqual|deepStrictEqual|ok|throws))?\s*\(', text):
                    containing = [r for r in ranges if r['startOffset'] <= match.start() < r['endOffset']]
                    if containing and min(containing, key=lambda r: r['endOffset'] - r['startOffset'])['count'] > 0:
                        assertions += 1
        except (ValueError, KeyError, OSError):
            continue
    return list(imports.values()), assertions


def run_checks(store, operation_id, repository, payload):
    from coder_worker_runtime import _boundary, changed_check_sources
    from coder_browser import browser_check
    from coder_evidence import attach
    operation = store.get(operation_id)
    revision = payload['revision_id']
    folder = store.root / 'checks' / operation['job_id'] / revision / operation_id
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / 'source.tar.gz'
    repository.archive(revision, archive)
    root, audit = folder / 'workspace', folder / 'audit'
    # Reconnection replays persisted operations; a new check operation gets a fresh copy.
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    audit.mkdir(exist_ok=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(root, filter='data')
    instrumentation = folder / 'instrumentation'
    instrumentation.mkdir(exist_ok=True)
    (instrumentation / 'sitecustomize.py').write_text(PYTHON_TRACE)
    profiles = {p['cwd']: p for p in payload.get('profiles', [])}
    environments = {}
    results = []
    for index, check in enumerate(payload.get('checks', [])):
        _boundary(store, operation_id)
        cwd = safe_relative(root, check.get('cwd', '.'))
        evidence_dir = folder / f'evidence-{index}'
        evidence_dir.mkdir(exist_ok=True)
        check_audit = audit / str(index)
        check_audit.mkdir(exist_ok=True)
        for name, content in check.get('files', {}).items():
            path = safe_relative(check_audit, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        started = time.time()
        timeout = min(payload['settings']['daedalus_command_seconds'], payload['seconds_remaining'] - (started - operation['started']))
        row = {**check, 'revision_id': revision, 'audit_dir': str(check_audit), 'passed': False}
        command = check.get('command', '')
        try:
            if check.get('setup'):
                profile = profiles.get(check.get('cwd', '.'), {'commands': {'setup': [check['command']]}})
                key, versions = environment_key(cwd, profile)
                cache = store.root / 'environments' / operation['job_id'] / key
                cache.mkdir(parents=True, exist_ok=True)
                environments[check.get('cwd', '.')] = (cache, versions)
                ready = cache / ('ready-' + hashlib.sha256(check['command'].encode()).hexdigest() + '.json')
                row['toolchains'] = versions
                if command.startswith('python3 -m venv .venv'):
                    if not (cache / '.venv').exists():
                        # Venv console-script shebangs must name the stable cache,
                        # never a disposable verification-copy path.
                        subprocess.run(['python3', '-m', 'venv', str(cache / '.venv')], check=True, timeout=timeout)
                    command = command.replace('python3 -m venv .venv', 'true', 1)
                # Only dependency directories are reused. Builds and results are not.
                for name in ('.venv', 'node_modules', 'vendor'):
                    if (cache / name).exists() and not (cwd / name).exists():
                        (cwd / name).symlink_to(cache / name, target_is_directory=True)
                if ready.exists() and '-e .' not in check['command'] and not check['command'].startswith(('cmake', 'dotnet')):
                    row.update(passed=True, environment_reused=True, exit_code=0)
            if not row['passed'] and check.get('kind') == 'file':
                path = safe_relative(root, check['path'])
                actual = file_hash(path) if path.is_file() else None
                passed = actual is not None
                for assertion in check.get('assertions', []):
                    if assertion['kind'] == 'unchanged':
                        old = repository.git('show', f"{payload['baseline_revision']}:{check['path']}")
                        passed &= actual == hashlib.sha256(old).hexdigest()
                    elif assertion['kind'] == 'contains':
                        passed &= path.is_file() and assertion['value'] in path.read_text()
                    elif assertion['kind'] == 'nonempty':
                        passed &= path.is_file() and path.stat().st_size > 0
                row.update(passed=bool(passed), source_bindings=[{'path': check['path'], 'sha256': actual}], coverage_observed=bool(passed))
            elif not row['passed'] and check.get('kind') == 'browser':
                browser = browser_check(cwd, check, evidence_dir, timeout, policy_version=6,
                    step_timeout=payload['settings']['daedalus_browser_step_seconds'],
                    startup_timeout=payload['settings']['daedalus_browser_startup_seconds'],
                    viewports=payload['settings']['daedalus_browser_viewports'],
                    emit=lambda action: store.event(operation_id, 'browser_action', action=action, revision_id=revision))
                attach(store, operation['job_id'], browser, revision)
                row.update(browser, coverage_observed=bool(check.get('steps')), execution_succeeded=browser.get('passed', False))
            elif not row['passed']:
                log = folder / f'check-{index}.log'
                environment = {k: v for k, v in os.environ.items() if k not in {
                    'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'PIP_TARGET', 'PIP_PREFIX', 'PIP_CONSTRAINT',
                    'PIP_REQUIRE_VIRTUALENV', 'PIP_CONFIG_FILE', 'npm_config_prefix', 'NPM_CONFIG_PREFIX',
                    'NPM_CONFIG_USERCONFIG', 'NPM_CONFIG_GLOBALCONFIG', 'GOFLAGS'}}
                environment.update(DAEDALUS_PROJECT_ROOT=str(root), DAEDALUS_AUDIT_DIR=str(check_audit),
                    DAEDALUS_PROVENANCE=str(evidence_dir), NODE_V8_COVERAGE=str(evidence_dir),
                    PYTHONPATH=os.pathsep.join([str(instrumentation), str(cwd), str(root)]),
                    PIP_CONFIG_FILE=os.devnull, PYTHONDONTWRITEBYTECODE='1',
                    PATH=os.pathsep.join([str(cwd / '.venv/bin'), str(root / '.venv/bin'), environment.get('PATH', '')]))
                if not check.get('is_test') and check.get('origin') != 'independent' and check.get('phase') != 'launch':
                    environment.pop('PYTHONPATH', None)
                    environment.pop('NODE_V8_COVERAGE', None)
                with log.open('wb') as output:
                    process = subprocess.Popen(['bash', '-c', command], cwd=cwd, env=environment,
                        stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        code = process.wait(timeout=max(0.001, timeout))
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL); process.wait(); code = -1
                    finally:
                        # Commands may not leave background preview servers behind.
                        try: os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                with log.open('rb') as source:
                    source.seek(max(0, log.stat().st_size - resolve('reviewer', payload['settings']).input_budget))
                    tail = source.read().decode(errors='replace')
                count = native_count(check.get('test_runner') or check['command'], tail)
                if count is None and check.get('is_test'):
                    count = report_count(cwd, started)
                bindings, assertions = provenance(evidence_dir, root)
                observed = assertions > 0 or (count is not None and count > 0 and
                    (native_runner(check.get('test_runner') or command) or bool(check.get('binary'))))
                row.update(exit_code=code, test_count=count, assertions_executed=assertions,
                    passed=code == 0 and count != 0 and 'Assertion failed' not in tail,
                    coverage_observed=observed, source_bindings=bindings,
                    execution_succeeded=code == 0 and (observed or check.get('phase') == 'launch'),
                    environment_fault=code == 127 or bool(check.get('setup') and re.search(
                        r'Temporary failure|EAI_AGAIN|ENOTFOUND|Connection refused|Network is unreachable|'
                        r'No CMAKE_\w+_COMPILER|Could not find compiler|NETSDK1045|No Java runtime present', tail, re.I)),
                    log=str(log), log_tail=tail[-4000:], log_bytes=log.stat().st_size)
                if check.get('is_test') and not observed:
                    row.update(passed=False, failure_kind='missing_coverage', summary='No executed tests or assertions were observed')
                declared = []
                for subject in check.get('subjects', []):
                    path = safe_relative(root, subject)
                    if path.is_file() and path.suffix not in {'.py', '.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx'}:
                        declared.append({'path':subject, 'sha256':file_hash(path)})
                if bindings:
                    row['provenance_mode'] = 'observed'
                elif declared:
                    row.update(source_bindings=declared, provenance_mode='declared_source')
                if check.get('origin') == 'independent' and 'behavior' in check.get('evidence_types', []) and not bindings and not declared and not check.get('binary'):
                    row.update(passed=False, coverage_observed=False, failure_kind='missing_provenance', summary='No project imports were observed; review the command or declare an actual binary')
                if check.get('binary'):
                    binary = safe_relative(cwd, check['binary'])
                    # Bind only a directly executed project binary, not an arbitrary quoted filename.
                    words = shlex_words(check['command'])
                    invoked = any(w in {check['binary'], './' + check['binary']} for w in words[:2])
                    row['binary_provenance'] = {'path': check['binary'], 'sha256': file_hash(binary)} if binary.is_file() and invoked else None
                    if not row['binary_provenance']:
                        row.update(passed=False, coverage_observed=False, failure_kind='missing_provenance')
            changed = changed_check_sources(archive, root, [*payload['settings']['daedalus_exclude_dirs'], 'vendor', 'obj', 'bin'])
            # The legacy detector also checks newly created source. Extend it to
            # all project languages without treating build products as source.
            with tarfile.open(archive) as bundle:
                members = {m.name for m in bundle}
            excluded = {*payload['settings']['daedalus_exclude_dirs'], 'vendor', 'obj', 'bin'}
            for directory, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in excluded and not d.endswith('.egg-info')]
                for name in files:
                    path = Path(directory) / name
                    if path.relative_to(root).as_posix() not in members and path.suffix in {'.cs', '.fs', '.c', '.cc', '.cpp', '.h', '.hpp', '.php', '.rb', '.swift', '.kt'}:
                        changed.append(path.relative_to(root).as_posix())
            if changed:
                row.update(passed=False, coverage_observed=False, failure_kind='source_mutation', paths=changed,
                           summary='Verification changed immutable project source')
            if row['passed'] and check.get('setup') and not row.get('environment_reused'):
                cache, _ = environments[check.get('cwd', '.')]
                for name in ('.venv', 'node_modules', 'vendor'):
                    path = cwd / name
                    if path.is_dir() and not path.is_symlink() and not (cache / name).exists():
                        # Copy environments: original shebang paths stay available for this run.
                        shutil.copytree(path, cache / name, symlinks=True)
                ready.write_text(json.dumps({'command': check['command']}))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            row.update(passed=False, error=str(error), environment_fault=isinstance(error, FileNotFoundError))
        row['seconds'] = time.time() - started
        row['execution_id'] = f'{operation_id}:{index}'
        results.append(row)
        (folder / f'result-{index}.json').write_text(json.dumps(row))
        store.event(operation_id, 'check', check=row)
        store.update(operation_id, result={'revision_id': revision, 'checks': results, 'passed': False})
        if row.get('environment_fault') or row.get('failure_kind') == 'source_mutation':
            break
    return {'revision_id': revision, 'checks': results, 'environment_fault': any(r.get('environment_fault') for r in results),
            'passed': bool(results) and all(r['passed'] for r in results)}


def shlex_words(command):
    return shlex.split(command)
