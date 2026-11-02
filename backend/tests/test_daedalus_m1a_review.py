"""Regressions for the 2026-10-26 review of the M1a change (offline; the worker check uses recorded rows).

Each test names the confirmed finding it pins. Review is off by default, so the router, the rollback path and
the deliverable evidence must all work without a model-written audit or reviewer.
"""
import asyncio
from types import SimpleNamespace

from coder_policy7_evidence import acceptance, decide
from coder_policy7_controller import regression_entries, step
from tests.test_daedalus_required_tests import row


def failing_test(**extra):
    return {'id': 'test:.', 'cwd': '.', 'phase': 'test', 'is_test': True, 'origin': 'project', 'passed': False,
            'exit_code': 1, 'classification': 'application_defect', 'revision_id': 'r', 'execution_id': 'e', **extra}


def summary_for(checks, outcomes=()):
    return acceptance(list(outcomes), checks, {}, 'r')


def test_review_off_spent_repairs_end_the_request_with_their_durable_limit():
    check = failing_test()
    signature = decide({'checks': [check]}, summary_for([check]), {})['signature']   # the defects' own signature
    job = {'checks': [check], 'repair_round': 2, 'failed_result_history': [signature, signature]}
    decision = decide(job, summary_for([check]), {}, review=False)
    assert decision == {'action': 'candidate', 'reason': 'Both application repair rounds were used.', 'limit': 'application_repairs'}
    # With a reviewer the same history still asks for one diagnosis first.
    assert decide(job, summary_for([check]), {}, review=True)['action'] == 'audit'


def test_review_off_repairs_the_baseline_failure_the_request_asks_to_fix():
    check = failing_test(classification='existing_failure', test_ids={'ids': {'tests/test_calc.py::test_divide': 'failed'}})
    fix = {'checks': [check], 'mode': 'edit_project', 'user_task': 'Fix divide so test_divide passes'}
    assert decide(fix, summary_for([check]), {}, review=False)['action'] == 'repair'
    # An unrelated failure that was already there does not spend repair rounds.
    other = {**fix, 'user_task': 'Add a --json flag to the report command'}
    decision = decide(other, summary_for([check]), {}, review=False)
    assert decision['action'] == 'candidate' and 'baseline' in decision['reason']


def test_the_regression_packet_carries_the_row_that_lost_the_test():
    api = row(found={'tests/test_api.py::test_list': 'passed'}, command='pytest -q tests/test_api.py', log_tail='1 passed')
    models = row(passed=False, found={'tests/test_models.py::test_total': 'failed'}, command='pytest -q tests/test_models.py',
                 log_tail='E   assert 3 == 4')
    entries = regression_entries({'failed_revision': 'f' * 40, 'regressed': ['test:.::tests/test_models.py::test_total'],
                                  'lost_tests': [{'id': '.::tests/test_models.py::test_total', 'status': 'failed'}],
                                  'checks': [api, models]})
    assert entries[0]['command'] == 'pytest -q tests/test_models.py' and entries[0]['log_tail'] == 'E   assert 3 == 4'


def test_a_rollback_to_the_edit_baseline_never_reads_as_checks_passed():
    baseline = [row(found={'.::a': 'passed'}, revision_id='base', execution_id='b:0', coverage_observed=True, test_count=1)]
    job = {'id': 'job', 'project_id': 'p', 'state': 'checking', 'revision_id': 'broken', 'brief': {'outcomes': []}, 'checks': [],
           'eligible_revision': 'base', 'eligible_checks': baseline, 'eligible_from_baseline': True, 'repair_round': 0}
    result = {'restored_snapshot': {'revision': 'base'}, 'checks': [],
              'repair_regression': {'failed_revision': 'broken', 'regressed': ['test:.::a'], 'lost_tests': [{'id': '.::a', 'status': 'failed'}]}}
    saved = {}
    async def operate(*args, **kwargs):
        return result, {**job, 'worker_operation': 'op'}
    async def save(identity, **changes):
        saved.update(changes)
        return {**job, **changes}
    async def record(*args):
        pass
    asyncio.run(step(job, SimpleNamespace(store=SimpleNamespace(save=save, record_check=record), _operate=operate)))
    assert saved['state'] == 'candidate_packaging' and saved['revision_id'] == 'base'
    summary = saved['verification_summary']
    assert summary['rolled_back'] and not summary['checks_passed'] and not summary['accepted']
    assert 'broke tests that passed before' in saved['blocker']


