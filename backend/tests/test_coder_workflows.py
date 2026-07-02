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

from agents import aider_fixer, fixer, language_adapters, reviewer
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
    iso = contract["isolated_verification"]
    assert iso["applicable"] is True
    assert iso["required_for_delivery"] is True
    assert "-m venv" in iso["setup_cmd"]
    assert any("pip check" in cmd for cmd in iso["verify_cmds"])


def test_python_adapter_adds_pygame_isolated_runtime_smoke():
    manifest = [
        "pyproject.toml",
        "main.py",
        "ui/scoreboard.py",
    ]

    contract = language_adapters.detect_contract(manifest, "python")
    runtime_cmds = contract["isolated_verification"]["runtime_smoke_cmds"]

    assert runtime_cmds
    assert "pygame.font.Font" in runtime_cmds[0]
    assert "SDL_VIDEODRIVER=dummy" in runtime_cmds[0]


def test_manifest_adapters_expose_isolated_verification_contracts():
    cases = [
        (["package.json", "src/index.js"], "javascript"),
        (["Cargo.toml", "src/main.rs"], "rust"),
        (["go.mod", "main.go"], "go"),
        (["pom.xml", "src/main/java/App.java"], "java"),
    ]

    for manifest, language in cases:
        contract = language_adapters.detect_contract(manifest, language)
        iso = contract["isolated_verification"]
        assert iso["applicable"] is True
        assert iso["required_for_delivery"] is True
        assert iso["setup_cmd"]
        assert iso["verify_cmds"]


def test_node_adapter_runtime_smoke_is_guarded_and_bounded():
    contract = language_adapters.detect_contract(["package.json", "src/index.js"], "javascript")
    runtime_cmds = contract["isolated_verification"]["runtime_smoke_cmds"]

    assert runtime_cmds
    cmd = runtime_cmds[0]
    assert "no start script or bin" in cmd
    assert "binpath:" in cmd
    assert "timeout 15 npm start" in cmd
    assert "treating timeout as started-ok" in cmd
    assert contract["smoke_cmds"]
    assert contract["smoke_cmds"][0].startswith("if [ ! -f package.json ]")


def test_rust_adapter_runtime_smoke_only_for_binary_targets():
    lib_contract = language_adapters.detect_contract(["Cargo.toml", "src/lib.rs"], "rust")
    bin_contract = language_adapters.detect_contract(["Cargo.toml", "src/main.rs"], "rust")
    named_bin_contract = language_adapters.detect_contract(["Cargo.toml", "src/bin/server.rs"], "rust")

    assert lib_contract["smoke_cmds"] == []
    assert lib_contract["isolated_verification"]["runtime_smoke_cmds"] == []
    assert bin_contract["smoke_cmds"]
    assert "timeout 15 cargo run -- --help" in bin_contract["smoke_cmds"][0]
    assert "treating timeout as started-ok" in bin_contract["smoke_cmds"][0]
    assert "cargo run --bin server -- --help" in named_bin_contract["smoke_cmds"][0]


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


def test_fixer_detects_method_mismatch_without_mutating():
    # Detect, don't mutate: the Fixer no longer auto-rewrites call names (a
    # blanket string-replace can't express a bidirectional swap and produced
    # wrong-but-passing edits). It detects the mismatch and surfaces the
    # actual class methods to the coder LLM instead.
    issues = [{
        "file": "main.py",
        "summary": (
            "Runtime method-name mismatch: main.py calls "
            "particle_system.create_explosion(...), but effects/particle_system.py "
            "only defines add_explosion(...), so collisions raise AttributeError."
        ),
        "suggested_fix_scope": ["main.py", "effects/particle_system.py"],
    }]
    main_src = "\n".join([
        "from effects.particle_system import ParticleSystem",
        "",
        "particle_system = ParticleSystem()",
        "particle_system.create_explosion(ball.x, ball.y, (0, 255, 255))",
        "",
    ])
    contents = {
        "/root/projects/neon-pong-game/main.py": main_src,
        "/root/projects/neon-pong-game/effects/particle_system.py": "\n".join([
            "class ParticleSystem:",
            "    def add_explosion(self, x, y, color, count=20):",
            "        return []",
            "",
        ]),
    }

    # Detection fires on the mismatch.
    assert fixer._python_symbol_mismatch_errors(contents, issues)
    # The symbol reference exposes the ACTUAL methods for the prompt hint.
    defined = fixer._python_defined_methods(contents, issues)
    assert defined.get("ParticleSystem") == ["add_explosion"]
    # Detection is non-mutating — the source is untouched.
    assert contents["/root/projects/neon-pong-game/main.py"] == main_src
    # And it no longer exposes the old blanket-replace repair.
    assert not hasattr(fixer, "_repair_python_symbol_mismatches")


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

    assert "You are editing an existing project" in prompt
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


def test_openhands_worker_fresh_build_ignores_nonexistent_project_id(tmp_path, monkeypatch):
    # Pre-refactor behavior: a NEW build never adopts a non-existing project_id;
    # it lands in a fresh task-derived dir so it can't inherit continue-mode and
    # write a single file into a colliding/stale project directory.
    worker = _import_openhands_worker_for_prompt_tests(monkeypatch)
    monkeypatch.setattr(worker, "PROJECTS_DIR", tmp_path)
    req = worker.RunRequest(
        task="Build a neon pong game",
        language="python",
        project_id="neon-pong-game",  # does not exist yet
    )

    work_dir, project_name, reusing = worker._workspace_for_request(req)

    assert reusing is False
    assert work_dir.is_dir()
    assert work_dir.parent == tmp_path
    sys.modules.pop("openhands_worker", None)


def test_openhands_worker_reuses_existing_project_id(tmp_path, monkeypatch):
    # A provided project_id is honored only as a genuine resume (the dir exists).
    worker = _import_openhands_worker_for_prompt_tests(monkeypatch)
    monkeypatch.setattr(worker, "PROJECTS_DIR", tmp_path)
    (tmp_path / "neon-pong-game").mkdir()
    req = worker.RunRequest(
        task="Add a scoreboard",
        language="python",
        project_id="neon-pong-game",
    )

    work_dir, project_name, reusing = worker._workspace_for_request(req)

    assert project_name == "neon-pong-game"
    assert work_dir == tmp_path / "neon-pong-game"
    assert reusing is True
    sys.modules.pop("openhands_worker", None)


def test_openhands_worker_prompt_has_no_required_file_manifest(tmp_path, monkeypatch):
    # Reverted builder prompt: the Architect manifest rides in plan CONTEXT, not
    # a separate REQUIRED FILE MANIFEST / PROJECT COMMANDS gate.
    worker = _import_openhands_worker_for_prompt_tests(monkeypatch)
    req = worker.RunRequest(task="Build neon pong", language="python")

    prompt = worker._build_task_prompt(req, str(tmp_path), continuing=False)

    assert "## REQUIRED FILE MANIFEST" not in prompt
    assert "## PROJECT COMMANDS FROM ARCHITECT" not in prompt
    assert "create ALL files" in prompt  # scaffold still builds everything
    sys.modules.pop("openhands_worker", None)


