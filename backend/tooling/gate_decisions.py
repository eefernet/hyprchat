"""Unit-testable decision helpers for the Daedalus workflow gate.

The ordered gate still lives in ``tools.exec_tool`` while this module takes
over the shared context and counter math. It intentionally does not import
``tools.py``; IO-heavy lookups are injected so the pure decision pieces can be
tested offline.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import database as db

from tooling.workflow_gate import (
    latest_user_msg_ts,
    parse_ts_loose,
    runs_since,
    uploaded_project_bootstrap_block_message,
    uploaded_project_manual_gate_state,
    uploaded_project_tool_allowed_during_bootstrap,
)


Run = dict[str, Any]
AsyncBoolResolver = Callable[[Any], Awaitable[bool]]
AsyncContextResolver = Callable[..., Awaitable[dict | None]]
AsyncStringResolver = Callable[[], Awaitable[str]]

FIX_ROLES = {"fixer", "aider.fix"}
FIX_TERMINAL_STATUSES = {"succeeded", "failed", "partial", "no_op"}


async def _default_false(_since: Any = None) -> bool:
    return False


async def _default_none(*_args: Any, **_kwargs: Any) -> dict | None:
    return None


async def _default_text() -> str:
    return ""


@dataclass(slots=True)
class GateContext:
    conv_id: str
    name: str
    args: dict[str, Any]
    runs: list[Run] = field(default_factory=list)
    latest_user_ts: datetime | None = None
    is_v2: bool = False
    latest_workflow: dict | None = None
    conv_row: dict | None = None
    snapshot_partial: bool = False
    aider_context_resolver: AsyncContextResolver = _default_none
    research_since: AsyncBoolResolver = _default_false
    ship_anyway: Callable[[], Awaitable[bool]] = _default_false
    latest_user_task_text: AsyncStringResolver = _default_text

    def runs_window(self, limit: int) -> list[Run]:
        """Preserve old per-gate windows while sharing one DB snapshot."""
        return self.runs[:limit]


@dataclass(slots=True)
class GateDecision:
    action: str
    message: str = ""
    event_status: str = ""
    event_icon: str = "code"
    log: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def block(cls, *, message: str, event_status: str,
              event_icon: str = "code", log: str = "") -> "GateDecision":
        return cls(
            action="block",
            message=message,
            event_status=event_status,
            event_icon=event_icon,
            log=log,
        )

    @classmethod
    def redirect(cls, *, tool: str, args: dict[str, Any],
                 event_status: str, event_icon: str = "wrench",
                 log: str = "") -> "GateDecision":
        return cls(
            action="redirect",
            tool=tool,
            args=args,
            event_status=event_status,
            event_icon=event_icon,
            log=log,
        )


@dataclass(slots=True)
class FixBudget:
    role: str
    succeeded: int
    attempts: int
    research_done: bool
    base_cap: int
    cap_limit: int


async def build_gate_context(
    name: str,
    args: dict[str, Any],
    conv_id: str,
    is_v2_resolver: Callable[..., Awaitable[bool]],
    *,
    aider_context_resolver: AsyncContextResolver = _default_none,
    research_since: AsyncBoolResolver = _default_false,
    ship_anyway: Callable[[], Awaitable[bool]] = _default_false,
    latest_user_task_text: AsyncStringResolver = _default_text,
) -> GateContext:
    """Build the single per-call snapshot used by gate evaluators."""
    args = args or {}
    conv_row = None
    runs: list[Run] = []
    latest_workflow = None
    latest_user_ts = None
    is_v2 = False
    snapshot_partial = False
    if conv_id:
        try:
            conv_row = await db.get_conversation(conv_id)
            is_v2 = await is_v2_resolver(conv_id, conv_row=conv_row)
        except Exception as exc:
            print(f"[v2-gate] context base lookup failed (non-fatal): {exc}")
            snapshot_partial = True
            try:
                is_v2 = await is_v2_resolver(conv_id, conv_row=None)
            except Exception as inner_exc:
                print(f"[v2-gate] context v2 fallback failed (non-fatal): {inner_exc}")
                is_v2 = False
        if is_v2:
            try:
                runs = await db.get_runs_by_conversation(conv_id, limit=50)
            except Exception as exc:
                print(f"[v2-gate] context runs snapshot failed (non-fatal): {exc}")
                snapshot_partial = True
                runs = []
            try:
                latest_user_ts = await latest_user_msg_ts(conv_id, conv_row=conv_row)
            except Exception as exc:
                print(f"[v2-gate] context latest-user snapshot failed (non-fatal): {exc}")
                snapshot_partial = True
                latest_user_ts = None
            try:
                latest_workflow = await db.get_latest_coder_workflow(conv_id)
            except Exception as exc:
                print(f"[v2-gate] context workflow snapshot failed (non-fatal): {exc}")
                snapshot_partial = True
                latest_workflow = None
    return GateContext(
        conv_id=conv_id,
        name=name,
        args=args,
        runs=runs,
        latest_user_ts=latest_user_ts,
        is_v2=is_v2,
        latest_workflow=latest_workflow,
        conv_row=conv_row,
        snapshot_partial=snapshot_partial,
        aider_context_resolver=aider_context_resolver,
        research_since=research_since,
        ship_anyway=ship_anyway,
        latest_user_task_text=latest_user_task_text,
    )


def fixer_source_role(run: Run, run_role_by_id: dict[str, str]) -> str:
    env = run.get("result_envelope") or {}
    return (
        env.get("source_role")
        or run_role_by_id.get(run.get("parent_run_id"))
        or "reviewer"
    )


def infer_fix_parent_role(ctx: GateContext) -> str:
    """Infer whether a pending fix is reviewer- or acceptance-driven."""
    run_role_by_id = {r.get("id"): r.get("role") for r in ctx.runs}
    requested_parent_id = (ctx.args.get("reviewer_run_id") or "").strip()
    parent_role = ""
    if requested_parent_id:
        parent_role = run_role_by_id.get(requested_parent_id, "")
    if not parent_role:
        for run in ctx.runs:
            if run.get("role") not in {"reviewer", "acceptance"}:
                continue
            env = run.get("result_envelope") or {}
            if (env.get("status") or "").lower() in {"issues", "error"}:
                parent_role = run.get("role", "reviewer")
                break
    return parent_role or "reviewer"


async def compute_fix_budget(ctx: GateContext, role: str) -> FixBudget:
    """Shared fixer/Aider cycle budget for the current user request.

    ``succeeded`` and ``attempts`` intentionally differ:
    succeeded gates cycle caps, while terminal attempts release or exhaust
    post-review states after a fixer/Aider pass declined or failed.
    """
    role = role or "reviewer"
    run_role_by_id = {r.get("id"): r.get("role") for r in ctx.runs}
    window = runs_since(ctx.runs, ctx.latest_user_ts)
    succeeded = 0
    attempts = 0
    for run in window:
        if run.get("role") not in FIX_ROLES:
            continue
        if fixer_source_role(run, run_role_by_id) != role:
            continue
        status = (run.get("status") or "").lower()
        if status == "succeeded":
            succeeded += 1
        if status in FIX_TERMINAL_STATUSES:
            attempts += 1
    research_done = await ctx.research_since(ctx.latest_user_ts)
    base_cap = 2 if role == "acceptance" else 3
    cap_limit = 4 if research_done else base_cap
    return FixBudget(
        role=role,
        succeeded=succeeded,
        attempts=attempts,
        research_done=research_done,
        base_cap=base_cap,
        cap_limit=cap_limit,
    )


def issue_signatures(envelope: dict) -> set[tuple[str, str]]:
    sigs: set[tuple[str, str]] = set()
    for issue in (envelope or {}).get("issues") or []:
        path = (issue.get("file") or "").strip()
        summary = (issue.get("summary") or "").strip().lower()[:60]
        if path and summary:
            sigs.add((path, summary))
    return sigs


async def evaluate_gate(_ctx: GateContext) -> GateDecision | None:
    for evaluator in (
        eval_qa_terminal,
        eval_uploaded_bootstrap,
    ):
        decision = await evaluator(_ctx)
        if decision is not None:
            return decision
    return None


async def eval_qa_terminal(ctx: GateContext) -> GateDecision | None:
    if not ctx.conv_id or not ctx.is_v2:
        return None
    if ctx.name in {"ask_project", "get_coder_workflow", "cancel_coder_workflow"}:
        return None

    terminal_qa_run = None
    for run in ctx.runs_window(12):
        role = run.get("role", "")
        if role == "qa":
            env = run.get("result_envelope") or {}
            if (run.get("status") == "succeeded"
                    and not env.get("looks_like_change_request", False)):
                terminal_qa_run = run
            break
        if (role in {"reviewer", "acceptance", "fixer", "aider.fix"}
                or role.startswith("builder")):
            break

    if terminal_qa_run is not None:
        qa_ts = parse_ts_loose(terminal_qa_run.get("started_at"))
        if (ctx.latest_user_ts is not None and qa_ts is not None
                and qa_ts < ctx.latest_user_ts):
            terminal_qa_run = None

    if terminal_qa_run is None:
        return None

    qid = terminal_qa_run.get("id", "?")
    return GateDecision.block(
        message=(
            f"BLOCKED — ask_project ({qid}) just answered the user's question. "
            f"The user asked something; you have the answer. Your VERY NEXT output "
            f"MUST be plain text relaying that answer to the user.\n\n"
            f"Do NOT call run_review, run_fixer, run_aider_fix, generate_code, "
            f"read_file, write_file, run_shell, download_project, or any other "
            f"tool. The project Q&A path is read-only.\n\n"
            f"If the user follows up with another question, call ask_project again. "
            f"If they explicitly request a change, route that new turn through "
            f"fix_uploaded_project or a write workflow."
        ),
        event_status=f"⛔ Blocked — answer the user (ask_project {qid[:14]}… is terminal)",
        event_icon="code",
        log=(f"[v2-gate] state=qa-terminal blocked tool={ctx.name} "
             f"trigger={qid}"),
    )


async def eval_uploaded_bootstrap(ctx: GateContext) -> GateDecision | None:
    if not ctx.conv_id or not ctx.is_v2:
        return None
    if uploaded_project_tool_allowed_during_bootstrap(ctx.name):
        return None
    gate_state = uploaded_project_manual_gate_state(
        ctx.latest_workflow,
        ctx.runs_window(20),
    )
    if not gate_state:
        return None
    project_id = (ctx.latest_workflow or {}).get("project_id") or ""
    project_dir = f"/root/projects/{project_id}" if project_id else ""
    task = await ctx.latest_user_task_text()
    body = uploaded_project_bootstrap_block_message(
        ctx.name,
        ctx.latest_workflow or {},
        project_dir,
        task,
        gate_state,
    )
    return GateDecision.block(
        message=body,
        event_status=(
            "⛔ Blocked — uploaded-project fixes use Aider first"
            if gate_state == "bootstrap"
            else "⛔ Blocked — uploaded-project run already in progress"
        ),
        event_icon="code",
        log=(f"[v2-gate] state=uploaded-project-{gate_state} "
             f"blocked tool={ctx.name} workflow={(ctx.latest_workflow or {}).get('id','?')}"),
    )


FSM_TERMINAL_STATES = {
    "accepted",
    "blocked",
    "cancelled",
    "complete",
    "completed",
    "delivered",
}


def derive_workflow_phase(ctx: GateContext) -> str:
    """Best-effort FSM phase derived from recent run history.

    This deliberately returns an empty string for ambiguous cases. The first
    reconciliation pass is log-only, so false positives are more harmful than
    missing a low-confidence divergence.
    """
    for run in ctx.runs_window(20):
        role = run.get("role", "")
        status = (run.get("status") or "").lower()
        env = run.get("result_envelope") or {}
        env_status = (env.get("status") or "").lower()
        if role == "qa":
            return ""
        if role == "reviewer":
            if env_status in {"issues", "error"}:
                return "fixing"
            if env_status == "clean":
                return "accepting"
            return ""
        if role == "acceptance":
            if env_status in {"issues", "error"}:
                return "fixing"
            if env_status in {"accepted", "clean"}:
                return "accepted"
            return ""
        if role in FIX_ROLES and status in FIX_TERMINAL_STATUSES:
            if env.get("source_role") == "acceptance" and env.get("docs_only"):
                return "accepting"
            return "reviewing"
        if role.startswith("builder"):
            if status == "succeeded":
                return "reviewing"
            if status in {"partial", "stuck"} and env.get("manifest_missing"):
                return "building"
            if status == "failed":
                return "reviewing"
            return ""
    return ""


async def reconcile_workflow_state(ctx: GateContext) -> None:
    """Log divergence between FSM state and the derived gate phase.

    Autocorrection is intentionally deferred; this function is non-fatal and
    write-free so behavior stays unchanged while we collect real-run evidence.
    """
    try:
        wf = ctx.latest_workflow or {}
        actual = (wf.get("state") or "").lower()
        if not wf or not actual or actual in FSM_TERMINAL_STATES:
            return
        derived = derive_workflow_phase(ctx)
        if not derived or derived == actual:
            return
        print(
            f"[wf-fsm] divergence conv={ctx.conv_id} workflow={wf.get('id', '?')} "
            f"state={actual} derived={derived} tool={ctx.name}",
            flush=True,
        )
    except Exception as exc:
        print(f"[wf-fsm] reconcile failed (non-fatal): {exc}", flush=True)
