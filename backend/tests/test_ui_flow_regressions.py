"""Fresh UI replay keeps scenarios across append/reorder, detects replacements."""
from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from coder_required_tests import passing, regressions, ui_flow_changes
from coder_ui_checks import run


def flow(button, expected):
    return {'kind':'desktop', 'command':'/usr/bin/python3 app.py', 'toolkit':'qt', 'steps':[
        {'action':'click','name':button},
        {'action':'text','name':expected,'role':'label','equals':expected}]}


@pytest.fixture
def replay(tmp_path, monkeypatch):
    clear_value = 'Ready'
    @contextmanager
    def session(*args):
        current = ''
        def action(request):
            nonlocal current
            if request['action'] == 'click':
                current = {'Convert':'Error','Reset':'Ready','Clear':clear_value}[request['name']]
            return {'text':current, 'nodes':[], 'accessibility':current}
        yield action
    monkeypatch.setattr('coder_ui_checks.session', session)
    store = SimpleNamespace(get=lambda _: {'payload':{'revision_id':'revision','ui_preflight_version':1}}, event=lambda *a,**k:None)
    def check(flows, *, clear_result='Ready'):
        nonlocal clear_value
        clear_value = clear_result
        (tmp_path/'.daedalus-ui.json').write_text(json.dumps(flows))
        return run(store,'op',SimpleNamespace(root=tmp_path),flows,origin='project_interaction')
    return check


def test_append_and_reorder_preserve_required_scenarios(replay):
    before = [flow('Convert','Error')]
    required = passing(replay(before))
    for after in [before+[flow('Reset','Ready')], [flow('Reset','Ready')]+before]:
        assert regressions(required,replay(after)) == []
        changes = ui_flow_changes(before, after)
        assert not changes['removed_scenarios'] and not changes['changed_expectations']
    assert all('ui-scenario:' in name for name in required)


@pytest.mark.parametrize('replacement', [[], [flow('Reset','Ready')]])
def test_same_count_replacement_and_deletion_lose_old_scenario(replay, replacement):
    before = [flow('Convert','Error')]
    lost = regressions(passing(replay(before)), replay(replacement))
    assert len(lost)==1 and lost[0]['status']=='missing'
    assert ui_flow_changes(before,replacement)['removed_scenarios']


def test_changed_expectation_is_explicit_and_reviewable(replay):
    before, after = [flow('Clear','Cleared')], [flow('Clear','Ready')]
    # A previously observed passing baseline has the same scenario; its old
    # expected value is intentionally superseded by the new user requirement.
    baseline = replay(before, clear_result='Cleared')
    assert all(row['passed'] for row in baseline)
    changes = ui_flow_changes(before, after)
    assert not changes['removed_scenarios'] and len(changes['changed_expectations'])==1
    assert regressions(passing(baseline), replay(after)) == []
    # Weakening equality to a substring is a different check, not a value edit.
    weakened = deepcopy(after)
    weakened[0]['steps'][-1].pop('equals')
    weakened[0]['steps'][-1]['contains']='Read'
    assert ui_flow_changes(after, weakened)['removed_scenarios']


def test_resumed_old_positional_id_remains_readable_but_is_not_new_requirement(replay):
    checks = replay([flow('Convert','Error')])
    assert regressions(['.::ui-flow:0'], checks) == []
    assert '.::ui-flow:0' not in passing(checks)
