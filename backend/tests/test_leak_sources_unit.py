"""Offline unit tests for leak_sources.py — adapter parsing, fail-open behavior,
and the dispatcher (dedupe / depth scaling / deadline). No network: the thin
research wrappers (web_get / searxng_search / wikileaks_search / clean_html) are
monkeypatched so the call-time `from research import ...` never runs.

Run: python -m pytest tests/test_leak_sources_unit.py -v
"""

import asyncio
import sys
from pathlib import Path

from .optional_deps import HAS_AIOSQLITE, install_aiosqlite_stub

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

if not HAS_AIOSQLITE:
    install_aiosqlite_stub()

import leak_sources  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class FakeResp:
    def __init__(self, status_code=200, json_data=None, raise_json=False):
        self.status_code = status_code
        self._json = json_data
        self._raise_json = raise_json
        self.text = ""

    def json(self):
        if self._raise_json:
            raise ValueError("bad json")
        return self._json


def _set_web_get(monkeypatch, resp):
    async def _fake(http, url, **kw):
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(leak_sources, "web_get", _fake)


# ── clean_html is identity in tests (no research import needed) ──
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _identity_clean(monkeypatch):
    monkeypatch.setattr(leak_sources, "clean_html", lambda t: (t or "").strip())


# ── DOJ ──

def test_doj_parse(monkeypatch):
    _set_web_get(monkeypatch, FakeResp(200, {"results": [
        {"title": "DOJ charges X", "url": "https://www.justice.gov/opa/pr/x", "teaser": "A teaser."},
        {"title": "No URL", "url": "", "teaser": "skipped"},
    ]}))
    out = _run(leak_sources.search_doj(object(), "x", 6))
    assert len(out) == 1
    r = out[0]
    assert r["url"] == "https://www.justice.gov/opa/pr/x"
    assert r["kind"] == "gov" and r["source"] == "DOJ"
    assert r["title"] == "DOJ charges X"
    assert r["content"] == "A teaser."


def test_doj_fail_open_non200(monkeypatch):
    _set_web_get(monkeypatch, FakeResp(500, None))
    assert _run(leak_sources.search_doj(object(), "x", 6)) == []


def test_doj_fail_open_raises(monkeypatch):
    _set_web_get(monkeypatch, RuntimeError("boom"))
    assert _run(leak_sources.search_doj(object(), "x", 6)) == []


def test_doj_fail_open_bad_json(monkeypatch):
    _set_web_get(monkeypatch, FakeResp(200, None, raise_json=True))
    assert _run(leak_sources.search_doj(object(), "x", 6)) == []


# ── CourtListener ──

def test_courtlistener_parse(monkeypatch):
    _set_web_get(monkeypatch, FakeResp(200, {"results": [
        {"caseName": "USA v. Doe", "docket_absolute_url": "/docket/123/usa-v-doe/",
         "court": "S.D.N.Y.", "dateFiled": "2020-01-01", "docketNumber": "20-cr-1"},
        {"caseName": "no path"},
    ]}))
    out = _run(leak_sources.search_courtlistener(object(), "doe", 6))
    assert len(out) == 1
    r = out[0]
    assert r["url"] == "https://www.courtlistener.com/docket/123/usa-v-doe/"
    assert r["kind"] == "court" and r["source"] == "CourtListener"
    assert "S.D.N.Y." in r["content"] and "Docket 20-cr-1" in r["content"]


def test_courtlistener_token_header(monkeypatch):
    captured = {}
    async def _fake(http, url, **kw):
        captured["headers"] = kw.get("headers", {})
        return FakeResp(200, {"results": []})
    monkeypatch.setattr(leak_sources, "web_get", _fake)
    monkeypatch.setattr(leak_sources.config, "COURTLISTENER_TOKEN", "secret123")
    _run(leak_sources.search_courtlistener(object(), "x", 4))
    assert captured["headers"].get("Authorization") == "Token secret123"


