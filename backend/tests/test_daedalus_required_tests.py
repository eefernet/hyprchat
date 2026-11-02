"""Required tests: per-test identities from runner output and the rule that a passing test stays passing (offline).

Plan 2026-10-26 [R5]: tests are tracked by the id the runner itself reports, the required set only grows,
a newly failing required test is re-run once, and a revision that loses one is rolled back to the last
revision that kept them all.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import coder_test_ids as ids
from coder_required_tests import failing_parents, file_of, grow, judged, passing, regressions, rerun_packages


def row(cwd='.', passed=True, found=None, **extra):
    return {'id': f'test:{cwd}', 'cwd': cwd, 'phase': 'test', 'is_test': True, 'origin': 'project', 'passed': passed,
            'test_ids': {'ids': found or {}, 'source': 'hooks'}, **extra}


def test_cargo_names_are_qualified_by_their_test_target():
    log = '''   Compiling calc v0.1.0
     Running unittests src/lib.rs (target/debug/deps/calc-1a2b3c)
test tests::adds ... ok
test tests::divides ... FAILED
test tests::slow ... ignored
     Running tests/cli.rs (target/debug/deps/cli-9f8e7d)
test adds ... ok
   Doc-tests calc
test src/lib.rs - add (line 3) ... ok
'''
    assert ids.cargo(log) == {'src/lib.rs::tests::adds': 'passed', 'src/lib.rs::tests::divides': 'failed',
                              'src/lib.rs::tests::slow': 'skipped', 'tests/cli.rs::adds': 'passed',
                              'doc:calc::src/lib.rs - add': 'passed'}
    # A doc example's line number moves with every edit above it: the same test before and after an edit.
    moved = log.replace('(line 3)', '(line 9)')
    assert ids.cargo(moved) == ids.cargo(log)


def test_go_verbose_and_json_output_name_tests_by_package():
    verbose = '=== RUN   TestAdd\n--- PASS: TestAdd (0.00s)\n=== RUN   TestSub\n    --- FAIL: TestSub/neg (0.00s)\n--- FAIL: TestSub (0.00s)\nFAIL\nFAIL\texample.com/calc\t0.01s\n'
    assert ids.go(verbose) == {'example.com/calc::TestAdd': 'passed', 'example.com/calc::TestSub/neg': 'failed',
                               'example.com/calc::TestSub': 'failed'}
    stream = '\n'.join(json.dumps(e) for e in [{'Action': 'run', 'Package': 'p', 'Test': 'TestA'},
                                                 {'Action': 'pass', 'Package': 'p', 'Test': 'TestA'},
                                                 {'Action': 'skip', 'Package': 'p', 'Test': 'TestB'}])
    assert ids.go(stream) == {'p::TestA': 'passed', 'p::TestB': 'skipped'}


def test_ctest_and_dotnet_lines():
    log = ('    Start 1: adds\n1/3 Test #1: adds .............................   Passed    0.00 sec\n'
           '2/3 Test #2: divides ..........................***Failed    0.01 sec\n'
           '3/3 Test #3: missing ..........................***Not Run   0.00 sec\n')
    assert ids.ctest(log) == {'adds': 'passed', 'divides': 'failed', 'missing': 'failed'}
    console = '  Passed Calc.Tests.AddsTwoNumbers [2 ms]\n  Failed Calc.Tests.Divides [1 ms]\n  Skipped Calc.Tests.Later [1 ms]\n'
    assert ids.dotnet(console) == {'Calc.Tests.AddsTwoNumbers': 'passed', 'Calc.Tests.Divides': 'failed', 'Calc.Tests.Later': 'skipped'}


def test_node_tap_and_spec_reporters_keep_suite_names():
    tap = '''TAP version 13
# Subtest: math
    # Subtest: adds
    ok 1 - adds
    # Subtest: divides
    not ok 2 - divides
    # Subtest: later
    ok 3 - later # SKIP
not ok 1 - math
# Subtest: top level
ok 2 - top level
'''
    # The describe() suite reports an aggregate result of its own; only leaf tests are identities.
    assert ids.node(tap) == {'math > adds': 'passed', 'math > divides': 'failed', 'math > later': 'skipped',
                             'top level': 'passed'}
    spec = '''▶ math
  ✔ adds (0.5ms)
  ✖ divides (1.2ms)
▶ math (2ms)
✔ top level (0.1ms)
ℹ tests 3
✖ failing tests:

✖ divides (1.2ms)
'''
    assert ids.node(spec) == {'math > adds': 'passed', 'math > divides': 'failed', 'top level': 'passed'}
    # Node 23+ closes a suite with its own result line instead of a second `▶ name (2ms)`.
    closing = spec.replace('▶ math (2ms)', '✖ math (2ms)')
    assert ids.node(closing) == ids.node(spec)


def test_a_new_failing_test_inside_an_existing_suite_is_not_a_lost_test():
    before = '''# Subtest: parser
    # Subtest: parses numbers
    ok 1 - parses numbers
ok 1 - parser
'''
    after = '''# Subtest: parser
    # Subtest: parses numbers
    ok 1 - parses numbers
    # Subtest: parses hex
    not ok 2 - parses hex
not ok 1 - parser
'''
    required = passing([row(found=ids.node(before))])
    assert required == ['.::parser > parses numbers']
    assert regressions(required, [row(passed=False, found=ids.node(after))]) == []


def test_the_parser_follows_the_real_runner_not_the_npm_wrapper():
    jest = 'PASS src/sum.test.js\n  ✓ adds two numbers (2 ms)\n  ✓ handles negatives\n'
    assert ids._runner('jest') == '' and ids._runner('npm run test') == ''
    assert ids._runner('node --test tests/*.test.js') == 'node' and ids._runner('node app.test.js') == ''
    # A Jest or vitest run has no per-test identities here; its durations would make every name unstable.
    log = Path(__file__).parent / '_jest.log'
    try:
        log.write_text(jest)
        assert ids.collect('jest', log, Path(__file__).parent, Path(__file__).parent / 'missing-evidence')['ids'] == {}
    finally:
        log.unlink()


def test_names_never_carry_the_execution_copy_path(tmp_path):
    first, second = tmp_path / 'op-1' / 'project', tmp_path / 'op-2' / 'project'
    def recorded(root):
        evidence = root.parent / 'evidence'; evidence.mkdir(parents=True)
        (evidence / 'test-ids-1.jsonl').write_text(json.dumps({'runner': 'pytest', 'outcome': 'passed',
            'id': f'tests/test_x.py::test_file[{root}/tests/data.txt]'}) + '\n')
        return ids.collect('pytest', root.parent / 'none.log', root, evidence, root=root)['ids']
    assert recorded(first) == recorded(second) == {'tests/test_x.py::test_file[tests/data.txt]': 'passed'}


def test_junit_reports_written_by_this_command(tmp_path):
    reports = tmp_path / 'target' / 'surefire-reports'; reports.mkdir(parents=True)
    (reports / 'TEST-AppTest.xml').write_text('''<testsuite><testcase classname="app.AppTest" name="greets"/>
<testcase classname="app.AppTest" name="fails"><failure message="x"/></testcase>
<testcase classname="app.AppTest" name="later"><skipped/></testcase></testsuite>''')
    assert ids.junit(tmp_path) == {'app.AppTest.greets': 'passed', 'app.AppTest.fails': 'failed', 'app.AppTest.later': 'skipped'}
    assert ids.junit(tmp_path, after=os.path.getmtime(reports / 'TEST-AppTest.xml') + 10) == {}


def run_with_hooks(tmp_path, args):
    """Run a real Python test command with the controller's instrumentation, as run_revision does."""
    instrumentation = tmp_path / 'instrumentation'; instrumentation.mkdir()
    (instrumentation / 'sitecustomize.py').write_text(ids.UNITTEST_RECORDER)
    (instrumentation / 'daedalus_test_ids.py').write_text(ids.PYTEST_PLUGIN)
    evidence = tmp_path / 'evidence'; evidence.mkdir()
    env = {**os.environ, 'PYTHONPATH': str(instrumentation), 'DAEDALUS_PROVENANCE': str(evidence),
           'DAEDALUS_TEST_IDS': '1', 'PYTEST_PLUGINS': 'daedalus_test_ids', 'PYTHONDONTWRITEBYTECODE': '1'}
    log = tmp_path / 'command.log'
    with open(log, 'wb') as stream:
        subprocess.run([sys.executable, *args], cwd=tmp_path / 'project', env=env, stdout=stream, stderr=subprocess.STDOUT)
    return ids.collect(' '.join(['python3', *args]), log, tmp_path / 'project', evidence)