def test_openhands_worker_agent_finished_flags_no_finish(monkeypatch):
    # A run with no FINISHED status and no finish action is flagged incomplete so
    # a truncated one-file build is never reported as a success.
    worker = _import_openhands_worker_for_prompt_tests(monkeypatch)
    assert worker._agent_finished("AgentExecutionStatus.FINISHED", []) is True
    assert worker._agent_finished("running", [{"action": "finish"}]) is True
    assert worker._agent_finished("running", [{"action": "file_create"}]) is False
    assert worker._agent_finished("", []) is False
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


def test_chat_plain_plan_project_result_gets_generate_code_nudge(monkeypatch):
    chat = _import_chat_with_optional_stubs(monkeypatch)
    plain_plan = "\n".join([
        "Plan ready for a large project.",
        "Create these files:",
        "- app.py",
        "- server.py",
        "- frontend/src/main.jsx",
        "- frontend/src/state.js",
        "Use tests/test_app.py for verification.",
        "Details: " + ("implement routing, state, persistence, and tests. " * 40),
    ])

    assert len(plain_plan) > 1000
    assert chat._plan_project_file_ref_count(plain_plan) >= 3
    assert chat._plan_project_should_nudge_generate_code(plain_plan) is True
    sys.modules.pop("agents.chat", None)


def test_chat_daedalus_verified_result_skips_generate_code_nudge(monkeypatch):
    chat = _import_chat_with_optional_stubs(monkeypatch)
    verified_result = "\n".join([
        "Plan ready for a large project.",
        "- app.py",
        "- server.py",
        "- frontend/src/main.jsx",
        "- frontend/src/state.js",
        "Details: " + ("implement routing, state, persistence, and tests. " * 40),
        "=== AUTOMATIC VERIFICATION - run_review already ran; do NOT call it again ===",
        "REVIEW CLEAN.",
        "PROJECT COMPLETE. OpenHands agent built the project in /root/projects/demo.",
    ])

    assert len(verified_result) > 1000
    assert chat._plan_project_file_ref_count(verified_result) >= 3
    assert chat._plan_project_should_nudge_generate_code(verified_result) is False
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


def test_greenfield_aider_context_accepts_issue_project_dir(monkeypatch):
    import tools

    async def no_uploaded(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "_uploaded_project_aider_context", no_uploaded)
    monkeypatch.setattr(tools.config, "AIDER_ENABLED", True)
    monkeypatch.setattr(tools.config, "AIDER_FOR_GREENFIELD", True)
    issue_run = {
        "id": "run-acceptance",
        "role": "acceptance",
        "project_id": "",
        "result_envelope": {
            "status": "issues",
            "project_dir": "/root/projects/neon-pong",
        },
    }

    ctx = _run(tools._aider_repair_context("conv-neon", issue_run=issue_run))

    assert ctx == {
        "workflow": None,
        "project_id": "neon-pong",
        "project_dir": "/root/projects/neon-pong",
    }


def test_aider_yields_to_fixer_after_same_role_aider_attempt():
    import tools

    issue_run = {"id": "run-acceptance-2", "role": "acceptance", "result_envelope": {"status": "issues"}}
    runs = [
        issue_run,
        {
            "id": "run-aider",
            "role": "aider.fix",
            "parent_run_id": "run-acceptance-1",
            "status": "succeeded",
            "result_envelope": {"source_role": "acceptance", "status": "applied"},
        },
        {"id": "run-acceptance-1", "role": "acceptance", "result_envelope": {"status": "issues"}},
    ]

    assert tools._latest_repair_before_issue_was_aider(runs, issue_run) is True
    runs[1]["role"] = "fixer"
    assert tools._latest_repair_before_issue_was_aider(runs, issue_run) is False


def test_aider_first_context_requires_worker_health(monkeypatch):
    import tools

    class Resp:
        status_code = 200

        def json(self):
            return {"installed": False}

    class Http:
        async def get(self, *args, **kwargs):
            return Resp()

    async def repair_ctx(*args, **kwargs):
        return {"workflow": None, "project_id": "neon", "project_dir": "/root/projects/neon"}

    monkeypatch.setattr(tools, "_aider_repair_context", repair_ctx)
    monkeypatch.setattr(tools.config, "AIDER_ENABLED", True)
    tools._AIDER_HEALTH_CACHE.update({"url": "", "ts": 0.0, "healthy": False})

    ctx = _run(tools._aider_first_context(Http(), "conv-neon"))

    assert ctx is None


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


def test_uploaded_project_bootstrap_exec_tool_uses_extracted_gate(monkeypatch):
    import tools

    class Events:
        def __init__(self):
            self.items = []

        async def emit(self, conv_id, event_type, data):
            self.items.append((conv_id, event_type, data))

    workflow = {
        "id": "cw-upload",
        "mode": "fix_uploaded_project",
        "state": "fixing",
        "project_id": "proj-upload",
        "user_task": "Fix the import mismatch",
        "artifact_status": "not_ready",
        "cancel_requested": False,
    }

    async def get_conversation(conv_id):
        return {
            "id": conv_id,
            "model_config_id": "mc-v2",
            "messages": [{"role": "user", "content": "Fix the import mismatch"}],
        }

    async def get_runs_by_conversation(_conv_id, limit=50):
        assert limit == 50
        return []

    async def get_latest_coder_workflow(_conv_id, project_id=None):
        return workflow

    async def is_v2(_conv_id, conv_row=None):
        return True

    async def latest_task(_conv_id):
        return "Fix the import mismatch"

    monkeypatch.setattr(tools.db, "get_conversation", get_conversation)
    monkeypatch.setattr(tools.db, "get_runs_by_conversation", get_runs_by_conversation)
    monkeypatch.setattr(tools.db, "get_latest_coder_workflow", get_latest_coder_workflow)
    monkeypatch.setattr(tools, "_is_v2_persona", is_v2)
    monkeypatch.setattr(tools, "_latest_user_task_text", latest_task)

    events = Events()
    result = _run(tools.exec_tool(
        http=object(),
        events=events,
        name="read_file",
        args={"path": "/root/projects/proj-upload/main.py"},
        conv_id="conv-upload",
    ))

    assert result.startswith("BLOCKED")
    assert "uploaded-project fixes must start with Aider" in result
    assert "run_aider_fix(project_dir='/root/projects/proj-upload'" in result
    assert events.items[-1][2]["status"] == "⛔ Blocked — uploaded-project fixes use Aider first"


