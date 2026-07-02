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


# ─── progress_verdict — file-path based, reword-proof ───────────────────────


def _issues_env(*files, status="issues"):
    return {"status": status,
            "issues": [{"file": f, "summary": f"problem in {f}"} for f in files]}


def test_progress_verdict_same_file_reworded_summary_is_no_progress():
    # The empirical failure case: acceptance rewords the summary each round
    # ("lists NumPy as a requirement" vs "claims NumPy is used") but the file
    # is the same — that is NOT progress.
    src = {"status": "issues", "issues": [
        {"file": "README.md", "summary": "The README lists NumPy as a requirement"}]}
    ver = {"status": "issues", "issues": [
        {"file": "README.md", "summary": "The README claims NumPy is used for math"}]}
    assert gate_decisions.progress_verdict(src, ver) == "no_progress"


def test_progress_verdict_disjoint_files_is_progress():
    assert gate_decisions.progress_verdict(
        _issues_env("a.py"), _issues_env("b.py")) == "progress"


def test_progress_verdict_clean_or_accepted_is_progress():
    assert gate_decisions.progress_verdict(
        _issues_env("a.py"), {"status": "clean", "issues": []}) == "progress"
    assert gate_decisions.progress_verdict(
        _issues_env("a.py"), {"status": "accepted", "issues": []}) == "progress"


def test_progress_verdict_basename_matches_nested_path():
    assert gate_decisions.progress_verdict(
        _issues_env("cli.py"), _issues_env("src/cli.py")) == "no_progress"
    assert gate_decisions.progress_verdict(
        _issues_env("src/cli.py"), _issues_env("cli.py")) == "no_progress"


def test_progress_verdict_count_fallback_when_files_missing():
    src = {"status": "error", "issues": [{"summary": "boom"}, {"summary": "bang"}]}
    fewer = {"status": "error", "issues": [{"summary": "boom"}]}
    same = {"status": "error", "issues": [{"summary": "x"}, {"summary": "y"}]}
    assert gate_decisions.progress_verdict(src, fewer) == "progress"
    assert gate_decisions.progress_verdict(src, same) == "no_progress"


def test_progress_verdict_missing_source_is_no_progress():
    assert gate_decisions.progress_verdict(None, _issues_env("a.py")) == "no_progress"
    assert gate_decisions.progress_verdict({}, {"status": "clean"}) == "no_progress"


def test_issue_scoped_files_unions_file_and_fix_scope():
    env = {"issues": [
        {"file": "src/app.py", "suggested_fix_scope": ["README.md", "./cli.py"]},
        {"file": "", "suggested_fix_scope": "tests/test_app.py"},
    ]}
    assert gate_decisions.issue_scoped_files(env) == {
        "src/app.py", "README.md", "cli.py", "tests/test_app.py"}


def test_normalize_issue_path_strips_project_dir_and_dots():
    norm = gate_decisions.normalize_issue_path
    assert norm("/root/projects/x/app.py", "/root/projects/x") == "app.py"
    assert norm("./src/cli.py") == "src/cli.py"
    assert norm("src\\cli.py") == "src/cli.py"


# ─── compute_fix_battle — the streak walk ────────────────────────────────────
#
# Helpers build runs in CHRONOLOGICAL order; the DB returns newest-first, so
# _battle() reverses before calling.


def _battle(chronological_runs, latest_user_ts=None):
    return gate_decisions.compute_fix_battle(
        list(reversed(chronological_runs)), latest_user_ts)