def test_requested_documentation_is_evidenced_without_a_model_audit(tmp_path, monkeypatch):
    from tests.test_daedalus_required_tests import check_operation
    outcome = {'id': 'o1', 'text': 'A README explains usage', 'evidence_types': ['documentation'], 'component': '.'}
    (tmp_path / 'workspace').mkdir()
    (tmp_path / 'workspace' / 'README.md').write_text('Run `python3 wc.py file.txt`.\n')
    result, *_ = check_operation(tmp_path, monkeypatch, {'brief': {'outcomes': [outcome]}},
                                 {'': [row(found={'t': 'passed'}, coverage_observed=True, test_count=1)]})
    docs = [c for c in result['checks'] if c['id'] == 'documentation:o1']
    assert docs and docs[0]['file_bindings'][0]['path'] == 'README.md'
    summary = acceptance([outcome], [c for c in result['checks'] if c.get('revision_id')], {}, docs[0]['revision_id'])
    assert summary['outcomes'][0]['missing_evidence'] == []


def test_a_lost_named_test_rolls_back_to_the_last_eligible_revision_not_an_older_checkpoint(tmp_path, monkeypatch):
    from tests.test_daedalus_required_tests import check_operation
    lost = [row(passed=False, found={'a': 'passed', 'b': 'failed'})]
    result, _, _, eligible, repository = check_operation(
        tmp_path, monkeypatch, {'required_tests': ['.::a', '.::b'], 'repair_checkpoint': 'OLDER'}, {'': lost, 'rerun': lost})
    assert result['restored_snapshot']['revision'] == eligible


def test_a_finish_the_hook_denied_at_the_turn_ceiling_goes_to_the_checks():
    from coder_policy7_ops import edit_transition
    job = {'builder': 'sdk', 'brief': {'batches': [{'files': ['app.py']}]}, 'build_continuations': 0}
    unresolved = {'builder': 'sdk', 'changed': ['app.py'], 'category': 'source_changed', 'agent_finished': False,
                  'hook_denied_unresolved': True}
    assert edit_transition(job, unresolved)['state'] == 'checking'
    # Without a denial an unfinished pass that wrote files still gets a continuation.
    assert edit_transition(job, {**unresolved, 'hook_denied_unresolved': False})['state'] == 'editing'


def documentation_check(tmp_path, monkeypatch, files, component):
    """The check operation over a source tree (and its execution copy) holding the documentation `files`."""
    import coder_repository
    from tests.test_daedalus_required_tests import check_operation
    init = coder_repository.Repository.__init__
    def seeded(self, root, *args, **kwargs):
        for base in (tmp_path / 'source', tmp_path / 'workspace'):
            for name in files:
                (base / name).parent.mkdir(parents=True, exist_ok=True); (base / name).write_text('GET /api/items\n')
        init(self, root, *args, **kwargs)
    monkeypatch.setattr(coder_repository.Repository, '__init__', seeded)
    outcome = {'id': 'o1', 'text': 'Document the server API in a README', 'evidence_types': ['documentation'], 'component': component}
    result, *_ = check_operation(tmp_path, monkeypatch, {'brief': {'outcomes': [outcome]}},
                                 {'': [row(found={'t': 'passed'}, coverage_observed=True, test_count=1)]})
    revision = result['checks'][0]['revision_id']
    summary = acceptance([outcome], [c for c in result['checks'] if c.get('revision_id')], {}, revision)
    return result, [c for c in result['checks'] if c['id'] == 'documentation:o1'], summary