def test_exec_tool_calls_extracted_gate_with_partial_v2_context(monkeypatch):
    import tools
    from tooling.gate_decisions import GateDecision

    class Events:
        def __init__(self):
            self.items = []

        async def emit(self, conv_id, event_type, data):
            self.items.append((conv_id, event_type, data))

    seen = {}

    async def get_conversation(conv_id):
        return {
            "id": conv_id,
            "model_config_id": "mc-v2",
            "messages": [{"role": "user", "created_at": "2026-01-01 12:00:00"}],
        }

    async def get_runs_by_conversation(_conv_id, limit=50):
        assert limit == 50
        raise RuntimeError("transient runs read failure")

    async def get_latest_coder_workflow(_conv_id, project_id=None):
        return None

    async def is_v2(_conv_id, conv_row=None):
        return True

    async def fake_evaluate_gate(ctx):
        seen["ctx"] = ctx
        return GateDecision.block(
            message="BLOCKED partial context",
            event_status="⛔ partial context evaluated",
        )

    monkeypatch.setattr(tools.db, "get_conversation", get_conversation)
    monkeypatch.setattr(tools.db, "get_runs_by_conversation", get_runs_by_conversation)
    monkeypatch.setattr(tools.db, "get_latest_coder_workflow", get_latest_coder_workflow)
    monkeypatch.setattr(tools, "_is_v2_persona", is_v2)
    monkeypatch.setattr(tools, "evaluate_gate", fake_evaluate_gate)

    events = Events()
    result = _run(tools.exec_tool(
        http=object(),
        events=events,
        name="read_file",
        args={"path": "/root/projects/demo/app.py"},
        conv_id="conv-partial",
    ))

    assert result == "BLOCKED partial context"
    assert seen["ctx"].is_v2 is True
    assert seen["ctx"].snapshot_partial is True
    assert seen["ctx"].runs == []
    assert events.items[-1][2]["status"] == "⛔ partial context evaluated"


def test_partial_snapshot_discarded_so_gates_refetch_runs(monkeypatch):
    """A partial gate snapshot must never feed the gates as an (empty)
    authoritative runs list. When the v2 lookup fails closed during
    build_gate_context but a later _check_v2() re-check succeeds, the
    phantom-fixer gate must see the real actionable reviewer run from the DB
    instead of blocking run_fixer against runs=[]."""
    import tools
    from agents import fixer as fixer_mod

    class Events:
        async def emit(self, *a, **k):
            pass

    conv_calls = {"n": 0}
    v2_calls = {"n": 0}
    reviewer_run = {
        "id": "run-rev-1",
        "role": "reviewer",
        "status": "succeeded",
        "started_at": "2026-01-01 12:05:00",
        "result_envelope": {
            "status": "issues",
            "project_dir": "/root/projects/demo",
            "issues": [{"severity": "bug", "file": "app.py",
                        "summary": "boom", "suggested_fix_scope": ["app.py"]}],
        },
    }

    async def get_conversation(conv_id):
        conv_calls["n"] += 1
        if conv_calls["n"] == 1:
            raise RuntimeError("transient conversation read failure")
        return {
            "id": conv_id,
            "model_config_id": "mc-v2",
            "messages": [{"role": "user", "created_at": "2026-01-01 12:00:00"}],
        }

    async def get_runs_by_conversation(_conv_id, limit=50):
        return [reviewer_run]

    async def get_run(run_id):
        return reviewer_run if run_id == "run-rev-1" else None

    async def get_latest_coder_workflow(_conv_id, project_id=None):
        return None

    async def no_project(_conv_id):
        return None

    async def is_v2(_conv_id, conv_row=None):
        v2_calls["n"] += 1
        # Build-time fallback fails closed; the later re-check succeeds.
        return v2_calls["n"] > 1

    async def unhealthy(_http, force=False):
        return False

    async def fake_run_fixer(_http, _events, _conv_id, **kwargs):
        return {"status": "skipped", "summary": "stub"}

    monkeypatch.setattr(tools.db, "get_conversation", get_conversation)
    monkeypatch.setattr(tools.db, "get_runs_by_conversation", get_runs_by_conversation)
    monkeypatch.setattr(tools.db, "get_run", get_run)
    monkeypatch.setattr(tools.db, "get_latest_coder_workflow", get_latest_coder_workflow)
    monkeypatch.setattr(tools.db, "get_coding_project_by_conv", no_project)
    monkeypatch.setattr(tools, "_is_v2_persona", is_v2)
    monkeypatch.setattr(tools, "_aider_worker_healthy", unhealthy)
    monkeypatch.setattr(fixer_mod, "run_fixer", fake_run_fixer)

    result = _run(tools.exec_tool(
        http=object(),
        events=Events(),
        name="run_fixer",
        args={"reviewer_run_id": "run-rev-1"},
        conv_id="conv-gate-partial",
    ))

    # Pre-fix, the phantom-fixer gate evaluated the stale empty snapshot and
    # returned "BLOCKED — run_fixer requires a recent reviewer ...".
    assert "requires a recent reviewer or acceptance envelope" not in result
    assert result == "FIXER SKIPPED: stub."


def test_acceptance_base_cap_forces_research_before_delivery(monkeypatch):
    import tools

    class Events:
        def __init__(self):
            self.items = []

        async def emit(self, conv_id, event_type, data):
            self.items.append((conv_id, event_type, data))

    runs = [
        {
            "id": "run-acceptance",
            "role": "acceptance",
            "status": "succeeded",
            "started_at": "2026-06-24T10:03:00",
            "result_envelope": {
                "status": "issues",
                "project_dir": "/root/projects/neon-pong-game",
                "issues": [{
                    "category": "runtime",
                    "file": "main.py",
                    "summary": "main.py calls create_explosion but ParticleSystem defines add_explosion.",
                    "suggested_fix_scope": ["main.py", "effects/particle_system.py"],
                }],
            },
        },
        {
            "id": "run-fix-2",
            "role": "fixer",
            "status": "succeeded",
            "parent_run_id": "run-acceptance",
            "started_at": "2026-06-24T10:02:00",
            "result_envelope": {"status": "applied", "source_role": "acceptance"},
        },
        {
            "id": "run-fix-1",
            "role": "fixer",
            "status": "succeeded",
            "parent_run_id": "run-acceptance",
            "started_at": "2026-06-24T10:01:00",
            "result_envelope": {"status": "applied", "source_role": "acceptance"},
        },
    ]

    async def get_runs_by_conversation(_conv_id, limit=20):
        return runs

    async def get_latest_coder_workflow(_conv_id, project_id=None):
        return None

    async def is_v2(_conv_id, conv_row=None):
        return True

    async def no_user_ts(_conv_id):
        return None

    async def no_ship_anyway(_conv_id):
        return False

    monkeypatch.setattr(tools.db, "get_runs_by_conversation", get_runs_by_conversation)
    monkeypatch.setattr(tools.db, "get_latest_coder_workflow", get_latest_coder_workflow)
    monkeypatch.setattr(tools, "_is_v2_persona", is_v2)
    monkeypatch.setattr(tools, "_latest_user_msg_ts", no_user_ts)
    monkeypatch.setattr(tools, "_latest_user_requested_ship_anyway", no_ship_anyway)

    result = _run(tools.exec_tool(
        None,
        Events(),
        "download_project",
        {"directory": "/root/projects/neon-pong-game"},
        "conv-neon",
    ))

    assert result.startswith("BLOCKED")
    assert "deep_research(" in result
    assert "run_fixer(reviewer_run_id='run-acceptance')" not in result
    assert "download_project" in result
    assert tools._fix_cap_releases_tool("read_file") is True
    assert tools._fix_cap_releases_tool("download_project") is False


