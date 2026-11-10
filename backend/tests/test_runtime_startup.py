"""Storage pressure and delayed container admission cannot masquerade as app bugs."""
from types import SimpleNamespace
import json
import time

import pytest

import hephaestus_storage as storage
import coder_ui_checks as ui


@pytest.mark.parametrize('free,required,allowed', [(15,0,True),(10,0,True),(9,0,False),(11,2,False)])
def test_command_admission_checks_reserve_without_discovery(monkeypatch, tmp_path, free, required, allowed):
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(total=100*storage.GIB, used=(100-free)*storage.GIB, free=free*storage.GIB))
    monkeypatch.setattr(storage, 'collect', lambda *a,**k: pytest.fail('Whole-VM collection on command launch'))
    if allowed: storage.require_space(tmp_path, required*storage.GIB)
    else:
        with pytest.raises(OSError, match='storage reserve'): storage.require_space(tmp_path, required*storage.GIB)


@pytest.fixture
def startup(tmp_path, monkeypatch):
    from .test_engine_reliability import operation
    import coder_worker_runtime
    store = operation(tmp_path/'store')
    now = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: now[0])
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(coder_worker_runtime, 'enforce_storage', lambda *a: None)
    started, ready, log = (tmp_path/name for name in ('started','ready','log'))
    log.write_text('trusted GUI startup log')
    process = SimpleNamespace(poll=lambda: None)
    def run(on_sleep, operation_id='op'):
        def sleep(seconds):
            now[0] += 1
            on_sleep(now[0] - initial)
        initial = now[0]
        monkeypatch.setattr(time, 'sleep', sleep)
        return ui.wait_for_session(store, operation_id, process, started, ready, log)
    return store, started, ready, process, run


def test_delayed_admission_does_not_spend_gui_readiness_window(startup):
    store, started, ready, process, run = startup
    def progress(seconds):
        if seconds >= 30: started.touch()
        if seconds >= 32: ready.touch()
    run(progress)
    payload = store.get('op')['payload']
    assert payload['_preparation_seconds'] == 32
    assert '_preparation_deadline' not in payload
    assert store.get('op')['calls'] == 0


def test_gui_exit_is_environment_failure(startup):
    store, started, ready, process, run = startup
    process.poll = lambda: 125
    with pytest.raises(ui.UIObservationFailure) as failure: run(lambda _: None)
    assert failure.value.details['environment_fault']
    assert failure.value.details['category'] == 'gui_startup'
    assert failure.value.details['log_tail'] == 'trusted GUI startup log'


def test_actual_gui_readiness_still_has_twenty_second_limit(startup):
    store, started, ready, process, run = startup
    started.touch()
    with pytest.raises(ui.UIObservationFailure, match='20 seconds') as failure: run(lambda _: None)
    assert failure.value.details['category'] == 'gui_startup'
    assert store.get('op')['payload']['_preparation_seconds'] == 20


def test_preparation_limit_survives_reentry_and_cannot_trigger_app_repair(startup):
    store, started, ready, process, run = startup
    payload = store.get('op')['payload']
    store.update('op', payload={**payload, '_preparation_seconds':599})
    for _ in range(2):
        with pytest.raises(ui.UIObservationFailure) as failure: run(lambda _: None)
        assert failure.value.details['category'] == 'preparation_limit'
        assert failure.value.details['retryable'] is False
    assert store.get('op')['payload']['_preparation_seconds'] == 600


def test_stop_during_admission_is_not_a_test_failure(startup):
    store, started, ready, process, run = startup
    with pytest.raises(InterruptedError): run(lambda _: store.update('op', cancel_requested=True))
    assert store.get('op')['calls'] == 0


def test_gui_and_dependency_time_share_durable_job_limit_across_resume(startup):
    store, started, ready, process, run = startup
    ledger=store.root/'preparation-ledgers/job.json';ledger.parent.mkdir()
    ledger.write_text(json.dumps({'seconds':599,'failures':{}}))
    with pytest.raises(ui.UIObservationFailure) as failure:run(lambda _:None)
    assert failure.value.details['category']=='preparation_limit'
    payload=store.get('op')['payload'];payload.pop('_preparation_seconds')
    store.create('resumed','job','check',payload);store.update('resumed',started=time.time())
    with pytest.raises(ui.UIObservationFailure) as failure:run(lambda _:pytest.fail('Consumed preparation was reset'),'resumed')
    assert failure.value.details['category']=='preparation_limit'
    assert json.loads(ledger.read_text())['seconds']==600


def test_startup_failure_attempts_survive_new_operations(startup):
    store, started, ready, process, run = startup
    process.poll=lambda:125
    for identity in ('op','second','third'):
        if identity!='op':
            store.create(identity,'job','check',store.get('op')['payload']);store.update(identity,started=time.time())
        with pytest.raises(ui.UIObservationFailure) as failure:run(lambda _:None,identity)
        assert failure.value.details['category']==('preparation_limit' if identity=='third' else 'gui_startup')
    ledger=json.loads((store.root/'preparation-ledgers/job.json').read_text())
    assert ledger['failures']['gui-startup']==2
