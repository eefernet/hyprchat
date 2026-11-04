import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from coder_ui_checks import UIFlowValidationError, replay, validate


def test_ui_cleanup_stops_children_in_separate_process_groups(tmp_path):
    import subprocess, sys, time, psutil
    from coder_ui_session import stop_session
    pidfile = tmp_path / 'child.pid'
    code = ('import subprocess,sys,time;from pathlib import Path;'
            'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],start_new_session=True);'
            f'Path({str(pidfile)!r}).write_text(str(p.pid));time.sleep(60)')
    parent = subprocess.Popen([sys.executable, '-c', code], start_new_session=True)
    child = None
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline: time.sleep(.02)
        child = psutil.Process(int(pidfile.read_text()))
        stop_session(parent)
        assert parent.poll() is not None
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    finally:
        stop_session(parent)
        if child and child.is_running():
            try: child.kill()
            except psutil.NoSuchProcess: pass


def flow(action='text', expected='Ready'):
    return {'kind': 'web', 'file_path': 'index.html', 'steps': [
        {'action': 'click', 'selector': '#clear'}, {'action': action, 'selector': '#result', 'equals': expected}]}


def test_collects_all_schema_errors_before_replay():
    first = flow('observe'); second = flow()
    second['steps'][0]['equals'] = 'Ready'
    second['steps'][1]['equals'] = 0
    with pytest.raises(UIFlowValidationError) as caught:
        validate([first, second], 1, preflight_version=1)
    errors = caught.value.errors
    assert {r['flow_index'] for r in errors} == {0, 1}
    assert any('whole interface' in e['reason'] for e in errors)
    assert any('never on click' in e['reason'] for e in errors)
    assert any('must be text' in e['reason'] for e in errors)


@pytest.mark.parametrize('expected', ['', '0.00', '“Ready”', 'Ready'])
def test_exact_values_not_coerced(expected):
    value = [flow(expected=expected)]
    assert validate(value, 1, preflight_version=1) is value
    assert value[0]['steps'][1]['equals'] == expected


@pytest.mark.parametrize('step', [
    {'action': 'observe', 'name': 'Result', 'contains': 'Ready'},
    {'action': 'fill', 'selector': '#task', 'value': '', 'equals': ''},
    {'action': 'text', 'selector': '#result', 'equals': '', 'contains': 'Ready'},
])
def test_rejects_invalid_read_or_mutating_assertions(step):
    f = flow(); f['steps'][1] = step
    with pytest.raises(UIFlowValidationError): validate([f], 1, preflight_version=1)


def test_legacy_manifest_validation_unchanged():
    assert validate([flow('observe')], 1)


def test_input_text_cannot_prove_empty_value():
    requests = []
    def action(request):
        requests.append(request)
        return {'text': '', 'input_control': True} if request['action'] == 'text' else {}
    result = replay(flow(expected=''), action, strict=True)
    assert not result['passed']
    assert result['diagnostic']['category'] == 'ui_flow_property'
    assert result['diagnostic']['observed'] == ''
    assert result['observations'][-1]['failed']
    assert len(requests) == 4 and requests[-1]['action'] == 'diagnostics'


def test_failed_observation_has_expected_actual_and_stops(monkeypatch):
    import coder_ui_checks
    clock = iter(range(0, 1000, 20)); monkeypatch.setattr(coder_ui_checks.time, 'monotonic', lambda: next(clock))
    requests = []
    def action(request):
        requests.append(request)
        return {'text': 'Task cannot be blank.'} if request['action'] == 'text' else {}
    f = flow(expected='Please enter a task'); f['steps'].append({'action': 'click', 'selector': '#delete'})
    result = replay(f, action, strict=True)
    assert not result['passed']
    assert result['diagnostic']['expected'] == 'Please enter a task'
    assert result['diagnostic']['observed'] == 'Task cannot be blank.'
    assert result['diagnostic']['locator'] == {'selector': '#result'}
    assert result['diagnostic']['step_index'] == 1
    assert len(requests) == 4 and requests[-1]['action'] == 'diagnostics'


def test_actual_value_assertion_passes():
    result = replay(flow('value', ''), lambda r: {'value': ''} if r['action'] == 'value' else {}, strict=True)
    assert result['passed'] and result['assertions'] == 1


def spec():
    return {'flows': [flow()], 'files': [], 'profile': {'profiles': []}, 'ready': True, 'fingerprint': 'source1-env1'}


