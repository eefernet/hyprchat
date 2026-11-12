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
    pytest.skip(
        "aiosqlite not installed; install backend/requirements.txt to run required Daedalus hardening coverage",
        allow_module_level=True,
    )

import database as db  # noqa: E402
import tools  # noqa: E402


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


# NOTE: the stranded-architect auto-recovery test was removed with the
# Architect→Builder auto-handoff. Daedalus no longer auto-resumes a completed
# plan into the Builder; the model calls generate_code on a later turn.


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


def _issue_env(summary="boom"):
    return {
        "status": "issues", "summary": "still broken",
        "project_dir": "/root/projects/demo",
        "issues": [{"file": "app.py", "summary": summary,
                    "suggested_fix_scope": ["app.py"]}],
    }


def _seed_cap_conversation(conv_id: str, mc_id: str, *, fix_role: str = "fixer",
                           attempts: int = 4):
    """Interleave issue→fix pairs so every fix attempt is VERIFIED as
    no-progress (same file flagged again), ending on a reviewer with issues.
    The progress budget scores verified no-progress attempts, so seeds must
    interleave — a bare pile of fix runs has pending verification and counts
    a zero streak. Returns the final reviewer run id."""
    _run(db.create_model_config(mc_id, "Daedalus Coder v2", "test-model",
                                tool_ids=["run_fixer"]))
    _run(db.create_conversation(conv_id, "Cap Test", model_config_id=mc_id))
    t = 0
    for i in range(attempts):
        rev_id = f"run-rev-{conv_id}-{i}"
        _run(db.create_run(rev_id, conv_id, role="reviewer", status="succeeded"))
        _run(db.update_run(rev_id, status="succeeded",
                           result_envelope=_issue_env(f"boom take {i}"), ended=True))
        _run(_set_run_times(rev_id, f"2026-01-01T00:00:{t:02d}", f"2026-01-01T00:00:{t:02d}"))
        t += 1
        fix_id = f"run-fx-{conv_id}-{i}"
        _run(db.create_run(fix_id, conv_id, role=fix_role,
                           parent_run_id=rev_id, status="succeeded"))
        _run(db.update_run(fix_id, status="succeeded", result_envelope={
            "status": "applied", "source_role": "reviewer",
        }, ended=True))
        _run(_set_run_times(fix_id, f"2026-01-01T00:00:{t:02d}", f"2026-01-01T00:00:{t:02d}"))
        t += 1
    final_rev = f"run-rev-{conv_id}"
    _run(db.create_run(final_rev, conv_id, role="reviewer", status="succeeded"))
    _run(db.update_run(final_rev, status="succeeded",
                       result_envelope=_issue_env("boom final"), ended=True))
    _run(_set_run_times(final_rev, f"2026-01-01T00:00:{t:02d}", f"2026-01-01T00:00:{t + 1:02d}"))
    return final_rev


# ---------------------------------------------------------------------------
# Acceptance-driven escalation ladder: progress budget → research → last shot → stop/ask
# ---------------------------------------------------------------------------

def _seed_acceptance_conversation(conv_id: str, mc_id: str, *,
                                  attempts: int, sig: str = "boom"):
    """Interleave acceptance-issue→fix pairs so each acceptance-driven fix is
    VERIFIED as no-progress by the next acceptance run (same file), ending on
    an acceptance run with issues. Returns that final acceptance run id."""
    _run(db.create_model_config(mc_id, "Daedalus Coder v2", "test-model",
                                tool_ids=["run_fixer", "write_file"]))
    _run(db.create_conversation(conv_id, "Accept Test", model_config_id=mc_id))
    t = 0
    for j in range(attempts):
        aid = f"run-acc-{conv_id}-{j}"
        _run(db.create_run(aid, conv_id, role="acceptance", status="succeeded"))
        _run(db.update_run(aid, status="succeeded", result_envelope={
            "status": "issues", "summary": "still broken",
            "project_dir": "/root/projects/demo",
            "issues": [{"file": "app.py", "summary": f"{sig} round {j}",
                        "suggested_fix_scope": ["app.py"]}],
        }, ended=True))
        _run(_set_run_times(aid, f"2026-01-01T00:00:{t:02d}", f"2026-01-01T00:00:{t:02d}"))
        t += 1
        rid = f"run-fx-{conv_id}-{j}"
        _run(db.create_run(rid, conv_id, role="fixer",
                           parent_run_id=aid, status="succeeded"))
        _run(db.update_run(rid, status="succeeded", result_envelope={
            "status": "applied", "source_role": "acceptance",
        }, ended=True))
        _run(_set_run_times(rid, f"2026-01-01T00:00:{t:02d}", f"2026-01-01T00:00:{t:02d}"))
        t += 1
    last_acc = f"run-acc-{conv_id}-final"
    _run(db.create_run(last_acc, conv_id, role="acceptance", status="succeeded"))
    _run(db.update_run(last_acc, status="succeeded", result_envelope={
        "status": "issues", "summary": "still broken",
        "project_dir": "/root/projects/demo",
        "issues": [{"file": "app.py", "summary": f"{sig} final",
                    "suggested_fix_scope": ["app.py"]}],
    }, ended=True))
    _run(_set_run_times(last_acc, f"2026-01-01T00:00:{t:02d}", f"2026-01-01T00:00:{t + 1:02d}"))
    return last_acc


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


def test_reaper_preserves_orphaned_research_as_interrupted(tmp_path):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_research_report("rep-stuck", query="q1", title="Stuck"))
    _run(db.update_research_report("rep-stuck", status="running"))
    _run(db.create_research_report("rep-done", query="q2", title="Done"))
    _run(db.update_research_report("rep-done", status="complete"))

    stats = _run(db.reap_stale_runs())

    assert stats["reports_reaped"] == 1
    assert _run(db.get_research_report("rep-stuck"))["status"] == "interrupted"
    assert _run(db.get_research_report("rep-done"))["status"] == "complete"