def project(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    (root / 'test_calc.py').write_text('import unittest\n\nclass Calc(unittest.TestCase):\n'
        '    def test_adds(self):\n        self.assertEqual(1 + 1, 2)\n'
        '    def test_breaks(self):\n        self.assertEqual(1 + 1, 3)\n'
        '    @unittest.skip("later")\n    def test_later(self):\n        pass\n'
        '    def test_parts(self):\n        for n in (1, 2):\n            with self.subTest(n=n):\n                self.assertLess(n, 2)\n')
    return root


def test_unittest_results_are_recorded_by_the_sitecustomize_hook(tmp_path):
    project(tmp_path)
    found = run_with_hooks(tmp_path, ['-m', 'unittest', '-v', 'test_calc'])
    assert found['source'] == 'hooks'
    assert found['ids'] == {'test_calc.Calc.test_adds': 'passed', 'test_calc.Calc.test_breaks': 'failed',
                            'test_calc.Calc.test_later': 'skipped', 'test_calc.Calc.test_parts': 'failed'}


def test_pytest_results_are_recorded_by_the_plugin(tmp_path):
    pytest.importorskip('pytest')
    project(tmp_path)
    found = run_with_hooks(tmp_path, ['-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_calc.py'])
    assert found['ids']['test_calc.py::Calc::test_adds'] == 'passed'
    assert found['ids']['test_calc.py::Calc::test_breaks'] == 'failed'
    assert found['ids']['test_calc.py::Calc::test_later'] == 'skipped'


def test_required_tests_must_keep_passing():
    before = [row(found={'a': 'passed', 'b': 'passed', 'c': 'failed'})]
    required = passing(before)
    assert required == ['.::a', '.::b']   # a test that never passed is not required
    assert regressions(required, [row(found={'a': 'passed', 'b': 'passed', 'new': 'passed'})]) == []
    assert regressions(required, [row(passed=False, found={'a': 'passed', 'b': 'failed'})]) == [{'id': '.::b', 'status': 'failed'}]
    assert regressions(required, [row(found={'a': 'passed', 'b': 'skipped'})]) == [{'id': '.::b', 'status': 'skipped'}]
    # Rewriting the suite around a test removes it: that is a lost test, not a renamed row.
    assert regressions(required, [row(found={'a': 'passed', 'b2': 'passed'})]) == [{'id': '.::b', 'status': 'missing'}]
    # A failing run that names no tests (a compile error in the suite) loses every required test of that package.
    assert {r['status'] for r in regressions(required, [row(passed=False)])} == {'failed'}
    assert rerun_packages(regressions(required, [row(passed=False)])) == ['.']
    # A package whose build failed never ran its tests: the build gate owns that failure.
    build_failed = {'id': 'build:.', 'cwd': '.', 'phase': 'build', 'origin': 'project', 'passed': False}
    assert regressions(required, [build_failed, row(cwd='web', found={'x': 'passed'})]) == []
    assert regressions(required, [{'id': 'launch:app', 'phase': 'launch', 'passed': False}]) == []
    # A deleted suite, a moved package or a removed test command is a lost test, not a skipped package.
    assert regressions(required, [row(cwd='web', found={'x': 'passed'})]) == [
        {'id': '.::a', 'status': 'missing'}, {'id': '.::b', 'status': 'missing'}]
    # A test command narrowed to a script that reports no names loses them too.
    assert {r['status'] for r in regressions(required, [row()])} == {'missing'}
    assert not judged(required, [row(cwd='web')]) and judged(required, before)
    assert regressions(required, [row(found={'a': 'passed'})], superseded=['.::b']) == []
    assert grow(required, [row(found={'a': 'passed', 'b': 'passed', 'new': 'passed'})]) == ['.::a', '.::b', '.::new']


def test_unreplaced_skeleton_placeholders_are_never_required():
    placeholder = row(found={'placeholder': 'passed'}, test_files=[{'path': 'tests/test_app.py', 'sha256': 'skeleton'}])
    assert passing([placeholder], {'skeleton'}) == []
    assert passing([{**placeholder, 'test_files': [{'path': 'tests/test_app.py', 'sha256': 'real'}]}], {'skeleton'}) == ['.::placeholder']
    # A row that runs the unchanged placeholder beside a real test file grows nothing either.
    mixed = {**placeholder, 'test_files': [{'path': 'tests/api.test.js', 'sha256': 'skeleton'}, {'path': 'tests/tasks.test.js', 'sha256': 'new'}]}
    assert passing([mixed], {'skeleton'}) == []


def test_test_files_come_from_ids_that_name_them():
    assert file_of('.::tests/test_calc.py::test_adds') == 'tests/test_calc.py'
    assert file_of('api::tests/test_api.py::test_list') == 'api/tests/test_api.py'
    assert file_of('.::test_calc.Calc.test_adds') == ''


def test_only_the_request_can_supersede_an_inherited_test():
    from coder_policy7_ops import authorizes_test_changes, superseded_tests
    assert authorizes_test_changes('Change the greeting to Hi and update the tests')
    assert not authorizes_test_changes('Add a --json flag; do not modify the tests')
    job = {'task': 'Add a --json flag', 'original_task': 'Add a --json flag', 'baseline_required': ['.::t.py::a'],
           'baseline_checks': [{'test_files': [{'path': 't.py'}]}], 'baseline_test_files': ['t.py']}
    assert superseded_tests(job, ['.::t.py::a', '.::t.py::new']) == []
    assert superseded_tests({**job, 'baseline_test_files': []}, ['.::t.py::a']) == ['.::t.py::a']
    changed = {**job, 'task': 'Say Hi instead and update the tests', 'original_task': 'Say Hi instead and update the tests'}
    assert superseded_tests(changed, ['.::t.py::a', '.::t.py::new']) == ['.::t.py::a']   # tests passing during this job stay


def check_operation(tmp_path, monkeypatch, job, rows_by_attempt):
    """execute_operation('check') with run_revision replaced by recorded rows (offline)."""
    import coder_project_runtime
    from coder_policy7_ops import execute_operation
    from coder_repository import Repository
    root = tmp_path / 'source'; root.mkdir()
    (root / 'app.py').write_text('VALUE = 0\n')
    repository = Repository(root, tmp_path / 'revisions')
    older = repository.snapshot()['revision']   # a repair checkpoint from before the last eligible revision
    (root / 'app.py').write_text('VALUE = 1\n')
    eligible = repository.snapshot(parent=older)['revision']
    job = {key: older if value == 'OLDER' else value for key, value in job.items()}
    (root / 'app.py').write_text('VALUE = 2\n')
    current = repository.snapshot(parent=eligible)['revision']
    repository.refresh()
    workspace = tmp_path / 'workspace'; workspace.mkdir(exist_ok=True)
    calls = []
    def run_revision(store, op_id, repo, profile, *, project_id, audit_source=None, checks=None, attempt=''):
        calls.append(attempt)
        rows = [{**r, 'revision_id': current} for r in rows_by_attempt[attempt]]
        return {'revision_id': current, 'checks': rows, 'workspace': str(workspace), 'passed': all(r['passed'] for r in rows)}
    monkeypatch.setattr(coder_project_runtime, 'run_revision', run_revision)
    monkeypatch.setattr(coder_project_runtime, 'contract', lambda repo, explicit=None: {
        'profiles': [], 'services': [], 'checks': [{'id': 'test:.', 'phase': 'test', 'is_test': True, 'cwd': '.', 'command': 'pytest'}]})
    events = []
    payload = {'revision_id': current, 'original_task': 'Add a flag', 'project_id': 'p', 'settings': {'daedalus_exclude_dirs': ['.git']},
               'policy7_job': {'brief': {'outcomes': []}, 'eligible_revision': eligible, **job}}
    store = SimpleNamespace(root=tmp_path / 'store', get=lambda op: {'payload': payload, 'kind': 'check', 'job_id': 'job'},
                            event=lambda op, kind, **data: events.append((kind, data)))
    return execute_operation(store, 'check-op', repository), calls, events, eligible, repository


def test_losing_a_required_test_restores_the_last_eligible_revision(tmp_path, monkeypatch):
    lost = [row(passed=False, found={'a': 'passed', 'b': 'failed'})]
    result, calls, events, eligible, repository = check_operation(tmp_path, monkeypatch, {'required_tests': ['.::a', '.::b']},
                                                                  {'': lost, 'rerun': lost})
    assert calls == ['', 'rerun']   # one re-run before a failing required test counts
    assert result['restored_snapshot']['revision'] == eligible
    assert result['repair_regression']['regressed'] == ['test:.::b']
    assert result['repair_regression']['lost_tests'] == [{'id': '.::b', 'status': 'failed'}]
    assert (repository.root / 'app.py').read_text() == 'VALUE = 1\n'


def test_a_flaky_required_test_that_passes_again_is_not_lost(tmp_path, monkeypatch):
    first = [row(passed=False, found={'a': 'passed', 'b': 'failed'})]
    again = [row(found={'a': 'passed', 'b': 'passed', 'c': 'passed'})]
    result, calls, events, _, repository = check_operation(tmp_path, monkeypatch, {'required_tests': ['.::a', '.::b']},
                                                           {'': first, 'rerun': again})
    assert 'restored_snapshot' not in result and result['eligible']
    assert result['checks'][0]['flaky'] and result['checks'][0]['first_attempt']['exit_code'] is None
    assert result['required_tests'] == ['.::a', '.::b', '.::c']
    assert ('required_tests_rerun', {'packages': ['.'], 'recovered': ['test:.']}) in events
    assert (repository.root / 'app.py').read_text() == 'VALUE = 2\n'


def test_a_new_failing_test_is_not_a_regression(tmp_path, monkeypatch):
    rows = [row(passed=False, found={'a': 'passed', 'new': 'failed'})]
    result, calls, _, _, _ = check_operation(tmp_path, monkeypatch, {'required_tests': ['.::a']}, {'': rows})
    assert calls == [''] and 'restored_snapshot' not in result
    assert result['eligible'] and result['required_tests'] == ['.::a']


def test_a_restored_revision_says_which_tests_it_protected():
    from coder_policy7_controller import regression_entries
    entries = regression_entries({'failed_revision': 'f' * 40, 'regressed': ['test:.::tests/test_calc.py::test_adds'],
                                  'lost_tests': [{'id': '.::tests/test_calc.py::test_adds', 'status': 'missing'}],
                                  'checks': [row(passed=False, log_tail='E   assert 3 == 2')]})
    assert entries[0]['priority'] and 'the test tests/test_calc.py::test_adds disappear' in entries[0]['reason']
    assert entries[0]['log_tail'] == 'E   assert 3 == 2'


@pytest.mark.skipif(not __import__('shutil').which('bwrap'), reason='Linux sandbox: the real check operation (Codebox)')
def test_the_real_check_operation_records_unittest_ids_and_rolls_back_a_lost_test(tmp_path):
    import time
    from tests.test_daedalus_policy7 import fixture
    from coder_policy7_ops import execute_operation
    tests = ('import unittest\nimport app\n\nclass T(unittest.TestCase):\n'
             '    def test_add(self):\n        self.assertEqual(app.add(2, 3), 5)\n'
             '    def test_sub(self):\n        self.assertEqual(app.sub(5, 3), 2)\n')
    repo, store = fixture(tmp_path, {'app.py': 'def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n',
                                     'test_app.py': tests})
    eligible = store.get('check')['payload']['revision_id']
    commands = {'packages': {'.': {'test': 'python3 -m unittest -v test_app'}}}

    def check(name, revision, job):
        payload = {**store.get('check')['payload'], 'revision_id': revision, 'original_task': 'Add mul', 'project_id': 'p',
                   'policy7_job': {'brief': {'outcomes': []}, 'execution_commands': commands, **job}}
        store.create(name, 'job', 'check', payload)
        store.update(name, started=time.time(), status='running')
        return execute_operation(store, name, repo)

    first = check('first', eligible, {})
    row = next(c for c in first['checks'] if c.get('is_test'))
    assert row['test_ids']['ids'] == {'test_app.T.test_add': 'passed', 'test_app.T.test_sub': 'passed'}, row.get('log_tail')
    assert first['eligible'] and first['required_tests'] == ['.::test_app.T.test_add', '.::test_app.T.test_sub']
    # The next change breaks sub: one re-run, then the revision is rolled back to the last one that kept it.
    (repo.root / 'app.py').write_text('def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return b - a\n')
    broken = repo.snapshot(parent=eligible)['revision']
    second = check('second', broken, {'required_tests': first['required_tests'], 'eligible_revision': eligible})
    assert second['repair_regression']['lost_tests'] == [{'id': '.::test_app.T.test_sub', 'status': 'failed'}]
    assert second['restored_snapshot']['revision'] == eligible
    assert 'return a - b' in (repo.root / 'app.py').read_text()
    assert (store.root / 'execution' / 'second-rerun').is_dir()


def runtime_fixture(tmp_path, monkeypatch, files, failing):
    """run_revision on a real repository with execute() faked: commands matching ``failing`` exit 1."""
    import coder_project_runtime as runtime
    from tests.test_daedalus_policy7 import fixture
    repo, store = fixture(tmp_path, files)
    def execute(store, op, command, cwd, env, log, **kwargs):
        log.write_text('✔ adds (0.5ms)\n')   # the node spec reporter's line for one passing test
        return 1 if failing(command, Path(cwd)) else 0
    monkeypatch.setattr(runtime, 'execute', execute)
    return runtime.run_revision(store, 'check', repo, runtime.contract(repo), project_id='p')


def node_package(test='node --test tests/*.test.js'):
    return {'package.json': json.dumps({'scripts': {'test': test}}), 'tests/a.test.js': 'x'}


def test_a_subpackage_whose_setup_failed_is_excused_and_only_that_one(tmp_path, monkeypatch):
    # review2 #7: the setup row had no cwd, so mdtoc's failed `npm install` read as the root's and every
    # mdtoc required test was "missing": a correct revision rolled back, the real setup error never shown.
    files = {**{'mdtoc/' + k: v for k, v in node_package().items()}, **{'web/' + k: v for k, v in node_package().items()}}
    result = runtime_fixture(tmp_path, monkeypatch, files, lambda command, cwd: command.startswith('npm') and cwd.name == 'mdtoc')
    setup = next(r for r in result['checks'] if r['id'] == 'mdtoc:setup:0')
    assert not setup['passed'] and setup['cwd'] == 'mdtoc' and setup['origin'] == 'project'
    assert result['untested'] == ['mdtoc']
    required = ['mdtoc::adds', 'web::adds', 'web::gone']
    assert regressions(required, result['checks'], untested=result['untested']) == [{'id': 'web::gone', 'status': 'missing'}]
    assert regressions(required, result['checks']) == [{'id': 'web::gone', 'status': 'missing'}]   # the rows agree now


def test_an_invalid_execution_config_skips_the_tests_without_losing_them(tmp_path, monkeypatch):
    # review2 #7: `{"test": ...}` at the top level fails validation; the execution-config row (no phase)
    # stopped '.' from running tests, and every '.' required test was rolled back as "missing".
    files = {**node_package(), '.daedalus.json': json.dumps({'test': 'npm test'})}
    result = runtime_fixture(tmp_path, monkeypatch, files, lambda command, cwd: command == 'exit 1')
    assert any(r['id'] == 'execution-config' and not r['passed'] for r in result['checks'])
    assert '.' in result['untested'] and not any(r.get('is_test') for r in result['checks'])
    assert regressions(['.::adds'], result['checks'], untested=result['untested']) == []
    assert regressions(['.::adds'], result['checks']) == []


def test_an_invalid_execution_config_excuses_every_package_it_configured(tmp_path, monkeypatch):
    # review3 #5: an invalid config voids every package's .daedalus.json commands, but only '.' was untested, so a
    # package whose test came only from the config (`api`: no test script; `tool`: no manifest at all) lost its
    # required tests as "missing" and the revision was rolled back over a config error the builder never saw.
    from coder_policy7_evidence import blocking
    from coder_policy7_phases import layer
    api = {'api/package.json': json.dumps({'scripts': {'start': 'node server.js'}}), 'api/tests/a.test.js': 'x', 'api/server.js': 'x'}
    tool = {'tool/tests/a.test.js': 'x', 'tool/cli.js': 'x'}
    for files, name in ((api, 'api'), (tool, 'tool')):
        def run(config, label):
            (tmp_path / label).mkdir()
            return runtime_fixture(tmp_path / label, monkeypatch, {**files, '.daedalus.json': config}, lambda command, cwd: command == 'exit 1')
        valid = json.dumps({'packages': {name: {'test': 'node --test tests/a.test.js'}}})
        good = run(valid, name)
        required = passing(good['checks'])
        assert required == [name + '::adds'] and good['untested'] == []
        for label, config in (('key', valid[:-1] + ', "test": "npm test"}'), ('comma', valid[:-1] + ',}')):
            bad = run(config, f'{name}-{label}')
            row = next(r for r in bad['checks'] if r['id'] == 'execution-config')
            assert not row['passed'] and row['cwd'] == '.' and row['reason'].startswith('Invalid .daedalus.json')
            assert blocking(row) and layer(row) == 1   # the controller's prerequisite path, not a rollback
            assert '*' in bad['untested'] and not judged(required, bad['checks'])
            assert regressions(required, bad['checks'], untested=bad['untested']) == []
            assert regressions(required, bad['checks']) == []   # the rows agree


def test_an_invalid_execution_config_is_never_eligible_and_never_rolled_back(tmp_path, monkeypatch):
    config = {'id': 'execution-config', 'cwd': '.', 'command': 'exit 1', 'origin': 'project', 'passed': False}
    result, calls, _, _, repository = check_operation(tmp_path, monkeypatch, {'required_tests': ['api::adds']}, {'': [config]})
    assert calls == [''] and 'restored_snapshot' not in result
    assert result['eligible'] is False and result['required_tests'] == ['api::adds']
    assert (repository.root / 'app.py').read_text() == 'VALUE = 2\n'


def test_probe_copy_guard_rows_never_excuse_a_deleted_suite():
    # review2 #9: a failed workflow:/interface:labels: row (disposable probe copy, phase launch) read as a
    # service that did not start, so a deleted suite in the same revision went unnoticed.
    required = ['.::tests/test_app.py::test_add']
    lost = [{'id': required[0], 'status': 'missing'}]
    base = [{'id': '.:build:0', 'cwd': '.', 'phase': 'build', 'origin': 'project', 'passed': True},
            {'id': 'launch:app', 'cwd': '.', 'phase': 'launch', 'passed': True}]
    for guard in ({'id': 'workflow:app:delete', 'origin': 'project'}, {'id': 'interface:labels:app', 'origin': 'controller'},
                  {'id': 'form:app', 'origin': 'project'}, {'id': 'page:app', 'origin': 'project'}):
        checks = [*base, {**guard, 'cwd': '.', 'phase': 'launch', 'passed': False, 'probe_context': 'disposable'}]
        assert regressions(required, checks) == lost, guard
        assert regressions(required, checks, untested=[]) == lost, guard
        assert regressions(required, [*base, {**guard, 'cwd': '.', 'phase': 'launch', 'passed': False}]) == lost, guard
    # A CLI launch command only stops its own package's tests; a main-copy service stops every package's.
    cli = {'id': 'b:launch', 'cwd': 'b', 'phase': 'launch', 'origin': 'project', 'command': 'npm run start', 'passed': False}
    assert regressions(['a::t', 'b::t'], [row(cwd='x'), cli]) == [{'id': 'a::t', 'status': 'missing'}]
    assert regressions(['a::t', 'b::t'], [row(cwd='x'), cli], untested=['b']) == [{'id': 'a::t', 'status': 'missing'}]
    assert regressions(required, [base[0], {**base[1], 'passed': False}]) == []
    assert regressions(required, [base[0]], untested=['*']) == []


def test_a_package_script_wrapper_resolves_to_the_runner_it_starts(tmp_path):
    # review2 #8: `npm test` from a .daedalus.json override (or a script calling another script) named no
    # runner, its log gave no ids, and every required test of a correct revision was rolled back as "missing".
    from coder_profiles import discover
    from coder_repository import Repository
    spec = '✔ adds (0.5ms)\n✔ subtracts (0.1ms)\nℹ tests 2\n'
    def runner_and_ids(files):
        root = tmp_path / f'p{len(list(tmp_path.iterdir()))}'
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True); (root / name).write_text(text)
        test = next(c for c in discover(Repository(root, root.parent / (root.name + '-revisions')))['checks'] if c.get('is_test'))
        (root.parent / 'test.log').write_text(spec)
        found = ids.collect(test['test_runner'], root.parent / 'test.log', root, root.parent / 'no-evidence')['ids']
        return test, [row(passed=True, found=found)]
    test, checks = runner_and_ids(node_package())
    assert test['command'] == 'npm run test' and test['test_runner'] == 'node --test tests/*.test.js'
    required = passing(checks)
    assert required == ['.::adds', '.::subtracts']
    override = {**node_package(), '.daedalus.json': json.dumps({'packages': {'.': {'test': 'npm test'}}})}
    test, checks = runner_and_ids(override)
    assert test['command'] == 'npm test' and test['test_runner'] == 'node --test tests/*.test.js'
    assert regressions(required, checks) == []
    nested = {'package.json': json.dumps({'scripts': {'test': 'npm run test:unit', 'test:unit': 'node --test tests/*.test.js'}}),
              'tests/a.test.js': 'x'}
    test, checks = runner_and_ids(nested)
    assert test['test_runner'] == 'node --test tests/*.test.js' and regressions(required, checks) == []
    # A wrapper whose final script really prints no names (jest) still loses them.
    jest = {'package.json': json.dumps({'scripts': {'test': 'npm run unit', 'unit': 'jest'}}), 'tests/a.test.js': 'x'}
    test, checks = runner_and_ids(jest)
    assert test['test_runner'] == 'jest' and {r['status'] for r in regressions(required, checks)} == {'missing'}