def test_a_component_readme_is_evidence_beside_an_unrelated_root_docs_folder(tmp_path, monkeypatch):
    """Finding 4: docs/setup.md at the root documents no `server`; binding it made the row unmatchable for ever."""
    result, docs, summary = documentation_check(tmp_path, monkeypatch, ['server/README.md', 'README.md', 'docs/setup.md'], 'server')
    assert [b['path'] for b in docs[0]['file_bindings']] == ['server/README.md', 'README.md']
    assert summary['outcomes'][0]['missing_evidence'] == [] and result['missing_deliverables'] == []


def test_only_an_unrelated_root_docs_folder_is_a_missing_deliverable_the_builder_repairs(tmp_path, monkeypatch):
    result, docs, summary = documentation_check(tmp_path, monkeypatch, ['docs/setup.md'], 'server')
    assert docs == [] and result['missing_deliverables'] == ['o1']
    job = {'checks': result['checks'], 'missing_deliverables': result['missing_deliverables']}
    decision = decide(job, summary, {}, review=False)
    assert decision['action'] == 'repair' and 'Requested documentation is missing' in str(decision['feedback'])


def test_a_missing_component_document_is_repaired_where_the_gate_counts_it():
    """Review 3 finding 4: the repair targeted the planned docs/server-api.md, which can never document `server`."""
    from coder_repair_packet import packet, render, repair_targets
    outcome = {'id': 'o1', 'text': 'Document the server API', 'evidence_types': ['documentation'], 'component': 'server'}
    test = row(cwd='server', coverage_observed=True, test_count=1)
    planned = ['server/app.py', 'server/test_app.py', 'docs/server-api.md', 'requirements.txt']
    job = {'checks': [test], 'missing_deliverables': ['o1'], 'mode': 'build_from_prompt',
           'brief': {'outcomes': [outcome], 'batches': [{'files': planned}]},
           'last_patch': {'source_hashes': dict.fromkeys(planned, 'h')}}
    decision = decide(job, summary_for([test], [outcome]), {}, review=False)
    assert decision['action'] == 'repair' and [d['id'] for d in decision['feedback']] == ['o1:documentation']
    assert 'add server/README.md' in decision['feedback'][0]['reason']
    assert repair_targets(job, decision['feedback']) == ['server/README.md']
    assert 'server/README.md' in render(packet(job, decision['feedback'], targets=lambda d: repair_targets(job, [d])))
    # A planned document the gate counts (inside the component, or a root README) stays the target; the outcome
    # may arrive as its id, and a requirements file is never documentation.
    job['brief']['batches'] = [{'files': ['server/app.py', 'server/API.md', 'README.md', 'requirements.txt']}]
    assert repair_targets(job, [{'id': 'o1:documentation', 'outcome': 'o1'}]) == ['server/API.md', 'README.md']
    job['brief']['outcomes'] = [{**outcome, 'component': '.'}]
    job['brief']['batches'] = [{'files': ['app.py', 'requirements.txt']}]
    assert repair_targets(job, [{'id': 'o1:documentation', 'outcome': 'o1'}]) == ['README.md']


