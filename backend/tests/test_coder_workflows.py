import asyncio
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
    sys.modules.setdefault("aiosqlite", types.SimpleNamespace())

from agents import language_adapters
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
    assert "python -m pip install -e ." in contract["build_cmd"]
    assert contract["test_cmd"] == "python -m pytest -q"
    assert "python -m taskforge --help" in contract["smoke_cmds"]
    assert contract["safe_lint"] is True
    assert "__main__.py" in " ".join(contract["package_rules"])


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


def test_new_coder_tools_are_registered():
    for name in (
        "start_coder_workflow",
        "run_aider_fix",
        "get_coder_workflow",
        "cancel_coder_workflow",
    ):
        assert name in CODEAGENT_TOOLS

    assert "uploaded-project fixes" in CODEAGENT_TOOLS["run_aider_fix"]["function"]["description"]