# Real Node 25.8 output: `totals` as a leaf, then with a t.test() child, then as a describe() of the same name.
LEAF_SPEC = '✔ totals (0.64ms)\n✔ other (0.06ms)\nℹ tests 2\n'
PARENT_SPEC = '▶ totals\n  ✔ empty cart (0.44ms)\n✔ totals (0.97ms)\n✔ other (0.05ms)\nℹ tests 3\n'
PARENT_TAP = '''TAP version 13
# Subtest: totals
    # Subtest: empty cart
    ok 1 - empty cart
      ---
      duration_ms: 0.454417
      type: 'test'
      ...
    1..1
ok 1 - totals
  ---
  duration_ms: 0.978625
  type: 'test'
  ...
# Subtest: other
ok 2 - other
1..2
'''
CHILD_FAILS_SPEC = '▶ totals\n  ✖ empty cart (1.04ms)\n✖ totals (1.63ms)\n\n✖ failing tests:\n\n✖ empty cart (1.04ms)\n'
CHILD_FAILS_TAP = '# Subtest: totals\n    # Subtest: empty cart\n    not ok 1 - empty cart\nnot ok 1 - totals\n'
OWN_BODY_FAILS_SPEC = '▶ totals\n  ✔ empty cart (0.42ms)\n✖ totals (1.59ms)\n\n✖ failing tests:\n\n✖ totals (1.59ms)\n'
OWN_BODY_FAILS_TAP = '# Subtest: totals\n    # Subtest: empty cart\n    ok 1 - empty cart\nnot ok 1 - totals\n'
# Real Node 25.8 output (trimmed): one bug fails the parent's own assertion AND its new subtest.
BOTH_FAIL_SPEC = ('▶ totals\n  ✖ empty cart (0.97ms)\n✖ totals (1.59ms)\n✔ other (0.10ms)\nℹ tests 3\nℹ fail 2\n\n✖ failing tests:\n\n'
                  'test at tests/a.test.js:2:37\n✖ empty cart (0.97ms)\n  AssertionError [ERR_ASSERTION]: 1 == 0\n\n'
                  'test at tests/a.test.js:2:1\n✖ totals (1.59ms)\n  AssertionError [ERR_ASSERTION]: 1 == 2\n')
