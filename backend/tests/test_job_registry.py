import asyncio
import pytest
import database as db
import job_registry as jobs
import task_queue
import image_job_store


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db,'DATABASE_PATH',str(tmp_path/'jobs.db'))
    asyncio.run(db.init_db())


def test_domain_state_is_atomic_persistent_and_owner_scoped(isolated):
    async def run():
        await db.create_research_report('private',query='Private research')
        job = await jobs.get('research:private')
        assert job['status']=='queued' and 'cancel' in job['actions']
        before = (await jobs.list_jobs())['cursor']
        await db.update_research_report('private',status='partial',error='Insufficient sources')
        assert (await jobs.get(job['id']))['status']=='partial'
        assert (await jobs.changes(before))[0]['job_id']==job['id']
        other = await db.create_user('Other')
        token=db.set_current_user_id(other['id'])
        try:
            assert await jobs.get(job['id']) is None
            assert (await jobs.list_jobs())['jobs']==[]
            assert await jobs.changes(0)==[]
            with pytest.raises(LookupError): await jobs.act(job['id'],'cancel')
        finally: db.reset_current_user_id(token)
        await db.init_db()
        assert (await jobs.get(job['id']))['error']=='Insufficient sources'
        await db.delete_research_report('private')
        assert await jobs.get(job['id']) is None
    asyncio.run(run())


def test_submission_and_domain_failure_expose_only_supported_actions(isolated):
    async def run():
        image=await image_job_store.create({})
        assert (await jobs.get('image:'+image['id']))['status']=='queued'
        image.update(status='interrupted',error='Submission outcome unknown')
        await image_job_store.save(image)
        assert (await jobs.get('image:'+image['id']))['actions']==[]
        task=await db.create_scheduled_task('task',title='Scheduled fixture')
        identity=await task_queue.enqueue(task,trigger='manual')
        assert (await jobs.get('scheduled:'+identity))['status']=='queued'
        await task_queue.start_run(identity)
        await db.reap_stale_task_runs()
        job=await jobs.get('scheduled:'+identity)
        assert job['status']=='interrupted' and job['actions']==[]
    asyncio.run(run())


def test_transaction_rollback_does_not_publish_job(isolated):
    async def run():
        conn=await db.get_db()
        try:
            await conn.execute("INSERT INTO research_reports(id,user_id,query) VALUES('rollback','default','never committed')")
            await conn.rollback()
        finally: await conn.close()
        assert await jobs.get('research:rollback') is None
    asyncio.run(run())


def test_legacy_acceptance_and_stale_phase_are_not_running(isolated):
    async def run():
        await db.create_conversation(id='legacy-conversation',title='Fixture')
        await db.create_run('legacy-run','legacy-conversation','fixer',status='running')
        await db.create_coder_workflow('legacy-workflow','legacy-conversation',state='fixing',active_run_id='legacy-run')
        assert (await jobs.get('coding:legacy-workflow'))['status']=='running'
        await db.update_run('legacy-run',status='succeeded',ended=True)
        assert (await jobs.get('coding:legacy-workflow'))['status']=='interrupted'
        assert (await db.get_coder_workflow('legacy-workflow'))['state']=='fixing'
        await db.update_coder_workflow('legacy-workflow',state='accepted')
        assert (await jobs.get('coding:legacy-workflow'))['status']=='succeeded'
        assert not (await jobs.list_jobs(status='active'))['jobs']
    asyncio.run(run())
