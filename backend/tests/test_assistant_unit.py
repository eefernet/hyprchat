"""Offline unit tests for the Personal Assistant core (agents/assistant.py +
the scheduler's check-in delivery seams).

Covers the July 2026 bug-fix batch:
- headless tool resolution falls through task → conversation → persona
  (an empty list means "unset" — check-ins are created without tool_ids)
- headless model resolution honors the persona base_model (explicit user
  choice, cloud allowed) and cloud-strips inherited fallbacks
- SSE error chunks without a done → the run RAISES (no more "succeeded"
  task_runs for failed briefs); the final assistant text is returned and a
  stale prior brief is never handed back
- build_checkin_prompt stamps the PROFILE timezone, not UTC
- the notifications gatherer skips kind="checkin" rows and survives body=None
- ensure_assistant repoints check-ins after re-seeding a deleted pinned
  conversation

No live server, DB, or model: database helpers, agents.chat, and
model_providers are all faked. Run:
    python -m pytest tests/test_assistant_unit.py -v
"""

import asyncio
import importlib.machinery
import sys
import types
from pathlib import Path

import pytest

from .optional_deps import HAS_AIOSQLITE, install_aiosqlite_stub

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

if not HAS_AIOSQLITE:
    install_aiosqlite_stub()

import config  # noqa: E402
from agents import assistant as assistant_mod  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _stub_module(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    mod.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _reject_cloud(model_id):
    mid = str(model_id or "")
    return "" if mid.split(":", 1)[0] in ("openai", "anthropic", "custom") and ":" in mid else mid


_ERROR_CHUNK = 'data: {"type": "error", "error": "model exploded"}\n\n'
_DONE_CHUNK = 'data: {"type": "done", "model": "m", "message_id": 3}\n\n'


def _wire_headless(monkeypatch, *, conv, persona=None, chunks=(),
                   conv_after=None):
    """Fake every collaborator of run_headless_chat; returns the capture dict."""
    captured = {}

    async def fake_gen(req, http, events, *maps):
        captured["req"] = req
        for c in chunks:
            yield c

    _stub_module(monkeypatch, "agents.chat", chat_stream_generate=fake_gen)
    _stub_module(monkeypatch, "model_providers", reject_cloud=_reject_cloud)

    convs = {"calls": 0}

    async def fake_get_conversation(cid):
        convs["calls"] += 1
        if convs["calls"] == 1:
            return conv
        return conv_after if conv_after is not None else conv

    async def fake_get_model_config(mc_id):
        return persona

    async def _empty(*a, **k):
        return []

    monkeypatch.setattr(assistant_mod.db, "get_conversation", fake_get_conversation)
    monkeypatch.setattr(assistant_mod.db, "get_model_config", fake_get_model_config)
    monkeypatch.setattr(assistant_mod.db, "get_tools", _empty)
    monkeypatch.setattr(assistant_mod.db, "get_connector_tools", _empty)
    monkeypatch.setattr(config, "DEFAULT_MODEL", "local-default")
    return captured


def _conv(**over):
    base = {"model": "", "tool_ids": [], "model_config_id": "mc1", "messages": []}
    base.update(over)
    return base


# ── run_headless_chat: tool resolution ───────────────────────────────────

def test_missing_conversation_raises(monkeypatch):
    async def none(cid):
        return None
    monkeypatch.setattr(assistant_mod.db, "get_conversation", none)
    with pytest.raises(RuntimeError, match="not found"):
        _run(assistant_mod.run_headless_chat(conversation_id="gone", prompt="x"))


def test_empty_tools_fall_through_to_persona(monkeypatch):
    cap = _wire_headless(monkeypatch, conv=_conv(),
                         persona={"tool_ids": ["codeagent", "quick_search"]},
                         chunks=[_DONE_CHUNK])
    _run(assistant_mod.run_headless_chat(conversation_id="c1", prompt="go"))
    # persona list applies, quick_search stays stripped from scheduled runs
    assert cap["req"].tool_ids == ["codeagent"]


def test_explicit_task_tools_beat_persona(monkeypatch):
    cap = _wire_headless(monkeypatch, conv=_conv(),
                         persona={"tool_ids": ["codeagent"]},
                         chunks=[_DONE_CHUNK])
    _run(assistant_mod.run_headless_chat(conversation_id="c1", prompt="go",
                                         tool_ids=["manage_tasks"]))
    assert cap["req"].tool_ids == ["manage_tasks"]


def test_conversation_tools_beat_persona(monkeypatch):
    cap = _wire_headless(monkeypatch, conv=_conv(tool_ids=["execute_code"]),
                         persona={"tool_ids": ["codeagent"]},
                         chunks=[_DONE_CHUNK])
    _run(assistant_mod.run_headless_chat(conversation_id="c1", prompt="go"))
    assert cap["req"].tool_ids == ["execute_code"]


# ── run_headless_chat: model resolution ──────────────────────────────────

def test_persona_base_model_wins_cloud_allowed(monkeypatch):
    cap = _wire_headless(monkeypatch, conv=_conv(model="llama3"),
                         persona={"tool_ids": [], "base_model": "anthropic:claude-x"},
                         chunks=[_DONE_CHUNK])
    _run(assistant_mod.run_headless_chat(conversation_id="c1", prompt="go"))
    assert cap["req"].model == "anthropic:claude-x"


def test_inherited_cloud_conv_model_is_stripped(monkeypatch):
    cap = _wire_headless(monkeypatch, conv=_conv(model="openai:gpt-9"),
                         persona={"tool_ids": [], "base_model": ""},
                         chunks=[_DONE_CHUNK])
    _run(assistant_mod.run_headless_chat(conversation_id="c1", prompt="go"))
    assert cap["req"].model == "local-default"


def test_local_conv_model_inherited(monkeypatch):
    cap = _wire_headless(monkeypatch, conv=_conv(model="qwen3:32b"),
                         persona=None, chunks=[_DONE_CHUNK])
    _run(assistant_mod.run_headless_chat(conversation_id="c1", prompt="go"))
    assert cap["req"].model == "qwen3:32b"


# ── run_headless_chat: failure honesty + result text ─────────────────────

def test_error_chunk_without_done_raises(monkeypatch):
    _wire_headless(monkeypatch, conv=_conv(), chunks=[_ERROR_CHUNK])
    with pytest.raises(RuntimeError, match="model exploded"):
        _run(assistant_mod.run_headless_chat(conversation_id="c1", prompt="go"))


def test_error_then_done_does_not_raise(monkeypatch):
    _wire_headless(monkeypatch, conv=_conv(), chunks=[_ERROR_CHUNK, _DONE_CHUNK])
    assert _run(assistant_mod.run_headless_chat(conversation_id="c1", prompt="go")) == ""


def test_returns_new_assistant_text(monkeypatch):
    before = _conv(messages=[{"id": 1, "role": "user", "content": "hi"},
                             {"id": 2, "role": "assistant", "content": "old brief"}])
    after = _conv(messages=before["messages"] + [
        {"id": 3, "role": "user", "content": "dump"},
        {"id": 4, "role": "assistant", "content": "fresh brief"}])
    _wire_headless(monkeypatch, conv=before, conv_after=after, chunks=[_DONE_CHUNK])
    out = _run(assistant_mod.run_headless_chat(conversation_id="c1", prompt="go"))
    assert out == "fresh brief"


def test_stale_prior_brief_never_returned(monkeypatch):
    conv = _conv(messages=[{"id": 1, "role": "user", "content": "hi"},
                           {"id": 2, "role": "assistant", "content": "old brief"}])
    _wire_headless(monkeypatch, conv=conv, conv_after=conv, chunks=[_DONE_CHUNK])
    out = _run(assistant_mod.run_headless_chat(conversation_id="c1", prompt="go"))
    assert out == ""  # nothing NEW was persisted — old brief must not leak out


# ── build_checkin_prompt ─────────────────────────────────────────────────

def test_checkin_prompt_stamps_profile_timezone(monkeypatch):
    async def profile(user_id=None):
        return {"timezone": "America/Chicago", "enabled_gatherers": ["t1"]}

    async def gather(user_id):
        return "block content"
    monkeypatch.setattr(assistant_mod.db, "get_assistant_profile", profile)
    monkeypatch.setitem(assistant_mod._GATHERERS, "t1", gather)
    out = _run(assistant_mod.build_checkin_prompt({"user_id": "u1"}, "focus here"))
    assert "America/Chicago)" in out
    assert " UTC)" not in out
    assert "### t1\nblock content" in out
    assert "## Focus for this check-in\nfocus here" in out


def test_checkin_prompt_gatherer_failure_degrades(monkeypatch):
    async def profile(user_id=None):
        return {"timezone": "UTC", "enabled_gatherers": ["boom"]}

    async def gather(user_id):
        raise RuntimeError("imap down")
    monkeypatch.setattr(assistant_mod.db, "get_assistant_profile", profile)
    monkeypatch.setitem(assistant_mod._GATHERERS, "boom", gather)
    out = _run(assistant_mod.build_checkin_prompt({"user_id": "u1"}, ""))
    assert "(unavailable: imap down)" in out


# ── notifications gatherer ───────────────────────────────────────────────

def test_notifications_gatherer_skips_checkins_and_null_bodies(monkeypatch):
    async def rows(limit=10, unseen_only=True, user_id=None):
        return [
            {"kind": "checkin", "title": "Morning check-in", "body": "the brief"},
            {"kind": "task", "title": "Backup done", "body": None},
            {"kind": "email", "title": "Urgent email", "body": "from boss"},
        ]
    monkeypatch.setattr(assistant_mod.db, "list_notifications", rows)
    out = _run(assistant_mod._gather_notifications("u1"))
    assert "Morning check-in" not in out         # checkin rows are noise-looped
    assert "- [task] Backup done: " in out       # None body must not blow up
    assert "- [email] Urgent email: from boss" in out


# ── ensure_assistant self-healing ────────────────────────────────────────

def test_ensure_assistant_repoints_checkins_on_reseed(monkeypatch):
    created, repointed = {}, []

    async def profile(user_id=None):
        return {"user_id": "u1", "model_config_id": "mc1", "conversation_id": "dead"}

    async def get_mc(mc_id):
        return {"id": "mc1"} if mc_id == "mc1" else None

    async def get_conv(cid):
        return created.get(cid)  # "dead" was deleted; new conv exists once created

    async def create_conv(cid, **kw):
        created[cid] = {"id": cid, **kw}

    async def update_conv(cid, **kw):
        return None

    async def list_tasks(user_id=None):
        return [
            {"id": "t1", "task_type": "check_in", "conversation_id": "dead"},
            {"id": "t2", "task_type": "llm", "conversation_id": "dead"},
        ]

    async def update_task(task_id, fields, user_id=None):
        repointed.append((task_id, fields.get("conversation_id")))

    async def upsert(**kw):
        return {"user_id": "u1", **kw}

    monkeypatch.setattr(assistant_mod.db, "get_assistant_profile", profile)
    monkeypatch.setattr(assistant_mod.db, "get_model_config", get_mc)
    monkeypatch.setattr(assistant_mod.db, "get_conversation", get_conv)
    monkeypatch.setattr(assistant_mod.db, "create_conversation", create_conv)
    monkeypatch.setattr(assistant_mod.db, "update_conversation", update_conv)
    monkeypatch.setattr(assistant_mod.db, "list_scheduled_tasks", list_tasks)
    monkeypatch.setattr(assistant_mod.db, "update_scheduled_task", update_task)
    monkeypatch.setattr(assistant_mod.db, "upsert_assistant_profile", upsert)
    monkeypatch.setattr(assistant_mod.db, "_scope_user", lambda uid=None: "u1")

    result = _run(assistant_mod.ensure_assistant("u1"))
    new_id = result["conversation_id"]
    assert new_id and new_id != "dead"
    assert new_id in created
    # only the check_in was repointed; the llm task keeps its own target
    assert repointed == [("t1", new_id)]


# ── scheduler delivery seams ─────────────────────────────────────────────

def test_deliver_result_checkin_kind_and_title(monkeypatch):
    import scheduler
    sent = {}

    async def fake_notify(title, body, **kw):
        sent.update({"title": title, "body": body, **kw})
        return 1
    monkeypatch.setattr(scheduler.notifications, "notify", fake_notify)
    task = {"id": "t1", "title": "Morning check-in", "task_type": "check_in",
            "delivery_json": {"notify": True}, "conversation_id": "c1"}
    _run(scheduler._deliver_result(task, "succeeded", "the actual brief text", ""))
    assert sent["kind"] == "checkin"
    assert sent["title"] == "Morning check-in"     # no "Task done:" boilerplate
    assert sent["body"] == "the actual brief text"


def test_deliver_result_failed_checkin_keeps_task_shape(monkeypatch):
    import scheduler
    sent = {}

    async def fake_notify(title, body, **kw):
        sent.update({"title": title, "body": body, **kw})
        return 1
    monkeypatch.setattr(scheduler.notifications, "notify", fake_notify)
    task = {"id": "t1", "title": "Morning check-in", "task_type": "check_in",
            "delivery_json": {"notify": True}, "conversation_id": "c1"}
    _run(scheduler._deliver_result(task, "failed", "", "model exploded"))
    assert sent["title"].startswith("Task failed:")
    assert sent["body"] == "model exploded"


def test_event_names_include_urgent_email():
    import scheduler
    assert "urgent_email_received" in scheduler.EVENT_NAMES
    assert "research_completed" in scheduler.EVENT_NAMES
    assert "artifact_created" in scheduler.EVENT_NAMES