def test_courtlistener_no_token_no_header(monkeypatch):
    captured = {}
    async def _fake(http, url, **kw):
        captured["headers"] = kw.get("headers", {})
        return FakeResp(200, {"results": []})
    monkeypatch.setattr(leak_sources, "web_get", _fake)
    monkeypatch.setattr(leak_sources.config, "COURTLISTENER_TOKEN", "")
    _run(leak_sources.search_courtlistener(object(), "x", 4))
    assert "Authorization" not in captured["headers"]


# ── Archive.org ──

def test_archive_org_parse(monkeypatch):
    _set_web_get(monkeypatch, FakeResp(200, {"response": {"docs": [
        {"identifier": "cia-mkultra", "title": "MKULTRA", "description": ["line1", "line2"]},
        {"title": "no identifier"},
    ]}}))
    out = _run(leak_sources.search_archive_org(object(), "mkultra", 6))
    assert len(out) == 1
    r = out[0]
    assert r["url"] == "https://archive.org/details/cia-mkultra"
    assert r["kind"] == "archive" and r["source"] == "Archive.org"
    assert "line1 line2" in r["content"]


def test_archive_org_fail_open(monkeypatch):
    _set_web_get(monkeypatch, FakeResp(200, None, raise_json=True))
    assert _run(leak_sources.search_archive_org(object(), "x", 6)) == []


# ── WikiLeaks adapter (reuses research._search_wikileaks via wrapper) ──

def test_wikileaks_adapter(monkeypatch):
    async def _fake_wl(http, searxng_url, query, count, **kw):
        return [
            {"title": "🔓 Cable 1", "url": "https://wikileaks.org/plusd/cables/1", "content": "body", "score": 5},
            {"title": "no url", "url": "", "content": ""},
        ]
    monkeypatch.setattr(leak_sources, "wikileaks_search", _fake_wl)
    out = _run(leak_sources.search_wikileaks(object(), "topic", 6, searxng_url="http://sx"))
    assert len(out) == 1
    assert out[0]["kind"] == "leak" and out[0]["source"] == "WikiLeaks"
    assert out[0]["url"] == "https://wikileaks.org/plusd/cables/1"


