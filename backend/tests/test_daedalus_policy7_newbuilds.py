"""New-build delivery: request probes, workflow guard, repair packet, foundation gate, skeletons.

One test per recorded finding of the 2026-09-24 campaigns (frozen-6/7/8, reliability sweeps); each names
the run that found it. Pure Python, no inference, no server. The Codebox-only skeleton verification lives at
the bottom and skips without the toolchains and bwrap.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from coder_policy7_phases import layer, progress, ordered
from coder_request_probes import requested_commands, launcher, render, command_rows, solution_test_rows, solution_projects
from coder_workflow_guard import workflow_steps, workflow_rows
from coder_repair_packet import packet, render as render_packet
from coder_skeletons import SKELETONS, select_skeleton, describe, apply, placeholder_row, manifest_hash, names_for, guidance as skeleton_guidance


class FakeStore:
    def __init__(self, root):
        self.root = Path(root); self.events = []
    def get(self, op_id):
        return {'payload': {'settings': {'daedalus_command_seconds': 60}}}
    def event(self, op_id, kind, **data):
        self.events.append((kind, data))


# --- request probes -------------------------------------------------------------------------

def test_frozen7_missing_requested_entry_file_is_a_probe_defect(tmp_path):
    # frozen-7 repeat delivered wordfreq/__init__.py and no wordfreq.py; every generated check passed.
    task = 'Build a CLI: `python3 wordfreq.py <file>` prints the top words; `--json` switches to JSON output.'
    specs = requested_commands(task)
    assert [s['excerpt'] for s in specs] == ['python3 wordfreq.py <file>']   # a lone flag is not an invocation
    (tmp_path / 'wordfreq').mkdir(); (tmp_path / 'wordfreq' / '__init__.py').write_text('')
    calls = []
    def runner(store, op_id, root, argv, *, project_id, log):
        calls.append(argv)
        return 2, "python3: can't open file '" + argv[1] + "': [Errno 2] No such file or directory"
    rows = command_rows(FakeStore(tmp_path / 'state'), 'op', tmp_path, {'profiles': [{'toolchains': ['python3']}]}, task, 'r1',
                        project_id='p', runner=runner)
    assert len(rows) == 1 and calls[0][:2] == ['python3', 'wordfreq.py'] and Path(calls[0][2]).is_file()
    row = rows[0]
    assert not row['passed'] and row['classification'] == 'application_defect' and row['evidence_types'] == [] and row['outcomes'] == []
    assert row['request_excerpt'] == 'python3 wordfreq.py <file>' and 'never acceptance evidence' in row['reason']
    assert layer(row) == 1   # cannot start: fix before anything else
    assert progress(rows)['phase'] == 1 and progress(rows)['blocked_by'] == ['probe:command:1']


def test_unknown_placeholders_and_unlocated_programs_are_skipped_not_failed(tmp_path):
    task = 'Run `unitconv <value> <from> <to>` and `python3 tool.py <file>`; also `npm install` and `min X`.'
    specs = requested_commands(task)
    assert {s['excerpt'] for s in specs} == {'unitconv <value> <from> <to>', 'python3 tool.py <file>', 'min X'}
    assert launcher({'profiles': []}, tmp_path, specs[0]) is None          # no built binary named unitconv
    assert launcher({'profiles': []}, tmp_path, [s for s in specs if s['excerpt'] == 'min X'][0]) is None
    rendered, note = render(['unitconv', '<value>', '<from>', '<to>'], tmp_path / 'fx')
    assert rendered is None and 'placeholder' in note
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, {'profiles': []}, task, 'r1', project_id='p',
                        runner=lambda *a, **k: (0, ''))
    assert [r['command'] for r in rows] == [c for c in (r['command'] for r in rows) if c.startswith('python3 tool.py')]


def test_placeholder_first_invocations_run_the_built_dotnet_application(tmp_path):
    # csharp-csvstats: "Running the app with `<file.csv> --column <name>`" names no program at all.
    (tmp_path / 'CsvStats').mkdir(); (tmp_path / 'CsvStats' / 'CsvStats.csproj').write_text('<Project Sdk="Microsoft.NET.Sdk"><OutputType>Exe</OutputType></Project>')
    (tmp_path / 'CsvStats.Tests').mkdir(); (tmp_path / 'CsvStats.Tests' / 'CsvStats.Tests.csproj').write_text('<Project><PackageReference Include="xunit" /></Project>')
    spec = requested_commands('Running the app with `<file.csv> --column <name>` prints min, max and mean.')[0]
    assert spec['application']
    argv = launcher({'profiles': [{'toolchains': ['dotnet']}]}, tmp_path, spec)
    assert argv[:5] == ['dotnet', 'run', '--no-build', '--project', 'CsvStats/CsvStats.csproj'] and argv[5] == '--'
    rendered, note = render(argv, tmp_path / 'fx')
    assert rendered[-2:] == ['--column', 'value'] and Path(rendered[-3]).read_text().startswith('name,value')


def test_frozen7_empty_dotnet_solution_is_named_not_just_missing_coverage(tmp_path):
    # frozen-7 C#: `dotnet test` exited 0 with no project to restore; the gate only said "zero tests".
    (tmp_path / 'CsvStats.sln').write_text('Microsoft Visual Studio Solution File, Format Version 12.00\nGlobal\nEndGlobal\n')
    row = solution_test_rows(tmp_path, 'r1', 'op')[0]
    assert not row['passed'] and 'lists no projects' in row['reason'] and layer(row) == 1
    sln = describe('dotnet-xunit', 'solution named CsvStats')
    full = tmp_path / 'full'; full.mkdir()
    apply(full, sln)
    assert solution_projects((full / 'CsvStats.sln').read_text()) == ['CsvStats/CsvStats.csproj', 'CsvStats.Tests/CsvStats.Tests.csproj']
    assert solution_test_rows(full, 'r1', 'op')[0]['passed']
    (full / 'CsvStats.Tests' / 'AppTests.cs').write_text('public class AppTests {}')
    assert 'no [Fact]' in solution_test_rows(full, 'r1', 'op')[0]['reason']


# --- browser workflows ----------------------------------------------------------------------

def test_frozen6_kanban_edit_and_drag_workflows_are_probed():
    # frozen-6 Kanban: Edit called a missing GET /api/tasks/:id (404); drag sent a status-only PUT (400).
    task = ('Kanban board: create, edit and delete tasks; move tasks between columns via drag-and-drop; '
            'data persistence in SQLite.')
    assert workflow_steps(task) == ['persist', 'edit', 'drag', 'delete']
    def prober(url, steps, **kw):
        return {'fault': '', 'writes': [], 'errors': [], 'steps': {
            'persist': {'status': 'passed', 'detail': 'still shown after reload'},
            'edit': {'status': 'failed', 'detail': 'the edit workflow sent a request the server rejected: GET /api/tasks/3 -> 404'},
            'drag': {'status': 'failed', 'detail': 'the drag workflow sent a request the server rejected: PUT /api/tasks/3 -> 400'},
            'delete': {'status': 'advisory', 'detail': 'no visible Delete control was found'}}}
    rows = workflow_rows({'id': 'app', 'cwd': '.'}, 'http://127.0.0.1:1', {}, task, prober=prober)
    by_id = {r['id']: r for r in rows}
    assert by_id['workflow:app:persist']['passed'] and by_id['workflow:app:delete']['passed']   # advisory never fails
    assert not by_id['workflow:app:edit']['passed'] and by_id['workflow:app:edit']['classification'] == 'application_defect'
    assert not by_id['workflow:app:drag']['passed'] and all(r['evidence_types'] == [] and r['outcomes'] == [] for r in rows)
    assert layer(by_id['workflow:app:drag']) == 3
    down = workflow_rows({'id': 'app'}, 'http://x', {}, task, prober=lambda *a, **k: {'fault': 'Error: browser missing', 'steps': {}, 'writes': [], 'errors': []})
    assert all(r['environment_fault'] and r['classification'] == 'environment' and not r['passed'] for r in down)


def test_pilotA_workflow_guard_is_a_registered_isolated_probe():
    # Pilot A kanban (2026-09-25): every web case crashed with "Unsupported isolated probe" because the
    # sandboxed call layer allowlists probe functions and the new guard was not on it.
    source = (Path(__file__).resolve().parents[1] / 'coder_sandbox_call.py').read_text()
    assert "('coder_workflow_guard', 'run_workflows')" in source
    import coder_sandbox_call, coder_workflow_guard
    assert callable(getattr(coder_workflow_guard, 'run_workflows'))


def test_pilotA2_workflow_guard_has_no_unresolved_names():
    # Pilot A kanban (2026-09-25): `_fill` was imported inside run_workflows but used by _Session methods as a
    # global, so every workflow step faulted with NameError inside the sandbox where no unit test executes it.
    import ast, builtins
    source = (Path(__file__).resolve().parents[1] / 'coder_workflow_guard.py').read_text()
    tree = ast.parse(source)
    module_names = set(dir(builtins))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module_names.update((alias.asname or alias.name).split('.')[0] for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, ast.Assign):
            module_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    unresolved = set()
    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        local = {a.arg for a in func.args.args + func.args.kwonlyargs} | ({func.args.vararg.arg} if func.args.vararg else set())
        for node in ast.walk(func):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                local.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                local.update((alias.asname or alias.name).split('.')[0] for alias in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Lambda)) and node is not func:
                local.update(getattr(node, 'name', '') and [node.name] or [])
                if isinstance(node, ast.Lambda):
                    local.update(a.arg for a in node.args.args)
            elif isinstance(node, ast.comprehension):
                local.update(n.id for n in ast.walk(node.target) if isinstance(n, ast.Name))
        for node in ast.walk(func):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in local and node.id not in module_names:
                unresolved.add(node.id)
    assert unresolved == set(), unresolved


def test_pilotA3_silent_drag_that_moves_nothing_and_sends_nothing_fails():
    # Pilot A kanban (2026-09-25): persist/edit/delete passed, drag "completed" but the card never moved and no
    # request was sent; the guard said advisory and the independent browser journey failed on drag persistence.
    source = (Path(__file__).resolve().parents[1] / 'coder_workflow_guard.py').read_text()
    assert 'changed nothing: no request was sent' in source
    task = 'Move tasks between columns via drag-and-drop.'
    rows = workflow_rows({'id': 'app'}, 'http://x', {}, task, prober=lambda *a, **k: {'fault': '', 'writes': [], 'errors': [],
        'steps': {'drag': {'status': 'failed', 'detail': 'dropping the card onto another column changed nothing: no request was sent and the card stayed where it was.'}}})
    assert [r['id'] for r in rows] == ['workflow:app:drag'] and not rows[0]['passed'] and rows[0]['classification'] == 'application_defect'


def test_frozen8_literal_text_rendering_is_probed_only_when_requested():
    assert 'literal' in workflow_steps('Render entered text literally. Preserve expenses across reloads with localStorage.')
    assert workflow_steps('Build a CLI that prints word counts') == []
    task = 'Render entered text literally.'
    rows = workflow_rows({'id': 'app'}, 'http://x', {}, task, prober=lambda *a, **k: {'fault': '', 'writes': [], 'errors': [],
        'steps': {'literal': {'status': 'failed', 'detail': 'entered text is rendered as HTML: <b>daedalus-literal</b> became a bold element'}}})
    assert [r['id'] for r in rows] == ['workflow:app:literal'] and not rows[0]['passed'] and 'bold element' in rows[0]['reason']


# --- repair packet ----------------------------------------------------------------------------

def test_repair_packet_orders_foundation_before_interface_before_delivery():
    from coder_policy7_controller import repair_targets
    job = {'brief': {'outcomes': [{'id': 'o1', 'text': 'README documents usage'}, {'id': 'o2', 'text': 'Drag moves tasks'}],
                     'batches': [{'files': ['server.js', 'public/app.js', 'README.md']}]},
           'last_patch': {'source_hashes': {'server.js': 'a', 'public/app.js': 'b', 'README.md': 'c', 'database.js': 'd'}}, 'revision_id': 'r9'}
    defects = [
        {'id': 'audit-1', 'origin': 'independent', 'outcomes': ['o2'], 'reason': 'assertion failed', 'log_tail': 'AssertionError: expected 1 got 0'},
        {'id': 'o1:documentation', 'outcome': 'o1', 'origin': 'controller', 'reason': 'README missing'},
        {'id': 'launch:app', 'origin': 'project', 'phase': 'launch', 'command': 'node server.js', 'log_tail': "Error: Cannot find module './database'"},
        {'id': 'interface:#save', 'origin': 'controller', 'reason': 'missing'},
    ]
    entries = packet(job, defects, targets=lambda defect: repair_targets(job, [defect]))
    assert [e['id'] for e in entries] == ['launch:app', 'interface:#save', 'audit-1', 'o1:documentation']
    assert entries[0]['layer_label'] == 'foundation' and entries[0]['owning_files'] == ['server.js'] and entries[2]['request_excerpt'] == 'Drag moves tasks'
    assert entries[3]['request_excerpt'] == 'README documents usage' and entries[3]['owning_files'] == ['README.md']
    text = render_packet(entries)
    assert text.startswith('REPAIR PACKET') and text.index('launch:app') < text.index('audit-1') < text.index('o1:documentation')
    assert len(render_packet(entries * 20)) < 3000


def test_pilotA2_missing_deliverable_defects_carry_the_outcome_object_not_its_id():
    # Pilot A kanban (2026-09-25): decide() attaches the whole outcome dict as `outcome` on a missing-documentation
    # defect; using it as a dict key crashed the accepting stage ("unhashable type: 'dict'") and blocked the job.
    job = {'brief': {'outcomes': [{'id': 'o1', 'text': 'README documents the API'}]}, 'repair_targets': ['README.md']}
    by_object = {'id': 'o1:documentation', 'outcome': {'id': 'o1', 'text': 'README documents the API'}, 'reason': 'missing'}
    by_id = {'id': 'o1:documentation', 'outcome': 'o1', 'reason': 'missing'}
    for defect in (by_object, by_id):
        entry = packet(job, [defect])[0]
        assert entry['request_excerpt'] == 'README documents the API' and entry['layer'] == 4


def test_variance_java_generated_test_contradicting_the_request_is_flagged_in_the_packet():
    # Contained sweep 2: a builder-written Kelvin test with a wrong expectation drove eight rewrites of correct code.
    job = {'brief': {'outcomes': [{'id': 'o1', 'text': 'Convert 273.15 K to 0 C'}]}, 'repair_targets': ['src/main/java/App.java']}
    defect = {'id': '.:test:0', 'origin': 'project', 'is_test': True, 'test_origin': 'current_builder', 'outcomes': ['o1'],
              'assertion_failures': [{'expected': '0.0', 'actual': '546.3'}], 'exit_code': 1, 'log_tail': 'FAILED KelvinTest'}
    entry = packet(job, [defect])[0]
    assert entry['expected'] == '0.0' and entry['actual'] == '546.3' and entry['test_origin'] == 'current_builder'
    text = render_packet([entry])
    assert 'written in this job, not by the user' in text and 'expected: 0.0 / actual: 546.3' in text


def test_v2_kanban_repair_rounds_do_not_repeat_the_full_conventions_text():
    from coder_policy7_builder import guidance
    from coder_policy7 import EXECUTION_HELP
    assert EXECUTION_HELP in guidance({'repair_round': 0})
    repair = guidance({'repair_round': 1, 'repair_feedback': [{'id': 'x', 'origin': 'project'}]})
    assert EXECUTION_HELP not in repair and 'PROJECT CONVENTIONS: unchanged' in repair


def test_contained_rust_repair_guidance_forbids_reinitialising_the_checkpoint():
    from coder_policy7_builder import guidance
    job = {'repair_round': 1, 'repair_feedback': [{'id': 'o1:documentation', 'outcome': 'o1', 'reason': 'README missing'}],
           'repair_packet': [{'layer': 4, 'layer_label': 'delivery', 'id': 'o1:documentation', 'origin': 'controller', 'test_origin': '',
                              'request_excerpt': 'README with usage', 'command': '', 'cwd': '.', 'input': '', 'expected': '', 'actual': '',
                              'trace': [], 'reason': 'README missing', 'owning_files': ['README.md'], 'failed_revision': 'r1'}]}
    text = guidance(job)
    assert 'REPAIR PACKET' in text and 'cargo new' in text and 'never delete or re-initialise' in text


# --- foundation gate ----------------------------------------------------------------------------

def test_launch_and_page_failures_are_prerequisites_before_any_audit(monkeypatch, tmp_path):
    # frozen-7 Kanban: the server threw on start; the job still authored an audit and spent a review on it.
    from tests.test_daedalus_policy7_lifecycle import _drive, BRIEF
    dead = {'id': 'launch:app', 'origin': 'project', 'phase': 'launch', 'passed': False, 'command': 'node server.js',
            'log_tail': 'TypeError: database.initDatabase is not a function', 'revision_id': 'r1'}
    async def operate(job, kind, **extra):
        assert kind == 'check'
        return {'checks': [dead], 'execution_profile': {'checks': []}, 'workspace': str(tmp_path)}, {**job, 'worker_operation': 'w1'}
    job = _drive(monkeypatch, tmp_path, dict(state='checking', revision_id='r1', brief=BRIEF, builder='sdk'), operate)
    assert job['state'] == 'coding' and job['preflight_failures'][0]['id'] == 'launch:app' and job['preflight_retries'] == 1
    assert job['stage_progress']['phase'] == 1 and job['stage_progress']['blocked_by'] == ['launch:app']
    assert not job.get('audit_attempts')


def test_layers_cover_every_row_kind():
    assert layer({'id': '.:setup:0', 'phase': 'setup'}) == 1 and layer({'id': 'page:app'}) == 1 and layer({'id': 'execution-contract'}) == 1
    assert layer({'id': 'probe:command:1', 'exit_code': 2, 'log_tail': 'usage error'}) == 2
    assert layer({'id': 'probe:command:1', 'exit_code': 127, 'log_tail': ''}) == 1
    assert layer({'id': '.:test:0', 'phase': 'test', 'is_test': True}) == 2 and layer({'id': 'interface:--json'}) == 2
    assert layer({'id': 'skeleton:placeholder'}) == 2 and layer({'id': 'immutable-source'}) == 2
    assert layer({'id': 'form:app'}) == 3 and layer({'id': 'audit-2', 'origin': 'independent'}) == 3
    assert layer({'id': 'protected:styles.css'}) == 4 and layer({'id': 'o1:tests', 'outcome': 'o1'}) == 4
    assert layer({'id': 'doc-1', 'kind': 'file', 'evidence_types': ['documentation']}) == 4
    # sweep3: negative probes are core behavior even when the bad input makes the program die with 127; a flag
    # probe follows the positive rule; the requested-labels row is interface integration.
    assert layer({'id': 'probe:invalid:2:1', 'exit_code': 127, 'log_tail': 'command not found'}) == 2
    assert layer({'id': 'probe:missing:1', 'exit_code': 0, 'log_tail': ''}) == 2
    assert layer({'id': 'probe:command:1:flag:max-depth', 'exit_code': 2, 'log_tail': ''}) == 2
    assert layer({'id': 'interface:labels:app', 'origin': 'controller'}) == 3 and layer({'id': 'interface:#save'}) == 2
    rows = [{'id': 'audit-1', 'origin': 'independent', 'passed': False}, {'id': '.:build:0', 'phase': 'build', 'origin': 'project', 'passed': True}]
    assert progress(rows) == {'phase': 3, 'of': 4, 'label': 'interface integration', 'complete': False, 'blocked_by': ['audit-1']}
    assert progress([{'id': 'x', 'passed': True}])['complete'] and progress([])['phase'] == 1
    assert [d['id'] for d in ordered([{'id': 'audit-1', 'origin': 'independent'}, {'id': 'form:app'}, {'id': 'launch:app'}])] == ['launch:app', 'form:app', 'audit-1']


# --- skeletons ------------------------------------------------------------------------------------

def test_skeleton_selection_matches_the_build_cases_and_falls_back_to_the_general_builder():
    cases = {c['name']: c['task'] for c in json.loads((Path(__file__).resolve().parents[1] / 'evals' / 'build_cases.json').read_text())}
    from evals.policy7_fixtures import WEB_TASKS, MEDIUM_TASK
    chosen = {name: select_skeleton(task) for name, task in cases.items()}
    assert chosen['csharp-csvstats'] == 'dotnet-xunit' and chosen['kanban-live'] == 'express-sqlite'
    assert all(v is None for k, v in chosen.items() if k not in {'csharp-csvstats', 'kanban-live'}), chosen
    assert select_skeleton(WEB_TASKS[0]) == 'static-web' and select_skeleton(MEDIUM_TASK) == 'react-fastapi-sqlite'
    # Never for uploads or follow-ups, never when files already exist, never for other modes.
    assert select_skeleton(cases['kanban-live'], job={'inherited': [{'id': 'o1', 'text': 'x'}]}) is None
    assert select_skeleton(cases['kanban-live'], job={'inventory': {'files': 3}}) is None
    assert select_skeleton(cases['kanban-live'], job={'mode': 'edit_project'}) is None
    assert names_for('dotnet-xunit', cases['csharp-csvstats']) == {'App': 'CsvStats', 'Tests': 'CsvStats.Tests'}
    assert names_for('dotnet-xunit', 'Build a C# tool', 'csv stats') == {'App': 'CsvStats', 'Tests': 'CsvStats.Tests'}


def test_skeletons_can_be_switched_off_for_attribution_pilots(monkeypatch):
    task = 'Build a full-stack web application with Node.js + Express and SQLite'
    assert select_skeleton(task) == 'express-sqlite'
    monkeypatch.setenv('DAEDALUS_SKELETONS', 'off')
    assert select_skeleton(task) is None


def test_pilotB_focused_retry_starts_a_fresh_sdk_session_and_budget_rejections_are_reported(tmp_path):
    # 2026-09-25: 15 of 15 archived repair retries died at their first call with "Builder input exceeds its
    # configured input budget" (zero events) because the retry re-sent the whole task into the first attempt's
    # session; the job then said "no source changes after a focused retry".
    from coder_policy7_builder import sdk_build
    from coder_policy7_ops import edit_transition
    from tests.test_daedalus_policy7 import fixture
    captured = {}
    def runner(store, op, repo):
        raise RuntimeError('Conversation run failed: litellm.APIConnectionError - {"error": "Builder input exceeds its configured input budget"}')
    for index, recovery in enumerate((False, True)):
        (tmp_path / f'r{index}').mkdir()
        repository, _ = fixture(tmp_path / f'r{index}', {})
        store = FakeStore(tmp_path / 'state'); store.update = lambda op, payload: captured.update(payload)
        result = sdk_build(store, 'op', repository, {'brief': {'outcomes': [], 'batches': []}, 'repair_round': 1, 'recovery_used': recovery,
                                                     'repair_feedback': [{'id': 'audit-1', 'origin': 'independent'}]},
                           {'settings': {'daedalus_exclude_dirs': []}, 'revision_id': 'r0'}, runner=runner)
        assert captured['milestone_id'] == ('build-r1-retry' if recovery else 'build-r1')
        assert result['category'] == 'input_budget' and result['input_budget'] and result['changed'] == []
        decision = edit_transition({'builder': 'sdk', 'brief': {'batches': [{'task': 'app', 'files': ['a.py']}]}, 'repair_round': 1, 'recovery_used': recovery}, result)
        if not recovery:
            assert decision == {'state': 'editing', 'recovery_used': True}
        else:
            assert decision['state'] == 'candidate' and decision['stop_limit'] == 'input_budget' and 'never ran' in decision['reason']
    from coder_policy7_evidence import DURABLE_LIMITS
    assert 'input_budget' not in DURABLE_LIMITS   # Continue stays available after raising the Settings limits


def test_sweep_cpp_spent_allowance_during_repair_is_resumable_not_no_progress():
    # Sweep C++ (2026-09-25): the repair retry ran (input budget fine) and hit the model-call allowance at 91 calls;
    # decide() then saw unchanged source with the same signature and parked a DURABLE no_progress, disabling Continue.
    from coder_policy7_evidence import acceptance, decide, DURABLE_LIMITS
    outcomes = [{'id': 'o1', 'text': 'det works', 'evidence_types': ['behavior'], 'component': '.'}]
    checks = [{'id': '.:test:0', 'origin': 'project', 'passed': False, 'is_test': True, 'cwd': '.', 'phase': 'test', 'exit_code': 1,
               'classification': 'application_defect', 'revision_id': 'r1', 'execution_id': 'op:0', 'log_tail': 'FAILED det',
               'source_bindings': [{'path': 'src/main.cpp', 'sha256': 'a' * 64}]}]
    summary = acceptance(outcomes, checks, {}, 'r1', request='matrix tool')
    base = {'checks': checks, 'brief': {'outcomes': outcomes, 'batches': [{'task': 'build', 'files': ['src/main.cpp']}]}, 'repair_round': 1,
            'unchanged_source': True, 'revision_id': 'r1'}
    signature = decide({**base, 'last_patch': {'category': 'source_changed'}}, summary, {})['signature']
    stuck = decide({**base, 'failure_signature': signature, 'last_patch': {'category': 'no_op', 'agent_finished': True}}, summary, {})
    assert stuck['action'] == 'candidate' and stuck['limit'] == 'no_progress' and 'no_progress' in DURABLE_LIMITS
    spent = decide({**base, 'failure_signature': signature, 'last_patch': {'category': 'allowance', 'allowance_spent': True, 'changed': []}}, summary, {})
    assert spent['action'] == 'candidate' and spent['limit'] == 'model_calls' and 'model_calls' not in DURABLE_LIMITS
    assert 'Continue grants' in spent['reason']


def test_sweep_kanban_repair_prompt_is_bounded_to_the_input_budget(tmp_path):
    # Sweep Kanban (2026-09-25): the round-2 repair prompt exceeded the bridge's input budget at its first call.
    from types import SimpleNamespace
    from coder_policy7_prompt import repair_task, OVERHEAD_TOKENS, TIERS
    from coder_worker_runtime import evidence_catalog
    from context_policy import estimate_tokens
    root = tmp_path / 'project'; root.mkdir()
    for i in range(400):
        (root / f'module_with_a_long_name_{i:03d}.js').write_text('x')
    failures = [{'id': f'workflow:app:{i}', 'layer': 3, 'origin': 'project', 'request_excerpt': 'r' * 300, 'command': 'c' * 300,
                 'trace': ['t' * 240] * 4, 'reason': 'w' * 400, 'owning_files': ['a.js'] * 6} for i in range(40)]
    payload = {'round_id': 2, 'revision_id': 'r2', 'original_task': 'Build a kanban board ' * 40, 'builder_guidance': 'g' * 6000,
               'brief': {'outcomes': [], 'batches': []}, 'evidence': {'failures': failures}}
    policy = SimpleNamespace(input_budget=28979)
    from coder_policy7_prompt import HEADROOM_TOKENS
    task, tier = repair_task(payload, SimpleNamespace(root=root), payload['evidence'], '/evidence.json', policy, catalog=evidence_catalog)
    assert tier > 0 and estimate_tokens(task) <= policy.input_budget - OVERHEAD_TOKENS - HEADROOM_TOKENS
    assert 'Original request:' in task and 'g' * 6000 in task and 'Focused defects:' in task and '… (400 files)' in task
    assert '"failures": {"items": []' in task   # a repair round's catalog carries the brief only; failures live in the guidance
    small = {**payload, 'round_id': 0, 'evidence': {'failures': failures[:2]}, 'builder_guidance': 'short'}
    task, tier = repair_task(small, SimpleNamespace(root=root), small['evidence'], '/e.json', policy, catalog=evidence_catalog)
    assert tier == 0 and len(TIERS) == 4
    # v2 Kanban (2026-09-25): a typical repair message (24 KB) must leave HEADROOM for the session's own reads.
    typical = {**payload, 'builder_guidance': 'g' * 6600, 'evidence': {'failures': failures[:4]}, 'original_task': 'k' * 2000}
    task, tier = repair_task(typical, SimpleNamespace(root=root), typical['evidence'], '/e.json', policy, catalog=evidence_catalog)
    assert tier >= 1 and estimate_tokens(task) <= policy.input_budget - OVERHEAD_TOKENS - HEADROOM_TOKENS


def test_sweep_kanban_input_budget_stop_is_resumable_not_no_progress():
    from coder_policy7_evidence import acceptance, decide, DURABLE_LIMITS
    outcomes = [{'id': 'o1', 'text': 'drag works', 'evidence_types': ['behavior'], 'component': '.'}]
    checks = [{'id': 'workflow:app:drag', 'origin': 'project', 'passed': False, 'phase': 'launch', 'classification': 'application_defect',
               'revision_id': 'r1', 'execution_id': 'op:0', 'log_tail': 'silent drag'}]
    summary = acceptance(outcomes, checks, {}, 'r1', request='kanban')
    job = {'checks': checks, 'brief': {'outcomes': outcomes, 'batches': [{'task': 'b', 'files': ['app.js']}]}, 'repair_round': 1,
           'unchanged_source': True, 'revision_id': 'r1', 'editor_stopped': 'The builder prompt for this round exceeds the configured input budget, so the model never ran.',
           'last_patch': {'category': 'input_budget', 'input_budget': True, 'changed': []}}
    decision = decide(job, summary, {})
    assert decision['action'] == 'candidate' and decision['limit'] == 'input_budget' and 'input_budget' not in DURABLE_LIMITS
    assert 'never ran' in decision['reason']
    # verify-kanban (2026-09-25): an overflow AFTER the session wrote files is not "never ran"; repair rounds stay available.
    worked = {**job, 'unchanged_source': False, 'editor_stopped': '', 'last_patch': {'category': 'source_changed', 'input_budget': True, 'changed': ['public/app.js']}}
    assert decide(worked, summary, {})['action'] == 'repair'
    from coder_policy7_ops import edit_transition
    sdk = {'builder': 'sdk', 'brief': {'batches': [{'task': 'b', 'files': ['app.js']}]}, 'repair_round': 0, 'build_continuations': 1}
    assert edit_transition(sdk, {'category': 'source_changed', 'input_budget': True, 'changed': ['public/app.js']})['state'] == 'checking'
    assert edit_transition(sdk, {'category': 'input_budget', 'input_budget': True, 'changed': []}) == {'state': 'editing', 'recovery_used': True}


def test_pilotB_duplicated_check_rows_make_one_packet_entry_each():
    job = {'brief': {'outcomes': []}, 'repair_targets': ['app.js']}
    defects = [{'id': 'workflow:app:edit', 'origin': 'project', 'reason': 'dup'}, {'id': 'workflow:app:edit', 'origin': 'project', 'reason': 'dup'},
               {'id': 'skeleton:placeholder', 'origin': 'controller', 'paths': ['tests/api.test.js']}]
    assert [e['id'] for e in packet(job, defects)] == ['skeleton:placeholder', 'workflow:app:edit']


def test_skeleton_is_applied_once_to_an_empty_tree_and_never_to_uploads_or_followups(tmp_path):
    spec = describe('express-sqlite', 'Express + SQLite board')
    assert spec['hash'] == manifest_hash('express-sqlite') and 'server.js' in spec['files'] and not spec['applied']
    written = apply(tmp_path, spec)
    assert set(written) == set(spec['files']) and (tmp_path / 'database.js').is_file()
    assert apply(tmp_path, spec) == []                       # second application writes nothing
    assert apply(tmp_path / 'other', {**spec, 'hash': 'stale'}) == []   # a deploy drift never writes a different skeleton
    # The builder applies it only for round 0, pass 0, on an empty tree.
    from coder_policy7_builder import sdk_build
    from tests.test_daedalus_policy7 import fixture
    for index, job in enumerate(({'skeleton': spec, 'repair_round': 1}, {'skeleton': spec, 'build_continuations': 1}, {'skeleton': {**spec, 'applied': True}})):
        (tmp_path / f'case{index}').mkdir()
        repository, _ = fixture(tmp_path / f'case{index}', {})
        store = FakeStore(tmp_path / 'state'); store.update = lambda *a, **k: None
        result = sdk_build(store, 'op', repository, {'brief': {'outcomes': [], 'batches': []}, **job},
                           {'settings': {'daedalus_exclude_dirs': []}, 'revision_id': 'r0'},
                           runner=lambda store, op, repo: {'agent_finished': True})
        assert not result['skeleton_applied'] and not (repository.root / 'server.js').exists()


def test_skeleton_placeholder_files_block_until_replaced(tmp_path):
    spec = describe('static-web', 'expense tracker in index.html')
    apply(tmp_path, spec)
    row = placeholder_row(tmp_path, spec, 'r1', 'op')
    assert not row['passed'] and set(row['paths']) == {'app.js', 'tests/test_browser.py'} and row['evidence_types'] == []
    assert row['classification'] == 'application_defect' and 'implement none of the requested behavior' in row['reason'].lower()
    (tmp_path / 'app.js').write_text('document.querySelector("#add-expense").onclick = () => {};\n')
    assert placeholder_row(tmp_path, spec, 'r1', 'op')['paths'] == ['tests/test_browser.py']
    (tmp_path / 'tests' / 'test_browser.py').write_text('assert True\n')
    assert placeholder_row(tmp_path, spec, 'r1', 'op')['passed']
    # The controller's repair targets name exactly the unchanged files for a skeleton defect.
    from coder_policy7_controller import repair_targets
    job = {'brief': {'batches': [{'files': ['index.html']}]}, 'last_patch': {'source_hashes': {'app.js': 'a', 'index.html': 'b'}}}
    assert repair_targets(job, [{'id': 'skeleton:placeholder', 'paths': ['app.js', 'tests/test_browser.py']}]) == ['app.js']
    text = skeleton_guidance(spec)
    assert 'implements NONE of the requested behavior' in text and 'app.js' in text


def test_frozen7_kanban_server_only_uses_names_database_js_exports():
    # frozen-7 Kanban: server.js called getAllTasks on a module that exported a raw sqlite3 database.
    import re
    files = SKELETONS['express-sqlite']['files']
    exported = set(re.search(r'module\.exports = \{([^}]+)\}', files['database.js']).group(1).replace(' ', '').split(','))
    imported = set(re.search(r"const \{([^}]+)\} = require\('\./database'\)", files['server.js']).group(1).replace(' ', '').split(','))
    assert imported <= exported and 'init' in imported


def test_frozen6_medium_skeleton_declares_no_stdlib_pip_dependency_and_serves_the_built_frontend():
    files = SKELETONS['react-fastapi-sqlite']['files']
    assert 'sqlite3' not in files['requirements.txt'] and 'fastapi' in files['requirements.txt']
    assert 'dev' not in json.loads(files['frontend/package.json'])['scripts']     # the frontend is built, never launched as a service
    contract = json.loads(files['.daedalus-run.json'])
    assert contract['dependencies'] == {'.': ['frontend']} and '{port}' in contract['services'][0]['command']
    assert 'python3 -m uvicorn app:app --host 127.0.0.1 --port 8000' in files['README.md']
    assert "StaticFiles(directory=DIST, html=True)" in files['app.py'] and 'APP_DB_PATH' in files['db.py']


def test_skeleton_identity_is_an_operation_input_and_stage_progress_is_not():
    from coder_policy7_controller import operation_inputs
    job = {'brief': {}, 'skeleton': {'id': 'static-web', 'hash': 'h'}, 'repair_packet': [{'id': 'x'}], 'stage_progress': {'phase': 2},
           'calls_used': 5, 'worker_contact_at': 'now'}
    inputs = operation_inputs(job)
    assert inputs['skeleton'] == job['skeleton'] and inputs['repair_packet'] == job['repair_packet']
    assert 'stage_progress' not in inputs and 'calls_used' not in inputs


def test_live7_apply_settings_works_inside_the_worker_bundle_without_backend_config(monkeypatch):
    # Production 2026-09-25: every policy-7 build failed with "IsolationUnavailable ... No module named 'config'"
    # because the sandboxed SDK child calls context_policy.apply_settings and config.py is not in the worker bundle.
    import sys
    import context_policy
    monkeypatch.setitem(sys.modules, 'config', None)   # `import config` now raises ImportError, as on the worker
    monkeypatch.setattr(context_policy, '_FALLBACK_STORE', None)
    context_policy.apply_settings({**context_policy.DEFAULTS, 'context_compaction': 'on', 'openhands_num_ctx': 65536})
    values = context_policy.runtime_settings()
    assert values['openhands_num_ctx'] == 65536 and values['context_compaction'] == 'on'


def test_worker_bundle_modules_never_import_backend_only_modules_unguarded():
    # A backend module the worker imports but does not receive fails at operation time, not at startup.
    import ast, sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    deploy_monitor = pytest.importorskip('deploy_monitor')   # repo root only; the Codebox dev base ships backend/ alone
    bundle = {Path(p).name for p in deploy_monitor.WORKER_FILES | deploy_monitor.WORKER_SHARED if p.endswith('.py')}
    backend_only = {'config', 'database', 'main', 'tools', 'events', 'rag', 'coder_jobs', 'coder_policy7_controller', 'coder_presentation'} - {n[:-3] for n in bundle}
    offenders = []
    for name in sorted(bundle):
        tree = ast.parse((Path(__file__).resolve().parents[1] / name).read_text())
        guarded = {id(n) for t in ast.walk(tree) if isinstance(t, ast.Try) for n in ast.walk(t) if n is not t}
        for node in ast.walk(tree):
            targets = [a.name.split('.')[0] for a in node.names] if isinstance(node, ast.Import) else [(node.module or '').split('.')[0]] if isinstance(node, ast.ImportFrom) else []
            if any(t in backend_only for t in targets) and id(node) not in guarded:
                offenders.append(f'{name}:{node.lineno} imports {targets}')
    assert offenders == [], offenders


def test_release_runner_refuses_an_uninstalled_model_with_a_clear_error():
    source = (Path(__file__).resolve().parents[1] / 'evals' / 'run_policy7_release.py').read_text()
    assert 'is not installed at' in source and "next(m for m in installed" not in source
    assert "'model':model,'model_digest'" in source


# --- Codebox only: each skeleton installs, builds, launches and runs its placeholder tests -----------------

@pytest.mark.skipif(not shutil.which('bwrap') or not shutil.which('dotnet') and not Path('/root/.dotnet/dotnet').exists(),
                    reason='needs the Codebox toolchains and bubblewrap')
@pytest.mark.parametrize('skeleton_id', sorted(SKELETONS))
def test_each_skeleton_installs_builds_launches_and_only_its_placeholder_row_fails(tmp_path, skeleton_id):
    from tests.test_daedalus_policy7 import fixture
    from coder_project_runtime import contract, run_revision
    from coder_policy7_evidence import blocking
    from coder_skeletons import files_for
    spec = describe(skeleton_id, 'solution named DemoApp with a console app project `DemoApp` and an xUnit test project `DemoApp.Tests`', 'Demo App')
    repository, store = fixture(tmp_path, files_for(skeleton_id, spec['names']))
    profile = contract(repository)
    result = run_revision(store, 'check', repository, profile, project_id='skeleton-' + skeleton_id)
    failing = [r for r in result['checks'] if blocking(r)]
    assert failing == [], json.dumps([{k: r.get(k) for k in ('id', 'command', 'log_tail')} for r in failing], indent=1)[:6000]
    assert any(r.get('is_test') and r['passed'] for r in result['checks']), 'the placeholder tests must execute'
    assert not placeholder_row(result['workspace'], spec, 'r', 'check')['passed']


# --- sweep3: negative and output-aware probes, browser-guard repair targets ----------------------

RUST_TASK = ('Build a Rust command-line tool with Cargo, package name base64tool, implementing standard Base64 without external '
             'crates. `base64tool encode <text>` prints the Base64 encoding and `base64tool decode <base64>` prints the decoded text. '
             'Invalid Base64 input prints an error to stderr and exits with code 2. Put the codec in src/lib.rs, add integration '
             'tests under tests/ run by `cargo test`, and include a README.')
NODE_TASK = ('Build a Node.js command-line tool named mdtoc with no runtime dependencies. Running `node mdtoc.js <file.md>` prints a '
             'table of contents for the Markdown headings: one line per heading as two spaces of indentation per level below 1, then '
             '`- [Title](#slug)`, where the slug is the lowercased title with spaces replaced by hyphens and characters other than '
             'letters, digits and hyphens removed. Support `--max-depth N` to ignore deeper headings. A missing file prints an error to '
             'stderr and exits with code 2. Include tests run by `npm test` using node:test, and a README.')
GOOD_TOC = '- [Title](#title)\n  - [Section](#section)'


def _cargo_project(root):
    (root / 'Cargo.toml').write_text('[package]\nname = "base64tool"\nversion = "0.1.0"\n')
    (root / 'target' / 'debug').mkdir(parents=True)
    (root / 'target' / 'debug' / 'base64tool').write_text('')
    (root / 'target' / 'debug' / 'base64tool').chmod(0o755)   # launcher() locates executables only


def test_sweep3_rust_invalid_input_wording_becomes_negative_probes(tmp_path):
    # sweep3-c: the decoder accepted `====` (printed a NUL byte, exit 0) and no test, audit or probe ever asked.
    from coder_request_probes import wording_rules
    assert wording_rules(RUST_TASK) == [{'kind': 'invalid', 'what': 'base64', 'code': 2, 'stderr_required': True,
                                         'sentence': 'Invalid Base64 input prints an error to stderr and exits with code 2.'}]
    _cargo_project(tmp_path)
    mode, calls = {'strict': False, 'quiet': False}, []
    def runner(store, op_id, root, argv, *, project_id, log, stderr):
        calls.append(argv)
        if 'encode' in argv:
            return 0, 'aGVsbG8gd29ybGQ=', ''
        if argv[-1] == 'aGVsbG8gd29ybGQ=':
            return 0, 'hello world', ''
        if mode['strict']:
            return 2, '', '' if mode['quiet'] else 'error: invalid base64 input'
        return 0, 'h', ''   # lenient decoder: garbage in, a byte out
    profile = {'profiles': [{'toolchains': ['cargo']}]}
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, RUST_TASK, 'r1', project_id='p', runner=runner)
    positives = [r for r in rows if r['id'].startswith('probe:command:')]
    negatives = [r for r in rows if r['id'].startswith('probe:invalid:')]
    assert [r['passed'] for r in positives] == [True, True] and not [r for r in rows if r['id'].startswith('probe:missing')]
    decode = positives[1]['id'].rsplit(':', 1)[-1]   # the decode invocation's own index
    assert [r['id'] for r in negatives] == [f'probe:invalid:{decode}:{k}' for k in (1, 2, 3, 4)]
    assert [c[-1] for c in calls[-4:]] == ['%%%', '====', 'a===', 'A']
    for row in negatives:
        assert not row['passed'] and row['classification'] == 'application_defect' and row['origin'] == 'controller'
        assert row['evidence_types'] == [] and row['outcomes'] == [] and layer(row) == 2
        assert row['request_excerpt'] == 'Invalid Base64 input prints an error to stderr and exits with code 2.'
        assert row['expected'] == 'exit 2, empty stdout, a message on stderr' and row['actual'].startswith("exit 0, stdout 'h'")
        assert row['reason'].startswith('Diagnostic probe (never acceptance evidence)') and '<base64>=' in row['probe_input']
    mode['strict'] = True
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, RUST_TASK, 'r1', project_id='p', runner=runner)
    assert all(r['passed'] for r in rows) and len([r for r in rows if r['id'].startswith('probe:invalid:')]) == 4
    mode['quiet'] = True   # exits 2 silently: the request asked for a message on stderr
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, RUST_TASK, 'r1', project_id='p', runner=runner)
    assert all(not r['passed'] and r['actual'].endswith('stderr empty') for r in rows if r['id'].startswith('probe:invalid:'))


def test_sweep3_node_missing_file_wording_becomes_a_missing_probe(tmp_path):
    (tmp_path / 'mdtoc.js').write_text('')
    mode, calls = {'strict': False}, []
    def runner(store, op_id, root, argv, *, project_id, log, stderr):
        calls.append(argv)
        if Path(argv[2]).is_file():
            return 0, GOOD_TOC, ''
        return (2, '', 'Error: ENOENT: no such file') if mode['strict'] else (0, '', '')
    profile = {'profiles': [{'toolchains': ['node']}]}
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, NODE_TASK, 'r1', project_id='p', runner=runner)
    by_id = {r['id']: r for r in rows}
    assert list(by_id) == ['probe:command:1', 'probe:command:1:flag:max-depth', 'probe:missing:1']
    assert by_id['probe:command:1']['passed'] and by_id['probe:command:1:flag:max-depth']['passed']
    assert calls[1][-2:] == ['--max-depth', '2']
    missing = by_id['probe:missing:1']
    assert not Path(calls[-1][2]).exists() and 'missing-input.md' in calls[-1][2] and 'does not exist' in missing['probe_input']
    assert not missing['passed'] and missing['classification'] == 'application_defect' and missing['evidence_types'] == []
    assert missing['request_excerpt'] == 'A missing file prints an error to stderr and exits with code 2.'
    assert missing['expected'] == 'exit 2, empty stdout, a message on stderr' and missing['actual'] == 'exit 0, empty stdout, stderr empty'
    mode['strict'] = True
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, NODE_TASK, 'r1', project_id='p', runner=runner)
    assert all(r['passed'] for r in rows) and len(rows) == 3


def test_negative_probes_are_skipped_without_a_binding_or_a_startable_program(tmp_path):
    import os
    # (a) "invalid expressions" names a class the controller has no invalid value for: no row, never a guess.
    task = 'Build `rpncalc <expression>` which prints the result. Invalid expressions print an error to stderr and exit with code 2.'
    (tmp_path / 'build').mkdir(); binary = tmp_path / 'build' / 'rpncalc'; binary.write_text(''); os.chmod(binary, 0o755)
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, {'profiles': [{'toolchains': ['cmake']}]}, task, 'r1',
                        project_id='p', runner=lambda *a, **k: (0, '7', ''))
    assert [r['id'] for r in rows] == ['probe:command:1'] and rows[0]['passed']
    # (b) the program cannot start: the positive row is the foundation defect; bad-input rows would only repeat it.
    other = tmp_path / 'other'; other.mkdir(); _cargo_project(other)
    rows = command_rows(FakeStore(other / 's'), 'op', other, {'profiles': [{'toolchains': ['cargo']}]}, RUST_TASK, 'r1', project_id='p',
                        runner=lambda *a, **k: (127, 'bash: target/debug/base64tool: No such file or directory', ''))
    assert [r['id'] for r in rows] == ['probe:command:1', 'probe:command:2'] and all(layer(r) == 1 for r in rows)
    # (c) nothing to launch: nothing at all.
    empty = tmp_path / 'empty'; empty.mkdir()
    assert command_rows(FakeStore(empty / 's'), 'op', empty, {'profiles': [{'toolchains': ['cargo']}]}, RUST_TASK, 'r1', project_id='p',
                        runner=lambda *a, **k: (0, '', '')) == []


def test_sweep3_node_probe_fails_on_empty_output_and_on_a_fenced_heading(tmp_path):
    # sweep3-c p1 printed nothing and passed; p2 listed `- [not a heading](#not-a-heading)` from inside the fixture's
    # fenced block and passed, because the probe judged exit 0 alone.
    from coder_request_probes import output_faults
    (tmp_path / 'mdtoc.js').write_text('')
    profile = {'profiles': [{'toolchains': ['node']}]}
    def rows_for(output):
        return command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, NODE_TASK, 'r1', project_id='p',
                            runner=lambda store, op, root, argv, *, project_id, log: (0, output))
    silent = rows_for('')[0]
    assert not silent['passed'] and silent['classification'] == 'application_defect' and silent['exit_code'] == 0
    assert silent['expected'] == 'output on stdout' and silent['actual'].startswith('it printed nothing') and 'exited 0 but' in silent['reason']
    fenced = rows_for(GOOD_TOC + '\n- [not a heading](#not-a-heading)')[0]
    assert not fenced['passed'] and 'not a heading' in fenced['expected'] and 'fenced code block' in fenced['expected'] and fenced['actual'].startswith('it is listed')
    assert rows_for(GOOD_TOC)[0]['passed']
    missing_section = rows_for('- [Title](#title)')[0]
    assert not missing_section['passed'] and "'Section'" in missing_section['expected']
    # The rule is tied to the request's purpose: a converter that prints fenced code is not judged by it.
    converter = 'Build `md2html <file.md>` which prints the document as HTML.'
    assert output_faults('`md2html <file.md>` prints the document as HTML.', converter, '<pre># not a heading</pre>', ['md']) == []
    assert output_faults('`md2html <file.md>` prints the document as HTML.', converter, '', ['md']) == [('output on stdout', 'it printed nothing')]


def test_probe_runner_protocol_reads_stderr_from_the_side_file(tmp_path):
    from coder_request_probes import _run
    import inspect
    from coder_project_runtime import run_probe
    assert 'stderr' in inspect.signature(run_probe).parameters   # the real runner splits the streams on request
    def splitting(store, op_id, root, argv, *, project_id, log, stderr):
        Path(stderr).write_text('boom'); return 2, 'out'
    def legacy(store, op_id, root, argv, *, project_id, log):
        return 2, 'merged'
    assert _run(splitting, None, 'op', tmp_path, ['x'], 'p', tmp_path / 'l', tmp_path / 'fx' / 'e') == (2, 'out', 'boom')
    assert _run(legacy, None, 'op', tmp_path, ['x'], 'p', tmp_path / 'l', tmp_path / 'fx' / 'e2') == (2, 'merged', '')
    # `go run` wraps the child's exit code (always 1): the program's own code is what a negative probe judges.
    wrapped = lambda *a, **k: (1, '', 'open nope.log: no such file or directory\nexit status 2\n')
    assert _run(wrapped, None, 'op', tmp_path, ['go', 'run', '.', 'nope.log'], 'p', tmp_path / 'l', tmp_path / 'fx' / 'e3')[0] == 2
    assert _run(wrapped, None, 'op', tmp_path, ['./logsummary', 'nope.log'], 'p', tmp_path / 'l', tmp_path / 'fx' / 'e4')[0] == 1


def test_sweep3_kanban_workflow_defects_own_the_page_script_and_server():
    # sweep3-c p1: skeleton:placeholder + three failing workflow:app:* rows, and the repair was narrowed to tests/api.test.js.
    from coder_repair_packet import repair_targets
    known = ['server.js', 'database.js', 'package.json', 'public/index.html', 'public/style.css', 'public/app.js',
             'tests/api.test.js', '.daedalus-run.json', 'README.md']
    job = {'brief': {'outcomes': [], 'batches': [{'files': known}]}, 'last_patch': {'source_hashes': {k: 'h' for k in known}}, 'revision_id': 'r3'}
    skeleton = {'id': 'skeleton:placeholder', 'origin': 'controller', 'paths': ['tests/api.test.js'], 'reason': 'placeholder unchanged'}
    workflows = [{'id': f'workflow:app:{step}', 'origin': 'project', 'cwd': '.', 'command': f'{step} workflow in headless Chromium',
                  'exit_code': None, 'log_tail': f'the {step} step changed nothing', 'reason': 'x'} for step in ('edit', 'drag', 'delete')]
    targets = repair_targets(job, [skeleton, *workflows])
    assert targets[0] == 'tests/api.test.js' and {'public/app.js', 'public/index.html', 'server.js'} <= set(targets) and 'database.js' not in targets
    assert repair_targets(job, [skeleton]) == ['tests/api.test.js']
    assert repair_targets(job, [workflows[1]]) == ['public/app.js', 'public/index.html', 'server.js']
    assert repair_targets(job, [{'id': 'interface:labels:app', 'origin': 'controller', 'reason': 'no label'}])[:2] == ['public/app.js', 'public/index.html']
    entries = packet(job, [skeleton, *workflows], targets=lambda d: repair_targets(job, [d]))
    by_id = {e['id']: e for e in entries}
    assert by_id['skeleton:placeholder']['owning_files'] == ['tests/api.test.js'] and by_id['workflow:app:drag']['owning_files'][0] == 'public/app.js'


def test_repair_packet_puts_priority_entries_first_and_keeps_a_defects_own_expected_actual():
    from coder_repair_packet import _expected_actual
    job = {'brief': {'outcomes': []}, 'repair_targets': ['server.js', 'tests/api.test.js'], 'revision_id': 'r4'}
    launch = {'id': 'launch:app', 'origin': 'project', 'phase': 'launch', 'command': 'node server.js', 'log_tail': 'EADDRINUSE'}
    regressed = {'id': '.:test:0', 'origin': 'project', 'is_test': True, 'phase': 'test', 'priority': True, 'command': 'npm test',
                 'expected': 'the retained suite keeps passing', 'actual': '1 failed (was 0)', 'reason': 'your previous repair broke it'}
    probe = {'id': 'probe:invalid:2:2', 'origin': 'controller', 'exit_code': 0, 'expected': 'exit 2, empty stdout', 'actual': "exit 0, stdout '\\x00'"}
    entries = packet(job, [launch, regressed, probe])
    assert [e['id'] for e in entries] == ['.:test:0', 'launch:app', 'probe:invalid:2:2'] and entries[0]['priority'] and not entries[1]['priority']
    assert entries[0]['expected'] == 'the retained suite keeps passing' and entries[2]['actual'] == "exit 0, stdout '\\x00'"
    assert _expected_actual(probe) == ('exit 2, empty stdout', "exit 0, stdout '\\x00'") and _expected_actual(launch)[0].startswith('the service')
    text = render_packet(entries)
    assert text.index('.:test:0') < text.index('launch:app') and '[FIRST - core behavior] .:test:0' in text
    assert 'expected: the retained suite keeps passing / actual: 1 failed (was 0)' in text


def test_sweep3_medium_server_log_is_runtime_state_not_source(tmp_path):
    from coder_project_runtime import runtime_data_path
    (tmp_path / 'server.log').write_text('listening'); (tmp_path / 'server.js').write_text('x')
    (tmp_path / 'store.bin').write_bytes(b'SQLite format 3\x00' + b'\0' * 32)
    assert runtime_data_path(tmp_path, 'server.log') and runtime_data_path(tmp_path, 'data/tasks.db') and runtime_data_path(tmp_path, 'store.bin')
    assert not runtime_data_path(tmp_path, 'server.js') and not runtime_data_path(tmp_path, 'public/app.js') and not runtime_data_path(tmp_path, 'nope.txt')


def test_fix4_rust_probes_resolve_the_program_inside_the_cargo_package_directory(tmp_path):
    # fix4-c pass1 (2026-09-26): the Cargo package lived in base64tool/, Cargo.toml was looked up at the root only
    # and every probe was silently skipped; the model's padding tests failed while no probe ever ran.
    from coder_request_probes import launcher, requested_commands, package_roots
    pkg = tmp_path / 'base64tool'
    (pkg / 'src').mkdir(parents=True); (pkg / 'target' / 'debug').mkdir(parents=True)
    (pkg / 'Cargo.toml').write_text('[package]\nname = "base64tool"\nversion = "0.1.0"\n')
    binary = pkg / 'target' / 'debug' / 'base64tool'; binary.write_text('#!/bin/sh\n'); binary.chmod(0o755)
    profile = {'profiles': [{'cwd': 'base64tool', 'toolchains': ['cargo']}]}
    assert [b.name for b in package_roots(profile, tmp_path)] == [tmp_path.name, 'base64tool']
    seen = []
    def runner(store, op_id, root, argv, *, project_id, log, stderr, cwd='.'):
        seen.append((cwd, argv[-1]))
        if 'encode' in argv or argv[-1] == 'aGVsbG8gd29ybGQ=':
            return 0, 'aGVsbG8gd29ybGQ=' if 'encode' in argv else 'hello world', ''
        return 2, '', 'error: invalid base64 input'
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, RUST_TASK, 'r1', project_id='p', runner=runner)
    ids = [r['id'] for r in rows]
    assert 'probe:command:1' in ids and 'probe:command:2' in ids and any(i.startswith('probe:invalid:') for i in ids)
    assert all(r['cwd'] == 'base64tool' and r['passed'] for r in rows), [(r['id'], r['cwd'], r['passed']) for r in rows]
    assert all(cwd == 'base64tool' for cwd, _ in seen) and all(r['command'].startswith(str(binary)) for r in rows)
    # A Node script in a package directory runs there; a script that exists nowhere keeps the root invocation
    # so the probe fails honestly (frozen-7: `python3 wordfreq.py` delivered as a package).
    (tmp_path / 'pkg').mkdir(); (tmp_path / 'pkg' / 'mdtoc.js').write_text('')
    node_profile = {'profiles': [{'cwd': 'pkg', 'toolchains': ['node']}]}
    spec = requested_commands('Run `node mdtoc.js <file.md>` to print the TOC.')[0]
    assert launcher(node_profile, tmp_path, spec) == ['node', 'mdtoc.js', '<file.md>'] and spec['cwd'] == 'pkg'
    spec = requested_commands('Run `python3 wordfreq.py <file>` to count words.')[0]
    assert launcher(node_profile, tmp_path, spec) == ['python3', 'wordfreq.py', '<file>'] and spec['cwd'] == '.'


def test_fix4_node_markdown_fixture_puts_an_info_string_fence_before_a_heading():
    # fix4-c pass1-node-mdtoc: `\`\`\`text` was never closed and `## Next` vanished; the old fixture ended with its fence.
    from coder_request_probes import FIXTURES, output_faults
    text = FIXTURES['md']['text']
    assert text.index('```text') < text.index('## Section') and text.count('```') == 2
    task = 'Print a table of contents for the Markdown headings.'
    assert output_faults('`node mdtoc.js <file.md>` prints the TOC.', task, '- [Title](#title)\n', ['md']) == \
        [("the output mentions 'Section' (a heading of the input file)", 'it does not')]
    assert output_faults('`node mdtoc.js <file.md>` prints the TOC.', task, '- [Title](#title)\n  - [Section](#section)\n', ['md']) == []


def test_fix4_node_requested_output_shape_is_checked_on_the_positive_probe():
    # fix4-c pass2-node-mdtoc (2026-09-26): `# Intro\n  ## Getting Started!` was printed instead of `- [Title](#slug)`
    # lines; the model's five tests and the audit agreed with it and the probe passed on fixture content alone.
    from coder_request_probes import output_templates, output_faults
    node = ('Running `node mdtoc.js <file.md>` prints a table of contents for the Markdown headings: one line per heading as '
            'two spaces of indentation per level below 1, then `- [Title](#slug)`, where the slug is the lowercased title.')
    [(template, shape)] = output_templates(node)
    assert template == '- [Title](#slug)' and shape.match('  - [Getting Started!](#getting-started)') and not shape.match('# Intro')
    bad = '# Intro\n  ## Getting Started!\n    ### Deep Dive\n'
    faults = output_faults(node, node, bad, [])
    assert faults == [('every output line shaped like `- [Title](#slug)` (the request states that format)', "a line is '# Intro'")]
    assert output_faults(node, node, '- [Intro](#intro)\n  - [Getting Started!](#getting-started)\n', []) == []
    go = 'Running `go run . <logfile>` reads a log file and prints one line per level as `LEVEL count`, sorted by level.'
    [(template, shape)] = output_templates(go)
    assert template == 'LEVEL count' and output_faults(go, go, 'ERROR 1\nINFO 3\nWARN 1\n', []) == []
    assert output_faults(go, go, 'ERROR: 1 line\n', []) != []
    # flags, long prose spans and sentences without "line" wording are never templates
    assert output_templates('Support `--top N` (default 10) to limit the number of lines and `--json` to print JSON.') == []
    assert output_templates('`matrixtool transpose "1,2;3,4"` prints the transposed matrix in the same `a,b;c,d` format.') == []


def test_fix5_node_probe_row_carries_the_input_content_and_observed_output_into_the_packet(tmp_path):
    # fix5-c pass1-node-mdtoc (2026-09-27): the packet said "a small Markdown file ... the output mentions 'Section': it
    # does not"; the builder cannot read the execution copy, saw nothing to fix and parked the job on no_progress.
    from coder_repair_packet import packet, render
    (tmp_path / 'mdtoc.js').write_text('')
    def runner(store, op_id, root, argv, *, project_id, log, stderr):
        return 0, '- [Title](#title)\n', ''
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, {'profiles': [{'toolchains': ['node']}]},
                        'Running `node mdtoc.js <file.md>` prints the Markdown headings, one line per heading.', 'r1', project_id='p', runner=runner)
    row = next(r for r in rows if r['id'] == 'probe:command:1')
    assert not row['passed'] and '```text' in row['probe_input'] and '## Section' in row['probe_input']
    assert row['actual'].endswith("observed stdout '- [Title](#title)'")
    job = {'brief': {'batches': [{'files': ['mdtoc.js']}]}, 'last_patch': {'source_hashes': {'mdtoc.js': 'x'}}, 'revision_id': 'r1'}
    text = render(packet(job, [row]))
    assert '```text' in text and "observed stdout '- [Title](#title)'" in text


def test_fix5_rust_encode_of_the_controller_input_must_be_the_standard_base64(tmp_path):
    # fix5-c pass2-rust-base64tool (2026-09-27): encode printed `aGVsbG8gd29ybGQA` (an `A` instead of `=` padding) and the
    # positive probe passed on exit 0; decode printed `hello world\x00`. The controller knows its own input's encoding.
    from coder_request_probes import known_transform
    assert known_transform(RUST_TASK, ['x', 'encode', 'hello world']) == 'aGVsbG8gd29ybGQ='
    assert known_transform(RUST_TASK, ['x', 'decode', 'aGVsbG8gd29ybGQ=']) == 'hello world'
    assert known_transform('Build a wordfreq tool.', ['x', 'encode', 'hello world']) is None
    _cargo_project(tmp_path)
    def runner(store, op_id, root, argv, *, project_id, log, stderr):
        if 'encode' in argv:
            return 0, 'aGVsbG8gd29ybGQA\n', ''
        if argv[-1] == 'aGVsbG8gd29ybGQ=':
            return 0, 'hello world\x00\n', ''
        return 2, '', 'error'
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, {'profiles': [{'toolchains': ['cargo']}]}, RUST_TASK, 'r1', project_id='p', runner=runner)
    positives = [r for r in rows if r['id'] in ('probe:command:1', 'probe:command:2')]
    assert [r['passed'] for r in positives] == [False, False]
    assert "stdout exactly 'aGVsbG8gd29ybGQ='" in positives[0]['expected'] and "it printed 'aGVsbG8gd29ybGQA'" in positives[0]['actual']
    assert "stdout exactly 'hello world'" in positives[1]['expected']
