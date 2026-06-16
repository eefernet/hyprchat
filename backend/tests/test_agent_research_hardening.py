import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from .optional_deps import HAS_AIOSQLITE


BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if not HAS_AIOSQLITE:
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


class _NoPostHTTP:
    async def post(self, *_args, **_kwargs):
        raise AssertionError("unexpected direct HTTP post")




class _OllamaStreamShim:
    """Adapts a post()-style fake to complete_chat's streaming Ollama path:
    wraps the fake's JSON body into a single NDJSON chunk."""

    def __init__(self, fake, url, json_payload, timeout):
        self.fake = fake
        self.url = url
        self.json_payload = json_payload
        self.timeout = timeout

    async def __aenter__(self):
        import json as _json
        resp = await self.fake.post(self.url, json=self.json_payload, timeout=self.timeout)
        body = resp.json() if resp.status_code == 200 else {}

        class _SResp:
            status_code = resp.status_code

            async def aiter_lines(_s):
                msg = body.get("message") if isinstance(body.get("message"), dict) else {}
                yield _json.dumps({"message": msg, "thinking": body.get("thinking"), "done": True})

        return _SResp()

    async def __aexit__(self, *a):
        return False

class _FixerHTTP:
    def __init__(self):
        self.posts = []

    def stream(self, method, url, json=None, timeout=None):
        return _OllamaStreamShim(self, url, json, timeout)

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


def test_conspiracy_research_depth_coercion_handles_bad_strings(monkeypatch):
    seen = []

    async def fake_conspiracy_research(_http, _ollama_url, _default_model, _searxng_url,
                                       _events, topic, angle, depth, conv_id,
                                       kb_context=""):
        seen.append({"topic": topic, "angle": angle, "depth": depth, "conv_id": conv_id})
        return f"depth={depth}"

    monkeypatch.setattr(tools, "run_conspiracy_research", fake_conspiracy_research)

    result = _run(tools.exec_tool(
        http=object(), events=_FakeEvents(),
        name="conspiracy_research",
        args={"topic": "dummy topic", "angle": "evidence", "depth": "not-a-number"},
        conv_id="",
    ))

    assert result == "depth=4"
    assert seen == [{"topic": "dummy topic", "angle": "evidence", "depth": 4, "conv_id": ""}]

    result = _run(tools.exec_tool(
        http=object(), events=_FakeEvents(),
        name="conspiracy_research",
        args={"topic": "dummy topic", "depth": "5"},
        conv_id="",
    ))

    assert result == "depth=5"
    assert seen[-1]["depth"] == 5


def test_plan_project_uses_architect_for_non_daedalus_codeagent(tmp_path, monkeypatch):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_model_config(
        "mc-codeagent", "Plain CodeAgent", "test-model",
        tool_ids=["plan_project", "generate_code"],
    ))
    _run(db.create_conversation(
        "conv-codeagent", "Non-Daedalus CodeAgent", model_config_id="mc-codeagent",
    ))

    from agents import architect

    calls = []

    async def fake_run_architect(http, events, conv_id, *, task, language_hint,
                                 kb_chunks=None, conv_model=""):
        calls.append({
            "http": http,
            "conv_id": conv_id,
            "task": task,
            "language_hint": language_hint,
            "kb_chunks": kb_chunks,
            "conv_model": conv_model,
        })
        return {
            "status": "ok",
            "run_id": "run-architect",
            "plan": {"project_id": "proj-architect"},
        }

    monkeypatch.setattr(architect, "run_architect", fake_run_architect)
    monkeypatch.setattr(architect, "format_plan_for_chat", lambda plan: "ARCHITECT PLAN")

    result = _run(tools.exec_tool(
        http=_NoPostHTTP(), events=_FakeEvents(),
        name="plan_project",
        args={"task": "build a tiny app", "language": "python"},
        conv_id="conv-codeagent",
        conv_model="plain-model",
    ))

    assert result == "ARCHITECT PLAN"
    assert len(calls) == 1
    assert calls[0]["conv_id"] == "conv-codeagent"
    assert calls[0]["language_hint"] == "python"
    workflow = _run(db.get_latest_coder_workflow("conv-codeagent", "proj-architect"))
    assert workflow is not None
    assert workflow["state"] == "planning"
    assert workflow["active_run_id"] == "run-architect"