def test_repair_wording_alone_never_promotes_an_unrelated_baseline_failure():
    """Finding 5: "fix" or "bug" anywhere in the request made any pre-existing failure a repair target."""
    def action(task, failing):
        check = failing_test(classification='existing_failure', test_ids={'ids': {failing: 'failed', 'tests/test_cli.py::test_json': 'passed'}})
        decision = decide({'checks': [check], 'mode': 'edit_project', 'user_task': task}, summary_for([check]), {}, review=False)
        return decision['action'], decision.get('reason', '')
    net = 'tests/test_net.py::test_fetch_remote_feed'
    for task in ('Fix the --json flag so it prints valid JSON instead of a Python dict.',
                 'There is a bug: the README example is wrong. Update the usage section.',
                 'Add --output; an invalid path fails with exit code 2.'):
        verdict, reason = action(task, net)
        assert verdict == 'candidate' and 'baseline' in reason, task
    # The failing test's subject together with repair wording still names it.
    assert action('Fix the divide bug', 'tests/test_calc.py::test_divide')[0] == 'repair'
    assert action('The calc module is broken for negative numbers', 'tests/test_calc.py::test_negative[-1]')[0] == 'repair'
    assert action('Fix the Divide function in the Go package', 'calc::TestDivide')[0] == 'repair'
    # The subject without a repair request is new work, not this failure.
    assert action('Add rounding to divide', 'tests/test_calc.py::test_divide')[0] == 'candidate'


def test_a_passing_test_file_in_the_same_suite_row_never_promotes_a_baseline_failure():
    """Review 3 finding 3: a whole-suite row traces every test file, so the PASSING test_report.py promoted test_net.py."""
    def action(task, ids, files=('tests/test_net.py', 'tests/test_report.py')):
        check = failing_test(classification='existing_failure', test_ids={'ids': ids},
                             test_files=[{'path': p, 'sha256': p} for p in files])
        decision = decide({'checks': [check], 'mode': 'edit_project', 'user_task': task}, summary_for([check]), {}, review=False)
        return decision['action'], decision.get('reason', '')
    suite = {'tests/test_net.py::test_fetch_remote_feed': 'failed', 'tests/test_report.py::test_json': 'passed'}
    for task in ('Fix the report command so --json prints valid JSON.',
                 'Fix report.py so --json is valid; tests/test_report.py must keep passing.',
                 'Add a --csv option; update test_report.py to cover it.',
                 'Update tests/test_report.py and add a --csv option to the report command.'):
        verdict, reason = action(task, suite)
        assert verdict == 'candidate' and 'baseline' in reason, task
    # The failing test's own file or subject still names it.
    assert action('Fix test_fetch_remote_feed; it fails offline.', suite)[0] == 'repair'
    assert action('tests/test_net.py fails offline; mock the request.', suite)[0] == 'repair'
    assert action('Make test_net.py pass without network access.', suite)[0] == 'repair'
    # Ids without a file part: a lone traced file holds the failure, two traced files name nothing.
    node = {'feed > fetches the remote feed': 'failed'}
    assert action('Fix feed.test.js so it passes offline', node, files=('test/feed.test.js',))[0] == 'repair'
    assert action('Fix report.test.js so it passes offline', node, files=('test/feed.test.js', 'test/report.test.js'))[0] == 'candidate'
    # A Go package in the id is a subject, not a file name: it needs repair wording like any subject.
    assert action('Add a calc mode', {'calc::TestDivide': 'failed'}, files=())[0] == 'candidate'


def baseline_action(task, ids, files=(), **extra):
    check = failing_test(classification='existing_failure', test_ids={'ids': ids},
                         test_files=[{'path': p, 'sha256': p} for p in files], **extra)
    decision = decide({'checks': [check], 'mode': 'edit_project', 'user_task': task}, summary_for([check]), {}, review=False)
    return decision['action'], decision.get('reason', '')


def test_a_cargo_unit_test_source_file_named_for_new_work_never_promotes_its_baseline_failure():
    """Review 4 finding 2: a cargo unit test's id names its source file, so "Add a --json flag to src/main.rs" promoted a
    network test that already failed. A failing compiled row traces no test files, so the id is all there is."""
    main = {'src/main.rs::tests::loads_remote_config': 'failed', 'src/main.rs::tests::counts_words': 'passed'}
    for task, ids in (('Add a --json flag to src/main.rs that prints the counts as JSON.', main),
                      ('Add a --json flag in main.rs.', main),
                      ('In src/lib.rs, add a `slugify` helper and use it in main.rs.',
                       {'src/lib.rs::tests::fetches_mirror': 'failed', 'src/lib.rs::tests::slug': 'passed'})):
        verdict, reason = baseline_action(task, ids)
        assert verdict == 'candidate' and 'baseline' in reason, task
    # The failing test itself, or an integration test file of its own, still names it.
    assert baseline_action('Fix loads_remote_config; it fails offline.', main)[0] == 'repair'
    assert baseline_action('tests/cli.rs fails offline; mock the mirror.', {'tests/cli.rs::fetches': 'failed'})[0] == 'repair'


