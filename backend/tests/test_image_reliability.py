import ast
import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from PIL import Image
import config
import database as db
import image_job_store as store
from artifact_files import artifact_file_metadata


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path/'db.sqlite'))
    monkeypatch.setattr(config, 'SANDBOX_OUTPUTS_DIR', str(tmp_path/'outputs'))
    monkeypatch.setattr(config, 'ARTIFACT_DIR', str(tmp_path/'artifacts'))
    monkeypatch.setattr(config, 'COMFYUI_URL', 'http://fixture')
    asyncio.run(db.init_db())
    return tmp_path


def endpoints(comfy, http=None):
    tree=ast.parse((Path(__file__).resolve().parents[1]/'main.py').read_text())
    nodes=[]
    for node in tree.body:
        if isinstance(node,ast.AsyncFunctionDef) and node.name in {'get_image_job','cancel_image_job'}:
            node.decorator_list=[d for d in node.decorator_list if isinstance(d,ast.Attribute) and d.attr=='serialized']
            nodes.append(node)
    namespace={'config':config,'db':db,'image_job_store':store,'comfyui':comfy,'HTTPException':HTTPException,
               'asyncio':asyncio,'os':__import__('os'),'httpx':http,'_artifact_file_metadata':artifact_file_metadata,
               '_track_bg':asyncio.create_task}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'image-endpoints','exec'),namespace)
    return SimpleNamespace(**namespace)


def test_transfer_failure_is_retryable_without_cleanup_and_concurrent_polls_deduplicate(env):
    async def run():
        job=await store.create({})
        job.update(prompt_id='upstream',status='queued')
        await store.save(job)
        state={'fail':True,'forgot':0}
        async def history(_):return {'outputs':{'image':{}},'status':{'status_str':'success'}}
        async def fetch(_):
            if state['fail']:raise OSError('transfer interrupted')
            out=io.BytesIO();Image.new('RGB',(2,2)).save(out,format='PNG');return out.getvalue()
        async def forget(_):state['forgot']+=1
        async def free():pass
        comfy=SimpleNamespace(get_history=history,fetch_image=fetch,forget_job=forget,free_memory=free,
                              outputs_from_history=lambda _: [{'filename':'up.png'}])
        api=endpoints(comfy)
        with pytest.raises(HTTPException) as failed:await api.get_image_job(job['id'])
        assert failed.value.status_code==502
        assert (await store.get(job['id']))['status']=='queued'
        assert state['forgot']==0
        state['fail']=False
        results=await asyncio.gather(api.get_image_job(job['id']),api.get_image_job(job['id']))
        await asyncio.sleep(0)
        assert all(r['status']=='done' for r in results)
        assert results[0]['images']==results[1]['images']
        assert state['forgot']==1
        conn=await db.get_db()
        try:
            rows=await conn.execute_fetchall('SELECT storage_path FROM artifacts')
            assert len(rows)==1 and Path(rows[0]['storage_path']).is_relative_to(config.ARTIFACT_DIR)
        finally:await conn.close()
    asyncio.run(run())


def test_owner_checks_survive_restart(env):
    async def run():
        job=await store.create({'prompt':'private'})
        other=await db.create_user('Other')
        token=db.set_current_user_id(other['id'])
        try:
            assert await store.get(job['id']) is None
            with pytest.raises(HTTPException) as denied:await endpoints(SimpleNamespace()).get_image_job(job['id'])
            assert denied.value.status_code==404
        finally:db.reset_current_user_id(token)
        assert (await store.get(job['id']))['params']['prompt']=='private'
    asyncio.run(run())


def test_queued_cancel_is_targeted_and_unconfirmed_cancel_stays_pending(env):
    async def run():
        job=await store.create({});job.update(prompt_id='mine',status='queued');await store.save(job)
        calls=[]
        class Client:
            async def __aenter__(self):return self
            async def __aexit__(self,*a):pass
            async def post(self,url,json):calls.append((url,json));return SimpleNamespace(raise_for_status=lambda:None)
        async def position(_,strict=False):return None
        comfy=SimpleNamespace(queue_position=position)
        api=endpoints(comfy,SimpleNamespace(AsyncClient=lambda **kw:Client()))
        assert (await api.cancel_image_job(job['id']))['status']=='cancelled'
        assert calls==[('http://fixture/queue',{'delete':['mine']})]
        job['status']='queued';await store.save(job)
        async def unreachable(_,strict=False):raise OSError('queue unreachable')
        comfy.queue_position=unreachable
        with pytest.raises(OSError):await api.cancel_image_job(job['id'])
        assert (await store.get(job['id']))['status']=='cancelling'
    asyncio.run(run())
