"""Regressions from live M1a jobs (offline; recorded check rows, no model).

cw3-13fb755c (2026-10-28): "Add a --min-length N option ... and add tests for the new option." The builder added
the option and NO test; the delivered suite was exactly the 6 inherited tests, and acceptance() still reported
checks_passed because any passing project test row satisfied every ``tests`` outcome, inherited or new.
"""
from coder_policy7 import make_brief
from coder_policy7_evidence import acceptance, baseline_test_ids, decide, requests_new_tests
from coder_required_tests import key
from tests.test_daedalus_required_tests import row

REQUEST = 'Add a --min-length N option to wordfreq.py that drops shorter words, and add tests for the new option.'
PARENT = [{'id': 'o1', 'text': 'wordfreq.py counts word frequencies with executable unit tests', 'evidence_types': ['tests'],
           'component': '.', 'test_interfaces': []}]
INHERITED_IDS = [f'tests/test_wordfreq.py::test_{name}' for name in ('counts', 'sorts', 'empty', 'punctuation', 'case', 'stdin')]
BASELINE = ['.::' + ident for ident in INHERITED_IDS]


def follow_up_brief():
    answer = {'outcomes': [{'text': 'Executable unit tests for the new --min-length option', 'evidence_types': ['tests']}],
              'batches': [{'task': 'Add the option and its tests', 'files': ['wordfreq.py', 'tests/test_wordfreq.py']}],
              'inheritance': [{'parent_id': 'o1', 'action': 'retain'}]}
    return make_brief(answer, REQUEST, PARENT)


def suite(ids):
    return row('.', True, found={ident: 'passed' for ident in ids}, revision_id='r', execution_id='e', coverage_observed=True,
               execution_succeeded=True, test_files=[{'path': 'tests/test_wordfreq.py', 'sha256': 'a' * 64}])


def test_cw3_13fb755c_make_brief_marks_retained_outcomes_inherited_and_new_ones_not():
    outcomes = follow_up_brief()['outcomes']
    assert [o['text'] for o in outcomes] == [PARENT[0]['text'], 'Executable unit tests for the new --min-length option']
    assert outcomes[0]['inherited'] is True and 'inherited' not in outcomes[1]
    assert all('tests' in o['evidence_types'] for o in outcomes)
    fresh = make_brief({'outcomes': ['Count words'], 'batches': [{'task': 'Build', 'files': ['wordfreq.py']}]}, 'Build wordfreq.py')
    assert all('inherited' not in o for o in fresh['outcomes'])


def test_cw3_13fb755c_inherited_suite_does_not_satisfy_an_added_tests_outcome():
    outcomes = follow_up_brief()['outcomes']
    summary = acceptance(outcomes, [suite(INHERITED_IDS)], {}, 'r', baseline_tests=BASELINE)
    assert summary['checks_ok'] and summary['exercised'] and not summary['checks_passed']
    inherited, added = summary['outcomes']
    assert inherited['status'] == 'passed' and inherited['missing_evidence'] == []
    assert added['status'] == 'unverified' and added['missing_evidence'] == ['tests'] and added['missing_new_test']
    assert 'inherited' in added['reason'] and 'no new test covers this outcome' in added['reason']
    assert 'Executable unit tests for the new --min-length option' in added['reason']


def test_cw3_13fb755c_one_new_passing_test_satisfies_the_added_outcome():
    outcomes = follow_up_brief()['outcomes']
    summary = acceptance(outcomes, [suite([*INHERITED_IDS, 'tests/test_wordfreq.py::test_min_length'])], {}, 'r', baseline_tests=BASELINE)
    assert summary['checks_passed'] and all(o['status'] == 'passed' for o in summary['outcomes'])
    # A new test that FAILS is no evidence for the added outcome either (and blocks on its own).
    broken = {**suite(INHERITED_IDS), 'passed': False, 'exit_code': 1, 'classification': 'application_defect'}
    broken['test_ids'] = {'ids': {**broken['test_ids']['ids'], 'tests/test_wordfreq.py::test_min_length': 'failed'}, 'source': 'hooks'}
    assert not acceptance(outcomes, [broken], {}, 'r', baseline_tests=BASELINE)['checks_passed']


def test_cw3_13fb755c_new_builds_without_a_baseline_are_unchanged():
    outcomes = follow_up_brief()['outcomes']
    summary = acceptance(outcomes, [suite(INHERITED_IDS)], {}, 'r')
    assert summary['checks_passed'] and all(o['status'] == 'passed' for o in summary['outcomes'])
    assert not any(o.get('missing_new_test') for o in summary['outcomes'])


