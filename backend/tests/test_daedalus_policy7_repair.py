"""Recorded false acceptance, mixed failures and medium-edit recovery."""
import json
from pathlib import Path
import shutil

import pytest


def evidence(**changes):
    return {'id': 'audit-1', 'origin': 'independent', 'passed': True, 'revision_id': 'revision',
        'execution_id': 'op:1', 'outcomes': ['o1'], 'evidence_types': ['behavior'],
        'coverage_observed': True, 'execution_succeeded': True,
        'source_bindings': [{'path': 'app.py', 'sha256': 'a'*64}], **changes}


def test_failure_signatures_ignore_execution_paths_ports_times_and_addresses():
    def failure(path, port, seconds):
        return [{'id': 'x', 'classification': 'unresolved', 'log_tail': f'{path}/project/app.py HTTP 127.0.0.1:{port} in {seconds}s'}]
    assert failure_signature(failure('/worker/execution/op-1', 1111, 1.2)) == failure_signature(failure('/worker/execution/op-2', 2222, 9.8))


def test_log_inspection_enforces_job_ownership_for_policy7(tmp_path):
    from coder_worker_runtime import WorkerStore, read_check_log
    store = WorkerStore(tmp_path)
    for job in ('mine', 'other'):
        store.create(job+'-1', job, 'check', {'policy_version': 7})
        folder = tmp_path/'execution'/(job+'-1'); folder.mkdir(parents=True)
        (folder/'command.log').write_text(job)
    op = store.get('mine-1')
    assert read_check_log(store, op, str(tmp_path/'execution/mine-1/command.log'))['content'] == 'mine'
    with pytest.raises(ValueError, match='belonging'):
        read_check_log(store, op, str(tmp_path/'execution/other-1/command.log'))


# --- Frozen proof 2026-09-17: six working repairs, zero internal acceptances ---

def _python_repair():
    outcomes = [{'id': 'o1', 'text': 'Total is correct', 'evidence_types': ['behavior'], 'component': '.'},
                {'id': 'o2', 'text': 'Delivered tests', 'evidence_types': ['tests'], 'component': '.'}]
    checks = [evidence(), evidence(id='.:test:0', origin='project', is_test=True, cwd='.', phase='test', outcomes=[],
                                   test_files=[{'path': 'tests/check_ledger.py', 'sha256': 'b'*64}])]
    return outcomes, checks


def test_compiled_runners_supply_observation_and_provenance(tmp_path):
    """Policy 7 could not accept any Java or Go project: only Python/Node runs were observable."""
    from coder_project_runtime import compiled_bindings
    root = tmp_path / 'p'
    for name in ('src/main/java/app/Calc.java', 'src/test/java/app/CalcTest.java', 'target/classes/app/Calc.java', 'calc.go', 'calc_test.go'):
        (root / name).parent.mkdir(parents=True, exist_ok=True); (root / name).write_text('x')
    java = [b['path'] for b in compiled_bindings('mvn test', root, root)]
    assert java == ['src/main/java/app/Calc.java', 'src/test/java/app/CalcTest.java']
    assert [b['path'] for b in compiled_bindings('go test -v ./...', root, root)] == ['calc.go', 'calc_test.go']
    assert compiled_bindings('python3 tests/check.py', root, root) == []


# Live job cw3-f7209a44 (Local Task Board): 120/120 calls, blocked before any audit or review.


def test_an_undeclared_script_binary_is_an_application_defect_not_a_sandbox_fault():
    from coder_project_runtime import failure_class, host_fault
    assert not host_fault(127, '> kanban@1.0.0 dev\n> nodemon server.js\n\nsh: 1: nodemon: not found\n')
    assert host_fault(127, 'sh: 1: npm: not found\n') and host_fault(127, '') and not host_fault(1, 'sh: 1: npm: not found')
    row = {'id': 'launch:app', 'phase': 'launch', 'passed': False, 'origin': 'project',
           'environment_fault': host_fault(127, 'sh: 1: nodemon: not found')}
    assert failure_class(row) != 'environment'


