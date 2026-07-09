"""Offline unit tests for reranker.py — LLM rerank of hybrid RAG candidates.

No live server, no Ollama: complete_chat is monkeypatched.
Run: python -m pytest tests/test_reranker_unit.py -v
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import reranker  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _chunks(n):
    return [
        {"text": f"chunk number {i}", "filename": f"f{i}.md", "kb_id": "kb1",
         "chunk_index": i, "score": 0.9 - i * 0.05}
        for i in range(n)
    ]


# ── Score parsing ────────────────────────────────────────────────────────────

def test_parse_scores_object():
    assert reranker._parse_scores('{"scores": [1, 9.5, 0]}', 3) == [1.0, 9.5, 0.0]


def test_parse_scores_bare_array():
    assert reranker._parse_scores("[3, 4, 5]", 3) == [3.0, 4.0, 5.0]


def test_parse_scores_embedded_array():
    assert reranker._parse_scores('Here you go: [7, 2]', 2) == [7.0, 2.0]


def test_parse_scores_clamped():
    assert reranker._parse_scores('{"scores": [-5, 99]}', 2) == [0.0, 10.0]


def test_parse_scores_rejects_bad():
    assert reranker._parse_scores("", 2) is None
    assert reranker._parse_scores("no numbers here", 2) is None
    assert reranker._parse_scores('{"scores": [1]}', 2) is None       # wrong length
    assert reranker._parse_scores('{"scores": [1, "x"]}', 2) is None  # non-numeric


# ── rerank() behavior ────────────────────────────────────────────────────────

def test_disabled_passthrough(monkeypatch):
    monkeypatch.setattr(config, "RAG_RERANKER", "none")
    chunks = _chunks(6)
    out = _run(reranker.rerank("query", chunks, 3))
    assert out == chunks[:3]


def test_rerank_reorders(monkeypatch):
    monkeypatch.setattr(config, "RAG_RERANKER", "llm")
    monkeypatch.setattr(config, "WORKSPACE_MODEL", "small:1b")

    async def fake_complete_chat(http, model, prompt, **kw):
        assert "chunk number 0" in prompt
        return '{"scores": [1, 2, 10, 3]}'  # third candidate is best

    monkeypatch.setattr(reranker.model_providers, "complete_chat", fake_complete_chat)
    chunks = _chunks(4)
    out = _run(reranker.rerank("query", chunks, 2))
    assert [c["chunk_index"] for c in out] == [2, 3]
    assert out[0]["rerank_score"] == 10.0
    # Input chunks are not mutated
    assert "rerank_score" not in chunks[2]


def test_rerank_ties_keep_fused_order(monkeypatch):
    monkeypatch.setattr(config, "RAG_RERANKER", "llm")
    monkeypatch.setattr(config, "WORKSPACE_MODEL", "small:1b")

    async def fake_complete_chat(http, model, prompt, **kw):
        return '{"scores": [5, 5, 5]}'

    monkeypatch.setattr(reranker.model_providers, "complete_chat", fake_complete_chat)
    out = _run(reranker.rerank("query", _chunks(3), 3))
    assert [c["chunk_index"] for c in out] == [0, 1, 2]


def test_rerank_fails_open_on_error(monkeypatch):
    monkeypatch.setattr(config, "RAG_RERANKER", "llm")
    monkeypatch.setattr(config, "WORKSPACE_MODEL", "small:1b")

    async def boom(http, model, prompt, **kw):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(reranker.model_providers, "complete_chat", boom)
    chunks = _chunks(5)
    out = _run(reranker.rerank("query", chunks, 3))
    assert out == chunks[:3]


def test_rerank_fails_open_on_garbage(monkeypatch):
    monkeypatch.setattr(config, "RAG_RERANKER", "llm")
    monkeypatch.setattr(config, "WORKSPACE_MODEL", "small:1b")

    async def garbage(http, model, prompt, **kw):
        return "I think these are all great excerpts!"

    monkeypatch.setattr(reranker.model_providers, "complete_chat", garbage)
    chunks = _chunks(4)
    out = _run(reranker.rerank("query", chunks, 2))
    assert out == chunks[:2]


def test_rerank_cloud_model_rejected(monkeypatch):
    # A cloud-prefixed workspace model must never be used for scoring; with no
    # local fallback the reranker passes through.
    monkeypatch.setattr(config, "RAG_RERANKER", "llm")
    monkeypatch.setattr(config, "WORKSPACE_MODEL", "openai:gpt-5")
    monkeypatch.setattr(config, "DEFAULT_MODEL", "anthropic:claude-opus-4-5")

    called = []

    async def fake_complete_chat(http, model, prompt, **kw):
        called.append(model)
        return "[1, 2]"

    monkeypatch.setattr(reranker.model_providers, "complete_chat", fake_complete_chat)
    chunks = _chunks(4)
    out = _run(reranker.rerank("query", chunks, 2))
    assert out == chunks[:2]
    assert called == []


def test_single_chunk_short_circuits(monkeypatch):
    monkeypatch.setattr(config, "RAG_RERANKER", "llm")
    chunks = _chunks(1)
    out = _run(reranker.rerank("query", chunks, 3))
    assert out == chunks