BOTH_FAIL_TAP = ("TAP version 13\n# Subtest: totals\n    # Subtest: empty cart\n    not ok 1 - empty cart\n      ---\n"
                 "      failureType: 'testCodeFailure'\n      error: '1 == 0'\n      ...\n    1..1\nnot ok 1 - totals\n  ---\n"
                 "  failureType: 'testCodeFailure'\n  error: '1 == 2'\n  ...\n# Subtest: other\nok 2 - other\n1..2\n# fail 2\n")


def node_row(text, passed=True):
    groups = {}
    found = ids.node(text, groups)
    return row(passed=passed, found=found, test_ids={'ids': found, 'aggregates': groups, 'source': 'node'})


def test_a_required_test_that_gains_subtests_is_still_present():
    # review2 #10: a passing leaf that became a t.test() parent (or a describe of the same name) was only an
    # aggregate, so its required id read as "missing" and a correct edit was rolled back.
    required = passing([node_row(LEAF_SPEC)])
    assert required == ['.::other', '.::totals']
    for text in (PARENT_SPEC, PARENT_TAP, PARENT_SPEC.replace('✔ totals (0.97ms)', '✔ totals (1.04ms)')):
        checks = [node_row(text)]
        assert regressions(required, checks) == []
        # Aggregates never become identities or required tests themselves (finding 10 stays fixed).
        assert grow(required, checks) == ['.::other', '.::totals', '.::totals > empty cart']
    assert ids.node(PARENT_TAP) == {'totals > empty cart': 'passed', 'other': 'passed'}
    # A new failing subtest is judged by itself: no loss for the parent, the child is a new failure to repair.
    for text in (CHILD_FAILS_SPEC, CHILD_FAILS_TAP):
        assert regressions(['.::totals'], [node_row(text, passed=False)]) == []
    # The parent's own assertion fails while every test under it passes: that test broke.
    for text in (OWN_BODY_FAILS_SPEC, OWN_BODY_FAILS_TAP):
        assert regressions(['.::totals'], [node_row(text, passed=False)]) == [{'id': '.::totals', 'status': 'failed'}]
    # A child the flaky re-run credited (it failed first) explains the parent's failure, as a failing child does.
    credited = node_row(CHILD_FAILS_TAP, passed=False)
    credited['test_ids']['ids']['totals > empty cart'] = 'passed'
    assert regressions(['.::totals'], [{**credited, 'flaky_tests': ['totals > empty cart']}]) == []