@pytest.fixture
def local_preflight(tmp_path, monkeypatch):
    import coder_ui_preflight as preflight
    value = spec(); sessions = []
    monkeypatch.setenv('DAEDALUS_UI_INSTANCE', 'process1')
    monkeypatch.setattr(preflight, 'inspect', lambda *args: dict(value))
    monkeypatch.setattr(preflight, 'copy_source', lambda *args: None)
    monkeypatch.setattr(preflight, 'close_interactive', lambda *args: None)
    @contextmanager
    def session(root, runtime, deadline, operation):
        sessions.append(operation)
        yield lambda request: {'text': 'Ready'} if request['action'] == 'text' else {}
    monkeypatch.setattr(preflight, 'local_session', session)
    return preflight, value, sessions


def test_reuse_only_same_operation_instance_source_environment(tmp_path, monkeypatch, local_preflight):
    p, value, sessions = local_preflight
    assert p.run(tmp_path, tmp_path/'diag', 'op')['passed']
    assert p.run(tmp_path, tmp_path/'diag', 'op')['reused']
    value['fingerprint'] = 'source2-env1'
    assert not p.run(tmp_path, tmp_path/'diag', 'op').get('reused')
    value['fingerprint'] = 'source2-env2'
    p.run(tmp_path, tmp_path/'diag', 'op')
    p.run(tmp_path, tmp_path/'diag', 'next-op')
    monkeypatch.setenv('DAEDALUS_UI_INSTANCE', 'restarted')
    p.run(tmp_path, tmp_path/'diag', 'next-op')
    assert len(sessions) == 5


def test_incomplete_or_interrupted_has_no_success_receipt(tmp_path, monkeypatch, local_preflight):
    p, value, sessions = local_preflight
    @contextmanager
    def interrupted(*args):
        raise InterruptedError('Stop')
        yield
    monkeypatch.setattr(p, 'local_session', interrupted)
    with pytest.raises(InterruptedError): p.run(tmp_path, tmp_path/'diag', 'op')
    assert not (tmp_path/'diag/ui-preflight/receipt.json').exists()
    @contextmanager
    def timeout(*args):
        raise TimeoutError('deadline')
        yield
    monkeypatch.setattr(p, 'local_session', timeout)
    result = p.run(tmp_path, tmp_path/'diag', 'op')
    assert result['status'] == 'incomplete' and not result['passed']
    assert not (tmp_path/'diag/ui-preflight/receipt.json').exists()


def test_unready_environment_defers_without_launch(tmp_path, local_preflight):
    p, value, sessions = local_preflight; value['ready'] = False
    result = p.run(tmp_path, tmp_path/'diag', 'op')
    assert result['status'] == 'deferred' and not result['passed'] and sessions == []


def test_missing_and_malformed_manifests(tmp_path, monkeypatch):
    import coder_ui_preflight as p
    root=tmp_path/'source'; root.mkdir(); (root/'index.html').write_text('<input>')
    monkeypatch.setattr(p, 'interfaces', lambda *args: ['web'])
    with pytest.raises(UIFlowValidationError, match='Missing UI flows'): p.inspect(root, tmp_path/'diag')
    (root/'.daedalus-ui.json').write_text('{')
    with pytest.raises(UIFlowValidationError, match='not valid JSON'): p.inspect(root, tmp_path/'diag')


def test_hook_schema_feedback_precedes_dependency_deferral_and_shared_limit(tmp_path, monkeypatch):
    import coder_engine_hook as hook, coder_ui_preflight as preflight, coder_profiles
    (tmp_path/'main.py').write_text('print(42)')
    monkeypatch.setattr(coder_profiles, 'discover', lambda *args: {'profiles': [], 'checks': []})
    def invalid(*args): raise UIFlowValidationError([{'flow_index': 0, 'reason': 'Wrong observation'}, {'flow_index': 1, 'reason': 'Wrong comparison'}])
    monkeypatch.setattr(preflight, 'inspect', invalid)
    state={'finish_seen': True, 'root': str(tmp_path), 'ui_directory': str(tmp_path/'diag'), 'ui_preflight_version':1,
           'operation_id':'op','command_seconds':10,'denials':0,'nudges':0}
    assert hook.evaluate(state)[0] and state['denials']==1
    assert hook.evaluate(state)[0] and state['denials']==2
    assert not hook.evaluate(state)[0] and state['denials']==2


