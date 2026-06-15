import asyncio
import importlib
import sys
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

from agents import aider_fixer, language_adapters, reviewer
from tools import CODEAGENT_TOOLS


def _run(coro):
    return asyncio.run(coro)


def test_python_adapter_catches_cli_package_contract():
    manifest = [
        "pyproject.toml",
        "taskforge/__init__.py",
        "taskforge/__main__.py",
        "taskforge/cli.py",
        "tests/test_cli.py",
    ]

    contract = language_adapters.detect_contract(manifest, "python")

    assert contract["language"] == "python"
    assert contract["build_system"] == "pyproject.toml"
    assert "/root/venv/bin/python3 -m pip install -e ." in contract["build_cmd"]
    assert contract["test_cmd"] == "/root/venv/bin/python3 -m pytest -q"
    assert contract["aider_test_cmd"] == "/root/venv/bin/python3 -m pytest -q"
    assert "/root/venv/bin/python3 -m taskforge --help" in contract["smoke_cmds"]
    assert contract["safe_lint"] is True
    assert "__main__.py" in " ".join(contract["package_rules"])


def test_aider_scope_expands_cli_import_mismatch():
    envelope = {
        "summary": (
            "The CLI tests fail because the main entry point attempts to import "
            "list_tasks from commands.py, but the actual function is named "
            "list_tasks_command."
        ),
        "issues": [{
            "file": "taskforge/__main__.py",
            "summary": "Entry point ImportError from commands.py.",
            "suggested_fix_scope": ["taskforge/__main__.py"],
        }],
    }

    files = aider_fixer._allowed_files_from_issues(envelope)

    assert "taskforge/__main__.py" in files
    assert "commands.py" in files
    assert "cli.py" in files


def test_aider_task_scope_keeps_initial_import_fix_surgical():
    task = (
        "Problem: ImportError - the entry point imports listtasks from "
        "taskforge.commands, but the actual function is named listtasks_command. "
        "Fix it please"
    )

    files = aider_fixer._allowed_files_from_task(task)

    assert "taskforge/commands.py" in files
    assert "__main__.py" in files
    assert "cli.py" in files
    assert "commands.py" in files
    assert "db.py" not in files


def test_reviewer_detects_stale_uploaded_project_root():
    failure = """
FAILED tests/test_cli.py::test_help - AssertionError: CompletedProcess(args=['python', '-m', 'taskforge'],
cwd="/root/projects/taskforge-5ddd", returncode=1, stderr="ImportError: cannot import name 'list_tasks'")
"""
    grep_hits = [{
        "file": "tests/test_cli.py",
        "line": 18,
        "text": "subprocess.run(cmd, cwd='/root/projects/taskforge-5ddd')",
        "stale_paths": ["/root/projects/taskforge-5ddd"],
    }]

    parsed = reviewer._stale_path_issue_from_failure(
        failure,
        "/root/projects/proj-abc",
        grep_hits,
    )

    assert parsed is not None
    assert parsed["status"] == "issues"
    assert parsed["stale_project_paths"] == ["/root/projects/taskforge-5ddd"]
    assert parsed["expected_project_dir"] == "/root/projects/proj-abc"
    issue = parsed["issues"][0]
    assert issue["file"] == "tests/test_cli.py"
    assert issue["lines"] == [18]
    assert issue["suggested_fix_scope"][0] == "tests/test_cli.py"
    assert "taskforge/__main__.py" not in issue["suggested_fix_scope"]


def test_reviewer_grep_parser_ignores_active_project_root():
    out = "\n".join([
        "./tests/test_cli.py:7:ROOT = '/root/projects/taskforge-5ddd'",
        "./tests/test_cli.py:8:ACTIVE = '/root/projects/proj-abc'",
    ])

    hits = reviewer._parse_project_path_grep(out, "/root/projects/proj-abc")

    assert len(hits) == 1
    assert hits[0]["file"] == "tests/test_cli.py"
    assert hits[0]["stale_paths"] == ["/root/projects/taskforge-5ddd"]