# ---------------------------------------------------------------------------
# Turn-scoped gate policy (G1/G2) + research-anchor unification
# ---------------------------------------------------------------------------

async def _set_message_times(conv_id: str, ts: str):
    conn = await db.get_db()
    try:
        await conn.execute(
            "UPDATE messages SET created_at=? WHERE conversation_id=?",
            (ts, conv_id),
        )
        await conn.commit()
    finally:
        await conn.close()


def _patch_fixer_config(monkeypatch):
    monkeypatch.setattr(fixer.config, "CODEBOX_URL", "http://codebox")
    monkeypatch.setattr(fixer.config, "OLLAMA_URL", "http://ollama")
    monkeypatch.setattr(fixer.config, "FIXER_MODEL", "fixer-test-model")
    monkeypatch.setattr(fixer.config, "CODER_MODEL", "")
    monkeypatch.setattr(fixer.config, "PLANNING_MODEL", "")
    monkeypatch.setattr(fixer.config, "DEFAULT_MODEL", "fallback-model")
    monkeypatch.setattr(fixer.config, "DEFAULT_NUM_CTX", 8192)
    monkeypatch.setattr(fixer.config, "AIDER_ENABLED", False)


def _seed_cap_conversation(conv_id: str, mc_id: str, *, fix_role: str = "fixer"):
    """Three successful reviewer-driven fix runs + a reviewer with issues."""
    _run(db.create_model_config(mc_id, "Daedalus Coder v2", "test-model",
                                tool_ids=["run_fixer"]))
    _run(db.create_conversation(conv_id, "Cap Test", model_config_id=mc_id))
    for i in range(3):
        rid = f"run-fx-{conv_id}-{i}"
        _run(db.create_run(rid, conv_id, role=fix_role, status="succeeded"))
        _run(db.update_run(rid, status="succeeded", result_envelope={
            "status": "applied", "source_role": "reviewer",
        }, ended=True))
        _run(_set_run_times(rid, f"2026-01-01T00:00:0{i}", f"2026-01-01T00:00:0{i}"))
    _run(db.create_run(f"run-rev-{conv_id}", conv_id, role="reviewer", status="succeeded"))
    _run(db.update_run(f"run-rev-{conv_id}", status="succeeded", result_envelope={
        "status": "issues", "summary": "still broken",
        "project_dir": "/root/projects/demo",
        "issues": [{"file": "app.py", "summary": "boom",
                    "suggested_fix_scope": ["app.py"]}],
    }, ended=True))
    _run(_set_run_times(f"run-rev-{conv_id}", "2026-01-01T00:00:05", "2026-01-01T00:00:06"))


def test_cycle_cap_resets_after_new_user_message(tmp_path, monkeypatch):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _seed_cap_conversation("conv-capr", "mc-capr")
    # The user sends a NEW request after the three exhausted cycles.
    _run(db.add_message("conv-capr", "user", "try a different approach"))
    _run(_set_message_times("conv-capr", "2026-01-01 00:01:00"))
    _patch_fixer_config(monkeypatch)

    result = _run(tools.exec_tool(
        http=_FixerHTTP(), events=_FakeEvents(),
        name="run_fixer", args={"reviewer_run_id": "run-rev-conv-capr"},
        conv_id="conv-capr",
    ))

    # The cap BLOCK message says "Hard cap of"; the phrase "Hard cap:" also
    # appears benignly in chained-review guidance text.
    assert "Hard cap of" not in result
    assert "BLOCKED" not in result


def test_cycle_cap_blocks_within_same_user_turn(tmp_path, monkeypatch):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _seed_cap_conversation("conv-capb", "mc-capb")
    # The user message predates the three cycles — same request, cap holds.
    _run(db.add_message("conv-capb", "user", "fix my app"))
    _run(_set_message_times("conv-capb", "2025-12-31 00:00:00"))
    _patch_fixer_config(monkeypatch)

    result = _run(tools.exec_tool(
        http=_FixerHTTP(), events=_FakeEvents(),
        name="run_fixer", args={"reviewer_run_id": "run-rev-conv-capb"},
        conv_id="conv-capb",
    ))

    assert "BLOCKED" in result and "Hard cap" in result


