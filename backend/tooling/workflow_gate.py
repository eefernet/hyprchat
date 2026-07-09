"""Shared helpers for Daedalus workflow gating.

The main tool dispatcher still owns the ordered gate decisions, but these
helpers keep timestamp windows, workflow state transitions, uploaded-project
bootstrap checks, and the in-memory research handoff in one place.
"""
from __future__ import annotations

import os
import re
import time
import uuid
import json
import calendar
from collections import OrderedDict
from datetime import datetime

import config
import database as db


def parse_ts_loose(s) -> "datetime | None":
    """Parse Python isoformat and SQLite CURRENT_TIMESTAMP strings."""
    if not s:
        return None
    s = str(s).strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


async def latest_user_msg_ts(conv_id: str, conv_row: dict | None = None):
    """Timestamp of the newest persisted user message, or None."""
    try:
        conv = conv_row or await db.get_conversation(conv_id)
        for m in reversed((conv or {}).get("messages") or []):
            if m.get("role") == "user":
                return parse_ts_loose(m.get("created_at"))
    except Exception as e:
        print(f"[v2-gate] latest-user-msg lookup failed (non-fatal): {e}")
    return None


def runs_since(runs: list, ts) -> list:
    """Runs whose started_at >= ts. ts=None keeps all runs."""
    if ts is None:
        return runs
    out = []
    for r in runs:
        rts = parse_ts_loose(r.get("started_at"))
        if rts is None or rts >= ts:
            out.append(r)
    return out


def v2_name_match(name: str) -> bool:
    """Match v2/Daedalus persona names without matching unrelated substrings."""
    n = (name or "").lower()
    return "daedalus" in n or re.search(r"\bv2\b", n) is not None


async def is_v2_persona(conv_id: str, conv_row: dict | None = None) -> bool:
    """Return True iff this conversation is using the v2 coding persona.

    Pass conv_row if the caller already fetched it. Failures are non-fatal and
    return False so the gate stays off instead of misfiring on a normal chat.
    """
    try:
        if conv_row is None:
            conv_row = await db.get_conversation(conv_id)
        mc_id = (conv_row or {}).get("model_config_id") if conv_row else None
        if not mc_id:
            return False
        mc = await db.get_model_config(mc_id)
        return bool(mc and v2_name_match(mc.get("name") or ""))
    except Exception as e:
        print(f"[v2-gate] persona lookup failed (non-fatal): {e}")
        return False


async def deep_research_called_since(conv_id: str, since_iso: str | None) -> bool:
    """Return True iff deep_research has run since the trigger timestamp."""
    try:
        cached = get_recent_research(conv_id) if conv_id else None
        if cached:
            if since_iso:
                since_dt = parse_ts_loose(since_iso)
                if since_dt:
                    since_unix = (
                        calendar.timegm(since_dt.timetuple())
                        + since_dt.microsecond / 1_000_000
                    )
                    if cached.get("ts", 0) >= since_unix:
                        return True
            else:
                return True
    except Exception as e:
        print(f"[v2-gate] in-stream research cache check failed (non-fatal): {e}")

    if not since_iso:
        return False
    try:
        conv = await db.get_conversation(conv_id)
        msgs = (conv or {}).get("messages") or []
        since_ts = parse_ts_loose(since_iso)
        for m in reversed(msgs):
            if m.get("role") != "assistant":
                continue
            m_ts = parse_ts_loose(m.get("created_at"))
            if since_ts and m_ts and m_ts < since_ts:
                return False
            md = m.get("metadata") or {}
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except Exception:
                    md = {}
            for ev in md.get("saved_events") or []:
                if ev.get("type") != "tool_start":
                    continue
                if (ev.get("data") or {}).get("tool") == "deep_research":
                    return True
        return False
    except Exception as e:
        print(f"[v2-gate] deep_research history check failed (non-fatal): {e}")
        return False


async def prior_fix_attempts_context(conv_id: str, *, max_attempts: int = 3) -> str:
    """Compact history of fix attempts since the latest user message."""
    if not conv_id:
        return ""
    try:
        runs = await db.get_runs_by_conversation(conv_id, limit=50)
        uts = await latest_user_msg_ts(conv_id)
        attempts = [
            r for r in runs_since(runs, uts)
            if (r.get("role") in {"fixer", "aider.fix"}
                and r.get("status") in {"succeeded", "partial", "failed"})
        ]
        if not attempts:
            return ""
        lines = []
        for r in reversed(attempts[:max_attempts]):
            env = r.get("result_envelope") or {}
            files = ", ".join(
                os.path.basename(f) or f for f in (env.get("files_touched") or [])[:6]
            ) or "(no files written)"
            summary = (env.get("summary") or "")[:160]
            errs = "; ".join(str(e) for e in (env.get("errors") or [])[:2])[:200]
            lines.append(
                f"- {r.get('role')} [{env.get('status') or r.get('status')}] "
                f"touched: {files}. {summary}"
                + (f" Errors: {errs}" if errs else "")
            )
        return "\n".join(lines)
    except Exception as e:
        print(f"[v2-gate] prior-attempt context failed (non-fatal): {e}")
        return ""


