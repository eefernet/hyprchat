import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_HAS_AIOSQLITE = importlib.util.find_spec("aiosqlite") is not None
if not _HAS_AIOSQLITE:
    sys.modules.setdefault("aiosqlite", types.SimpleNamespace(Connection=object, Row=object))

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
    if importlib.util.find_spec("fastapi") is None:
        class DummyFastAPI:
            def get(self, *args, **kwargs):
                return lambda fn: fn

            def post(self, *args, **kwargs):
                return lambda fn: fn

        monkeypatch.setitem(sys.modules, "fastapi", types.SimpleNamespace(FastAPI=lambda *a, **k: DummyFastAPI()))
        monkeypatch.setitem(sys.modules, "fastapi.responses", types.SimpleNamespace(StreamingResponse=object))
    if importlib.util.find_spec("pydantic") is None:
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

        monkeypatch.setitem(sys.modules, "pydantic", types.SimpleNamespace(BaseModel=DummyBaseModel))
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
    if importlib.util.find_spec("chromadb") is None:
        async def _noop_index(*args, **kwargs):
            return None

        monkeypatch.setitem(
            sys.modules,
            "rag",
            types.SimpleNamespace(RESEARCH_TOOLS=set(), index_research=_noop_index),
        )
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
    assert state["count"] == 0
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