def test_aider_success_counts_toward_cap(tmp_path, monkeypatch):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _seed_cap_conversation("conv-capa", "mc-capa", fix_role="aider.fix")
    _run(db.add_message("conv-capa", "user", "fix my app"))
    _run(_set_message_times("conv-capa", "2025-12-31 00:00:00"))
    _patch_fixer_config(monkeypatch)

    result = _run(tools.exec_tool(
        http=_FixerHTTP(), events=_FakeEvents(),
        name="run_aider_fix", args={"task": "fix the reviewer issues"},
        conv_id="conv-capa",
    ))

    assert "BLOCKED" in result and "Hard cap" in result


def _seed_qa_conversation(conv_id: str, mc_id: str):
    _run(db.create_model_config(mc_id, "Daedalus Coder v2", "test-model",
                                tool_ids=["ask_project"]))
    _run(db.create_conversation(conv_id, "QA Test", model_config_id=mc_id))
    _run(db.create_run(f"run-qa-{conv_id}", conv_id, role="qa", status="succeeded"))
    _run(db.update_run(f"run-qa-{conv_id}", status="succeeded", result_envelope={
        "status": "answered", "answer": "It works like this.",
        "looks_like_change_request": False,
    }, ended=True))
    _run(_set_run_times(f"run-qa-{conv_id}", "2026-01-01T00:00:10", "2026-01-01T00:00:11"))


def test_qa_terminal_gate_releases_on_new_user_turn(tmp_path):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _seed_qa_conversation("conv-qar", "mc-qar")
    _run(db.add_message("conv-qar", "user", "now run the tests"))
    _run(_set_message_times("conv-qar", "2026-01-01 00:01:00"))

    result = _run(tools.exec_tool(
        http=object(), events=_FakeEvents(),
        name="read_file", args={"path": "/root/projects/demo/app.py"},
        conv_id="conv-qar",
    ))

    # Gate released — the tool reaches its dispatcher (which then fails on the
    # stub http object, proving we got past the gate).
    assert "BLOCKED" not in result


def test_qa_terminal_gate_blocks_same_turn(tmp_path):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _seed_qa_conversation("conv-qab", "mc-qab")
    _run(db.add_message("conv-qab", "user", "how does it work?"))
    _run(_set_message_times("conv-qab", "2026-01-01 00:00:00"))

    result = _run(tools.exec_tool(
        http=object(), events=_FakeEvents(),
        name="read_file", args={"path": "/root/projects/demo/app.py"},
        conv_id="conv-qab",
    ))

    assert "BLOCKED" in result and "ask_project" in result