def test_acceptance_review_blocks_after_source_aider_acceptance_fix(monkeypatch):
    import tools

    class Events:
        async def emit(self, *_args, **_kwargs):
            pass

    runs = [
        {
            "id": "run-aider",
            "role": "aider.fix",
            "status": "succeeded",
            "parent_run_id": "run-acceptance",
            "result_envelope": {
                "status": "applied",
                "source_role": "acceptance",
                "docs_only": False,
                "files_touched": ["main.py"],
            },
        },
        {
            "id": "run-review",
            "role": "reviewer",
            "status": "succeeded",
            "result_envelope": {
                "status": "clean",
                "project_dir": "/root/projects/demo",
            },
        },
    ]

    async def get_runs_by_conversation(_conv_id, limit=50):
        return runs

    async def get_conversation(_conv_id):
        return {"id": _conv_id, "messages": [], "model_config_id": ""}

    async def get_latest_coder_workflow(_conv_id, project_id=None):
        return None

    async def is_v2(_conv_id, conv_row=None):
        return False

    monkeypatch.setattr(tools.db, "get_runs_by_conversation", get_runs_by_conversation)
    monkeypatch.setattr(tools.db, "get_conversation", get_conversation)
    monkeypatch.setattr(tools.db, "get_latest_coder_workflow", get_latest_coder_workflow)
    monkeypatch.setattr(tools, "_is_v2_persona", is_v2)

    result = _run(tools.exec_tool(
        object(),
        Events(),
        "run_acceptance_review",
        {"project_dir": "/root/projects/demo"},
        "conv-accept",
    ))

    assert "cannot run immediately after source/test/manifest fixes" in result
    assert "Call run_review first" in result


def test_acceptance_review_allows_docs_only_aider_acceptance_fix(monkeypatch):
    import tools
    from agents import acceptance

    class Events:
        async def emit(self, *_args, **_kwargs):
            pass

    reviewer_run = {
        "id": "run-review",
        "role": "reviewer",
        "status": "succeeded",
        "result_envelope": {
            "status": "clean",
            "project_dir": "/root/projects/demo",
            "project_id": "demo",
        },
    }
    runs = [
        {
            "id": "run-aider",
            "role": "aider.fix",
            "status": "succeeded",
            "parent_run_id": "run-acceptance",
            "result_envelope": {
                "status": "applied",
                "source_role": "acceptance",
                "docs_only": True,
                "files_touched": ["README.md"],
            },
        },
        reviewer_run,
    ]

    async def get_runs_by_conversation(_conv_id, limit=50):
        return runs

    async def get_conversation(_conv_id):
        return {"id": _conv_id, "messages": [], "model_config_id": ""}

    async def get_latest_coder_workflow(_conv_id, project_id=None):
        return None

    async def get_run(run_id):
        return reviewer_run if run_id == reviewer_run["id"] else None

    async def is_v2(_conv_id, conv_row=None):
        return False

    async def no_prior_acceptance(_conv_id):
        return ""

    async def no_workflow_event(*_args, **_kwargs):
        return ""

    async def fake_acceptance(*_args, **_kwargs):
        return {"status": "accepted", "summary": "ok", "run_id": "run-accepted"}

    monkeypatch.setattr(tools.db, "get_runs_by_conversation", get_runs_by_conversation)
    monkeypatch.setattr(tools.db, "get_conversation", get_conversation)
    monkeypatch.setattr(tools.db, "get_latest_coder_workflow", get_latest_coder_workflow)
    monkeypatch.setattr(tools.db, "get_run", get_run)
    monkeypatch.setattr(tools, "_is_v2_persona", is_v2)
    monkeypatch.setattr(tools, "_prior_acceptance_issues_context", no_prior_acceptance)
    monkeypatch.setattr(tools, "_apply_workflow_event", no_workflow_event)
    monkeypatch.setattr(acceptance, "run_acceptance_review", fake_acceptance)

    result = _run(tools.exec_tool(
        object(),
        Events(),
        "run_acceptance_review",
        {"project_dir": "/root/projects/demo", "reviewer_run_id": "run-review"},
        "conv-accept-docs",
    ))

    assert result.startswith("ACCEPTANCE ACCEPTED")


def test_new_coder_tools_are_registered():
    for name in (
        "start_coder_workflow",
        "run_aider_fix",
        "get_coder_workflow",
        "cancel_coder_workflow",
    ):
        assert name in CODEAGENT_TOOLS

    assert "Primary repair editor" in CODEAGENT_TOOLS["run_aider_fix"]["function"]["description"]
    assert "project_id" in CODEAGENT_TOOLS["run_aider_fix"]["function"]["parameters"]["properties"]


def test_daedalus_seed_prompt_prefers_aider_for_existing_project_roots(monkeypatch):
    from agents import personas

    captured = {}

    class FakeDB:
        async def get_model_configs(self):
            return []

        async def get_kbs(self):
            return []

        async def create_model_config(self, mc_id, name, base_model, system_prompt,
                                      tool_ids, kb_ids, parameters):
            captured["system_prompt"] = system_prompt
            captured["tool_ids"] = tool_ids

    monkeypatch.setattr(personas, "db", FakeDB())

    _run(personas.seed_coder_bot_v2())

    prompt = captured["system_prompt"]
    assert "Existing project root" in prompt
    assert "run_aider_fix(issue_run_id='run-...'" in prompt
    assert "Fixer is fallback only" in prompt
    assert "Greenfield/OpenHands output: call `run_fixer" not in prompt
    assert "Acceptance returns issues, call `run_fixer" not in prompt


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


def test_aider_error_with_edits_routes_to_review_not_fixer(tmp_path, monkeypatch):
    """An Aider run that FAILED but already wrote files must not fall back to
    the marker Fixer (second editor, stale envelope, no review in between) —
    it must route to run_review instead."""
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import tools
    import database as db
    from agents import fixer as fixer_mod
    _prep_aider_db(tmp_path, db)

    async def healthy(_http, force=False):
        return True

    async def fake_aider(_http, _events, _conv_id, **kwargs):
        return {
            "status": "error",
            "summary": "tests failing after edits",
            "files_touched": ["main.py"],
            "project_dir": "/root/projects/proj-a",
            "stderr_tail": "AssertionError",
            "source_role": "reviewer",
            "run_id": "run-aider-x",
        }

    fixer_calls = {"n": 0}

    async def spy_fixer(*a, **k):
        fixer_calls["n"] += 1
        return {"status": "skipped", "summary": "stub"}

    monkeypatch.setattr(tools, "_aider_worker_healthy", healthy)
    monkeypatch.setattr(aider_fixer, "run_aider_fix", fake_aider)
    monkeypatch.setattr(fixer_mod, "run_fixer", spy_fixer)

    result = _run(tools.exec_tool(
        http=_AiderFakeHTTP({}),
        events=_NullEvents(),
        name="run_aider_fix",
        args={"task": "fix it", "project_dir": "/root/projects/proj-a"},
        conv_id="conv-aider",
    ))

    assert result.startswith("AIDER FAILED (error)")
    assert "run_review" in result
    assert fixer_calls["n"] == 0