def test_a_request_naming_the_failing_test_file_promotes_it_when_its_ids_carry_no_file_part():
    """Review 4 finding 3: dotted unittest ids, a pytest collection error and jest name no file, so a row that traced two
    test files promoted nothing and parked the test the request asked to fix as out of scope."""
    two = ('tests/test_calc.py', 'tests/test_report.py')
    unit = {'tests.test_calc.CalcTest.test_divide': 'failed', 'tests.test_report.ReportTest.test_json': 'passed'}
    discover = {'test_calc.CalcTest.test_divide': 'failed', 'test_report.ReportTest.test_json': 'passed'}   # discover -s tests
    collect = 'ERROR collecting tests/test_calc.py\nE   ModuleNotFoundError\nERROR tests/test_calc.py - ModuleNotFoundError: calc'
    tap = {'test/feed.test.js > feed > fetches the remote feed': 'failed', 'test/report.test.js > report > prints json': 'passed'}
    js, jest = ('test/feed.test.js', 'test/report.test.js'), ('__tests__/feed.test.js', '__tests__/report.test.js')
    jest_log = 'PASS __tests__/report.test.js\nFAIL __tests__/feed.test.js\n  ● feed › fetches the remote feed\n'
    for ids in (unit, discover):
        assert baseline_action('Fix the failing tests in tests/test_calc.py.', ids, two)[0] == 'repair'
    # pytest records no id for a module that fails to import; its own failure lines name the module.
    for ids in ({}, {'tests/test_report.py::test_json': 'passed'}):
        assert baseline_action('tests/test_calc.py fails to import; fix it.', ids, two, log_tail=collect)[0] == 'repair'
    # conftest.py and helpers are traced as well, but the only test module still holds the failure.
    assert baseline_action('tests/test_calc.py fails to import; fix it.', {}, ('tests/test_calc.py', 'conftest.py', 'tests/helpers.py'))[0] == 'repair'
    assert baseline_action('Fix the failing test in test/feed.test.js.', tap, js)[0] == 'repair'
    assert baseline_action('Fix the failing test in __tests__/feed.test.js.', {}, jest, log_tail=jest_log)[0] == 'repair'
    # Finding 3 still holds: the passing file, or a failure line naming a file the row did not trace, names nothing.
    for task, ids, files, tail in (('Fix the report command so --json prints valid JSON.', unit, two, ''),
                                   ('Fix tests/test_report.py so it covers --csv.', discover, two, ''),
                                   ('Fix tests/test_report.py so it covers --csv.', {}, two, collect),
                                   ('Fix the failing test in test/report.test.js.', tap, js, ''),
                                   ('Fix __tests__/net.test.js so it passes offline.', {}, jest, 'FAIL __tests__/net.test.js\n')):
        verdict, reason = baseline_action(task, ids, files, log_tail=tail)
        assert verdict == 'candidate' and 'baseline' in reason, task


