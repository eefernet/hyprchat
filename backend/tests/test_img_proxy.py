import asyncio
import importlib
import sys
import time
from pathlib import Path

import pytest

from .optional_deps import (
    HAS_AIOSQLITE,
    HAS_CHROMADB,
    HAS_FASTAPI,
    install_aiosqlite_stub,
    install_rag_stub,
)

if not HAS_FASTAPI:
    pytest.skip("fastapi not installed", allow_module_level=True)

from fastapi import HTTPException  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _run(coro):
    return asyncio.run(coro)


def _import_main(monkeypatch):
    if not HAS_AIOSQLITE:
        install_aiosqlite_stub()
    if not HAS_CHROMADB:
        install_rag_stub(monkeypatch)
    sys.modules.pop("main", None)
    return importlib.import_module("main")


class _StreamResponse:
    def __init__(self, *, url, status_code=200, headers=None, content=b"ok"):
        self.url = url
        self.status_code = status_code
        self.headers = headers or {"content-type": "image/png"}
        self._content = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_bytes(self):
        yield self._content


class _StreamHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def stream(self, method, url, **kwargs):
        self.urls.append(url)
        if not self.responses:
            raise AssertionError(f"unexpected fetch: {url}")
        return self.responses.pop(0)


def test_img_proxy_blocks_private_url_before_fetch(monkeypatch):
    main = _import_main(monkeypatch)
    http = _StreamHTTP([])
    monkeypatch.setattr(main, "http", http)

    with pytest.raises(HTTPException) as exc:
        _run(main.img_proxy("http://127.0.0.1/private.png"))

    assert exc.value.status_code == 400
    assert http.urls == []


def test_img_proxy_rejects_redirect_to_private_target(monkeypatch):
    import research

    main = _import_main(monkeypatch)
    research._DNS_CACHE["example.com"] = (time.time(), True)
    http = _StreamHTTP([
        _StreamResponse(
            url="https://example.com/start.png",
            status_code=302,
            headers={"location": "http://127.0.0.1/private.png"},
            content=b"",
        )
    ])
    monkeypatch.setattr(main, "http", http)

    with pytest.raises(HTTPException) as exc:
        _run(main.img_proxy("https://example.com/start.png"))

    assert exc.value.status_code == 400
    assert http.urls == ["https://example.com/start.png"]