def test_aider_error_without_edits_still_falls_back_to_fixer(tmp_path, monkeypatch):
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import tools
    import database as db
    from agents import fixer as fixer_mod
    _prep_aider_db(tmp_path, db)
    # An actionable reviewer run so the fallback can resolve a real envelope.
    _run(db.create_run("run-rev-a", "conv-aider", role="reviewer", status="running"))
    _run(db.update_run("run-rev-a", status="succeeded", ended=True, result_envelope={
        "status": "issues",
        "project_dir": "/root/projects/proj-a",
        "issues": [{"severity": "bug", "file": "main.py", "summary": "boom",
                    "suggested_fix_scope": ["main.py"]}],
    }))

    async def healthy(_http, force=False):
        return True

    async def fake_aider(_http, _events, _conv_id, **kwargs):
        return {
            "status": "error",
            "summary": "aider crashed before editing",
            "files_touched": [],
            "project_dir": "/root/projects/proj-a",
            "stderr_tail": "",
        }

    fixer_calls = {"n": 0}

    async def spy_fixer(*a, **k):
        fixer_calls["n"] += 1
        return {"status": "skipped", "summary": "stub"}

    monkeypatch.setattr(tools, "_aider_worker_healthy", healthy)
    monkeypatch.setattr(aider_fixer, "run_aider_fix", fake_aider)
    monkeypatch.setattr(fixer_mod, "run_fixer", spy_fixer)

    result = _run(tools.exec_tool(
        http=_AiderFakeHTTP({}),
        events=_NullEvents(),
        name="run_aider_fix",
        args={"task": "fix it", "project_dir": "/root/projects/proj-a"},
        conv_id="conv-aider",
    ))

    assert fixer_calls["n"] == 1
    assert result == "FIXER SKIPPED: stub."


def test_aider_workflow_mode_matches_project_kind(tmp_path, monkeypatch):
    """run_aider_fix must not label a greenfield repair workflow as
    fix_uploaded_project — the uploaded-project gates key on that mode."""
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import tools
    import database as db

    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    _run(db.create_conversation("conv-green", "Greenfield"))
    _run(db.upsert_coding_project(
        "proj-g", "Snake", conversation_id="conv-green",
        description="Snake game built from prompt", language="python",
    ))
    _run(db.create_conversation("conv-upl", "Uploaded"))
    _run(db.upsert_coding_project(
        "proj-u", "Legacy", conversation_id="conv-upl",
        description="Uploaded project: legacy.zip", language="python",
    ))

    async def healthy(_http, force=False):
        return True

    async def fake_aider(_http, _events, _conv_id, **kwargs):
        return {"status": "error", "summary": "boom",
                "files_touched": ["a.py"],
                "project_dir": kwargs.get("project_dir", ""), "stderr_tail": ""}

    monkeypatch.setattr(tools, "_aider_worker_healthy", healthy)
    monkeypatch.setattr(aider_fixer, "run_aider_fix", fake_aider)

    _run(tools.exec_tool(
        http=_AiderFakeHTTP({}), events=_NullEvents(),
        name="run_aider_fix",
        args={"task": "fix the crash", "project_dir": "/root/projects/proj-g"},
        conv_id="conv-green",
    ))
    _run(tools.exec_tool(
        http=_AiderFakeHTTP({}), events=_NullEvents(),
        name="run_aider_fix",
        args={"task": "fix the crash", "project_dir": "/root/projects/proj-u"},
        conv_id="conv-upl",
    ))

    green_wf = _run(db.get_latest_coder_workflow("conv-green"))
    upl_wf = _run(db.get_latest_coder_workflow("conv-upl"))
    assert green_wf is not None and green_wf["mode"] == "build_from_prompt"
    assert upl_wf is not None and upl_wf["mode"] == "fix_uploaded_project"


def test_aider_docs_only_acceptance_fix_skips_auto_review(tmp_path, monkeypatch):
    """Docs-only acceptance fixes go straight back to Acceptance — the Aider
    applied path must not burn a full build/test run_review cycle (same
    routing run_fixer already uses)."""
    if not _HAS_AIOSQLITE:
        pytest.skip("aiosqlite not installed")
    import tools
    import database as db
    _prep_aider_db(tmp_path, db)

    async def healthy(_http, force=False):
        return True

    async def fake_aider(_http, _events, _conv_id, **kwargs):
        return {
            "status": "applied",
            "summary": "reworded README",
            "files_touched": ["README.md"],
            "project_dir": "/root/projects/proj-a",
            "source_role": "acceptance",
            "docs_only": True,
            "run_id": "run-aider-docs",
        }

    monkeypatch.setattr(tools, "_aider_worker_healthy", healthy)
    monkeypatch.setattr(aider_fixer, "run_aider_fix", fake_aider)

    result = _run(tools.exec_tool(
        http=_AiderFakeHTTP({}),
        events=_NullEvents(),
        name="run_aider_fix",
        args={"task": "fix the README wording", "project_dir": "/root/projects/proj-a"},
        conv_id="conv-aider",
    ))

    assert result.startswith("AIDER APPLIED EDITS")
    assert "run_acceptance_review" in result
    assert "AUTOMATIC VERIFICATION" not in result


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


def _base_architect_plan():
    return {
        "project_id": "neon-pong",
        "language": "python",
        "build_system": "pyproject",
        "build_cmd": "python -m compileall .",
        "test_cmd": "",
        "manifest": [
            {"path": "main.py", "purpose": "game loop", "estimated_loc": 120},
            {"path": "config.py", "purpose": "shared constants", "estimated_loc": 40},
        ],
        "success_criteria": ["build_cmd exits 0"],
    }


def test_architect_contract_fields_are_optional():
    from agents import architect

    plan = _base_architect_plan()

    valid, err = architect._validate_plan(plan)

    assert valid, err
    assert "entrypoint" not in plan
    assert "shared_constants" not in plan
    assert "interfaces" not in plan
    assert "cross_file_contracts" not in plan


def test_architect_contract_fields_validate_and_render():
    from agents import architect

    plan = _base_architect_plan()
    plan.update({
        "status": "ok",
        "entrypoint": {"run_cmd": "python main.py", "module": "main.py"},
        "dependency_policy": {
            "runtime": "Python 3.13",
            "packages": [{
                "name": "pygame-ce",
                "version": "latest",
                "reason": "pygame-compatible wheels for Python 3.13",
            }],
            "constraints": ["Do not pin pygame==2.5 on Python 3.13."],
        },
        "shared_constants": [{
            "name": "BALL_BASE_SPEED",
            "value": "300",
            "defined_in": "config.py",
            "used_by": ["game_entities.py", "main.py"],
        }],
        "interfaces": [{
            "file": "game_entities.py",
            "name": "Paddle.update",
            "signature": "def update(self, input_handler, keys, mouse_y, is_left) -> None",
            "notes": "called each frame from main.py",
        }],
        "cross_file_contracts": [{
            "producer": "config.py",
            "consumer": "game_entities.py",
            "contract": "Import BALL_BASE_SPEED exactly; do not use BASE_SPEED.",
        }],
    })

    valid, err = architect._validate_plan(plan)
    rendered = architect.format_plan_for_chat(plan)

    assert valid, err
    assert plan["entrypoint"]["run_cmd"] == "python main.py"
    assert plan["dependency_policy"]["packages"][0]["name"] == "pygame-ce"
    assert plan["shared_constants"][0]["name"] == "BALL_BASE_SPEED"
    assert plan["interfaces"][0]["name"] == "Paddle.update"
    assert "## Interface Contract" in rendered
    assert "BALL_BASE_SPEED" in rendered
    assert "Paddle.update" in rendered
    assert "config.py" in rendered and "game_entities.py" in rendered


