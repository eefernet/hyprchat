"""Archival holds the global admission lock only briefly, and never publishes evidence that changed (plan 2a)."""
import threading
import time

import pytest
import hephaestus_admission as admission
import hephaestus_archive as archive
import hephaestus_inventory as inv
import hephaestus_maintenance as maintenance
from .test_hephaestus_maintenance import file, root, store   # noqa: F401  (root is a fixture)


def environment(root, name='old-test'):
    path = store(root, name); source = file(path / 'jobs/job/app.py')
    registry = maintenance.discover(root)
    row = next(r for r in registry['resources'].values() if r['kind'] == 'test_environment')
    outbox = inv.control(root) / 'evidence-outbox'; outbox.mkdir(exist_ok=True)
    return row, maintenance.resource_path(root, row['path']), outbox, source


def run(root, row, path, outbox, **options):
    return archive.archive_environment(root, row, path, outbox, **{'idle': lambda: True, 'fingerprint': maintenance.evidence_fingerprint,
        'identity': maintenance.identity, 'excluded': maintenance.EVIDENCE_EXCLUDED, **options})


def shared_admission_is_free(root):
    try:
        with inv.lock(root, 'admission', exclusive=False, blocking=False): return True
    except BlockingIOError:
        return False


def test_new_work_is_admitted_while_the_archive_is_being_compressed(root):
    row, path, outbox, _ = environment(root)
    seen = []
    record = run(root, row, path, outbox, between=lambda: seen.append(shared_admission_is_free(root)))
    assert seen == [True] and record['sha256'] and (outbox / (row['id'] + '.tar.gz')).exists()
    assert not list(outbox.glob('*.pending*'))
    assert run(root, row, path, outbox, between=lambda: pytest.fail('unchanged evidence must not be compressed again')) == record


def test_evidence_that_changes_during_compression_is_never_published(root):
    row, path, outbox, source = environment(root)
    with pytest.raises(RuntimeError, match='changed during archival'):
        run(root, row, path, outbox, between=lambda: source.write_text('changed while compressing'))
    assert not (outbox / (row['id'] + '.tar.gz')).exists() and not (outbox / (row['id'] + '.json')).exists()
    assert not list(outbox.glob('*.pending*'))


def test_work_that_arrived_during_compression_defers_the_publish(root):
    row, path, outbox, _ = environment(root)
    answers = iter([True, False])
    assert run(root, row, path, outbox, idle=lambda: next(answers)) is None
    assert not (outbox / (row['id'] + '.tar.gz')).exists() and (outbox / (row['id'] + '.tar.pending')).exists()


def test_busy_commit_keeps_the_pending_archive_and_the_next_pass_reuses_it(root, monkeypatch):
    row, path, outbox, _ = environment(root)
    release = threading.Event(); held = threading.Event()
    def job():                                   # a running job holds admission shared for longer than the commit wait
        with inv.lock(root, 'admission', exclusive=False):
            held.set(); release.wait(5)
    def start():
        threading.Thread(target=job, daemon=True).start(); assert held.wait(2)
    try:
        assert run(root, row, path, outbox, between=start, commit_wait=.3) is None
    finally:
        release.set()
    pending = outbox / (row['id'] + '.tar.pending')
    assert pending.exists() and not (outbox / (row['id'] + '.tar.gz')).exists()
    time.sleep(.05)
    monkeypatch.setattr(archive, '_compress', lambda *a: pytest.fail('a matching pending archive must be reused'))
    record = run(root, row, path, outbox)
    assert record['sha256'] == archive._sha(outbox / (row['id'] + '.tar.gz')) and not list(outbox.glob('*.pending*'))


def test_archive_does_not_start_while_admission_is_held_or_the_environment_is_active(root):
    row, path, outbox, _ = environment(root)
    with inv.lock(root, 'admission', exclusive=False):
        assert run(root, row, path, outbox) is None
    assert run(root, row, path, outbox, idle=lambda: False) is None
    with inv.lock(root, 'resource:' + row['id']):
        assert run(root, row, path, outbox) is None
    assert not list(outbox.iterdir())


def test_tampered_pending_archive_is_rebuilt_not_published(root):
    row, path, outbox, _ = environment(root)
    answers = iter([True, False, True, True])
    assert run(root, row, path, outbox, idle=lambda: next(answers)) is None
    (outbox / (row['id'] + '.tar.pending')).write_bytes(b'corrupted')
    record = run(root, row, path, outbox, idle=lambda: next(answers))
    assert record['sha256'] == archive._sha(outbox / (row['id'] + '.tar.gz')) and record['bytes'] > 9


def test_bounded_wait_reports_busy_abandoned_and_success():
    clock = [0.0]
    def sleep(seconds): clock[0] += seconds
    calls = []
    def flaky(root, name, **options):
        from contextlib import contextmanager
        @contextmanager
        def held():
            calls.append(clock[0])
            if len(calls) < 4: raise BlockingIOError
            yield
        return held()
    import pytest as _p
    original = admission.lock; admission.lock = flaky
    try:
        with admission.admitted('/x', seconds=5, poll=.5, clock=lambda: clock[0], sleep=sleep) as waited:
            assert waited == 1.5 and len(calls) == 4
        calls.clear(); calls.extend([0] * 10 ** 6 * 0)
        admission.lock = lambda *a, **k: (_ for _ in ()).throw(BlockingIOError())
        clock[0] = 0.0
        with _p.raises(admission.AdmissionBusy, match='nothing was dispatched'):
            with admission.admitted('/x', seconds=2, poll=.5, clock=lambda: clock[0], sleep=sleep): pass
        assert clock[0] == 2.0                                   # the wait never runs past its allowance
        with _p.raises(admission.AdmissionAbandoned):
            with admission.admitted('/x', seconds=60, alive=lambda: False, clock=lambda: clock[0], sleep=sleep): pass
        assert issubclass(admission.AdmissionBusy, BlockingIOError)
    finally:
        admission.lock = original
