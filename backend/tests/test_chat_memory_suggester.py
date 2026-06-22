"""Memory suggester model selection — cloud chat models must not be POSTed to Ollama."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from .optional_deps import HAS_AIOSQLITE, HAS_CHROMADB, install_aiosqlite_stub, install_rag_stub

_BACKEND = Path(__file__).resolve().parent.parent
_AGENTS = _BACKEND / "agents"
for _p in (_BACKEND, _AGENTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

if not HAS_AIOSQLITE:
    install_aiosqlite_stub()
if not HAS_CHROMADB:
    install_rag_stub()

from agents import chat as chat_mod


def _run(coro):
    return asyncio.run(coro)


class _CaptureHTTP:
    def __init__(self):
        self.models = []

    async def post(self, url, json=None, timeout=None):
        self.models.append((json or {}).get("model"))

        class _R:
            status_code = 200

            def json(self_inner):
                return {"response": "[]"}

        return _R()


class _NullEvents:
    async def emit(self, *a, **k):
        pass


def test_workspace_suggester_rejects_cloud_chat_model(monkeypatch):
    http = _CaptureHTTP()
    monkeypatch.setattr(chat_mod.config, "WORKSPACE_MODEL", "", raising=False)
    monkeypatch.setattr(chat_mod.config, "DEFAULT_MODEL", "qwen3.5:4b", raising=False)
    monkeypatch.setattr(chat_mod.config, "OLLAMA_URL", "http://ollama", raising=False)
    monkeypatch.setattr(chat_mod.db, "create_workspace_memory_suggestions",
                        lambda *a, **k: _noop(), raising=False)

    req = SimpleNamespace(model="anthropic:claude-x")
    ws = {"id": "ws1", "memory_enabled": 1, "name": "W"}
    _run(chat_mod._suggest_workspace_memories(
        req, http, _NullEvents(), "conv", ws,
        "a fairly long user message that exceeds the 120 char floor so the suggester actually runs the model call",
        "and a long assistant answer too, padding this out well beyond the threshold for sure",
        None,
    ))

    assert http.models, "suggester should have called the model"
    assert http.models[0] == "qwen3.5:4b"
    assert "anthropic:" not in (http.models[0] or "")


async def _noop(*a, **k):
    return None