def test_architect_contract_malformed_optional_fields_degrade():
    from agents import architect

    plan = _base_architect_plan()
    plan.update({
        "entrypoint": "python main.py",
        "dependency_policy": 7,
        "shared_constants": 9,
        "interfaces": None,
        "cross_file_contracts": 3.14,
    })

    valid, err = architect._validate_plan(plan)

    assert valid, err
    assert "entrypoint" not in plan
    assert "dependency_policy" not in plan
    assert "shared_constants" not in plan
    assert "interfaces" not in plan
    assert "cross_file_contracts" not in plan


def test_builder_context_includes_architect_contract_without_required_file_changes():
    import tools

    plan = _base_architect_plan()
    plan.update({
        "entrypoint": {"run_cmd": "python main.py", "module": "main.py"},
        "dependency_policy": {
            "runtime": "Python 3.13",
            "packages": [{"name": "pygame-ce", "reason": "Python 3.13 compatible"}],
        },
        "shared_constants": [{
            "name": "BALL_BASE_SPEED",
            "value": "300",
            "defined_in": "config.py",
            "used_by": ["game_entities.py"],
        }],
        "interfaces": [{
            "file": "game_entities.py",
            "name": "Paddle.update",
            "signature": "def update(self, input_handler, keys, mouse_y, is_left) -> None",
        }],
        "cross_file_contracts": [{
            "producer": "config.py",
            "consumer": "game_entities.py",
            "contract": "Use BALL_BASE_SPEED exactly.",
        }],
    })

    context = tools._build_architect_context(plan)
    required = tools._manifest_required_code_files(plan["manifest"])

    assert "Shared Interface Contract" in context
    assert "Entrypoint run command: python main.py" in context
    assert "pygame-ce" in context
    assert "BALL_BASE_SPEED" in context
    assert "Paddle.update" in context
    assert "config.py -> game_entities.py" in context
    assert required == ["main.py", "config.py"]


def test_manifest_wired_into_completeness_gate():
    """Architect manifest → completeness gate, binary assets excluded.

    Regression: a planned 9-file Daedalus build that wrote only 4 files was
    reported 'succeeded' because the completeness gate (_expected_files) was
    empty when the model didn't pass required_files. The Architect manifest is
    now wired in via _manifest_required_code_files, so a short build is caught.
    """
    import tools

    manifest = [
        {"path": "main.py"}, {"path": "game_entities.py"},
        {"path": "visual_effects.py"}, {"path": "audio_manager.py"},
        {"path": "ui_renderer.py"}, {"path": "config.py"},
        {"path": "pyproject.toml"},
        {"path": "assets/sounds/collision.wav"},
        {"path": "assets/sounds/score.wav"},
    ]
    required = tools._manifest_required_code_files(manifest)

    # Binary/media assets are excluded so they can't falsely flag incomplete.
    assert "assets/sounds/collision.wav" not in required
    assert "assets/sounds/score.wav" not in required
    # Source/config files are required; 9 manifest entries minus 2 wav = 7.
    assert "main.py" in required and "pyproject.toml" in required
    assert len(required) == 7

    # Builder wrote only 4 of the 7 required code files → strict manifest
    # presence reports the 3 missing source files (not the .wav assets).
    project_dir = "/root/projects/stunning-neon-pong"
    written = [
        f"{project_dir}/main.py",
        f"{project_dir}/game_entities.py",
        f"{project_dir}/visual_effects.py",
        f"{project_dir}/audio_manager.py",
    ]
    satisfied, missing = tools._manifest_presence(written, project_dir, required)
    assert set(satisfied) == {
        "main.py", "game_entities.py", "visual_effects.py", "audio_manager.py",
    }
    assert set(missing) == {"ui_renderer.py", "config.py", "pyproject.toml"}
    # Strict manifest: any missing planned file keeps the build partial (the
    # undershoot block flags _is_incomplete = bool(_missing) when strict).
    assert missing


def test_manifest_filter_requires_text_deliverables_excludes_binary():
    """Filter keeps every non-binary deliverable required; only true binaries drop.

    .svg is text the model can author (it was wrongly excluded before), and
    .json/.toml/.yaml/.md are real deliverables — all must stay required. Only
    formats the coder model can't produce (audio/raster/fonts/archives/pdf) drop.
    """
    import tools

    manifest = [
        {"path": "app.py"}, {"path": "config.json"}, {"path": "pyproject.toml"},
        {"path": "data.yaml"}, {"path": "README.md"}, {"path": "logo.svg"},
        {"path": "assets/click.wav"}, {"path": "sprite.png"}, {"path": "font.ttf"},
        {"path": "bundle.zip"}, {"path": "manual.pdf"},
    ]
    required = set(tools._manifest_required_code_files(manifest))
    # Required text/data deliverables (incl. .svg now):
    assert {"app.py", "config.json", "pyproject.toml", "data.yaml",
            "README.md", "logo.svg"} <= required
    # Genuinely-binary assets the model can't author are dropped:
    assert required.isdisjoint(
        {"assets/click.wav", "sprite.png", "font.ttf", "bundle.zip", "manual.pdf"})


def test_scaled_build_rounds_scales_with_file_count():
    """Round budget grows with the planned file count, floored + capped."""
    import tools
    floor = 20  # config.OPENHANDS_MAX_ROUNDS default
    assert tools._scaled_build_rounds(0, floor) == 30        # base
    assert tools._scaled_build_rounds(3, floor) == 45
    assert tools._scaled_build_rounds(5, floor) == 55
    assert tools._scaled_build_rounds(9, floor) == 75
    assert tools._scaled_build_rounds(15, floor) == 100      # 105 capped to 100
    assert tools._scaled_build_rounds(20, floor, cap=140) == 130
    assert tools._scaled_build_rounds(40, floor, cap=140) == 140
    assert tools._scaled_build_rounds(40, floor) == 100      # hard cap
    # Never below the configured floor (raising the env knob raises the floor):
    assert tools._scaled_build_rounds(1, 90) == 90
    # Monotonic non-decreasing in file count.
    seq = [tools._scaled_build_rounds(n, floor) for n in range(0, 25)]
    assert seq == sorted(seq)


def test_builder_continue_pass_budget_scales_for_project_size():
    """Backend-owned continue attempts scale across 3-20 file projects."""
    import tools

    assert tools._max_builder_continue_passes(0) == 3
    assert tools._max_builder_continue_passes(3) == 3
    assert tools._max_builder_continue_passes(8) == 3
    assert tools._max_builder_continue_passes(12) == 3
    assert tools._max_builder_continue_passes(16) == 4
    assert tools._max_builder_continue_passes(20) == 5
    assert tools._max_builder_continue_passes(30) == 6


