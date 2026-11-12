import asyncio
import importlib
import shlex
import subprocess
import sys
import tarfile
import types
from pathlib import Path

import pytest

from .optional_deps import (
    HAS_AIOSQLITE,
    HAS_CHROMADB,
    HAS_FASTAPI,
    HAS_PYDANTIC,
    install_aiosqlite_stub,
    install_rag_stub,
    module_stub,
)


BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_HAS_AIOSQLITE = HAS_AIOSQLITE
if not _HAS_AIOSQLITE:
    install_aiosqlite_stub()

from tools import CODEAGENT_TOOLS


def _run(coro):
    return asyncio.run(coro)


def _import_chat_with_optional_stubs(monkeypatch):
    if not HAS_CHROMADB:
        install_rag_stub(monkeypatch)
    async def _noop_quick_search(*args, **kwargs):
        return None

    monkeypatch.setitem(
        sys.modules,
        "quick_search",
        types.SimpleNamespace(run_quick_search_for_chat=_noop_quick_search),
    )
    sys.modules.pop("agents.chat", None)
    return importlib.import_module("agents.chat")


def test_chat_repeated_blocked_tool_state_stops_on_second_duplicate(monkeypatch):
    chat = _import_chat_with_optional_stubs(monkeypatch)
    state = {"key": "", "count": 0}
    blocked = (
        "BLOCKED - reviewer (run-deadbeef1234) returned status='issues'.\n\n"
        "Your VERY NEXT tool call MUST be:\n  run_aider_fix(issue_run_id='run-deadbeef1234')"
    )

    assert chat._record_blocked_tool_result(state, "read_file", blocked) is False
    assert chat._record_blocked_tool_result(state, "read_file", blocked) is True
    assert state["count"] == 2
    assert chat._next_action_from_blocked_result(blocked).startswith("run_aider_fix")
    assert chat._record_blocked_tool_result(state, "read_file", "OK") is False
    # A successful result no longer resets the counter — an OK sibling tool in
    # the same batch must not defeat the duplicate-BLOCKED detection.
    assert state["count"] == 2
    sys.modules.pop("agents.chat", None)


def test_chat_budget_exhausted_next_action_asks_user(monkeypatch):
    chat = _import_chat_with_optional_stubs(monkeypatch)
    blocked = (
        "BLOCKED - acceptance still has issues after deep_research and the full "
        "automated repair budget is exhausted.\n\n"
        "Ask whether to ship as-is or authorize manual intervention."
    )

    action = chat._next_action_from_blocked_result(blocked)

    assert "ask whether to ship as-is" in action
    assert "write_file" not in action
    sys.modules.pop("agents.chat", None)


def test_workflow_crud_round_trip(tmp_path):
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import database as db

    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())

    workflow_id = "cw-test"
    _run(db.create_conversation("conv-test", "Workflow Test"))
    _run(db.create_coder_workflow(
        workflow_id,
        "conv-test",
        project_id="proj-test",
        mode="fix_uploaded_project",
        state="fixing",
        user_task="fix broken CLI",
        contract={"language": "python", "test_cmd": "python -m pytest -q"},
        active_run_id="run-test",
    ))

    wf = _run(db.get_coder_workflow(workflow_id))
    assert wf["mode"] == "fix_uploaded_project"
    assert wf["contract_json"]["test_cmd"] == "python -m pytest -q"
    assert wf["cancel_requested"] is False

    _run(db.update_coder_workflow(
        workflow_id,
        state="cancelled",
        artifact_status="cancelled",
        cancel_requested=True,
    ))
    wf = _run(db.get_latest_coder_workflow("conv-test", "proj-test"))
    assert wf["state"] == "cancelled"
    assert wf["artifact_status"] == "cancelled"
    assert wf["cancel_requested"] is True


