import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if importlib.util.find_spec("aiosqlite") is None:
    pytest.skip("aiosqlite not installed", allow_module_level=True)

import database as db  # noqa: E402
import tools  # noqa: E402
from agents import fixer  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeEvents:
    def __init__(self):
        self.events = []

    async def emit(self, conv_id, event_type, data):
        self.events.append((conv_id, event_type, data))


class _FixerHTTP:
    def __init__(self):
        self.posts = []

    async def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        if url.endswith("/api/chat"):
            return _FakeResponse({
                "message": {
                    "content": (
                        "### EDIT: /root/projects/demo/app.py\n"
                        "```python\n"
                        "print('fixed')\n"
                        "```\n\n"
                        "### SUMMARY: use the researched API shape"
                    )
                }
            })

        command = (json or {}).get("command", "")
        if command.startswith("test -d "):
            return _FakeResponse({"exit_code": 0, "stdout": "OK\n", "stderr": ""})
        if command.startswith("cat "):
            return _FakeResponse({"exit_code": 0, "stdout": "print('broken')\n", "stderr": ""})
        if "base64 -d" in command:
            return _FakeResponse({"exit_code": 0, "stdout": "OK\n", "stderr": ""})
        return _FakeResponse({"exit_code": 0, "stdout": "", "stderr": ""})


async def _set_run_times(run_id: str, started_at: str, ended_at: str | None = None):
    conn = await db.get_db()
    try:
        await conn.execute(
            "UPDATE runs SET started_at=?, ended_at=? WHERE id=?",
            (started_at, ended_at, run_id),
        )
        await conn.commit()
    finally:
        await conn.close()


def test_recent_research_cache_is_lru_bounded_and_expires(monkeypatch):
    tools._RECENT_RESEARCH.clear()
    monkeypatch.setattr(tools.time, "time", lambda: 1000.0)

    for i in range(tools._RECENT_RESEARCH_MAX):
        tools._stash_research_result(f"conv-{i}", f"topic {i}", f"report {i}")

    assert tools._get_recent_research("conv-0")["report"] == "report 0"
    tools._stash_research_result("conv-new", "topic new", "report new")

    assert len(tools._RECENT_RESEARCH) == tools._RECENT_RESEARCH_MAX
    assert "conv-0" in tools._RECENT_RESEARCH
    assert "conv-1" not in tools._RECENT_RESEARCH

    monkeypatch.setattr(tools.time, "time", lambda: 1601.0)
    assert tools._get_recent_research("conv-0") is None
    assert "conv-0" not in tools._RECENT_RESEARCH


def test_deep_research_since_checks_cache_timestamp(monkeypatch):
    tools._RECENT_RESEARCH.clear()
    monkeypatch.setattr(tools.time, "time", lambda: 1000.0)
    monkeypatch.setattr(tools.db, "get_conversation", AsyncMock(return_value={"messages": []}))
    tools._stash_research_result("conv-cache", "topic", "report")

    assert _run(tools._deep_research_called_since(
        "conv-cache", "1970-01-01T00:16:39.500000"
    )) is True
    assert _run(tools._deep_research_called_since(
        "conv-cache", "1970-01-01T00:16:41"
    )) is False


def test_deep_research_since_falls_back_to_saved_events(monkeypatch):
    tools._RECENT_RESEARCH.clear()
    monkeypatch.setattr(tools.db, "get_conversation", AsyncMock(return_value={
        "messages": [{
            "role": "assistant",
            "created_at": "2026-01-01 00:00:02",
            "metadata": {
                "saved_events": [{
                    "type": "tool_start",
                    "data": {"tool": "deep_research"},
                }]
            },
        }]
    }))

    assert _run(tools._deep_research_called_since(
        "conv-events", "2026-01-01 00:00:00"
    )) is True