def test_reviewer_detects_persistent_state_schema_issue():
    failure = """
FAILED tests/test_cli.py::test_cli_stats - AssertionError
E where 1 = CompletedProcess(args=['python', '-m', 'taskforge', 'stats'],
stderr='Error: no such column: status\\n').returncode
FAILED tests/test_cli.py::test_cli_export - AssertionError
E where 1 = CompletedProcess(args=['python', '-m', 'taskforge', 'export'],
stderr='Error: No item with that key\\n').returncode
"""
    project_files = ["taskforge/cli.py", "taskforge/db.py", "tests/test_cli.py"]
    refs = [{"file": "tests/test_cli.py", "line": 96, "hits": 2}]

    parsed = reviewer._state_isolation_issue_from_failure(
        failure,
        "/root/projects/proj-abc",
        project_files,
        refs,
    )

    assert parsed is not None
    assert parsed["deterministic_issue"] == "persistent_test_state"
    issue = parsed["issues"][0]
    assert issue["file"] == "taskforge/db.py"
    assert "taskforge/db.py" in issue["suggested_fix_scope"]
    assert "tests/test_cli.py" in issue["suggested_fix_scope"]
    assert issue["test_isolation_suspected"] is True


def test_reviewer_state_paths_ignore_interpreter_paths():
    failure = """
FAILED tests/test_cli.py::test_cli_stats - AssertionError
E where 1 = CompletedProcess(args=['/root/venv/bin/python3', '-m', 'taskforge', 'stats'],
stderr='Error: no such column: status\\n').returncode
"""

    paths = reviewer._extract_state_paths(failure)

    assert "/root/venv/bin/python3" not in paths


def test_reviewer_sanitizes_nonexistent_storage_guess_to_real_db_file():
    parsed = {
        "status": "issues",
        "summary": "Three CLI tests fail due to database schema mismatches.",
        "issues": [{
            "severity": "test",
            "file": "taskforge/storage.py",
            "lines": [],
            "summary": "The stats command fails with no such column: status.",
            "suggested_fix_scope": ["taskforge/storage.py", "taskforge/cli.py"],
        }],
    }
    failure = "Error: no such column: status\nError: No item with that key\n"
    project_files = ["taskforge/cli.py", "taskforge/db.py", "tests/test_cli.py"]
    refs = [{"file": "tests/test_cli.py", "line": 96, "hits": 2}]

    sanitized = reviewer._sanitize_review_envelope(parsed, failure, project_files, refs)
    issue = sanitized["issues"][0]

    assert issue["file"] == "taskforge/db.py"
    assert issue["normalized_from_file"] == "taskforge/storage.py"
    assert "taskforge/storage.py" not in issue["suggested_fix_scope"]
    assert "taskforge/db.py" in issue["suggested_fix_scope"]
    assert "tests/test_cli.py" in issue["suggested_fix_scope"]


def _import_openhands_worker_for_prompt_tests(monkeypatch):
    if not HAS_FASTAPI:
        class DummyFastAPI:
            def get(self, *args, **kwargs):
                return lambda fn: fn

            def post(self, *args, **kwargs):
                return lambda fn: fn

        monkeypatch.setitem(sys.modules, "fastapi", module_stub("fastapi", FastAPI=lambda *a, **k: DummyFastAPI()))
        monkeypatch.setitem(sys.modules, "fastapi.responses", module_stub("fastapi.responses", StreamingResponse=object))
    if not HAS_PYDANTIC:
        class DummyBaseModel:
            def __init__(self, **kwargs):
                annotations = getattr(self.__class__, "__annotations__", {})
                for key in annotations:
                    if hasattr(self.__class__, key):
                        val = getattr(self.__class__, key)
                        if isinstance(val, (list, dict)):
                            val = val.copy()
                        setattr(self, key, val)
                for key, val in kwargs.items():
                    setattr(self, key, val)

        monkeypatch.setitem(sys.modules, "pydantic", module_stub("pydantic", BaseModel=DummyBaseModel))
    sys.modules.pop("openhands_worker", None)
    return importlib.import_module("openhands_worker")


