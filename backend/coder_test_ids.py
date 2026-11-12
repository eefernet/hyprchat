"""Per-test identity from each runner's own report: which named tests passed, failed or were skipped.

A count says how many tests ran; an identity says WHICH. The required set (coder_required_tests) is
built from identities, so an edit cannot drop, skip or break a test that passed before by rewriting the
suite around it. Only a runner's own reporting counts:

* pytest and unittest: report hooks installed by the controller (``PYTEST_PLUGINS`` and sitecustomize).
* Maven / Gradle: their JUnit XML reports. .NET: console logger lines at normal verbosity.
* cargo, ``go test -v`` / ``-json``, ctest and ``node --test`` (TAP or spec): the per-test lines they print.

The parser is chosen from the test runner itself (an npm script's own text), never from the npm/npx
wrapper. Runners without per-test output (jest/vitest default reporters, make) give no identities; the
existing suite-level rules still apply to them. Names must be stable across revisions: suite and file
wrappers, doc-test line numbers and execution-copy paths are left out. Worker-shared: imports nothing
from the backend.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex

MAX_IDS = 2000
LOG_BYTES = 4_000_000
OUTCOMES = ('passed', 'failed', 'skipped')

# Loaded by pytest through PYTEST_PLUGINS (the instrumentation dir is on PYTHONPATH for test rows).
PYTEST_PLUGIN = r'''import json, os

def _write(nodeid, outcome):
    target = os.environ.get('DAEDALUS_PROVENANCE')
    if not target:
        return
    try:
        with open(os.path.join(target, 'test-ids-%d.jsonl' % os.getpid()), 'a') as stream:
            stream.write(json.dumps({'runner': 'pytest', 'id': nodeid, 'outcome': outcome}) + '\n')
    except OSError:
        pass

def pytest_runtest_logreport(report):
    if hasattr(report, 'wasxfail'):
        if report.when == 'call' or report.skipped:
            _write(report.nodeid, 'skipped')
        return
    if report.when == 'call' or (report.when == 'setup' and not report.passed):
        _write(report.nodeid, report.outcome)
    elif report.when == 'teardown' and report.failed:
        _write(report.nodeid, 'failed')
'''

# Appended to the controller's sitecustomize; active only when DAEDALUS_TEST_IDS is set (project test rows).
UNITTEST_RECORDER = r'''
def _daedalus_unittest_ids():
    import os
    if not os.environ.get('DAEDALUS_TEST_IDS') or not os.environ.get('DAEDALUS_PROVENANCE'):
        return
    try:
        import json
        import unittest.result as result_module
    except Exception:
        return
    def write(test, outcome):
        try:
            ident = test.id()
            with open(os.path.join(os.environ['DAEDALUS_PROVENANCE'], 'test-ids-%d.jsonl' % os.getpid()), 'a') as stream:
                stream.write(json.dumps({'runner': 'unittest', 'id': ident, 'outcome': outcome}) + '\n')
        except Exception:
            pass
    def wrap(name, outcome):
        original = getattr(result_module.TestResult, name)
        def method(self, test, *args):
            write(test, outcome)
            return original(self, test, *args)
        setattr(result_module.TestResult, name, method)
    for name, outcome in (('addSuccess', 'passed'), ('addFailure', 'failed'), ('addError', 'failed'), ('addSkip', 'skipped'),
                          ('addExpectedFailure', 'skipped'), ('addUnexpectedSuccess', 'failed')):
        wrap(name, outcome)
    original_subtest = result_module.TestResult.addSubTest
    def subtest(self, test, sub, err):
        if err is not None:
            write(test, 'failed')
        return original_subtest(self, test, sub, err)
    result_module.TestResult.addSubTest = subtest
_daedalus_unittest_ids()
'''


def merge(found, ident, outcome):
    """A failure anywhere wins over a pass of the same name; skips never hide a result."""
    ident = ' '.join(str(ident).split())
    if not ident or outcome not in OUTCOMES or (ident not in found and len(found) >= MAX_IDS):
        return
    previous = found.get(ident)
    if previous == 'failed' or previous == 'passed' and outcome == 'skipped':
        return
    found[ident] = outcome


def hook_records(evidence):
    found = {}
    for path in sorted(Path(evidence).glob('test-ids-*.jsonl')):
        try:
            lines = path.read_text(errors='replace').splitlines()
        except OSError:
            continue
        latest = {}
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue   # a child killed mid-write: lose that line, keep the rest
            if isinstance(record, dict) and record.get('id'):
                latest[str(record['id'])] = str(record.get('outcome'))   # a rerun plugin's last result wins
        for ident, outcome in latest.items():
            merge(found, ident, outcome)
    return found


def junit(root, after=0):
    """JUnit XML written by this command (Maven surefire, Gradle)."""
    import xml.etree.ElementTree as ET
    found = {}
    root = Path(root)
    for path in [*root.glob('target/surefire-reports/TEST-*.xml'), *root.glob('build/test-results/**/TEST-*.xml'),
                 *root.glob('*/target/surefire-reports/TEST-*.xml'), *root.glob('*/build/test-results/**/TEST-*.xml')]:
        try:
            if path.stat().st_mtime < after:
                continue
            tree = ET.parse(path)
        except (OSError, ET.ParseError):
            continue
        for case in tree.iter('testcase'):
            name = '.'.join(part for part in (case.get('classname'), case.get('name')) if part)
            outcome = ('failed' if case.find('failure') is not None or case.find('error') is not None
                       else 'skipped' if case.find('skipped') is not None else 'passed')
            merge(found, name, outcome)
    return found


def _runner(command):
    """The runner whose per-test lines the log holds; '' when it is unknown or prints none."""
    try:
        words = [Path(word).name for word in shlex.split(command)]
    except ValueError:
        return ''
    if 'node' in words:
        # Only the node test runner prints per-test lines; `node app.test.js` prints whatever the script does.
        return 'node' if '--test' in words else ''
    return next((word for word in words if word in {'cargo', 'go', 'ctest', 'dotnet'}), '')


def cargo(text):
    found, target = {}, ''
    for line in text.splitlines():
        running = re.match(r'\s*Running (?:unittests )?(\S+)', line)
        if running:
            target = running.group(1)
            continue
        doc = re.match(r'\s*Doc-tests (\S+)', line)
        if doc:
            target = 'doc:' + doc.group(1)
            continue
        test = re.match(r'test (.+?) \.\.\. (ok|FAILED|ignored)\b', line)
        if test:
            name = test.group(1)
            if target.startswith('doc:'):
                # `src/lib.rs - add (line 2)`: the line number moves with every edit above the example.
                name = re.sub(r' \(line \d+\)', '', name)
            merge(found, f'{target}::{name}' if target else name,
                  {'ok': 'passed', 'FAILED': 'failed', 'ignored': 'skipped'}[test.group(2)])
    return found


def go(text):
    found, pending = {}, []
    for line in text.splitlines():
        if line.startswith('{'):
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if isinstance(event, dict) and event.get('Test') and event.get('Action') in {'pass', 'fail', 'skip'}:
                merge(found, f"{event.get('Package', '')}::{event['Test']}",
                      {'pass': 'passed', 'fail': 'failed', 'skip': 'skipped'}[event['Action']])
            continue
        result = re.match(r'\s*--- (PASS|FAIL|SKIP): (\S+)', line)
        if result:
            pending.append((result.group(2), {'PASS': 'passed', 'FAIL': 'failed', 'SKIP': 'skipped'}[result.group(1)]))
            continue
        package = re.match(r'(?:ok|FAIL)\s+(\S+)\s', line + ' ')
        if package and pending and package.group(1) != 'FAIL':
            for name, outcome in pending:
                merge(found, f'{package.group(1)}::{name}', outcome)
            pending = []
    for name, outcome in pending:   # a package summary line never printed (crash, timeout)
        merge(found, name, outcome)
    return found


def ctest(text):
    found = {}
    for line in text.splitlines():
        test = re.match(r'\s*\d+/\d+ Test\s+#\d+: (\S+) \.*\s*(\*\*\*)?\s*(Passed|Not Run|Disabled|Skipped|\S+)', line)
        if test:
            status = test.group(3)
            # "Not Run" means the test executable is missing: a failure, not a skip.
            merge(found, test.group(1), 'passed' if status == 'Passed' else 'skipped' if status in {'Disabled', 'Skipped'} else 'failed')
    return found


def node(text, aggregates=None):
    """Leaf tests of TAP (``ok 1 - name``, nested ``# Subtest:``) and the spec reporter (``✔ name (1ms)``).

    A describe() suite, a t.test() parent or a file wrapper also reports a result: a new failing test
    inside an existing suite would turn that aggregate into a lost required test, so aggregates are not
    identities. They go to ``aggregates`` instead, so a required test that later gains subtests of its own
    is still present (coder_required_tests.regressions). An older spec reporter closes a suite with a bare
    ``▶ name (2ms)`` that carries no outcome: that one stays unknown.
    """
    found, stack, suites = {}, [], []
    aggregates = {} if aggregates is None else aggregates
    for line in text.splitlines():
        if line.strip().startswith('✖ failing tests:'):
            break   # the spec reporter repeats failures there without their suite names
        indent = len(line) - len(line.lstrip(' '))
        subtest = re.match(r'\s*# Subtest: (.+)$', line)
        if subtest:
            stack = [entry for entry in stack if entry[0] < indent] + [[indent, subtest.group(1).strip(), False]]
            continue
        tap = re.match(r'\s*(not ok|ok) \d+ - (.+?)(\s+#\s*(SKIP|TODO)\b.*)?$', line)
        if tap:
            own = next((entry for entry in stack if entry[0] == indent), None)
            for entry in stack:
                if entry[0] < indent:
                    entry[2] = True   # an enclosing subtest has results of its own: it is an aggregate
            parents = [entry[1] for entry in stack if entry[0] < indent]
            stack = [entry for entry in stack if entry[0] < indent]
            name = ' > '.join([*parents, tap.group(2).strip()])
            merge(aggregates if own and own[2] else found, name,
                  'skipped' if tap.group(4) else 'passed' if tap.group(1) == 'ok' else 'failed')
            continue
        suite = re.match(r'\s*▶ (.+?)(?: \([\d.]+m?s\))?$', line)
        if suite:
            suites = [(i, n) for i, n in suites if i < indent] + [(indent, suite.group(1).strip())]
            continue
        spec = re.match(r'\s*([✔✓✖✗﹣])\s+(.+?)(?: \([\d.]+m?s\))?(\s+# (SKIP|TODO).*)?$', line)
        if spec:
            closes = (indent, spec.group(2).strip()) in suites   # Node 23+ closes a suite with its own result line
            parents = [n for i, n in suites if i < indent]
            if closes:
                suites = [(i, n) for i, n in suites if i < indent]
            name = ' > '.join([*parents, spec.group(2).strip()])
            outcome = 'skipped' if spec.group(4) or spec.group(1) == '﹣' else 'passed' if spec.group(1) in '✔✓' else 'failed'
            merge(aggregates if closes else found, name, outcome)
    return found


def dotnet(text):
    found = {}
    for line in text.splitlines():
        test = re.match(r'\s*(Passed|Failed|Skipped) (\S.*?) \[[^\]]*\]\s*$', line)
        if test:
            merge(found, test.group(2), {'Passed': 'passed', 'Failed': 'failed', 'Skipped': 'skipped'}[test.group(1)])
    return found


PARSERS = {'cargo': cargo, 'go': go, 'ctest': ctest, 'node': node, 'dotnet': dotnet}


def stable(found, roots):
    """Names without the execution copy's absolute paths (a node file wrapper, a pytest id parametrized on a path)."""
    prefixes = sorted({str(path).rstrip('/') + '/' for root in roots if root for path in (Path(root), Path(root).resolve())},
                      key=len, reverse=True)
    clean = {}
    for ident, outcome in found.items():
        for prefix in prefixes:
            ident = ident.replace(prefix, '')
        merge(clean, ident[:300], outcome)
    return clean


def collect(command, log_path, cwd, evidence, started=0, root=None):
    """{'ids': {name: outcome}, 'source': which report produced them}; empty ids when the runner reports none.

    ``command`` is the test runner itself (an npm script's text), not the wrapper that starts it. Node suite
    and parent results come back apart as ``aggregates``: never identities, never required.
    """
    roots = (root, cwd)
    found = hook_records(evidence)
    if found:
        return {'ids': stable(found, roots), 'source': 'hooks'}
    found = junit(cwd, started)
    if found:
        return {'ids': stable(found, roots), 'source': 'junit'}
    try:
        with open(log_path, 'rb') as stream:
            text = stream.read(LOG_BYTES).decode(errors='replace')
    except OSError:
        text = ''
    parser, aggregates = PARSERS.get(_runner(command)), {}
    found = stable((node(text, aggregates) if parser is node else parser(text)) if parser else {}, roots)
    result = {'ids': found, 'source': _runner(command) if found else ''}
    if found and aggregates:
        result['aggregates'] = stable(aggregates, roots)
    return result