def test_cw3_13fb755c_decide_names_the_outcome_that_needs_a_new_test():
    outcomes = follow_up_brief()['outcomes']
    checks = [suite(INHERITED_IDS)]
    summary = acceptance(outcomes, checks, {}, 'r', baseline_tests=BASELINE)
    job = {'checks': checks, 'brief': {'outcomes': outcomes}, 'execution_profile': {'checks': checks}, 'repair_round': 0,
           'audit_corrections': 0, 'mode': 'edit_project', 'baseline_required': BASELINE}
    decision = decide(job, summary, {}, review=False)
    assert decision['action'] == 'repair', decision
    defect = next(d for d in decision['feedback'] if d['id'] == 'o2:tests')
    assert defect['classification'] == 'application_defect'
    assert '--min-length' in defect['reason'] and 'Add a new test' in defect['reason']
    assert 'lack the requested interface coverage' not in defect['reason']


# --- Review of the first gate (2026-10-28): the demand keyed on "not inherited" alone. Uploaded-project edits
# inherit nothing (coder_jobs.create passes inherited=[]), so every tests outcome demanded a brand-new test id.


def test_live_m1a_requests_new_tests_reads_the_request_wording_only():
    asked = ['add tests', 'add a test', 'write tests', 'create tests', 'include tests', 'ship it with tests for parsing',
             'cover it with tests', 'new tests', 'including unit tests', REQUEST]   # a bare "tests for the new option" has no verb: not a demand
    kept = ['tests must still pass', 'existing tests continue to pass', 'keep the tests passing', 'run the tests',
            'all existing tests must still pass', 'Add the option, run the tests', 'Fix the --json flag so the tests pass',
            'Change the default sort order', 'Run the tests for the new module', '', None]
    assert [text for text in asked if not requests_new_tests(text)] == []
    assert [text for text in kept if requests_new_tests(text)] == []


def test_live_m1a_uploaded_project_edit_keeps_checks_passed_with_the_unchanged_suite():
    # No parent brief: nothing is inherited, and the request asks only that the suite keep passing.
    request = 'Fix the off-by-one in wordfreq.py; all existing tests must still pass.'
    brief = make_brief({'outcomes': ['Existing unit tests continue to pass', 'The off-by-one is fixed'],
                        'batches': [{'task': 'Fix it', 'files': ['wordfreq.py']}]}, request)
    assert all('inherited' not in o for o in brief['outcomes']) and 'tests' in brief['outcomes'][0]['evidence_types']
    summary = acceptance(brief['outcomes'], [suite(INHERITED_IDS)], {}, 'r', request=request, baseline_tests=BASELINE)
    assert summary['checks_passed'] and all(o['status'] == 'passed' for o in summary['outcomes'])
    assert not any(o.get('missing_new_test') for o in summary['outcomes'])
    # The same rows with no request still demand (callers that pass none keep the earlier behaviour).
    assert not acceptance(brief['outcomes'], [suite(INHERITED_IDS)], {}, 'r', baseline_tests=BASELINE)['checks_passed']


def test_live_m1a_replacement_wording_without_a_request_for_new_tests_is_not_demanded():
    request = 'Change the default sort order'
    answer = {'outcomes': [{'text': 'Sorting behaviour is covered by unit tests', 'evidence_types': ['tests']}],
              'batches': [{'task': 'Change the sort', 'files': ['wordfreq.py']}],
              'inheritance': [{'parent_id': 'o1', 'action': 'replace', 'request_quote': 'default sort order', 'replacement_outcomes': [1]}]}
    outcomes = make_brief(answer, request, PARENT)['outcomes']
    assert [o['text'] for o in outcomes] == ['Sorting behaviour is covered by unit tests'] and 'inherited' not in outcomes[0]
    summary = acceptance(outcomes, [suite(INHERITED_IDS)], {}, 'r', request=request, baseline_tests=BASELINE)
    assert summary['checks_passed'] and not any(o.get('missing_new_test') for o in summary['outcomes'])


def test_live_m1a_runner_without_test_identities_keeps_the_earlier_behaviour():
    # jest/vitest/npm wrappers report no ids: the new-test filter cannot apply and the row counts as before.
    outcomes = follow_up_brief()['outcomes']
    jest = {**suite([]), 'test_ids': {'ids': {}, 'source': 'runner'}, 'test_count': 7, 'test_runner': 'jest'}
    summary = acceptance(outcomes, [jest], {}, 'r', request=REQUEST, baseline_tests=[])
    assert summary['exercised'] and summary['checks_passed'] and all(o['status'] == 'passed' for o in summary['outcomes'])
    assert not any(o.get('missing_new_test') for o in summary['outcomes'])


