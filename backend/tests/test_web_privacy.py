"""Privacy regressions: no persistent site identifiers or query-bearing logs."""
import asyncio
import logging
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import research
from web_privacy import install_private_access_logging


def test_public_fetches_do_not_replay_cookies_across_redirects_or_searches(monkeypatch):
    requests = []

    async def handle(request):
        requests.append((request.url.path, request.headers.get('cookie')))
        if request.url.path == '/first':
            return httpx.Response(302, headers={
                'location': '/second', 'set-cookie': 'visitor=profile-alice; Path=/'})
        return httpx.Response(200, headers={'set-cookie': 'visitor=profile-bob; Path=/'})

    real_client = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs.pop('proxy') == 'http://vpn.test:8899'
        assert kwargs['trust_env'] is False
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(research.httpx, 'AsyncClient', client)
    monkeypatch.setattr(config, 'OUTBOUND_PROXY_URL', 'http://vpn.test:8899')
    monkeypatch.setattr(research, '_WEB_FETCH_CLIENT', None)
    monkeypatch.setattr(research, '_WEB_FETCH_CLIENT_PROXY', None)

    async def check():
        try:
            await research.web_get(None, 'https://source.test/first', follow_redirects=True)
            await research.web_get(None, 'https://source.test/later-search')
            async with research.web_stream(None, 'GET', 'https://source.test/image') as response:
                await response.aread()
            assert requests == [('/first', None), ('/second', None),
                                ('/later-search', None), ('/image', None)]
            assert not list(research._WEB_FETCH_CLIENT.cookies.jar)
        finally:
            await research.close_web_fetch_client()

    asyncio.run(check())


def test_access_logs_keep_route_and_status_without_query_or_session():
    logger = logging.getLogger('uvicorn.access')
    old_filters = logger.filters[:]
    try:
        install_private_access_logging()
        install_private_access_logging()
        record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0,
            '%s - "%s %s HTTP/%s" %d',
            ('127.0.0.1:1', 'GET', '/api/img-proxy?u=https%3A%2F%2Fsite.test%2Fprivate&session=secret', '1.1', 200), None)
        assert logger.filter(record)
        assert record.getMessage() == '127.0.0.1:1 - "GET /api/img-proxy HTTP/1.1" 200'
        assert len(logger.filters) == len(old_filters) + 1
    finally:
        logger.filters[:] = old_filters