async def prior_acceptance_issues_context(conv_id: str) -> str:
    """Most recent acceptance verdict for this user request."""
    if not conv_id:
        return ""
    try:
        runs = await db.get_runs_by_conversation(conv_id, limit=50)
        uts = await latest_user_msg_ts(conv_id)
        for r in runs_since(runs, uts):
            if r.get("role") != "acceptance":
                continue
            env = r.get("result_envelope") or {}
            if (env.get("status") or "").lower() not in {"issues", "error"}:
                return ""
            lines = []
            for i in (env.get("issues") or [])[:6]:
                lines.append(
                    f"- [{i.get('category', i.get('severity', '?'))}] "
                    f"{i.get('file', '')}: {(i.get('summary') or '')[:140]}"
                )
            return "\n".join(lines)
        return ""
    except Exception as e:
        print(f"[v2-gate] prior-acceptance context failed (non-fatal): {e}")
        return ""


async def fix_budget_note(conv_id: str, source_role: str) -> str:
    """One-line fix-cycle budget readout for the current user request."""
    # Function-level import: gate_decisions imports this module at load time.
    from tooling.gate_decisions import (
        FIX_ATTEMPT_HARD_CEILING,
        NO_PROGRESS_LAST_SHOT,
        NO_PROGRESS_RESEARCH_AT,
        compute_fix_battle,
    )
    try:
        runs = await db.get_runs_by_conversation(conv_id, limit=50)
        uts = await latest_user_msg_ts(conv_id)
        battle = compute_fix_battle(runs, uts)
        return (
            f"\n\nFix-cycle budget: {battle.no_progress_streak} consecutive "
            f"attempt(s) without verified progress (research forced at "
            f"{NO_PROGRESS_RESEARCH_AT}, last automated attempt at "
            f"{NO_PROGRESS_LAST_SHOT}); {battle.total_attempts}/"
            f"{FIX_ATTEMPT_HARD_CEILING} total attempts this request. "
            f"Verified progress resets the counter."
        )
    except Exception:
        return ""


WF_EVENT_TRANSITIONS = {
    "PLAN_DONE":     ("planning",  "not_ready"),
    "BUILD_STARTED": ("building",  "not_ready"),
    "BUILD_OK":      ("reviewing", "not_ready"),
    "FIX_APPLIED":   ("reviewing", "not_ready"),
    "REVIEW_CLEAN":  ("accepting", "not_ready"),
    "REVIEW_ISSUES": ("fixing",    "not_ready"),
    # Sandbox environment fault: nothing in the project to fix — the workflow
    # parks as blocked (non-polling terminal for WorkflowCard) until the user
    # remediates the Codebox and a new review runs.
    "REVIEW_ENV_FAULT": ("blocked", "not_ready"),
    "ACCEPT_OK":     ("accepted",  "accepted"),
    "ACCEPT_ISSUES": ("fixing",    "not_ready"),
}


def is_environment_fault(envelope: dict | None) -> bool:
    """Reviewer classified the failure as a sandbox environment fault (missing
    absolute path outside the project that no project file references), or
    acceptance found the project_dir itself missing/empty (wrong path — a
    clean-reviewed project cannot legitimately have zero files).
    Nothing in the project can be edited to fix it — such envelopes must never
    route to Aider/Fixer, count toward fix budgets, or force deep_research."""
    return (envelope or {}).get("deterministic_issue") in (
        "environment_fault", "empty_project_dir")


def environment_fault_notice(envelope: dict | None) -> str:
    env = envelope or {}
    detail = (env.get("summary") or "").strip()
    return (
        "⛔ SANDBOX ENVIRONMENT FAULT — not a project code issue.\n\n"
        f"{detail}\n\n"
        "Do NOT call run_aider_fix, run_fixer, or deep_research for this — "
        "there is nothing in the project to fix. Report the problem to the "
        "user so they can remediate the Codebox environment, then call "
        "run_review again once it is fixed."
    )


