"""Offline unit tests for context auto-compaction (_maybe_compact_context).

Covers the threshold skip, the rebuilt-window user-boundary rule, the
summary_until_msg_id fold bookkeeping, and fail-open behavior when the
summarizer returns nothing. DB and LLM calls are monkeypatched.
"""
import asyncio
import sys
from pathlib import Path

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


def _run(coro):
    return asyncio.run(coro)


def _dialogue(n, chars=400):
    """n alternating user/assistant messages starting with user."""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": f"{role}-{i} " + "z" * chars})
    return out


def _wire(monkeypatch, *, rows=None, old_summary="", until_id=0, llm_reply="NEW SUMMARY TEXT"):
    monkeypatch.setattr(chat_mod.config, "CONTEXT_COMPACTION", "on", raising=False)
    monkeypatch.setattr(chat_mod.config, "WORKSPACE_MODEL", "small:3b", raising=False)
    monkeypatch.setattr(chat_mod.config, "DEFAULT_MODEL", "local-default:8b", raising=False)
    monkeypatch.setattr(chat_mod.config, "OLLAMA_URL", "http://ollama.invalid", raising=False)
    state = {"saved": None, "llm_calls": 0}

    async def fake_summary(conv_id):
        return old_summary, until_id

    async def fake_conv(conv_id):
        return {"messages": rows or []}

    async def fake_set_summary(conv_id, summary, until):
        state["saved"] = (conv_id, summary, until)

    async def fake_complete_chat(http, model, prompt, **kw):
        state["llm_calls"] += 1
        return llm_reply

    monkeypatch.setattr(chat_mod.db, "get_conversation_summary", fake_summary)
    monkeypatch.setattr(chat_mod.db, "get_conversation", fake_conv)
    monkeypatch.setattr(chat_mod.db, "set_conversation_summary", fake_set_summary)
    monkeypatch.setattr(chat_mod.model_providers, "complete_chat", fake_complete_chat)
    return state


def test_below_threshold_returns_unchanged(monkeypatch):
    state = _wire(monkeypatch)
    msgs = [{"role": "user", "content": "hi"}]
    out = _run(chat_mod._maybe_compact_context(None, None, msgs, {"num_ctx": 8192}, "c1", False))
    assert out is msgs
    assert state["llm_calls"] == 0 and state["saved"] is None


def test_disabled_ephemeral_and_no_conv_skip(monkeypatch):
    state = _wire(monkeypatch)
    big = [{"role": "user", "content": "z" * 50_000}]
    monkeypatch.setattr(chat_mod.config, "CONTEXT_COMPACTION", "off", raising=False)
    assert _run(chat_mod._maybe_compact_context(None, None, big, {"num_ctx": 1024}, "c1", False)) is big
    monkeypatch.setattr(chat_mod.config, "CONTEXT_COMPACTION", "on", raising=False)
    assert _run(chat_mod._maybe_compact_context(None, None, big, {"num_ctx": 1024}, "c1", True)) is big
    assert _run(chat_mod._maybe_compact_context(None, None, big, {"num_ctx": 1024}, "", False)) is big
    # num_ctx unknown (0/auto) also fails open
    assert _run(chat_mod._maybe_compact_context(None, None, big, {"num_ctx": 0}, "c1", False)) is big
    assert state["llm_calls"] == 0


def test_rebuild_windows_on_user_boundary_and_saves_fold_point(monkeypatch):
    dialogue = _dialogue(9)  # u a u a u a u a u
    rows = [{"id": i + 1, **m} for i, m in enumerate(dialogue)]
    state = _wire(monkeypatch, rows=rows)
    msgs = [{"role": "system", "content": "persona prompt"}] + dialogue
    # est tokens ≈ 9*400/4 = 900 > 0.75*1024 = 768 → compacts
    out = _run(chat_mod._maybe_compact_context(None, None, msgs, {"num_ctx": 1024}, "c1", False))
    assert out is not msgs
    assert out[0] is msgs[0]  # leading system block preserved first
    assert out[1]["role"] == "system"
    assert "EARLIER CONVERSATION" in out[1]["content"]
    assert "NEW SUMMARY TEXT" in out[1]["content"]
    # kept window must open on a user turn, never mid-pair on an assistant
    assert out[2]["role"] == "user"
    for m in out[2:]:
        assert m["role"] in ("user", "assistant")
    # only rows[:-8] (the first row, id 1) was foldable → fold point 1
    assert state["saved"] == ("c1", "NEW SUMMARY TEXT", 1)


def test_no_new_foldable_rows_reuses_summary_without_llm(monkeypatch):
    dialogue = _dialogue(9)
    rows = [{"id": i + 1, **m} for i, m in enumerate(dialogue)]
    # until_id=9: everything already folded — the persisted summary is reused
    state = _wire(monkeypatch, rows=rows, old_summary="prior facts", until_id=9)
    msgs = [{"role": "system", "content": "persona"}] + dialogue
    out = _run(chat_mod._maybe_compact_context(None, None, msgs, {"num_ctx": 1024}, "c1", False))
    assert state["llm_calls"] == 0  # prompt-cache stability: no re-summarize
    assert state["saved"] is None
    assert "prior facts" in out[1]["content"]


def test_fail_open_on_empty_summarizer_output(monkeypatch):
    dialogue = _dialogue(9)
    rows = [{"id": i + 1, **m} for i, m in enumerate(dialogue)]
    state = _wire(monkeypatch, rows=rows, llm_reply="")
    msgs = [{"role": "system", "content": "persona"}] + dialogue
    out = _run(chat_mod._maybe_compact_context(None, None, msgs, {"num_ctx": 1024}, "c1", False))
    assert out is msgs  # unchanged object — fail-open
    assert state["saved"] is None  # never persists an empty summary


def test_transcript_cap_does_not_advance_fold_point_past_unsummarized(monkeypatch):
    """Rows dropped by _COMPACT_TRANSCRIPT_CAP must NOT advance
    summary_until_msg_id — they'd be permanently lost from both the summary
    and the context otherwise. The marker stops at the last row folded."""
    cap = chat_mod._COMPACT_TRANSCRIPT_CAP
    # 12 old rows, each ~cap/3 chars: only the first ~3 fit in one transcript.
    big = _dialogue(12, chars=cap // 3)
    recent = _dialogue(9)
    rows = [{"id": i + 1, **m} for i, m in enumerate(big + recent)]
    state = _wire(monkeypatch, rows=rows)
    msgs = [{"role": "system", "content": "persona"}] + big + recent
    out = _run(chat_mod._maybe_compact_context(None, None, msgs, {"num_ctx": 1024}, "c1", False))
    assert out is not msgs
    assert state["saved"] is not None
    _, _, until = state["saved"]
    # Not all 12 foldable rows fit under the cap, so the marker must stay
    # strictly below the last foldable row id (12) — the tail folds next pass.
    assert until < 12
    assert until >= 1


def test_fail_open_on_db_error(monkeypatch):
    state = _wire(monkeypatch)

    async def boom(conv_id):
        raise RuntimeError("db locked")

    monkeypatch.setattr(chat_mod.db, "get_conversation_summary", boom)
    msgs = [{"role": "user", "content": "z" * 10_000}]
    out = _run(chat_mod._maybe_compact_context(None, None, msgs, {"num_ctx": 1024}, "c1", False))
    assert out is msgs
