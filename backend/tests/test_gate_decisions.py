import asyncio
import sys
from datetime import datetime
from pathlib import Path

from .optional_deps import HAS_AIOSQLITE, install_aiosqlite_stub


BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if not HAS_AIOSQLITE:
    install_aiosqlite_stub()

from tooling import gate_decisions  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _run_row(
    run_id,
    role,
    status="succeeded",
    started_at="2026-01-02 00:00:00",
    *,
    env=None,
    parent_run_id="",
):
    return {
        "id": run_id,
        "role": role,
        "status": status,
        "started_at": started_at,
        "parent_run_id": parent_run_id,
        "result_envelope": env or {},
    }


def _ctx(runs, *, latest_user_ts=None, research_done=False):
    async def research_since(_since):
        return research_done

    return gate_decisions.GateContext(
        conv_id="conv-gate",
        name="run_fixer",
        args={},
        runs=runs,
        latest_user_ts=latest_user_ts,
        is_v2=True,
        research_since=research_since,
    )


def test_compute_fix_budget_keeps_successes_and_attempts_distinct():
    latest_user = datetime(2026, 1, 1, 12, 0, 0)
    runs = [
        _run_row("rev-1", "reviewer", env={"status": "issues"}),
        _run_row("fix-1", "fixer", "succeeded", env={"source_role": "reviewer"}),
        _run_row("fix-2", "aider.fix", "failed", parent_run_id="rev-1"),
        _run_row(
            "fix-old",
            "fixer",
            "succeeded",
            "2026-01-01 11:59:00",
            env={"source_role": "reviewer"},
        ),
    ]

    budget = _run(gate_decisions.compute_fix_budget(_ctx(runs, latest_user_ts=latest_user), "reviewer"))

    assert budget.succeeded == 1
    assert budget.attempts == 2
    assert budget.base_cap == 3
    assert budget.cap_limit == 3


def test_compute_fix_budget_scopes_by_source_role_with_parent_fallback():
    runs = [
        _run_row("rev-1", "reviewer", env={"status": "issues"}),
        _run_row("acc-1", "acceptance", env={"status": "issues"}),
        _run_row("fix-review", "aider.fix", "succeeded", parent_run_id="rev-1"),
        _run_row("fix-accept", "fixer", "no_op", parent_run_id="acc-1"),
        _run_row("fix-accept-2", "fixer", "succeeded", env={"source_role": "acceptance"}),
    ]

    reviewer = _run(gate_decisions.compute_fix_budget(_ctx(runs), "reviewer"))
    acceptance = _run(gate_decisions.compute_fix_budget(_ctx(runs), "acceptance"))

    assert reviewer.succeeded == 1
    assert reviewer.attempts == 1
    assert reviewer.base_cap == 3
    assert acceptance.succeeded == 1
    assert acceptance.attempts == 2
    assert acceptance.base_cap == 2


def test_compute_fix_budget_research_bumps_cap_to_four_for_both_roles():
    runs = [
        _run_row("acc-1", "acceptance", env={"status": "issues"}),
        _run_row("fix-1", "fixer", "succeeded", env={"source_role": "acceptance"}),
    ]

    budget = _run(gate_decisions.compute_fix_budget(_ctx(runs, research_done=True), "acceptance"))

    assert budget.research_done is True
    assert budget.base_cap == 2
    assert budget.cap_limit == 4


def test_gate_context_window_slicing_preserves_old_gate_windows():
    runs = [_run_row(f"run-{i}", "qa") for i in range(50)]
    ctx = _ctx(runs)

    assert [r["id"] for r in ctx.runs_window(12)] == [f"run-{i}" for i in range(12)]
    assert [r["id"] for r in ctx.runs_window(20)] == [f"run-{i}" for i in range(20)]


def test_eval_qa_terminal_blocks_same_turn():
    ctx = _ctx(
        [_run_row(
            "qa-1",
            "qa",
            started_at="2026-01-01 12:01:00",
            env={"looks_like_change_request": False},
        )],
        latest_user_ts=datetime(2026, 1, 1, 12, 0, 0),
    )
    ctx.name = "read_file"

    decision = _run(gate_decisions.eval_qa_terminal(ctx))

    assert decision.action == "block"
    assert "ask_project (qa-1)" in decision.message
    assert "state=qa-terminal" in decision.log


def test_eval_uploaded_bootstrap_blocks_manual_tool():
    async def latest_task():
        return "Fix the import mismatch"

    ctx = _ctx([])
    ctx.name = "read_file"
    ctx.latest_workflow = {
        "id": "wf-upload",
        "mode": "fix_uploaded_project",
        "state": "fixing",
        "project_id": "proj-upload",
        "user_task": "Fix imports",
        "artifact_status": "not_ready",
        "cancel_requested": False,
    }
    ctx.latest_user_task_text = latest_task

    decision = _run(gate_decisions.eval_uploaded_bootstrap(ctx))

    assert decision.action == "block"
    assert "uploaded-project fixes must start with Aider" in decision.message
    assert "run_aider_fix(project_dir='/root/projects/proj-upload'" in decision.message
    assert "state=uploaded-project-bootstrap" in decision.log