async def apply_workflow_event(conv_id: str, event: str, *,
                               run_id: str = "", project_id: str = "",
                               mode_hint: str = "build_from_prompt",
                               user_task: str = "") -> str:
    """Single transition point for coder_workflows state."""
    state_artifact = WF_EVENT_TRANSITIONS.get(event)
    if not state_artifact or not conv_id:
        return ""
    state, artifact = state_artifact
    try:
        wf = None
        if project_id:
            wf = await db.get_latest_coder_workflow(conv_id, project_id=project_id)
        if not wf:
            wf = await db.get_latest_coder_workflow(conv_id)
        if not wf:
            wf_id = f"cw-{uuid.uuid4().hex[:12]}"
            await db.create_coder_workflow(
                wf_id, conv_id, project_id=project_id or "",
                mode=mode_hint, state=state,
                user_task=(user_task or "")[:500],
                active_run_id=run_id, artifact_status=artifact,
            )
            print(f"[wf-fsm] {event}: created {wf_id} state={state}", flush=True)
            return wf_id
        if wf.get("state") == "cancelled":
            return wf.get("id", "")
        kwargs = dict(state=state, artifact_status=artifact)
        if run_id:
            kwargs["active_run_id"] = run_id
        if project_id:
            kwargs["project_id"] = project_id
        await db.update_coder_workflow(wf["id"], **kwargs)
        print(f"[wf-fsm] {event}: {wf.get('id')} {wf.get('state')}->{state}", flush=True)
        return wf.get("id", "")
    except Exception as e:
        print(f"[wf-fsm] {event} failed (non-fatal): {e}")
        return ""


RECENT_RESEARCH: OrderedDict[str, dict] = OrderedDict()
RECENT_RESEARCH_MAX = 32
RESEARCH_FRESH_SECONDS = 600


def stash_research_result(conv_id: str, topic: str, report: str) -> None:
    """Record the most recent deep_research result for this conv."""
    if not conv_id or not report:
        return
    RECENT_RESEARCH[conv_id] = {
        "topic": topic or "",
        "report": report,
        "ts": time.time(),
    }
    RECENT_RESEARCH.move_to_end(conv_id)
    while len(RECENT_RESEARCH) > RECENT_RESEARCH_MAX:
        RECENT_RESEARCH.popitem(last=False)


def get_recent_research(conv_id: str) -> "dict | None":
    """Return fresh cached research for conv_id, promoting LRU order."""
    entry = RECENT_RESEARCH.get(conv_id)
    if not entry:
        return None
    if (time.time() - entry.get("ts", 0)) > RESEARCH_FRESH_SECONDS:
        RECENT_RESEARCH.pop(conv_id, None)
        return None
    RECENT_RESEARCH.move_to_end(conv_id)
    return entry


async def uploaded_project_aider_context(conv_id: str, *,
                                         issue_run: dict | None = None,
                                         project_dir: str = "",
                                         project_id: str = "") -> dict | None:
    """Return routing context when this conversation is an uploaded-project fix."""
    if not conv_id or not getattr(config, "AIDER_ENABLED", True):
        return None
    env = (issue_run or {}).get("result_envelope") or {}
    project_dir = (project_dir or env.get("project_dir") or "").strip()
    project_id = (project_id or (issue_run or {}).get("project_id") or "").strip()
    project_id = project_id or project_id_from_dir(project_dir)

    try:
        wf = await db.get_latest_coder_workflow(conv_id, project_id=project_id) if project_id else None
        if not wf:
            wf = await db.get_latest_coder_workflow(conv_id)
        if wf and wf.get("mode") == "fix_uploaded_project" and not wf.get("cancel_requested"):
            project_id = project_id or wf.get("project_id") or ""
            project_dir = project_dir or (f"/root/projects/{project_id}" if project_id else "")
            return {"workflow": wf, "project_id": project_id, "project_dir": project_dir}
    except Exception as e:
        print(f"[v2-gate] uploaded workflow lookup failed (non-fatal): {e}")

    try:
        active = await db.get_coding_project_by_conv(conv_id)
        if active:
            active_pid = active.get("openhands_project_id") or active.get("id") or ""
            desc = active.get("description") or ""
            if desc.startswith("Uploaded project:") and (not project_id or project_id == active_pid):
                project_id = project_id or active_pid
                project_dir = project_dir or (f"/root/projects/{project_id}" if project_id else "")
                return {"workflow": None, "project_id": project_id, "project_dir": project_dir}
    except Exception as e:
        print(f"[v2-gate] uploaded project lookup failed (non-fatal): {e}")
    return None


UPLOADED_PROJECT_BOOTSTRAP_ALLOWED_TOOLS = {
    "run_aider_fix",
    "run_review",
    "run_acceptance_review",
    "get_coder_workflow",
    "cancel_coder_workflow",
}
UPLOADED_PROJECT_TERMINAL_STATES = {
    "accepted", "delivered", "complete", "completed", "cancelled", "blocked",
}
UPLOADED_PROJECT_TERMINAL_ARTIFACTS = {
    "accepted", "delivered", "partial_delivered", "cancelled",
}
UPLOADED_PROJECT_ACTIVE_RUN_STATUSES = {"queued", "running", "pending"}
UPLOADED_PROJECT_AGENT_ROLES = {"aider.fix", "reviewer", "acceptance", "fixer"}