def test_collect_returns_node_aggregates_apart_from_ids(tmp_path):
    (tmp_path / 'node.log').write_text(PARENT_SPEC)
    found = ids.collect('node --test tests/*.test.js', tmp_path / 'node.log', tmp_path, tmp_path / 'no-evidence')
    assert found == {'ids': {'totals > empty cart': 'passed', 'other': 'passed'}, 'source': 'node', 'aggregates': {'totals': 'passed'}}


def test_a_required_parent_that_fails_with_its_new_subtest_is_never_eligible(tmp_path, monkeypatch):
    # review3 #6: the failing child made the failing parent "present" (no loss), so the revision became the
    # eligible_revision a later rollback restores, with a required test failing on its own assertion.
    required = ['.::other', '.::totals']
    for text in (BOTH_FAIL_SPEC, BOTH_FAIL_TAP):
        checks = [node_row(text, passed=False)]
        assert checks[0]['test_ids']['aggregates'] == {'totals': 'failed'}
        assert regressions(required, checks) == []   # not lost: no re-run, no rollback
        assert failing_parents(required, checks) == ['.::totals']
        assert failing_parents(required, checks, superseded=['.::totals']) == []
    # The parser cannot tell a child-only failure apart, so it is not eligible either; passing parents still are.
    assert failing_parents(['.::totals'], [node_row(CHILD_FAILS_TAP, passed=False)]) == ['.::totals']
    assert failing_parents(required, [node_row(PARENT_TAP)]) == failing_parents(required, [node_row(LEAF_SPEC)]) == []
    for label, text, eligible in (('both', BOTH_FAIL_TAP, False), ('spec', BOTH_FAIL_SPEC, False), ('parent', PARENT_TAP, True)):
        (tmp_path / label).mkdir()
        checks = [node_row(text, passed=eligible)]
        result, calls, _, _, _ = check_operation(tmp_path / label, monkeypatch, {'required_tests': required}, {'': checks})
        assert calls == [''] and 'restored_snapshot' not in result and result['lost_tests'] == []
        assert result['eligible'] is eligible, label
        assert result['required_tests'] == (required + ['.::totals > empty cart'] if eligible else required)


