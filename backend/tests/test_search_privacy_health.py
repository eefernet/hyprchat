"""Search privacy and health-probe regression tests; no external requests."""
import asyncio
import importlib.util
from pathlib import Path
import socket
import sys
from unittest.mock import AsyncMock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
import config
import quick_search
import research
from .optional_deps import load_route_module

spec = importlib.util.spec_from_file_location("vpn_proxy", ROOT / "scripts/searxng/searxng-web-proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    research._DNS_CACHE.clear()
    quick_search._DNS_CACHE.clear()
    monkeypatch.setattr(config, "OUTBOUND_PROXY_URL", "http://vpn:8899")
    monkeypatch.setattr(config, "TRUSTED_WEB_PROXY_URL", "")
    monkeypatch.setattr(config, "SEARXNG_URL", "http://search:8888")


@pytest.fixture
def health(monkeypatch):
    pytest.importorskip("fastapi")
    return load_route_module(monkeypatch, "health")


def test_health_concurrent_requests_share_probe_and_preserve_diagnosis(monkeypatch, health):
    calls = []
    async def get(url, **kwargs):
        calls.append(url)
        if url.endswith("healthz"):
            return httpx.Response(200)
        await asyncio.sleep(.01)
        return httpx.Response(200, json={"results": [{"url": "https://example.org"}],
            "unresponsive_engines": [["duckduckgo", "access denied"]]})
    client = AsyncMock()
    client.get.side_effect = get
    monkeypatch.setattr(health, "_http", lambda: client)
    async def check():
        results = await asyncio.gather(*(health._check_searxng() for _ in range(8)))
        for r in results:
            assert r["status"] == "degraded"
            assert r["search_status"] == "partial"
            assert not r["rate_limited"]
            assert "duckduckgo: access denied" in r["search_summary"]
            assert r["search_checked_at"]
        results[0]["engine_errors"].clear()
        cached = await health._check_searxng()
        assert cached["search_sample_cached"]
        assert cached["engine_errors"]
        assert sum(u.endswith("/search") for u in calls) == 1
        assert sum(u.endswith("/healthz") for u in calls) == 9
    asyncio.run(check())


def test_health_expiry_endpoint_change_and_listener_outage(monkeypatch, health):
    now = [100.0]
    monkeypatch.setattr(health.time, "monotonic", lambda: now[0])
    client = AsyncMock()
    listener_ok = [True]
    async def get(url, **kwargs):
        if url.endswith("healthz"):
            return httpx.Response(200 if listener_ok[0] else 503)
        return httpx.Response(200, json={"results": [{"url": "https://example.org"}]})
    client.get.side_effect = get
    monkeypatch.setattr(health, "_http", lambda: client)
    async def check():
        await health._check_searxng()
        now[0] += 899
        assert (await health._check_searxng())["search_sample_cached"]
        now[0] += 1
        assert not (await health._check_searxng())["search_sample_cached"]
        monkeypatch.setattr(config, "SEARXNG_URL", "http://new-search:8888")
        assert not (await health._check_searxng())["search_sample_cached"]
        listener_ok[0] = False
        assert (await health._check_searxng())["status"] == "error"
        assert sum(c.args[0].endswith("/search") for c in client.get.call_args_list) == 3
    asyncio.run(check())


def test_cancelled_health_caller_does_not_cancel_shared_probe(monkeypatch, health):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        async def get(url, **kwargs):
            if url.endswith("healthz"):
                return httpx.Response(200)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"results": [], "unresponsive_engines": [["brave", "too many requests"]]})
        client = AsyncMock()
        client.get.side_effect = get
        monkeypatch.setattr(health, "_http", lambda: client)
        first = asyncio.create_task(health._check_searxng())
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(health._check_searxng())
        release.set()
        r = await second
        assert r["rate_limited"]
        assert r["search_status"] == "rate_limited"
        assert sum(c.args[0].endswith("/search") for c in client.get.call_args_list) == 1
    asyncio.run(check())


@pytest.mark.parametrize("url", ["http://localhost/a", "http://127.0.0.1/", "http://10.1.1.1/", "http://[::1]/", "file:///etc/passwd", "http://test.local/", "http://user:pass@example.com/"])
def test_trusted_proxy_still_rejects_unsafe_urls(monkeypatch, url):
    monkeypatch.setattr(config, "TRUSTED_WEB_PROXY_URL", config.OUTBOUND_PROXY_URL)
    async def check():
        assert not await research._url_safe_for_fetch(url, resolve_dns=True)
        assert not await quick_search._url_safe(url)
    asyncio.run(check())


def test_only_explicit_matching_trusted_proxy_skips_local_dns(monkeypatch):
    lookups = []
    def resolve(*args, **kwargs):
        lookups.append(args[0])
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    async def check():
        monkeypatch.setattr(config, "TRUSTED_WEB_PROXY_URL", config.OUTBOUND_PROXY_URL)
        assert await research._url_safe_for_fetch("https://example.com", resolve_dns=True)
        assert await quick_search._url_safe("https://example.com")
        assert not lookups
        monkeypatch.setattr(config, "OUTBOUND_PROXY_URL", "http://untrusted:8899")
        assert await research._url_safe_for_fetch("https://example.com", resolve_dns=True)
        assert await quick_search._url_safe("https://example.com")
        assert len(lookups) == 2
        monkeypatch.setattr(config, "OUTBOUND_PROXY_URL", "")
        assert not research._proxy_validates_destinations()
    asyncio.run(check())


@pytest.mark.parametrize("bad", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "fd00::1", "224.0.0.1"])
def test_proxy_rejects_mixed_dns_answers(monkeypatch, bad):
    async def check():
        loop = asyncio.get_running_loop()
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0)),
                 (socket.AF_INET6 if ":" in bad else socket.AF_INET, socket.SOCK_STREAM, 6, "", (bad, 0))]
        monkeypatch.setattr(loop, "getaddrinfo", AsyncMock(return_value=infos))
        with pytest.raises(ValueError, match="blocked DNS"):
            await proxy.resolve_public_ipv4("example.com")
    asyncio.run(check())


def test_proxy_resolves_once_and_connects_to_validated_ip(monkeypatch):
    async def check():
        loop = asyncio.get_running_loop()
        resolve = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0))])
        connect = AsyncMock(return_value=("reader", "writer"))
        monkeypatch.setattr(loop, "getaddrinfo", resolve)
        monkeypatch.setattr(proxy.asyncio, "open_connection", connect)
        assert await proxy.connect_public("example.com", 443) == ("reader", "writer")
        assert resolve.await_count == 1
        connect.assert_awaited_once_with("93.184.215.14", 443)
    asyncio.run(check())


def test_proxy_blocks_private_redirect_target_before_connect(monkeypatch):
    async def check():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]))
        connect = AsyncMock()
        monkeypatch.setattr(proxy.asyncio, "open_connection", connect)
        with pytest.raises(ValueError):
            await proxy.connect_public("redirect.example.com", 443)
        connect.assert_not_awaited()
    asyncio.run(check())


def test_proxy_timeout_includes_dns(monkeypatch):
    async def resolve(host):
        await asyncio.sleep(10)
    monkeypatch.setattr(proxy, "resolve_public_ipv4", resolve)
    monkeypatch.setattr(proxy, "CONNECT_TIMEOUT", .01)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(proxy.connect_public("example.com", 443))
