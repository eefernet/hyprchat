"""Office comparisons, immutable restoration and durable batch exports."""
import asyncio
import difflib
import hashlib
import io
import json
import uuid
import zipfile

from fastapi import HTTPException
import cancel_registry
import database as db
import documents
from document_formats import inspect_document, extract_text, OFFICE_EXTS


async def family(artifact_id):
    artifact,_=await documents.owned_source(artifact_id)
    conn=await db.get_db()
    try:
        rows=await conn.execute_fetchall('''WITH RECURSIVE related(id) AS (
            SELECT ? UNION SELECT a.id FROM artifacts a JOIN related r
            ON a.supersedes_artifact_id=r.id OR a.id=(SELECT supersedes_artifact_id FROM artifacts WHERE id=r.id)
            WHERE a.user_id=?) SELECT a.id,a.filename,a.sha256,a.created_at,a.supersedes_artifact_id
            FROM artifacts a JOIN related r ON r.id=a.id WHERE a.user_id=? ORDER BY a.created_at DESC LIMIT 100''',
            (artifact_id,db.current_user_id(),db.current_user_id()))
        return [dict(r) for r in rows]
    finally:await conn.close()


async def compare(current_id,earlier_id):
    current,current_path=await documents.owned_source(current_id)
    earlier,earlier_path=await documents.owned_source(earlier_id)
    if current_path.suffix.lower() not in OFFICE_EXTS or earlier_path.suffix.lower() not in OFFICE_EXTS:
        raise HTTPException(415,'Comparison requires DOCX, PPTX or XLSX documents')
    async def inspect(path):
        text,structure=await asyncio.gather(asyncio.to_thread(extract_text,path,200000),asyncio.to_thread(inspect_document,path,limit=500))
        return text,structure
    (new,new_structure),(old,old_structure)=await asyncio.gather(inspect(current_path),inspect(earlier_path))
    return {'current':{'id':current_id,'filename':current['filename'],'sha256':current['sha256'],'structure':new_structure},
        'earlier':{'id':earlier_id,'filename':earlier['filename'],'sha256':earlier['sha256'],'structure':old_structure},
        'diff':'\n'.join(difflib.unified_diff(old.splitlines(),new.splitlines(),fromfile=earlier['filename'],tofile=current['filename'],lineterm='')),
        'bounded':True,'note':'Text is limited to 200,000 characters and structure to 500 entries per document. Visual formatting requires preview inspection.'}


async def restore(current_id,earlier_id,expected_sha256):
    current,path=await documents.owned_source(current_id)
    earlier,source=await documents.owned_source(earlier_id)
    if earlier_id not in {r['id'] for r in await family(current_id)}:raise HTTPException(409,'Choose a revision from the same document history')
    if not expected_sha256 or hashlib.sha256(path.read_bytes()).hexdigest()!=expected_sha256:raise HTTPException(409,'Document changed; refresh before restoring')
    data=await asyncio.to_thread(source.read_bytes)
    if hashlib.sha256(data).hexdigest()!=earlier['sha256']:raise HTTPException(409,'Earlier revision failed its integrity check')
    conn=await db.get_db()
    try:
        await conn.execute('BEGIN IMMEDIATE')
        successors=await conn.execute_fetchall('SELECT id FROM artifacts WHERE supersedes_artifact_id=? AND user_id=?',(current_id,db.current_user_id()))
        if successors:raise HTTPException(409,'A newer revision exists; open it before restoring')
        restored=await documents.store_file(data,earlier['filename'],conversation_id=current['conversation_id'],source=current,role='revision',
            content_text=earlier.get('content_text') or '',metadata={**(earlier.get('metadata') or {}),'restored_from':earlier_id},_connection=conn)
        await conn.commit()
    finally:await conn.close()
    return restored