def test_live_m1a_follow_up_asking_for_tests_with_only_inherited_ids_is_withheld():
    outcomes = follow_up_brief()['outcomes']
    summary = acceptance(outcomes, [suite(INHERITED_IDS)], {}, 'r', request=REQUEST, baseline_tests=BASELINE)
    assert summary['checks_ok'] and summary['exercised'] and not summary['checks_passed']
    added = summary['outcomes'][1]
    assert added['missing_new_test'] and 'no new test covers this outcome' in added['reason']
    assert acceptance(outcomes, [suite([*INHERITED_IDS, 'tests/test_wordfreq.py::test_min_length'])], {}, 'r',
                      request=REQUEST, baseline_tests=BASELINE)['checks_passed']


def test_live_m1a_baseline_comparison_is_qualified_by_the_package_cwd():
    outcomes = follow_up_brief()['outcomes']
    ids = ['tests/test_tool.py::test_a', 'tests/test_tool.py::test_b']
    def pkg(found):
        return row('pkg', True, found={ident: 'passed' for ident in found}, revision_id='r', execution_id='e', coverage_observed=True,
                   execution_succeeded=True, test_files=[{'path': 'pkg/tests/test_tool.py', 'sha256': 'b' * 64}])
    assert [key(pkg(ids), ident) for ident in ids] == ['pkg::' + ident for ident in ids]
    baseline = ['pkg::' + ident for ident in ids]
    assert not acceptance(outcomes, [pkg(ids)], {}, 'r', request=REQUEST, baseline_tests=baseline)['checks_passed']
    assert acceptance(outcomes, [pkg([*ids, 'tests/test_tool.py::test_new'])], {}, 'r', request=REQUEST, baseline_tests=baseline)['checks_passed']
    # The same ids keyed under the root package are a different test: the comparison never drops the cwd.
    assert acceptance(outcomes, [pkg(ids)], {}, 'r', request=REQUEST, baseline_tests=['.::' + ident for ident in ids])['checks_passed']


def test_live_m1a_make_brief_dedups_a_restated_outcome_already_marked_inherited():
    # A parent outcome that an earlier follow-up marked inherited and the planner's verbatim restatement are one row.
    parent = [{**PARENT[0], 'inherited': True}]
    answer = {'outcomes': [{'text': PARENT[0]['text'], 'evidence_types': ['tests']}],
              'batches': [{'task': 'Tweak', 'files': ['wordfreq.py']}], 'inheritance': [{'parent_id': 'o1', 'action': 'retain'}]}
    brief = make_brief(answer, 'Tweak the output format', parent)
    assert [o['text'] for o in brief['outcomes']] == [PARENT[0]['text']] and brief['outcomes'][0]['inherited'] is True
    assert brief['requirement_history'][0]['active_outcome_ids'] == ['o1']


def test_live_m1a_decide_reason_does_not_double_the_period():
    answer = {'outcomes': [{'text': 'Executable unit tests for the new --min-length option.', 'evidence_types': ['tests']}],
              'batches': [{'task': 'Add the option and its tests', 'files': ['wordfreq.py', 'tests/test_wordfreq.py']}],
              'inheritance': [{'parent_id': 'o1', 'action': 'retain'}]}
    outcomes = make_brief(answer, REQUEST, PARENT)['outcomes']
    checks = [suite(INHERITED_IDS)]
    summary = acceptance(outcomes, checks, {}, 'r', request=REQUEST, baseline_tests=BASELINE)
    job = {'checks': checks, 'brief': {'outcomes': outcomes}, 'execution_profile': {'checks': checks}, 'repair_round': 0,
           'audit_corrections': 0, 'mode': 'edit_project', 'baseline_required': BASELINE}
    defect = next(d for d in decide(job, summary, {}, review=False)['feedback'] if d['id'] == 'o2:tests')
    assert '..' not in defect['reason'] and defect['reason'].endswith('option. Add a new test for it to the project suite.')