# Real Node 25.8 output (trimmed): the required `totals > a` flakes once; an unrelated new test `new` fails both times.
FLAKE_FIRST_TAP = ("TAP version 13\n# Subtest: totals\n    # Subtest: a\n    not ok 1 - a\n      ---\n      failureType: 'testCodeFailure'\n"
                   "      error: '1 == 2'\n      ...\n    1..1\nnot ok 1 - totals\n  ---\n  failureType: 'subtestsFailed'\n"
                   "  error: '1 subtest failed'\n  ...\n# Subtest: other\nok 2 - other\n# Subtest: new\nnot ok 3 - new\n  ---\n"
                   "  failureType: 'testCodeFailure'\n  error: '1 == 2'\n  ...\n1..3\n# tests 4\n# pass 1\n# fail 3\n")
FLAKE_AGAIN_TAP = ("TAP version 13\n# Subtest: totals\n    # Subtest: a\n    ok 1 - a\n    1..1\nok 1 - totals\n# Subtest: other\n"
                   "ok 2 - other\n# Subtest: new\nnot ok 3 - new\n  ---\n  failureType: 'testCodeFailure'\n  error: '1 == 2'\n  ...\n"
                   "1..3\n# tests 4\n# pass 3\n# fail 1\n")
FLAKE_FIRST_SPEC = ('▶ totals\n  ✖ a (1.07ms)\n✖ totals (1.63ms)\n✔ other (0.04ms)\n✖ new (0.09ms)\nℹ tests 4\nℹ fail 3\n\n'
                    '✖ failing tests:\n\ntest at t.test.js:2:39\n✖ a (1.07ms)\n  AssertionError [ERR_ASSERTION]: 1 == 2\n\n'
                    'test at t.test.js:4:1\n✖ new (0.09ms)\n  AssertionError [ERR_ASSERTION]: 1 == 2\n')