def test_progress_diagnostics_do_not_become_checks():
    from coder_progress import project
    batch=[{'seq':1,'type':'ui_preflight','data':{'status':'running'}},
           {'seq':2,'type':'ui_preflight','data':{'status':'passed','passed':True,'diagnostic_only':True,'fingerprint':'x'}}]
    running,_=project(None,batch[:1],'op'); assert running['current']['kind']=='ui_preflight'
    done,events=project(running,batch[1:],'op')
    assert done['current'] is None and done['ui_preflight']['diagnostic_only']
    assert 'checks' not in done and events[0]['kind']=='ui_preflight'


def test_storage_failure_defers_instead_of_consuming_application_repair(tmp_path, monkeypatch, local_preflight):
    p, value, sessions = local_preflight
    from coder_environment_publish import PreparationFailure
    def no_space(*args): raise PreparationFailure('storage', 'Insufficient quota')
    monkeypatch.setattr(p, 'copy_source', no_space)
    result=p.run(tmp_path,tmp_path/'diag','op')
    assert result['status']=='deferred' and result['category']=='storage' and not result['passed']


def test_non_string_action_and_vacuous_contains_are_schema_errors():
    malformed=flow();malformed['steps'][1]['action']={}
    empty=flow();empty['steps'][1].pop('equals');empty['steps'][1]['contains']=''
    with pytest.raises(UIFlowValidationError) as e: validate([malformed,empty],1,preflight_version=1)
    assert len(e.value.errors)>=2


def test_successful_hook_preflight_keeps_repair_count(tmp_path, monkeypatch):
    import coder_engine_hook as hook, coder_ui_preflight as preflight, coder_profiles
    (tmp_path/'app.py').write_text('print(42)')
    monkeypatch.setattr(coder_profiles,'discover',lambda *args:{'profiles':[],'checks':[]})
    monkeypatch.setattr(preflight,'inspect',lambda *args:spec())
    monkeypatch.setattr(preflight,'run',lambda *args,**kwargs:{'passed':True,'status':'passed','diagnostic_only':True})
    state={'finish_seen':True,'root':str(tmp_path),'ui_directory':str(tmp_path/'diag'),'ui_preflight_version':1,'operation_id':'op','command_seconds':10,'denials':1,'nudges':0}
    assert not hook.evaluate(state)[0] and state['denials']==1 and state['passed']


def test_hook_environment_failure_does_not_use_repair(tmp_path, monkeypatch):
    import coder_engine_hook as hook, coder_ui_preflight as preflight, coder_profiles
    (tmp_path/'app.py').write_text('print(42)')
    monkeypatch.setattr(coder_profiles,'discover',lambda *args:{'profiles':[],'checks':[]})
    monkeypatch.setattr(preflight,'inspect',lambda *args:spec())
    monkeypatch.setattr(preflight,'run',lambda *args,**kwargs:{'passed':False,'status':'deferred','reason':'Browser missing'})
    state={'finish_seen':True,'root':str(tmp_path),'ui_directory':str(tmp_path/'diag'),'ui_preflight_version':1,'operation_id':'op','command_seconds':10,'denials':0,'nudges':0}
    assert not hook.evaluate(state)[0] and state['denials']==0 and not state['passed']


def test_host_and_sandbox_use_identical_environment_key(tmp_path, monkeypatch):
    import coder_profiles
    (tmp_path/'requirements.txt').write_text('example==1.0')
    profile={'toolchains':['python3','pip'],'commands':{'setup':['python3 -m venv .venv']}}
    read=Path.read_text
    monkeypatch.setattr(Path,'read_text',lambda p,*a,**kw:json.dumps({'profiles':{'languages':{'image':'sha256:fixed'}}}) if str(p)=='/etc/hephaestus/runtime.json' else read(p,*a,**kw))
    monkeypatch.setenv('HEPHAESTUS_EXECUTOR','docker');monkeypatch.setenv('HEPHAESTUS_RUNTIME_PROFILE','languages')
    host=coder_profiles.environment_key(tmp_path,profile)
    monkeypatch.delenv('HEPHAESTUS_EXECUTOR');monkeypatch.setenv('DAEDALUS_RUNTIME_IMAGE','sha256:fixed')
    assert coder_profiles.environment_key(tmp_path,profile)==host
    monkeypatch.setenv('DAEDALUS_RUNTIME_IMAGE','sha256:changed')
    assert coder_profiles.environment_key(tmp_path,profile)!=host
