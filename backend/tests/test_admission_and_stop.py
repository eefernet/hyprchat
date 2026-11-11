"""Bounded admission wait, honest never-started failures, and unconfirmed container stops (plan 2b, 2c)."""
import json
import socket
import socketserver
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import hephaestus_inventory as inv
import hephaestus_runtime as runtime
from coder_failures import classify_failure
from hephaestus_admission import AdmissionAbandoned, AdmissionBusy
from hephaestus_reaper import reap, remember
from hephaestus_runtime import Broker

GIB = 1024 ** 3


@pytest.fixture
def broker(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'ROOT', tmp_path)
    instance = Broker(tmp_path / 'broker', {})
    instance.calls = []
    monkeypatch.setattr(instance, '_execute', lambda spec, connection, emit, waited=0.0: instance.calls.append(waited) or 'ran')
    monkeypatch.setattr(Broker, 'connected', staticmethod(lambda connection: getattr(connection, 'open', True)))
    return instance


def rows(broker):
    with broker.connect() as db: return db.execute('SELECT id,status FROM executions').fetchall()


def hold(root, seconds):
    """Maintenance holding admission exclusively for a while, in another thread."""
    held, release = threading.Event(), threading.Event()
    def work():
        with inv.lock(root, 'admission'):
            held.set(); release.wait(seconds)
    thread = threading.Thread(target=work, daemon=True); thread.start(); assert held.wait(2)
    return release


def test_launch_waits_out_a_short_maintenance_hold_inside_its_own_deadline(broker, tmp_path):
    release = hold(tmp_path, 5)
    threading.Timer(.5, release.set).start()
    assert broker.execute({'id': 'op1', 'timeout': 30}, object(), lambda frame: None) == 'ran'
    assert .3 < broker.calls[0] < 5          # the wait is handed on so it shortens, never extends, the deadline
    assert not broker.waiting and not broker.cancelled


def test_exhausted_wait_dispatches_nothing_and_the_ledger_says_so(broker, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'ADMISSION_WAIT', .4)
    release = hold(tmp_path, 5)
    try:
        with pytest.raises(AdmissionBusy, match='nothing was dispatched'):
            broker.execute({'id': 'op2', 'timeout': 900}, object(), lambda frame: None)
    finally:
        release.set()
    assert broker.calls == [] and rows(broker) == [] and not broker.waiting
    assert broker.cancel('op2', 'scope') == {'stopped': True, 'status': 'not_dispatched'}
    # A short deadline is never stretched to the full admission allowance.
    monkeypatch.setattr(runtime, 'ADMISSION_WAIT', 60)
    release = hold(tmp_path, 5); started = time.monotonic()
    try:
        with pytest.raises(AdmissionBusy): broker.execute({'id': 'op3', 'timeout': .3}, object(), lambda frame: None)
    finally:
        release.set()
    assert time.monotonic() - started < 2


def test_cancel_or_disconnect_while_waiting_can_never_start_the_work_later(broker, tmp_path):
    release = hold(tmp_path, 5); outcome = []
    def launch(identity, connection):
        try: outcome.append(broker.execute({'id': identity, 'timeout': 60}, connection, lambda frame: None))
        except Exception as error: outcome.append(error)
    thread = threading.Thread(target=launch, args=('op4', object())); thread.start()
    until = time.monotonic() + 2
    while 'op4' not in broker.waiting and time.monotonic() < until: time.sleep(.01)
    assert broker.cancel('op4', 'scope')['status'] == 'not_dispatched'
    thread.join(3); assert not thread.is_alive()
    assert isinstance(outcome[0], AdmissionAbandoned) and broker.calls == [] and rows(broker) == []
    class Gone: open = False
    launch('op5', Gone())
    assert isinstance(outcome[1], AdmissionAbandoned) and broker.calls == []
    release.set()
    # A cancel for an identity that is not waiting leaves nothing behind to poison a later launch.
    assert broker.cancel('never-launched', 'scope')['status'] == 'not_dispatched' and not broker.cancelled


def test_only_the_supervisors_exception_type_classifies_a_never_started_step():
    failure = classify_failure('NotDispatched: The worker did not start this step. Nothing ran and no model calls were used. [Errno 11]', 'code')
    assert failure['category'] == 'not_dispatched' and 'Continue' in failure['next_action']
    assert classify_failure('RuntimeError: NotDispatched: The worker did not start this step.', 'coding')['category'] == 'not_dispatched'
    forged = 'IsolationUnavailable: SDK child did not return a checkpoint: NotDispatched: nothing was dispatched'
    assert classify_failure(forged, 'code')['category'] == 'environment'


class Reply(socketserver.StreamRequestHandler):
    def handle(self):
        self.server.seen.append(json.loads(self.rfile.readline()))
        self.wfile.write(json.dumps(self.server.reply).encode() + b'\n')


@contextmanager
def broker_socket(tmp_path, monkeypatch, reply):
    import shutil, tempfile
    folder = tempfile.mkdtemp(prefix='hb', dir='/tmp')      # AF_UNIX paths are limited to about 100 bytes
    path = folder + '/b.sock'
    server = socketserver.ThreadingUnixStreamServer(path, Reply); server.reply, server.seen = reply, []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv('HEPHAESTUS_BROKER_SOCKET', path)
    try: yield server
    finally:
        server.shutdown(); server.server_close(); shutil.rmtree(folder, ignore_errors=True)


def sandbox():
    from coder_sandbox import Sandbox
    box = Sandbox.__new__(Sandbox)
    box.execution_identity, box.stop_status = ('op', 'scope'), None
    box.servers, box.temporary, box.cache_lease, box.shared_leases = [], None, None, []
    return box


