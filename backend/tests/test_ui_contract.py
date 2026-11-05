"""User-owned outcomes cannot be weakened by a generated application/test."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

import coder_ui_contract as contract


@pytest.mark.parametrize('task,values', [
    ('Add a Reset button that clears the inputs and result.', ['', '']),
    ('Clicking Reset clears the input.', ['']),
    ('Make Reset set the result to "Ready".', ['Ready']),
    ('After clicking Reset, show "Ready" in the result.', ['Ready']),
    ('Add a Reset button that clears the input and sets the result label to Ready.', ['', 'Ready']),
    ('Add a Reset button that sets input #amount to "0.00".', ['0.00']),
])
def test_literal_outcomes_are_frozen(task, values):
    result = contract.freeze(task)
    assert [c['expected'] for c in result['criteria']] == values
    assert all(c['action']['name'] == 'Reset' and c['request_excerpt'] == task for c in result['criteria'])


@pytest.mark.parametrize('task', [
    'Add a Reset button.', 'Add a Reset button that does not clear the result.',
    'Add a Reset button that sets the result to its default.',
    'Add a Reset button that clears the result unless there is an error.',
    'Improve the web UI appearance.', 'Add a Reset button that clears the result or input.',
])
def test_ambiguous_or_unsupported_outcomes_stay_unverified(task):
    result = contract.freeze(task)
    assert result['criteria'] and all(not c['supported'] for c in result['criteria'])


def test_scope_and_explicit_replacement_preserve_history():
    assert contract.freeze('Add a Reset button that clears the result.', mode='build_from_prompt')['criteria'][0]['expected'] == ''
    assert contract.freeze('Add a CLI subcommand.')['criteria'] == []
    source = {'id': 'source', 'original_user_request': 'Create output id total.'}
    first = contract.freeze('Add a Reset button that clears the inputs and result.', source)
    before = deepcopy(first)
    second = contract.freeze('Make Reset set the result to "Ready".', {'ui_contract': first})
    assert first == before
    assert len(second['criteria']) == 2 and len(second['history']) == 1
    assert second['history'][0]['expected'] == ''
    assert second['criteria'][1]['expected'] == 'Ready'
    assert second['criteria'][1]['target']['source_selector'] == '#total'
    assert second['history'][0]['superseded_by'] == second['criteria'][1]['id']
    unchanged = contract.freeze('Add a Cancel button that clears the result.', {'ui_contract': first})
    assert len(unchanged['criteria']) == 3 and not unchanged['history']
    assert second['entrypoint_task'] == source['original_user_request']


def test_result_binding_excludes_button_and_input_descendant_labels():
    result = {'id': 'result', 'role': 'label', 'name': 'filled'}
    nodes = [result, {'id': 'reset-label', 'role': 'label', 'name': 'Reset', 'interactive_ancestor': True},
             {'id': 'set-label', 'role': 'label', 'name': 'Set', 'interactive_ancestor': True}]
    assert contract._matches(nodes, {'kind': 'result'}) == [result]
    with pytest.raises(contract.Unverified, match='found 2'):
        contract._matches(nodes + [{'id': 'other', 'role': 'label', 'name': 'Other'}], {'kind': 'result'})


@pytest.mark.parametrize('identity', [{'selector': '#result'}, {'name': 'Result'}, {'name': 'Output'}])
def test_identified_result_is_not_ambiguous_with_input_caption(identity):
    result = {'role': 'div', 'text': '', **identity}
    caption = {'role': 'label', 'text': 'Task'}
    controls = [{'name': 'Result', 'editable': True}, {'name': 'Result', 'activatable': True}]
    assert contract._matches([caption, result, *controls], {'kind': 'result'}) == [result]
    # Two genuinely identified result displays still require clarification.
    with pytest.raises(contract.Unverified, match='found 2'):
        contract._matches([caption, result, {'selector': '#output', 'role': 'div'}], {'kind': 'result'})


def test_contract_is_persisted_before_dispatch_and_old_jobs_keep_behavior(monkeypatch, tmp_path):
    import coder_jobs
    from db import coder_jobs as jobs
    from .test_daedalus_jobs import setup
    setup(monkeypatch, tmp_path)
    monkeypatch.setenv('DAEDALUS_ENGINE_URL', 'http://candidate:8587')
    captured = []
    monkeypatch.setattr(coder_jobs, 'spawn', lambda identity: captured.append(identity))
    async def scenario():
        job = await coder_jobs.create('conversation', 'Create a web calculator.', model='local', engine='openhands159', key='new')
        stored = await jobs.get(job['id'])
        assert captured == [job['id']]
        assert stored['ui_contract_version'] == 1 and stored['ui_contract']['version'] == 1
        original = deepcopy(stored['ui_contract'])
        await jobs.save(job['id'], state='cancelled', calls_used=4, seconds_used=10, repair_round=1)
        resumed = await coder_jobs.resume(job['id'], budget_mode='preserve')
        assert resumed['ui_contract'] == original
        assert (resumed['calls_used'], resumed['seconds_used'], resumed['repair_round']) == (4, 10, 1)
        old = await jobs.create('conversation', 'old', 'build_from_prompt', '', 'local', 'old', engine='openhands159', policy_version=7)
        assert not old.get('ui_contract_version')
    asyncio.run(scenario())


def test_followup_keeps_confirmed_flows_and_freezes_source_provenance(monkeypatch, tmp_path):
    import coder_jobs
    from .test_daedalus_versions import setup, deliver_candidate
    setup(monkeypatch, tmp_path)
    monkeypatch.setenv('DAEDALUS_ENGINE_URL', 'http://candidate:8587')
    flow = {'kind': 'web', 'file_path': 'index.html', 'steps': [
        {'action': 'click', 'name': 'Calculate'}, {'action': 'text', 'selector': '#total', 'equals': '22.00'}]}
    async def scenario():
        source = await coder_jobs.create('conversation', 'Create index.html with output id total.', model='local', engine='openhands159', key='base', ui_checks=[flow])
        await deliver_candidate(tmp_path, source['id'], 'a'*40)
        edit = await coder_jobs.create('conversation', 'Add a Reset button that clears the result.', mode='edit_project',
            project_id=source['project_id'], model='local', key='edit', ui_checks=[])
        assert edit['ui_checks'] == [flow]
        criterion = edit['ui_contract']['criteria'][0]
        assert criterion['provenance']['source_job_id'] == source['id']
        assert criterion['provenance']['source_revision_id'] == 'a'*40
        assert criterion['expected'] == '' and criterion['target']['source_selector'] == '#total'
    asyncio.run(scenario())


@pytest.mark.parametrize('condition,expected', [
    ('correct', 'passed'), ('wrong', 'failed'), ('noop', 'unverified'),
    ('duplicate', 'unverified'), ('missing', 'unverified'), ('no_setup', 'unverified'),
    ('removed_source_input', 'unverified'),
])
@pytest.mark.parametrize('activation', [{}, {'name': 'Reset'}, {'selector': '#reset'}])
@pytest.mark.parametrize('mode', ['build_from_prompt', 'edit_project'])
def test_observation_not_project_verdict_drives_outcomes(monkeypatch, condition, expected, activation, mode):
    import coder_ui_checks
    specification = contract.freeze('Add a Reset button that clears the result.', mode=mode)
    state = {'text': 'seed', 'clicks': 0}
    nodes = [{'selector': '#result', 'tag': 'output', 'role': 'output', 'name': '', 'showing': True},
             {'selector': '#reset', 'name': 'Reset', 'activatable': True, 'showing': True}]
    if condition == 'duplicate': nodes.append({**nodes[0], 'selector': '#another'})
    if condition == 'missing': nodes = nodes[1:]
    def action(request):
        if request['action'] == 'launch': return {'nodes': nodes}
        if request['action'] == 'observe': return {'nodes': nodes}
        if request['action'] == 'fill': state['text'] = '' if condition == 'noop' else 'seed'
        if request['action'] == 'text': return {'text': state['text']}
        if request['action'] == 'click':
            state['clicks'] += 1
            state['text'] = '0.00' if condition == 'wrong' else ''
        return {}
    @contextmanager
    def session(*args): yield action
    monkeypatch.setattr(coder_ui_checks, 'session', session)
    monkeypatch.setattr(coder_ui_checks, 'bind_project_entrypoint', lambda *a: None)
    # Avoid a real polling delay in this deterministic fake, while still proving
    # retries read observations and do not dispatch the action a second time.
    ticks = iter(range(100))
    monkeypatch.setattr(contract.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(contract.time, 'sleep', lambda _: None)
    worker = SimpleNamespace(get=lambda _: {'payload': {'revision_id': 'candidate'}}, event=lambda *a, **kw: None)
    flow = {'kind': 'web', 'file_path': 'index.html', 'steps': [{'action': 'fill', 'selector': '#input', 'value': 'seed'}]}
    if activation:
        flow['steps'].append({'action': 'click', **activation})
    if condition == 'no_setup': flow['steps'] = []
    bound = {specification['criteria'][0]['id']: {'targets': [{'selector': '#removed'}]}} if condition == 'removed_source_input' else {}
    result = contract.run(worker, 'op', None, specification, [{'revision_id': 'source', 'flow': flow}], bound)
    assert result[0]['status'] == expected
    assert state['clicks'] == (1 if expected in {'passed', 'failed'} else 0)
    if expected in {'passed', 'failed'}:
        assert result[0]['evidence'][0]['revision_id'] == 'candidate'
    if expected == 'failed': assert "'0.00'" in result[0]['reason']


def test_unverified_does_not_request_repairs_but_failed_outcome_does():
    base = {'checks_ok': True, 'checks_passed': True, 'failures': []}
    unknown = {'id': 'a', 'status': 'unverified', 'request_excerpt': 'Reset', 'reason': 'Ambiguous'}
    contract.apply_summary(base, [unknown])
    assert base['checks_ok'] and not base['checks_passed'] and not base['failures']
    failed = {**unknown, 'status': 'failed', 'reason': "Expected ''; observed '0.00'"}
    contract.apply_summary(base, [failed])
    assert len(base['failures']) == 1 and '0.00' in base['failures'][0]['reason']


@pytest.mark.parametrize('repairs,expected', [(0, 'coding'), (1, 'coding'), (2, 'candidate_packaging')])
def test_contract_failures_share_existing_two_repairs(repairs, expected):
    from coder_engine_controller import step
    changes = []
    async def save(*args, **kw): changes.append(kw)
    async def record(*args): pass
    async def operate(*args, **kw):
        return {'checks': [], 'execution_profile': {}, 'summary': {'checks_passed': False, 'failures': [
            {'id': 'ui-contract:a', 'reason': "Expected ''; observed '0.00'"}]}}, {'worker_operation': 'op'}
    controller = SimpleNamespace(store=SimpleNamespace(save=save, record_check=record), _operate=operate)
    asyncio.run(step({'id': 'job', 'state': 'checking', 'revision_id': 'r', 'repair_round': repairs}, controller))
    assert changes[-1]['state'] == expected
    assert changes[-1].get('repair_round', repairs) <= 2


def test_unverified_candidate_remains_accept_ready_and_does_not_greenwash_metadata():
    from coder_presentation import presentation
    job = {'engine':'openhands159', 'state': 'ready_for_review', 'policy_version': 7, 'ui_contract_version': 1, 'revision_id': 'r',
           'verification_summary': {'revision_id': 'r', 'checks_ok': True, 'checks_passed': False, 'exercised': True, 'failures': [],
                'unverified_ui_criteria': ['a'], 'criteria': [{'id': 'a', 'status': 'unverified'}]},
           'candidate_artifact': {'id': 'artifact', 'metadata': {'revision_id': 'r', 'runnable': True, 'checks_passed': True}}}
    value = presentation(job)
    assert value['phase'] == 'Ready for review' and value['actions']['accept']['enabled']
    assert not value['actions']['resume']['enabled']


def test_dynamic_desktop_result_requires_one_changed_label_in_the_same_session():
    from coder_ui_contract import _matches, Unverified
    target = {'kind': 'result', 'name': ''}
    initial = [{'id': str(i), 'role': 'label', 'text': value, 'name': value, 'session_id': 'current'}
               for i, value in enumerate(('Celsius', 'Ready'))]
    converted = [initial[0], {**initial[1], 'text': '77.0', 'name': '77.0'}]
    assert _matches(converted, target, initial_labels=initial) == [converted[1]]
    names_only = [{k:v for k,v in n.items() if k != 'text'} for n in initial]
    converted_names = [names_only[0], {**names_only[1], 'name':'77.0'}]
    assert _matches(converted_names, target, initial_labels=names_only) == [converted_names[1]]
    for ambiguous in (initial, [{**converted[0], 'text': 'updated'}, converted[1]],
                      [initial[0], {**converted[1], 'id': 'replacement'}],
                      [initial[0], {**converted[1], 'session_id': 'other'}]):
        with pytest.raises(Unverified):
            _matches(ambiguous, target, initial_labels=initial)