def test_aider_prompt_includes_known_test_root(tmp_path, monkeypatch):
    worker = _import_openhands_worker_for_prompt_tests(monkeypatch)
    req = worker.AiderRunRequest(
        project_dir="/root/projects/proj-abc",
        task="Fix the import mismatch",
        test_cmd="/root/venv/bin/python3 -m pytest -q",
        allowed_files=["tests/test_cli.py", "taskforge/__main__.py"],
        issue_envelope={
            "stale_project_paths": ["/root/projects/taskforge-5ddd"],
            "test_stdout_tail": "cwd=\"/root/projects/taskforge-5ddd\"\nImportError: cannot import name",
            "issues": [{
                "file": "tests/test_cli.py",
                "summary": "Hardcoded stale project path.",
                "suggested_fix_scope": ["tests/test_cli.py"],
            }],
        },
    )

    prompt = worker._write_aider_prompt(req, tmp_path).read_text()

    assert "## Known Test Root" in prompt
    assert "Active project root: `/root/projects/proj-abc`" in prompt
    assert "/root/projects/taskforge-5ddd" in prompt
    assert "cwd=\"/root/projects/taskforge-5ddd\"" in prompt
    assert "make the tests derive paths from the current" in prompt
    sys.modules.pop("openhands_worker", None)


def test_aider_scope_and_prompt_include_test_state_isolation(tmp_path, monkeypatch):
    envelope = {
        "summary": "Tests fail with persistent storage/schema state errors.",
        "state_error_signals": ["no such column", "No item with that key"],
        "issues": [{
            "file": "taskforge/db.py",
            "summary": "Make storage path configurable for tests.",
            "suggested_fix_scope": ["taskforge/db.py", "tests/test_cli.py"],
            "state_error_signals": ["no such column: status"],
            "test_isolation_suspected": True,
        }],
        "test_stdout_tail": "Error: no such column: status\nError: No item with that key",
    }

    files = aider_fixer._allowed_files_from_issues(envelope)
    assert "taskforge/db.py" in files
    assert "tests/test_cli.py" in files
    assert "db.py" in files
    assert "test_cli.py" in files

    worker = _import_openhands_worker_for_prompt_tests(monkeypatch)
    req = worker.AiderRunRequest(
        project_dir="/root/projects/proj-abc",
        task="Fix database schema test failures",
        test_cmd="/root/venv/bin/python3 -m pytest -q",
        allowed_files=["taskforge/db.py", "tests/test_cli.py"],
        issue_envelope=envelope,
    )

    prompt = worker._write_aider_prompt(req, tmp_path).read_text()

    assert "## Test State Isolation" in prompt
    assert "fresh per-test state path" in prompt
    assert "schema creation/migration idempotent" in prompt
    assert "Do not rename existing command/business functions" in prompt
    sys.modules.pop("openhands_worker", None)


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


def test_chat_uploaded_project_manual_duplicate_summary(monkeypatch):
    chat = _import_chat_with_optional_stubs(monkeypatch)

    async def get_latest_coder_workflow(_conv_id, project_id=None):
        return {
            "id": "cw-uploaded",
            "mode": "fix_uploaded_project",
            "state": "fixing",
            "project_id": "proj-abc",
            "active_run_id": "run-aider",
        }

    async def get_runs_by_conversation(_conv_id, limit=20):
        return [
            {
                "id": "run-aider",
                "role": "aider.fix",
                "result_envelope": {"status": "error"},
            },
            {
                "id": "run-review",
                "role": "reviewer",
                "result_envelope": {
                    "status": "issues",
                    "summary": "Tests still fail after Aider edits.",
                    "issues": [{
                        "severity": "test",
                        "file": "tests/test_cli.py",
                        "summary": "The CLI subprocess path is still wrong.",
                    }],
                },
            },
        ]

    chat.db = types.SimpleNamespace(
        get_latest_coder_workflow=get_latest_coder_workflow,
        get_runs_by_conversation=get_runs_by_conversation,
    )

    assert _run(chat._is_active_uploaded_manual_loop("conv-uploaded", ["read_file"])) is True
    assert _run(chat._is_active_uploaded_manual_loop("conv-uploaded", ["run_review"])) is False

    summary = _run(chat._duplicate_manual_tool_summary("conv-uploaded", ["read_file"]))

    assert "duplicate detector rejected" in summary
    assert "Active project path: `/root/projects/proj-abc`" in summary
    assert "Latest reviewer summary: Tests still fail after Aider edits." in summary
    assert 'Next valid action: `run_review(project_id="proj-abc")`' in summary
    sys.modules.pop("agents.chat", None)


