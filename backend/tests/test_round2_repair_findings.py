"""Regressions for the two repairs build 38436 needed in comparison round 2 (2026-11-10)."""
import json
import pytest
from coder_engine_ops import summarize
from coder_ui_checks import UIFlowValidationError, workspace_bound_commands


def test_prepared_interpreter_is_project_relative(tmp_path, monkeypatch):
    """cw3-dda874906563: the model copied the returned absolute interpreter into a UI flow replayed elsewhere."""
    from .test_engine_reliability import operation
    from coder_repository import Repository
    import coder_engine_environment as environment
    import coder_project_runtime as runtime
    root = tmp_path / 'source'; (root / '.venv/bin').mkdir(parents=True)
    (root / '.venv/bin/python').write_text(''); (root / 'requirements.txt').write_text('six==1.17.0\n')
    repo = Repository(root, tmp_path / 'index', ('.venv',)); repo.refresh()
    monkeypatch.setattr(runtime, 'prepare_environment', lambda *a: [{'passed': True}])
    result = environment.refresh(operation(tmp_path / 'store'), 'op', repo, ['requirements.txt'], {}, tmp_path / 'logs')
    assert result['passed'] and result['python'] == '.venv/bin/python'
    assert str(root) not in json.dumps(result)


def test_flow_command_naming_the_session_workspace_is_rejected(tmp_path):
    flows = [{'kind': 'desktop', 'command': f'{tmp_path}/.venv/bin/python app.py', 'steps': [{'action': 'observe'}]},
             {'kind': 'desktop', 'command': '.venv/bin/python app.py', 'steps': [{'action': 'observe'}]},
             {'kind': 'desktop', 'command': '/usr/bin/python3 app.py', 'steps': [{'action': 'observe'}]},
             {'kind': 'web', 'file_path': 'index.html', 'steps': [{'action': 'observe'}]}]
    errors = workspace_bound_commands(flows, tmp_path)
    assert [e['flow_index'] for e in errors] == [0]
    assert 'project-relative' in errors[0]['reason'] and str(tmp_path) not in errors[0]['reason']
    assert workspace_bound_commands('not a list', tmp_path) == []


def test_preflight_reports_workspace_bound_flow_before_finish(tmp_path):
    from coder_ui_preflight import inspect
    root = tmp_path / 'project'; root.mkdir(); (root / 'app.py').write_text('print(1)\n')
    (root / '.daedalus-ui.json').write_text(json.dumps([{'kind': 'desktop', 'toolkit': 'qt',
        'command': f'{root}/.venv/bin/python app.py',
        'steps': [{'action': 'click', 'name': 'Convert'}, {'action': 'text', 'name': 'Result', 'role': 'label', 'equals': '32.0'}]}]))
    with pytest.raises(UIFlowValidationError, match='workspace path'):
        inspect(root, tmp_path / 'state')


def test_provenance_diagnostic_keeps_the_observed_endpoint_guidance():
    """38436 replaced the repair text with a diagnostic that no longer named DAEDALUS_APP_URL."""
    diagnostic = 'No executed application source or controller service traffic was bound to this check.'
    row = {'id': 'tests', 'passed': False, 'revision_id': 'r', 'classification': 'missing_provenance', 'reason': diagnostic}
    reason = summarize([row], 'r', ['web'])['failures'][0]['reason']
    assert reason.startswith(diagnostic)
    assert 'DAEDALUS_APP_URL' in reason and 'test-owned server' in reason
    again = summarize([{**row, 'reason': reason}], 'r', ['web'])['failures'][0]['reason']
    assert again.count('DAEDALUS_APP_URL') == reason.count('DAEDALUS_APP_URL')
