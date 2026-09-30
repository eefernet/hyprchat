"""Requested accessible labels and button names: parser, guard row, decide routing, skeleton markers.

The medium board (sweep3, 2026-09-26) failed every independent browser verdict at
get_by_label('Project name'): its inputs were never associated with the label text the request spelled
out, no model test or audit used a label, and the workflow rows stayed advisory because no item could be
created through the form. Pure Python, no browser, no server; one test per finding.
"""
from __future__ import annotations

import json
from pathlib import Path

from coder_policy7_evidence import requested_interfaces, decide
from coder_skeletons import SKELETONS, files_for
from coder_workflow_guard import workflow_rows, workflow_steps, column_names
from evals.policy7_fixtures import MEDIUM_TASK

KANBAN_TASK = next(c['task'] for c in json.loads((Path(__file__).resolve().parents[1] / 'evals' / 'build_cases.json').read_text())
                   if c['name'] == 'kanban-live')


def test_sweep3_medium_requested_labels_and_buttons_are_parsed():
    wanted = requested_interfaces(MEDIUM_TASK)
    assert wanted['labels'] == ['Project name', 'Task title', 'Priority', 'Status', 'Search', 'Status filter']
    assert wanted['buttons'] == ['Create project', 'Add task']
    assert wanted['conditional_buttons'] == ['Edit', 'Delete', 'Save task']
    # Existing selector/flag parsing is untouched and the new keys are always present.
    cli = requested_interfaces('print `- [Title](#slug)` and keep #total; support --max-depth N')
    assert cli['selectors'] == ['total'] and cli['flags'] == ['--max-depth']
    assert cli['labels'] == [] and cli['buttons'] == [] and cli['conditional_buttons'] == []
    # Requests that never name a label or button yield nothing: no row can ever be emitted for them.
    for text in (KANBAN_TASK, 'Build a CLI that prints word counts', 'add labels to tasks and filter by label.'):
        wanted = requested_interfaces(text)
        assert wanted['labels'] == [] and wanted['buttons'] == [] and wanted['conditional_buttons'] == [], text[:40]
    # Buttons that exist only once an item does are conditional; a plain button is not.
    assert requested_interfaces('Show Edit and Delete buttons for each task, and Save task when editing.')['conditional_buttons'] == ['Edit', 'Delete', 'Save task']
    assert requested_interfaces('Provide a Delete button that removes the task.')['buttons'] == ['Delete']
    assert requested_interfaces('Kanban with columns Todo, In Progress, Done and a Save button per item.')['conditional_buttons'] == ['Save']
    signup = requested_interfaces('Use labels: Name, Email and buttons Sign up.')
    assert signup['labels'] == ['Name', 'Email'] and signup['buttons'] == ['Sign up']


def test_sweep3_medium_missing_accessible_labels_fail_the_interface_row():
    assert workflow_steps(MEDIUM_TASK) == ['persist', 'edit', 'delete', 'literal']
    seen = {}

    def prober(url, steps, **kw):
        seen.update(kw)
        return {'fault': '', 'writes': [], 'errors': [], 'interfaces': {'missing_labels': ['Project name'], 'missing_buttons': []},
                'steps': {step: {'status': 'advisory', 'detail': 'no item could be created through the page form'} for step in steps}}
    rows = workflow_rows({'id': 'app', 'cwd': '.'}, 'http://127.0.0.1:1', {}, MEDIUM_TASK, prober=prober)
    # The guard receives exactly the names the request spelled out.
    assert seen['interfaces']['labels'] == ['Project name', 'Task title', 'Priority', 'Status', 'Search', 'Status filter']
    assert seen['interfaces']['buttons'] == ['Create project', 'Add task'] and seen['interfaces']['conditional_buttons'] == ['Edit', 'Delete', 'Save task']
    by_id = {r['id']: r for r in rows}
    row = by_id['interface:labels:app']
    assert not row['passed'] and row['classification'] == 'application_defect' and row['origin'] == 'controller'
    assert row['evidence_types'] == [] and row['outcomes'] == [] and row['missing_labels'] == ['Project name']
    assert 'never acceptance evidence' in row['reason'] and 'Project name' in row['reason'] and 'aria-label' in row['reason']
    assert set(by_id) == {'interface:labels:app', 'workflow:app:persist', 'workflow:app:edit', 'workflow:app:delete', 'workflow:app:literal'}
    assert all(by_id[f'workflow:app:{s}']['passed'] for s in ('persist', 'edit', 'delete', 'literal'))   # advisory never fails

    def resolved(url, steps, **kw):
        return {'fault': '', 'writes': [], 'errors': [], 'interfaces': {'missing_labels': [], 'missing_buttons': []},
                'steps': {step: {'status': 'passed', 'detail': 'ok'} for step in steps}}
    rows = workflow_rows({'id': 'app'}, 'http://x', {}, MEDIUM_TASK, prober=resolved)
    assert {r['id']: r['passed'] for r in rows}['interface:labels:app'] and all(r['passed'] for r in rows)
    # A request that names no label or button gets no row at all (the Kanban case).
    rows = workflow_rows({'id': 'app'}, 'http://x', {}, KANBAN_TASK, prober=resolved)
    assert not any(r['id'].startswith('interface:labels') for r in rows) and rows
    # Labels named but no workflow wording: the labels row alone is still worth a browser pass.
    rows = workflow_rows({'id': 'app'}, 'http://x', {}, 'Use accessible labels Name and buttons Go.',
                         prober=lambda url, steps, **kw: {'fault': '', 'writes': [], 'errors': [], 'steps': {},
                                                          'interfaces': {'missing_labels': [], 'missing_buttons': ['Go']}})
    assert [r['id'] for r in rows] == ['interface:labels:app'] and not rows[0]['passed'] and rows[0]['missing_buttons'] == ['Go']
    # A browser that cannot start is an environment fault, never a pass and never a defect.
    down = workflow_rows({'id': 'app'}, 'http://x', {}, MEDIUM_TASK,
                         prober=lambda *a, **k: {'fault': 'Error: browser missing', 'steps': {}, 'writes': [], 'errors': []})
    assert all(r['environment_fault'] and r['classification'] == 'environment' and not r['passed'] for r in down)