def test_research_release_uses_reviewer_started_at(tmp_path, monkeypatch):
    """Research stashed while the review was still running satisfies the
    fix-needed gate's whitelist check, so deep_research is NOT re-whitelisted
    (it would be redundant) and run_fixer is NOT research-blocked."""
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_model_config("mc-anchor", "Daedalus Coder v2", "test-model",
                                tool_ids=["run_fixer", "deep_research"]))
    _run(db.create_conversation("conv-anchor", "Anchor Test", model_config_id="mc-anchor"))

    _run(db.create_run("run-fx-anchor", "conv-anchor", role="fixer", status="succeeded"))
    _run(db.update_run("run-fx-anchor", status="succeeded", result_envelope={
        "status": "applied", "source_role": "reviewer",
    }, ended=True))
    _run(_set_run_times("run-fx-anchor", "1970-01-01T00:16:00", "1970-01-01T00:16:10"))

    _run(db.create_run("run-rev-anchor", "conv-anchor", role="reviewer", status="succeeded"))
    _run(db.update_run("run-rev-anchor", status="succeeded", result_envelope={
        "status": "issues", "summary": "still broken",
        "project_dir": "/root/projects/demo",
        "issues": [{"file": "app.py", "summary": "boom",
                    "suggested_fix_scope": ["app.py"]}],
    }, ended=True))
    # Review ran from 00:16:40 (=1000s) to 00:17:00 (=1020s).
    _run(_set_run_times("run-rev-anchor", "1970-01-01T00:16:40", "1970-01-01T00:17:00"))

    # Research stashed at t=1010 — between the review's start and end.
    tools._RECENT_RESEARCH.clear()
    monkeypatch.setattr(tools.time, "time", lambda: 1010.0)
    tools._stash_research_result("conv-anchor", "boom error", "use the new API")
    monkeypatch.setattr(tools.config, "AIDER_ENABLED", False)
    _patch_fixer_config(monkeypatch)

    # deep_research is no longer whitelisted (research already done since
    # started_at) → fix-needed gate blocks it and routes to run_fixer.
    res_dr = _run(tools.exec_tool(
        http=object(), events=_FakeEvents(),
        name="deep_research", args={"topic": "boom", "depth": 2},
        conv_id="conv-anchor",
    ))
    assert "BLOCKED" in res_dr and "run_fixer" in res_dr

    # ...and run_fixer is NOT research-blocked (STUCK uses the same anchor).
    res_fx = _run(tools.exec_tool(
        http=_FixerHTTP(), events=_FakeEvents(),
        name="run_fixer", args={"reviewer_run_id": "run-rev-anchor"},
        conv_id="conv-anchor",
    ))
    assert "Agent Research first" not in res_fx
    assert "BLOCKED" not in res_fx


def test_fixer_receives_prior_attempt_history(tmp_path, monkeypatch):
    """Attempt #2 must see what attempt #1 already changed."""
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_model_config("mc-hist", "Daedalus Coder v2", "test-model",
                                tool_ids=["run_fixer"]))
    _run(db.create_conversation("conv-hist", "Attempt History", model_config_id="mc-hist"))
    _run(db.add_message("conv-hist", "user", "fix my app"))
    _run(_set_message_times("conv-hist", "2025-12-31 00:00:00"))

    # Attempt #1: a fixer run that already touched app.py but didn't stick.
    _run(db.create_run("run-fx-hist", "conv-hist", role="fixer", status="succeeded"))
    _run(db.update_run("run-fx-hist", status="succeeded", result_envelope={
        "status": "applied", "source_role": "reviewer",
        "files_touched": ["/root/projects/demo/app.py"],
        "summary": "Renamed handler to fix import",
    }, ended=True))
    _run(_set_run_times("run-fx-hist", "2026-01-01T00:00:01", "2026-01-01T00:00:02"))

    _run(db.create_run("run-rev-hist", "conv-hist", role="reviewer", status="succeeded"))
    _run(db.update_run("run-rev-hist", status="succeeded", result_envelope={
        "status": "issues", "summary": "still broken",
        "project_dir": "/root/projects/demo",
        "issues": [{"file": "app.py", "summary": "boom",
                    "suggested_fix_scope": ["app.py"]}],
    }, ended=True))
    _run(_set_run_times("run-rev-hist", "2026-01-01T00:00:03", "2026-01-01T00:00:04"))

    _patch_fixer_config(monkeypatch)
    http = _FixerHTTP()

    result = _run(tools.exec_tool(
        http=http, events=_FakeEvents(),
        name="run_fixer", args={"reviewer_run_id": "run-rev-hist"},
        conv_id="conv-hist",
    ))

    assert "BLOCKED" not in result
    prompt = [p for p in http.posts if p["url"].endswith("/api/chat")][0]["json"]["messages"][0]["content"]
    assert "Previous fix attempts" in prompt
    assert "app.py" in prompt
    assert "Renamed handler to fix import" in prompt

    runs = _run(db.get_runs_by_conversation("conv-hist", limit=10))
    new_fixer = next(r for r in runs if r["role"] == "fixer" and r["id"] != "run-fx-hist")
    assert new_fixer["result_envelope"]["attempt_history_used"] is True


