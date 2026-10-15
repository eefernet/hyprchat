import asyncio
import pytest
import database as db
import indexing_jobs


def test_saved_source_integrity_owner_fence_and_restart(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DATABASE_PATH',str(tmp_path/'index.db'))
    async def run():
        await db.init_db()
        await db.create_kb('kb','Fixture')
        path=tmp_path/'source.txt';path.write_text('Original')
        identity=await indexing_jobs.submit('kb','source.txt',path)
        assert (await indexing_jobs.get(identity))['status']=='queued'
        other=await db.create_user('Other');token=db.set_current_user_id(other['id'])
        try:
            assert await indexing_jobs.get(identity) is None
            with pytest.raises(LookupError):await indexing_jobs.execute(identity)
        finally:db.reset_current_user_id(token)
        path.write_text('Changed')
        result=await indexing_jobs.execute(identity)
        assert 'Source changed' in result['error']
        assert (await indexing_jobs.get(identity))['status']=='failed'
        second=await indexing_jobs.submit('kb','source.txt',path)
        await indexing_jobs.reconcile()
        assert (await indexing_jobs.get(second))['status']=='interrupted'
        assert (await indexing_jobs.get(identity))['status']=='failed'
    asyncio.run(run())