def test_blocking_incomplete_builder_gate_logic():
    """The build-incomplete gate blocks partial+missing until complete or superseded."""
    import tools

    def builder(status, missing):
        return {"role": "builder.scaffold", "status": status,
                "result_envelope": {"manifest_missing": missing, "project_id": "neon"}}

    arch = {"role": "architect", "status": "succeeded", "result_envelope": {}}
    reviewer = {"role": "reviewer", "status": "succeeded",
                "result_envelope": {"status": "clean"}}

    # Most-recent builder partial WITH missing files → blocks (returns that run).
    runs = [builder("partial", ["audio.py"]), arch]
    assert tools._blocking_incomplete_builder(runs) is runs[0]
    runs = [builder("stuck", ["audio.py"]), arch]
    assert tools._blocking_incomplete_builder(runs) is runs[0]

    # Most-recent builder succeeded → no block.
    assert tools._blocking_incomplete_builder([builder("succeeded", []), arch]) is None
    # Partial but nothing actually missing → no block.
    assert tools._blocking_incomplete_builder([builder("partial", []), arch]) is None

    # A newer reviewer/fix run supersedes the raw build state → no block.
    assert tools._blocking_incomplete_builder(
        [reviewer, builder("partial", ["audio.py"])]) is None

    # Multiple partial attempts still block: count alone is not a delivery release.
    runs = [builder("partial", ["audio.py"]), builder("partial", ["audio.py"]), arch]
    assert tools._blocking_incomplete_builder(runs) is runs[0]


def test_generate_code_is_builder_completion_tool_for_partial_manifest():
    """Plain generate_code is allowed to finish an incomplete manifest build."""
    import tools

    partial = {
        "role": "builder.scaffold",
        "status": "partial",
        "result_envelope": {"manifest_missing": ["assets/sounds.py"]},
    }
    stuck = {
        "role": "builder.scaffold",
        "status": "stuck",
        "result_envelope": {"manifest_missing": ["assets/sounds.py"]},
    }
    complete = {
        "role": "builder.scaffold",
        "status": "succeeded",
        "result_envelope": {"manifest_missing": []},
    }

    assert tools._builder_completion_allowed("generate_code", {}, partial) is True
    assert tools._builder_completion_allowed("generate_code", {}, stuck) is True
    assert tools._builder_completion_allowed("run_review", {}, partial) is False
    assert tools._builder_completion_allowed("generate_code", {}, complete) is False


def test_openhands_worker_missing_required_files_normalizes_paths(tmp_path, monkeypatch):
    """Manifest paths arrive in mixed shapes (./x, /root/projects/p/x, x) — the
    worker's missing check must resolve them all against the workspace root."""
    worker = _import_openhands_worker_for_prompt_tests(monkeypatch)
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "main.py").write_text("print('hi')\n")
    (tmp_path / "config.py").write_text("X = 1\n")

    missing = worker._missing_required_files(tmp_path, [
        "./backend/main.py",
        "/root/projects/demo/config.py",
        "frontend/app.js",
        "",
    ])

    assert missing == ["frontend/app.js"]
    sys.modules.pop("openhands_worker", None)


def test_openhands_worker_completion_nudges_fill_missing_files(tmp_path, monkeypatch):
    """A text-only early finish leaves planned files missing; the nudge loop
    resumes the SAME conversation until the manifest is complete."""
    worker = _import_openhands_worker_for_prompt_tests(monkeypatch)
    (tmp_path / "main.py").write_text("print('hi')\n")

    class DummyConversation:
        def __init__(self):
            self.messages = []

        def send_message(self, msg):
            self.messages.append(msg)

        def run(self):
            # Each resumed run writes one more planned file.
            for rel in ("config.py", "entities/ball.py"):
                target = tmp_path / rel
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("x = 1\n")
                    return

    conv = DummyConversation()
    nudges, missing = worker._run_completion_nudges(
        conv, tmp_path, ["main.py", "config.py", "entities/ball.py"],
    )

    assert nudges == 2
    assert missing == []
    # The nudge message names the missing files and forbids text-only replies.
    assert "config.py" in conv.messages[0]
    assert "finish" in conv.messages[0]
    sys.modules.pop("openhands_worker", None)


def test_openhands_worker_completion_nudges_stop_on_no_progress(tmp_path, monkeypatch):
    """A wedged model that ignores nudges must not burn the whole budget: one
    text-only reply earns an insistent retry naming a concrete file, then two
    consecutive no-progress nudges end the loop."""
    worker = _import_openhands_worker_for_prompt_tests(monkeypatch)

    class StubbornConversation:
        def __init__(self):
            self.runs = 0
            self.messages = []

        def send_message(self, msg):
            self.messages.append(msg)

        def run(self):
            self.runs += 1

    conv = StubbornConversation()
    nudges, missing = worker._run_completion_nudges(
        conv, tmp_path, ["never_written.py"],
    )

    assert nudges == 2  # stopped after two consecutive no-progress passes
    assert conv.runs == 2
    assert missing == ["never_written.py"]
    # The retry escalates: names a concrete file and calls out the text-only reply.
    assert "STOP narrating" in conv.messages[1]
    assert "never_written.py" in conv.messages[1]
    sys.modules.pop("openhands_worker", None)


def _no_change_gate_env(monkeypatch, aider_envelope):
    """Shared setup: newest run is a fix attempt, older run is a reviewer with
    issues. Returns the Events collector; the caller execs read_file."""
    import tools

    aider_run = {
        "id": "run-aider-nc",
        "role": "aider.fix",
        "status": "failed",
        "started_at": "2026-01-01 12:10:00",
        "result_envelope": aider_envelope,
    }
    reviewer_run = {
        "id": "run-rev-nc",
        "role": "reviewer",
        "status": "succeeded",
        "started_at": "2026-01-01 12:05:00",
        "result_envelope": {
            "status": "issues",
            "project_dir": "/root/projects/demo",
            "issues": [{"severity": "bug", "file": "app.py",
                        "summary": "boom", "suggested_fix_scope": ["app.py"]}],
        },
    }
    runs = [aider_run, reviewer_run]

    async def get_conversation(conv_id):
        return {
            "id": conv_id,
            "model_config_id": "mc-v2",
            "messages": [{"role": "user", "created_at": "2026-01-01 12:00:00"}],
        }

    async def get_runs_by_conversation(_conv_id, limit=50):
        return runs

    async def get_run(run_id):
        for r in runs:
            if r["id"] == run_id:
                return r
        return None

    async def get_latest_coder_workflow(_conv_id, project_id=None):
        return None

    async def no_project(_conv_id):
        return None

    async def is_v2(_conv_id, conv_row=None):
        return True

    async def unhealthy(_http, force=False):
        return False

    monkeypatch.setattr(tools.db, "get_conversation", get_conversation)
    monkeypatch.setattr(tools.db, "get_runs_by_conversation", get_runs_by_conversation)
    monkeypatch.setattr(tools.db, "get_run", get_run)
    monkeypatch.setattr(tools.db, "get_latest_coder_workflow", get_latest_coder_workflow)
    monkeypatch.setattr(tools.db, "get_coding_project_by_conv", no_project)
    monkeypatch.setattr(tools, "_is_v2_persona", is_v2)
    monkeypatch.setattr(tools, "_aider_worker_healthy", unhealthy)

    class Events:
        def __init__(self):
            self.items = []

        async def emit(self, conv_id, event_type, data):
            self.items.append((conv_id, event_type, data))

    return Events()


