"""Offline unit tests for the 'auto' pseudo-model router (_route_auto_model).

Covers the category map, the deterministic long-context path (no classifier
call), classifier timeout/error fallback, and the never-returns-'auto'
invariant. The classifier LLM is monkeypatched — no Ollama needed.
"""
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

from agents import chat as chat_mod  # noqa: E402


ROUTING = {
    "chat": "chat-model:7b",
    "code": "code-model:14b",
    "reasoning": "think-model:32b",
    "long_context": "big-ctx:8b",
}


def _run(coro):
    return asyncio.run(coro)


def _req(*contents, role="user"):
    return SimpleNamespace(messages=[{"role": role, "content": c} for c in contents])


def _setup(monkeypatch, routing, classifier_word="chat", classifier_exc=None):
    monkeypatch.setattr(chat_mod.config, "MODEL_ROUTING", routing, raising=False)
    monkeypatch.setattr(chat_mod.config, "DEFAULT_MODEL", "local-default:8b", raising=False)
    monkeypatch.setattr(chat_mod.config, "WORKSPACE_MODEL", "small:3b", raising=False)
    monkeypatch.setattr(chat_mod.config, "OLLAMA_URL", "http://ollama.invalid", raising=False)
    calls = []

    async def fake_complete_chat(http, model, prompt, **kw):
        calls.append(model)
        if classifier_exc is not None:
            raise classifier_exc
        return classifier_word

    monkeypatch.setattr(chat_mod.model_providers, "complete_chat", fake_complete_chat)
    return calls


def test_category_map(monkeypatch):
    for word, expect in [("code", ("code-model:14b", "code")),
                         ("reasoning", ("think-model:32b", "reasoning")),
                         ("chat", ("chat-model:7b", "chat"))]:
        _setup(monkeypatch, dict(ROUTING), classifier_word=word)
        got = _run(chat_mod._route_auto_model(_req("do the thing"), None))
        assert got == expect, f"word={word}"


def test_long_context_is_deterministic_no_classifier_call(monkeypatch):
    calls = _setup(monkeypatch, dict(ROUTING), classifier_word="code")
    big = "x" * 60_000  # ≈15K est tokens > 12K threshold
    model, cat = _run(chat_mod._route_auto_model(_req(big), None))
    assert (model, cat) == ("big-ctx:8b", "long_context")
    assert calls == []  # long-context routing must not burn an LLM call


def test_long_context_unconfigured_falls_through(monkeypatch):
    routing = dict(ROUTING, long_context="")
    _setup(monkeypatch, routing, classifier_word="code")
    model, cat = _run(chat_mod._route_auto_model(_req("x" * 60_000), None))
    assert (model, cat) == ("code-model:14b", "code")


def test_classifier_timeout_falls_back_to_chat(monkeypatch):
    _setup(monkeypatch, dict(ROUTING), classifier_exc=asyncio.TimeoutError())
    model, cat = _run(chat_mod._route_auto_model(_req("hard question"), None))
    assert (model, cat) == ("chat-model:7b", "chat_fallback")


def test_classifier_error_falls_back_to_chat(monkeypatch):
    _setup(monkeypatch, dict(ROUTING), classifier_exc=RuntimeError("ollama down"))
    model, cat = _run(chat_mod._route_auto_model(_req("hard question"), None))
    assert (model, cat) == ("chat-model:7b", "chat_fallback")


def test_garbage_classifier_output_falls_back(monkeypatch):
    _setup(monkeypatch, dict(ROUTING), classifier_word="bananas")
    model, cat = _run(chat_mod._route_auto_model(_req("hello"), None))
    assert (model, cat) == ("chat-model:7b", "chat_fallback")


def test_empty_user_message_skips_classifier(monkeypatch):
    calls = _setup(monkeypatch, dict(ROUTING), classifier_word="code")
    model, cat = _run(chat_mod._route_auto_model(_req("   "), None))
    assert cat == "chat_fallback"
    assert calls == []


def test_never_returns_auto_even_when_config_says_auto(monkeypatch):
    routing = {"chat": "auto", "code": "auto", "reasoning": "auto", "long_context": ""}
    _setup(monkeypatch, routing, classifier_word="code")
    model, cat = _run(chat_mod._route_auto_model(_req("write a script"), None))
    assert model == "local-default:8b"
    assert model != "auto"


def test_cloud_workspace_model_never_classifies(monkeypatch):
    calls = _setup(monkeypatch, dict(ROUTING), classifier_word="chat")
    monkeypatch.setattr(chat_mod.config, "WORKSPACE_MODEL", "openai:gpt-4o", raising=False)
    _run(chat_mod._route_auto_model(_req("hello there friend"), None))
    # reject_cloud strips the cloud id; the local default classifies instead
    assert calls == ["local-default:8b"]
