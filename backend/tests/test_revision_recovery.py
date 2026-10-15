import asyncio
import hashlib
import io
import json
import zipfile
import pytest
from fastapi import HTTPException
import database as db
import documents
import document_revisions
import research_checkpoints as checkpoints
from .test_documents_service import env


def docx(text):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr('word/document.xml',f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return out.getvalue()


def test_document_comparison_restore_and_stale_head_fence(env):
    async def run():
        original=await documents.store_file(docx('Original'), 'original.docx',conversation_id='conv-docs',role='original')
        current=await documents.store_file(docx('Updated'), 'updated.docx',conversation_id='conv-docs',source=original,role='revision')
        comparison=await document_revisions.compare(current['id'],original['id'])
        assert '-Original' in comparison['diff'] and '+Updated' in comparison['diff']
        with pytest.raises(HTTPException) as error:await document_revisions.restore(current['id'],original['id'],'stale')
        assert error.value.status_code==409
        restored=await document_revisions.restore(current['id'],original['id'],current['sha256'])
        assert restored['id'] not in {current['id'],original['id']}
        assert restored['sha256']==original['sha256'] and restored['supersedes_artifact_id']==current['id']
        assert (await documents.owned_source(current['id']))[1].read_bytes()==docx('Updated')
        with pytest.raises(HTTPException) as stale:await document_revisions.restore(current['id'],original['id'],current['sha256'])
        assert stale.value.status_code==409
        user=await db.create_user('Other');token=db.set_current_user_id(user['id'])
        try:
            with pytest.raises(HTTPException) as denied:await document_revisions.compare(current['id'],original['id'])
            assert denied.value.status_code==404
        finally:db.reset_current_user_id(token)
    asyncio.run(run())


def test_research_versions_reject_mutated_evidence_and_keep_old_drafts(env):
    async def run():
        await db.create_research_report('report',query='fixture')
        await checkpoints.save('report','writing',{'composition':{}},{'num_ctx':32000},'Old draft')
        await checkpoints.save('report','section',{'composition':{},'writer':{'sections':{'Intro':'Saved'}}},{'num_ctx':32000},'New draft')
        versions=(await checkpoints.versions('report'))['versions']
        assert len(versions)==2
        compared=await checkpoints.compare('report',versions[-1]['id'],versions[0]['id'])
        assert '-Old draft' in compared['diff'] and '+New draft' in compared['diff']
        assert (await checkpoints.load('report'))['payload']['writer']['sections']['Intro']=='Saved'
        conn=await db.get_db()
        try:
            await conn.execute("INSERT INTO research_documents(report_id,source_id,content) VALUES('report','S1','new evidence')")
            await conn.commit()
        finally:await conn.close()
        with pytest.raises(ValueError,match='incompatible'):await checkpoints.load('report')
        assert len((await checkpoints.versions('report'))['versions'])==2
    asyncio.run(run())


def test_batch_cancellation_targets_only_child_and_waits_for_ack(env,monkeypatch):
    async def run():
        import task_queue
        for identity in ('doc-batch-parent','doc-child','doc-unrelated'):
            await db.create_run(identity,'conv-docs','documents',status='running')
        await db.update_run('doc-batch-parent',result_envelope={'action':'batch_export','child_run_id':'doc-child'})
        stopped=[]
        async def stop(http,identity):
            stopped.append(identity)
            return 'Worker acknowledgement pending'
        monkeypatch.setattr(documents,'stop_worker',stop)
        result=await documents.cancel_run(None,'doc-batch-parent')
        assert stopped==['doc-child'] and result['status']=='cancelling'
        assert (await db.get_run('doc-unrelated'))['status']=='running'
        await task_queue.reconcile_children()
        assert (await db.get_run('doc-batch-parent'))['status']=='cancelling'
        await db.update_run('doc-child',status='cancelled',ended=True)
        await task_queue.reconcile_children()
        assert (await db.get_run('doc-batch-parent'))['status']=='cancelled'
    asyncio.run(run())