def test_project_archive_excludes_aider_runtime_files(tmp_path):
    import tools

    project = tmp_path / "project"
    paths = {
        "main.py": "print('ok')\n",
        "README.md": "# Demo\n",
        "requirements.txt": "pygame-ce>=2.5.0\n",
        ".gitignore": ".aider*\n",
        ".aider.chat.history.md": "private repair transcript\n",
        ".aider.tags.cache.v4/cache.db": "sqlite bytes",
        "nested/.aider.local.md": "nested aider note\n",
        "__pycache__/main.cpython-313.pyc": "bytecode",
        ".git/config": "[core]\n",
    }
    for rel, content in paths.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    qproject = shlex.quote(str(project))
    included = subprocess.check_output(
        f"cd {qproject} && find . -type f {tools._project_archive_find_include_filter()} | sort",
        shell=True,
        text=True,
    ).splitlines()
    excluded = subprocess.check_output(
        f"cd {qproject} && find . \\( {tools._project_archive_find_exclude_expr()} \\) -print | sort",
        shell=True,
        text=True,
    ).splitlines()

    assert "./main.py" in included
    assert "./README.md" in included
    assert "./requirements.txt" in included
    assert "./.gitignore" in included
    assert not any(".aider" in item for item in included)
    assert "./.aider.chat.history.md" in excluded
    assert "./.aider.tags.cache.v4/cache.db" in excluded
    assert "./nested/.aider.local.md" in excluded
    assert "./__pycache__/main.cpython-313.pyc" in excluded
    assert "./.git/config" in excluded

    archive = tmp_path / "project.tar.gz"
    subprocess.run(
        f"cd {qproject} && tar czf {shlex.quote(str(archive))} "
        f"{tools._project_archive_tar_exclude_args()} .",
        shell=True,
        check=True,
        text=True,
    )
    with tarfile.open(archive, "r:gz") as tf:
        names = set(tf.getnames())

    assert "./main.py" in names
    assert "./README.md" in names
    assert "./requirements.txt" in names
    assert "./.gitignore" in names
    assert not any(".aider" in name for name in names)
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
    assert not any(name == "./.git" or name.startswith("./.git/") for name in names)


# ---------------------------------------------------------------------------
# Lifecycle hardening — stale-run reaper + Aider finalize/fallback semantics
# ---------------------------------------------------------------------------

import json as _json


def test_reap_stale_runs_marks_orphans_failed(tmp_path):
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import database as db

    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_conversation("conv-reap", "Reaper Test"))
    _run(db.create_run("run-stale", "conv-reap", role="aider.fix", status="running"))
    _run(db.create_run("run-queued", "conv-reap", role="fixer", status="queued"))
    _run(db.create_run("run-done", "conv-reap", role="reviewer", status="succeeded"))
    _run(db.create_coder_workflow(
        "cw-stale", "conv-reap", project_id="proj-r1",
        mode="fix_uploaded_project", state="fixing",
        user_task="x", active_run_id="run-stale",
    ))
    _run(db.create_coder_workflow(
        "cw-fine", "conv-reap", project_id="proj-r2",
        mode="fix_uploaded_project", state="fixing",
        user_task="x", active_run_id="run-done",
    ))

    stats = _run(db.reap_stale_runs())

    assert stats["runs_reaped"] == 2
    assert stats["workflows_blocked"] == 1
    assert _run(db.get_run("run-stale"))["status"] == "failed"
    assert _run(db.get_run("run-queued"))["status"] == "failed"
    assert _run(db.get_run("run-done"))["status"] == "succeeded"
    env = _run(db.get_run("run-stale"))["result_envelope"]
    assert env["summary"] == "orphaned by backend restart"
    assert _run(db.get_coder_workflow("cw-stale"))["state"] == "blocked"
    assert _run(db.get_coder_workflow("cw-fine"))["state"] == "fixing"


class _NullEvents:
    async def emit(self, *a, **k):
        pass


_README_ISSUE_ENVELOPE = {
    "status": "issues",
    "issues": [{"file": "README.md", "summary": "README claims NumPy is used",
                "suggested_fix_scope": ["README.md"]}],
}


