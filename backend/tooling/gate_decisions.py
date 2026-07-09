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
    is_environment_fault,
    latest_user_msg_ts,
    parse_ts_loose,
    runs_since,
    uploaded_project_bootstrap_block_message,
    uploaded_project_manual_gate_state,
    uploaded_project_tool_allowed_during_bootstrap,
)


Run = dict[str, Any]
AsyncBoolResolver = Callable[[Any], Awaitable[bool]]
AsyncStringResolver = Callable[[], Awaitable[str]]

FIX_ROLES = {"fixer", "aider.fix"}
FIX_TERMINAL_STATUSES = {"succeeded", "failed", "partial", "no_op"}

# Progress-based fix budget: consecutive fix attempts without verified
# progress force deep_research at 4, allow one post-research attempt at 5,
# and a hard ceiling of total attempts per user request backstops loops
# where each fix resolves old issues but introduces new ones.
NO_PROGRESS_RESEARCH_AT = 4
NO_PROGRESS_LAST_SHOT = 5
FIX_ATTEMPT_HARD_CEILING = 25

_ISSUE_ENV_STATUSES = {"issues", "error"}
_VERIFY_ENV_STATUSES = {"issues", "error", "clean", "accepted"}


async def _default_false(_since: Any = None) -> bool:
    return False


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
class FixBattleState:
    """Progress-scored fix budget for the current user request.

    ``no_progress_streak`` counts consecutive fix attempts (Aider + Fixer,
    reviewer- and acceptance-driven combined) whose follow-up verification
    still reported the same trouble; verified progress resets it.
    ``total_attempts`` never resets within a turn and backs the hard ceiling.
    """
    total_attempts: int = 0
    no_progress_streak: int = 0
    # Roles ("aider.fix"/"fixer") of the current streak's attempts, oldest
    # first — drives the Aider→Fixer escalation policy.
    streak_editors: list[str] = field(default_factory=list)
    pending_verification: bool = False
    research_done: bool = False

    def _trailing(self, role: str) -> int:
        n = 0
        for r in reversed(self.streak_editors):
            if r != role:
                break
            n += 1
        return n

    @property
    def trailing_aider(self) -> int:
        return self._trailing("aider.fix")

    @property
    def trailing_fixer(self) -> int:
        return self._trailing("fixer")


