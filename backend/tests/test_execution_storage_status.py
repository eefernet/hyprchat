"""Read-only Hephaestus capacity, unavailable states and private metadata filtering."""
import asyncio
import httpx
import pytest
from hephaestus_scope import verify
from storage_diagnostics import execution_storage_status


def read(handler, monkeypatch, owner='admin'):
    monkeypatch.setenv('HEPHAESTUS_TOKEN', 'storage-test-secret')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await execution_storage_status(client, 'http://worker:8585/', owner)
    return asyncio.run(run())


def test_signed_read_only_request_and_allowlisted_response(monkeypatch):
    def handler(request):
        assert request.method == 'GET' and str(request.url) == 'http://worker:8585/status'
        scope = verify(request.headers, 'storage-test-secret')
        assert scope['owner'] == 'admin' and scope['project'] == 'storage-status'
        return httpx.Response(200, json={'runtime': 'hephaestus', 'disk': {'total': 100, 'used': 60, 'free': 40}, 'private': '/secret', 'languages': ['python']})
    assert read(handler, monkeypatch) == {'status': 'ok', 'runtime': 'hephaestus', 'disk': {'total': 100, 'used': 60, 'free': 40}}


@pytest.mark.parametrize('body', [None, [], {'runtime': 'codebox'}, {'runtime': 'hephaestus', 'disk': None},
    {'runtime': 'hephaestus', 'disk': {'total': 0, 'used': 0, 'free': 0}},
    {'runtime': 'hephaestus', 'disk': {'total': 100, 'used': -1, 'free': 101}},
    {'runtime': 'hephaestus', 'disk': {'total': 100, 'used': 80, 'free': 80}},
    {'runtime': 'hephaestus', 'disk': {'total': 100, 'used': True, 'free': 99}},
    {'runtime': 'hephaestus', 'disk': {'total': '100', 'used': 1, 'free': 99}},
])
def test_invalid_data_is_unavailable_not_zero_capacity(body, monkeypatch):
    result = read(lambda request: httpx.Response(200, json=body), monkeypatch)
    assert result['status'] == 'unavailable' and 'disk' not in result


@pytest.mark.parametrize('status', [301, 401, 403, 500])
def test_upstream_failures_never_expose_body_or_follow_redirects(status, monkeypatch):
    def handler(request):
        return httpx.Response(status, text='secret upstream details', headers={'Location': 'http://other/status'})
    result = read(handler, monkeypatch)
    assert result['status'] == 'unavailable' and f'HTTP {status}' in result['message']
    assert 'secret' not in result['message']


@pytest.mark.parametrize('error', [httpx.ConnectError, httpx.ReadTimeout])
def test_offline_vm_keeps_local_storage_available(error, monkeypatch):
    def handler(request):
        raise error('private host details', request=request)
    result = read(handler, monkeypatch)
    assert result['status'] == 'unavailable' and 'Local files are still available' in result['message']


def test_unconfigured_or_unsigned_never_calls_worker(monkeypatch):
    def handler(request):
        pytest.fail('Must not send an unauthenticated request')
    assert read(handler, monkeypatch, owner='')['status'] == 'unavailable'
    monkeypatch.delenv('HEPHAESTUS_TOKEN')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await execution_storage_status(client, 'http://worker:8585', 'admin')
    assert asyncio.run(run())['status'] == 'not_configured'