def _seq(*specs):
    """Build chronological runs: ('rev', files...) / ('acc', ...) /
    ('aider'|'fixer', parent_id) / ('clean',) / raw dicts pass through."""
    out = []
    for i, spec in enumerate(specs):
        ts = f"2026-01-02 00:{i:02d}:00"
        if isinstance(spec, dict):
            spec = {**spec, "started_at": ts}
            out.append(spec)
            continue
        kind, *rest = spec
        rid = f"{kind}-{i}"
        if kind in ("rev", "acc"):
            role = "reviewer" if kind == "rev" else "acceptance"
            out.append(_run_row(rid, role, started_at=ts, env=_issues_env(*rest)))
        elif kind == "clean":
            role = rest[0] if rest else "reviewer"
            status = "accepted" if role == "acceptance" else "clean"
            out.append(_run_row(rid, role, started_at=ts,
                                env={"status": status, "issues": []}))
        elif kind in ("aider", "fixer"):
            role = "aider.fix" if kind == "aider" else "fixer"
            parent = rest[0] if rest else ""
            out.append(_run_row(rid, role, "succeeded", started_at=ts,
                                parent_run_id=parent))
        else:
            raise AssertionError(f"unknown spec {spec}")
    return out


def test_battle_streak_counts_no_progress_across_editors_and_roles():
    runs = _seq(
        ("rev", "a.py"),
        ("aider", "rev-0"),
        ("rev", "a.py"),        # verification: same file → no progress
        ("fixer", "rev-2"),
        ("rev", "a.py"),        # still same file → no progress
        ("acc", "a.py"),
        ("aider", "acc-5"),
        ("acc", "a.py"),        # acceptance-driven no progress too
    )
    state = _battle(runs)
    assert state.total_attempts == 3
    assert state.no_progress_streak == 3
    assert state.streak_editors == ["aider.fix", "fixer", "aider.fix"]


def test_battle_resets_streak_on_verified_progress():
    runs = _seq(
        ("rev", "a.py"),
        ("aider", "rev-0"),
        ("rev", "a.py"),        # no progress
        ("aider", "rev-2"),
        ("rev", "b.py"),        # a.py resolved → progress, new battle
        ("fixer", "rev-4"),
        ("rev", "b.py"),        # no progress on b.py
    )
    state = _battle(runs)
    assert state.total_attempts == 3      # ceiling counter never resets
    assert state.no_progress_streak == 1  # only the b.py attempt
    assert state.streak_editors == ["fixer"]


def test_battle_clean_verification_resets_streak():
    runs = _seq(
        ("rev", "a.py"),
        ("aider", "rev-0"),
        ("clean",),
    )
    state = _battle(runs)
    assert state.total_attempts == 1
    assert state.no_progress_streak == 0


def test_battle_pending_verification_flagged_not_counted():
    runs = _seq(
        ("rev", "a.py"),
        ("aider", "rev-0"),
    )
    state = _battle(runs)
    assert state.total_attempts == 1
    assert state.no_progress_streak == 0
    assert state.pending_verification is True


def test_battle_source_resolution_falls_back_to_nearest_older_issue_run():
    # Fix run with no parent_run_id still resolves its source envelope from
    # the nearest older same-role issue run.
    runs = _seq(
        ("rev", "a.py"),
        ("aider",),           # no parent id
        ("rev", "a.py"),
    )
    state = _battle(runs)
    assert state.no_progress_streak == 1


def test_battle_env_fault_verification_is_skipped():
    fault = _run_row("rev-fault", "reviewer", env={
        "status": "error", "deterministic_issue": "environment_fault",
        "issues": [{"file": "a.py", "summary": "sandbox broken"}]})
    runs = _seq(
        ("rev", "a.py"),
        ("aider", "rev-0"),
        fault,                # proves nothing about the fix
    )
    state = _battle(runs)
    assert state.no_progress_streak == 0
    assert state.pending_verification is True


def test_battle_turn_scoped_to_latest_user_message():
    runs = _seq(
        ("rev", "a.py"),
        ("aider", "rev-0"),
        ("rev", "a.py"),
    )
    latest_user = datetime(2026, 1, 2, 0, 30, 0)  # after all runs
    state = _battle(runs, latest_user_ts=latest_user)
    assert state.total_attempts == 0
    assert state.no_progress_streak == 0