def test_requested_interface_rows_classified_unverified_reach_repair():
    # Verified read-only on the sweep3 tree: a failing interface:* row is classified `unverified_interface`,
    # which no branch of decide() routed, so the job parked as a candidate instead of repairing.
    check = {'id': 'interface:#save', 'origin': 'controller', 'passed': False, 'classification': 'unverified_interface',
             'interface_status': 'unresolved', 'evidence_types': [], 'outcomes': [], 'revision_id': 'r1',
             'log_tail': 'The request names #save but no delivered source file defines or handles it.',
             'reason': 'Requested interface #save could not be resolved from source; runtime proof is required'}
    job = {'checks': [check], 'repair_round': 0, 'revision_id': 'r1'}
    result = decide(job, {'outcomes': []}, {})
    assert result['action'] == 'repair', result
    assert [c['id'] for c in result['feedback']] == ['interface:#save']


def test_sweep3_kanban_drag_falls_back_to_the_requested_column_headings():
    assert column_names(KANBAN_TASK) == ['Todo', 'In Progress', 'Done']
    assert column_names('Build a CLI that prints word counts') == []
    seen = {}
    rows = workflow_rows({'id': 'app'}, 'http://x', {}, KANBAN_TASK, prober=lambda url, steps, **kw: (seen.update(kw) or {
        'fault': '', 'writes': [], 'errors': [], 'interfaces': {}, 'steps': {s: {'status': 'passed', 'detail': 'ok'} for s in steps}}))
    assert seen['interfaces']['columns'] == ['Todo', 'In Progress', 'Done'] and rows
    source = (Path(__file__).resolve().parents[1] / 'coder_workflow_guard.py').read_text()
    assert 'ancestor::*[self::section or self::div or self::article][1]' in source


def test_sweep3_skeleton_placeholders_are_unmistakable_and_versioned():
    express = files_for('express-sqlite', {})
    assert express['public/app.js'].splitlines()[0].startswith('// PLACEHOLDER: replace this whole file')
    assert "test('PLACEHOLDER — replace with the requested API tests'" in express['tests/api.test.js']
    react = files_for('react-fastapi-sqlite', {})
    assert react['tests/test_browser.py'].splitlines()[0].startswith('# PLACEHOLDER: replace this whole file')
    assert SKELETONS['express-sqlite']['version'] >= 2 and SKELETONS['react-fastapi-sqlite']['version'] >= 2


def test_fix4_kanban_drag_drops_onto_the_innermost_zone_not_the_outer_column():
    # fix4-c pass1-kanban-live (2026-09-26): `#in-progress-column` matched CONTAINER before the inner `.task-list`
    # that held the drop listener; the synthetic drop reached nothing and the row said "no request was sent"
    # while the app's fault was a partial PUT the server rejected with 400.
    import ast, inspect
    import coder_workflow_guard as guard
    assert guard.DROP_ZONE.split(',')[0].strip() == '[data-status]' and '.task-list' in guard.DROP_ZONE
    source = inspect.getsource(guard._Session.drag)
    assert 'drag_to(' in source and 'DROP_ZONE' in source
    # the write counter is captured BEFORE any drag event is dispatched
    assert source.index("writes_before = len(self.result['writes'])") < source.index("dispatch_event('dragstart'")


