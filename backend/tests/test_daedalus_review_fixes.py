"""Four review findings: regressions through execution, scope and lifecycle boundaries."""
import asyncio
import json
from pathlib import Path
import shutil

import pytest


PARENTS = [{'id': 'o1', 'text': 'Store records in CSV', 'evidence_types': ['behavior']},
           {'id': 'o2', 'text': 'Reject negative amounts', 'evidence_types': ['behavior']}]


def test_process_observer_ignores_failed_exec_and_unrelated_runtime(tmp_path):
    from coder_process_evidence import artifacts, observe
    (tmp_path / 'app').write_bytes(b'\x7fELF' + b'fixture')
    (tmp_path / 'App.class').write_bytes(b'\xca\xfe\xba\xbe' + b'fixture')
    known = artifacts(tmp_path, ['.'])
    trace = tmp_path / 'trace'
    trace.write_text('10 execve("/usr/bin/python3", ["python3"], []) = 0\n'
                     f'10 openat(AT_FDCWD, "{tmp_path}/App.class", O_RDONLY) = 3<{tmp_path}/App.class>\n'
                     f'10 execve("{tmp_path}/app", ["app"], []) = -1 ENOENT\n10 +++ exited with 0 +++\n')
    assert observe(trace, tmp_path, tmp_path, known, 'r')['processes'] == []
    trace.write_text(f'10 execve("/usr/bin/java", ["java", "App"], []) = 0\n'
                     f'10 openat(AT_FDCWD, "{tmp_path}/App.class", O_RDONLY) = 3<{tmp_path}/App.class>\n10 +++ exited with 7 +++\n')
    result = observe(trace, tmp_path, tmp_path, known, 'r')
    assert result['processes'][0]['artifacts'][0]['path'] == 'App.class'
    (tmp_path / 'App.class').write_bytes(b'changed')
    with pytest.raises(RuntimeError, match='changed'):
        observe(trace, tmp_path, tmp_path, known, 'r')


def test_explicit_protected_file_changes_are_restored(tmp_path):
    from coder_execution_utils import restore_protected
    from coder_repository import Repository
    root = tmp_path / 'source'; root.mkdir()
    (root / 'README.md').write_text('Protected original\n')
    (root / 'README.md.backup').write_text('Editable backup\n')
    repo = Repository(root, tmp_path / 'repository', [])
    baseline = repo.snapshot()['revision']
    (root / 'README.md').write_text('Unauthorized change\n')
    (root / 'README.md.backup').write_text('Requested change\n')
    assert restore_protected(repo, baseline, ['README.md']) == ['README.md']
    assert (root / 'README.md').read_text() == 'Protected original\n'
    assert (root / 'README.md.backup').read_text() == 'Requested change\n'
    assert restore_protected(repo, baseline, ['README.md']) == []


def test_continue_waits_for_cancelled_controller_to_exit(monkeypatch, tmp_path):
    import config
    import database as db
    import coder_jobs as controller
    from db import coder_jobs as store
    from context_policy import DEFAULTS
    monkeypatch.setenv('DAEDALUS_ENGINE_URL','http://worker:8587')
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'race.db'))
    monkeypatch.setattr(config, 'CONTEXT_SETTINGS', {**DEFAULTS, 'daedalus_v3_enabled': True, 'daedalus_policy7_builds': True})
    monkeypatch.setattr(controller, 'spawn', lambda *args: None)
    monkeypatch.setattr(controller, '_RUNNERS', {})
    async def run():
        await db.init_db(); await db.create_conversation('conv', model='local')
        job = await controller.create('conv', 'Build', model='local')
        await store.save(job['id'], state='cancelled', resume_state='coding', repair_round=1)
        release = asyncio.Event()
        async def old_controller():
            await release.wait()
            current = await store.get(job['id'])
            if current['state'] != 'cancelled':
                await store.save(job['id'], state='blocked', blocker='Stale cancelled operation')
        old = asyncio.create_task(old_controller()); controller._RUNNERS[job['id']] = old
        resumed = asyncio.create_task(controller.resume(job['id']))
        await asyncio.sleep(.02)
        assert (await store.get(job['id']))['state'] == 'cancelled'
        release.set(); await resumed
        current = await store.get(job['id'])
        assert current['state'] == 'queued' and current['repair_round'] == 1
        assert current['resume_after_inspect'] == 'coding'
    asyncio.run(run())


from coder_check_evidence import blocking