def test_no_change_fix_run_does_not_force_review(monkeypatch):
    """An Aider run that changed NOTHING on disk (status no_changes, empty
    files_touched) must not gate on run_review — the prior reviewer's issues
    keep driving the state. With auto-dispatch, the blocked manual tool is
    converted straight into the repair path instead of ping-ponging between
    'call run_review first' and the research gates."""
    import tools

    events = _no_change_gate_env(monkeypatch, {
        "status": "no_changes",
        "summary": "Aider completed without changes.",
        "project_dir": "/root/projects/demo",
        "files_touched": [],
    })

    result = _run(tools.exec_tool(
        http=object(),
        events=events,
        name="read_file",
        args={"path": "/root/projects/demo/app.py"},
        conv_id="conv-no-change-gate",
    ))

    assert "run_review has not been called yet" not in result
    # Aider is unhealthy in this env → the manual tool auto-dispatches to the
    # fallback Fixer instead of returning a BLOCKED lecture.
    assert result.startswith("AUTO-DISPATCH")
    assert "run_fixer" in result


def test_fix_run_with_files_touched_still_forces_review(monkeypatch):
    """A failed fix run that DID write files changed the tree — review stays
    mandatory before anything else."""
    import tools

    events = _no_change_gate_env(monkeypatch, {
        "status": "error",
        "summary": "Aider exited non-zero after edits.",
        "project_dir": "/root/projects/demo",
        "files_touched": ["app.py"],
    })

    result = _run(tools.exec_tool(
        http=object(),
        events=events,
        name="read_file",
        args={"path": "/root/projects/demo/app.py"},
        conv_id="conv-touched-gate",
    ))

    assert "run_review has not been called yet" in result


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


def test_fix_needed_manual_tool_auto_dispatches_to_aider(monkeypatch):
    """With Aider healthy and budget remaining, a blocked manual tool in
    fix-needed state is converted into run_aider_fix instead of a BLOCKED
    lecture — flailing becomes repair progress."""
    import tools
    from agents import aider_fixer as aider_mod

    events = _no_change_gate_env(monkeypatch, {
        "status": "no_changes",
        "summary": "Aider completed without changes.",
        "project_dir": "/root/projects/demo",
        "files_touched": [],
    })

    async def healthy(_http, force=False):
        return True

    async def fake_aider_ctx(_http, _conv_id, issue_run=None):
        return {"project_dir": "/root/projects/demo"}

    dispatched = {}

    async def fake_run_aider_fix(_http, _events, _conv_id, **kwargs):
        dispatched.update(kwargs)
        return {"status": "cancelled", "summary": "stub aider run"}

    monkeypatch.setattr(tools, "_aider_worker_healthy", healthy)
    monkeypatch.setattr(tools, "_aider_first_context", fake_aider_ctx)
    monkeypatch.setattr(aider_mod, "run_aider_fix", fake_run_aider_fix)

    result = _run(tools.exec_tool(
        http=_AnyOkHTTP(),
        events=events,
        name="write_file",
        args={"path": "/root/projects/demo/app.py", "content": "print('x')\n"},
        conv_id="conv-auto-dispatch",
    ))

    assert result.startswith("AUTO-DISPATCH")
    assert "run_aider_fix" in result
    # The dispatched task names the pending issue and the blocked call's intent.
    assert dispatched.get("task") and "boom" in dispatched["task"]
    assert "write_file" in dispatched["task"]


def test_fix_needed_at_research_gate_stays_blocked_no_dispatch(monkeypatch):
    """At the base cap without research, manual tools must stay BLOCKED with the
    deep_research demand — auto-dispatch must not bypass the research ladder."""
    import tools

    aider_success = {
        "role": "aider.fix",
        "status": "succeeded",
        "started_at": "2026-01-01 12:20:00",
        "result_envelope": {"status": "applied", "source_role": "reviewer",
                            "files_touched": ["app.py"]},
    }
    reviewer_run = {
        "id": "run-rev-cap",
        "role": "reviewer",
        "status": "succeeded",
        "started_at": "2026-01-01 12:25:00",
        "result_envelope": {
            "status": "issues",
            "project_dir": "/root/projects/demo",
            "issues": [{"severity": "bug", "file": "app.py",
                        "summary": "boom", "suggested_fix_scope": ["app.py"]}],
        },
    }
    # Newest-first: reviewer with issues, then THREE successful reviewer-driven
    # fixes → base cap (3) hit, no research since → research gate must hold.
    runs = [reviewer_run] + [
        {**aider_success, "id": f"run-fx-{i}",
         "started_at": f"2026-01-01 12:1{i}:00"} for i in (3, 2, 1)
    ]

    async def get_conversation(conv_id):
        return {
            "id": conv_id,
            "model_config_id": "mc-v2",
            "messages": [{"role": "user", "created_at": "2026-01-01 12:00:00"}],
        }

    async def get_runs_by_conversation(_conv_id, limit=50):
        return runs

    async def get_run(run_id):
        return next((r for r in runs if r.get("id") == run_id), None)

    async def none_wf(_conv_id, project_id=None):
        return None

    async def no_project(_conv_id):
        return None

    async def is_v2(_conv_id, conv_row=None):
        return True

    async def healthy(_http, force=False):
        return True

    monkeypatch.setattr(tools.db, "get_conversation", get_conversation)
    monkeypatch.setattr(tools.db, "get_runs_by_conversation", get_runs_by_conversation)
    monkeypatch.setattr(tools.db, "get_run", get_run)
    monkeypatch.setattr(tools.db, "get_latest_coder_workflow", none_wf)
    monkeypatch.setattr(tools.db, "get_coding_project_by_conv", no_project)
    monkeypatch.setattr(tools, "_is_v2_persona", is_v2)
    monkeypatch.setattr(tools, "_aider_worker_healthy", healthy)

    class Events:
        async def emit(self, *a, **k):
            pass

    result = _run(tools.exec_tool(
        http=_AnyOkHTTP(),
        events=Events(),
        name="write_file",
        args={"path": "/root/projects/demo/app.py", "content": "print('x')\n"},
        conv_id="conv-cap-gate",
    ))

    assert "AUTO-DISPATCH" not in result
    assert "BLOCKED" in result
    assert "deep_research" in result


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


def test_aider_scope_mines_non_python_mentions():
    envelope = {
        "summary": "Schema mismatch between frontend/src/services/api.ts and Program.cs",
        "issues": [{
            "file": "",
            "summary": "Type drift; also see styles.css and app.toml",
            "suggested_fix_scope": [],
        }],
    }

    files = aider_fixer._allowed_files_from_issues(envelope)

    assert "frontend/src/services/api.ts" in files
    assert "Program.cs" in files
    assert "styles.css" in files
    assert "app.toml" in files
