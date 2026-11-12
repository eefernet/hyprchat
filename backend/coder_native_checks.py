"""Ordinary native checks on disposable revision copies, with audit provenance."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex

from coder_repository import file_hash
from coder_verification import test_count

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


def provenance_diagnostic(directory, command, runner):
    """Explain unbound execution without admitting advisory URLs as source bindings."""
    jsdom = False
    unbound = set()
    for path in directory.glob('coverage-*.json'):
        try:
            data = json.loads(path.read_text())
            for row in data.get('result', []):
                executed = any(r.get('count', 0) > 0 for f in row.get('functions', []) for r in f.get('ranges', []))
                url = row.get('url', '')
                if not executed: continue
                jsdom |= '/node_modules/jsdom/' in url
                # Never expose query strings or local paths outside the project.
                if not url or url.startswith(('http:', 'https:', 'about:')):
                    unbound.add('anonymous' if not url else url.split(':', 1)[0] + ':')
        except (OSError, ValueError, TypeError):
            continue
    kind = 'unsupported_dynamic_document' if jsdom else 'application_execution_unbound'
    detail = ('JSDOM execution was observed, but inline document scripts are not bound to exact project bytes by this tracer. '
              'This does not establish that the test used copied logic or that the actual HTML was executed. '
              if jsdom else 'No executed application source or controller service traffic was bound to this check. ')
    return {'category': kind, 'command': command, 'selected_test_runner': runner or command,
            'jsdom_execution_observed': jsdom, 'unbound_script_kinds': sorted(unbound),
            'reason': detail + 'Run a browser test against the real application or import its source module. '
            'After adding a test, update and run the manifest default test command so it selects that test. '
            'Reading source, a copied implementation, or a passing assertion alone does not prove application behavior.'}