def uploaded_project_tool_allowed_during_bootstrap(name: str) -> bool:
    return name in UPLOADED_PROJECT_BOOTSTRAP_ALLOWED_TOOLS


def uploaded_project_manual_gate_state(workflow: dict | None,
                                       runs: list[dict] | None = None) -> str:
    """Return bootstrap/inflight when an uploaded fix must not use manual tools."""
    if not workflow or workflow.get("mode") != "fix_uploaded_project":
        return ""
    if workflow.get("cancel_requested"):
        return ""
    state = (workflow.get("state") or "").lower()
    artifact = (workflow.get("artifact_status") or "").lower()
    if state in UPLOADED_PROJECT_TERMINAL_STATES:
        return ""
    if artifact in UPLOADED_PROJECT_TERMINAL_ARTIFACTS:
        return ""

    runs = runs or []
    active_run_id = workflow.get("active_run_id") or ""
    for run in runs:
        if active_run_id and run.get("id") == active_run_id:
            if (run.get("status") or "").lower() in UPLOADED_PROJECT_ACTIVE_RUN_STATUSES:
                return "inflight"
            break

    for run in runs:
        role = run.get("role") or ""
        if role in UPLOADED_PROJECT_AGENT_ROLES or role.startswith("builder"):
            return ""
    return "bootstrap"


def project_id_from_dir(project_dir: str) -> str:
    """Best-effort project id from a Codebox project path."""
    if not project_dir:
        return ""
    name = os.path.basename(project_dir.rstrip("/"))
    return name if name.startswith("proj-") else ""


def uploaded_project_bootstrap_block_message(name: str, workflow: dict,
                                             project_dir: str,
                                             task: str,
                                             gate_state: str = "bootstrap") -> str:
    project_id = (workflow or {}).get("project_id") or project_id_from_dir(project_dir)
    project_dir = project_dir or (f"/root/projects/{project_id}" if project_id else "/root/projects/<active-project>")
    task_hint = (task or (workflow or {}).get("user_task") or "latest user request").strip()
    task_hint = re.sub(r"\s+", " ", task_hint)[:700].replace("\\", "\\\\").replace("'", "\\'")
    if gate_state == "inflight":
        reason = (
            "an uploaded-project Aider/Reviewer run is already in progress. "
            "Wait for that run or inspect workflow status."
        )
        next_call = "get_coder_workflow(workflow_id='<workflow_id>')"
    else:
        reason = (
            "uploaded-project fixes must start with Aider or Reviewer from the active "
            "project root, not manual file tools."
        )
        next_call = (
            f"run_aider_fix(project_dir='{project_dir}', task='{task_hint}')"
        )
    return (
        f"BLOCKED — {reason}\n\n"
        f"Active project path: `{project_dir}`"
        + (f"\nProject id: `{project_id}`" if project_id else "")
        + "\n\n"
        f"Your VERY NEXT tool call MUST be:\n  {next_call}\n\n"
        f"Do NOT call {name}, read_file, write_file, run_shell, execute_code, "
        f"generate_code, run_fixer, list_files, or search_files for this uploaded-project "
        f"fix. Aider owns the edit; Reviewer verifies it."
    )


# Backward-compatible underscored aliases for existing tools.py tests.
_parse_ts_loose = parse_ts_loose
_latest_user_msg_ts = latest_user_msg_ts
_runs_since = runs_since
_v2_name_match = v2_name_match
_is_v2_persona = is_v2_persona
_deep_research_called_since = deep_research_called_since
_prior_fix_attempts_context = prior_fix_attempts_context
_prior_acceptance_issues_context = prior_acceptance_issues_context
_fix_budget_note = fix_budget_note
_WF_EVENT_TRANSITIONS = WF_EVENT_TRANSITIONS
_apply_workflow_event = apply_workflow_event
_is_environment_fault = is_environment_fault
_environment_fault_notice = environment_fault_notice
_RECENT_RESEARCH = RECENT_RESEARCH
_RECENT_RESEARCH_MAX = RECENT_RESEARCH_MAX
_RESEARCH_FRESH_SECONDS = RESEARCH_FRESH_SECONDS
_stash_research_result = stash_research_result
_get_recent_research = get_recent_research
_uploaded_project_aider_context = uploaded_project_aider_context
_uploaded_project_tool_allowed_during_bootstrap = uploaded_project_tool_allowed_during_bootstrap
_uploaded_project_manual_gate_state = uploaded_project_manual_gate_state
_project_id_from_dir = project_id_from_dir
_uploaded_project_bootstrap_block_message = uploaded_project_bootstrap_block_message