def test_build_gate_context_fetches_each_shared_snapshot_once(monkeypatch):
    calls = {"conv": 0, "runs": 0, "workflow": 0, "v2": 0}

    async def get_conversation(conv_id):
        calls["conv"] += 1
        assert conv_id == "conv-1"
        return {
            "id": conv_id,
            "model_config_id": "mc-1",
            "messages": [{"role": "user", "created_at": "2026-01-01 12:00:00"}],
        }

    async def get_runs_by_conversation(conv_id, limit=0):
        calls["runs"] += 1
        assert conv_id == "conv-1"
        assert limit == 50
        return [_run_row("run-1", "reviewer")]

    async def get_latest_coder_workflow(conv_id):
        calls["workflow"] += 1
        assert conv_id == "conv-1"
        return {"id": "wf-1", "state": "reviewing"}

    async def is_v2(conv_id, conv_row=None):
        calls["v2"] += 1
        assert conv_id == "conv-1"
        assert conv_row["id"] == "conv-1"
        return True

    monkeypatch.setattr(gate_decisions.db, "get_conversation", get_conversation)
    monkeypatch.setattr(gate_decisions.db, "get_runs_by_conversation", get_runs_by_conversation)
    monkeypatch.setattr(gate_decisions.db, "get_latest_coder_workflow", get_latest_coder_workflow)

    ctx = _run(gate_decisions.build_gate_context("run_review", {}, "conv-1", is_v2))

    assert ctx.is_v2 is True
    assert ctx.latest_workflow["id"] == "wf-1"
    assert ctx.runs[0]["id"] == "run-1"
    assert calls == {"conv": 1, "runs": 1, "workflow": 1, "v2": 1}


def test_build_gate_context_skips_heavy_reads_for_non_v2(monkeypatch):
    calls = {"conv": 0, "runs": 0, "workflow": 0, "v2": 0}

    async def get_conversation(conv_id):
        calls["conv"] += 1
        assert conv_id == "conv-normal"
        return {"id": conv_id, "model_config_id": "mc-normal", "messages": []}

    async def get_runs_by_conversation(_conv_id, limit=0):
        calls["runs"] += 1
        raise AssertionError("non-v2 context must not fetch runs")

    async def get_latest_coder_workflow(_conv_id):
        calls["workflow"] += 1
        raise AssertionError("non-v2 context must not fetch workflow")

    async def is_v2(conv_id, conv_row=None):
        calls["v2"] += 1
        assert conv_id == "conv-normal"
        assert conv_row["id"] == "conv-normal"
        return False

    monkeypatch.setattr(gate_decisions.db, "get_conversation", get_conversation)
    monkeypatch.setattr(gate_decisions.db, "get_runs_by_conversation", get_runs_by_conversation)
    monkeypatch.setattr(gate_decisions.db, "get_latest_coder_workflow", get_latest_coder_workflow)

    ctx = _run(gate_decisions.build_gate_context("research", {}, "conv-normal", is_v2))

    assert ctx.is_v2 is False
    assert ctx.runs == []
    assert ctx.latest_workflow is None
    assert calls == {"conv": 1, "runs": 0, "workflow": 0, "v2": 1}


def test_build_gate_context_returns_partial_v2_context_on_snapshot_failure(monkeypatch):
    calls = {"conv": 0, "runs": 0, "workflow": 0, "v2": 0}

    async def get_conversation(conv_id):
        calls["conv"] += 1
        assert conv_id == "conv-partial"
        return {
            "id": conv_id,
            "model_config_id": "mc-v2",
            "messages": [{"role": "user", "created_at": "2026-01-01 12:00:00"}],
        }

    async def get_runs_by_conversation(_conv_id, limit=0):
        calls["runs"] += 1
        assert limit == 50
        raise RuntimeError("db unavailable")

    async def get_latest_coder_workflow(conv_id):
        calls["workflow"] += 1
        assert conv_id == "conv-partial"
        return {"id": "wf-partial", "state": "reviewing"}

    async def is_v2(conv_id, conv_row=None):
        calls["v2"] += 1
        assert conv_id == "conv-partial"
        assert conv_row["id"] == "conv-partial"
        return True

    async def research_since(_since):
        return True

    monkeypatch.setattr(gate_decisions.db, "get_conversation", get_conversation)
    monkeypatch.setattr(gate_decisions.db, "get_runs_by_conversation", get_runs_by_conversation)
    monkeypatch.setattr(gate_decisions.db, "get_latest_coder_workflow", get_latest_coder_workflow)

    ctx = _run(gate_decisions.build_gate_context(
        "read_file",
        {},
        "conv-partial",
        is_v2,
        research_since=research_since,
    ))

    assert ctx.is_v2 is True
    assert ctx.snapshot_partial is True
    assert ctx.runs == []
    assert ctx.latest_user_ts == datetime(2026, 1, 1, 12, 0, 0)
    assert ctx.latest_workflow["id"] == "wf-partial"
    assert _run(ctx.research_since(None)) is True
    assert calls == {"conv": 1, "runs": 1, "workflow": 1, "v2": 1}


def test_reconcile_workflow_state_logs_high_confidence_divergence(capsys):
    ctx = _ctx([
        _run_row("rev-clean", "reviewer", env={"status": "clean"}),
    ])
    ctx.latest_workflow = {"id": "wf-1", "state": "reviewing"}

    _run(gate_decisions.reconcile_workflow_state(ctx))

    captured = capsys.readouterr()
    assert "[wf-fsm] divergence" in captured.out
    assert "state=reviewing derived=accepting" in captured.out


def test_reconcile_workflow_state_ignores_terminal_workflows(capsys):
    ctx = _ctx([
        _run_row("rev-clean", "reviewer", env={"status": "clean"}),
    ])
    ctx.latest_workflow = {"id": "wf-1", "state": "cancelled"}

    _run(gate_decisions.reconcile_workflow_state(ctx))

    assert capsys.readouterr().out == ""