def test_a_job_accepted_by_the_old_gate_is_rechecked_on_continue_not_republished(monkeypatch, tmp_path):
    """Finding 14: an evidence-version-3 verdict parked at publication re-entered packaging on every Continue."""
    import config, database as db, coder_jobs as controller
    from db import coder_jobs as store
    from context_policy import DEFAULTS
    from coder_policy7_evidence import EVIDENCE_VERSION
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'jobs.db'))
    monkeypatch.setattr(config, 'CONTEXT_SETTINGS', {**DEFAULTS, 'daedalus_v3_enabled': True, 'daedalus_policy7_builds': True})
    monkeypatch.setattr(controller, 'spawn', lambda *args: None)
    async def operate(job, kind, **extra):
        assert kind == 'inspect', kind
        return {'snapshot': {'revision': 'r1', 'tree': 't'}, 'inventory': {'files': 1}, 'workspace': '/w'}, job
    monkeypatch.setattr(controller, '_operate', operate)
    brief = {'project_name': 'App', 'outcomes': [{'id': 'o1', 'text': 'Has README', 'evidence_types': ['documentation'], 'component': '.'}],
             'batches': [{'task': 'Write README', 'files': ['README.md']}]}
    async def parked(key, version):
        job = await controller.create('conversation', 'task', model='coder:local', key=key)
        job = await store.save(job['id'], state='blocked', resume_state='packaging', revision_id='r1', brief=brief,
            verification_version=3, worker_operation='', acceptance={'accepted': True, 'revision_id': 'r1', 'coverage': []},
            verification_summary={'accepted': True, 'evidence_version': version, 'revision_id': 'r1', 'criteria': [], 'outcomes': []})
        return await controller.resume(job['id'])
    async def scenario():
        await db.init_db()
        await db.create_conversation('conversation', model='coder:local')
        old = await parked('old-gate', 3)
        assert old['resume_after_inspect'] == 'checking' and old['acceptance'] is None and old['review'] is None
        await step(old, controller)   # inspect
        assert (await store.get(old['id']))['state'] == 'checking'
        # Only publication failed under the current gate: that verdict still stands.
        current = await parked('current-gate', EVIDENCE_VERSION)
        assert current['resume_after_inspect'] == 'packaging' and current['acceptance']['accepted']
    asyncio.run(scenario())


def adopt_required(job, checks, required):
    """One controller check that adopts `required` (an eligible revision); returns the saved changes."""
    job = {'id': 'job', 'project_id': 'p', 'state': 'checking', 'revision_id': 'r2', 'brief': {'outcomes': []}, 'checks': [], **job}
    result = {'checks': checks, 'required_tests': required, 'eligible': True, 'superseded_tests': [], 'execution_profile': {}}
    saved = {}
    async def operate(*args, **kwargs):
        return result, {**job, 'worker_operation': 'op'}
    async def save(identity, **changes):
        saved.update(changes)
        return {**job, **saved}
    async def record(*args):
        pass
    asyncio.run(step(job, SimpleNamespace(store=SimpleNamespace(save=save, record_check=record), _operate=operate)))
    return {**job, **saved}


def test_a_job_that_baselined_before_required_tests_can_still_supersede_what_the_request_changes():
    """Finding 16: no baseline_required on an older job meant "update the tests" never superseded an inherited test."""
    from coder_policy7_ops import superseded_tests
    from coder_required_tests import regressions
    hello = '.::tests/test_greet.py::test_hello'
    before = [row(found={'tests/test_greet.py::test_hello': 'passed'})]
    after = [row(found={'tests/test_greet.py::test_hi': 'passed'})]   # the requested rename
    old = adopt_required({'mode': 'edit_project', 'user_task': 'Change the greeting to Hi and update the tests',
                          'baseline_checks': [row(found={})]}, before, [hello])
    assert old['baseline_required'] == [hello]
    assert superseded_tests(old, [hello]) == [hello] and regressions([hello], after, superseded_tests(old, [hello])) == []
    # A new build never baselined: the tests it wrote itself are never inherited, whatever the wording says.
    new = adopt_required({'mode': 'build_from_prompt', 'user_task': 'Build a greeter and fix the tests'}, before, [hello])
    assert 'baseline_required' not in new and superseded_tests(new, [hello]) == []
    assert regressions([hello], after, []) == [{'id': hello, 'status': 'missing'}]
    # A job that baselined after the deploy keeps the inherited set it recorded then.
    current = adopt_required({'mode': 'edit_project', 'user_task': 'update the tests', 'baseline_checks': [], 'baseline_required': []},
                             before, [hello])
    assert current['baseline_required'] == []