def test_mark_latest_uploaded_workflow_blocked(tmp_path, monkeypatch):
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import database as db
    chat = _import_chat_with_optional_stubs(monkeypatch)

    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_conversation("conv-blocked", "Blocked Loop Test"))
    _run(db.create_coder_workflow(
        "cw-blocked",
        "conv-blocked",
        project_id="proj-blocked",
        mode="fix_uploaded_project",
        state="fixing",
        user_task="fix broken CLI",
        contract={"language": "python"},
        active_run_id="run-aider",
    ))

    _run(chat._mark_latest_uploaded_workflow_blocked("conv-blocked"))

    wf = _run(db.get_coder_workflow("cw-blocked"))
    assert wf["state"] == "blocked"
    assert wf["active_run_id"] == "run-aider"
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


def test_uploaded_workflow_resolves_aider_context(tmp_path):
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import database as db
    import tools

    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())

    _run(db.create_conversation("conv-upload", "Upload Fix Test"))
    _run(db.create_coder_workflow(
        "cw-upload",
        "conv-upload",
        project_id="proj-upload",
        mode="fix_uploaded_project",
        state="fixing",
        user_task="uploaded project",
        contract={"language": "python"},
    ))

    issue_run = {
        "id": "run-review",
        "role": "reviewer",
        "project_id": "",
        "result_envelope": {
            "status": "issues",
            "project_dir": "/root/projects/proj-upload",
        },
    }
    ctx = _run(tools._uploaded_project_aider_context("conv-upload", issue_run=issue_run))

    assert ctx is not None
    assert ctx["project_id"] == "proj-upload"
    assert ctx["project_dir"] == "/root/projects/proj-upload"


def test_uploaded_project_bootstrap_gate_blocks_manual_tools():
    import tools

    workflow = {
        "id": "cw-upload",
        "mode": "fix_uploaded_project",
        "state": "fixing",
        "project_id": "proj-upload",
        "user_task": "Fix the import mismatch",
        "artifact_status": "not_ready",
        "cancel_requested": False,
    }

    assert tools._uploaded_project_manual_gate_state(workflow, []) == "bootstrap"
    assert tools._uploaded_project_tool_allowed_during_bootstrap("run_aider_fix") is True
    assert tools._uploaded_project_tool_allowed_during_bootstrap("read_file") is False

    body = tools._uploaded_project_bootstrap_block_message(
        "read_file",
        workflow,
        "/root/projects/proj-upload",
        "Fix the import mismatch",
    )

    assert "uploaded-project fixes must start with Aider" in body
    assert "run_aider_fix(project_dir='/root/projects/proj-upload'" in body
    assert "Do NOT call read_file" in body


def test_uploaded_project_bootstrap_gate_releases_after_agent_run():
    import tools

    workflow = {
        "mode": "fix_uploaded_project",
        "state": "reviewing",
        "project_id": "proj-upload",
        "artifact_status": "not_ready",
        "cancel_requested": False,
    }
    runs = [{"id": "run-aider", "role": "aider.fix", "status": "succeeded"}]

    assert tools._uploaded_project_manual_gate_state(workflow, runs) == ""


def test_new_coder_tools_are_registered():
    for name in (
        "start_coder_workflow",
        "run_aider_fix",
        "get_coder_workflow",
        "cancel_coder_workflow",
    ):
        assert name in CODEAGENT_TOOLS

    assert "uploaded-project fixes" in CODEAGENT_TOOLS["run_aider_fix"]["function"]["description"]
    assert "project_id" in CODEAGENT_TOOLS["run_aider_fix"]["function"]["parameters"]["properties"]


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


class _AiderResp:
    def __init__(self, code, payload):
        self.status_code = code
        self._payload = payload
        self.text = _json.dumps(payload)

    def json(self):
        return self._payload


