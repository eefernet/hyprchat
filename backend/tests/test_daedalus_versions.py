"""Project versions: the working pointer, the promotion log, user Accept and their fences (offline).

Plan 2026-10-26 [R3]: follow-ups start from the latest delivered build, Accept promotes one exact revision,
and a failed follow-up never touches the accepted version.
"""
import asyncio
import json
import tarfile

import pytest

import config
import coder_jobs as controller
import database as db
from context_policy import DEFAULTS
from db import coder_jobs as store
from db import coder_promotion
from coder_presentation import presentation


def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'jobs.db'))
    monkeypatch.setattr(config, 'DATABASE_PATH', str(tmp_path / 'jobs.db'), raising=False)
    monkeypatch.setattr(config, 'SANDBOX_OUTPUTS_DIR', str(tmp_path / 'outputs'))
    monkeypatch.setattr(config, 'ARTIFACT_DIR', str(tmp_path / 'artifacts'))
    monkeypatch.setattr(config, 'CONTEXT_SETTINGS', {**DEFAULTS, 'daedalus_v3_enabled': True, 'daedalus_policy7_builds': True,
                                                     'daedalus_policy7_edits': True})
    monkeypatch.setattr(controller, 'spawn', lambda *args: None)
    async def initialize():
        await db.init_db()
        await db.create_conversation('conversation', model='coder:local')
    asyncio.run(initialize())


def candidate_archive(tmp_path, revision):
    """A candidate tarball the way the worker packages one: project source plus its review report."""
    (tmp_path / 'outputs').mkdir(exist_ok=True)
    path = tmp_path / 'outputs' / f'candidate-x-{revision}.tar.gz'
    source = tmp_path / 'src'; source.mkdir(exist_ok=True)
    (source / 'app.py').write_text('print("hello")\n')
    (source / f'DAEDALUS_REVIEW_{revision}.json').write_text('{"label": "Review candidate; not accepted"}')
    with tarfile.open(path, 'w:gz') as archive:
        archive.add(source / 'app.py', arcname='app.py')
        archive.add(source / f'DAEDALUS_REVIEW_{revision}.json', arcname=f'DAEDALUS_REVIEW_{revision}.json')
    return path


async def complete(tmp_path, job_id, revision):
    """Automatic completion as packaging does it after every confirmed example passed."""
    summary = {'revision_id': revision, 'accepted': True, 'checks_passed': True, 'evidence_version': 4}
    await store.save(job_id, state='packaging', revision_id=revision, verification_summary=summary,
                     acceptance={'accepted': True, 'revision_id': revision})
    path = tmp_path / 'outputs' / f'final-{revision[:8]}.tar.gz'
    path.parent.mkdir(exist_ok=True); path.write_bytes(b'x')
    return await store.finish_artifact(job_id, revision, artifact_id='final-' + revision[:8], filename=path.name,
        url='/api/downloads/' + path.name, kind='archive', storage_path=str(path), status='accepted',
        metadata={'revision_id': revision})


async def forget_start(job_id):
    """Make a job look created before the creation snapshot of the working pointer existed."""
    connection = await db.get_db()
    try:
        payload = json.loads((await connection.execute_fetchall('SELECT job_json FROM coder_workflows WHERE id=?', (job_id,)))[0]['job_json'])
        for key in ('working_job_at_start', 'working_revision_at_start', 'promotion_baseline_id'):
            payload.pop(key, None)
        await connection.execute('UPDATE coder_workflows SET job_json=? WHERE id=?', (json.dumps(payload), job_id))
        await connection.commit()
    finally:
        await connection.close()


async def deliver_candidate(tmp_path, job_id, revision, checks_passed=True):
    """Publish a review candidate exactly as the controller's candidate_packaging stage does."""
    summary = {'revision_id': revision, 'accepted': False, 'checks_passed': checks_passed, 'checks_ok': checks_passed,
               'exercised': checks_passed, 'evidence_version': 4}
    await store.save(job_id, state='candidate_packaging', revision_id=revision, verification_summary=summary)
    path = candidate_archive(tmp_path, revision)
    return await store.finish_candidate(job_id, revision, artifact_id='artifact-' + revision[:8], filename=path.name,
        url='/api/downloads/' + path.name, kind='archive', storage_path=str(path), status='review_candidate',
        metadata={'revision_id': revision, 'checks_passed': checks_passed, 'runnable': True})