class FakeOllama:
    """/api/ps + /api/generate over a fixed VRAM budget: a model that does not fit beside the others spills."""
    def __init__(self, vram, sizes, resident=(), busy=(), visible=None, load_error=None):
        # ``visible`` = the names /api/ps shows (the supervisor bridge filters it to the active model);
        # ``load_error`` = a 400 {'error': ...} answered to every load, as the bridge answers a rejected warm request.
        self.vram, self.sizes, self.loaded, self.calls, self.busy = vram, sizes, {}, [], set(busy)
        self.visible, self.load_error = visible, load_error
        for name, ctx in resident:
            self.place(name, ctx)

    def place(self, name, ctx):
        free = self.vram - sum(m['size_vram'] for n, m in self.loaded.items() if n != name)
        size = self.sizes[name]
        self.loaded[name] = {'name': name, 'model': name, 'context_length': ctx, 'size': size, 'size_vram': min(size, max(0, free))}

    def get(self, url, timeout=None):
        self.calls.append(('ps',))
        models = [m for n, m in self.loaded.items() if self.visible is None or n in self.visible]
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {'models': models})

    def post(self, url, json=None, timeout=None):
        if json.get('keep_alive') == 0:
            # Ollama 0.35 accepts the unload and ignores it while the runner is answering a request.
            self.calls.append(('unload', json['model']))
            if json['model'] not in self.busy:
                self.loaded.pop(json['model'], None)
        elif self.load_error:
            self.calls.append(('rejected-load', json['model']))
            def refuse(): raise RuntimeError('400 Client Error')
            return SimpleNamespace(status_code=400, raise_for_status=refuse, json=lambda: {'error': self.load_error})
        elif json['model'] in self.loaded and self.loaded[json['model']]['context_length'] == json['options']['num_ctx']:
            self.calls.append(('noop-load', json['model']))   # already loaded at that context: Ollama changes nothing
        else:
            self.calls.append(('load', json['model'])); self.place(json['model'], json['options']['num_ctx'])
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {})


def test_a_model_squeezed_by_another_resident_model_evicts_it_and_loads_alone():
    from coder_inference import ensure_context
    policy = SimpleNamespace(num_ctx=65536)
    # Live 2026-10-27: planning model resident, coder at 64K context spilled 1.6 GiB to the CPU (0.5 tok/s).
    ollama = FakeOllama(vram=38, sizes={'coder': 21, 'planner': 19}, resident=[('planner', 65536)])
    ensure_context('http://ollama', 'coder', policy, 60, requests_client=ollama, settle=0, confirm=0)
    assert ollama.loaded['coder']['size_vram'] == 21 and 'planner' not in ollama.loaded
    assert ('unload', 'planner') in ollama.calls
    # Already resident at the right context: nothing is reloaded.
    before = len(ollama.calls)
    ensure_context('http://ollama', 'coder', policy, 60, requests_client=ollama, settle=0, confirm=0)
    assert all(call == ('ps',) for call in ollama.calls[before:])
    # A model that fits beside the other is left alone with it.
    roomy = FakeOllama(vram=48, sizes={'coder': 21, 'planner': 19}, resident=[('planner', 65536)])
    ensure_context('http://ollama', 'coder', policy, 60, requests_client=roomy, settle=0, confirm=0)
    assert 'planner' in roomy.loaded and ('unload', 'planner') not in roomy.calls