async def submit_batch(http,artifact_ids,format,conversation_id):
    documents.require_enabled()
    if not 1<=len(artifact_ids)<=20 or len(set(artifact_ids))!=len(artifact_ids):raise HTTPException(400,'Choose 1–20 distinct documents')
    if format not in {'pdf','txt','csv','docx','pptx','xlsx'}:raise HTTPException(400,'Unsupported export format')
    if not await db.get_conversation(conversation_id):raise HTTPException(404,'Conversation not found')
    for aid in artifact_ids:
        _,path=await documents.owned_source(aid)
        if path.suffix.lower() not in OFFICE_EXTS:raise HTTPException(415,'Batch export requires Office documents')
    run_id='doc-batch-'+uuid.uuid4().hex
    await db.create_run(run_id,conversation_id,'documents',status='queued')
    await db.update_run(run_id,result_envelope={'action':'batch_export','format':format,'artifact_ids':artifact_ids,'results':[]})
    task=asyncio.create_task(_batch(http,run_id,artifact_ids,format,conversation_id))
    documents._tasks[run_id]=task
    task.add_done_callback(lambda done:documents._tasks.pop(run_id,None))
    return {'run_id':run_id,'status':'queued'}


async def _batch(http,run_id,artifact_ids,format,conversation_id):
    results=[];exports=[];total=0;child_id=None
    cancel_registry.register(run_id)
    try:
        row=await db.get_run(run_id)
        if not row or row['status']=='cancelling':raise cancel_registry.RunCancelled(run_id)
        await db.update_run(run_id,status='running')
        for aid in artifact_ids:
            if cancel_registry.is_cancelled(run_id):raise cancel_registry.RunCancelled(run_id)
            try:
                child=await documents.submit(http,{'action':'convert','artifact_id':aid,'format':format,'conversation_id':conversation_id},wait=False)
                child_id=child['run_id']
                await db.update_run(run_id,result_envelope={'action':'batch_export','results':results,'child_run_id':child_id,'completed':len(results),'total':len(artifact_ids)})
                async def wait_child():
                    while True:
                        row=await db.get_run(child_id)
                        if not row:raise RuntimeError('Child document job is unavailable')
                        if row['status'] not in {'queued','pending','running','cancelling'}:
                            return row.get('result_envelope') or {'status':row['status']}
                        await asyncio.sleep(.2)
                result=await cancel_registry.await_cancellable(wait_child(),run_id)
                child_id=None
                if result.get('status')!='succeeded':raise RuntimeError(result.get('error') or 'Conversion failed; inspect child job')
                outputs=[a for a in result.get('artifacts',[]) if a.get('role')=='export']
                if not outputs:raise RuntimeError('Conversion produced no export')
                exports.extend(outputs);results.append({'artifact_id':aid,'status':'succeeded','outputs':outputs})
            except (cancel_registry.RunCancelled,asyncio.CancelledError):raise
            except Exception as exc:results.append({'artifact_id':aid,'status':'failed','error':str(exc)[:500]})
            await db.update_run(run_id,result_envelope={'action':'batch_export','results':results,'completed':len(results),'total':len(artifact_ids)})
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as bundle:
            for n,export in enumerate(exports,1):
                artifact,path=await documents.owned_source(export['artifact_id'])
                data=await asyncio.to_thread(path.read_bytes);total+=len(data)
                if total>200*1024*1024:raise ValueError('Batch output exceeds 200 MB; individual exports are retained')
                bundle.writestr(f'{n:02d}-{artifact["filename"]}',data)
            bundle.writestr('results.json',json.dumps(results,indent=2))
        artifact=await documents.store_file(buffer.getvalue(),'document-exports.zip',conversation_id=conversation_id,run_id=run_id,role='bundle')
        status='succeeded' if all(r['status']=='succeeded' for r in results) else 'partial' if exports else 'failed'
        await db.update_run(run_id,status=status,result_envelope={'action':'batch_export','results':results,'artifact':artifact,'status':status},ended=True)
    except BaseException as exc:
        cancelled=isinstance(exc,(cancel_registry.RunCancelled,asyncio.CancelledError))
        status=('interrupted' if isinstance(exc,asyncio.CancelledError) else 'cancelled') if cancelled else 'failed'
        if child_id:
            child=await documents.cancel_run(http,child_id)
            if child['status'] in {'queued','running','cancelling'}:status='cancelling'
        await db.update_run(run_id,status=status,result_envelope={'action':'batch_export','results':results,'child_run_id':child_id,'error':str(exc)[:500],'status':status},ended=status!='cancelling')
        if isinstance(exc,asyncio.CancelledError):raise
    finally:cancel_registry.cleanup(run_id)