def test_sandbox_stop_reports_the_brokers_answer_once(tmp_path, monkeypatch):
    with broker_socket(tmp_path, monkeypatch, {'result': {'stopped': True, 'status': 'not_dispatched'}}) as server:
        box = sandbox()
        assert box.stop() == 'not_dispatched' and box.stop() == 'not_dispatched'
        box.__exit__(None, None, None)
        assert len(server.seen) == 1 and server.seen[0] == {'action': 'cancel', 'id': 'op', 'scope': 'scope'}


def test_unconfirmed_stop_is_its_own_error_and_an_environment_fault_in_ui_checks(tmp_path, monkeypatch):
    from coder_sandbox import IsolationUnavailable, StopUnconfirmed
    with broker_socket(tmp_path, monkeypatch, {'result': {'exit_code': 125, 'error': 'Container stop is unconfirmed'}}):
        with pytest.raises(StopUnconfirmed, match='could not confirm the writer stopped'):
            sandbox().__exit__(None, None, None)
    import coder_ui_checks as ui
    class Store:
        events = []
        def get(self, operation): return {'payload': {'revision_id': 'r', 'engine_behavior_version': 2, 'ui_preflight_version': 1}}
        def event(self, operation, kind, **data): self.events.append(data)
    @contextmanager
    def failing(*args):
        raise StopUnconfirmed('Hephaestus could not confirm the writer stopped: Container stop is unconfirmed')
        yield
    monkeypatch.setattr(ui, 'session', failing); monkeypatch.setattr(ui, 'validate', lambda *a, **k: None)
    flow = {'kind': 'desktop', 'command': 'python3 app.py', 'steps': [{'action': 'click', 'name': 'Convert'}]}
    row, = ui.run(Store(), 'op', None, [flow])
    assert not row['passed'] and row['classification'] == 'environment' and row['environment_fault'] is True
    assert issubclass(StopUnconfirmed, IsolationUnavailable)


class Removal:
    def __init__(self, code, stderr=''): self.returncode, self.stderr = code, stderr


def reserved_broker(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *a, **k: Removal(0))      # systemctl stop of the slice
    instance = Broker(tmp_path / 'broker', {})
    instance.reserve_identity({'id': 'stuck', 'scope': 'scope'})
    instance.update('stuck', 'stop_unconfirmed', {'error': 'timeout'})
    instance.groups['g'] = {'memory': 3 * GIB, 'users': 1, 'cgroup': tmp_path}; instance.reserved = 3 * GIB
    remember(instance, 'stuck', group='g', memory=3 * GIB, unit='g.slice', reserved=True, outcome={'exit_code': 130, 'failure_category': 'cancelled'})
    return instance


def test_reservation_is_held_until_the_container_is_confirmed_gone(tmp_path, monkeypatch):
    broker = reserved_broker(tmp_path, monkeypatch)
    still_there = lambda *a, **k: Removal(1, 'Error response from daemon: cannot remove container: device busy')
    assert reap(broker, run=still_there, force=True) == []
    assert broker.reserved == 3 * GIB and 'stuck' in broker.unconfirmed and rows(broker)[0]['status'] == 'stop_unconfirmed'
    def hangs(*a, **k): raise runtime.subprocess.TimeoutExpired('docker', 30)
    assert reap(broker, run=hangs, force=True) == [] and broker.reserved == 3 * GIB
    assert reap(broker, run=lambda *a, **k: Removal(1, 'Error: No such container: hephaestus-stuck'), force=True) == ['stuck']
    assert broker.reserved == 0 and 'g' not in broker.groups and not broker.unconfirmed
    with broker.connect() as db: row = db.execute("SELECT status,result FROM executions WHERE id='stuck'").fetchone()
    assert row['status'] == 'failed' and json.loads(row['result'])['stop_confirmed_late'] is True
    assert reap(broker, run=lambda *a, **k: pytest.fail('nothing left to reap'), force=True) == []


def test_reaping_is_throttled_and_never_releases_twice(tmp_path, monkeypatch):
    broker = reserved_broker(tmp_path, monkeypatch)
    calls = []
    def slow(*a, **k):
        calls.append(1); time.sleep(.2); return Removal(0)
    clock = [100.0]
    assert reap(broker, run=lambda *a, **k: Removal(1, 'busy'), clock=lambda: clock[0]) == []
    assert reap(broker, run=lambda *a, **k: pytest.fail('throttled'), clock=lambda: clock[0] + 1) == []
    results = []
    threads = [threading.Thread(target=lambda: results.append(reap(broker, run=slow, force=True))) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(results, key=len) == [[], [], [], ['stuck']] and len(calls) == 1 and broker.reserved == 0


def test_judge_waits_out_maintenance_before_any_assertion_and_reports_exhaustion_as_an_interruption():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'evals'))
    import engine_judge
    class Store:
        def __init__(self, busy): self.busy, self.created = busy, []
        def create(self, identity, job, kind, payload):
            if self.busy: self.busy -= 1; raise BlockingIOError(11, 'Resource temporarily unavailable')
            self.created.append((identity, job, kind)); return True
    clock = [0.0]
    def sleep(seconds): clock[0] += seconds
    store = Store(3)
    assert engine_judge.admit(store, 'eval-1', 'job', {}, clock=lambda: clock[0], sleep=sleep) is True
    assert store.created == [('eval-1', 'job', 'check')] and clock[0] == 1.5
    with pytest.raises(TimeoutError, match='no assertion ran'):
        engine_judge.admit(Store(10 ** 6), 'eval-2', 'job', {}, seconds=3, clock=lambda: clock[0], sleep=sleep)