# --- Second review (2026-10-28): the first wording predicate let a creation verb anywhere in the clause bind to a
# far-away "tests", so ordinary preservation wording on uploaded-project edits demanded a brand-new test.
def test_live_m1a_preservation_and_execution_wording_never_demand_new_tests():
    for text in ('Add the option without breaking the test suite', 'Add a flag and keep the test suite green',
                 'Add the option and run the tests', 'Add the option so that all tests (including slow ones) pass',
                 'Refactor the parser with the existing tests unchanged', 'Run the tests for the new module',
                 'Add the option and update the existing tests', 'Add the option; the tests should keep passing',
                 'Fix the --json flag so it prints valid JSON.', 'make the test suite faster'):
        assert not requests_new_tests(text), text
    for text in (REQUEST, 'Write unit tests for the parser', 'Create a regression test for the crash', 'Include tests covering the new flag',
                 'Cover it with tests', 'Ship it with tests for every command', 'Add new tests', 'Introduce comprehensive tests',
                 'add a test case for empty input'):
        assert requests_new_tests(text), text


def test_live_m1a_an_id_less_runner_beside_an_id_bearing_suite_keeps_its_standing():
    outcomes = follow_up_brief()['outcomes']
    jest = {**suite([]), 'cwd': 'web', 'test_ids': {'ids': {}, 'source': ''}, 'test_count': 3,
            'test_files': [{'path': 'web/app.test.js', 'sha256': 'b' * 64}], 'execution_id': 'e2'}
    # The pytest package reports only inherited ids; the Node package reports none, so it cannot be told to be new or old.
    summary = acceptance(outcomes, [suite(INHERITED_IDS), jest], {}, 'r', request=REQUEST, baseline_tests=BASELINE)
    assert summary['checks_passed'], summary['outcomes']
    # With only the id-bearing inherited suite the demand still stands.
    assert not acceptance(outcomes, [suite(INHERITED_IDS)], {}, 'r', request=REQUEST, baseline_tests=BASELINE)['checks_passed']


# --- Third review (2026-10-28): the direct-object predicate missed terse owner phrasings ("Add a --json flag with tests.")
# and matched adjective uses ("Add a test-mode flag"); a baseline test that FAILED at baseline passed for a new one.
def test_live_m1a_terse_add_with_tests_wording_is_a_demand_and_adjective_uses_are_not():
    for text in ('Add a --json flag with tests.', 'Add --json output, with tests.', 'Add docs and tests for the new flag.',
                 'Add a README and tests.', 'Add the option and a test for it.', 'Add a --min-length option and corresponding tests.',
                 'Add a --min-length option plus tests.', 'Add the new option (with a unit test).', 'Extend the test suite to cover the new flag.',
                 'Add pytest tests for the parser.', 'Add thorough tests.', 'Add 3 tests for the edge cases.', 'Ship it with a test for every command'):
        assert requests_new_tests(text), text
    for text in ('Add a test-mode flag.', 'Add the test command to the README.', 'Create a test database on startup.', 'Add a unit tests folder',
                 'Write the test results to a file', 'Include the tests in the CI run.', 'Added tests already exist; just fix the bug.',
                 'Introduce a test harness.', 'Create a test runner script.', "Don't add tests.", 'No need to add tests.',
                 'Add the option without new tests.', 'Add logging. Tests are in tests/ and must keep passing.'):
        assert not requests_new_tests(text), text


def test_live_m1a_an_inherited_test_that_failed_at_baseline_is_a_repair_not_a_new_test():
    request = 'Fix the divide bug in calc.py and add a test for it.'
    brief = make_brief({'outcomes': ['The divide bug is fixed', 'A test for the divide fix'],
                        'batches': [{'task': 'Fix and test', 'files': ['calc.py', 'tests/test_calc.py']}]}, request)
    ids = ['tests/test_calc.py::test_add', 'tests/test_calc.py::test_divide']
    job = {'baseline_required': ['.::tests/test_calc.py::test_add'], 'baseline_observed': ['.::' + i for i in ids]}
    assert baseline_test_ids(job) == job['baseline_observed']
    assert baseline_test_ids({'baseline_required': ['x']}) == ['x'] and baseline_test_ids({}) is None
    delivered = [suite(ids)]   # both pass now; test_divide failed at baseline and is still not a NEW test
    assert not acceptance(brief['outcomes'], delivered, {}, 'r', request=request, baseline_tests=baseline_test_ids(job))['checks_passed']
    # The passing-only set alone would have let the repaired test pass for the requested new one.
    assert acceptance(brief['outcomes'], delivered, {}, 'r', request=request, baseline_tests=job['baseline_required'])['checks_passed']
    assert acceptance(brief['outcomes'], [suite([*ids, 'tests/test_calc.py::test_divide_by_zero_message'])], {}, 'r', request=request,
                      baseline_tests=baseline_test_ids(job))['checks_passed']