def test_battle_partial_and_failed_count_as_attempts_never_progress():
    partial = _run_row("aider-p", "aider.fix", "partial", parent_run_id="rev-0")
    failed = _run_row("fixer-f", "fixer", "failed", parent_run_id="rev-0")
    runs = _seq(
        ("rev", "a.py"),
        partial,
        ("rev", "a.py"),
        failed,
        ("rev", "a.py"),
    )
    state = _battle(runs)
    assert state.total_attempts == 2
    assert state.no_progress_streak == 2


def test_battle_acceptance_fix_verified_by_next_acceptance_not_review():
    # Acceptance-driven fix followed by a clean reviewer run must NOT count
    # as progress — the acceptance re-run is the verification.
    runs = _seq(
        ("acc", "README.md"),
        ("aider", "acc-0"),
        ("clean", "reviewer"),      # intermediate auto-review, ignored
        ("acc", "README.md"),       # acceptance still flags README
    )
    state = _battle(runs)
    assert state.no_progress_streak == 1


# ─── preferred_fix_editor — 2-and-2 escalation blocks ────────────────────────


def _state(editors):
    s = gate_decisions.FixBattleState()
    s.no_progress_streak = len(editors)
    s.streak_editors = list(editors)
    return s


def test_preferred_editor_defaults_to_aider():
    assert gate_decisions.preferred_fix_editor(_state([])) == "aider"
    assert gate_decisions.preferred_fix_editor(_state(["aider.fix"])) == "aider"


def test_preferred_editor_escalates_after_two_aider_no_progress():
    assert gate_decisions.preferred_fix_editor(
        _state(["aider.fix", "aider.fix"])) == "fixer"


def test_preferred_editor_gives_fixer_a_two_attempt_block_then_alternates():
    assert gate_decisions.preferred_fix_editor(
        _state(["aider.fix", "aider.fix", "fixer"])) == "fixer"
    assert gate_decisions.preferred_fix_editor(
        _state(["aider.fix", "aider.fix", "fixer", "fixer"])) == "aider"


def test_preferred_editor_reset_returns_to_aider():
    # Progress reset empties the streak — back to Aider-first.
    assert gate_decisions.preferred_fix_editor(_state([])) == "aider"


def test_battle_thresholds_are_wired():
    assert gate_decisions.NO_PROGRESS_RESEARCH_AT == 4
    assert gate_decisions.NO_PROGRESS_LAST_SHOT == 5
    assert gate_decisions.FIX_ATTEMPT_HARD_CEILING == 25


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


# ─── Environment-fault envelope routing ─────────────────────────────────────

from tooling import workflow_gate  # noqa: E402


def _env_fault_envelope():
    return {
        "status": "error",
        "deterministic_issue": "environment_fault",
        "summary": ("Sandbox environment fault: the build phase failed because "
                    "`/root/pip-constraints.txt` does not exist on the Codebox "
                    "and no project file references it."),
        "issues": [{"severity": "environment", "file": "",
                    "suggested_fix_scope": []}],
    }


def test_is_environment_fault_matches_only_the_marker():
    assert workflow_gate.is_environment_fault(_env_fault_envelope())
    assert not workflow_gate.is_environment_fault({"status": "error"})
    assert not workflow_gate.is_environment_fault(
        {"status": "issues", "deterministic_issue": "dependency_install_failure"})
    assert not workflow_gate.is_environment_fault(None)


def test_environment_fault_notice_reports_and_forbids_fix_tools():
    notice = workflow_gate.environment_fault_notice(_env_fault_envelope())
    assert "SANDBOX ENVIRONMENT FAULT" in notice
    assert "/root/pip-constraints.txt" in notice
    assert "run_aider_fix" in notice and "deep_research" in notice
    assert "run_review" in notice  # the recovery path stays explicit


def test_env_fault_workflow_event_parks_blocked():
    state, artifact = workflow_gate.WF_EVENT_TRANSITIONS["REVIEW_ENV_FAULT"]
    assert state == "blocked"
    assert artifact == "not_ready"