def test_a_freshly_loaded_model_is_read_again_before_it_counts_as_resident():
    from coder_inference import ensure_context
    # Live 2026-10-28: /api/ps said size_vram == size for a second after the load, then settled to the spill.
    class Settling(FakeOllama):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); self.reads = 0
        def get(self, url, timeout=None):
            self.reads += 1
            response = super().get(url, timeout)
            if self.reads == 2:   # the first read after the load still shows the whole model in VRAM
                entry = dict(self.loaded['coder']); entry['size_vram'] = entry['size']
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'models': [entry, *[m for n, m in self.loaded.items() if n != 'coder']]})
            return response
    ollama = Settling(vram=38, sizes={'coder': 21, 'planner': 19}, resident=[('planner', 65536)])
    ensure_context('http://ollama', 'coder', SimpleNamespace(num_ctx=65536), 60, requests_client=ollama, settle=0, confirm=0)
    assert 'planner' not in ollama.loaded and ollama.loaded['coder']['size_vram'] == 21


def test_a_busy_runner_that_ignores_unloads_is_reported_as_busy_not_unfit():
    import pytest
    from coder_inference import ensure_context
    # Live 2026-10-28: the baseline kept the spilled coder answering; every keep_alive=0 returned in 2 ms, nothing unloaded.
    ollama = FakeOllama(vram=38, sizes={'coder': 21, 'planner': 19}, resident=[('planner', 65536), ('coder', 65536)], busy={'coder'})
    with pytest.raises(ValueError, match='still answering a request'):
        ensure_context('http://ollama', 'coder', SimpleNamespace(num_ctx=65536), 61, requests_client=ollama, settle=0, confirm=0)
    assert ('noop-load', 'coder') not in ollama.calls   # never reloads on top of a runner that was not freed
    # Once the runner is idle the same call evicts and reloads alone.
    ollama.busy.clear()
    ensure_context('http://ollama', 'coder', SimpleNamespace(num_ctx=65536), 61, requests_client=ollama, settle=0, confirm=0)
    assert ollama.loaded['coder']['size_vram'] == 21 and 'planner' not in ollama.loaded


def test_a_model_that_cannot_fit_even_alone_fails_visibly():
    import pytest
    from coder_inference import ensure_context
    ollama = FakeOllama(vram=16, sizes={'coder': 21})
    with pytest.raises(ValueError, match='does not fit in GPU memory'):
        ensure_context('http://ollama', 'coder', SimpleNamespace(num_ctx=65536), 60, requests_client=ollama, settle=0, attempts=2, confirm=0)


def test_live_m1a_follow_up_a_child_behind_the_bridge_cannot_evict_what_it_cannot_see():
    import pytest
    from coder_inference import ensure_context
    policy = SimpleNamespace(num_ctx=65536)
    # Live 2026-10-28 (cw3-13fb755c): the bridge filters /api/ps to the active model and refuses POSTs naming another,
    # so the child's loop unloaded itself six times beside the resident planner and raised a false "does not fit".
    # This is WHY eviction lives in the supervisor bridge (test_daedalus_sandbox.py); the child's loop is left intact.
    bridged = FakeOllama(vram=38, sizes={'coder': 21, 'planner': 19}, resident=[('planner', 65536)], visible={'coder'})
    with pytest.raises(ValueError, match='does not fit in GPU memory'):
        ensure_context('http://ollama', 'coder', policy, 60, requests_client=bridged, settle=0, confirm=0)
    assert ('unload', 'planner') not in bridged.calls and 'planner' in bridged.loaded
    assert bridged.calls.count(('unload', 'coder')) >= 2 and bridged.loaded['coder']['size_vram'] < 21
    # The bridge answers a refused warm load with 400 {'error': reason}; the child surfaces that reason, not "400 Client Error".
    refused = FakeOllama(vram=38, sizes={'coder': 21}, load_error='coder is spilled to the CPU and Ollama will not unload a runner that is still answering a request')
    with pytest.raises(ValueError, match='still answering a request'):
        ensure_context('http://ollama', 'coder', policy, 60, requests_client=refused, settle=0, confirm=0)
    assert ('rejected-load', 'coder') in refused.calls