def test_fix5_kanban_a_submit_handler_that_throws_fails_the_form_and_first_workflow_rows():
    # fix5-c pass1-kanban-live (2026-09-27): `taskIdInput is not defined` on submit; nothing created; every guard row advisory
    # while the verifier failed "UI creation did not persist" and the script error.
    from coder_workflow_guard import creation_failure
    from coder_frontend_guard import form_row
    assert creation_failure(True, ['x']) is None and creation_failure(None, []) is None
    status, detail = creation_failure(None, ['ReferenceError: taskIdInput is not defined'])
    assert status == 'failed' and 'taskIdInput is not defined' in detail and 'create handler' in detail
    def threw(url, **kw):
        return {'forms': 1, 'submitted': 1, 'writes': [], 'errors': ['ReferenceError: taskIdInput is not defined'], 'fault': ''}
    row = form_row({'id': 'app', 'cwd': '.'}, 'http://127.0.0.1:1', {}, prober=threw)
    assert row['passed'] is False and 'taskIdInput is not defined' in row['reason'] and 'sent no request' in row['reason']
    def silent(url, **kw):
        return {'forms': 1, 'submitted': 1, 'writes': [], 'errors': [], 'fault': ''}
    assert form_row({'id': 'app', 'cwd': '.'}, 'http://127.0.0.1:1', {}, prober=silent)['passed'] is True   # a silent form stays advisory
    def wrote(url, **kw):
        return {'forms': 1, 'submitted': 1, 'writes': [{'method': 'POST', 'path': '/api/tasks', 'status': 201}], 'errors': ['console noise'], 'fault': ''}
    assert form_row({'id': 'app', 'cwd': '.'}, 'http://127.0.0.1:1', {}, prober=wrote)['passed'] is True   # a write that landed wins


def test_fix5_kanban_edit_and_delete_controls_are_scoped_to_the_workflow_item():
    # fix5-c pass2-kanban-live (2026-09-27): the page's FIRST Delete button belonged to another task; the guard deleted that
    # one, failed "still shown after Delete" twice, and the verifier (scoped to the created card) passed all 34 checks.
    import inspect
    import coder_workflow_guard as guard
    assert 'marker' in guard.SCOPED_CONTROL_JS and '<= 6' in guard.SCOPED_CONTROL_JS
    source = inspect.getsource(guard._Session._named_button)
    assert source.count('_scoped(') == 2 and 'controls.first' in inspect.getsource(guard._scoped)
    class Controls:
        def __init__(self, index): self.index, self.picked = index, []
        def evaluate_all(self, js, args): assert args == [guard.MARKER, guard.EDITED]; return self.index
        def nth(self, i): self.picked.append(i); return ('nth', i)
        @property
        def first(self): return ('first',)
    assert guard._scoped(Controls(2)) == ('nth', 2)
    assert guard._scoped(Controls(-1)) == ('first',)
    class Broken(Controls):
        def evaluate_all(self, js, args): raise RuntimeError('detached')
    assert guard._scoped(Broken(0)) == ('first',)


def test_fix5_medium_a_rejected_write_while_creating_fails_the_first_workflow_row():
    # fix5-web pass1-medium (2026-09-27): labels found, but the task POST was rejected (no selected project) and every
    # workflow row said "no item could be created" (advisory) while the verifier failed at the task form.
    from coder_workflow_guard import creation_failure
    assert creation_failure(None, [], [{'method': 'POST', 'path': '/api/tasks', 'status': 201}]) is None
    status, detail = creation_failure(None, [], [{'method': 'POST', 'path': '/api/projects', 'status': 201}, {'method': 'POST', 'path': '/api/tasks', 'status': 422}])
    assert status == 'failed' and 'POST /api/tasks -> 422' in detail and 'selected' in detail
    assert creation_failure('item', [], [{'method': 'POST', 'path': '/api/tasks', 'status': 422}]) is None   # created anyway: the steps judge it


def test_fix6_medium_per_item_buttons_the_request_names_must_exist_on_a_created_item():
    # fix6-web pass1-medium (2026-09-27): "Show Edit and Delete buttons for each task" — the item was created, no Edit or
    # Delete control was visible on it, and the rows only said so as advisory.
    from coder_workflow_guard import missing_item_controls, workflow_rows
    steps = {'persist': {'status': 'passed', 'detail': 'the created item is still shown after reload'},
             'edit': {'status': 'advisory', 'detail': 'no visible Edit control was found'},
             'delete': {'status': 'advisory', 'detail': 'no visible Delete control was found'}}
    assert missing_item_controls({'created': True, 'steps': steps}, ['Edit', 'Delete', 'Save task']) == ['Edit', 'Delete']
    assert missing_item_controls({'created': False, 'steps': steps}, ['Edit', 'Delete']) == []
    assert missing_item_controls({'created': True, 'steps': steps}, []) == []
    task = ('Build a board. Users add/edit/delete tasks. Use accessible labels Task title and buttons Add task. '
            'Show Edit and Delete buttons for each task, and Save task when editing.')
    def prober(url, requested, **kw):
        return {'steps': steps, 'writes': [], 'errors': [], 'fault': '', 'created': True,
                'interfaces': {'requested': {'labels': ['Task title'], 'buttons': ['Add task'], 'conditional_buttons': ['Edit', 'Delete', 'Save task']},
                               'missing_labels': [], 'missing_buttons': []}}
    rows = workflow_rows({'id': 'app', 'cwd': '.'}, 'http://127.0.0.1:1', {}, task, prober=prober)
    row = next(r for r in rows if r['id'] == 'interface:labels:app')
    assert row['passed'] is False and row['missing_per_item'] == ['Edit', 'Delete'] and 'for each item' in row['reason']