def test_a_follow_up_starts_from_the_delivered_candidate(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        assert build['source_pointer'] == 'working'
        await deliver_candidate(tmp_path, build['id'], 'a' * 40)
        project = await db.get_coding_project(build['project_id'])
        assert project['working_job_id'] == build['id'] and project['working_revision_id'] == 'a' * 40
        assert not project['accepted_revision_id']
        # "now add --top N": this raised "no accepted revision yet" before the working pointer existed.
        edit = await controller.create('conversation', 'Add a --top N flag', mode='edit_project', model='coder:local', key='edit')
        assert edit['source_job_id'] == build['id'] and edit['source_revision_id'] == 'a' * 40
        assert edit['source_pointer'] == 'working'
        # Asking for the accepted version when nothing was accepted is a clear error, not a silent fallback.
        await store.save(edit['id'], state='cancelled')
        with pytest.raises(ValueError, match='no accepted version'):
            await controller.create('conversation', 'Change it', mode='edit_project', model='coder:local', key='e2',
                                    source_pointer='accepted')
    asyncio.run(scenario())


def test_accept_promotes_the_exact_revision_and_strips_the_review_report(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40)
        job = await store.get(build['id'])
        assert job['presentation']['phase'] == 'Checks passed — please review'
        assert job['presentation']['actions']['accept']['enabled']
        assert job['presentation']['actions']['accept']['revision_id'] == 'a' * 40
        assert not job['presentation']['actions']['resume']['enabled']   # nothing to continue on a passing build
        with pytest.raises(LookupError):
            await coder_promotion.accept(build['project_id'], 'b' * 40)
        result = await coder_promotion.accept(build['project_id'], 'a' * 40)
        project = await db.get_coding_project(build['project_id'])
        assert project['accepted_revision_id'] == 'a' * 40 and project['accepted_job_id'] == build['id']
        assert project['accepted_artifact_id'] == result['artifact']['id']
        with tarfile.open(result['artifact']['storage_path']) as archive:
            names = archive.getnames()
        assert 'app.py' in names and not any('DAEDALUS_REVIEW_' in name for name in names)
        accepted = await store.get(build['id'])
        assert accepted['state'] == 'completed' and accepted['accepted_by'] == 'user'
        assert accepted['presentation']['phase'] == 'Complete' and accepted['presentation']['detail'] == 'Accepted by you'
        promotions = await coder_promotion.promotions(build['project_id'])
        assert [(p['revision_id'], p['kind']) for p in promotions] == [('a' * 40, 'user')]
        again = await coder_promotion.accept(build['project_id'], 'a' * 40)
        assert again['already_accepted']
        # Pinned: neither the working nor the accepted archive may be deleted while a pointer names it.
        pinned = await coder_promotion.pinned_artifact_ids()
        assert {project['working_artifact_id'], project['accepted_artifact_id']} <= pinned
    asyncio.run(scenario())


def test_a_failed_follow_up_leaves_the_accepted_version_alone(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40)
        await coder_promotion.accept(build['project_id'], 'a' * 40)
        edit = await controller.create('conversation', 'Add JSON output', mode='edit_project', model='coder:local', key='edit')
        assert edit['source_revision_id'] == 'a' * 40
        await deliver_candidate(tmp_path, edit['id'], 'b' * 40, checks_passed=False)
        project = await db.get_coding_project(build['project_id'])
        assert project['working_revision_id'] == 'b' * 40 and project['working_job_id'] == edit['id']
        assert project['accepted_revision_id'] == 'a' * 40   # still recoverable and downloadable
        assert (await store.get(edit['id']))['presentation']['phase'] in {'Ready for review', 'Build incomplete'}
    asyncio.run(scenario())


def test_a_parked_job_resumes_from_its_own_delivery_but_not_after_a_newer_follow_up(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40, checks_passed=False)
        # Its own candidate moved the working pointer; Continue must not see that as "newer work".
        resumed = await store.save(build['id'], state='queued', event='resume', candidate_revision='a' * 40)
        assert resumed['state'] == 'queued'
        await deliver_candidate(tmp_path, build['id'], 'c' * 40, checks_passed=False)
        edit = await controller.create('conversation', 'Fix it', mode='edit_project', model='coder:local', key='edit')
        await deliver_candidate(tmp_path, edit['id'], 'd' * 40, checks_passed=False)
        with pytest.raises(ValueError, match='newer version'):
            await store.save(build['id'], state='queued', event='resume', candidate_revision='c' * 40)
    asyncio.run(scenario())


def test_migration_backfills_the_working_pointer_from_existing_candidates(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40)
        connection = await db.get_db()
        try:
            await connection.execute("UPDATE coding_projects SET working_job_id='',working_revision_id='',working_artifact_id=''")
            await connection.commit()
            await store.migrate(connection)   # one-time: a later startup never rewrites the pointer
            assert not (await db.get_coding_project(build['project_id']))['working_job_id']
            # A database from before the working pointer (user_version 1): the candidate exists, the pointer does not.
            await connection.execute("PRAGMA user_version=1")
            await connection.commit()
            await store.migrate(connection)
            assert (await connection.execute_fetchall("PRAGMA user_version"))[0][0] == 2
        finally:
            await connection.close()
        project = await db.get_coding_project(build['project_id'])
        assert (project['working_job_id'], project['working_revision_id']) == (build['id'], 'a' * 40)
    asyncio.run(scenario())


def test_card_labels_follow_the_check_outcome():
    base = {'state': 'ready_for_review', 'policy_version': 7, 'candidate_artifact': {'id': 'x', 'metadata': {'revision_id': 'r'}}}
    passed = presentation({**base, 'verification_summary': {'checks_passed': True, 'checks_ok': True, 'exercised': True}})
    assert passed['phase'] == 'Checks passed — please review' and passed['not_verified']
    idle = presentation({**base, 'verification_summary': {'checks_passed': False, 'checks_ok': True, 'exercised': False}})
    # Nothing failed and nothing is left to evidence: Continue would only re-run the same checks.
    assert idle['phase'] == 'Built, not exercised' and not idle['actions']['resume']['enabled']
    spent = presentation({**base, 'stop_limit': 'model_calls', 'verification_summary': {'checks_passed': False, 'checks_ok': True, 'exercised': False}})
    assert spent['actions']['resume']['enabled']   # a spent allowance still continues
    broken = presentation({**base, 'candidate_artifact': {'id': 'x', 'metadata': {'revision_id': 'r', 'runnable': False}},
                           'verification_summary': {'checks_passed': False, 'checks_ok': False}})
    assert broken['phase'] == 'Build incomplete'
    examples = presentation({'state': 'completed', 'artifact': {'id': 'a', 'metadata': {'accepted_by': 'examples'}},
                             'artifact_status': 'delivered', 'acceptance': {'kind': 'examples'},
                             'verification_summary': {'confirmed_examples': 3}, 'policy_version': 7})
    assert examples['phase'] == 'Complete' and examples['detail'] == 'Your 3 agreed checks passed'


def test_an_earlier_accepted_version_can_be_accepted_again(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40)
        await coder_promotion.accept(build['project_id'], 'a' * 40)
        edit = await controller.create('conversation', 'Add JSON output', mode='edit_project', model='coder:local', key='edit')
        await deliver_candidate(tmp_path, edit['id'], 'b' * 40)
        await coder_promotion.accept(build['project_id'], 'b' * 40)
        # "Restore this version": the same archive is promoted again, never a duplicate artifact row (was a 500).
        again = await coder_promotion.accept(build['project_id'], 'a' * 40)
        project = await db.get_coding_project(build['project_id'])
        assert project['accepted_revision_id'] == 'a' * 40 and again['artifact']['id'] == project['accepted_artifact_id']
        assert [(p['revision_id'], p['kind']) for p in await coder_promotion.promotions(build['project_id'])] == [
            ('a' * 40, 'user'), ('b' * 40, 'user'), ('a' * 40, 'user')]
    asyncio.run(scenario())


def test_accept_refuses_a_job_that_was_resumed_meanwhile(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40, checks_passed=False)
        real_history = store.project_history
        async def stale_history(project_id):
            rows = await real_history(project_id)
            # Another tab pressed Continue after this snapshot was read.
            await store.save(build['id'], state='queued', event='resume', candidate_revision='a' * 40)
            return rows
        monkeypatch.setattr(store, 'project_history', stale_history)
        with pytest.raises(ValueError, match='Wait for the job to finish'):
            await coder_promotion.accept(build['project_id'], 'a' * 40)
        assert (await store.get(build['id']))['state'] == 'queued'
        assert not (await db.get_coding_project(build['project_id']))['accepted_revision_id']
    asyncio.run(scenario())


def test_older_jobs_cannot_resume_over_a_newer_follow_up_whatever_pointer_they_used(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40, checks_passed=False)
        # A job from before the pointers existed has no source_pointer and was fenced on the (empty) accepted head.
        connection = await db.get_db()
        try:
            row = (await connection.execute_fetchall('SELECT job_json FROM coder_workflows WHERE id=?', (build['id'],)))[0]
            legacy = json.loads(row['job_json']); legacy.pop('source_pointer', None)
            await connection.execute('UPDATE coder_workflows SET job_json=? WHERE id=?', (json.dumps(legacy), build['id']))
            await connection.commit()
        finally:
            await connection.close()
        edit = await controller.create('conversation', 'Add JSON output', mode='edit_project', model='coder:local', key='edit')
        await deliver_candidate(tmp_path, edit['id'], 'b' * 40, checks_passed=False)
        with pytest.raises(ValueError, match='newer version'):
            await store.save(build['id'], state='queued', event='resume', candidate_revision='a' * 40)
        assert (await db.get_coding_project(build['project_id']))['working_revision_id'] == 'b' * 40
    asyncio.run(scenario())


def test_accepting_another_build_never_strands_a_follow_up_from_the_accepted_version(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40)
        await coder_promotion.accept(build['project_id'], 'a' * 40)
        first = await controller.create('conversation', 'Add JSON output', mode='edit_project', model='coder:local', key='e1')
        await deliver_candidate(tmp_path, first['id'], 'b' * 40)
        follow = await controller.create('conversation', 'Add CSV output', mode='edit_project', model='coder:local', key='e2',
                                         source_pointer='accepted')
        assert follow['source_revision_id'] == 'a' * 40
        await coder_promotion.accept(build['project_id'], 'b' * 40)   # while the follow-up is still working
        delivered = await deliver_candidate(tmp_path, follow['id'], 'c' * 40)
        assert delivered and (await db.get_coding_project(build['project_id']))['working_revision_id'] == 'c' * 40
        assert (await db.get_coding_project(build['project_id']))['accepted_revision_id'] == 'b' * 40
    asyncio.run(scenario())


def test_continue_refuses_a_candidate_with_nothing_left_to_do(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40)
        with pytest.raises(ValueError, match='nothing left to continue'):
            await controller.resume(build['id'], candidate_revision='a' * 40)
    asyncio.run(scenario())


def test_a_newer_follow_up_cannot_resume_over_an_older_jobs_later_delivery(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        old = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, old['id'], 'a' * 40, checks_passed=False)
        new = await controller.create('conversation', 'Add JSON output', mode='edit_project', model='coder:local', key='edit')
        assert (new['source_revision_id'], new['working_job_at_start']) == ('a' * 40, old['id'])
        await store.save(new['id'], state='blocked', blocker='paused')   # parked with nothing delivered
        await store.save(old['id'], state='queued', event='resume', candidate_revision='a' * 40)
        await deliver_candidate(tmp_path, old['id'], 'o' * 40, checks_passed=False)
        # Created earlier is not delivered earlier: the old job's o came after the new job read a.
        with pytest.raises(ValueError, match='newer version'):
            await store.save(new['id'], state='queued', event='resume')
        project = await db.get_coding_project(old['project_id'])
        assert (project['working_job_id'], project['working_revision_id']) == (old['id'], 'o' * 40)
    asyncio.run(scenario())


def test_a_follow_up_cannot_complete_over_a_sibling_that_completed_first(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40, checks_passed=False)
        first = await controller.create('conversation', 'Add JSON output', mode='edit_project', model='coder:local', key='e1')
        await store.save(first['id'], state='blocked', blocker='paused')
        second = await controller.create('conversation', 'Add CSV output', mode='edit_project', model='coder:local', key='e2')
        assert second['source_revision_id'] == 'a' * 40
        await store.save(second['id'], state='blocked', blocker='paused')
        await store.save(first['id'], state='queued', event='resume')
        await complete(tmp_path, first['id'], 'e' * 40)
        # The second edit still builds on a; completing it would replace e as the accepted version too.
        with pytest.raises(ValueError, match='newer version'):
            await store.save(second['id'], state='queued', event='resume')
        project = await db.get_coding_project(build['project_id'])
        assert project['accepted_revision_id'] == project['working_revision_id'] == 'e' * 40
    asyncio.run(scenario())


def test_jobs_without_the_creation_snapshot_fence_on_when_working_was_delivered(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40)
        await coder_promotion.accept(build['project_id'], 'a' * 40)
        first = await controller.create('conversation', 'Add JSON output', mode='edit_project', model='coder:local', key='e1')
        await deliver_candidate(tmp_path, first['id'], 'b' * 40)
        follow = await controller.create('conversation', 'Add CSV output', mode='edit_project', model='coder:local', key='e2',
                                         source_pointer='accepted')
        await forget_start(follow['id'])
        # b was delivered before the follow-up started: it is that job's past, not newer work.
        await deliver_candidate(tmp_path, follow['id'], 'c' * 40, checks_passed=False)
        late = await controller.create('conversation', 'Add XML output', mode='edit_project', model='coder:local', key='e3')
        await forget_start(late['id'])
        await store.save(late['id'], state='blocked', blocker='paused')
        await store.save(follow['id'], state='queued', event='resume', candidate_revision='c' * 40)
        await deliver_candidate(tmp_path, follow['id'], 'd' * 40, checks_passed=False)
        with pytest.raises(ValueError, match='newer version'):   # d was delivered after the late job read c
            await store.save(late['id'], state='queued', event='resume')
    asyncio.run(scenario())


def test_a_stopped_candidate_can_still_be_accepted(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40)
        stopped = await controller.cancel(build['id'])
        assert stopped['state'] == 'cancelled' and stopped['cancel_requested']   # the flag outlives the Stop
        await coder_promotion.accept(build['project_id'], 'a' * 40)
        project = await db.get_coding_project(build['project_id'])
        assert project['accepted_revision_id'] == 'a' * 40 and (await store.get(build['id']))['state'] == 'completed'
        # A Stop still waiting for its worker has not finished.
        other = await controller.create('conversation', 'Build a line counter', model='coder:local', key='other')
        await deliver_candidate(tmp_path, other['id'], 'b' * 40)
        assert (await store.request_cancel(other['id']))['state'] == 'cancelling'
        with pytest.raises(ValueError, match='Wait for the job to finish'):
            await coder_promotion.accept(other['project_id'], 'b' * 40)
    asyncio.run(scenario())


def test_an_accept_in_the_same_second_before_a_follow_up_started_does_not_block_its_promotion(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40)
        await coder_promotion.accept(build['project_id'], 'a' * 40)
        edit = await controller.create('conversation', 'Add JSON output', mode='edit_project', model='coder:local', key='edit',
                                       source_pointer='accepted')
        connection = await db.get_db()
        try:   # promotion times keep whole seconds: the Accept reads as the same second as the job
            await connection.execute("UPDATE coding_project_promotions SET created_at=datetime((SELECT created_at FROM coder_workflows WHERE id=?))",
                                     (edit['id'],))
            await connection.commit()
        finally:
            await connection.close()
        await complete(tmp_path, edit['id'], 'e' * 40)
        project = await db.get_coding_project(build['project_id'])
        assert project['accepted_revision_id'] == 'e' * 40 and not (await store.get(edit['id'])).get('accepted_pointer')
        assert [(p['revision_id'], p['kind']) for p in await coder_promotion.promotions(build['project_id'])][0] == ('e' * 40, 'examples')
        # An Accept made after a job started still wins over that job's automatic promotion.
        later = await controller.create('conversation', 'Add CSV output', mode='edit_project', model='coder:local', key='later')
        await coder_promotion.accept(build['project_id'], 'a' * 40)
        await complete(tmp_path, later['id'], 'f' * 40)
        project = await db.get_coding_project(build['project_id'])
        assert (project['accepted_revision_id'], project['working_revision_id']) == ('a' * 40, 'f' * 40)
        assert (await store.get(later['id']))['accepted_pointer'] == 'kept_user_promotion'
    asyncio.run(scenario())


def test_deleting_the_conversation_of_a_candidate_never_locks_its_uploaded_project(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        await db.create_conversation('first', model='coder:local')
        await db.upsert_coding_project(project_id='proj-up', name='Upload', conversation_id='first', openhands_project_id='proj-up')
        repair = await controller.create('first', 'Fix the bug', mode='edit_project', project_id='proj-up', model='coder:local', key='u')
        assert repair['source_pointer'] == 'upload'
        await deliver_candidate(tmp_path, repair['id'], 'a' * 40, checks_passed=False)
        await db.delete_conversation('first')   # the job cascades with it; its working pointer must not stay behind
        assert not (await db.get_coding_project('proj-up'))['working_job_id']
        edit = await controller.create('conversation', 'Now add Y', mode='edit_project', project_id='proj-up', model='coder:local', key='y')
        assert edit['source_pointer'] == 'upload' and edit['source_project_id'] == 'proj-up'
        # A pointer that already dangles (an older database, or the bulk delete) is cleared when a job is created.
        await store.save(edit['id'], state='cancelled')
        connection = await db.get_db()
        try:
            await connection.execute("UPDATE coding_projects SET working_job_id='cw3-gone',working_revision_id=?,working_artifact_id='x' "
                                     "WHERE id='proj-up'", ('b' * 40,))
            await connection.commit()
        finally:
            await connection.close()
        again = await controller.create('conversation', 'And Z', mode='edit_project', project_id='proj-up', model='coder:local', key='z')
        assert again['source_pointer'] == 'upload' and again['working_job_at_start'] == ''
        assert (await db.get_coding_project('proj-up'))['working_revision_id'] == ''
    asyncio.run(scenario())


async def restart():
    await db.init_db()   # the real startup path: every deploy restarts the service


async def raw(sql, *params):
    connection = await db.get_db()
    try:
        await connection.execute(sql, params)
        await connection.commit()
    finally:
        await connection.close()


def test_deleting_the_newer_deliverys_conversation_repoints_working_and_a_restart_keeps_it(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40, checks_passed=False)
        await db.create_conversation('second', model='coder:local')
        follow = await controller.create('second', 'Add JSON', mode='edit_project', project_id=build['project_id'],
                                         model='coder:local', key='f')
        await deliver_candidate(tmp_path, follow['id'], 'b' * 40, checks_passed=False)
        await db.delete_conversation('second')
        # Blanked, the next edit was refused until a restart refilled the pointer under jobs started meanwhile.
        project = await db.get_coding_project(build['project_id'])
        assert (project['working_job_id'], project['working_revision_id']) == (build['id'], 'a' * 40)
        edit = await controller.create('conversation', 'Add CSV', mode='edit_project', project_id=build['project_id'],
                                       model='coder:local', key='e')
        assert (edit['source_job_id'], edit['source_revision_id'], edit['source_pointer']) == (build['id'], 'a' * 40, 'working')
        await store.save(edit['id'], state='blocked', blocker='paused')
        await restart()
        project = await db.get_coding_project(build['project_id'])
        assert (project['working_job_id'], project['working_revision_id']) == (build['id'], 'a' * 40)
        assert (await store.save(edit['id'], state='queued', event='resume'))['state'] == 'queued'
        await restart()   # left running across a restart, it still delivers
        assert await deliver_candidate(tmp_path, edit['id'], 'c' * 40, checks_passed=False)
    asyncio.run(scenario())


def test_deleting_a_later_repairs_conversation_keeps_an_uploaded_project_on_the_surviving_fix(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        for name in ('first', 'second'):
            await db.create_conversation(name, model='coder:local')
        await db.upsert_coding_project(project_id='proj-up', name='Upload', conversation_id='first', openhands_project_id='proj-up')
        first = await controller.create('first', 'Fix the bug', mode='edit_project', project_id='proj-up', model='coder:local', key='u1')
        await deliver_candidate(tmp_path, first['id'], 'a' * 40, checks_passed=False)
        second = await controller.create('second', 'Add Y', mode='edit_project', project_id='proj-up', model='coder:local', key='u2')
        await deliver_candidate(tmp_path, second['id'], 'b' * 40, checks_passed=False)
        await db.delete_conversation('second')
        # Was seeded from the raw upload, silently dropping the first fix, and stranded by the next restart.
        edit = await controller.create('conversation', 'Add Z', mode='edit_project', project_id='proj-up', model='coder:local', key='m')
        assert (edit['source_job_id'], edit['source_revision_id'], edit['source_pointer']) == (first['id'], 'a' * 40, 'working')
        await store.save(edit['id'], state='blocked', blocker='paused')
        await restart()
        assert (await store.save(edit['id'], state='queued', event='resume'))['state'] == 'queued'
        assert await deliver_candidate(tmp_path, edit['id'], 'c' * 40, checks_passed=False)
    asyncio.run(scenario())


def test_a_job_started_from_the_deleted_delivery_still_delivers_unless_newer_work_survives(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40, checks_passed=False)
        await db.create_conversation('second', model='coder:local')
        project_id = build['project_id']
        follow = await controller.create('second', 'Add JSON', mode='edit_project', project_id=project_id, model='coder:local', key='f')
        await deliver_candidate(tmp_path, follow['id'], 'b' * 40, checks_passed=False)
        running = await controller.create('conversation', 'Add CSV', mode='edit_project', project_id=project_id, model='coder:local', key='z')
        assert (running['source_revision_id'], running['working_job_at_start']) == ('b' * 40, follow['id'])
        await db.delete_conversation('second')
        # Working moved back to a, which this job had already seen: re-pointing must not make it stale.
        assert await deliver_candidate(tmp_path, running['id'], 'c' * 40, checks_passed=False)
        # A job that saw b when work in a kept conversation was delivered after it started stays stale.
        await db.create_conversation('third', model='coder:local')
        late = await controller.create('third', 'Add XML', mode='edit_project', project_id=project_id, model='coder:local', key='t1')
        await deliver_candidate(tmp_path, late['id'], 'l' * 40, checks_passed=False)
        parked = await controller.create('conversation', 'Add YAML', mode='edit_project', project_id=project_id,
                                         model='coder:local', key='p')
        assert parked['working_job_at_start'] == late['id']
        await store.save(parked['id'], state='blocked', blocker='paused')
        newer = await controller.create('conversation', 'Add TOML', mode='edit_project', project_id=project_id, model='coder:local', key='n')
        await deliver_candidate(tmp_path, newer['id'], 'n' * 40, checks_passed=False)
        top = await controller.create('third', 'Add INI', mode='edit_project', project_id=project_id, model='coder:local', key='t2')
        await deliver_candidate(tmp_path, top['id'], 't' * 40, checks_passed=False)
        await db.delete_conversation('third')
        assert (await db.get_coding_project(project_id))['working_revision_id'] == 'n' * 40
        with pytest.raises(ValueError, match='newer version'):
            await store.save(parked['id'], state='queued', event='resume')
    asyncio.run(scenario())


async def accepted_upload(tmp_path, automatic=False):
    """An uploaded project whose repair (conversation 'first') was accepted by the owner or completed by examples."""
    await db.create_conversation('first', model='coder:local')
    await db.upsert_coding_project(project_id='proj-up', name='Upload', conversation_id='first', openhands_project_id='proj-up')
    repair = await controller.create('first', 'Fix the bug', mode='edit_project', project_id='proj-up', model='coder:local', key='u')
    if automatic:
        await complete(tmp_path, repair['id'], 'a' * 40)
    else:
        await deliver_candidate(tmp_path, repair['id'], 'a' * 40)
        await coder_promotion.accept('proj-up', 'a' * 40)
    project = await db.get_coding_project('proj-up')
    assert project['accepted_job_id'] == repair['id'] and project['openhands_project_id'] == 'proj-up'   # was the job id
    return repair


@pytest.mark.parametrize('automatic', [False, True], ids=['accept', 'examples'])
def test_a_deleted_accepted_upload_repair_is_reported_never_silently_dropped(monkeypatch, tmp_path, automatic):
    setup(monkeypatch, tmp_path)
    async def scenario():
        repair = await accepted_upload(tmp_path, automatic)
        await db.delete_conversation('first')
        project = await db.get_coding_project('proj-up')
        assert not project['working_job_id'] and not project['accepted_job_id'] and not project['accepted_revision_id']
        promotions = await coder_promotion.promotions('proj-up')
        assert promotions[0]['job_id'] == repair['id']   # the log and the pinned accepted archive stay
        assert promotions[0]['artifact_id'] in await coder_promotion.pinned_artifact_ids()
        # Asking for the accepted version gets the truth, not "no accepted version yet" or the raw upload.
        with pytest.raises(ValueError, match='deleted with its conversation'):
            await controller.create('conversation', 'Add Y', mode='edit_project', project_id='proj-up', model='coder:local',
                                    key='ya', source_pointer='accepted')
        # A default edit would seed from the raw upload and drop the accepted fix: refused the same way, before and
        # after a restart, while the accepted archive stays downloadable to upload again.
        for key in ('y', 'z'):
            with pytest.raises(ValueError, match='upload the accepted archive'):
                await controller.create('conversation', 'Add Y', mode='edit_project', project_id='proj-up', model='coder:local', key=key)
            await restart()
        project = await db.get_coding_project('proj-up')
        assert not project['working_job_id'] and not project['accepted_job_id']
    asyncio.run(scenario())


def test_an_accepted_pointer_left_dangling_by_an_older_database_is_reported_then_released_once(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        repair = await accepted_upload(tmp_path)
        await raw('DELETE FROM coder_workflows WHERE id=?', repair['id'])   # gone without release_working_pointers
        # Still named as the accepted version: never silently replaced by the raw upload.
        with pytest.raises(ValueError, match='deleted with its conversation'):
            await controller.create('conversation', 'Add Y', mode='edit_project', project_id='proj-up', model='coder:local', key='y')
        await raw('PRAGMA user_version=1')
        await restart()
        project = await db.get_coding_project('proj-up')
        assert not project['working_job_id'] and not project['accepted_job_id']   # released once by the backfill
        with pytest.raises(ValueError, match='upload the accepted archive'):
            await controller.create('conversation', 'Add Y', mode='edit_project', project_id='proj-up', model='coder:local', key='y2')
    asyncio.run(scenario())


def test_deleting_every_conversation_releases_both_pointers(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        build = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        await deliver_candidate(tmp_path, build['id'], 'a' * 40)
        await coder_promotion.accept(build['project_id'], 'a' * 40)
        assert (await db.get_coding_project(build['project_id']))['openhands_project_id'] == build['id']   # a build keeps the job id
        connection = await db.get_db()
        try:   # as DELETE /api/conversations does
            await db.release_working_pointers(connection, db.current_user_id())
            await connection.execute('DELETE FROM conversations WHERE user_id=?', (db.current_user_id(),))
            await connection.commit()
        finally:
            await connection.close()
        project = await db.get_coding_project(build['project_id'])
        assert not project['working_job_id'] and not project['accepted_job_id']
    asyncio.run(scenario())


async def accepted_in_second(tmp_path, kind):
    """A delivers in a kept conversation, then B (conversation 'second') delivers b and the owner Accepts b."""
    await db.create_conversation('second', model='coder:local')
    if kind == 'build':
        first = await controller.create('conversation', 'Build a word counter', model='coder:local', key='build')
        project_id = first['project_id']
    else:
        await db.create_conversation('first', model='coder:local')
        await db.upsert_coding_project(project_id='proj-up', name='Upload', conversation_id='first', openhands_project_id='proj-up')
        project_id = 'proj-up'
        first = await controller.create('first', 'Fix the bug', mode='edit_project', project_id=project_id, model='coder:local', key='u1')
    await deliver_candidate(tmp_path, first['id'], 'a' * 40, checks_passed=False)
    later = await controller.create('second', 'Add JSON', mode='edit_project', project_id=project_id, model='coder:local', key='f')
    await deliver_candidate(tmp_path, later['id'], 'b' * 40)
    await coder_promotion.accept(project_id, 'b' * 40)
    return project_id, first, later


@pytest.mark.parametrize('kind', ['build', 'upload'])
def test_deleting_the_accepted_jobs_conversation_never_rebases_working_onto_an_older_delivery(monkeypatch, tmp_path, kind):
    setup(monkeypatch, tmp_path)
    async def scenario():
        project_id, first, _ = await accepted_in_second(tmp_path, kind)
        await db.delete_conversation('second')
        # Working moved back to a, delivered before the owner accepted b: the next edit silently dropped b.
        project = await db.get_coding_project(project_id)
        assert not project['working_job_id'] and not project['accepted_job_id']
        for key in ('e1', 'e2'):
            with pytest.raises(ValueError, match='upload the accepted archive'):
                await controller.create('conversation', 'Add CSV', mode='edit_project', project_id=project_id, model='coder:local', key=key)
            await restart()
    asyncio.run(scenario())


def test_a_delivery_made_after_the_lost_accept_still_takes_over_working(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        project_id, first, _ = await accepted_in_second(tmp_path, 'build')
        # The Accept a minute ago, a delivered before it (one test runs inside one second).
        await raw("UPDATE coder_workflow_events SET created_at=strftime('%Y-%m-%dT%H:%M:%f','now','-2 minutes') WHERE workflow_id=?", first['id'])
        await raw("UPDATE coding_project_promotions SET created_at=datetime('now','-1 minute')")
        await db.create_conversation('third', model='coder:local')
        kept = await controller.create('third', 'Add XML', mode='edit_project', project_id=project_id, model='coder:local', key='t')
        await deliver_candidate(tmp_path, kept['id'], 'c' * 40, checks_passed=False)
        top = await controller.create('second', 'Add INI', mode='edit_project', project_id=project_id, model='coder:local', key='s')
        await deliver_candidate(tmp_path, top['id'], 'd' * 40, checks_passed=False)
        await db.delete_conversation('second')
        edit = await controller.create('conversation', 'Add CSV', mode='edit_project', project_id=project_id, model='coder:local', key='e')
        assert (edit['source_job_id'], edit['source_revision_id']) == (kept['id'], 'c' * 40)
        await store.save(edit['id'], state='cancelled')
        # Accepted was already cleared; once c goes too, a still predates the lost Accept.
        await db.delete_conversation('third')
        assert not (await db.get_coding_project(project_id))['working_job_id']
        with pytest.raises(ValueError, match='upload the accepted archive'):
            await controller.create('conversation', 'Add TOML', mode='edit_project', project_id=project_id, model='coder:local', key='e2')
    asyncio.run(scenario())


def test_the_backfill_never_fills_working_from_before_an_accepted_version_whose_job_is_gone(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        project_id, first, later = await accepted_in_second(tmp_path, 'build')
        # An older database: no working pointer or promotion log, the accepted job deleted without a release.
        await raw('DELETE FROM coder_workflows WHERE id=?', later['id'])
        await raw("UPDATE coding_projects SET working_job_id='',working_revision_id='',working_artifact_id='' WHERE id=?", project_id)
        await raw('DELETE FROM coding_project_promotions')
        await raw('PRAGMA user_version=1')
        await restart()
        project = await db.get_coding_project(project_id)
        assert not project['working_job_id'] and not project['accepted_job_id']
        assert [(p['revision_id'], p['kind']) for p in await coder_promotion.promotions(project_id)] == [('b' * 40, 'controller')]
        with pytest.raises(ValueError, match='upload the accepted archive'):
            await controller.create('conversation', 'Add CSV', mode='edit_project', project_id=project_id, model='coder:local', key='e')
    asyncio.run(scenario())