# ---------------------------------------------------------------------------
# Fixer parser / status, node adapter, synthesizer gating
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Path plumbing: dotfile preservation + indexer file prioritization
# ---------------------------------------------------------------------------

class _CmdCaptureHTTP:
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.commands = []

    async def post(self, url, json=None, timeout=None):
        self.commands.append((json or {}).get("command", ""))

        class _R:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        return _R({"exit_code": 0, "stdout": self.stdout, "stderr": ""})


def test_v2_name_match_word_boundary():
    import tools
    assert tools._v2_name_match("🏛️ Daedalus")
    assert tools._v2_name_match("Coder Bot v2")
    assert tools._v2_name_match("V2 Builder")
    assert not tools._v2_name_match("v2ray helper")
    assert not tools._v2_name_match("levi2000")
    assert not tools._v2_name_match("")


def test_event_bus_unsubscribe_deletes_empty_conversation_key():
    from events import EventBus

    bus = EventBus()
    q = _run(bus.subscribe("conv-events"))

    assert "conv-events" in bus._subscribers

    _run(bus.unsubscribe("conv-events", q))

    assert "conv-events" not in bus._subscribers


def test_seed_coder_kb_targets_daedalus_not_generic_coder_profile():
    if not HAS_CHROMADB:
        install_rag_stub()
    from seed_kb import seed_coder_kb

    picked = seed_coder_kb._find_daedalus_config([
        {"id": "mc-coder", "name": "Personal Coder Helper", "kb_ids": []},
        {"id": "mc-daedalus", "name": "🏛️ Daedalus", "kb_ids": []},
    ])

    assert picked["id"] == "mc-daedalus"
    assert seed_coder_kb._find_daedalus_config([
        {"id": "mc-coder", "name": "Personal Coder Helper", "kb_ids": []},
    ]) is None


def test_blocked_counter_ignores_tool_name_and_interleaved_success(monkeypatch):
    chat = _import_chat_with_optional_stubs(monkeypatch)
    state = {"key": "", "count": 0}
    blocked = "BLOCKED — reviewer (run-aabbccdd1122) returned issues. Call run_fixer."

    assert chat._record_blocked_tool_result(state, "read_file", blocked) is False
    # An interleaved successful sibling result must not reset the counter.
    assert chat._record_blocked_tool_result(state, "search_files", "ok result") is False
    # A different tool hitting the same blocking trigger trips the stop.
    assert chat._record_blocked_tool_result(state, "list_files", blocked) is True
    sys.modules.pop("agents.chat", None)


def test_reject_cloud_filters_only_cloud_ids():
    import model_providers as mp
    assert mp.reject_cloud("qwen3.5:27b") == "qwen3.5:27b"
    assert mp.reject_cloud("hf.co/foo/bar:Q4") == "hf.co/foo/bar:Q4"
    assert mp.reject_cloud("anthropic:claude-x") == ""
    assert mp.reject_cloud("openai:gpt-5") == ""
    assert mp.reject_cloud("") == ""


def test_complete_chat_ollama_path_streams_and_reads_thinking_fallback():
    import json as _json
    import model_providers as mp

    class _SResp:
        status_code = 200

        async def aiter_lines(self):
            yield _json.dumps({"message": {"content": "", "thinking": "the "}, "done": False})
            yield _json.dumps({"message": {"content": "", "thinking": "answer"}, "done": True})

    class _Ctx:
        async def __aenter__(self):
            return _SResp()

        async def __aexit__(self, *a):
            return False

    class _HTTP:
        def __init__(self):
            self.calls = []

        def stream(self, method, url, json=None, timeout=None):
            self.calls.append((url, json))
            return _Ctx()

    http = _HTTP()
    out = _run(mp.complete_chat(http, "qwen3.5:27b", "hi",
                                num_ctx=4096, num_predict=2048,
                                ollama_url="http://ollama"))

    assert out == "the answer"
    url, payload = http.calls[0]
    assert url == "http://ollama/api/chat"
    assert payload["stream"] is True
    assert payload["think"] is False
    assert payload["options"]["num_ctx"] == 4096
    assert payload["options"]["num_predict"] == 2048