FLAKE_AGAIN_SPEC = ('▶ totals\n  ✔ a (0.46ms)\n✔ totals (1.01ms)\n✔ other (0.05ms)\n✖ new (0.68ms)\nℹ tests 4\nℹ fail 1\n\n'
                    '✖ failing tests:\n\ntest at t.test.js:4:1\n✖ new (0.68ms)\n  AssertionError [ERR_ASSERTION]: 1 == 2\n')


def test_a_required_parent_that_passes_on_the_rerun_is_judged_by_the_rerun(tmp_path, monkeypatch):
    # review4 #4: a row still failing on the re-run kept its first-run aggregates, so failing_parents held a revision
    # ineligible after every required test passed again, and a parent whose own body flaked was rolled back.
    required = ['.::totals', '.::totals > a']
    def check(label, first, again):
        (tmp_path / label).mkdir()
        return check_operation(tmp_path / label, monkeypatch, {'required_tests': required},
                               {'': [node_row(first, passed=False)], 'rerun': [node_row(again, passed=False)]})
    for label, first, again in (('tap', FLAKE_FIRST_TAP, FLAKE_AGAIN_TAP), ('spec', FLAKE_FIRST_SPEC, FLAKE_AGAIN_SPEC)):
        result, calls, events, _, _ = check(label, first, again)
        assert calls == ['', 'rerun'] and 'restored_snapshot' not in result and result['lost_tests'] == []
        assert result['eligible'], label
        assert result['required_tests'] == ['.::other', '.::totals', '.::totals > a']   # grows: `other` passed
        row_ = result['checks'][0]
        assert not row_['passed'] and row_['test_ids']['aggregates'] == {'totals': 'passed'}   # `new` still fails
        assert row_['flaky_tests'] == ['totals > a'] and row_['flaky_aggregates'] == ['totals']
        assert ('required_tests_rerun', {'packages': ['.'], 'recovered': ['test:.']}) in events
    # The parent's own body flaked (every test under it passed both times): not lost, no rollback.
    own = '# Subtest: totals\n    # Subtest: a\n    ok 1 - a\nnot ok 1 - totals\n# Subtest: other\nok 2 - other\n# Subtest: new\nnot ok 3 - new\n'
    result, calls, _, _, _ = check('own', own, own.replace('not ok 1 - totals', 'ok 1 - totals'))
    assert calls == ['', 'rerun'] and 'restored_snapshot' not in result and result['eligible']
    assert result['checks'][0]['flaky_tests'] == [] and result['checks'][0]['flaky_aggregates'] == ['totals']
    # A parent that fails again is not credited: its own body broke, so the revision is rolled back.
    result, _, _, _, _ = check('still', own, own)
    assert result['repair_regression']['lost_tests'] == [{'id': '.::totals', 'status': 'failed'}]