def test_http_audits_are_told_about_state_assumptions_and_unrequested_endpoints():
    from coder_audit_hygiene import show_failing_line, web_audit_faults
    request = 'GET /api/tasks - list\nPOST /api/tasks\nPUT /api/tasks/:id\nDELETE /api/tasks/:id'
    audit = ('import os, requests\nAPP_URL = os.environ["DAEDALUS_APP_URL"]\n'
             'tasks = requests.get(f"{APP_URL}/api/tasks").json()\nassert len(tasks) == 1\n'
             'requests.get(f"{APP_URL}/api/tasks/{task_id}")\nrequests.put(f"{APP_URL}/api/tasks/{task_id}", json={})\n')
    faults = web_audit_faults('test_behavior.py', audit, request)
    assert len(faults) == 2 and 'exact number' in faults[0] and 'GET /api/tasks/{task_id}' in faults[1] and 'PUT' not in faults[1].split('Use ONLY')[0]
    assert not web_audit_faults('test_cli.py', 'assert len(rows) == 1\n', request)
    assert not web_audit_faults('t.py', audit.replace('assert len(tasks) == 1\n', '').replace('requests.get(f"{APP_URL}/api/tasks/{task_id}")\n', ''), request)
    handler = 'try:\n    run()\nexcept Exception as e:\n    print(f"Test failed: {e}")\n'
    assert 'traceback.print_exc()' in show_failing_line(handler) and show_failing_line(show_failing_line(handler)).count('print_exc') == 1


# Code audit 2026-09-21: findings traced by reading the gate, one regression each.


def test_compiled_evidence_needs_the_runners_own_summary_not_any_printed_number():
    # `test: ; @echo "Total: 5"` under make, or an app printing "7 out of 10", became five executed tests.
    from coder_native_checks import compiled_count
    assert compiled_count('make test', 'Total: 5\n') is None and compiled_count('make test', 'Processed 7 out of 10 rows\n') is None
    assert compiled_count('ctest --test-dir build --output-on-failure', '100% tests passed, 0 tests failed out of 2\n\nTotal Test time (real) = 0.01 sec') == 2
    assert compiled_count('mvn test', '[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS') == 3
    assert compiled_count('cargo test --workspace', 'test result: ok. 2 passed; 0 failed;\n\ntest result: ok. 1 passed; 0 failed;') == 3
    assert compiled_count('go test -v ./...', '=== RUN   TestAdd\n--- PASS: TestAdd (0.00s)\n--- PASS: TestSub (0.00s)\nPASS\nok  \tcalc\t0.002s') == 2
    assert compiled_count('dotnet test Calc.sln --no-build', 'Passed!  - Failed:     0, Passed:     4, Skipped:     0, Total:     4, Duration: 5 ms') == 4
    assert compiled_count('go test -v ./...', 'ok  \tcalc\t0.002s [no tests to run]') == 0
    assert compiled_count('./gradlew test', 'BUILD SUCCESSFUL in 2s') is None      # Gradle is counted from its JUnit XML
    assert compiled_count('python3 run.py', 'Tests run: 9, Failures: 0, Errors: 0') is None


def test_compiled_subprocess_provenance_needs_a_real_subprocess_call(tmp_path):
    # The words "subprocess" and "DAEDALUS_PROJECT_ROOT" in a comment were enough to bind every compiled source.
    from coder_project_runtime import launches_project
    (tmp_path / 'talk.py').write_text('# would use subprocess with DAEDALUS_PROJECT_ROOT\nassert True\n')
    assert not launches_project(tmp_path)
    (tmp_path / 'talk.py').write_text('import os, subprocess\nroot = os.environ["DAEDALUS_PROJECT_ROOT"]\n'
                                      'out = subprocess.run([root + "/build/calc", "2", "3"], capture_output=True, text=True)\nassert out.stdout.strip() == "5"\n')
    assert launches_project(tmp_path)
    (tmp_path / 'talk.py').write_text('import os\nfrom subprocess import check_output\nroot = os.getenv("DAEDALUS_PROJECT_ROOT")\nassert check_output([root + "/calc"])\n')
    assert launches_project(tmp_path)
    (tmp_path / 'talk.py').write_text('def broken(:\n')
    assert not launches_project(tmp_path)


def test_browser_evidence_is_a_browser_request_not_a_mozilla_user_agent():
    # 'Mozilla/' in User-Agent alone: requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}) manufactured it.
    from coder_project_runtime import from_browser
    chromium = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/140.0', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
    assert from_browser(chromium) and from_browser({**chromium, 'Sec-Fetch-Mode': 'cors'})
    assert not from_browser({'User-Agent': 'Mozilla/5.0 (spoofed by a Python client)'})
    assert not from_browser({'User-Agent': 'python-requests/2.32', 'Sec-Fetch-Mode': 'navigate'}) and not from_browser({})


# Design review of the 2026-09-21 fixes: what the first versions still got wrong.