def test_wikileaks_fail_open(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("wl down")
    monkeypatch.setattr(leak_sources, "wikileaks_search", _boom)
    assert _run(leak_sources.search_wikileaks(object(), "t", 6, searxng_url="http://sx")) == []


# ── SearXNG-scoped adapter ──

def test_searxng_site(monkeypatch):
    async def _fake_sx(http, searxng_url, query, count, **kw):
        assert "site:vault.fbi.gov" in query
        return [{"title": "Vault doc", "url": "https://vault.fbi.gov/doc", "content": "snip"}]
    monkeypatch.setattr(leak_sources, "searxng_search", _fake_sx)
    out = _run(leak_sources._searxng_site(
        object(), "roswell", 6, searxng_url="http://sx",
        site="vault.fbi.gov", source="FBI Vault", kind="gov"))
    assert len(out) == 1
    assert out[0]["kind"] == "gov" and out[0]["source"] == "FBI Vault"


def test_searxng_site_no_url_returns_empty(monkeypatch):
    # No searxng_url configured → scoped sources contribute nothing (no call).
    called = {"n": 0}
    async def _fake_sx(*a, **k):
        called["n"] += 1
        return []
    monkeypatch.setattr(leak_sources, "searxng_search", _fake_sx)
    out = _run(leak_sources._searxng_site(
        object(), "x", 6, searxng_url="", site="cryptome.org", source="Cryptome", kind="leak"))
    assert out == [] and called["n"] == 0


# ── depth scaling ──

def test_limit_for_depth():
    assert leak_sources._limit_for_depth(3) == 4
    assert leak_sources._limit_for_depth(4) == 6
    assert leak_sources._limit_for_depth(5) == 8
    assert leak_sources._limit_for_depth(99) == 5  # unknown → default
    assert leak_sources._limit_for_depth("bad") == 5


# ── dispatcher: dedupe, fail-open, deadline ──

def _patch_adapters(monkeypatch, doj=None, court=None, arch=None, wl=None, scoped=None):
    async def _mk(items):
        return items or []
    async def _doj(http, q, limit): return doj or []
    async def _court(http, q, limit):
        if isinstance(court, Exception):
            raise court
        return court or []
    async def _arch(http, q, limit): return arch or []
    async def _wl(http, q, limit, **kw): return wl or []
    async def _scoped(http, q, limit, **kw): return scoped or []
    monkeypatch.setattr(leak_sources, "search_doj", _doj)
    monkeypatch.setattr(leak_sources, "search_courtlistener", _court)
    monkeypatch.setattr(leak_sources, "search_archive_org", _arch)
    monkeypatch.setattr(leak_sources, "search_wikileaks", _wl)
    monkeypatch.setattr(leak_sources, "_searxng_site", _scoped)


def test_dispatcher_dedup_and_failopen(monkeypatch):
    doj = [
        leak_sources._result("A", "https://justice.gov/a", "s", source="DOJ", kind="gov"),
        leak_sources._result("A dup", "https://justice.gov/a/", "s2", source="DOJ", kind="gov"),  # trailing slash dup
    ]
    wl = [leak_sources._result("WL same as DOJ", "https://JUSTICE.gov/a", "x", source="WikiLeaks", kind="leak")]  # case dup
    arch = [leak_sources._result("uniq", "https://archive.org/details/z", "d", source="Archive.org", kind="archive")]
    _patch_adapters(monkeypatch, doj=doj, court=RuntimeError("cl down"), arch=arch, wl=wl, scoped=[])
    out = _run(leak_sources.gather_leak_sources(object(), "q", depth=4, searxng_url="http://sx"))
    urls = [r["url"] for r in out]
    # trailing-slash + case dups collapse to one; failing CourtListener contributes nothing
    assert "https://justice.gov/a" in urls
    assert "https://archive.org/details/z" in urls
    # only 2 unique URLs survive
    assert len(out) == 2


def test_dispatcher_deadline(monkeypatch):
    async def _slow(http, q, limit):
        await asyncio.sleep(5)
        return [leak_sources._result("slow", "https://justice.gov/slow", "", source="DOJ", kind="gov")]
    fast = [leak_sources._result("fast", "https://archive.org/details/fast", "", source="Archive.org", kind="archive")]
    _patch_adapters(monkeypatch, arch=fast, scoped=[])
    monkeypatch.setattr(leak_sources, "search_doj", _slow)
    out = _run(leak_sources.gather_leak_sources(object(), "q", depth=4, searxng_url="http://sx", deadline=0.3))
    urls = [r["url"] for r in out]
    assert "https://archive.org/details/fast" in urls
    assert "https://justice.gov/slow" not in urls


def test_dispatcher_depth_scales_limit(monkeypatch):
    seen = {}
    async def _capture(http, q, limit):
        seen["limit"] = limit
        return []
    _patch_adapters(monkeypatch, scoped=[])
    monkeypatch.setattr(leak_sources, "search_doj", _capture)
    _run(leak_sources.gather_leak_sources(object(), "q", depth=5, searxng_url="http://sx"))
    assert seen["limit"] == 8


# ── bounded web_get shim (routes through research.fetch_bytes_safely) ──

def test_web_get_bounded_shim(monkeypatch, capsys):
    import types
    calls = {}

    async def _fake_fbs(http, url, *, timeout=15, headers=None, max_bytes=0):
        calls.update(url=url, max_bytes=max_bytes, headers=headers)
        return 200, {}, url, b'{"ok": true}'

    mod = types.ModuleType("research")
    mod.fetch_bytes_safely = _fake_fbs
    monkeypatch.setitem(sys.modules, "research", mod)
    r = _run(leak_sources.web_get(object(), "https://example.test/api?q=1"))
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls["max_bytes"] == 1_500_000  # bounded — never an uncapped read
    assert "HTTP" not in capsys.readouterr().out  # 200 is silent


def test_web_get_logs_non_200(monkeypatch, capsys):
    import types

    async def _fake_fbs(http, url, *, timeout=15, headers=None, max_bytes=0):
        return 403, {}, url, b"denied"

    mod = types.ModuleType("research")
    mod.fetch_bytes_safely = _fake_fbs
    monkeypatch.setitem(sys.modules, "research", mod)
    r = _run(leak_sources.web_get(object(), "https://www.courtlistener.com/api/rest/v4/search/?q=x"))
    assert r.status_code == 403
    out = capsys.readouterr().out
    assert "[LEAK]" in out and "403" in out
    assert "?q=x" not in out  # query string stripped from the log line


# ── fallback_state threading ──

def test_searxng_wrapper_passes_fallback_state(monkeypatch):
    import types
    seen = {}

    async def _fake_sx(http, sx, q, count, fallback_state=None):
        seen["fs"] = fallback_state
        return []

    mod = types.ModuleType("research")
    mod._search_searxng = _fake_sx
    monkeypatch.setitem(sys.modules, "research", mod)
    fs = {"remaining": 2}
    _run(leak_sources.searxng_search(object(), "http://sx", "q", 5, fallback_state=fs))
    assert seen["fs"] is fs


def test_dispatcher_threads_fallback_state(monkeypatch):
    seen = {}

    async def _wl(http, q, limit, **kw):
        seen["wl"] = kw.get("fallback_state")
        return []

    async def _scoped(http, q, limit, **kw):
        seen["scoped"] = kw.get("fallback_state")
        return []

    _patch_adapters(monkeypatch, scoped=[])
    monkeypatch.setattr(leak_sources, "search_wikileaks", _wl)
    monkeypatch.setattr(leak_sources, "_searxng_site", _scoped)
    fs = {"remaining": 8}
    _run(leak_sources.gather_leak_sources(object(), "q", depth=4,
                                          searxng_url="http://sx", fallback_state=fs))
    assert seen["wl"] is fs and seen["scoped"] is fs


# ── straggler cleanup + concurrency cap ──

def test_dispatcher_awaits_cancelled_stragglers(monkeypatch):
    # The deadline cancels slow adapters; gather_leak_sources must AWAIT them
    # so sockets close deterministically (no "Task was destroyed" noise).
    # Proof: the straggler's finally block runs before the dispatcher returns.
    state = {"finalized": False}

    async def _slow(http, q, limit):
        try:
            await asyncio.sleep(5)
        finally:
            state["finalized"] = True
        return []

    _patch_adapters(monkeypatch, scoped=[])
    monkeypatch.setattr(leak_sources, "search_doj", _slow)
    _run(leak_sources.gather_leak_sources(object(), "q", depth=4,
                                          searxng_url="http://sx", deadline=0.2))
    assert state["finalized"] is True


def test_dispatcher_concurrency_cap(monkeypatch):
    cur = {"n": 0, "max": 0}

    async def _track(http, q, limit, **kw):
        cur["n"] += 1
        cur["max"] = max(cur["max"], cur["n"])
        await asyncio.sleep(0.03)
        cur["n"] -= 1
        return []

    for name in ("search_doj", "search_courtlistener", "search_archive_org", "search_wikileaks"):
        monkeypatch.setattr(leak_sources, name, _track)
    monkeypatch.setattr(leak_sources, "_searxng_site", _track)
    _run(leak_sources.gather_leak_sources(object(), "q", depth=4, searxng_url="http://sx"))
    assert cur["max"] <= 3  # 11 adapters, but at most 3 in flight