def test_only_real_manifests_and_lockfiles_key_the_dependency_environment(tmp_path):
    # review2 #12: 'lock' in the name made clock.py a manifest; editing it looked like a dependency change,
    # so the stop hook skipped that package's checks (and the dependency cache missed).
    from coder_profiles import environment_key
    (tmp_path / 'clock.py').write_text('def now():\n    return 0\n')
    (tmp_path / 'test_clock.py').write_text('import clock\n')
    (tmp_path / 'requirements.txt').write_text('')
    profile = {'toolchains': [], 'commands': {'setup': ['python3 -m venv .venv']}}
    def key():
        return environment_key(tmp_path, profile)[0]
    start = key()
    for name in ('clock.py', 'test_clock.py', 'blocklist.py', 'unlock.js'):
        (tmp_path / name).write_text('changed = 1\n')
        assert key() == start, name
    for name in ('requirements.txt', 'poetry.lock', 'package-lock.json', 'yarn.lock', 'Pipfile', 'deno.lock'):
        before = key()
        (tmp_path / name).write_text('dependency = 2\n')
        assert key() != before, name


def test_files_the_setup_installs_from_key_the_dependency_environment(tmp_path, monkeypatch):
    # review3 #7: exact names missed requirements-lock.txt, constraints.txt or requirements/base.txt, so an edited
    # dependency list reused the stale cached .venv: a ModuleNotFoundError the builder could never fix.
    import coder_profiles
    package = tmp_path / 'p'; (package / 'requirements').mkdir(parents=True)
    (tmp_path / 'venv-cache' / 'bin').mkdir(parents=True); (tmp_path / 'venv-cache' / 'bin' / 'python').write_text('1')
    (package / '.venv').symlink_to(tmp_path / 'venv-cache', target_is_directory=True)
    named = ('requirements-lock.txt', 'requirements.lock.txt', 'requirements-dev.txt', 'constraints.txt', 'requirements/base.txt')
    for name in (*named, 'lock.py', 'clock.py', 'test_lock.py', '../outside.txt'):
        (package / name).write_text('a==1\n')
    profile = {'toolchains': [], 'commands': {'setup': [
        'python3 -m venv .venv && .venv/bin/python -m pip install -r requirements-lock.txt --requirement requirements.lock.txt '
        '--requirement=requirements-dev.txt -cconstraints.txt -r requirements/base.txt -r ../outside.txt']}}
    def key():
        return coder_profiles.environment_key(package, profile)[0]
    for name in named:
        before = key()
        (package / name).write_text('a==1\nb==2\n')
        assert key() != before, name
    start = key()
    # Source files, a file outside the package and the interpreter behind the cached .venv link are not inputs.
    for name in ('lock.py', 'clock.py', 'test_lock.py', '../outside.txt', '.venv/bin/python'):
        (package / name).write_text('changed = 1\n')
        assert key() == start, name
    # A standard setup names only top-level manifests: its key is what it was, so no cache is invalidated.
    (package / 'requirements.txt').write_text('a==1\n')
    standard = {'toolchains': [], 'commands': {'setup': ['python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt']}}
    with_inputs = coder_profiles.environment_key(package, standard)[0]
    monkeypatch.setattr(coder_profiles, 'setup_inputs', lambda root, profile: {})
    assert coder_profiles.environment_key(package, standard)[0] == with_inputs