def test_prior_attempt_context_excludes_older_turns(tmp_path):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_conversation("conv-hist2", "Attempt History 2"))
    _run(db.create_run("run-fx-old", "conv-hist2", role="fixer", status="succeeded"))
    _run(db.update_run("run-fx-old", status="succeeded", result_envelope={
        "status": "applied", "files_touched": ["/root/projects/demo/old.py"],
        "summary": "old turn fix",
    }, ended=True))
    _run(_set_run_times("run-fx-old", "2026-01-01T00:00:00", "2026-01-01T00:00:01"))
    # New user message AFTER the old attempt — history resets.
    _run(db.add_message("conv-hist2", "user", "now do something else"))
    _run(_set_message_times("conv-hist2", "2026-01-01 00:10:00"))

    ctx = _run(tools._prior_fix_attempts_context("conv-hist2"))

    assert ctx == ""


def test_prior_acceptance_context_returns_latest_issue_verdict(tmp_path):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_conversation("conv-pacc", "Prior Acceptance"))
    _run(db.create_run("run-acc-1", "conv-pacc", role="acceptance", status="succeeded"))
    _run(db.update_run("run-acc-1", status="succeeded", result_envelope={
        "status": "issues",
        "issues": [{"category": "packaging", "file": "pyproject.toml",
                    "summary": "missing console_scripts entry"}],
    }, ended=True))
    _run(_set_run_times("run-acc-1", "2026-01-01T00:00:01", "2026-01-01T00:00:02"))

    ctx = _run(tools._prior_acceptance_issues_context("conv-pacc"))
    assert "packaging" in ctx and "console_scripts" in ctx

    # An accepted verdict on top clears the constraint.
    _run(db.create_run("run-acc-2", "conv-pacc", role="acceptance", status="succeeded"))
    _run(db.update_run("run-acc-2", status="succeeded", result_envelope={
        "status": "accepted", "issues": [],
    }, ended=True))
    _run(_set_run_times("run-acc-2", "2026-01-01T00:00:03", "2026-01-01T00:00:04"))
    assert _run(tools._prior_acceptance_issues_context("conv-pacc")) == ""


def test_fix_budget_note_counts_per_request(tmp_path):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_conversation("conv-budget", "Budget"))
    _run(db.add_message("conv-budget", "user", "fix it"))
    _run(_set_message_times("conv-budget", "2025-12-31 00:00:00"))
    for i, role in enumerate(["fixer", "aider.fix"]):
        rid = f"run-bgt-{i}"
        _run(db.create_run(rid, "conv-budget", role=role, status="succeeded"))
        _run(db.update_run(rid, status="succeeded", result_envelope={
            "status": "applied", "source_role": "reviewer",
        }, ended=True))
        _run(_set_run_times(rid, f"2026-01-01T00:00:0{i}", f"2026-01-01T00:00:0{i}"))

    note = _run(tools._fix_budget_note("conv-budget", "reviewer"))
    assert "2/3" in note
    note_acc = _run(tools._fix_budget_note("conv-budget", "acceptance"))
    assert "0/2" in note_acc


def test_fixer_applied_creates_git_checkpoint(tmp_path, monkeypatch):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_model_config("mc-git", "Daedalus Coder v2", "test-model",
                                tool_ids=["run_fixer"]))
    _run(db.create_conversation("conv-git", "Git Checkpoint", model_config_id="mc-git"))
    _run(db.create_run("run-rev-git", "conv-git", role="reviewer", status="succeeded"))
    _run(db.update_run("run-rev-git", status="succeeded", result_envelope={
        "status": "issues", "summary": "broken",
        "project_dir": "/root/projects/demo",
        "issues": [{"file": "app.py", "summary": "boom",
                    "suggested_fix_scope": ["app.py"]}],
    }, ended=True))
    _patch_fixer_config(monkeypatch)
    http = _FixerHTTP()

    result = _run(tools.exec_tool(
        http=http, events=_FakeEvents(),
        name="run_fixer", args={"reviewer_run_id": "run-rev-git"},
        conv_id="conv-git",
    ))

    assert "BLOCKED" not in result
    git_cmds = [p["json"]["command"] for p in http.posts
                if isinstance(p.get("json"), dict) and "git" in (p["json"].get("command") or "")]
    assert any("git add -A" in c and "commit" in c for c in git_cmds), git_cmds