class _AiderFakeHTTP:
    """Injected client covering /aider/health, /aider/cancel, and the blocking
    /aider/run fallback. The SSE stream path uses aider_fixer's own
    httpx.AsyncClient, which each test controls separately."""

    def __init__(self, run_result, on_run=None):
        self.run_result = run_result
        self.on_run = on_run
        self.posts = []

    async def get(self, url, **kw):
        return _AiderResp(200, {"installed": True})

    async def post(self, url, **kw):
        self.posts.append(url)
        if url.endswith("/aider/run"):
            if self.on_run is not None:
                await self.on_run()
            return _AiderResp(200, self.run_result)
        return _AiderResp(200, {})


class _NullEvents:
    async def emit(self, *a, **k):
        pass


def _prep_aider_db(tmp_path, db):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_conversation("conv-aider", "Aider Test"))
    _run(db.create_coder_workflow(
        "cw-aider", "conv-aider", project_id="proj-a",
        mode="fix_uploaded_project", state="fixing", user_task="fix it",
    ))


def test_aider_finalize_maps_cancelled_state(tmp_path, monkeypatch):
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import config
    import database as db
    _prep_aider_db(tmp_path, db)
    # Stream endpoint unreachable (closed port) → step_n stays 0 → blocking
    # fallback runs through the fake client below.
    monkeypatch.setattr(config, "AIDER_WORKER_URL", "http://127.0.0.1:9")
    fake = _AiderFakeHTTP({"status": "cancelled", "summary": "stopped", "files_touched": []})

    env = _run(aider_fixer.run_aider_fix(
        fake, _NullEvents(), "conv-aider",
        project_dir="/root/projects/proj-a", task="fix it",
        project_id="proj-a", workflow_id="cw-aider",
    ))

    assert env["status"] == "cancelled"
    wf = _run(db.get_coder_workflow("cw-aider"))
    assert wf["state"] == "cancelled"
    assert wf["artifact_status"] == "cancelled"


def test_aider_finalize_respects_cancelled_workflow(tmp_path, monkeypatch):
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import config
    import database as db
    _prep_aider_db(tmp_path, db)
    monkeypatch.setattr(config, "AIDER_WORKER_URL", "http://127.0.0.1:9")

    async def _cancel_route_fires_mid_run():
        await db.update_coder_workflow(
            "cw-aider", state="cancelled",
            artifact_status="cancelled", cancel_requested=True,
        )

    fake = _AiderFakeHTTP(
        {"status": "error", "summary": "boom", "files_touched": []},
        on_run=_cancel_route_fires_mid_run,
    )

    env = _run(aider_fixer.run_aider_fix(
        fake, _NullEvents(), "conv-aider",
        project_dir="/root/projects/proj-a", task="fix it",
        project_id="proj-a", workflow_id="cw-aider",
    ))

    assert env["status"] == "error"
    wf = _run(db.get_coder_workflow("cw-aider"))
    # A failed run must NOT overwrite the user's cancel with 'blocked'.
    assert wf["state"] == "cancelled"


def test_aider_no_fallback_after_partial_stream(tmp_path, monkeypatch):
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import database as db
    _prep_aider_db(tmp_path, db)

    class _StreamResp:
        status_code = 200

        async def aiter_lines(self):
            yield 'data: {"type": "step", "action": "edit", "detail": "x"}'
            raise ConnectionError("link dropped")

        async def aread(self):
            return b""

    class _StreamCtx:
        async def __aenter__(self):
            return _StreamResp()

        async def __aexit__(self, *a):
            return False

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **k):
            return _StreamCtx()

    monkeypatch.setattr(aider_fixer.httpx, "AsyncClient", _FakeAsyncClient)
    fake = _AiderFakeHTTP({"status": "ok", "summary": "should not run", "files_touched": []})

    env = _run(aider_fixer.run_aider_fix(
        fake, _NullEvents(), "conv-aider",
        project_dir="/root/projects/proj-a", task="fix it",
        project_id="proj-a", workflow_id="cw-aider",
    ))

    assert env["status"] == "error"
    assert "not restarting" in env["summary"]
    assert not any(u.endswith("/aider/run") for u in fake.posts)
    assert any("/aider/cancel/" in u for u in fake.posts)