def test_fixer_persists_research_used_when_context_is_injected(tmp_path, monkeypatch):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_conversation("conv-fixer", "Fixer Research Test"))
    _run(db.create_run("run-review", "conv-fixer", role="reviewer", status="succeeded"))
    _run(db.update_run("run-review", status="succeeded", result_envelope={
        "status": "issues",
        "summary": "The app uses an outdated API.",
        "project_dir": "/root/projects/demo",
        "language": "python",
        "build_cmd": "python -m py_compile app.py",
        "test_cmd": "pytest -q",
        "issues": [{
            "severity": "test",
            "file": "app.py",
            "summary": "Outdated API call fails.",
            "suggested_fix_scope": ["app.py"],
        }],
    }, ended=True))

    http = _FixerHTTP()
    events = _FakeEvents()
    monkeypatch.setattr(fixer.config, "CODEBOX_URL", "http://codebox")
    monkeypatch.setattr(fixer.config, "OLLAMA_URL", "http://ollama")
    monkeypatch.setattr(fixer.config, "FIXER_MODEL", "fixer-test-model")
    monkeypatch.setattr(fixer.config, "CODER_MODEL", "")
    monkeypatch.setattr(fixer.config, "PLANNING_MODEL", "")
    monkeypatch.setattr(fixer.config, "DEFAULT_MODEL", "fallback-model")
    monkeypatch.setattr(fixer.config, "DEFAULT_NUM_CTX", 8192)

    envelope = _run(fixer.run_fixer(
        http, events, "conv-fixer",
        reviewer_run_id="run-review",
        research_context="# Agent Research\nUse the new API shape.",
    ))

    assert envelope["status"] == "applied"
    assert envelope["research_used"] is True
    ollama_prompt = [p for p in http.posts if p["url"].endswith("/api/chat")][0]["json"]["messages"][0]["content"]
    assert "Reference (recent web research from this conversation)" in ollama_prompt

    runs = _run(db.get_runs_by_conversation("conv-fixer", limit=5))
    fixer_run = next(r for r in runs if r["role"] == "fixer")
    assert fixer_run["result_envelope"]["research_used"] is True


def test_v2_gate_allows_agent_research_after_fixer_attempt(tmp_path, monkeypatch):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_model_config(
        "mc-v2", "Daedalus Coder v2", "test-model",
        tool_ids=["deep_research", "run_fixer"],
    ))
    _run(db.create_conversation(
        "conv-v2", "V2 Research Gate", model_config_id="mc-v2",
    ))

    _run(db.create_run("run-review-1", "conv-v2", role="reviewer", status="succeeded"))
    _run(db.update_run("run-review-1", status="succeeded", result_envelope={
        "status": "issues",
        "summary": "First review failed.",
        "project_dir": "/root/projects/demo",
        "issues": [{"file": "app.py", "summary": "API mismatch."}],
    }, ended=True))
    _run(_set_run_times("run-review-1", "2026-01-01T00:00:00", "2026-01-01T00:00:01"))

    _run(db.create_run("run-fixer-1", "conv-v2", role="fixer", parent_run_id="run-review-1", status="succeeded"))
    _run(db.update_run("run-fixer-1", status="succeeded", result_envelope={
        "status": "applied",
        "source_role": "reviewer",
        "files_touched": ["/root/projects/demo/app.py"],
    }, ended=True))
    _run(_set_run_times("run-fixer-1", "2026-01-01T00:00:02", "2026-01-01T00:00:03"))

    _run(db.create_run("run-review-2", "conv-v2", role="reviewer", status="succeeded"))
    _run(db.update_run("run-review-2", status="succeeded", result_envelope={
        "status": "issues",
        "summary": "Same issue returned after fixer.",
        "project_dir": "/root/projects/demo",
        "issues": [{"file": "app.py", "summary": "API mismatch."}],
    }, ended=True))
    _run(_set_run_times("run-review-2", "2026-01-01T00:00:04", "2026-01-01T00:00:05"))

    async def fake_run_deep_research(*_args, **_kwargs):
        return {
            "report": "Use the current API documented upstream.",
            "sources": [],
            "source_count": 0,
            "total_searches": 0,
            "pages_read": 0,
            "elapsed": 0.2,
            "key_entities": [],
        }

    tools._RECENT_RESEARCH.clear()
    monkeypatch.setattr(tools, "run_deep_research", fake_run_deep_research)
    events = _FakeEvents()

    result = _run(tools.exec_tool(
        http=object(),
        events=events,
        name="deep_research",
        args={"topic": "API mismatch exact error", "depth": 2},
        conv_id="conv-v2",
    ))

    assert "BLOCKED" not in result
    assert result.startswith("# Agent Research: API mismatch exact error")
    assert tools._get_recent_research("conv-v2") is not None
    assert any(ev[1] == "tool_start" and ev[2].get("tool") == "deep_research" for ev in events.events)