def test_run_events_table_append_and_hydrate(tmp_path):
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import database as db
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_conversation("conv-ev", "Events Test"))
    _run(db.create_run("run-ev", "conv-ev", role="reviewer", status="running"))

    for i in range(25):
        _run(db.append_run_event("run-ev", {"type": "step", "action": f"step{i}", "detail": str(i)}))

    run = _run(db.get_run("run-ev"))
    evs = run["events_log"]
    assert len(evs) == 25
    assert [e["action"] for e in evs] == [f"step{i}" for i in range(25)]  # ordered by seq
    assert all("ts" in e for e in evs)

    # List getter stays lean — does NOT per-row hydrate the events table.
    listed = _run(db.get_runs_by_conversation("conv-ev"))
    assert listed[0]["id"] == "run-ev"
    assert listed[0]["events_log"] == []  # legacy column empty; no N+1 query


def test_run_events_legacy_fallback(tmp_path):
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import json as _json
    import database as db
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_conversation("conv-leg", "Legacy Test"))
    _run(db.create_run("run-leg", "conv-leg", role="reviewer", status="running"))
    # Simulate a pre-migration row: events only in the legacy JSON column.
    async def _seed_legacy():
        conn = await db.get_db()
        try:
            await conn.execute(
                "UPDATE runs SET events_log=? WHERE id=?",
                (_json.dumps([{"type": "step", "action": "old"}]), "run-leg"),
            )
            await conn.commit()
        finally:
            await conn.close()
    _run(_seed_legacy())

    run = _run(db.get_run("run-leg"))
    assert [e["action"] for e in run["events_log"]] == ["old"]


def test_complete_chat_cloud_error_returns_empty(monkeypatch):
    import model_providers as mp

    async def _boom(*a, **k):
        raise mp.ProviderError("simulated provider 500", provider="anthropic")
        yield  # make it an async generator

    monkeypatch.setattr(mp, "stream_provider_chat", _boom)
    out = _run(mp.complete_chat(object(), "anthropic:claude-x", "hi", format_json=True))
    assert out == ""  # unified with the Ollama branch's "" on failure


def test_complete_chat_cloud_format_json_nudges_prompt(monkeypatch):
    import model_providers as mp
    seen = {}

    async def _capture(http, model_id, messages, options=None):
        seen["prompt"] = messages[0]["content"]
        for tok in ('{"ok":', ' true}'):
            yield {"type": "token", "content": tok}

    monkeypatch.setattr(mp, "stream_provider_chat", _capture)
    out = _run(mp.complete_chat(object(), "openai:gpt-x", "plan it", format_json=True))
    assert out == '{"ok": true}'
    assert "ONLY valid JSON" in seen["prompt"]


class _AnyOkHTTP:
    """Answers any codebox/ollama call with an empty success."""
    class _Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"exit_code": 0, "stdout": "", "stderr": ""}

    async def post(self, *a, **k):
        return self._Resp()

    async def get(self, *a, **k):
        return self._Resp()


def test_csharp_adapter_contract():
    from agents.language_adapters import detect_adapter

    a = detect_adapter(["App.csproj", "Program.cs",
                        "Tests/AppTests.csproj", "Tests/ProgramTests.cs"])
    assert a.language == "csharp"
    assert a.build_system == "dotnet"
    assert "dotnet build" in a.build_cmd and "/root/.dotnet" in a.build_cmd
    assert "dotnet test" in a.test_cmd  # test project present

    b = detect_adapter(["App.csproj", "Program.cs"])
    assert b.test_cmd == ""  # no test project → no dotnet test

    # Makefile-only C project routes to the C adapter, not generic.
    c = detect_adapter(["Makefile", "main.c", "util.h"])
    assert c.language == "c"
    assert c.build_system == "make"
    assert not c.safe_lint  # make mutates the tree — never a safe Aider lint


# ─── Hermetic worker environment ────────────────────────────────────────────