# ---------------------------------------------------------------------------
# Fixer parser / status, node adapter, synthesizer gating
# ---------------------------------------------------------------------------

def test_fixer_edit_parser_survives_inner_fences():
    from agents import fixer
    inner = "# Readme\n\nUsage:\n\n```python\nprint('hi')\n```\n\nDone."
    text = (
        f"### EDIT: /root/projects/p/README.md\n```markdown\n{inner}\n```\n\n"
        f"### EDIT: /root/projects/p/app.py\n```python\nprint('app')\n```\n\n"
        f"### SUMMARY: updated docs\n"
    )

    parsed = fixer._parse_fixer_output(text)

    assert parsed["parse_errors"] == []
    assert len(parsed["edits"]) == 2
    assert parsed["edits"][0]["path"] == "/root/projects/p/README.md"
    assert parsed["edits"][0]["content"] == inner
    assert parsed["edits"][1]["content"] == "print('app')"
    assert parsed["summary"] == "updated docs"


def test_fixer_edit_parser_skips_unterminated_fence():
    from agents import fixer
    text = "### EDIT: /root/projects/p/a.py\n```python\nprint('truncated"

    parsed = fixer._parse_fixer_output(text)

    assert parsed["edits"] == []
    assert any("unterminated" in e for e in parsed["parse_errors"])


def test_node_fallback_build_cmd_propagates_failure():
    contract = language_adapters.detect_contract(["script.js"], "javascript")
    assert "fail=1" in contract["build_cmd"]
    assert "exit $fail" in contract["build_cmd"]
    assert "-exec node --check {}" not in contract["build_cmd"]


def _seed_reviewer_run(db, conv_id, run_id, envelope):
    _run(db.create_run(run_id, conv_id, role="reviewer", status="running"))
    _run(db.update_run(run_id, status="succeeded", result_envelope=envelope, ended=True))


def test_synthesizer_skips_when_reviewer_reported_issues(tmp_path):
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import database as db
    import tools

    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_conversation("conv-synth", "Synth Test"))
    _seed_reviewer_run(db, "conv-synth", "run-rev-issues", {
        "status": "issues", "test_exit": 0,
        "summary": "lint issues", "issues": [{"file": "a.py", "summary": "x"}],
    })

    rid = _run(tools._synthesize_reviewer_from_test_failure(
        "conv-synth", "FAILED tests/test_a.py::test_x - AssertionError: boom",
        "/root/projects/p", "pytest", 1.0,
    ))

    # Reviewer already reported issues — must NOT stack a synthetic envelope.
    assert rid == ""


def test_synthesizer_fires_for_clean_reviewer(tmp_path):
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import database as db
    import tools

    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_conversation("conv-synth2", "Synth Test 2"))
    _seed_reviewer_run(db, "conv-synth2", "run-rev-clean", {
        "status": "clean", "test_exit": 0, "summary": "all good", "issues": [],
    })

    rid = _run(tools._synthesize_reviewer_from_test_failure(
        "conv-synth2", "FAILED tests/test_a.py::test_x - AssertionError: boom",
        "/root/projects/p", "pytest", 1.0,
    ))

    assert rid
    env = _run(db.get_run(rid))["result_envelope"]
    assert env["status"] == "issues"
    assert env.get("synthetic") is True


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


def test_indexer_path_join_preserves_dotfiles():
    from agents import project_indexer
    http = _CmdCaptureHTTP(stdout="X=1")

    _run(project_indexer._read_file(http, "/root/projects/p", "./.env.example"))

    assert any("/root/projects/p/.env.example" in c for c in http.commands), http.commands


def test_qa_path_join_preserves_dotfiles():
    from agents import project_qa
    http = _CmdCaptureHTTP(stdout="X=1")

    _run(project_qa._read_file_full(http, "/root/projects/p", "./.env.example"))

    assert any("/root/projects/p/.env.example" in c for c in http.commands), http.commands


