import asyncio
import pytest
import database as db
import scheduler
import task_queue


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path/'tasks.db'))
    asyncio.run(db.init_db())
    monkeypatch.setattr(scheduler, '_run_semaphore', asyncio.Semaphore(1))


def test_once_enqueue_and_schedule_advance_survive_restart(isolated):
    async def run():
        task = await db.create_scheduled_task('once', title='Once', next_run='2000-01-01')
        first, duplicate = await asyncio.gather(*[
            task_queue.enqueue(task, trigger='schedule', advance={'next_run': None, 'enabled': False}) for _ in range(2)])
        assert bool(first) != bool(duplicate)
        run_id = first or duplicate
        assert not (await db.get_scheduled_task('once'))['enabled']
        await db.reap_stale_task_runs()
        pending = await task_queue.pending()
        assert [r['id'] for r in pending] == [run_id]
        assert (await db.get_task_run(run_id))['status'] == 'queued'
    asyncio.run(run())


def test_coalesce_preserves_submission_and_running_work_is_not_repeated(isolated):
    async def run():
        task = await db.create_scheduled_task('daily', title='Daily', prompt='original')
        run_id = await task_queue.enqueue(task, trigger='manual')
        changed = {**task, 'prompt': 'changed'}
        assert await task_queue.enqueue(changed, trigger='manual') == run_id
        assert (await task_queue.pending())[0]['task']['prompt'] == 'original'
        assert await task_queue.start_run(run_id)
        assert not await task_queue.start_run(run_id)
        await db.reap_stale_task_runs()
        assert (await db.get_task_run(run_id))['status'] == 'interrupted'
        assert not await task_queue.pending()
    asyncio.run(run())


def test_queued_cancellation_never_dispatches_and_is_owner_scoped(isolated, monkeypatch):
    async def run():
        task = await db.create_scheduled_task('cancel', title='Cancel')
        run_id = await task_queue.enqueue(task, trigger='manual')
        other = await db.create_user('Other')
        token = db.set_current_user_id(other['id'])
        try:
            assert await task_queue.cancel(run_id) is None
        finally:
            db.reset_current_user_id(token)
        assert (await task_queue.cancel(run_id))['status'] == 'cancelled'
        async def never(*a, **k):
            raise AssertionError('cancelled queued work must not execute')
        monkeypatch.setattr(scheduler, '_dispatch', never)
        await scheduler._execute_task(task, run_id, trigger='manual')
        assert (await db.get_task_run(run_id))['status'] == 'cancelled'
    asyncio.run(run())


def test_running_only_after_slot_and_shutdown_keeps_waiting_run(isolated, monkeypatch):
    async def run():
        task = await db.create_scheduled_task('waiting', title='Waiting')
        run_id = await task_queue.enqueue(task, trigger='manual')
        await scheduler._run_semaphore.acquire()
        work = asyncio.create_task(scheduler._execute_task(task, run_id, trigger='manual'))
        await asyncio.sleep(.02)
        assert (await db.get_task_run(run_id))['status'] == 'queued'
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        assert (await db.get_task_run(run_id))['status'] == 'queued'
        scheduler._run_semaphore.release()
    asyncio.run(run())


def test_research_parent_waits_for_child_result(isolated, monkeypatch):
    async def run():
        task = await db.create_scheduled_task('research', title='Research', task_type='research')
        run_id = await task_queue.enqueue(task, trigger='manual')
        await db.create_research_report('child', query='Fixture')
        async def create(_): return {'id': 'child'}
        monkeypatch.setattr(scheduler, '_create_research_report', create)
        work = asyncio.create_task(scheduler._dispatch(task, run_id))
        await asyncio.sleep(.03)
        assert not work.done()
        await db.update_research_report('child', status='complete', summary='Saved result')
        assert 'Saved result' in await asyncio.wait_for(work, 2)
    asyncio.run(run())


def test_parent_cancellation_waits_for_authoritative_child(isolated):
    async def run():
        task=await db.create_scheduled_task('research-cancel',title='Research',task_type='research')
        run_id=await task_queue.enqueue(task,trigger='manual')
        await task_queue.start_run(run_id)
        await db.create_research_report('cancel-child',query='Fixture')
        await task_queue.attach_child(run_id,'cancel-child')
        await task_queue.cancel(run_id)
        await task_queue.reconcile_children()
        assert (await db.get_task_run(run_id))['status']=='cancelling'
        await db.update_research_report('cancel-child',status='cancelled')
        await task_queue.reconcile_children()
        assert (await db.get_task_run(run_id))['status']=='cancelled'
    asyncio.run(run())
