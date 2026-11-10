"""Source admission preserves exact archives, bounded recovery and cancellation."""
import asyncio
from copy import deepcopy
import hashlib
from pathlib import Path
import time

import httpx
import pytest

import coder_source_upload as upload


class Store:
    def __init__(self):
        self.job = {'id': 'job', 'repair_round': 1, 'calls_used': 7, 'seconds_used': 9}
    async def get(self, identity): return deepcopy(self.job)
    async def save(self, identity, **changes):
        self.job.update(deepcopy(changes))
        return await self.get(identity)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(upload, 'RETRY_SECONDS', .001)
    path = tmp_path / 'source'; path.write_bytes(b'archive' * 200000)
    settings = dict(daedalus_upload_mb=20, daedalus_extracted_mb=50, daedalus_min_free_mb=1,
                    daedalus_preparation_seconds=600)
    return Store(), path, hashlib.sha256(path.read_bytes()).hexdigest(), settings


def run(fixture, handler, *, client_type=httpx.AsyncClient):
    store, path, checksum, settings = fixture
    async def work():
        async with client_type(transport=httpx.MockTransport(handler)) as client:
            await upload.ensure_source(store, client, 'job', 'http://worker/source/' + checksum, path, checksum, settings)
    return asyncio.run(work())


@pytest.mark.parametrize('fault', ['maintenance', 'partial', 'lost_response'])
def test_recovery_uses_fresh_archive_or_exact_committed_receipt(fixture, fault):
    store, path, checksum, _ = fixture
    bodies = []; committed = False
    async def handler(request):
        nonlocal committed
        if request.method == 'GET': return httpx.Response(200 if committed else 404, json={'sha256': checksum})
        bodies.append(await request.aread())
        if len(bodies) == 1:
            if fault == 'lost_response': committed = True
            if fault == 'maintenance': return httpx.Response(503, json={'detail': 'VM maintenance is in progress; retry shortly'})
            raise httpx.ReadError('lost response', request=request)
        committed = True
        return httpx.Response(200, json={'sha256': checksum})
    run(fixture, handler)
    assert len(bodies) == (1 if fault == 'lost_response' else 2)
    assert all(body == path.read_bytes() for body in bodies)
    assert store.job['source_preparation']['status'] == 'complete'
    assert (store.job['repair_round'], store.job['calls_used'], store.job['seconds_used']) == (1, 7, 9)


def test_partially_consumed_request_opens_a_new_stream(fixture):
    bodies = []
    class Client:
        async def get(self, url, **kwargs): return httpx.Response(404, request=httpx.Request('GET', url))
        async def put(self, url, *, content, **kwargs):
            body = b''
            async for chunk in content:
                body += chunk
                if not bodies:
                    bodies.append(body)
                    await content.aclose()
                    raise httpx.WriteError('connection lost')
            bodies.append(body)
            return httpx.Response(200, json={'sha256': fixture[2]}, request=httpx.Request('PUT', url))
    asyncio.run(upload.ensure_source(fixture[0], Client(), 'job', 'http://worker/source', *fixture[1:]))
    assert len(bodies[0]) < len(bodies[1]) and bodies[1] == fixture[1].read_bytes()


@pytest.mark.parametrize('method', ['GET', 'PUT'])
def test_repeated_admission_failure_is_bounded_across_resume(fixture, method):
    requests = []
    def handler(request):
        requests.append(request.method)
        if request.method != method: return httpx.Response(404)
        return httpx.Response(503, json={'detail': 'VM maintenance is in progress; retry shortly'})
    with pytest.raises((TimeoutError, httpx.HTTPError)): run(fixture, handler)
    before = list(requests)
    # A fresh caller, including new execution allowance, cannot replay admission.
    fixture[0].job['allowance'] = 2
    with pytest.raises(TimeoutError): run(fixture, handler)
    assert before == requests and requests.count(method) == 3
    assert fixture[0].job['failure']['stage'] == 'source_preparation'
    assert fixture[0].job['source_preparation']['attempts'] == 3


@pytest.mark.parametrize('status', [400, 401, 403, 413, 422, 500, 503, 507])
def test_non_admission_failure_never_retries(fixture, status):
    uploads = []
    def handler(request):
        if request.method == 'GET': return httpx.Response(404)
        uploads.append(True)
        return httpx.Response(status, json={'detail': 'Invalid archive or unavailable storage'})
    with pytest.raises(httpx.HTTPStatusError): run(fixture, handler)
    assert len(uploads) == 1