def test_indexer_prioritizes_entrypoints_over_small_files():
    from agents import project_indexer
    lines = ["20 ./tiny1.cfg", "30 ./tiny2.cfg", "120000 ./src/core/engine.py",
             "500 ./src/main.py", "90000 ./src/big_module.py"]
    http = _CmdCaptureHTTP(stdout="\n".join(lines))

    paths = _run(project_indexer._list_source_files(http, "/root/projects/p", cap=3))

    # Entrypoint first despite being small; then largest files. The old
    # `sort -n | head` would have returned the three smallest files.
    assert paths == ["./src/main.py", "./src/core/engine.py", "./src/big_module.py"]


def test_v2_name_match_word_boundary():
    import tools
    assert tools._v2_name_match("🏛️ Daedalus")
    assert tools._v2_name_match("Coder Bot v2")
    assert tools._v2_name_match("V2 Builder")
    assert not tools._v2_name_match("v2ray helper")
    assert not tools._v2_name_match("levi2000")
    assert not tools._v2_name_match("")


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


def test_qa_short_circuit_skips_stale_run(monkeypatch):
    chat = _import_chat_with_optional_stubs(monkeypatch)
    stale = {
        "role": "qa", "status": "succeeded", "started_at": "2026-01-01T00:00:00",
        "result_envelope": {"answer": "old answer", "looks_like_change_request": False},
    }
    fresh = dict(stale, started_at="2026-01-01T00:10:00")

    assert chat._qa_run_qualifies(stale, "2026-01-01T00:05:00") is False
    assert chat._qa_run_qualifies(fresh, "2026-01-01T00:05:00") is True
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


def test_fixer_search_replace_parse_and_apply():
    from agents import fixer
    text = (
        "### EDIT: /root/p/app.py\n```python\n"
        "<<<<<<< SEARCH\ndef add(a, b):\n    return a - b\n=======\n"
        "def add(a, b):\n    return a + b\n>>>>>>> REPLACE\n"
        "```\n\n### SUMMARY: fix add\n"
    )
    parsed = fixer._parse_fixer_output(text)
    assert parsed["parse_errors"] == []
    edit = parsed["edits"][0]
    assert edit["mode"] == "replace"

    original = ("import math\n\ndef add(a, b):\n    return a - b\n\n"
                "def sub(a, b):\n    return a - b\n")
    new, errs = fixer._apply_search_replace(original, edit["blocks"])
    assert errs == []
    assert "def add(a, b):\n    return a + b" in new
    # sub() untouched — only the matched site changed.
    assert "def sub(a, b):\n    return a - b" in new


def test_fixer_search_replace_fuzzy_and_unmatched():
    from agents import fixer
    original = "    if x:\n        do_thing()\n    return x\n"
    # Indentation drift → fuzzy match still lands.
    new, errs = fixer._apply_search_replace(original, [("if x:\n    do_thing()", "if x and y:\n    do_thing()")])
    assert errs == []
    assert "if x and y:" in new
    # Nothing matches → None + error, file untouched.
    new2, errs2 = fixer._apply_search_replace(original, [("not in file", "x")])
    assert new2 is None
    assert "not found" in errs2[0]


def test_fixer_rewrite_section_still_supported():
    from agents import fixer
    text = ("### REWRITE: /root/p/tiny.py\n```python\nprint('all new')\n```\n\n"
            "### SUMMARY: rewrote\n")
    parsed = fixer._parse_fixer_output(text)
    assert parsed["edits"][0]["mode"] == "rewrite"
    assert parsed["edits"][0]["content"] == "print('all new')"


def test_fixer_delete_section_parse():
    from agents import fixer
    text = ("### DELETE: /root/projects/p/state.json\n\n"
            "### EDIT: /root/projects/p/app.py\n```python\n"
            "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n```\n\n"
            "### SUMMARY: removed runtime state, fixed x\n")
    parsed = fixer._parse_fixer_output(text)
    assert parsed["parse_errors"] == []
    modes = {e["path"]: e["mode"] for e in parsed["edits"]}
    assert modes["/root/projects/p/state.json"] == "delete"
    assert modes["/root/projects/p/app.py"] == "replace"


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