def test_workflow_fsm_creates_and_transitions(tmp_path):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_conversation("conv-fsm", "FSM Test"))

    # First event creates the row (greenfield builds had none before).
    wf_id = _run(tools._apply_workflow_event(
        "conv-fsm", "PLAN_DONE", run_id="run-p1",
        project_id="proj-fsm", user_task="build a thing",
    ))
    assert wf_id
    wf = _run(db.get_coder_workflow(wf_id))
    assert wf["state"] == "planning"

    _run(tools._apply_workflow_event("conv-fsm", "BUILD_OK", run_id="run-b1", project_id="proj-fsm"))
    assert _run(db.get_coder_workflow(wf_id))["state"] == "reviewing"
    _run(tools._apply_workflow_event("conv-fsm", "REVIEW_ISSUES", run_id="run-r1"))
    assert _run(db.get_coder_workflow(wf_id))["state"] == "fixing"
    _run(tools._apply_workflow_event("conv-fsm", "FIX_APPLIED", run_id="run-f1"))
    assert _run(db.get_coder_workflow(wf_id))["state"] == "reviewing"
    _run(tools._apply_workflow_event("conv-fsm", "REVIEW_CLEAN", run_id="run-r2"))
    assert _run(db.get_coder_workflow(wf_id))["state"] == "accepting"
    _run(tools._apply_workflow_event("conv-fsm", "ACCEPT_OK", run_id="run-a1"))
    wf = _run(db.get_coder_workflow(wf_id))
    assert wf["state"] == "accepted"
    assert wf["artifact_status"] == "accepted"

    # Unknown events are ignored; cancel is never overwritten.
    assert _run(tools._apply_workflow_event("conv-fsm", "NOT_AN_EVENT")) == ""
    _run(db.update_coder_workflow(wf_id, state="cancelled", cancel_requested=True))
    _run(tools._apply_workflow_event("conv-fsm", "BUILD_OK", run_id="run-b2"))
    assert _run(db.get_coder_workflow(wf_id))["state"] == "cancelled"


def test_fixer_applied_chains_automatic_review(tmp_path, monkeypatch):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_model_config("mc-chain", "Daedalus Coder v2", "test-model",
                                tool_ids=["run_fixer"]))
    _run(db.create_conversation("conv-chain", "Auto Chain", model_config_id="mc-chain"))
    _run(db.create_run("run-rev-chain", "conv-chain", role="reviewer", status="succeeded"))
    _run(db.update_run("run-rev-chain", status="succeeded", result_envelope={
        "status": "issues", "summary": "broken",
        "project_dir": "/root/projects/demo",
        "issues": [{"file": "app.py", "summary": "boom",
                    "suggested_fix_scope": ["app.py"]}],
    }, ended=True))
    _patch_fixer_config(monkeypatch)

    result = _run(tools.exec_tool(
        http=_FixerHTTP(), events=_FakeEvents(),
        name="run_fixer", args={"reviewer_run_id": "run-rev-chain"},
        conv_id="conv-chain",
    ))

    assert "AUTOMATIC VERIFICATION" in result
    assert "do NOT call it again" in result
    # The chain persisted a NEW reviewer run on top of the fixer run.
    runs = _run(db.get_runs_by_conversation("conv-chain", limit=10))
    roles = [r["role"] for r in runs]
    assert roles[0] == "reviewer" and "fixer" in roles
    # And the FSM tracked it: a workflow row now exists.
    wf = _run(db.get_latest_coder_workflow("conv-chain"))
    assert wf is not None


def test_reaper_fails_orphaned_research_reports(tmp_path):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_research_report("rep-stuck", query="q1", title="Stuck"))
    _run(db.update_research_report("rep-stuck", status="running"))
    _run(db.create_research_report("rep-done", query="q2", title="Done"))
    _run(db.update_research_report("rep-done", status="complete"))

    stats = _run(db.reap_stale_runs())

    assert stats["reports_reaped"] == 1
    assert _run(db.get_research_report("rep-stuck"))["status"] == "failed"
    assert _run(db.get_research_report("rep-done"))["status"] == "complete"