async def build_gate_context(
    name: str,
    args: dict[str, Any],
    conv_id: str,
    is_v2_resolver: Callable[..., Awaitable[bool]],
    *,
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


def normalize_issue_path(path: str, project_dir: str = "") -> str:
    """Normalize an issue file path for cross-envelope comparison."""
    p = (path or "").strip().replace("\\", "/")
    if not p:
        return ""
    if project_dir:
        pd = project_dir.strip().replace("\\", "/").rstrip("/")
        if pd and (p == pd or p.startswith(pd + "/")):
            p = p[len(pd):]
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/").rstrip("/")


def envelope_issue_files(envelope: dict | None, project_dir: str = "") -> set[str]:
    files: set[str] = set()
    for issue in (envelope or {}).get("issues") or []:
        p = normalize_issue_path(str(issue.get("file") or ""), project_dir)
        if p:
            files.add(p)
    return files


def issue_scoped_files(envelope: dict | None, project_dir: str = "") -> set[str]:
    """Files an issue envelope points at: issue["file"] + suggested_fix_scope."""
    files = envelope_issue_files(envelope, project_dir)
    for issue in (envelope or {}).get("issues") or []:
        scope = issue.get("suggested_fix_scope") or []
        if isinstance(scope, str):
            scope = [scope]
        for entry in scope:
            p = normalize_issue_path(str(entry or ""), project_dir)
            if p:
                files.add(p)
    return files


def paths_overlap(a: set[str], b: set[str]) -> bool:
    """Normalized-path equality, plus basename matching when either side is a
    bare basename — envelopes often cite "cli.py" where the other says
    "src/cli.py"."""
    if a & b:
        return True
    bases_a = {p.rsplit("/", 1)[-1] for p in a}
    bases_b = {p.rsplit("/", 1)[-1] for p in b}
    if any("/" not in p and p in bases_b for p in a):
        return True
    if any("/" not in p and p in bases_a for p in b):
        return True
    return False


def progress_verdict(src_env: dict | None, ver_env: dict | None) -> str:
    """Did a fix attempt verifiably resolve the issues that triggered it?

    Compares issue FILE PATHS between the triggering envelope and the
    follow-up verification — never summary text, which reviewers reword
    between rounds. Returns "progress" or "no_progress".
    """
    if not src_env:
        # An attempt that can't demonstrate what it targeted earns no reset.
        return "no_progress"
    ver_status = ((ver_env or {}).get("status") or "").lower()
    if ver_status in {"clean", "accepted"}:
        return "progress"
    src_files = envelope_issue_files(src_env)
    ver_files = envelope_issue_files(ver_env)
    if src_files and ver_files:
        # New issues confined to new files start a fresh battle; the hard
        # ceiling backstops fix-old/create-new loops.
        return "no_progress" if paths_overlap(src_files, ver_files) else "progress"
    src_n = len((src_env or {}).get("issues") or [])
    ver_n = len((ver_env or {}).get("issues") or [])
    if src_n > 0 and ver_n < src_n:
        return "progress"
    return "no_progress"


def compute_fix_battle(runs: list[Run], latest_user_ts) -> FixBattleState:
    """Score the current turn's fix attempts by verified progress.

    For each terminal fix run: resolve the envelope that triggered it
    (parent_run_id, else nearest older same-source-role issue run) and the
    verification that followed it (nearest newer same-source-role run), then
    apply ``progress_verdict``. Reviewer-driven fixes are verified by the
    auto run_review; acceptance-driven fixes by the next acceptance run.
    Environment-fault envelopes prove nothing and are skipped on both sides.
    """
    state = FixBattleState()
    ordered = list(reversed(runs_since(runs, latest_user_ts)))  # oldest first
    run_role_by_id = {r.get("id"): r.get("role") for r in ordered}
    run_by_id = {r.get("id"): r for r in ordered}

    fix_indices = [
        i for i, r in enumerate(ordered)
        if r.get("role") in FIX_ROLES
        and (r.get("status") or "").lower() in FIX_TERMINAL_STATUSES
    ]
    for idx in fix_indices:
        fix_run = ordered[idx]
        source_role = fixer_source_role(fix_run, run_role_by_id)

        src_env = None
        parent = run_by_id.get(fix_run.get("parent_run_id"))
        if parent is not None:
            penv = parent.get("result_envelope") or {}
            if (not is_environment_fault(penv)
                    and (penv.get("status") or "").lower() in _ISSUE_ENV_STATUSES):
                src_env = penv
        if src_env is None:
            for j in range(idx - 1, -1, -1):
                r = ordered[j]
                if r.get("role") != source_role:
                    continue
                env = r.get("result_envelope") or {}
                if is_environment_fault(env):
                    continue
                if (env.get("status") or "").lower() in _ISSUE_ENV_STATUSES:
                    src_env = env
                break

        ver_env = None
        for j in range(idx + 1, len(ordered)):
            r = ordered[j]
            if r.get("role") != source_role:
                continue
            env = r.get("result_envelope") or {}
            if is_environment_fault(env):
                continue
            if (env.get("status") or "").lower() in _VERIFY_ENV_STATUSES:
                ver_env = env
                break

        state.total_attempts += 1
        if ver_env is None:
            if idx == fix_indices[-1]:
                state.pending_verification = True
            continue
        if progress_verdict(src_env, ver_env) == "progress":
            state.no_progress_streak = 0
            state.streak_editors = []
        else:
            state.no_progress_streak += 1
            state.streak_editors.append(fix_run.get("role") or "")
    return state


def preferred_fix_editor(state: FixBattleState) -> str:
    """Aider-first with escalation: after 2 consecutive no-progress Aider
    attempts route to the in-house Fixer, give it a 2-attempt block, then
    alternate back."""
    if state.trailing_aider >= 2:
        return "fixer"
    if 0 < state.trailing_fixer < 2:
        return "fixer"
    return "aider"


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