def test_wrong_receipt_blocks_dispatch(fixture):
    with pytest.raises(ValueError, match='checksum receipt'):
        run(fixture, lambda r: httpx.Response(200, json={'sha256': 'b' * 64}))
    assert fixture[0].job['source_preparation']['status'] == 'blocked'


@pytest.mark.parametrize('phase', ['upload', 'wait'])
def test_stop_interrupts_upload_or_retry_wait(fixture, monkeypatch, phase):
    monkeypatch.setattr(upload, 'RETRY_SECONDS', 60)
    entered = asyncio.Event(); cancelled = []
    async def handler(request):
        if request.method == 'GET': return httpx.Response(404)
        entered.set()
        if phase == 'wait': return httpx.Response(503, json={'detail': 'VM maintenance is in progress; retry shortly'})
        try: await asyncio.sleep(60)
        finally: cancelled.append(True)
    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(upload.ensure_source(fixture[0], client, 'job', 'http://worker/source', *fixture[1:]))
            await entered.wait()
            fixture[0].job['cancel_requested'] = True
            with pytest.raises(InterruptedError): await asyncio.wait_for(task, 1)
    asyncio.run(work())
    assert phase == 'wait' or cancelled
    assert fixture[0].job['source_preparation']['status'] != 'complete'


def test_expired_durable_deadline_cannot_be_reset(fixture):
    fixture[0].job['source_preparation'] = {'sha256': fixture[2], 'deadline': time.time()-1, 'status':'pending', 'attempts':1}
    def unexpected(request): pytest.fail('Expired admission must not touch the worker')
    with pytest.raises(TimeoutError): run(fixture, unexpected)
    assert fixture[0].job['source_preparation']['attempts'] == 1


def test_controller_dispatches_exact_parent_once_and_resume_keeps_ledger(tmp_path, monkeypatch):
    import json
    import coder_jobs
    from db import coder_jobs as jobs
    from .test_daedalus_jobs import setup
    setup(monkeypatch, tmp_path)
    monkeypatch.setattr(upload, 'RETRY_SECONDS', .001)
    path=tmp_path/'accepted.tar.gz';path.write_bytes(b'accepted exact source')
    checksum=hashlib.sha256(path.read_bytes()).hexdigest(); revision='a'*64
    uploaded=[]; operations={}; payloads=[]
    def remote(request):
        if '/source/' in request.url.path:
            if request.method=='GET': return httpx.Response(404 if len(uploaded)<2 else 200,json={'sha256':checksum})
            uploaded.append(request.content)
            return httpx.Response(503,json={'detail':'VM maintenance is in progress; retry shortly'}) if len(uploaded)==1 else httpx.Response(200,json={'sha256':checksum})
        if request.url.path.endswith('/events'): return httpx.Response(200,json=[])
        if request.method=='POST':
            payload=json.loads(request.content);payloads.append(payload)
            operations.setdefault(request.url.path,{'status':'succeeded','calls':0,'started':100,'ended':100,'result':{}})
        return httpx.Response(200,json=operations[request.url.path])
    async def run():
        parent=await jobs.create('conversation','build','build_from_prompt','','coder:local','parent',engine='openhands159',policy_version=7)
        await jobs.save(parent['id'],revision_id=revision,candidate_artifact={'storage_path':str(path),'sha256':checksum,'metadata':{'revision_id':revision}})
        job=await jobs.create('conversation','edit','build_from_prompt','','coder:local','edit',engine='openhands159',policy_version=7)
        job=await jobs.save(job['id'],mode='edit_existing_project',source_job_id=parent['id'],source_revision_id=revision,source_pointer='accepted',repair_round=1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
            coder_jobs.configure(client,None)
            _, job=await coder_jobs._operate(job,'inspect')
            _, job=await coder_jobs._operate(job,'inspect')
            assert len(operations)==1 and len(uploaded)==2 and all(b==path.read_bytes() for b in uploaded)
            assert all(p['source_archive_sha256']==checksum and p['source_revision_id']==revision for p in payloads)
            assert job['calls_used']==0 and job['repair_round']==1
            ledger=job['source_preparation']
            await jobs.save(job['id'],state='blocked',resume_state='queued')
            resumed=await coder_jobs.resume(job['id'],budget_mode='new_allowance')
            assert resumed['source_preparation']==ledger and resumed['repair_round']==1
    asyncio.run(run())
