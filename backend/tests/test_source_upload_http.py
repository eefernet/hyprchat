"""Real loopback HTTP, worker validation and maintenance admission locks."""
import asyncio
import hashlib
import io
import socket
import tarfile
import threading
import time

import httpx
import pytest
from fastapi import FastAPI
import uvicorn

from coder_source_upload import ensure_source
from coder_worker_runtime import install_routes
from hephaestus_scope import headers, install_worker_auth
from hephaestus_inventory import lock
from .test_source_upload import Store


@pytest.fixture
def worker(tmp_path, monkeypatch):
    root = tmp_path/'managed'; state=root/'state/test'; state.mkdir(parents=True)
    monkeypatch.setenv('HEPHAESTUS_ROOT', str(root))
    monkeypatch.setenv('DAEDALUS_STATE_DIR', str(state))
    monkeypatch.setenv('HEPHAESTUS_EXECUTOR', 'docker')
    monkeypatch.setenv('HEPHAESTUS_TOKEN', 'fixture-only-token')
    monkeypatch.setenv('HEPHAESTUS_STORE_ROLE', 'test')
    monkeypatch.setattr('coder_source_upload.RETRY_SECONDS', .01)
    app=FastAPI(); install_routes(app, root/'projects', engine='openhands159'); install_worker_auth(app)
    sock=socket.socket(); sock.bind(('127.0.0.1',0)); sock.listen()
    server=uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    thread=threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True); thread.start()
    deadline=time.monotonic()+5
    while not server.started:
        assert thread.is_alive() and time.monotonic()<deadline
        time.sleep(.01)
    body=io.BytesIO()
    with tarfile.open(fileobj=body,mode='w:gz') as archive:
        data=b'print("accepted parent")\n'; info=tarfile.TarInfo('app.py');info.size=len(data);archive.addfile(info,io.BytesIO(data))
    path=tmp_path/'source.tar.gz';path.write_bytes(body.getvalue());sha=hashlib.sha256(body.getvalue()).hexdigest()
    settings={'daedalus_upload_mb':2,'daedalus_extracted_mb':2,'daedalus_min_free_mb':1,'daedalus_preparation_seconds':5}
    try: yield root,state,f'http://127.0.0.1:{sock.getsockname()[1]}',path,sha,settings
    finally:
        server.should_exit=True;thread.join(5);sock.close()
        assert not thread.is_alive()


@pytest.mark.parametrize('fault', ['maintenance','lost_response','exhausted'])
def test_real_admission_and_exact_source_recovery(worker, fault):
    root,state,base,path,sha,settings=worker;store=Store();requests=[];held=[]
    async def request(req):
        requests.append(req.method)
        if req.method=='PUT' and requests.count('PUT')==1 and fault!='lost_response':
            gate=lock(root,'admission',blocking=False);gate.__enter__();held.append(gate)
    async def response(resp):
        await resp.aread()
        if resp.status_code==503 and fault=='maintenance': held.pop().__exit__(None,None,None)
        if resp.request.method=='PUT' and fault=='lost_response': raise httpx.ReadError('Response lost after publication')
    async def run():
        async with httpx.AsyncClient(headers=headers('owner'),event_hooks={'request':[request],'response':[response]}) as client:
            await ensure_source(store, client, 'job',base+'/jobs/cw3-abc/source/'+sha,path,sha,settings)
    try:
        if fault=='exhausted':
            with pytest.raises(TimeoutError): asyncio.run(run())
            assert requests.count('PUT')==1 and store.job['source_preparation']['attempts']==3
            assert not (state/'sources/cw3-abc'/f'{sha}.tar.gz').exists()
        else:
            asyncio.run(run())
            assert requests.count('PUT')==(2 if fault=='maintenance' else 1)
            assert (state/'sources/cw3-abc'/f'{sha}.tar.gz').read_bytes()==path.read_bytes()
            assert store.job['source_preparation']['status']=='complete'
            # Another owner cannot inspect or replace the accepted source.
            with httpx.Client(headers=headers('different-owner')) as client:
                assert client.get(base+'/jobs/cw3-abc/source/'+sha).status_code==404
        assert not list((state/'sources').rglob('*.part'))
        assert (store.job['calls_used'], store.job['repair_round'])==(7,1)
    finally:
        for gate in held: gate.__exit__(None,None,None)


def test_overlapping_streams_publish_only_valid_exact_sources(worker):
    root,state,base,path,sha,settings=worker
    async def run():
        sent=asyncio.Event();finish=asyncio.Event()
        async def partial():
            yield path.read_bytes()[:10];sent.set();await finish.wait();yield path.read_bytes()[10:]
        url=base+'/jobs/cw3-abc/source/'+sha
        async with httpx.AsyncClient(headers=headers('owner')) as client:
            first=asyncio.create_task(client.put(url,content=partial(),params={'free_mb':1}))
            await sent.wait()
            second=await client.put(url,content=path.read_bytes(),params={'free_mb':1})
            finish.set();response=await first
            assert response.status_code==second.status_code==200
            rejected=await client.put(base+'/jobs/cw3-abc/source/'+'0'*64,content=path.read_bytes(),params={'free_mb':1})
            assert rejected.status_code==422
    asyncio.run(run())
    assert (state/'sources/cw3-abc'/f'{sha}.tar.gz').read_bytes()==path.read_bytes()
    assert not list((state/'sources').rglob('*.part'))