def test_only_api_supplied_protection_is_inherited_by_a_follow_up_job():
    # "Do not modify README.md" in one request must not forbid "now update README.md" in the next.
    from coder_jobs import inherited_protection
    source = {'protected_files': ['config.yaml', 'README.md'], 'grounded_protected_files': ['README.md']}
    assert inherited_protection(source, ['LICENSE']) == ['config.yaml', 'LICENSE']
    assert inherited_protection({}, []) == [] and inherited_protection(None, ['a.txt']) == ['a.txt']


def test_make_is_not_a_compiled_test_runner_and_newer_dotnet_summaries_count():
    from coder_native_checks import compiled_count
    from coder_project_runtime import COMPILED_RUNNERS
    assert 'make' not in COMPILED_RUNNERS     # a Makefile can echo any summary line; EXECUTION_HELP already says so
    assert compiled_count('make test', '100% tests passed, 0 tests failed out of 3\n') is None
    assert compiled_count('dotnet test App.sln --no-build', 'Test summary: total: 3, failed: 0, succeeded: 3, skipped: 0, duration: 0.5s') == 3


# Code audit 2026-09-21: small fail-closed robustness items.
def test_false_is_not_an_inherit_value_for_a_context_setting():
    from context_policy import positive_int
    assert positive_int(0, 'ctx', inherit=True) == 0 and positive_int('inherit', 'ctx', inherit=True) == 0
    for bad in (False, True):
        with pytest.raises(ValueError):
            positive_int(bad, 'ctx', inherit=True)


def test_a_truncated_trace_file_costs_its_assertions_not_the_whole_check(tmp_path):
    from coder_native_checks import provenance
    (tmp_path / 'python-1.json').write_text(json.dumps({'imports': [{'path': 'app.py', 'sha256': 'a' * 64}], 'assertions': 2}))
    (tmp_path / 'python-2.json').write_text('{"imports": [{"path": "b.py"')      # child killed mid-write
    bindings, assertions = provenance(tmp_path, tmp_path)
    assert assertions == 2 and [b['path'] for b in bindings] == ['app.py']


def test_a_finished_question_is_not_labelled_needs_attention():
    from coder_presentation import presentation
    assert presentation({'state': 'completed', 'answer': 'It uses SQLite.'})['phase'] == 'Answered'
    assert presentation({'state': 'completed', 'artifact': {'id': 'a'}, 'artifact_status': 'delivered'})['phase'] == 'Complete'
    assert presentation({'state': 'blocked'})['phase'] == 'Needs attention'


# Live run 2026-09-22, c-rpncalc (cw3-fa698e74a7c79be8b3df9565): the audit copied the prompt's shell quoting into a
# subprocess LIST argument, so the program received literal quote characters and exited 2 - twice, after both corrections.


# Live run 2026-09-22, kanban-live (cw3-67ef448bf0c65aeecc02cb55): the path regex stopped at the first quote inside an
# f-string interpolation, so DELETE /api/tasks/{created_task['id']} read as an unrequested endpoint and BOTH audit
# corrections were spent on a valid audit.
def test_interpolated_ids_in_audit_urls_are_the_requested_wildcard_endpoints():
    from coder_audit_hygiene import web_audit_faults
    request = 'GET /api/tasks\nPOST /api/tasks\nPUT /api/tasks/:id\nDELETE /api/tasks/:id'
    audit = ('import os, requests\nAPP_URL = os.environ["DAEDALUS_APP_URL"]\n'
             'created = requests.post(f"{APP_URL}/api/tasks", json={"title": "probe"}).json()\n'
             'requests.put(f"{APP_URL}/api/tasks/{created[\'id\']}", json={"status": "done"})\n'
             'requests.delete(f"{APP_URL}/api/tasks/{created["id"]}")\n'
             'requests.get(f"{APP_URL}/api/tasks")\n'
             'tid = created["id"]\nrequests.put(f"{APP_URL}/api/tasks/{tid}", json={})\n')
    assert web_audit_faults('test_behavior.py', audit, request) == []
    unrequested = audit + 'requests.get(f"{APP_URL}/api/tasks/{created[\'id\']}/history")\n'
    faults = web_audit_faults('test_behavior.py', unrequested, request)
    assert len(faults) == 1 and 'GET /api/tasks/{created[\'id\']}/history' in faults[0]


# Live run 2026-09-22, node-mdtoc (cw3-4de01cc04fa7c9de6ca24a74): the audit misread its own fixture (## Conclusion at
# level 1); the reviewer agreed three times; the builder chased it, then answered "nothing to change" (no_op, finished)
# at the end of both rounds. The audit-correction branch needed an identical failure signature and sat after the repair
# cap, so a correct program ended with application_repairs and both corrections unused.


from coder_check_evidence import failure_signature
