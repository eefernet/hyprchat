"""
Tool definitions and execution dispatch for HyprChat's integrated CodeAgent.
"""
import asyncio
import base64
import calendar
import hashlib
import json
import os
import re
import shlex
import time
import uuid
from datetime import datetime

import config
import database as db
import cancel_registry
from connectors import execute_connector_tool
from research import fetch_bytes_safely, run_deep_research, run_conspiracy_research, _fetch_page, _source_tier

# Strip ANSI escape codes from terminal output before feeding back to the model
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


def _artifact_file_metadata(path: str) -> dict:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return {"size_bytes": os.path.getsize(path), "sha256": h.hexdigest()}


def _artifact_index_text(path: str, filename: str, kind: str, mime_type: str = "", max_chars: int = 500000) -> str:
    kind = (kind or db.artifact_kind_for_filename(filename, mime_type)).lower()
    lower = (filename or "").lower()
    try:
        if kind in {"image", "pdf"}:
            meta = _artifact_file_metadata(path)
            return f"{filename}\n{kind.upper()} artifact\nsize_bytes={meta['size_bytes']}\nsha256={meta['sha256']}"
        if kind == "archive":
            import tarfile
            import zipfile
            lines = [f"Archive: {filename}"]
            if tarfile.is_tarfile(path):
                with tarfile.open(path, "r:*") as tf:
                    for member in tf.getmembers()[:500]:
                        lines.append(f"{member.name}{'/' if member.isdir() else ''} {member.size} bytes")
            elif zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as zf:
                    for info in zf.infolist()[:500]:
                        lines.append(f"{info.filename}{'/' if info.is_dir() else ''} {info.file_size} bytes")
            return "\n".join(lines)[:max_chars]
        text_exts = (
            ".txt", ".log", ".md", ".markdown", ".html", ".htm", ".py", ".js", ".jsx",
            ".ts", ".tsx", ".json", ".jsonl", ".csv", ".tsv", ".css", ".sh", ".rs",
            ".go", ".java", ".c", ".cpp", ".yaml", ".yml", ".toml", ".xml", ".ini",
            ".conf", ".cfg",
        )
        if kind in {"html", "markdown", "code", "data", "text"} or lower.endswith(text_exts):
            with open(path, "rb") as f:
                raw = f.read(min(max_chars * 4, 4 * 1024 * 1024))
            text = raw.decode("utf-8", errors="replace")
            if kind == "html" or lower.endswith((".html", ".htm")):
                text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
                text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
            return text[:max_chars]
    except Exception as e:
        print(f"[ArtifactIndex] {e}")
    return ""


def _parse_ts_loose(s) -> "datetime | None":
    """Parse timestamps from either Python isoformat (runs.started_at, has 'T'
    and microseconds) or SQLite CURRENT_TIMESTAMP (messages.created_at, space
    separator, no microseconds, sometimes a trailing 'Z'). Returns None on
    unrecognized input rather than raising — callers treat missing as 'before
    the current turn' which is the safe default."""
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


async def _latest_user_msg_ts(conv_id: str, conv_row: dict | None = None):
    """Timestamp of the newest persisted user message, or None.

    The cycle-cap / Q&A-terminal gates scope their counters to runs newer than
    this so a new user request resets the budget instead of inheriting an
    exhausted cap from earlier work on the same conversation."""
    try:
        conv = conv_row or await db.get_conversation(conv_id)
        for m in reversed((conv or {}).get("messages") or []):
            if m.get("role") == "user":
                return _parse_ts_loose(m.get("created_at"))
    except Exception as _e:
        print(f"[v2-gate] latest-user-msg lookup failed (non-fatal): {_e}")
    return None


def _runs_since(runs: list, ts) -> list:
    """Runs whose started_at >= ts. ts=None keeps all runs (fail-safe: gates
    fall back to the old whole-conversation behavior, never crash open)."""
    if ts is None:
        return runs
    out = []
    for r in runs:
        rts = _parse_ts_loose(r.get("started_at"))
        if rts is None or rts >= ts:
            out.append(r)
    return out


def _v2_name_match(name: str) -> bool:
    """Match v2/Daedalus persona names. Word-boundary 'v2' so unrelated names
    that merely contain the substring (e.g. 'v2ray helper') don't enable the
    workflow gate."""
    n = (name or "").lower()
    return "daedalus" in n or re.search(r"\bv2\b", n) is not None


async def _prior_fix_attempts_context(conv_id: str, *, max_attempts: int = 3) -> str:
    """Compact history of fix attempts since the latest user message.

    Injected into Fixer/Aider prompts so attempt #2 knows what attempt #1
    already changed — without it every fix call is stateless and the model's
    most common failure is re-making the same edit that already didn't work."""
    if not conv_id:
        return ""
    try:
        runs = await db.get_runs_by_conversation(conv_id, limit=50)
        uts = await _latest_user_msg_ts(conv_id)
        attempts = [
            r for r in _runs_since(runs, uts)
            if (r.get("role") in {"fixer", "aider.fix"}
                and r.get("status") in {"succeeded", "partial", "failed"})
        ]
        if not attempts:
            return ""
        lines = []
        # runs come newest-first; show oldest attempt first.
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


async def _prior_acceptance_issues_context(conv_id: str) -> str:
    """Most recent acceptance verdict (issues/error) for this user request —
    fed back into the next acceptance run so it verifies its own prior
    findings instead of moving the goalposts with brand-new nitpicks."""
    if not conv_id:
        return ""
    try:
        runs = await db.get_runs_by_conversation(conv_id, limit=50)
        uts = await _latest_user_msg_ts(conv_id)
        for r in _runs_since(runs, uts):
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


async def _fix_budget_note(conv_id: str, source_role: str) -> str:
    """One-line budget readout appended to fix results so the model can plan
    its remaining cycles instead of discovering the cap by hitting it."""
    try:
        runs = await db.get_runs_by_conversation(conv_id, limit=50)
        uts = await _latest_user_msg_ts(conv_id)
        source_role = source_role or "reviewer"
        succ = sum(
            1 for r in _runs_since(runs, uts)
            if (r.get("role") in {"fixer", "aider.fix"}
                and r.get("status") == "succeeded"
                and ((r.get("result_envelope") or {}).get("source_role") or "reviewer") == source_role)
        )
        cap = 2 if source_role == "acceptance" else 3
        return (f"\n\nFix-cycle budget: {min(succ, cap)}/{cap} successful "
                f"{source_role}-driven fix(es) used for this request.")
    except Exception:
        return ""


# In-process cache of the most recent deep_research result per conversation.
# The full research report only exists in the orchestrator's in-memory tool
# history for the current turn — saved_events keeps a summary but not the
# body. Caching it here lets the Fixer dispatcher pick it up across the
# orchestrator/fixer boundary without persisting research bodies to SQLite
# or breaking the Fixer's network-free contract. Bounded to 32 entries with
# LRU eviction (move-to-end on read); freshness window enforced at read time.
from collections import OrderedDict
_RECENT_RESEARCH: OrderedDict[str, dict] = OrderedDict()
_RECENT_RESEARCH_MAX = 32
_RESEARCH_FRESH_SECONDS = 600  # 10 min — drop on next read past this


def _stash_research_result(conv_id: str, topic: str, report: str) -> None:
    """Record the most recent deep_research result for this conv. LRU-bounded:
    accessed entries move to the end; eviction drops the least-recently-used."""
    if not conv_id or not report:
        return
    _RECENT_RESEARCH[conv_id] = {
        "topic": topic or "",
        "report": report,
        "ts": time.time(),
    }
    _RECENT_RESEARCH.move_to_end(conv_id)
    while len(_RECENT_RESEARCH) > _RECENT_RESEARCH_MAX:
        _RECENT_RESEARCH.popitem(last=False)


def _get_recent_research(conv_id: str) -> "dict | None":
    """Return the cached research entry for conv_id if it's still within the
    freshness window, else None. Stale entries are dropped on read. Accessed
    entries are promoted (LRU)."""
    entry = _RECENT_RESEARCH.get(conv_id)
    if not entry:
        return None
    if (time.time() - entry.get("ts", 0)) > _RESEARCH_FRESH_SECONDS:
        _RECENT_RESEARCH.pop(conv_id, None)
        return None
    _RECENT_RESEARCH.move_to_end(conv_id)
    return entry


def _issue_signatures(envelope: dict) -> set:
    """Reduce a reviewer envelope to a set of (file, summary_prefix) tuples
    suitable for set-overlap comparison. Used by the v2 gate to detect
    'same problem returned' across run_review cycles. Summary prefix is
    lowercased + stripped + truncated to 60 chars so trivial wording diffs
    don't defeat the overlap check, but the file path must match exactly."""
    sigs = set()
    for iss in (envelope or {}).get("issues") or []:
        f = (iss.get("file") or "").strip()
        s = (iss.get("summary") or "").strip().lower()[:60]
        if f and s:
            sigs.add((f, s))
    return sigs


def _paths_are_docs_only(paths: list[str]) -> bool:
    """True when a fixer touched only documentation files."""
    if not paths:
        return False
    doc_exts = {".md", ".rst", ".txt"}
    doc_names = {"README", "README.md", "README.rst", "README.txt"}
    for path in paths:
        name = os.path.basename(path or "")
        ext = os.path.splitext(name)[1].lower()
        if name in doc_names or ext in doc_exts:
            continue
        return False
    return True


async def _deep_research_called_since(conv_id: str, since_iso: str | None) -> bool:
    """Return True iff deep_research has run on this conversation since the
    trigger event at `since_iso`. Checks two sources:

      1. In-process `_RECENT_RESEARCH` cache (10-min freshness, populated
         immediately when deep_research's dispatcher returns). This is what
         catches mid-stream calls — a model that calls deep_research and
         then run_fixer in the same turn won't have the deep_research event
         persisted to the conversation yet (assistant message is only saved
         at stream end), so the DB walk below misses it. Without this check,
         the FINAL_CYCLE / STUCK_FIX gates fire a second time, the model
         dutifully re-runs deep_research, and the user sees duplicate
         carousels and ~3 minutes of wasted research.
      2. Persisted assistant messages' saved_events (cross-stream / restart
         survival). Only consulted when (1) returns nothing.
    """
    # In-stream check first — matches what's already populated via
    # `_stash_research_result` on deep_research completion. The cache is
    # keyed by conv_id. When since_iso is provided, we also verify the
    # cached entry is newer than the trigger event — pre-build research
    # done before the latest reviewer run must not satisfy the gate.
    try:
        _cached = _get_recent_research(conv_id) if conv_id else None
        if _cached:
            if since_iso:
                _since_dt = _parse_ts_loose(since_iso)
                if _since_dt:
                    _since_unix = calendar.timegm(_since_dt.timetuple()) + _since_dt.microsecond / 1_000_000
                    if _cached.get("ts", 0) >= _since_unix:
                        return True
                # Cached research is older than since_iso — fall through to DB
            else:
                return True
    except Exception as _e:
        print(f"[v2-gate] in-stream research cache check failed (non-fatal): {_e}")

    if not since_iso:
        return False
    try:
        conv = await db.get_conversation(conv_id)
        msgs = (conv or {}).get("messages") or []
        since_ts = _parse_ts_loose(since_iso)
        for m in reversed(msgs):
            if m.get("role") != "assistant":
                continue
            m_ts = _parse_ts_loose(m.get("created_at"))
            if since_ts and m_ts and m_ts < since_ts:
                # Once we walk past the trigger run, we can stop.
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
    except Exception as _e:
        print(f"[v2-gate] deep_research history check failed (non-fatal): {_e}")
        return False


_SHIP_ANYWAY_ACTION_RE = re.compile(
    r"\b(ship|package|download|deliver|export|archive|tarball|zip)\b"
    r"|give\s+me\s+(?:a\s+)?download",
    re.I,
)
_SHIP_ANYWAY_QUALIFIER_RE = re.compile(
    r"\b(anyway|as[-\s]?is|even\s+if|despite|regardless|ignore|skip)\b"
    r"|\b(tests?\s+(?:fail|failing|failed)|test\s+issues?|known\s+issues?|"
    r"with\s+(?:the\s+)?issues?|broken|unverified)\b",
    re.I,
)
_SHIP_ANYWAY_DIRECT_RE = re.compile(
    r"\b(ship|package|download|deliver|export|archive)\s+"
    r"(?:it|this|the\s+project|what\s+(?:you|was)\s+built)\b"
    r"|give\s+me\s+(?:a\s+)?download",
    re.I,
)


async def _latest_user_requested_ship_anyway(conv_id: str) -> bool:
    """True when the latest user turn explicitly asks for delivery even
    though the project may still have review/test issues.

    This is intentionally narrow and only used to relax delivery gates. It
    should not make repair/build tools bypass the v2 workflow.
    """
    if not conv_id:
        return False
    try:
        conv = await db.get_conversation(conv_id)
        msgs = (conv or {}).get("messages") or []
        latest = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
        content = (latest or {}).get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content)
        text = content.strip()
        if not text or not _SHIP_ANYWAY_ACTION_RE.search(text):
            return False
        return bool(
            _SHIP_ANYWAY_QUALIFIER_RE.search(text)
            or _SHIP_ANYWAY_DIRECT_RE.search(text)
        )
    except Exception as _e:
        print(f"[v2-gate] ship-anyway intent check failed (non-fatal): {_e}")
        return False


def _project_id_from_dir(project_dir: str) -> str:
    """Best-effort project id from a Codebox project path."""
    if not project_dir:
        return ""
    name = os.path.basename(project_dir.rstrip("/"))
    return name if name.startswith("proj-") else ""


async def _latest_user_task_text(conv_id: str) -> str:
    if not conv_id:
        return ""
    try:
        conv = await db.get_conversation(conv_id)
        msgs = (conv or {}).get("messages") or []
        latest = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
        content = (latest or {}).get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content)
        return content.strip()[:2000]
    except Exception as _e:
        print(f"[v2-gate] latest user task lookup failed (non-fatal): {_e}")
        return ""


async def _latest_actionable_issue_run(conv_id: str, requested_id: str = "") -> dict | None:
    """Return the requested/latest reviewer or acceptance run with issues."""
    if not conv_id and not requested_id:
        return None
    if requested_id:
        try:
            run = await db.get_run(requested_id)
            if run:
                return run
        except Exception as _e:
            print(f"[v2-gate] requested issue run lookup failed (non-fatal): {_e}")
    if not conv_id:
        return None
    try:
        runs = await db.get_runs_by_conversation(conv_id, limit=30)
        return next(
            (r for r in runs
             if r.get("role") in {"reviewer", "acceptance"}
             and ((r.get("result_envelope") or {}).get("status") or "").lower() in {"issues", "error"}),
            None,
        )
    except Exception as _e:
        print(f"[v2-gate] latest actionable issue lookup failed (non-fatal): {_e}")
        return None


def _task_from_issue_run(user_task: str, issue_run: dict | None) -> str:
    env = (issue_run or {}).get("result_envelope") or {}
    summary = (env.get("summary") or "").strip()
    issues = env.get("issues") or []
    issue_lines = []
    for i, issue in enumerate(issues[:5], 1):
        issue_lines.append(
            f"{i}. {issue.get('file') or '?'}: {(issue.get('summary') or '')[:240]}"
        )
    parts = []
    if user_task:
        parts.append(user_task)
    if summary:
        parts.append(f"Reviewer summary: {summary}")
    if issue_lines:
        parts.append("Reviewer issues:\n" + "\n".join(issue_lines))
    return "\n\n".join(parts).strip() or "Fix the uploaded project reviewer issues."


async def _uploaded_project_aider_context(conv_id: str, *,
                                          issue_run: dict | None = None,
                                          project_dir: str = "",
                                          project_id: str = "") -> dict | None:
    """Return routing context when this conversation is an uploaded-project fix.

    This intentionally does not classify all active coding projects as uploads:
    greenfield OpenHands builds also create coding_projects rows. Uploaded
    projects are identified by their workflow mode or upload-time description.
    """
    if not conv_id or not getattr(config, "AIDER_ENABLED", True):
        return None
    env = (issue_run or {}).get("result_envelope") or {}
    project_dir = (project_dir or env.get("project_dir") or "").strip()
    project_id = (project_id or (issue_run or {}).get("project_id") or "").strip()
    project_id = project_id or _project_id_from_dir(project_dir)

    try:
        wf = await db.get_latest_coder_workflow(conv_id, project_id=project_id) if project_id else None
        if not wf:
            wf = await db.get_latest_coder_workflow(conv_id)
        if wf and wf.get("mode") == "fix_uploaded_project" and not wf.get("cancel_requested"):
            project_id = project_id or wf.get("project_id") or ""
            project_dir = project_dir or (f"/root/projects/{project_id}" if project_id else "")
            return {"workflow": wf, "project_id": project_id, "project_dir": project_dir}
    except Exception as _e:
        print(f"[v2-gate] uploaded workflow lookup failed (non-fatal): {_e}")

    try:
        active = await db.get_coding_project_by_conv(conv_id)
        if active:
            active_pid = active.get("openhands_project_id") or active.get("id") or ""
            desc = active.get("description") or ""
            if desc.startswith("Uploaded project:") and (not project_id or project_id == active_pid):
                project_id = project_id or active_pid
                project_dir = project_dir or (f"/root/projects/{project_id}" if project_id else "")
                return {"workflow": None, "project_id": project_id, "project_dir": project_dir}
    except Exception as _e:
        print(f"[v2-gate] uploaded project lookup failed (non-fatal): {_e}")
    return None


_UPLOADED_PROJECT_BOOTSTRAP_ALLOWED_TOOLS = {
    "run_aider_fix",
    "run_review",
    "run_acceptance_review",
    "get_coder_workflow",
    "cancel_coder_workflow",
}
_UPLOADED_PROJECT_TERMINAL_STATES = {
    "accepted", "delivered", "complete", "completed", "cancelled", "blocked",
}
_UPLOADED_PROJECT_TERMINAL_ARTIFACTS = {
    "accepted", "delivered", "partial_delivered", "cancelled",
}
_UPLOADED_PROJECT_ACTIVE_RUN_STATUSES = {"queued", "running", "pending"}
_UPLOADED_PROJECT_AGENT_ROLES = {"aider.fix", "reviewer", "acceptance", "fixer"}


def _uploaded_project_tool_allowed_during_bootstrap(name: str) -> bool:
    return name in _UPLOADED_PROJECT_BOOTSTRAP_ALLOWED_TOOLS


def _uploaded_project_manual_gate_state(workflow: dict | None,
                                        runs: list[dict] | None = None) -> str:
    """Return bootstrap/inflight when an uploaded fix must not use manual tools."""
    if not workflow or workflow.get("mode") != "fix_uploaded_project":
        return ""
    if workflow.get("cancel_requested"):
        return ""
    state = (workflow.get("state") or "").lower()
    artifact = (workflow.get("artifact_status") or "").lower()
    if state in _UPLOADED_PROJECT_TERMINAL_STATES:
        return ""
    if artifact in _UPLOADED_PROJECT_TERMINAL_ARTIFACTS:
        return ""

    runs = runs or []
    active_run_id = workflow.get("active_run_id") or ""
    for run in runs:
        if active_run_id and run.get("id") == active_run_id:
            if (run.get("status") or "").lower() in _UPLOADED_PROJECT_ACTIVE_RUN_STATUSES:
                return "inflight"
            break

    for run in runs:
        role = run.get("role") or ""
        if role in _UPLOADED_PROJECT_AGENT_ROLES or role.startswith("builder"):
            return ""
    return "bootstrap"


def _uploaded_project_bootstrap_block_message(name: str, workflow: dict,
                                              project_dir: str,
                                              task: str,
                                              gate_state: str = "bootstrap") -> str:
    project_id = (workflow or {}).get("project_id") or _project_id_from_dir(project_dir)
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


# Pytest "FAILED tests/foo.py::test_x - reason" line, plus a fallback for
# generic "FAILED <path>:<line>" / "ERROR tests/foo.py" forms. Captures the
# file path so the synthesized reviewer envelope can route Fixer to the
# right `suggested_fix_scope` files.
_PYTEST_FAIL_RE = re.compile(
    r"^(?:FAILED|ERROR)\s+([\w./\-]+\.py)(?:::([\w.\[\]<>:_-]+))?(?:\s*-\s*(.{0,200}))?",
    re.MULTILINE,
)
_PYTEST_IMPORT_ERR_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError):\s*(.{0,160})", re.MULTILINE,
)


async def _synthesize_reviewer_from_test_failure(
    conv_id: str, test_output: str, project_dir: str,
    framework: str, elapsed: float,
) -> str:
    """If the most recent reviewer on this conv reported clean but run_tests
    just failed, persist a NEW reviewer-role run row whose envelope encodes
    the test failures as a real `issues` list. Returns the new run_id, or ""
    if no synthesis was warranted (e.g. no prior clean reviewer found).

    The synthesized run is the same shape the real Reviewer would produce, so
    `run_fixer(reviewer_run_id=...)` works without modification. Distinguished
    from a real reviewer envelope only by `synthetic: True` in the envelope
    (kept for diagnostics — Fixer ignores the field).
    """
    # Find the most recent run_review for this conversation.
    try:
        runs = await db.get_runs_by_conversation(conv_id, limit=20)
    except Exception:
        return ""
    last_review = next(
        (r for r in runs if r.get("role") == "reviewer"
         and (r.get("status") or "").lower() == "succeeded"),
        None,
    )
    if not last_review:
        # No prior reviewer means there's no false negative to compensate for.
        # Don't synthesize — just let run_tests' default response stand.
        return ""

    rev_env = last_review.get("result_envelope") or {}
    rev_test_exit = rev_env.get("test_exit", -1)
    rev_status = (rev_env.get("status") or "").lower()
    # Only synthesize if reviewer claimed clean (or a legacy envelope with no
    # status whose tests passed). If reviewer already reported issues, the
    # model has a real envelope to fix against — stacking a synthetic one on
    # top would shift the gate's "latest reviewer" baseline.
    if not (rev_status == "clean" or (not rev_status and rev_test_exit == 0)):
        return ""

    # Parse test output into structured issues. Group by file so multiple
    # failing tests in the same file collapse to a single fixer scope.
    by_file: dict[str, list[dict]] = {}
    for m in _PYTEST_FAIL_RE.finditer(test_output or ""):
        f = m.group(1)
        test_name = m.group(2) or ""
        reason = (m.group(3) or "").strip()
        by_file.setdefault(f, []).append({"test": test_name, "reason": reason})

    issues = []
    for f, fails in list(by_file.items())[:8]:  # cap
        # Resolve relative path → absolute under project_dir for fixer scope.
        abs_path = f if f.startswith("/") else f"{project_dir.rstrip('/')}/{f}"
        summary_lines = [f"{x['test']}: {x['reason'][:120]}" for x in fails[:3] if x['reason']]
        summary = (f"{len(fails)} failing test(s) in {f}: "
                   + ("; ".join(summary_lines) if summary_lines else "see test output"))
        issues.append({
            "severity": "test",
            "file": abs_path,
            "lines": [],
            "summary": summary[:300],
            # Scope to the test file plus a guess at the source-under-test —
            # if path is `tests/test_foo.py`, also include `src/foo.py` /
            # `foo.py`. Fixer's scope check only writes files we list, so
            # be conservative.
            "suggested_fix_scope": _guess_fix_scope(abs_path, project_dir),
        })

    # If no FAILED lines parsed but pytest still exited non-zero, fall back to
    # a single catch-all issue so the model has something to call run_fixer on.
    if not issues:
        ie_match = _PYTEST_IMPORT_ERR_RE.search(test_output or "")
        ie_summary = (f"ImportError during test collection: {ie_match.group(1).strip()}"
                      if ie_match else
                      "Tests failed but no FAILED lines parsed — see test output")
        issues.append({
            "severity": "test",
            "file": project_dir,
            "lines": [],
            "summary": ie_summary[:300],
            "suggested_fix_scope": [],
        })

    # Persist the synthesized envelope as a real run row so Fixer + the
    # frontend RunCard treat it identically to a normal reviewer run.
    new_id = f"run-{uuid.uuid4().hex[:12]}"
    envelope = {
        "status": "issues",
        "summary": (f"Synthesized from run_tests failure ({framework}, {elapsed:.1f}s) "
                    f"after Reviewer reported clean. {len(issues)} issue(s) detected."),
        "issues": issues,
        "build_cmd": rev_env.get("build_cmd", ""),
        "test_cmd": rev_env.get("test_cmd", ""),
        "lint_cmd": rev_env.get("lint_cmd", ""),
        "build_exit": rev_env.get("build_exit", 0),  # build wasn't re-run
        "test_exit": 1,                              # synthesized failure
        "lint_exit": rev_env.get("lint_exit", 0),
        "language": rev_env.get("language", "python"),
        "marker": rev_env.get("marker", ""),
        "project_dir": project_dir,
        "review_model": "(synthesized from run_tests)",
        "synthetic": True,
        "source_run_id": last_review.get("id", ""),
        "run_id": new_id,
    }
    try:
        await db.create_run(new_id, conv_id, role="reviewer",
                            project_id=last_review.get("project_id", ""),
                            parent_run_id=last_review.get("id", ""),
                            status="succeeded")
        await db.update_run(new_id, status="succeeded",
                            result_envelope=envelope, ended=True)
    except Exception as e:
        print(f"[TRIPWIRE] persist synthesized review failed: {e}")
        return ""
    return new_id


def _guess_fix_scope(test_file_abs: str, project_dir: str) -> list[str]:
    """Given a failing test file, propose the source files Fixer should be
    allowed to edit. Conservative — we list the test file itself plus the
    most plausible source-under-test sibling, never a glob.

    Supports Python (test_foo.py, foo_test.py), Java/Kotlin (FooTest.java),
    Go (foo_test.go), JavaScript/TypeScript (foo.test.js, foo.spec.ts),
    and Rust (tests/ convention)."""
    pdir = project_dir.rstrip("/")
    scope = [test_file_abs]
    base = os.path.basename(test_file_abs)
    dname = os.path.dirname(test_file_abs)

    # Python: test_foo.py → foo.py
    if base.startswith("test_") and base.endswith(".py"):
        stem = base[5:]
        for cand in (f"{pdir}/src/{stem}", f"{pdir}/{stem}",
                     f"{pdir}/app/{stem}", f"{pdir}/lib/{stem}"):
            scope.append(cand)
    elif base.endswith("_test.py"):
        stem = base[:-len("_test.py")] + ".py"
        for cand in (f"{pdir}/src/{stem}", f"{pdir}/{stem}"):
            scope.append(cand)

    # Java/Kotlin: FooTest.java → Foo.java, FooTests.java → Foo.java
    elif base.endswith(("Test.java", "Tests.java", "Test.kt", "Tests.kt")):
        for suffix in ("Tests.java", "Test.java", "Tests.kt", "Test.kt"):
            if base.endswith(suffix):
                src_base = base[:-len(suffix)] + base[base.rfind("."):]
                # Common Maven/Gradle layout: src/test/java/... → src/main/java/...
                src_dir = dname.replace("/src/test/", "/src/main/")
                scope.append(f"{src_dir}/{src_base}")
                scope.append(f"{pdir}/src/main/java/{src_base}")
                scope.append(f"{pdir}/{src_base}")
                break

    # Go: foo_test.go → foo.go (same directory)
    elif base.endswith("_test.go"):
        src_base = base[:-len("_test.go")] + ".go"
        scope.append(f"{dname}/{src_base}")
        scope.append(f"{pdir}/{src_base}")

    # JS/TS: foo.test.js → foo.js, foo.spec.ts → foo.ts
    elif any(base.endswith(s) for s in (".test.js", ".test.ts", ".test.jsx", ".test.tsx",
                                        ".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx")):
        for mid in (".test.", ".spec."):
            if mid in base:
                src_base = base.replace(mid, ".")
                scope.append(f"{pdir}/src/{src_base}")
                scope.append(f"{pdir}/{src_base}")
                scope.append(f"{dname}/{src_base}")
                break

    # Rust: tests/foo.rs → src/foo.rs or src/lib.rs
    elif "/tests/" in test_file_abs and base.endswith(".rs"):
        scope.append(f"{pdir}/src/{base}")
        scope.append(f"{pdir}/src/lib.rs")
        scope.append(f"{pdir}/src/main.rs")

    # Dedup, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for p in scope:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


async def _is_v2_persona(conv_id: str, conv_row: dict | None = None) -> bool:
    """Return True iff this conversation is using the v2 coding persona.

    Centralised so the workflow gate doesn't repeat the same db lookup +
    string match in five different places. Pass conv_row if the caller has
    already fetched it to avoid a second db query. Uses a direct by-id
    query instead of loading all configs. Failures are non-fatal and return
    False (fail-safe — gate stays off rather than mis-firing on a v1 persona)."""
    try:
        if conv_row is None:
            conv_row = await db.get_conversation(conv_id)
        _mc_id = (conv_row or {}).get("model_config_id") if conv_row else None
        if not _mc_id:
            return False
        _mc = await db.get_model_config(_mc_id)
        # NOTE: chat.py detects v2 from req.persona_id's config name; this
        # gate detects it from conversation.model_config_id. The matching
        # rules are shared via _v2_name_match so the two can only diverge on
        # *which* config they look at, not on how the name is interpreted.
        return bool(_mc and _v2_name_match(_mc.get("name") or ""))
    except Exception as _e:
        print(f"[v2-gate] persona lookup failed (non-fatal): {_e}")
        return False


# ── Ollama-native tool definitions ──
# Keep descriptions SHORT and CLEAR. Models perform better with concise tool docs.
CODEAGENT_TOOLS = {
    "execute_code": {
        "type": "function",
        "function": {
            "name": "execute_code",
            "description": "Execute source code directly in the sandbox. Pass complete source code with hardcoded test values. Working directory is /root/. Do NOT use input() or sys.argv — they will fail. For scripts needing arguments, use write_file + run_shell instead.",
            "parameters": {"type": "object", "properties": {
                "code": {"type": "string", "description": "Complete source code to execute (must be self-contained with hardcoded test values)"},
                "language": {"type": "string", "description": "Language: python, javascript, bash, c, cpp, rust, go, java, ruby, php, etc."},
            }, "required": ["code", "language"]},
        },
    },
    "run_shell": {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command in /root/. Use for: pip install, running saved scripts with args (python3 /root/app.py arg1 arg2), git, make, npm, cargo build. Preferred way to test scripts that take arguments.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
            }, "required": ["command"]},
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file to the sandbox. Files persist between calls. Always use absolute paths starting with /root/.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Absolute path, e.g. /root/app.py"},
                "content": {"type": "string", "description": "Complete file contents"},
            }, "required": ["path", "content"]},
        },
    },
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents from the sandbox.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Absolute file path"},
            }, "required": ["path"]},
        },
    },
    "list_files": {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List directory contents with sizes and permissions.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Directory path (default: /root)"},
            }, "required": []},
        },
    },
    "research": {
        "type": "function",
        "function": {
            "name": "research",
            "description": "Search the web and read top results. Returns actual page content from the best matches, not just snippets. Use for any factual, current, or real-world question.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Search query — be specific and detailed for best results"},
            }, "required": ["query"]},
        },
    },
    "fetch_url": {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch and read text content from a URL. Returns up to 8000 chars.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
            }, "required": ["url"]},
        },
    },
    "download_file": {
        "type": "function",
        "function": {
            "name": "download_file",
            "description": "Download a file from the sandbox to the user's browser.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Absolute path on sandbox, e.g. /root/output.png"},
            }, "required": ["path"]},
        },
    },
    "download_project": {
        "type": "function",
        "function": {
            "name": "download_project",
            "description": "Package a directory as .tar.gz and make it downloadable.",
            "parameters": {"type": "object", "properties": {
                "directory": {"type": "string", "description": "Directory to package, e.g. /root/myproject"},
            }, "required": ["directory"]},
        },
    },
    "delete_file": {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file or directory from the sandbox.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path to delete"},
            }, "required": ["path"]},
        },
    },
    "deep_research": {
        "type": "function",
        "function": {
            "name": "deep_research",
            "description": "Agent Research: multi-source web research for coding agents. Use for current or fast-moving APIs, SDKs, libraries, framework docs, repeated reviewer/fixer failures on the same error, or the final allowed fix cycle. Include exact error messages, package names, framework/runtime versions, failing commands, and relevant file context. Prefer depth=2 for coding blockers and depth=3 for broad unfamiliar tech; avoid depth 4-5 inside fix loops.",
            "parameters": {"type": "object", "properties": {
                "topic": {"type": "string", "description": "Exact coding/research question, error text, library/API behavior, or implementation blocker to verify"},
                "depth": {"type": "integer", "description": "Depth 1-5. For agents use 2 for focused coding blockers, 3 for broad unfamiliar tech; avoid 4-5 in fix loops"},
                "focus": {"type": "string", "description": "Optional constraints such as package version, framework, runtime, failing command, target file, or suspected root cause"},
                "mode": {"type": "string", "description": "Mode: research (default), compare, quick"},
                "topic_b": {"type": "string", "description": "Second topic for compare mode"},
            }, "required": ["topic"]},
        },
    },
    "conspiracy_research": {
        "type": "function",
        "function": {
            "name": "conspiracy_research",
            "description": "Deep investigative research across WikiLeaks, FOIA vaults, court records, gov archives, alt-media, and leaked documents. Use for any topic where official narratives may be incomplete.",
            "parameters": {"type": "object", "properties": {
                "topic": {"type": "string", "description": "What to investigate — a person, event, organization, or claim"},
                "angle": {"type": "string", "description": "Focus: evidence (default), key_players, timeline, debunk, documents, connections"},
                "depth": {"type": "integer", "description": "Search depth 3-5 (default 4). Higher = more sources searched"},
            }, "required": ["topic"]},
        },
    },
    "generate_code": {
        "type": "function",
        "function": {
            "name": "generate_code",
            "description": "Generate code using an autonomous coding agent (OpenHands). Handles entire projects: creates all files, installs dependencies, builds, and tests. Use for ANY coding task — single scripts or multi-file projects. Returns paths of all files created.",
            "parameters": {"type": "object", "properties": {
                "task": {"type": "string", "description": "Complete, detailed project specification. Include: what to build, features, input/output format, constraints. More detail = better results."},
                "language": {"type": "string", "description": "Primary language: python, javascript, typescript, rust, go, etc."},
                "context": {"type": "string", "description": "Optional: error messages to fix, existing code to modify, constraints, dependencies"},
            }, "required": ["task", "language"]},
        },
    },
    "plan_project": {
        "type": "function",
        "function": {
            "name": "plan_project",
            "description": "Create an architecture plan before writing code. Call this FIRST for any multi-file project. Uses a dedicated planning model to design file structure, dependencies, component interactions, and build order. Returns a structured plan — do NOT write code yet, implement the plan step by step after.",
            "parameters": {"type": "object", "properties": {
                "task": {"type": "string", "description": "What to build — detailed requirements and features"},
                "language": {"type": "string", "description": "Primary language: python, javascript, typescript, rust, go, etc."},
                "constraints": {"type": "string", "description": "Technical constraints, preferred libraries, deployment target, etc."},
            }, "required": ["task", "language"]},
        },
    },
    "run_review": {
        "type": "function",
        "function": {
            "name": "run_review",
            "description": "Review a project after generate_code (or after manual changes) by running its real build, test, and lint commands in the sandbox and analysing the output. Returns a structured issue list (compile / test / lint / smell). Use this INSTEAD of manually reading and rewriting files round-by-round — it's faster, more thorough, and produces actionable scoped fixes. Reviewer is read-only — it never edits code.",
            "parameters": {"type": "object", "properties": {
                "project_dir": {"type": "string", "description": "Absolute path to the project root in the sandbox, e.g. '/root/projects/pong-game'. If omitted, uses the conversation's active project."},
                "project_id": {"type": "string", "description": "Optional project_id from a previous generate_code run, used for run-graph linkage."},
            }, "required": []},
        },
    },
    "run_acceptance_review": {
        "type": "function",
        "function": {
            "name": "run_acceptance_review",
            "description": "Final acceptance gate after run_review is clean. Statically inspects the project against the user's request, README/docs, manifests, tests, entrypoints, and generated artifacts. Normal delivery should wait for acceptance to pass; if it returns issues, call run_fixer with its run_id. If the user explicitly asks to ship/download anyway, disclose the known issues and package the current state.",
            "parameters": {"type": "object", "properties": {
                "project_dir": {"type": "string", "description": "Absolute path to the project root in the sandbox. If omitted, uses the clean reviewer envelope."},
                "reviewer_run_id": {"type": "string", "description": "Optional run_id from the clean run_review call."},
                "project_id": {"type": "string", "description": "Optional project_id for run-graph linkage."},
            }, "required": []},
        },
    },
    "run_fixer": {
        "type": "function",
        "function": {
            "name": "run_fixer",
            "description": "Fallback targeted editor for issues identified by run_review or run_acceptance_review. For uploaded-project fixes, prefer run_aider_fix; run_fixer is mainly for greenfield/OpenHands projects or when Aider is disabled/unavailable. After reviewer-driven fixes, call run_review again. After docs-only acceptance fixes, call run_acceptance_review again; otherwise call run_review. Hard caps per user request: 3 reviewer-driven and 2 acceptance-driven successful fix cycles (run_aider_fix counts toward the same budget); a new user message resets the budget.",
            "parameters": {"type": "object", "properties": {
                "reviewer_run_id": {"type": "string", "description": "The run_id of the run_review or run_acceptance_review call whose issues you want to fix (e.g. 'run-bd6f9dc7b4e3'). If omitted, the most recent actionable review/acceptance run is used."},
            }, "required": []},
        },
    },
    "start_coder_workflow": {
        "type": "function",
        "function": {
            "name": "start_coder_workflow",
            "description": "Start a Coder Bot v2 workflow using the backend router. Modes: build_from_prompt uses OpenHands after planning; fix_uploaded_project uses Aider for existing uploaded-project edits; ask_uploaded_project uses read-only ProjectQA.",
            "parameters": {"type": "object", "properties": {
                "mode": {"type": "string", "description": "build_from_prompt | fix_uploaded_project | ask_uploaded_project"},
                "task": {"type": "string", "description": "The user's build, fix, or question task."},
                "project_id": {"type": "string", "description": "Optional uploaded project id. If omitted, uses the active project for this conversation."},
                "language": {"type": "string", "description": "Optional primary language for new builds."},
            }, "required": ["mode", "task"]},
        },
    },
    "run_aider_fix": {
        "type": "function",
        "function": {
            "name": "run_aider_fix",
            "description": "Apply surgical edits to an existing uploaded project using Aider in the Codebox worker. Use this for uploaded-project fixes instead of generate_code/OpenHands. After it returns, call run_review to verify. Counts toward the same per-user-request fix-cycle caps as run_fixer (3 reviewer-driven, 2 acceptance-driven).",
            "parameters": {"type": "object", "properties": {
                "project_dir": {"type": "string", "description": "Absolute project root in Codebox, e.g. /root/projects/proj-... . If omitted, uses the active uploaded project."},
                "task": {"type": "string", "description": "The user's requested fix/change, verbatim when possible."},
                "issue_run_id": {"type": "string", "description": "Optional reviewer/acceptance run_id whose issues should guide Aider."},
                "project_id": {"type": "string", "description": "Optional uploaded project id for workflow linkage."},
                "allowed_files": {"type": "array", "items": {"type": "string"}, "description": "Optional file scope Aider should focus on."},
            }, "required": ["task"]},
        },
    },
    "get_coder_workflow": {
        "type": "function",
        "function": {
            "name": "get_coder_workflow",
            "description": "Fetch workflow-level state for a Coder Bot v2 workflow.",
            "parameters": {"type": "object", "properties": {
                "workflow_id": {"type": "string", "description": "Workflow id returned by start_coder_workflow or upload-project."},
            }, "required": ["workflow_id"]},
        },
    },
    "cancel_coder_workflow": {
        "type": "function",
        "function": {
            "name": "cancel_coder_workflow",
            "description": "Cancel a Coder Bot v2 workflow and its active run, if any.",
            "parameters": {"type": "object", "properties": {
                "workflow_id": {"type": "string", "description": "Workflow id to cancel."},
            }, "required": ["workflow_id"]},
        },
    },
    "ask_project": {
        "type": "function",
        "function": {
            "name": "ask_project",
            "description": "Answer a question about an existing project's code (e.g. 'how does the ball physics work?', 'where is the score tracked?', 'what does GameEngine do?'). Read-only — never modifies files. The agent greps the project for relevant terms, reads the matching code, and produces a grounded answer with file:line citations. Use this INSTEAD of hand-rolling read_file + search_files when the user asks a question about the code. If the question is actually a change request (e.g. 'add a pause feature'), the result will flag it so you know to call generate_code or write_file instead.",
            "parameters": {"type": "object", "properties": {
                "question": {"type": "string", "description": "The user's question, verbatim or lightly cleaned. Be specific — pass the user's actual words rather than paraphrasing."},
                "project_dir": {"type": "string", "description": "Absolute path to the project root (e.g. '/root/projects/pong'). If omitted, the most recent successful builder run's project_dir is used."},
            }, "required": ["question"]},
        },
    },
    "search_files": {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for a text or regex pattern in project files. Returns matching lines with file paths and line numbers. Useful for finding function definitions, imports, TODOs, error strings, etc.",
            "parameters": {"type": "object", "properties": {
                "pattern": {"type": "string", "description": "Text or regex pattern to search for"},
                "path": {"type": "string", "description": "Directory to search in (default: /root)"},
                "file_pattern": {"type": "string", "description": "File glob filter, e.g. '*.py' or '*.ts'"},
            }, "required": ["pattern"]},
        },
    },
    "diff_files": {
        "type": "function",
        "function": {
            "name": "diff_files",
            "description": "Show unified diff between two files. Useful for comparing versions, reviewing changes, or debugging modifications.",
            "parameters": {"type": "object", "properties": {
                "path_a": {"type": "string", "description": "First file path"},
                "path_b": {"type": "string", "description": "Second file path"},
            }, "required": ["path_a", "path_b"]},
        },
    },
    "git_init": {
        "type": "function",
        "function": {
            "name": "git_init",
            "description": "Initialize a git repository in a project directory with a sensible .gitignore and initial commit.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Project directory path (default: /root)"},
                "language": {"type": "string", "description": "Primary language for .gitignore (python, javascript, rust, go, java)"},
            }, "required": []},
        },
    },
    "git_diff": {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show uncommitted changes in the git repository.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Repository directory (default: /root)"},
            }, "required": []},
        },
    },
    "git_commit": {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Stage all changes and create a git commit with the given message.",
            "parameters": {"type": "object", "properties": {
                "message": {"type": "string", "description": "Commit message"},
                "path": {"type": "string", "description": "Repository directory (default: /root)"},
            }, "required": ["message"]},
        },
    },
    "run_tests": {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Auto-detect and run tests in the project. Detects pytest, jest, cargo test, go test, etc.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Project directory (default: /root)"},
                "framework": {"type": "string", "description": "Force a specific framework: pytest, jest, cargo, go, npm"},
            }, "required": []},
        },
    },
    "lint_code": {
        "type": "function",
        "function": {
            "name": "lint_code",
            "description": "Auto-detect language and run linter/formatter. Python: ruff, JS/TS: prettier, Rust: cargo fmt, Go: gofmt.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Project directory (default: /root)"},
                "language": {"type": "string", "description": "Force language: python, javascript, typescript, rust, go"},
            }, "required": []},
        },
    },
    "resume_project": {
        "type": "function",
        "function": {
            "name": "resume_project",
            "description": "Resume a previous coding project. Reads the project's file listing from the sandbox and returns context from the last plan and file manifest so you can continue where you left off.",
            "parameters": {"type": "object", "properties": {
                "project_id": {"type": "string", "description": "Project ID to resume (from a previous generate_code or plan_project)"},
            }, "required": []},
        },
    },
}


# ── Text-based tool call parsing ──
# When models output tool calls as text instead of using native Ollama protocol,
# these functions extract and clean them.

def _extract_json_objects(text: str) -> list[str]:
    """Extract top-level JSON objects from text using brace-depth tracking.
    More reliable than regex for nested JSON structures."""
    objects = []
    i = 0
    while i < len(text):
        if text[i] == '{':
            depth = 0
            start = i
            in_str = False
            esc = False
            j = i
            while j < len(text):
                c = text[j]
                if esc:
                    esc = False
                elif c == '\\' and in_str:
                    esc = True
                elif c == '"' and not esc:
                    in_str = not in_str
                elif not in_str:
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                        if depth == 0:
                            objects.append(text[start:j + 1])
                            i = j
                            break
                j += 1
        i += 1
    return objects


def _normalize_tool_args(args):
    """Normalize tool arguments — handles string-encoded JSON from Ollama."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            args = {}
    if not isinstance(args, dict):
        args = {}
    return args


def _fix_json_newlines(text: str) -> str:
    """Fix JSON with unescaped newlines inside string values.
    Models often output JSON with real newlines in 'code' fields."""
    result = []
    in_str = False
    esc = False
    for i, c in enumerate(text):
        if esc:
            result.append(c)
            esc = False
            continue
        if c == '\\':
            result.append(c)
            if in_str:
                esc = True
            continue
        if c == '"' and not esc:
            in_str = not in_str
            result.append(c)
            continue
        if in_str and c == '\n':
            result.append('\\n')
            continue
        if in_str and c == '\t':
            result.append('\\t')
            continue
        result.append(c)
    return ''.join(result)


def parse_text_tool_calls(content: str, available_names: set) -> list[dict]:
    """Parse tool calls from model text when native Ollama tool protocol fails.
    Handles: raw JSON, <tool_call> tags, JSON in code blocks, bare JSON objects."""
    calls = []

    # Strip markdown code fences and model-specific special tokens for parsing
    stripped = re.sub(r'```(?:json|tool_call|tool)?\s*\n?', '', content).strip().rstrip('`')
    # Strip GPT-OSS / other model special tokens that appear after JSON
    stripped = re.sub(r'<\|(?:call|message|im_end|im_start|eot_id|end)\|>.*', '', stripped, flags=re.DOTALL).strip()

    # 1. Entire response is a single JSON tool call
    for _try_str in (stripped, _fix_json_newlines(stripped)):
        try:
            obj = json.loads(_try_str)
            if isinstance(obj, dict) and obj.get("name") in available_names and "arguments" in obj:
                return [{"function": {"name": obj["name"], "arguments": _normalize_tool_args(obj["arguments"])}}]
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    # 2. <tool_call>JSON</tool_call> tags (Qwen native format)
    # Also match <|call|>JSON patterns (GPT-OSS format)
    tag_matches = re.findall(
        r'<(?:tool[_\-]?call[s]?|\|call\|)>\s*(.*?)\s*</(?:tool[_\-]?call[s]?|\|call\|)>',
        content, re.DOTALL | re.IGNORECASE
    )
    for raw in tag_matches:
        for json_str in _extract_json_objects(raw):
            try:
                obj = json.loads(json_str)
                name = obj.get("name", "")
                args = _normalize_tool_args(obj.get("arguments", obj.get("parameters", {})))
                if name in available_names:
                    calls.append({"function": {"name": name, "arguments": args}})
            except (json.JSONDecodeError, TypeError):
                pass
    if calls:
        return calls

    # 2b. <function=NAME><parameter=KEY>VALUE</parameter>...</function> (qwen3-coder, Hermes XML-ish)
    # Example:
    #   <function=list_files>
    #   <parameter=path>
    #   /root/projects/proj-abc
    #   </parameter>
    #   </function>
    for fn_match in re.finditer(
        r'<function\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*>(.*?)</function\s*>',
        content, re.DOTALL | re.IGNORECASE,
    ):
        fname = fn_match.group(1).strip()
        body = fn_match.group(2)
        if fname not in available_names:
            continue
        args: dict = {}
        for p_match in re.finditer(
            r'<parameter\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*>(.*?)</parameter\s*>',
            body, re.DOTALL | re.IGNORECASE,
        ):
            pkey = p_match.group(1).strip()
            pval = p_match.group(2).strip()
            # Coerce obvious literals so numeric/bool params still work
            if pval.lower() in ("true", "false"):
                args[pkey] = (pval.lower() == "true")
            else:
                try:
                    if re.fullmatch(r'-?\d+', pval):
                        args[pkey] = int(pval)
                    elif re.fullmatch(r'-?\d+\.\d+', pval):
                        args[pkey] = float(pval)
                    else:
                        args[pkey] = pval
                except (ValueError, TypeError):
                    args[pkey] = pval
        calls.append({"function": {"name": fname, "arguments": args}})
    if calls:
        return calls

    # 3. JSON objects with name+arguments anywhere in text
    for json_str in _extract_json_objects(stripped):
        try:
            obj = json.loads(json_str)
            if isinstance(obj, dict) and obj.get("name") in available_names:
                args = obj.get("arguments", obj.get("parameters", {}))
                if args is not None:
                    calls.append({"function": {"name": obj["name"], "arguments": _normalize_tool_args(args)}})
        except (json.JSONDecodeError, TypeError):
            pass

    # 4. Try extracting from code blocks specifically
    if not calls:
        code_blocks = re.findall(r'```(?:json|tool_call|tool)?\s*\n(.*?)\n\s*```', content, re.DOTALL)
        for block in code_blocks:
            for json_str in _extract_json_objects(block.strip()):
                for _try in (json_str, _fix_json_newlines(json_str)):
                    try:
                        obj = json.loads(_try)
                        if isinstance(obj, dict) and obj.get("name") in available_names:
                            args = obj.get("arguments", obj.get("parameters", {}))
                            if args is not None:
                                calls.append({"function": {"name": obj["name"], "arguments": _normalize_tool_args(args)}})
                                break
                    except (json.JSONDecodeError, TypeError):
                        pass

    # 5. Python function call syntax: run_shell("cmd"), write_file("path", "content"), etc.
    #    Models sometimes write tool calls as Python code instead of using the protocol.
    if not calls:
        calls = _parse_python_tool_calls(content, available_names)

    return calls


def _parse_python_tool_calls(content: str, available_names: set) -> list[dict]:
    """Parse Python-style function calls from model output.
    Catches patterns like: run_shell("pip install foo"), write_file("/root/app.py", '''code'''), etc."""
    calls = []

    # Extract all code blocks, or use full content if no code blocks
    code_blocks = re.findall(r'```(?:\w*)\n(.*?)\n\s*```', content, re.DOTALL)
    texts_to_scan = code_blocks if code_blocks else [content]

    for text in texts_to_scan:
        for name in available_names:
            # Match tool_name( ... ) — find the opening paren after the tool name
            pattern = rf'\b{re.escape(name)}\s*\('
            for m in re.finditer(pattern, text):
                start = m.end()  # position after opening (
                args = _extract_balanced_parens(text, start)
                if args is None:
                    continue
                parsed = _parse_python_args(name, args)
                if parsed:
                    calls.append({"function": {"name": name, "arguments": parsed}})
    return calls


def _extract_balanced_parens(text: str, start: int) -> str | None:
    """Extract content between balanced parentheses starting at position after opening paren."""
    depth = 1
    i = start
    in_str = False
    str_char = None
    in_triple = False
    esc = False
    while i < len(text):
        c = text[i]
        if esc:
            esc = False
            i += 1
            continue
        if c == '\\' and in_str and not in_triple:
            esc = True
            i += 1
            continue
        # Triple-quote detection
        if not in_str and i + 2 < len(text) and text[i:i+3] in ("'''", '"""'):
            in_str = True
            in_triple = True
            str_char = text[i:i+3]
            i += 3
            continue
        if in_triple and i + 2 < len(text) and text[i:i+3] == str_char:
            in_str = False
            in_triple = False
            str_char = None
            i += 3
            continue
        if not in_str and c in ('"', "'"):
            in_str = True
            str_char = c
            i += 1
            continue
        if in_str and not in_triple and c == str_char:
            in_str = False
            str_char = None
            i += 1
            continue
        if not in_str:
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    return text[start:i]
        i += 1
    return None


def _parse_python_args(tool_name: str, raw_args: str) -> dict | None:
    """Parse Python function arguments into a tool arguments dict."""
    raw_args = raw_args.strip()
    if not raw_args:
        return {}

    # ── Handle keyword arguments first: tool(key="value", key2="value2") ──
    # Match patterns like: command="...", path="/root/...", task="..."
    kw_pattern = re.findall(r'(\w+)\s*=\s*(?:"""(.*?)"""|\'\'\'(.*?)\'\'\'|"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\')', raw_args, re.DOTALL)
    if kw_pattern:
        result = {}
        for kw_match in kw_pattern:
            key = kw_match[0]
            # Pick the first non-empty capture group (triple-double, triple-single, double, single)
            val = kw_match[1] or kw_match[2] or kw_match[3] or kw_match[4]
            val = val.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\')
            result[key] = val
        if result:
            return result

    # ── Positional arguments fallback ──
    # Try to evaluate string literals safely
    # We'll extract quoted string arguments
    args_list = []
    i = 0
    while i < len(raw_args):
        c = raw_args[i]
        if c in (' ', ',', '\n', '\t'):
            i += 1
            continue
        # Triple-quoted string
        if i + 2 < len(raw_args) and raw_args[i:i+3] in ("'''", '"""'):
            q = raw_args[i:i+3]
            end = raw_args.find(q, i + 3)
            if end == -1:
                return None
            args_list.append(raw_args[i+3:end])
            i = end + 3
            continue
        # Single/double quoted string
        if c in ('"', "'"):
            j = i + 1
            esc = False
            while j < len(raw_args):
                if esc:
                    esc = False
                elif raw_args[j] == '\\':
                    esc = True
                elif raw_args[j] == c:
                    break
                j += 1
            if j < len(raw_args):
                # Unescape basic escape sequences
                s = raw_args[i+1:j].replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\')
                args_list.append(s)
                i = j + 1
                continue
            return None
        # f-string — skip
        if c == 'f' and i + 1 < len(raw_args) and raw_args[i+1] in ('"', "'"):
            return None
        # Bare word or number — skip to next comma
        j = i
        depth = 0
        while j < len(raw_args):
            if raw_args[j] == ',' and depth == 0:
                break
            if raw_args[j] in ('(', '[', '{'):
                depth += 1
            elif raw_args[j] in (')', ']', '}'):
                depth -= 1
            j += 1
        token = raw_args[i:j].strip()
        if token:
            args_list.append(token)
        i = j + 1

    if not args_list:
        return None

    # Map positional args to known tool parameter names
    TOOL_PARAMS = {
        "execute_code": ["code", "language"],
        "run_shell": ["command"],
        "write_file": ["path", "content"],
        "read_file": ["path"],
        "list_files": ["path"],
        "download_file": ["filename"],
        "download_project": ["filenames", "project_name"],
        "delete_file": ["path"],
        "plan_project": ["task", "language", "constraints"],
        "run_review": ["project_dir", "project_id"],
        "run_acceptance_review": ["project_dir", "reviewer_run_id", "project_id"],
        "search_files": ["pattern", "path", "file_pattern"],
        "diff_files": ["path_a", "path_b"],
        "git_init": ["path", "language"],
        "git_diff": ["path"],
        "git_commit": ["message", "path"],
        "run_tests": ["path", "framework"],
        "lint_code": ["path", "language"],
        "resume_project": ["project_id"],
        "research": ["query"],
        "fetch_url": ["url"],
        "generate_code": ["task", "language", "context"],
        "start_coder_workflow": ["mode", "task", "project_id"],
        "run_aider_fix": ["project_dir", "task", "issue_run_id"],
        "get_coder_workflow": ["workflow_id"],
        "cancel_coder_workflow": ["workflow_id"],
    }

    param_names = TOOL_PARAMS.get(tool_name)
    if not param_names:
        # Unknown tool — use first arg as "input"
        return {"input": args_list[0]} if args_list else None

    result = {}
    for idx, val in enumerate(args_list):
        if idx < len(param_names):
            result[param_names[idx]] = val
    return result if result else None


def strip_tool_calls(content: str) -> str:
    """Remove tool call artifacts from content so the user sees clean text."""
    # Remove <tool_call>...</tool_call>
    content = re.sub(
        r'<tool[_\-]?call[s]?>\s*.*?\s*</tool[_\-]?call[s]?>',
        '', content, flags=re.DOTALL | re.IGNORECASE
    )
    # Remove qwen3-coder / Hermes-style <function=name>...<parameter=k>v</parameter>...</function>
    content = re.sub(
        r'<function\s*=\s*[A-Za-z_][A-Za-z0-9_]*\s*>.*?</function\s*>',
        '', content, flags=re.DOTALL | re.IGNORECASE
    )
    # Remove ```json blocks containing tool calls
    content = re.sub(
        r'```(?:json|tool_call|tool)?\s*\n?\s*\{[^`]*"name"[^`]*\}\s*\n?\s*```',
        '', content, flags=re.DOTALL
    )
    # Remove bare JSON tool call objects (name + arguments pattern)
    content = re.sub(
        r'\{\s*"name"\s*:\s*"[^"]*"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\}',
        '', content, flags=re.DOTALL
    )
    return content.strip()


# ── Sandbox venv management ──
_sandbox_venv_ready = False

async def _ensure_venv(http):
    """Lazily create a Python venv in the CodeBox sandbox. Called once per server lifetime."""
    global _sandbox_venv_ready
    if _sandbox_venv_ready:
        return True
    try:
        r = await http.post(f"{config.CODEBOX_URL}/command", json={
            "command": (
                "test -f /root/venv/bin/python3 || "
                "(python3 -m venv /root/venv && /root/venv/bin/pip3 install --upgrade pip -q 2>/dev/null); "
                "echo VENV_OK"
            ),
            "timeout": 30
        }, timeout=35)
        result = r.json()
        if "VENV_OK" in result.get("stdout", ""):
            _sandbox_venv_ready = True
            print("[SANDBOX] venv ready at /root/venv")
            return True
    except Exception as e:
        print(f"[SANDBOX] venv setup error: {e}")
    return False


# Language → filename substring hints for KB retrieval bias. Each entry lists the filename
# fragments (case-insensitive) that should be preferred when the task targets that language.
# Frameworks/runtimes commonly used WITH a language are included so e.g. a Java task biases
# toward javafx_ / swing_ / spring_ chunks even though those aren't "the Java language" itself.
# Substrings are matched against the chunk's source filename via rag.query(prefer_filename_hints).
_KB_LANG_HINTS: dict[str, list[str]] = {
    "python":     ["python_", "django_", "flask_", "fastapi_", "pandas_", "numpy_",
                   "pytorch_", "sqlalchemy_"],
    "java":       ["java_", "javafx_", "swing_", "spring_"],
    "kotlin":     ["kotlin_", "android_", "spring_"],
    "javascript": ["javascript_", "nodejs_", "express_", "react_", "vue_", "angular_",
                   "nextjs_", "svelte_", "jquery_", "npm_"],
    "typescript": ["typescript_", "nextjs_", "react_", "angular_", "nodejs_", "express_"],
    "rust":       ["rust_"],
    "go":         ["go_", "gin_"],
    "c":          ["c_reference", "cpp_"],
    "cpp":        ["cpp_", "c_reference", "cmake_"],
    "c++":        ["cpp_", "c_reference", "cmake_"],
    "csharp":     ["csharp_", "aspnet_", "unity_"],
    "c#":         ["csharp_", "aspnet_", "unity_"],
    "swift":      ["swift_", "swiftui_", "ios_"],
    "ruby":       ["ruby_", "rails_"],
    "php":        ["php_", "laravel_"],
    "lua":        ["lua_"],
    "elixir":     ["elixir_"],
    "haskell":    ["haskell_"],
    "scala":      ["scala_"],
    "dart":       ["dart_", "flutter_"],
    "perl":       ["perl_"],
    "html":       ["html_", "css_", "tailwind_", "bootstrap_"],
    "css":        ["css_", "tailwind_", "bootstrap_"],
    "bash":       ["bash_", "linux_", "vim_"],
    "sh":         ["bash_", "linux_"],
    "powershell": ["powershell_"],
    "sql":        ["sql_", "postgres_", "mysql_", "mongodb_", "redis_", "sqlalchemy_"],
}

# Free-form tokens that hint at a framework even if they don't appear in the language field.
# Searched in the task text (lowercased) when the language map alone doesn't pick a hint.
_KB_TASK_HINTS: dict[str, list[str]] = {
    "javafx":      ["javafx_"],
    "swing":       ["swing_"],
    "spring boot": ["spring_"],
    "react":       ["react_"],
    "vue":         ["vue_"],
    "angular":     ["angular_"],
    "next.js":     ["nextjs_"],
    "tailwind":    ["tailwind_"],
    "django":      ["django_"],
    "flask":       ["flask_"],
    "fastapi":     ["fastapi_"],
    "unity":       ["unity_"],
    "unreal":      ["unreal_"],
    "godot":       ["godot_"],
    "kubernetes":  ["kubernetes_"],
    "docker":      ["docker_"],
    "terraform":   ["terraform_"],
    "ansible":     ["ansible_"],
}


def _kb_filename_hints_for_language(language: str, task: str = "") -> list[str] | None:
    """Pick filename substring hints that should be preferred when retrieving KB chunks.

    Returns None when no hints apply, so callers can pass through to unbiased retrieval.
    """
    hints: list[str] = []
    lang_key = (language or "").lower().strip()
    if lang_key in _KB_LANG_HINTS:
        hints.extend(_KB_LANG_HINTS[lang_key])
    task_lower = (task or "").lower()
    for keyword, more in _KB_TASK_HINTS.items():
        if keyword in task_lower:
            hints.extend(more)
    if not hints:
        return None
    # De-duplicate while preserving order.
    seen = set()
    out = []
    for h in hints:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _get_run_cmd(language: str, filepath: str) -> str:
    """Return the shell command to run a file for the given language."""
    lang = language.lower()
    if lang in ("python", "python3", "py"):
        return f"python3 {filepath}"
    elif lang in ("javascript", "js"):
        return f"node {filepath}"
    elif lang in ("bash", "sh"):
        return f"bash {filepath}"
    elif lang in ("typescript", "ts"):
        return f"npx ts-node {filepath}"
    elif lang in ("rust", "rs"):
        return f"rustc {filepath} -o /tmp/_hc_bin && /tmp/_hc_bin"
    elif lang in ("go",):
        return f"go run {filepath}"
    elif lang in ("c",):
        return f"gcc {filepath} -o /tmp/_hc_bin -lm && /tmp/_hc_bin"
    elif lang in ("cpp", "c++"):
        return f"g++ {filepath} -o /tmp/_hc_bin && /tmp/_hc_bin"
    return filepath


# ── Tool execution dispatcher ──

async def exec_tool(
    http,
    events,
    name: str,
    args: dict,
    conv_id: str,
    custom_tool_map: dict = None,
    connector_tool_name_map: dict = None,
    conv_model: str = "",
    kb_ids: list = None,
    artifact_message_id: int | None = None,
) -> str:
    """Execute a built-in or custom tool and return the result string."""
    custom_tool_map = custom_tool_map or {}
    connector_tool_name_map = connector_tool_name_map or {}
    try:
        # ─── v2 workflow gate (deterministic over persuasion) ───────────────
        # Two interlocking states gate every non-meta tool call. Both fire
        # only for v2 personas (detected by model_config.name containing "v2").
        # v1 stays untouched.
        #
        #   State 1 — PENDING REVIEW: a builder or fixer just succeeded but
        #     no reviewer ran after it. Block everything except run_review.
        #     This is what stops the v1 antipattern of read_file → write_file
        #     → javac chains after generate_code.
        #
        #   State 2 — PENDING FIX: the most recent run is a reviewer with
        #     status=issues or status=error. Block everything except
        #     run_fixer (and run_review, in case the model wants to re-check).
        #     This is what stops the model from ignoring reviewer findings
        #     and shipping (or hand-editing one file at a time).
        #
        #   State 3 — Q&A TERMINAL: the most recent run is a successful `qa`
        #     and the question wasn't a change request. The model should be
        #     producing a plain-text response, not running more tools. Block
        #     everything except ask_project itself (so follow-up questions
        #     still work). If the user actually wants a change, they need to
        #     say so — ask_project will flag it and the gate will release.
        #
        #   State 4 — CYCLE LIMIT (run_fixer only): if there are already ≥3
        #     succeeded fixer runs on this conv, the same class of issue is
        #     persisting and another fixer call won't help. Refuse and tell
        #     the model to summarize for the user. The persona prompt has
        #     said "Hard cap: 3 review/fix cycles" since Phase 2; this
        #     enforces it server-side instead of trusting the model to obey.
        #
        # Cache the v2 check result once per exec_tool call so the gate
        # doesn't hit the DB for each sub-state. Initialized to None =
        # "not checked yet"; False/True = cached result.
        _v2_cached: bool | None = None
        async def _check_v2(cr=None) -> bool:
            nonlocal _v2_cached
            if _v2_cached is None:
                _v2_cached = await _is_v2_persona(conv_id, conv_row=cr)
            return _v2_cached

        # Q&A is read-only and terminal for the current turn. Some review/fix
        # tools are whitelisted below so the normal state gate can allow the
        # intended next step after builder/reviewer runs; block those explicitly
        # when the most recent meaningful run is a non-change ProjectQA answer.
        if conv_id and name not in {"ask_project", "get_coder_workflow", "cancel_coder_workflow"}:
            try:
                _runs_qt = await db.get_runs_by_conversation(conv_id, limit=12)
                _terminal_qa_run = None
                for _r in _runs_qt:
                    _role_qt = _r.get("role", "")
                    if _role_qt == "qa":
                        _env_qt = _r.get("result_envelope") or {}
                        if (_r.get("status") == "succeeded"
                                and not _env_qt.get("looks_like_change_request", False)):
                            _terminal_qa_run = _r
                        break
                    if (_role_qt in {"reviewer", "acceptance", "fixer", "aider.fix"}
                            or _role_qt.startswith("builder")):
                        break
                if _terminal_qa_run is not None:
                    # Turn-scoped: a QA answer is terminal only for the turn it
                    # answered. A newer user message ("now run the tests")
                    # releases the gate instead of blocking forever.
                    _uts_qt = await _latest_user_msg_ts(conv_id)
                    _qa_ts = _parse_ts_loose(_terminal_qa_run.get("started_at"))
                    if (_uts_qt is not None and _qa_ts is not None
                            and _qa_ts < _uts_qt):
                        _terminal_qa_run = None
                if _terminal_qa_run is not None and await _check_v2():
                    _qid = _terminal_qa_run.get("id", "?")
                    await events.emit(conv_id, "tool_end", {
                        "tool": name, "icon": "code",
                        "status": f"⛔ Blocked — answer the user (ask_project {_qid[:14]}… is terminal)",
                    })
                    print(f"[v2-gate] state=qa-terminal blocked tool={name} "
                          f"trigger={_qid}", flush=True)
                    return (
                        f"BLOCKED — ask_project ({_qid}) just answered the user's question. "
                        f"The user asked something; you have the answer. Your VERY NEXT output "
                        f"MUST be plain text relaying that answer to the user.\n\n"
                        f"Do NOT call run_review, run_fixer, run_aider_fix, generate_code, "
                        f"read_file, write_file, run_shell, download_project, or any other "
                        f"tool. The project Q&A path is read-only.\n\n"
                        f"If the user follows up with another question, call ask_project again. "
                        f"If they explicitly request a change, route that new turn through "
                        f"fix_uploaded_project or a write workflow."
                    )
            except Exception as _qte:
                print(f"[v2-gate] qa-terminal check failed (non-fatal): {_qte}")

        if (conv_id
                and not _uploaded_project_tool_allowed_during_bootstrap(name)
                and await _check_v2()):
            try:
                _wf_up = await db.get_latest_coder_workflow(conv_id)
                _runs_up = await db.get_runs_by_conversation(conv_id, limit=20)
                _gate_state_up = _uploaded_project_manual_gate_state(_wf_up, _runs_up)
                if _gate_state_up:
                    _pid_up = (_wf_up or {}).get("project_id") or ""
                    _project_dir_up = f"/root/projects/{_pid_up}" if _pid_up else ""
                    _task_up = await _latest_user_task_text(conv_id)
                    _body_up = _uploaded_project_bootstrap_block_message(
                        name,
                        _wf_up or {},
                        _project_dir_up,
                        _task_up,
                        _gate_state_up,
                    )
                    await events.emit(conv_id, "tool_end", {
                        "tool": name, "icon": "code",
                        "status": (
                            "⛔ Blocked — uploaded-project fixes use Aider first"
                            if _gate_state_up == "bootstrap"
                            else "⛔ Blocked — uploaded-project run already in progress"
                        ),
                    })
                    print(f"[v2-gate] state=uploaded-project-{_gate_state_up} "
                          f"blocked tool={name} workflow={(_wf_up or {}).get('id','?')}",
                          flush=True)
                    return _body_up
            except Exception as _upe:
                print(f"[v2-gate] uploaded-project bootstrap check failed (non-fatal): {_upe}")

        _runs_for_cap = None
        _uts_cap = None
        if conv_id and name in {"run_fixer", "run_aider_fix"}:
            _parent_role_for_cap = "reviewer"
            try:
                # limit=50 so the cap window isn't silently truncated by run
                # scroll; counters below are additionally scoped to the
                # current user request via _runs_since.
                _runs_for_cap = await db.get_runs_by_conversation(conv_id, limit=50)
                _uts_cap = await _latest_user_msg_ts(conv_id)
                _run_role_by_id_cap = {r.get("id"): r.get("role") for r in _runs_for_cap}
                _requested_parent_id = (args.get("reviewer_run_id") or "").strip()
                _parent_role_for_cap = ""
                if _requested_parent_id:
                    _parent_role_for_cap = _run_role_by_id_cap.get(_requested_parent_id, "")
                if not _parent_role_for_cap:
                    for _r in _runs_for_cap:
                        if _r.get("role") not in {"reviewer", "acceptance"}:
                            continue
                        _env = _r.get("result_envelope") or {}
                        if (_env.get("status") or "").lower() in {"issues", "error"}:
                            _parent_role_for_cap = _r.get("role", "reviewer")
                            break
                if not _parent_role_for_cap:
                    _parent_role_for_cap = "reviewer"

                # Uploaded-project fixes are owned by Aider. This redirect is
                # deliberately before cycle/research gates so stale personas
                # that still call run_fixer do not get stuck in the old scoped
                # Fixer loop. v2-only: a v1 persona's run_fixer must not be
                # silently rerouted.
                if (name == "run_fixer" and _parent_role_for_cap == "reviewer"
                        and getattr(config, "AIDER_ENABLED", True)
                        and await _check_v2()):
                    _requested_parent_id = (args.get("reviewer_run_id") or "").strip()
                    _issue_run_for_aider = await _latest_actionable_issue_run(conv_id, _requested_parent_id)
                    _aider_ctx = await _uploaded_project_aider_context(
                        conv_id, issue_run=_issue_run_for_aider,
                    )
                    if _issue_run_for_aider and _aider_ctx:
                        _issue_run_id = _issue_run_for_aider.get("id", "")
                        _project_dir = _aider_ctx.get("project_dir") or ""
                        _task = _task_from_issue_run(
                            await _latest_user_task_text(conv_id),
                            _issue_run_for_aider,
                        )
                        await events.emit(conv_id, "tool_end", {
                            "tool": "run_fixer", "icon": "wrench",
                            "status": "↪ Routing uploaded-project fix to Aider",
                        })
                        print(f"[v2-gate] redirecting run_fixer to run_aider_fix "
                              f"for uploaded project issue_run={_issue_run_id}", flush=True)
                        return await exec_tool(
                            http, events, "run_aider_fix",
                            {
                                "task": _task,
                                "project_dir": _project_dir,
                                "issue_run_id": _issue_run_id,
                            },
                            conv_id,
                            custom_tool_map=custom_tool_map,
                            connector_tool_name_map=connector_tool_name_map,
                            conv_model=conv_model,
                            kb_ids=kb_ids,
                            artifact_message_id=artifact_message_id,
                        )

                def _fixer_source_role_cap(_fr):
                    _fenv = _fr.get("result_envelope") or {}
                    return (_fenv.get("source_role")
                            or _run_role_by_id_cap.get(_fr.get("parent_run_id"))
                            or "reviewer")

                # Turn-scoped (G1) and Aider-inclusive (G2): only successful
                # fix runs since the latest user message count, and aider.fix
                # consumes the same budget as the scoped Fixer.
                _fixer_succ = sum(
                    1 for r in _runs_since(_runs_for_cap, _uts_cap)
                    if (r.get("role") in {"fixer", "aider.fix"}
                        and r.get("status") == "succeeded"
                        and _fixer_source_role_cap(r) == _parent_role_for_cap)
                )
                _cap_limit = 2 if _parent_role_for_cap == "acceptance" else 3
                if _fixer_succ >= _cap_limit:
                    # v1 doesn't run this loop, so cap only applies to v2.
                    if await _check_v2():
                        # Pull the latest actionable summary so the model has
                        # specifics to relay to the user.
                        _last_rev = next(
                            (r for r in _runs_for_cap if r.get("role") == _parent_role_for_cap),
                            None,
                        )
                        _rev_sum = ""
                        _rev_issues = []
                        if _last_rev:
                            _rev_env = _last_rev.get("result_envelope") or {}
                            _rev_sum = (_rev_env.get("summary") or "")[:300]
                            _rev_issues = _rev_env.get("issues") or []
                        _issue_lines = []
                        for _i, _iss in enumerate(_rev_issues[:3], 1):
                            _issue_lines.append(
                                f"  {_i}. [{_iss.get('severity','?')}] "
                                f"{_iss.get('file','?')}"
                                + (f":{','.join(str(x) for x in _iss.get('lines') or [])}" if _iss.get('lines') else "")
                                + f" — {(_iss.get('summary','') or '')[:160]}"
                            )
                        await events.emit(conv_id, "tool_end", {
                            "tool": name, "icon": "wrench",
                            "status": f"⛔ Cycle cap reached ({_fixer_succ}/{_cap_limit}) — summarize and stop",
                        })
                        print(f"[v2-gate] CYCLE CAP: blocking {name} (already "
                              f"{_fixer_succ} succeeded {_parent_role_for_cap}-driven fix runs "
                              f"since the latest user message)", flush=True)
                        return (
                            f"BLOCKED — Hard cap of {_cap_limit} {_parent_role_for_cap}/fix "
                            f"cycles already attempted for this user request "
                            f"({_fixer_succ} successful fix runs).\n\n"
                            f"The same class of issue is persisting and another fixer call "
                            f"will not help. Your VERY NEXT output MUST be plain text to the "
                            f"user that:\n"
                            f"  1. Summarizes what was changed across the {_fixer_succ} fix cycles\n"
                            f"  2. States the remaining issue with file:line references"
                            + (f"\n     (latest reviewer: \"{_rev_sum}\")" if _rev_sum else "")
                            + (("\n" + "\n".join(_issue_lines)) if _issue_lines else "")
                            + "\n  3. Asks the user for guidance — what behavior they actually "
                            f"want, or whether to skip this issue and ship anyway.\n\n"
                            f"Do NOT call run_fixer, run_aider_fix, run_review, generate_code, "
                            f"write_file, run_shell, or plan_project. You MAY call download_project / "
                            f"download_file to deliver what was built so far if the user wants "
                            f"to ship as-is. Otherwise, respond to the user with text."
                        )
            except Exception as _ce:
                print(f"[v2-gate] cycle cap check failed (non-fatal): {_ce}")

            # ─── State 5+6 — STUCK_FIX / FINAL_CYCLE — research nudge ───────
            # If the model is about to call run_fixer for the 2nd or 3rd time
            # AND either (a) the same issue signatures returned across reviewer
            # cycles, or (b) this would be the 3rd (last-allowed) fixer run,
            # block until deep_research has been called. The persona prompt
            # tells the model to do this; the gate enforces it server-side.
            #
            # Uses the same _runs_for_cap walk as the cycle cap — cheap because
            # we already paid for that db hit above. _runs_for_cap is
            # initialized to None before the first try block so it's always
            # in scope here.
            try:
                _runs_sf = _runs_for_cap
                if _runs_sf is None:
                    _runs_sf = await db.get_runs_by_conversation(conv_id, limit=50)
                if _parent_role_for_cap == "acceptance":
                    raise StopAsyncIteration("acceptance fixes do not use research nudge")
                # Same turn-scoped window as the cycle cap, counting both fix
                # paths — the gates must agree on how many cycles happened.
                _fsucc_sf = sum(
                    1 for r in _runs_since(_runs_sf, _uts_cap)
                    if (r.get("role") in {"fixer", "aider.fix"}
                        and r.get("status") == "succeeded"
                        and ((r.get("result_envelope") or {}).get("source_role")
                             or "reviewer") != "acceptance")
                )
                # 1 ≤ fixer_succ ≤ 2: between first failure and final cycle.
                # 0 = first attempt, no gate. ≥3 = cap (handled above).
                # Research nudge text is fixer-specific; Aider already requires
                # a follow-up run_review, so only run_fixer is gated here.
                if name == "run_fixer" and 1 <= _fsucc_sf <= 2 and await _check_v2():
                    _latest_rev_sf = next(
                        (r for r in _runs_sf if r.get("role") == "reviewer"),
                        None,
                    )
                    if _latest_rev_sf:
                        _cur_iss = _issue_signatures(_latest_rev_sf.get("result_envelope") or {})
                        _research_done = await _deep_research_called_since(
                            conv_id, _latest_rev_sf.get("started_at")
                        )

                        # State 5 — STUCK: same issue signature seen in a prior
                        # reviewer run AND no research call since.
                        _is_stuck = False
                        if _cur_iss:
                            _prior_revs_sf = [
                                r for r in _runs_sf
                                if r.get("role") == "reviewer"
                                and r.get("id") != _latest_rev_sf.get("id")
                            ]
                            if _prior_revs_sf:
                                _prior_rev_sf = _prior_revs_sf[0]
                                _prior_iss = _issue_signatures(
                                    _prior_rev_sf.get("result_envelope") or {}
                                )
                                if _cur_iss & _prior_iss:
                                    _is_stuck = True

                        # State 6 — FINAL CYCLE: about to use the last fixer
                        # slot (3rd attempt). Research before the last shot.
                        _is_final = (_fsucc_sf == 2)

                        if (_is_stuck or _is_final) and not _research_done:
                            _why_label = ("STUCK" if _is_stuck else "FINAL_CYCLE")
                            _gate_status = (
                                "⛔ Same issue returned — call Agent Research first"
                                if _is_stuck else
                                "⛔ Final fixer cycle — call Agent Research first"
                            )
                            # Pull a representative error to seed the research topic.
                            _seed_iss = (_latest_rev_sf.get("result_envelope") or {}).get("issues") or []
                            _seed_lines = []
                            for _i, _iss in enumerate(_seed_iss[:2], 1):
                                _seed_lines.append(
                                    f"  {_i}. [{_iss.get('severity','?')}] "
                                    f"{_iss.get('file','?')}"
                                    + (f":{','.join(str(x) for x in _iss.get('lines') or [])}" if _iss.get('lines') else "")
                                    + f" — {(_iss.get('summary','') or '')[:160]}"
                                )
                            _seed_block = ("\n" + "\n".join(_seed_lines)) if _seed_lines else ""

                            await events.emit(conv_id, "tool_end", {
                                "tool": "run_fixer", "icon": "wrench",
                                "status": _gate_status,
                            })
                            print(f"[v2-gate] {_why_label}: blocking run_fixer "
                                  f"(fixer_succ={_fsucc_sf}, stuck={_is_stuck}, "
                                  f"final={_is_final}, research_done={_research_done})", flush=True)

                            if _is_stuck:
                                _reason = (
                                    f"BLOCKED — run_review returned the same issue(s) you already "
                                    f"attempted to fix in a previous run_fixer cycle ({_fsucc_sf} fixer "
                                    f"run(s) so far). Calling run_fixer again without new information "
                                    f"will produce the same result.\n"
                                )
                            else:
                                _reason = (
                                    f"BLOCKED — this would be your 3rd (final) run_fixer call. "
                                    f"After it, the cycle cap blocks any further fixer runs. "
                                    f"Spend one round on web research before the last shot.\n"
                                )

                            return (
                                _reason
                                + "\nRecurring issue(s):" + _seed_block + "\n\n"
                                + "Your VERY NEXT tool call MUST be:\n"
                                + "  deep_research(topic='<exact error message + library/version>', depth=2)\n\n"
                                + "After Agent Research (`deep_research`) returns, call run_fixer — the Fixer will see the "
                                + "research result in your conversation and use it to ground the next "
                                + "edit. Use depth=2 (fast); only use depth=3 if the issue is genuinely "
                                + "obscure. Do NOT call run_fixer, generate_code, write_file, run_shell, "
                                + "or run_review until deep_research has run."
                            )
            except StopAsyncIteration:
                pass
            except Exception as _sfe:
                print(f"[v2-gate] stuck/final-cycle check failed (non-fatal): {_sfe}")

            # Phantom run_fixer guard: block run_fixer when the latest run
            # isn't a reviewer/acceptance run with status='issues'/'error'. Without this,
            # a hallucinated reviewer_run_id (the model sometimes invents
            # one right after generate_code) falls through to fixer.py,
            # which returns "no envelope to fix" — burning a cap slot for
            # zero work AND giving the model false signal that a fix
            # happened. State 2 of the gate (PENDING_FIX) covers the
            # legitimate "reviewer just ran with issues → call run_fixer"
            # case; this guard handles every other case.
            try:
                if await _check_v2():
                    _runs_pf = await db.get_runs_by_conversation(conv_id, limit=20)
                    _latest_meaningful = None
                    _MEANINGFUL_STATUSES = {"succeeded", "issues", "clean", "partial",
                                            "stuck", "failed", "error"}
                    for _r in _runs_pf:
                        _role_pf = _r.get("role", "")
                        _st_pf = (_r.get("status") or "").lower()
                        if (_role_pf in {"reviewer", "acceptance"} or _role_pf.startswith("builder")
                                or _role_pf == "fixer") and _st_pf in _MEANINGFUL_STATUSES:
                            _latest_meaningful = _r
                            break

                    _allow_fixer = False
                    if (_latest_meaningful
                            and _latest_meaningful.get("role") in {"reviewer", "acceptance"}):
                        _env_pf = _latest_meaningful.get("result_envelope") or {}
                        _rstatus_pf = (_env_pf.get("status") or "").lower()
                        if _rstatus_pf in ("issues", "error"):
                            _allow_fixer = True

                    if not _allow_fixer:
                        _trig_role = (_latest_meaningful or {}).get("role", "(none)")
                        _trig_id = (_latest_meaningful or {}).get("id", "?")
                        _trig_status = (_latest_meaningful or {}).get("status", "?")
                        # Surface a project_dir if we can find one.
                        _pd_hint = ""
                        for _r in _runs_pf:
                            _env_h = _r.get("result_envelope") or {}
                            _pd_try = (_env_h.get("project_dir") or "").strip()
                            if _pd_try:
                                _pd_hint = _pd_try
                                break
                        await events.emit(conv_id, "tool_end", {
                            "tool": "run_fixer", "icon": "wrench",
                            "status": "⛔ run_fixer needs an actionable review envelope first",
                        })
                        print(f"[v2-gate] PHANTOM FIXER: blocking run_fixer "
                              f"(latest meaningful run is {_trig_role}/{_trig_status} "
                              f"{(_trig_id or '')[:14]}, not review/acceptance with issues)", flush=True)
                        return (
                            f"BLOCKED — run_fixer requires a recent reviewer or acceptance envelope with "
                            f"issues, but the most recent meaningful run is "
                            f"'{_trig_role}' (status={_trig_status}). There is no "
                            f"actionable envelope to fix.\n\n"
                            f"Your VERY NEXT tool call MUST be:\n"
                            f"  run_review(project_dir='{_pd_hint or '/root/projects/...'}')\n\n"
                            f"Do not pass a hallucinated reviewer_run_id. Run review/acceptance "
                            f"first; if it returns issues, call run_fixer with that real id."
                        )
            except Exception as _pe:
                print(f"[v2-gate] phantom-fixer check failed (non-fatal): {_pe}")

        # ─── Anti-rebuild guard for generate_code (Bug 7) ────────────────
        # If a builder.* run already succeeded since the most recent user
        # message, refuse a 2nd generate_code in the same turn. The model
        # otherwise tends to fall into "let me try generate_code again" for
        # bug-fix turns where reviewer returned clean (because runtime bugs
        # like unconnected Qt signals or invalid font strings compile fine).
        # Re-running generate_code is ~60–90s of OpenHands work that almost
        # always produces a worse result than 2 surgical write_file edits.
        if conv_id and name == "generate_code":
            try:
                _conv_full = await db.get_conversation(conv_id)
                if await _check_v2(cr=_conv_full):
                    _latest_user_ts = await _latest_user_msg_ts(conv_id, conv_row=_conv_full)

                    if _latest_user_ts is not None:
                        _runs_rb = await db.get_runs_by_conversation(conv_id, limit=20)
                        _builder_succ_this_turn = 0
                        _last_builder_role = ""
                        for _r in _runs_rb:
                            if not (_r.get("role", "").startswith("builder")
                                    and _r.get("status") == "succeeded"):
                                continue
                            _r_ts = _parse_ts_loose(_r.get("started_at"))
                            if _r_ts and _r_ts >= _latest_user_ts:
                                _builder_succ_this_turn += 1
                                if not _last_builder_role:
                                    _last_builder_role = _r.get("role", "builder")

                        if _builder_succ_this_turn >= 1:
                            # Surface a concrete project_dir for the
                            # write_file / run_review next-step hint.
                            _pd_rb = ""
                            for _r in _runs_rb:
                                _envrb = _r.get("result_envelope") or {}
                                _pdrb = (_envrb.get("project_dir") or "").strip()
                                if _pdrb:
                                    _pd_rb = _pdrb
                                    break
                            await events.emit(conv_id, "tool_end", {
                                "tool": "generate_code", "icon": "package",
                                "status": "⛔ generate_code already ran this turn — use write_file",
                            })
                            print(f"[v2-gate] ANTI-REBUILD: blocking generate_code "
                                  f"({_builder_succ_this_turn} {_last_builder_role} run(s) "
                                  f"since user msg)", flush=True)
                            return (
                                f"BLOCKED — generate_code already ran "
                                f"{_builder_succ_this_turn} time(s) this turn "
                                f"(latest: {_last_builder_role}). The OpenHands "
                                f"feature-builder is for substantial NEW functionality "
                                f"(3+ new files, major refactor) — not for fixing 1–3 "
                                f"line bugs in existing files. Re-running it almost "
                                f"always produces a worse result than targeted edits.\n\n"
                                f"For the current user request, do this instead:\n"
                                f"  1. read_file(path='{_pd_rb or '/root/projects/.../main.py'}')\n"
                                f"  2. write_file(path='...', content='...')  with the fix\n"
                                f"  3. run_review(project_dir='{_pd_rb or '/root/projects/...'}')  to verify\n\n"
                                f"If the user actually wants a major new feature you haven't "
                                f"built yet, respond with plain text asking them to confirm — "
                                f"don't silently rebuild what's already there."
                            )
            except Exception as _rbe:
                print(f"[v2-gate] anti-rebuild check failed (non-fatal): {_rbe}")

        if conv_id and name not in ("run_review", "run_acceptance_review", "run_fixer",
                                    "run_aider_fix", "ask_project", "get_coder_workflow",
                                    "cancel_coder_workflow"):
            try:
                _runs_for_v2_gate = await db.get_runs_by_conversation(conv_id, limit=20)
                _pending_run = None    # state 1 trigger
                _pending_kind = ""
                _pending_review = None  # state 2 trigger
                _pending_acceptance_needed = None  # clean review must be accepted before delivery

                # Walk newest-first.
                # Builder/fixer runs in ANY non-trivial state require verification.
                # `partial` and `stuck` mean the build wrote some files but didn't
                # finish — the model needs run_review to find out what's there.
                # `failed` means the build crashed; run_review will fail fast and
                # tell the model so. Same logic for fixer. Only "queued"/"running"
                # (in-flight) and unknown statuses are skipped.
                _BUILDER_GATING = {"succeeded", "partial", "stuck", "failed"}
                _FIXER_GATING = {"succeeded", "failed", "partial"}
                for _r in _runs_for_v2_gate:
                    _role = _r.get("role", "")
                    if _role == "qa":
                        # Q&A-terminal handling lives in the pre-gate at the
                        # top of exec_tool; here a qa run just ends the walk.
                        break
                    if _role == "reviewer":
                        # Most recent reviewer: issues need fixer; clean needs acceptance.
                        _env_r = _r.get("result_envelope") or {}
                        _rstatus = (_env_r.get("status") or "").lower()
                        if _rstatus in ("issues", "error"):
                            _pending_review = _r
                        elif _rstatus == "clean":
                            _pending_acceptance_needed = _r
                        break
                    if _role == "acceptance":
                        # Acceptance issues route through the same Fixer, but
                        # accepted releases the delivery gate.
                        _env_a = _r.get("result_envelope") or {}
                        _astatus = (_env_a.get("status") or "").lower()
                        if _astatus in ("issues", "error"):
                            _pending_review = _r
                        break
                    if _role.startswith("builder") and _r.get("status") in _BUILDER_GATING:
                        _pending_run = _r
                        _pending_kind = "builder"
                        break
                    if _role in {"fixer", "aider.fix"} and _r.get("status") in _FIXER_GATING:
                        _env_f = _r.get("result_envelope") or {}
                        if (_env_f.get("source_role") == "acceptance"
                                and _env_f.get("docs_only")):
                            _pending_acceptance_needed = _r
                        else:
                            _pending_run = _r
                            _pending_kind = "fixer"
                        break

                # If neither state triggers, fall through and run normally.
                _gate_msg = None
                if _pending_run is not None:
                    _env = _pending_run.get("result_envelope") or {}
                    _pd = (_env.get("project_dir") or "").strip()
                    _pid = _pending_run.get("id", "?")
                    _why = ("generate_code" if _pending_kind == "builder" else "run_fixer")
                    if (name in {"download_project", "download_file"}
                            and await _latest_user_requested_ship_anyway(conv_id)):
                        print(f"[v2-gate] ship-anyway: allowing {name} before "
                              f"review because latest user requested delivery", flush=True)
                    else:
                        _gate_msg = (
                            "state", "review-needed",
                            f"BLOCKED — {_why} ({_pid}) just completed but "
                            f"run_review has not been called yet.\n\n"
                            f"Your VERY NEXT tool call MUST be:\n"
                            f"  run_review(project_dir='{_pd}')\n\n"
                            f"Do not call {name}, read_file, write_file, run_shell, javac, "
                            f"or any other tool first. The Reviewer runs the project's real "
                            f"build / tests / lint and tells you exactly what (if anything) "
                            f"still needs fixing — with file:line references and a fix scope.",
                            f"⛔ Blocked — call run_review first (after {_why} {_pid[:14]}…)",
                            _pid,
                        )
                elif _pending_acceptance_needed is not None:
                    _env = _pending_acceptance_needed.get("result_envelope") or {}
                    _pd = (_env.get("project_dir") or "").strip()
                    _rid = _pending_acceptance_needed.get("id", "?")
                    _source_role = _pending_acceptance_needed.get("role", "")
                    _reviewer_id = (_env.get("reviewer_run_id") or _rid)
                    if (name in {"download_project", "download_file"}
                            and await _latest_user_requested_ship_anyway(conv_id)):
                        print(f"[v2-gate] ship-anyway: allowing {name} before "
                              f"acceptance because latest user requested delivery", flush=True)
                    else:
                        # A docs-only fixer trigger's envelope reviewer_run_id
                        # points at the ACCEPTANCE run it fixed from — suggesting
                        # it teaches the model a wrong id. Only name the id when
                        # the trigger is the clean reviewer itself; the dispatcher
                        # auto-resolves the latest clean reviewer otherwise.
                        _call_hint = (
                            f"  run_acceptance_review(project_dir='{_pd}', reviewer_run_id='{_rid}')"
                            if _source_role == "reviewer"
                            else f"  run_acceptance_review(project_dir='{_pd}')"
                        )
                        _gate_msg = (
                            "state", "acceptance-needed",
                            f"BLOCKED — run_review is clean, but final acceptance has not "
                            f"passed yet.\n\n"
                            f"Your VERY NEXT tool call MUST be:\n"
                            f"{_call_hint}\n\n"
                            f"Acceptance verifies the generated project satisfies the user's "
                            f"request, has accurate docs, sane tests, and clean packaging. "
                            f"Do not call {name} or download_project until acceptance returns accepted "
                            f"unless the latest user message explicitly asks to ship/download anyway.",
                            f"⛔ Blocked — call run_acceptance_review first ({_source_role} {_rid[:14]}…)",
                            _rid,
                        )
                elif _pending_review is not None:
                    _rid = _pending_review.get("id", "?")
                    _pending_role = _pending_review.get("role", "reviewer")
                    _pending_env = _pending_review.get("result_envelope") or {}
                    _rstatus_disp = _pending_env.get("status", "?")
                    # Cap-aware release: once the fixer cycle budget is
                    # exhausted, the same review issues will keep blocking
                    # the conversation forever. Allow the model to deliver
                    # what was built (download_project / download_file) and
                    # inspect the tree (read_file / list_files) so the user
                    # gets the partial result instead of a dead session.
                    # Count total fixer attempts (succeeded + failed/no_op)
                    # for the release — a fixer that declined still represents
                    # an exhausted attempt. The cycle-cap check (which gates
                    # run_fixer itself) still counts only successes.
                    _FIXER_TERMINAL = {"succeeded", "failed", "partial", "no_op"}
                    _run_role_by_id = {r.get("id"): r.get("role") for r in _runs_for_v2_gate}
                    def _fixer_source_role(_fr):
                        _fenv = _fr.get("result_envelope") or {}
                        return (_fenv.get("source_role")
                                or _run_role_by_id.get(_fr.get("parent_run_id"))
                                or "reviewer")
                    # Same turn-scoped window as the cycle cap, so cap and
                    # cap-release agree on how many attempts happened.
                    _uts_gate = await _latest_user_msg_ts(conv_id)
                    _runs_gate_window = _runs_since(_runs_for_v2_gate, _uts_gate)
                    _fixer_attempts_gate = sum(
                        1 for _r in _runs_gate_window
                        if (_r.get("role") in {"fixer", "aider.fix"} and _r.get("status") in _FIXER_TERMINAL
                            and _fixer_source_role(_r) == _pending_role)
                    )
                    _fixer_succ_gate = sum(
                        1 for _r in _runs_gate_window
                        if (_r.get("role") in {"fixer", "aider.fix"} and _r.get("status") == "succeeded"
                            and _fixer_source_role(_r) == _pending_role)
                    )
                    _DELIVERY_OK_AFTER_CAP = {
                        "download_project", "download_file",
                        "read_file", "list_files",
                    }
                    _DELIVERY_SHIP_TOOLS = {"download_project", "download_file"}
                    _ship_anyway_gate = (
                        name in _DELIVERY_SHIP_TOOLS
                        and await _latest_user_requested_ship_anyway(conv_id)
                    )
                    _aider_ctx_gate = None
                    if _pending_role == "reviewer" and getattr(config, "AIDER_ENABLED", True):
                        _aider_ctx_gate = await _uploaded_project_aider_context(
                            conv_id, issue_run=_pending_review,
                        )
                    # Deadlock break: the FINAL_CYCLE gate (2 successful fixers,
                    # no research since the last reviewer) blocks run_fixer with
                    # the message "call Agent Research first". The STUCK_FIX gate
                    # does the same when issues recur after a fix cycle. But this
                    # post-review gate then blocks deep_research itself ("call
                    # run_fixer first") — circular. Whitelist deep_research once
                    # at least one fixer cycle has run, so the model can satisfy
                    # the research-first requirement and then retry run_fixer on
                    # the next round. Below 1 fixer attempt the model should
                    # actually try fixing before researching.
                    _cap_limit_gate = 2 if _pending_role == "acceptance" else 3
                    # Anchor on started_at to match the STUCK/FINAL gates —
                    # research stashed while the review was still running must
                    # satisfy both, or the gates disagree and loop.
                    if (name == "deep_research"
                            and _pending_role != "acceptance"
                            and _fixer_attempts_gate >= 1
                            and not await _deep_research_called_since(
                                conv_id, _pending_review.get("started_at"))):
                        print(f"[v2-gate] research-release: allowing deep_research "
                              f"despite fix-needed (fixer_attempts={_fixer_attempts_gate}, "
                              f"required by FINAL_CYCLE/STUCK_FIX)", flush=True)
                        # Skip _gate_msg → deep_research runs.
                    elif _ship_anyway_gate:
                        print(f"[v2-gate] ship-anyway: allowing {name} despite "
                              f"fix-needed because latest user requested delivery", flush=True)
                        # Skip _gate_msg entirely → delivery tool runs normally.
                    elif _fixer_attempts_gate >= _cap_limit_gate and name in _DELIVERY_OK_AFTER_CAP:
                        print(f"[v2-gate] cap-release: allowing {name} despite "
                              f"fix-needed (attempts={_fixer_attempts_gate}, "
                              f"succ={_fixer_succ_gate})", flush=True)
                        # Skip _gate_msg entirely → tool runs normally.
                    elif _aider_ctx_gate:
                        _project_dir = _aider_ctx_gate.get("project_dir") or _pending_env.get("project_dir") or ""
                        _gate_msg = (
                            "state", "fix-needed",
                            f"BLOCKED — {_pending_role} ({_rid}) returned status='{_rstatus_disp}' "
                            f"for an uploaded project.\n\n"
                            f"Your VERY NEXT tool call MUST be:\n"
                            f"  run_aider_fix(issue_run_id='{_rid}', project_dir='{_project_dir}', "
                            f"task='<latest user request + reviewer summary>')\n\n"
                            f"Uploaded-project fixes are handled by Aider from the project root. "
                            f"Do NOT call run_fixer, read_file, write_file, generate_code, or "
                            f"run_shell for this reviewer issue. After Aider returns, call "
                            f"run_review to verify build/tests.",
                            f"⛔ Blocked — call run_aider_fix first ({_pending_role} {_rid[:14]}… has issues)",
                            _rid,
                        )
                    else:
                        _tool_name = "run_acceptance_review" if _pending_role == "acceptance" else "run_review"
                        _issue_label = "run_acceptance_review" if _pending_role == "acceptance" else "run_review"
                        _gate_msg = (
                            "state", "fix-needed",
                            f"BLOCKED — {_issue_label} ({_rid}) returned status='{_rstatus_disp}' "
                            f"with issues that have not been addressed.\n\n"
                            f"Your VERY NEXT tool call MUST be:\n"
                            f"  run_fixer(reviewer_run_id='{_rid}')\n\n"
                            f"The Fixer reads each issue's fix-scope files, generates targeted "
                            f"edits via the coder model, and writes them back. Do NOT manually "
                            f"call read_file / write_file for these issues — that's the v1 "
                            f"antipattern that burns rounds. After run_fixer completes, call "
                            f"{_tool_name if _pending_role == 'acceptance' else 'run_review'} "
                            f"as instructed by the fixer result.",
                            f"⛔ Blocked — call run_fixer first ({_pending_role} {_rid[:14]}… has issues)",
                            _rid,
                        )
                if _gate_msg is not None:
                    # v1 has no run_review or run_fixer in its workflow, so
                    # the gate only blocks v2 personas.
                    if await _check_v2():
                        _, _state_label, _body, _short_status, _trigger_id = _gate_msg
                        await events.emit(conv_id, "tool_end", {
                            "tool": name, "icon": "code",
                            "status": _short_status,
                        })
                        print(f"[v2-gate] state={_state_label} blocked tool={name} "
                              f"trigger={_trigger_id}", flush=True)
                        return _body
            except Exception as _gge:
                # Gate is best-effort — don't crash legitimate work if the
                # runs/persona lookup fails. Log and proceed.
                print(f"[v2-gate] gate check failed (non-fatal): {_gge}")

        if name == "execute_code":
            code = args.get("code", "")
            language = args.get("language", "python")
            await events.emit(conv_id, "tool_start", {
                "tool": "execute_code", "icon": "code",
                "status": f"Running {language} code...",
            })
            start_time = time.time()

            # All execution goes through /command for consistent CWD at /root/
            b64_code = base64.b64encode(code.encode()).decode()
            lang_lower = language.lower()
            if lang_lower in ("python", "python3", "py"):
                await _ensure_venv(http)
                exec_cmd = (
                    f"cd /root && printf '%s' {shlex.quote(b64_code)} | base64 -d > /tmp/_hc_exec.py && "
                    f"/root/venv/bin/python3 /tmp/_hc_exec.py"
                )
            elif lang_lower in ("bash", "sh", "zsh"):
                exec_cmd = f"cd /root && printf '%s' {shlex.quote(b64_code)} | base64 -d | bash"
            elif lang_lower in ("javascript", "js", "node"):
                exec_cmd = f"cd /root && printf '%s' {shlex.quote(b64_code)} | base64 -d > /tmp/_hc_exec.js && node /tmp/_hc_exec.js"
            else:
                # Fallback: use /execute endpoint for compiled languages
                exec_task = asyncio.create_task(http.post(
                    f"{config.CODEBOX_URL}/execute",
                    json={"code": code, "language": language, "timeout": config.EXECUTION_TIMEOUT},
                    timeout=config.EXECUTION_TIMEOUT + 15,
                ))
                exec_cmd = None

            if exec_cmd:
                exec_task = asyncio.create_task(http.post(
                    f"{config.CODEBOX_URL}/command",
                    json={"command": exec_cmd, "timeout": config.EXECUTION_TIMEOUT},
                    timeout=config.EXECUTION_TIMEOUT + 15,
                ))
            while not exec_task.done():
                await asyncio.sleep(3)
                if not exec_task.done():
                    elapsed = int(time.time() - start_time)
                    await events.emit(conv_id, "tool_start", {
                        "tool": "execute_code", "icon": "code",
                        "status": f"Running {language}... {elapsed}s elapsed",
                    })
            try:
                r = exec_task.result()
                result = r.json()
            except Exception as ce:
                await events.emit(conv_id, "tool_end", {
                    "tool": "execute_code", "icon": "code",
                    "status": f"CodeBox unreachable: {str(ce)[:80]}",
                })
                return f"ERROR: CodeBox connection failed: {ce}\nMake sure CodeBox is running at {config.CODEBOX_URL}"
            success = result.get("exit_code", -1) == 0 or result.get("success", False)
            stdout = _strip_ansi(result.get("stdout", "")).strip()
            stderr = _strip_ansi(result.get("stderr", "")).strip()
            exec_time = result.get("execution_time", 0)
            exit_code = result.get("exit_code", -1)

            status_text = f"{'OK' if success else 'FAILED'} ({exec_time:.1f}s)"
            await events.emit(conv_id, "tool_end", {
                "tool": "execute_code", "icon": "code",
                "status": status_text,
                "detail": json.dumps({
                    "code": code[:2000], "language": language,
                    "stdout": stdout[:3000], "stderr": stderr[:2000],
                    "success": success,
                }),
            })

            if stdout or stderr:
                await events.emit(conv_id, "code_output", {
                    "language": language, "stdout": stdout[:3000],
                    "stderr": stderr[:1500] if not success else "",
                    "success": success, "exec_time": exec_time,
                })

            parts = [f"**{'SUCCESS' if success else 'FAILED'}** | {language} | exit {exit_code} | {exec_time:.1f}s"]
            if result.get("compile_output"):
                parts.append(f"\nCompiler:\n```\n{result['compile_output'][:2000]}\n```")
            if stdout:
                parts.append(f"\nstdout:\n```\n{stdout[:5000]}\n```")
            if stderr and not success:
                parts.append(f"\nstderr:\n```\n{stderr[:3000]}\n```")

            # Action hints — give specific guidance for common errors
            if not success:
                combined_err = (stderr + stdout).lower()
                if "eoferror" in combined_err or "eof when reading" in combined_err:
                    parts.append("\n---\n⚠️ input() does NOT work in this sandbox (no stdin). Remove all input() calls. Use hardcoded test values, function parameters, or sys.argv with write_file + run_shell.")
                elif "indexerror" in combined_err and "argv" in combined_err:
                    parts.append("\n---\n⚠️ sys.argv has no arguments in execute_code. To test scripts with arguments: 1) write_file to save the script, 2) run_shell to execute it with args (e.g., python3 /root/script.py arg1 arg2).")
                elif ("no such file" in combined_err or "not found" in combined_err) and "command" not in combined_err:
                    parts.append("\n---\n⚠️ File not found. Working directory is /root/. Use absolute paths (/root/filename) or save files with write_file first.")
                elif "modulenotfounderror" in combined_err or "no module named" in combined_err:
                    parts.append("\n---\n⚠️ Missing package. Install it first: run_shell(command='pip3 install <package>'), then retry.")
                else:
                    parts.append("\n---\nEXECUTION FAILED. Read the error above. Fix the root cause (do NOT retry the same code).")
            elif not stdout.strip():
                parts.append("\n---\nCode ran successfully with no output. Add print() statements if you need to verify results.")

            return "\n".join(parts)

        elif name == "research":
            query = args.get("query", "")
            await events.emit(conv_id, "tool_start", {"tool": "research", "icon": "search", "status": f'Searching: "{query[:50]}"'})
            import urllib.parse
            params = urllib.parse.urlencode({"q": query, "format": "json", "count": config.SEARCH_RESULTS_COUNT})
            r = await http.get(f"{config.SEARXNG_URL}/search?{params}", timeout=15)
            if r.status_code == 429:
                await asyncio.sleep(3.0)
                r = await http.get(f"{config.SEARXNG_URL}/search?{params}", timeout=15)
            if r.status_code >= 400:
                await events.emit(conv_id, "tool_end", {"tool": "research", "icon": "search", "status": f"⚠️ Search returned HTTP {r.status_code} — may be rate limited"})
                return f"**Web Search: {query}**\n\n⚠️ Search engine returned HTTP {r.status_code}. Upstream engines may be rate-limiting requests. Try again in a minute."
            data = r.json()
            results = data.get("results", [])[:config.SEARCH_RESULTS_COUNT]
            sr_cards = []
            for item in results:
                url = item.get("url", "")
                url_lower = url.lower()
                thumbnail = item.get("thumbnail") or item.get("img_src") or ""
                r_type = "web"
                if "youtube.com/watch" in url_lower or "youtu.be/" in url_lower:
                    r_type = "youtube"
                    vid_id = None
                    if "youtube.com/watch" in url_lower:
                        qs = url.split("?", 1)[1] if "?" in url else ""
                        for part in qs.split("&"):
                            if part.startswith("v="):
                                vid_id = part[2:].split("&")[0]; break
                    elif "youtu.be/" in url_lower:
                        vid_id = url.split("youtu.be/")[1].split("?")[0].split("/")[0]
                    if vid_id:
                        thumbnail = f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg"
                elif thumbnail or any(url_lower.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                    r_type = "image"
                sr_cards.append({"title": item.get("title", ""), "url": url,
                                 "snippet": item.get("content", "")[:200],
                                 "thumbnail": thumbnail, "type": r_type})
            if sr_cards:
                await events.emit(conv_id, "search_results", {"query": query, "results": sr_cards})

            # ── Fetch top 5 pages in parallel, prioritized by source tier ──
            fetch_urls = []
            for item in results:
                u = item.get("url", "")
                if u:
                    fetch_urls.append(u)
            fetch_urls.sort(key=_source_tier)
            fetch_urls = fetch_urls[:5]

            pages = []
            if fetch_urls:
                await events.emit(conv_id, "tool_status", {"tool": "research", "icon": "search", "status": f"Reading {len(fetch_urls)} pages..."})
                fetch_tasks = [_fetch_page(http, u) for u in fetch_urls]
                fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
                for u, fr in zip(fetch_urls, fetch_results):
                    if isinstance(fr, dict) and fr.get("content"):
                        pages.append(fr)

            await events.emit(conv_id, "tool_end", {"tool": "research", "icon": "search", "status": f'{len(results)} results, {len(pages)} pages read',
                "detail": json.dumps({"query": query, "results": [{"title": r.get("title",""), "url": r.get("url","")} for r in results[:5]]}),
            })

            # Build result: search listing + actual page content
            parts = [f"**Web Search: {query}**\n"]
            parts.append("## Search Results\n")
            for i, res in enumerate(results, 1):
                parts.append(f"{i}. **[{res.get('title', '')}]({res.get('url', '')})**\n   {res.get('content', '')}\n")

            if pages:
                parts.append("\n## Page Content (read from top results)\n")
                for pg in pages:
                    # Limit each page to 4000 chars to stay within context budget
                    content = pg["content"][:4000]
                    parts.append(f"### Source: {pg['url']}\n{content}\n\n---\n")
            else:
                parts.append("\n*(Could not fetch any page content — use the snippets above.)*\n")

            return "\n".join(parts)

        elif name == "fetch_url":
            url = args.get("url", "").strip()
            # Auto-prepend https:// if no protocol present
            if url and not url.startswith(("http://", "https://")):
                url = "https://" + url
            # Encode spaces in URL path (common input issue)
            url = url.replace(" ", "%20")
            await events.emit(conv_id, "tool_start", {"tool": "fetch_url", "icon": "globe", "status": f"Fetching: {url[:55]}"})
            try:
                status, headers, final_url, content = await fetch_bytes_safely(
                    http,
                    url,
                    timeout=15,
                    max_bytes=min(max(config.MAX_FETCH_CHARS * 8, 65536), 1024 * 1024),
                )
            except ValueError as e:
                await events.emit(conv_id, "tool_error", {"tool": "fetch_url", "icon": "globe", "status": str(e)})
                return f"ERROR: {e}"
            if status >= 400:
                await events.emit(conv_id, "tool_end", {"tool": "fetch_url", "icon": "globe", "status": f"HTTP {status}: {url[:40]}"})
                return f"ERROR: HTTP {status} fetching {url}"
            ct = headers.get("content-type", "") or ""
            m = re.search(r"charset=([^;\s]+)", ct, re.I)
            enc = (m.group(1) if m else "utf-8").strip("\"'")
            try:
                text = content.decode(enc, errors="replace")[:config.MAX_FETCH_CHARS]
            except LookupError:
                text = content.decode("utf-8", errors="replace")[:config.MAX_FETCH_CHARS]
            text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            await events.emit(conv_id, "tool_end", {"tool": "fetch_url", "icon": "globe", "status": f"Read {len(text)} chars"})
            return f"**Content from {final_url}:**\n\n{text[:config.MAX_FETCH_CHARS]}"

        elif name == "run_shell" or name == "install_package":
            command = args.get("command", args.get("package", ""))
            shell_timeout = config.EXECUTION_TIMEOUT
            if name == "install_package":
                pkg = command
                command = f"pip3 install {pkg} 2>&1; echo \"EXIT:$?\""
                shell_timeout = max(shell_timeout, 120)
            # Route pip/python commands through the venv
            cmd_stripped = command.strip()
            if any(cmd_stripped.startswith(p) for p in ("pip ", "pip3 ", "python ", "python3 ")):
                venv_ok = await _ensure_venv(http)
                if venv_ok:
                    command = f"export PATH=/root/venv/bin:$PATH && {command}"
            await events.emit(conv_id, "tool_start", {"tool": name, "icon": "terminal", "status": f"$ {command[:70]}"})
            r = await http.post(
                f"{config.CODEBOX_URL}/command",
                json={"command": command, "timeout": shell_timeout},
                timeout=shell_timeout + 10,
            )
            result = r.json()
            stdout = _strip_ansi(result.get("stdout", "")).strip()
            stderr = _strip_ansi(result.get("stderr", "")).strip()
            exit_code = result.get("exit_code", result.get("returncode", 0))
            success = exit_code == 0
            status_icon = "OK" if success else "FAILED"
            await events.emit(conv_id, "tool_end", {
                "tool": name, "icon": "terminal",
                "status": f"{status_icon} exit {exit_code}: {command[:50]}",
                "detail": json.dumps({"command": command, "stdout": stdout[:2000], "stderr": stderr[:1000], "exit_code": exit_code}),
            })
            out = f"```\n{stdout}\n```" if stdout else ""
            err = f"\nstderr:\n```\n{stderr}\n```" if stderr and not success else ""
            result_text = f"exit code: {exit_code}\n{out}{err}" if (stdout or stderr) else f"(exit code: {exit_code}, no output)"
            if not success:
                result_text += "\n---\nCommand failed. Check the error above and try a different approach or fix the command."

            # Detect dev server commands that time out — warn the model not to retry
            _dev_server_cmds = ("npm start", "npm run dev", "npm run serve", "npx vite", "yarn dev", "yarn start", "python3 -m http.server", "python -m http.server", "flask run", "uvicorn")
            if any(ds in cmd_stripped for ds in _dev_server_cmds) and (not stdout.strip() or len(stdout.strip()) < 50):
                result_text += (
                    "\n---\n⚠️ This looks like a dev server command. Dev servers run forever and WILL time out in this sandbox. "
                    "Do NOT retry this command. The project files are already built and ready — "
                    "use download_project to deliver them to the user instead."
                )
            return result_text

        elif name == "write_file":
            path = args.get("path", "")
            content = args.get("content", "")
            await events.emit(conv_id, "tool_start", {"tool": "write_file", "icon": "code", "status": f"Writing: {path}"})
            b64 = base64.b64encode(content.encode()).decode()
            quoted_path = shlex.quote(path)
            cmd = f"mkdir -p $(dirname {quoted_path}) && printf '%s' {shlex.quote(b64)} | base64 -d > {quoted_path} && echo OK"
            r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 30}, timeout=40)
            result = r.json()
            ok = "OK" in result.get("stdout", "") or result.get("exit_code", 1) == 0
            status = f"Written: {path}" if ok else f"Write failed: {path}"
            await events.emit(conv_id, "tool_end", {"tool": "write_file", "icon": "code", "status": status})
            return f"File written: {path} ({len(content)} bytes)" if ok else f"ERROR: Failed to write {path}: {result.get('stderr', '')[:200]}"

        elif name == "read_file":
            path = args.get("path", "/root")
            await events.emit(conv_id, "tool_start", {"tool": "read_file", "icon": "code", "status": f"Reading: {path}"})
            r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": f"cat {shlex.quote(path)} 2>&1", "timeout": 10}, timeout=15)
            result = r.json()
            content_out = result.get("stdout", "")
            await events.emit(conv_id, "tool_end", {"tool": "read_file", "icon": "code", "status": f"Read {len(content_out)} chars: {path}"})
            return f"**{path}** ({len(content_out)} chars):\n```\n{content_out[:10000]}\n```"

        elif name == "list_files":
            path = args.get("path", "/root")
            await events.emit(conv_id, "tool_start", {"tool": "list_files", "icon": "terminal", "status": f"ls {path}"})
            r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": f"ls -lahF {shlex.quote(path)} 2>&1", "timeout": 10}, timeout=15)
            result = r.json()
            await events.emit(conv_id, "tool_end", {"tool": "list_files", "icon": "terminal", "status": f"Listed: {path}"})
            return f"```\n{result.get('stdout', '(empty)')}\n```"

        elif name == "download_file":
            path = args.get("path", "")
            await events.emit(conv_id, "tool_start", {"tool": "download_file", "icon": "code", "status": f"Preparing: {path}"})
            qpath = shlex.quote(path)
            r = await http.post(f"{config.CODEBOX_URL}/command", json={
                "command": f"base64 -w0 {qpath} 2>/dev/null && echo '|||SEPARATOR|||' && basename {qpath}",
                "timeout": 30
            }, timeout=40)
            result = r.json()
            stdout = result.get("stdout", "")
            if "|||SEPARATOR|||" in stdout:
                parts = stdout.split("|||SEPARATOR|||")
                b64_data = parts[0].strip()
                filename = parts[1].strip() if len(parts) > 1 else path.split("/")[-1]
                estimated_size = len(b64_data) * 3 // 4
                if estimated_size > config.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
                    await events.emit(conv_id, "tool_end", {"tool": "download_file", "icon": "code",
                        "status": f"File too large ({estimated_size // (1024*1024)}MB > {config.MAX_UPLOAD_SIZE_MB}MB limit)"})
                    return f"ERROR: File too large to download (exceeds {config.MAX_UPLOAD_SIZE_MB}MB limit)"
                os.makedirs(config.SANDBOX_OUTPUTS_DIR, exist_ok=True)
                filepath = os.path.join(config.SANDBOX_OUTPUTS_DIR, filename)
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(b64_data))
                file_meta = _artifact_file_metadata(filepath)
                download_url = f"/api/downloads/{filename}"
                await events.emit(conv_id, "tool_end", {"tool": "download_file", "icon": "code",
                    "status": f"{filename} ready",
                    "detail": json.dumps({"file": filename, "path": path, "download_url": download_url}),
                })
                _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
                _ext = os.path.splitext(filename)[1].lower()
                artifact = None
                try:
                    cf_id = f"cf-{uuid.uuid4().hex[:8]}"
                    _mime = db.artifact_mime_type_for_filename(filename)
                    _kind = db.artifact_kind_for_filename(filename, _mime)
                    artifact = await db.add_conversation_file(
                        cf_id,
                        conv_id,
                        filename,
                        download_url,
                        message_id=artifact_message_id,
                        kind=_kind,
                        mime_type=_mime,
                        storage_path=filepath,
                        size_bytes=file_meta["size_bytes"],
                        sha256=file_meta["sha256"],
                        exists_status="present",
                        status="draft",
                        content_text=_artifact_index_text(filepath, filename, _kind, _mime),
                        metadata={
                            "source_tool": "download_file",
                            "source_path": path,
                            "size_bytes": file_meta["size_bytes"],
                            "sha256": file_meta["sha256"],
                        },
                    )
                except Exception as e:
                    print(f"[FileTrack] {e}")
                await events.emit(conv_id, "file_ready", {
                    "filename": filename, "url": download_url,
                    "is_image": _ext in _IMAGE_EXTS,
                    "artifact_id": (artifact or {}).get("id"),
                    "kind": (artifact or {}).get("kind") or db.artifact_kind_for_filename(filename),
                    "mime_type": (artifact or {}).get("mime_type") or db.artifact_mime_type_for_filename(filename),
                })
                if _ext in _IMAGE_EXTS:
                    return f"![{filename}]({download_url})\n\n**[Download {filename}]({download_url})**"
                return f"**[Download {filename}]({download_url})**"
            else:
                await events.emit(conv_id, "tool_end", {"tool": "download_file", "icon": "code", "status": f"File not found: {path}"})
                return f"ERROR: File not found or could not read: {path}"

        elif name == "download_project":
            directory = args.get("directory", "/root")
            _download_warning_lines = []
            _ship_anyway_requested = (
                await _latest_user_requested_ship_anyway(conv_id) if conv_id else False
            )

            # Reviewer gate (Coder Bot v2): if the most recent reviewer run on
            # this conversation reported issues or could not identify the
            # project, refuse to ship until the orchestrator runs a fixer pass
            # and re-runs run_review. v1 personas don't produce reviewer runs,
            # so this is naturally inert for them.
            if conv_id:
                try:
                    _runs_for_gate = await db.get_runs_by_conversation(conv_id, limit=20)
                    _FIXER_TERMINAL_FOR_DELIVERY = {"succeeded", "failed", "partial", "no_op"}
                    _run_role_by_id_for_delivery = {
                        r.get("id"): r.get("role") for r in _runs_for_gate
                    }

                    def _fixer_source_role_for_delivery(_fr):
                        _fenv = _fr.get("result_envelope") or {}
                        return (_fenv.get("source_role")
                                or _run_role_by_id_for_delivery.get(_fr.get("parent_run_id"))
                                or "reviewer")

                    def _fixer_attempts_for_delivery(_role: str) -> int:
                        return sum(
                            1 for _r in _runs_for_gate
                            if (_r.get("role") in {"fixer", "aider.fix"}
                                and _r.get("status") in _FIXER_TERMINAL_FOR_DELIVERY
                                and _fixer_source_role_for_delivery(_r) == _role)
                        )

                    _latest_reviewer = None
                    _latest_acceptance = None
                    for _r in _runs_for_gate:
                        if _r.get("role") == "acceptance":
                            _latest_acceptance = _r
                            break
                        if _r.get("role") == "reviewer":
                            _latest_reviewer = _r
                            break
                    if _latest_acceptance:
                        _env = _latest_acceptance.get("result_envelope") or {}
                        _astatus = (_env.get("status") or "").lower()
                        if _astatus != "accepted":
                            issues = _env.get("issues") or []
                            n = len(issues)
                            _acceptance_cap_exhausted = (
                                _fixer_attempts_for_delivery("acceptance") >= 2
                            )
                            if _ship_anyway_requested or _acceptance_cap_exhausted:
                                _download_warning_lines = [
                                    "WARNING: Shipped despite unresolved acceptance issues.",
                                    f"Acceptance status: {_astatus or '?'}; issue count: {n}.",
                                ]
                                print(f"[CHAT] download_project allowing ship-anyway "
                                      f"despite acceptance={_latest_acceptance.get('id')} "
                                      f"status={_astatus} issues={n} "
                                      f"requested={_ship_anyway_requested} "
                                      f"cap={_acceptance_cap_exhausted}")
                            else:
                                lines = [
                                    f"BLOCKED — last run_acceptance_review returned status='{_astatus}'.",
                                    "Do not call download_project until acceptance is accepted unless the latest user message explicitly asks to ship/download anyway.",
                                    "",
                                    f"Acceptance flagged {n} issue{'s' if n != 1 else ''}:",
                                ]
                                for i, iss in enumerate(issues[:5], 1):
                                    lines.append(
                                        f"  {i}. [{iss.get('category','?')}] {iss.get('file','?')}"
                                        + (f":{','.join(str(x) for x in iss.get('lines') or [])}" if iss.get('lines') else "")
                                        + f" — {iss.get('summary','')}"
                                    )
                                    scope = iss.get("suggested_fix_scope") or []
                                    if scope:
                                        lines.append(f"     fix scope: {', '.join(scope[:5])}")
                                lines.append("")
                                lines.append(
                                    f"REQUIRED NEXT STEP: run_fixer(reviewer_run_id='{_latest_acceptance.get('id')}'), "
                                    "then re-run review/acceptance as instructed by the fixer. "
                                    "If you cannot get it accepted, ask the user whether they want the current files as-is."
                                )
                                await events.emit(conv_id, "tool_end", {
                                    "tool": "download_project", "icon": "code",
                                    "status": f"⛔ Blocked — acceptance has {n} issue{'s' if n != 1 else ''}",
                                })
                                return "\n".join(lines)
                    if _latest_reviewer:
                        _env = _latest_reviewer.get("result_envelope") or {}
                        _rstatus = (_env.get("status") or "").lower()
                        if _rstatus in ("issues", "error"):
                            issues = _env.get("issues") or []
                            n = len(issues)
                            _reviewer_cap_exhausted = (
                                _fixer_attempts_for_delivery("reviewer") >= 3
                            )
                            if _ship_anyway_requested or _reviewer_cap_exhausted:
                                _download_warning_lines = [
                                    "WARNING: Shipped despite unresolved review/test issues.",
                                    f"Review status: {_rstatus}; issue count: {n}.",
                                    f"Build exit={_env.get('build_exit','?')}; test exit={_env.get('test_exit','?')}; lint exit={_env.get('lint_exit','?')}.",
                                ]
                                if issues:
                                    _download_warning_lines.append(
                                        "Latest issue: "
                                        + (issues[0].get("file") or "?")
                                        + " - "
                                        + (issues[0].get("summary") or "")[:220]
                                    )
                                print(f"[CHAT] download_project allowing ship-anyway "
                                      f"despite reviewer={_latest_reviewer.get('id')} "
                                      f"status={_rstatus} issues={n} "
                                      f"requested={_ship_anyway_requested} "
                                      f"cap={_reviewer_cap_exhausted}")
                            else:
                                lines = [
                                    f"BLOCKED — last run_review on this project returned status='{_rstatus}'.",
                                    f"Build: `{_env.get('build_cmd','')}` exit={_env.get('build_exit','?')}. "
                                    f"Tests: `{_env.get('test_cmd','')}` exit={_env.get('test_exit','?')}.",
                                    "",
                                    f"Do not call download_project until the project passes review unless the latest user message explicitly asks to ship/download anyway. "
                                    f"Reviewer flagged {n} issue{'s' if n != 1 else ''}:",
                                ]
                                for i, iss in enumerate(issues[:5], 1):
                                    lines.append(
                                        f"  {i}. [{iss.get('severity','?')}] {iss.get('file','?')}"
                                        + (f":{','.join(str(x) for x in iss.get('lines') or [])}" if iss.get('lines') else "")
                                        + f" — {iss.get('summary','')}"
                                    )
                                    scope = iss.get("suggested_fix_scope") or []
                                    if scope:
                                        lines.append(f"     fix scope: {', '.join(scope[:5])}")
                                lines.append("")
                                lines.append(
                                    "REQUIRED NEXT STEP: fix the listed issue with the appropriate fix worker, "
                                    "then call run_review again. Repeat until run_review returns CLEAN. "
                                    "If you cannot get it clean, ask the user whether they want the current files as-is."
                                )
                                await events.emit(conv_id, "tool_end", {
                                    "tool": "download_project", "icon": "code",
                                    "status": f"⛔ Blocked — last review had {n} issue{'s' if n != 1 else ''}",
                                })
                                print(f"[CHAT] download_project blocked: latest reviewer={_latest_reviewer.get('id')} status={_rstatus} issues={n}")
                                return "\n".join(lines)
                        if _rstatus == "clean":
                            if _ship_anyway_requested:
                                _download_warning_lines = [
                                    "WARNING: Shipped before final acceptance review.",
                                    f"Clean reviewer run: {_latest_reviewer.get('id')}.",
                                ]
                                print(f"[CHAT] download_project allowing ship-anyway "
                                      f"before acceptance after clean reviewer={_latest_reviewer.get('id')}")
                            else:
                                await events.emit(conv_id, "tool_end", {
                                    "tool": "download_project", "icon": "code",
                                    "status": "⛔ Blocked — acceptance review required",
                                })
                                return (
                                    f"BLOCKED — run_review ({_latest_reviewer.get('id')}) is clean, "
                                    "but run_acceptance_review has not accepted the project yet.\n\n"
                                    "REQUIRED NEXT STEP:\n"
                                    f"  run_acceptance_review(reviewer_run_id='{_latest_reviewer.get('id')}')"
                                )
                except Exception as _ge:
                    # Gate is best-effort — don't crash legitimate downloads if
                    # the runs query fails. Log and proceed.
                    print(f"[CHAT] download_project gate check failed (non-fatal): {_ge}")

            await events.emit(conv_id, "tool_start", {"tool": "download_project", "icon": "code", "status": f"Packaging: {directory}"})
            dirname = directory.rstrip("/").split("/")[-1] or "project"
            # Clean up auto-generated UUIDs from directory names (project-abc12345 → project)
            # But keep meaningful names like "portscout" or "weather-dashboard"
            if re.match(r'^project-[a-f0-9]{4,8}$', dirname):
                dirname = "project"
            tarname = f"{dirname}.tar.gz"
            qdir = shlex.quote(directory)
            qtarname = shlex.quote(f"/tmp/{tarname}")
            exclude_args = (
                "--exclude='.pytest_cache' --exclude='*/.pytest_cache/*' "
                "--exclude='__pycache__' --exclude='*/__pycache__/*' "
                "--exclude='*.pyc' --exclude='*.egg-info' --exclude='*.egg-info/*' "
                "--exclude='.mypy_cache' --exclude='*/.mypy_cache/*' "
                "--exclude='.ruff_cache' --exclude='*/.ruff_cache/*' "
                "--exclude='.cache' --exclude='*/.cache/*' "
                "--exclude='.git' --exclude='*/.git/*' "
                "--exclude='node_modules' --exclude='*/node_modules/*' "
                "--exclude='dist' --exclude='*/dist/*' "
                "--exclude='build' --exclude='*/build/*' "
                "--exclude='target' --exclude='*/target/*' "
                "--exclude='.next' --exclude='*/.next/*' "
                "--exclude='venv' --exclude='*/venv/*' "
                "--exclude='.venv' --exclude='*/.venv/*' "
                "--exclude='.npm' --exclude='*/.npm/*'"
            )
            summary_cmd = (
                f"cd {qdir} && "
                "included=$(find . -type f "
                "! -path '*/.pytest_cache/*' ! -path '*/__pycache__/*' ! -name '*.pyc' "
                "! -path '*.egg-info/*' ! -path '*/.mypy_cache/*' ! -path '*/.ruff_cache/*' "
                "! -path '*/.cache/*' ! -path '*/.git/*' ! -path '*/node_modules/*' "
                "! -path '*/dist/*' ! -path '*/build/*' ! -path '*/target/*' "
                "! -path '*/.next/*' ! -path '*/venv/*' ! -path '*/.venv/*' "
                "! -path '*/.npm/*' | wc -l); "
                "excluded=$(find . \\( "
                "-path '*/.pytest_cache/*' -o -path '*/__pycache__/*' -o -name '*.pyc' "
                "-o -path '*.egg-info/*' -o -path '*/.mypy_cache/*' -o -path '*/.ruff_cache/*' "
                "-o -path '*/.cache/*' -o -path '*/.git/*' -o -path '*/node_modules/*' "
                "-o -path '*/dist/*' -o -path '*/build/*' -o -path '*/target/*' "
                "-o -path '*/.next/*' -o -path '*/venv/*' -o -path '*/.venv/*' "
                "-o -path '*/.npm/*' \\) -print | wc -l); "
                "excluded_list=$(find . \\( "
                "-path '*/.pytest_cache/*' -o -path '*/__pycache__/*' -o -name '*.pyc' "
                "-o -path '*.egg-info/*' -o -path '*/.mypy_cache/*' -o -path '*/.ruff_cache/*' "
                "-o -path '*/.cache/*' -o -path '*/.git/*' -o -path '*/node_modules/*' "
                "-o -path '*/dist/*' -o -path '*/build/*' -o -path '*/target/*' "
                "-o -path '*/.next/*' -o -path '*/venv/*' -o -path '*/.venv/*' "
                "-o -path '*/.npm/*' \\) -print | sort | head -20); "
                "printf 'PACKAGING_SUMMARY:%s:%s\\n' \"$included\" \"$excluded\"; "
                "printf '%s\n' \"$excluded_list\" | sed '/^$/d' | sed 's/^/EXCLUDED_EXAMPLE:/'"
            )
            summary_r = await http.post(f"{config.CODEBOX_URL}/command", json={
                "command": summary_cmd,
                "timeout": 30,
            }, timeout=40)
            summary_out = summary_r.json().get("stdout", "") if summary_r.status_code == 200 else ""
            included_count = 0
            excluded_count = 0
            excluded_examples = []
            m_summary = re.search(r"PACKAGING_SUMMARY:(\d+):(\d+)", summary_out)
            if m_summary:
                included_count = int(m_summary.group(1))
                excluded_count = int(m_summary.group(2))
            for line in summary_out.splitlines():
                if line.startswith("EXCLUDED_EXAMPLE:"):
                    excluded_examples.append(line.split(":", 1)[1])
            r = await http.post(f"{config.CODEBOX_URL}/command", json={
                "command": f"cd {qdir} && tar czf {qtarname} {exclude_args} . 2>&1 && base64 -w0 {qtarname}",
                "timeout": 60
            }, timeout=70)
            result = r.json()
            raw = result.get("stdout", "").strip()
            b64_match = re.search(r'([A-Za-z0-9+/\n]{100,}={0,2})$', raw)
            b64_data = b64_match.group(1).replace("\n", "").strip() if b64_match else ""
            if b64_data:
                estimated_size = len(b64_data) * 3 // 4
                if estimated_size > config.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
                    await events.emit(conv_id, "tool_end", {"tool": "download_project", "icon": "code",
                        "status": f"Archive too large ({estimated_size // (1024*1024)}MB > {config.MAX_UPLOAD_SIZE_MB}MB limit)"})
                    return f"ERROR: Project archive too large (exceeds {config.MAX_UPLOAD_SIZE_MB}MB limit)"
                os.makedirs(config.SANDBOX_OUTPUTS_DIR, exist_ok=True)
                filepath = os.path.join(config.SANDBOX_OUTPUTS_DIR, tarname)
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(b64_data))
                file_meta = _artifact_file_metadata(filepath)
                download_url = f"/api/downloads/{tarname}"
                await events.emit(conv_id, "tool_end", {"tool": "download_project", "icon": "code",
                    "status": f"{tarname} ready",
                    "detail": json.dumps({
                        "file": tarname,
                        "directory": directory,
                        "download_url": download_url,
                        "included_count": included_count,
                        "excluded_count": excluded_count,
                        "excluded_examples": excluded_examples[:10],
                    }),
                })
                _wf = None
                _proj_for_wf = ""
                _partial = bool(_download_warning_lines)
                try:
                    if directory.startswith("/root/projects/"):
                        _proj_for_wf = directory.rstrip("/").rsplit("/", 1)[-1]
                    _wf = await db.get_latest_coder_workflow(conv_id, project_id=_proj_for_wf)
                except Exception as _wfe:
                    print(f"[WORKFLOW] latest workflow lookup failed: {_wfe}")
                artifact = None
                try:
                    cf_id = f"cf-{uuid.uuid4().hex[:8]}"
                    _mime = db.artifact_mime_type_for_filename(tarname)
                    _previous_artifact = None
                    if (_wf or {}).get("id"):
                        try:
                            _prior = await db.list_artifacts(workflow_id=_wf["id"], kind="archive", limit=1)
                            _previous_artifact = _prior[0] if _prior else None
                        except Exception as _pae:
                            print(f"[FileTrack] previous artifact lookup failed: {_pae}")
                    artifact = await db.add_conversation_file(
                        cf_id,
                        conv_id,
                        tarname,
                        download_url,
                        message_id=artifact_message_id,
                        run_id=(_wf or {}).get("active_run_id"),
                        workflow_id=(_wf or {}).get("id"),
                        kind="archive",
                        mime_type=_mime,
                        storage_path=filepath,
                        size_bytes=file_meta["size_bytes"],
                        sha256=file_meta["sha256"],
                        exists_status="present",
                        status="draft" if _partial else "accepted",
                        parent_artifact_id=(
                            (_previous_artifact or {}).get("parent_artifact_id")
                            or ((_previous_artifact or {}).get("id") if _previous_artifact else None)
                        ),
                        supersedes_artifact_id=(_previous_artifact or {}).get("id"),
                        content_text=_artifact_index_text(filepath, tarname, "archive", _mime),
                        tags=["project"] if _proj_for_wf else [],
                        metadata={
                            "source_tool": "download_project",
                            "directory": directory,
                            "project_id": _proj_for_wf,
                            "included_count": included_count,
                            "excluded_count": excluded_count,
                            "excluded_examples": excluded_examples[:10],
                            "artifact_status": "partial_delivered" if _partial else "delivered",
                            "partial": _partial,
                            "size_bytes": file_meta["size_bytes"],
                            "sha256": file_meta["sha256"],
                        },
                    )
                except Exception as e:
                    print(f"[FileTrack] {e}")
                await events.emit(conv_id, "file_ready", {
                    "filename": tarname,
                    "url": download_url,
                    "artifact_id": (artifact or {}).get("id"),
                    "kind": "archive",
                    "mime_type": (artifact or {}).get("mime_type") or db.artifact_mime_type_for_filename(tarname),
                    "workflow_id": (_wf or {}).get("id"),
                    "project_id": _proj_for_wf,
                    "artifact_status": "partial_delivered" if _partial else "delivered",
                })
                try:
                    if _wf:
                        await db.update_coder_workflow(
                            _wf["id"],
                            state="partial_delivered" if _partial else "accepted",
                            artifact_status="partial_delivered" if _partial else "delivered",
                        )
                except Exception as _wfe:
                    print(f"[WORKFLOW] delivery state update failed: {_wfe}")
                summary_lines = [
                    f"**[Download {tarname}]({download_url})**",
                    "",
                    f"Packaging summary: included {included_count} file(s), excluded {excluded_count} generated/cache/build artifact(s).",
                ]
                if _download_warning_lines:
                    summary_lines.extend([""] + _download_warning_lines)
                if excluded_examples:
                    summary_lines.append("Excluded examples: " + ", ".join(excluded_examples[:5]))
                return "\n".join(summary_lines)
            else:
                await events.emit(conv_id, "tool_end", {"tool": "download_project", "icon": "code", "status": f"Could not package: {directory}"})
                return f"ERROR: Could not package directory: {directory}"

        elif name == "delete_file":
            path = args.get("path", "")
            if not path or path in ("/", "/root", "/etc", "/usr", "/bin", "/tmp"):
                return f"ERROR: Refusing to delete protected path: {path}"
            await events.emit(conv_id, "tool_start", {"tool": "delete_file", "icon": "terminal", "status": f"Deleting: {path}"})
            r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": f"rm -rf {shlex.quote(path)}", "timeout": 10}, timeout=15)
            result = r.json()
            exit_code = result.get("exit_code", 0)
            ok = exit_code == 0
            await events.emit(conv_id, "tool_end", {"tool": "delete_file", "icon": "terminal", "status": f"{'Deleted' if ok else 'Failed'}: {path}"})
            return f"Deleted: {path}" if ok else f"ERROR: Delete failed (exit {exit_code}): {result.get('stderr', '')[:200]}"

        elif name == "search_files":
            pattern = args.get("pattern", "")
            if not pattern:
                return "ERROR: pattern is required"
            search_path = args.get("path", "/root")
            file_pattern = args.get("file_pattern", "")
            await events.emit(conv_id, "tool_start", {"tool": "search_files", "icon": "search", "status": f"Searching: {pattern[:40]}"})
            cmd = f"grep -rn"
            if file_pattern:
                cmd += f" --include={shlex.quote(file_pattern)}"
            cmd += f" {shlex.quote(pattern)} {shlex.quote(search_path)} 2>/dev/null | head -60"
            r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 15}, timeout=20)
            result = r.json()
            output = result.get("stdout", "").strip()
            match_count = len(output.splitlines()) if output else 0
            await events.emit(conv_id, "tool_end", {"tool": "search_files", "icon": "search", "status": f"Found {match_count} matches"})
            return output if output else f"No matches found for '{pattern}' in {search_path}"

        elif name == "diff_files":
            path_a = args.get("path_a", "")
            path_b = args.get("path_b", "")
            if not path_a or not path_b:
                return "ERROR: path_a and path_b are required"
            await events.emit(conv_id, "tool_start", {"tool": "diff_files", "icon": "terminal", "status": f"Diffing files..."})
            cmd = f"diff -u {shlex.quote(path_a)} {shlex.quote(path_b)}"
            r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 10}, timeout=15)
            result = r.json()
            output = result.get("stdout", "").strip()
            exit_code = result.get("exit_code", 0)
            await events.emit(conv_id, "tool_end", {"tool": "diff_files", "icon": "terminal", "status": "Diff complete"})
            if exit_code == 0:
                return "Files are identical."
            elif exit_code == 1:
                return output[:8000] if output else "Files differ but no diff output."
            else:
                return f"ERROR: diff failed: {result.get('stderr', '')[:200]}"

        elif name == "git_init":
            path = args.get("path", "/root")
            language = args.get("language", "python").lower()
            await events.emit(conv_id, "tool_start", {"tool": "git_init", "icon": "terminal", "status": f"Initializing git repo in {path}"})
            gitignore_map = {
                "python": "__pycache__/\n*.pyc\n*.pyo\nvenv/\n.env\n*.egg-info/\ndist/\nbuild/\n.pytest_cache/\n",
                "javascript": "node_modules/\n.env\ndist/\nbuild/\n*.log\n.cache/\ncoverage/\n",
                "typescript": "node_modules/\n.env\ndist/\nbuild/\n*.log\n.cache/\ncoverage/\n",
                "rust": "target/\nCargo.lock\n",
                "go": "bin/\n*.exe\nvendor/\n",
                "java": "*.class\ntarget/\n.idea/\n*.jar\nbuild/\n",
            }
            gitignore = gitignore_map.get(language, "__pycache__/\nnode_modules/\n.env\nvenv/\n")
            b64_gi = base64.b64encode(gitignore.encode()).decode()
            cmd = (
                f"cd {shlex.quote(path)} && "
                f"git init && "
                f"printf '%s' {shlex.quote(b64_gi)} | base64 -d > .gitignore && "
                f"git add -A && "
                f"git -c user.email='bot@hyprchat' -c user.name='HyprCoder' commit -m 'Initial commit' 2>&1"
            )
            r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 15}, timeout=20)
            result = r.json()
            output = result.get("stdout", "").strip()
            ok = result.get("exit_code", -1) == 0
            await events.emit(conv_id, "tool_end", {"tool": "git_init", "icon": "terminal", "status": f"{'Initialized' if ok else 'Failed'}"})
            return output[:3000] if output else ("Git repo initialized." if ok else f"ERROR: {result.get('stderr', '')[:200]}")

        elif name == "git_diff":
            path = args.get("path", "/root")
            await events.emit(conv_id, "tool_start", {"tool": "git_diff", "icon": "terminal", "status": "Checking changes..."})
            cmd = f"cd {shlex.quote(path)} && git diff && git diff --cached && git status --short"
            r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 10}, timeout=15)
            result = r.json()
            output = result.get("stdout", "").strip()
            await events.emit(conv_id, "tool_end", {"tool": "git_diff", "icon": "terminal", "status": "Done"})
            return output[:8000] if output else "No changes detected."

        elif name == "git_commit":
            message = args.get("message", "Update")
            path = args.get("path", "/root")
            await events.emit(conv_id, "tool_start", {"tool": "git_commit", "icon": "terminal", "status": f"Committing: {message[:40]}"})
            cmd = (
                f"cd {shlex.quote(path)} && "
                f"git add -A && "
                f"git -c user.email='bot@hyprchat' -c user.name='HyprCoder' commit -m {shlex.quote(message)} 2>&1"
            )
            r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 15}, timeout=20)
            result = r.json()
            output = result.get("stdout", "").strip()
            ok = result.get("exit_code", -1) == 0
            await events.emit(conv_id, "tool_end", {"tool": "git_commit", "icon": "terminal", "status": f"{'Committed' if ok else 'Failed'}"})
            return output[:3000] if output else ("Committed." if ok else f"ERROR: {result.get('stderr', '')[:200]}")

        elif name == "run_tests":
            path = args.get("path", "/root")
            framework = args.get("framework", "").lower()
            await events.emit(conv_id, "tool_start", {"tool": "run_tests", "icon": "code", "status": "Detecting test framework..."})

            if not framework:
                # Auto-detect
                detect_cmd = (
                    f"cd {shlex.quote(path)} && "
                    f"ls pytest.ini setup.cfg pyproject.toml conftest.py 2>/dev/null; "
                    f"ls package.json Cargo.toml go.mod 2>/dev/null; "
                    f"find . -maxdepth 3 -name 'test_*.py' -o -name '*_test.py' -o -name '*.test.js' -o -name '*.test.ts' -o -name '*.spec.js' 2>/dev/null | head -5"
                )
                detect_r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": detect_cmd, "timeout": 10}, timeout=15)
                detect_out = detect_r.json().get("stdout", "")
                if "Cargo.toml" in detect_out:
                    framework = "cargo"
                elif "go.mod" in detect_out:
                    framework = "go"
                elif "package.json" in detect_out:
                    # Check if jest or vitest
                    pkg_r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": f"cat {shlex.quote(path)}/package.json 2>/dev/null", "timeout": 5}, timeout=10)
                    pkg = pkg_r.json().get("stdout", "")
                    if "vitest" in pkg:
                        framework = "vitest"
                    elif "jest" in pkg or ".test.js" in detect_out or ".test.ts" in detect_out or ".spec.js" in detect_out:
                        framework = "jest"
                    else:
                        framework = "npm"
                elif any(f in detect_out for f in ("pytest.ini", "setup.cfg", "pyproject.toml", "conftest.py", "test_")):
                    framework = "pytest"
                else:
                    framework = "pytest"  # fallback

            test_cmds = {
                "pytest": f"cd {shlex.quote(path)} && /root/venv/bin/python3 -m pytest -v --tb=short 2>&1",
                "jest": f"cd {shlex.quote(path)} && npx jest --verbose 2>&1",
                "vitest": f"cd {shlex.quote(path)} && npx vitest run 2>&1",
                "npm": f"cd {shlex.quote(path)} && npm test 2>&1",
                "cargo": f"cd {shlex.quote(path)} && cargo test 2>&1",
                "go": f"cd {shlex.quote(path)} && go test ./... -v 2>&1",
            }
            cmd = test_cmds.get(framework, test_cmds["pytest"])
            await events.emit(conv_id, "tool_start", {"tool": "run_tests", "icon": "code", "status": f"Running {framework} tests..."})
            start = time.time()
            r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 120}, timeout=130)
            elapsed = time.time() - start
            result = r.json()
            output = _strip_ansi(result.get("stdout", "")).strip()
            ok = result.get("exit_code", -1) == 0
            await events.emit(conv_id, "tool_end", {"tool": "run_tests", "icon": "code", "status": f"{'PASSED' if ok else 'FAILED'} ({framework}, {elapsed:.1f}s)"})
            parts = [f"**{'TESTS PASSED' if ok else 'TESTS FAILED'}** | {framework} | {elapsed:.1f}s\n"]
            if output:
                parts.append(f"```\n{output[:8000]}\n```")

            # Post-clean tripwire: if run_tests just failed but the most recent
            # run_review on this conversation reported test_exit=0 / status=clean,
            # the reviewer is producing a false negative (e.g. its test_cmd
            # swallowed pytest's exit code via `|| echo`). Synthesize a real
            # reviewer envelope from this run_tests output so run_fixer becomes
            # callable with a non-empty `issues` list. Without this, the model
            # falls back to v1's manual write_file loop because `run_fixer`
            # against a clean reviewer envelope returns status='skipped'.
            if not ok and conv_id and framework == "pytest":
                try:
                    synth_id = await _synthesize_reviewer_from_test_failure(
                        conv_id, output, path, framework, elapsed,
                    )
                    if synth_id:
                        parts.append(
                            f"\n\n⚠ **Reviewer false-negative detected.** "
                            f"The latest `run_review` on this conversation reported "
                            f"`status=clean / test_exit=0`, but `run_tests` just failed. "
                            f"Synthesized a reviewer envelope from this failure: "
                            f"`{synth_id}`.\n\n"
                            f"**Next step — call `run_fixer(reviewer_run_id='{synth_id}')`** "
                            f"to apply targeted fixes. Do NOT call `write_file` manually for "
                            f"the failing files — that's the v1 antipattern v2 is meant to "
                            f"replace, and the v2 gate will block it on the next round."
                        )
                except Exception as _twe:
                    print(f"[TRIPWIRE] synthesize failed (non-fatal): {_twe}")

            return "\n".join(parts)

        elif name == "lint_code":
            path = args.get("path", "/root")
            language = args.get("language", "").lower()
            await events.emit(conv_id, "tool_start", {"tool": "lint_code", "icon": "code", "status": "Detecting language..."})

            if not language:
                detect_cmd = f"ls {shlex.quote(path)}/*.py {shlex.quote(path)}/**/*.py {shlex.quote(path)}/Cargo.toml {shlex.quote(path)}/go.mod {shlex.quote(path)}/package.json 2>/dev/null | head -10"
                detect_r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": detect_cmd, "timeout": 5}, timeout=10)
                detect_out = detect_r.json().get("stdout", "")
                if "Cargo.toml" in detect_out:
                    language = "rust"
                elif "go.mod" in detect_out:
                    language = "go"
                elif "package.json" in detect_out:
                    language = "javascript"
                elif ".py" in detect_out:
                    language = "python"
                else:
                    language = "python"

            lint_cmds = {
                "python": f"cd {shlex.quote(path)} && pip3 install -q ruff 2>/dev/null && ruff check --fix . 2>&1 && ruff format . 2>&1",
                "javascript": f"cd {shlex.quote(path)} && npx prettier --write '**/*.{{js,jsx,ts,tsx,json,css}}' 2>&1",
                "typescript": f"cd {shlex.quote(path)} && npx prettier --write '**/*.{{js,jsx,ts,tsx,json,css}}' 2>&1",
                "rust": f"cd {shlex.quote(path)} && cargo fmt 2>&1",
                "go": f"cd {shlex.quote(path)} && gofmt -w . 2>&1",
            }
            cmd = lint_cmds.get(language, lint_cmds["python"])
            await events.emit(conv_id, "tool_start", {"tool": "lint_code", "icon": "code", "status": f"Linting {language}..."})
            r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 60}, timeout=70)
            result = r.json()
            output = _strip_ansi(result.get("stdout", "")).strip()
            ok = result.get("exit_code", -1) == 0
            await events.emit(conv_id, "tool_end", {"tool": "lint_code", "icon": "code", "status": f"{'Done' if ok else 'Issues found'} ({language})"})
            parts = [f"**Lint/Format: {language}** {'✅ Clean' if ok else '⚠️ Issues'}\n"]
            if output:
                parts.append(f"```\n{output[:6000]}\n```")
            return "\n".join(parts)

        elif name == "resume_project":
            project_id = args.get("project_id", "")
            await events.emit(conv_id, "tool_start", {"tool": "resume_project", "icon": "activity", "status": "Loading project context..."})
            # Try DB first
            project = None
            if project_id:
                project = await db.get_coding_project(project_id)
            if not project:
                project = await db.get_coding_project_by_conv(conv_id)
            if not project:
                await events.emit(conv_id, "tool_end", {"tool": "resume_project", "icon": "activity", "status": "No project found"})
                return "No previous project found for this conversation. Start fresh with plan_project or generate_code."

            # Scan sandbox for current files
            scan_cmd = (
                "find /root/ -maxdepth 5 -type f "
                "! -path '*/node_modules/*' ! -path '*/.git/*' "
                "! -path '*/__pycache__/*' ! -path '*/.cache/*' "
                "! -path '*/.npm/*' ! -path '*/venv/*' "
                "! -path '*/.openhands/*' ! -path '*/.bash_history' "
                "! -name '*.pyc' ! -name 'package-lock.json' "
                "2>/dev/null | sort"
            )
            try:
                scan_r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": scan_cmd, "timeout": 10}, timeout=15)
                live_files = [f for f in scan_r.json().get("stdout", "").strip().splitlines() if f.strip()]
            except Exception:
                live_files = []

            await events.emit(conv_id, "tool_end", {"tool": "resume_project", "icon": "activity", "status": f"Loaded: {project['name']}"})
            parts = [
                f"**Resuming Project: {project['name']}**",
                f"- Language: {project.get('language', 'unknown')}",
                f"- Description: {project.get('description', 'N/A')}",
            ]
            if project.get("last_plan"):
                parts.append(f"\n**Previous Plan:**\n{project['last_plan'][:4000]}")
            if project.get("file_manifest"):
                parts.append(f"\n**Saved file manifest:**")
                for f in project["file_manifest"][:30]:
                    parts.append(f"  - {f}")
            if live_files:
                parts.append(f"\n**Live files on sandbox ({len(live_files)}):**")
                for f in live_files[:30]:
                    parts.append(f"  - {f}")
            parts.append("\nYou can now continue working on this project. Read any file to see its current state.")
            return "\n".join(parts)

        elif name == "get_coder_workflow":
            workflow_id = (args.get("workflow_id") or "").strip()
            if not workflow_id:
                return "ERROR: workflow_id is required"
            wf = await db.get_coder_workflow(workflow_id)
            if not wf:
                return f"ERROR: workflow not found: {workflow_id}"
            return "CODER WORKFLOW:\n" + json.dumps(wf, indent=2)

        elif name == "cancel_coder_workflow":
            workflow_id = (args.get("workflow_id") or "").strip()
            if not workflow_id:
                return "ERROR: workflow_id is required"
            wf = await db.get_coder_workflow(workflow_id)
            if not wf:
                return f"ERROR: workflow not found: {workflow_id}"
            active_run_id = wf.get("active_run_id") or ""
            await db.update_coder_workflow(
                workflow_id,
                state="cancelled",
                cancel_requested=True,
                artifact_status="cancelled",
            )
            signaled = False
            if active_run_id:
                signaled = cancel_registry.signal(active_run_id)
                try:
                    run = await db.get_run(active_run_id)
                    if run and run.get("status") in {"queued", "running", "pending"}:
                        env = run.get("result_envelope") or {}
                        env = {**env, "status": "cancelled", "summary": env.get("summary") or "Workflow cancelled"}
                        await db.update_run(active_run_id, status="cancelled", result_envelope=env, ended=True)
                except Exception as e:
                    print(f"[WORKFLOW] active run cancel DB update failed: {e}")
            await events.emit(conv_id, "tool_end", {
                "tool": "cancel_coder_workflow", "icon": "activity",
                "status": f"Workflow cancelled ({workflow_id})",
            })
            return f"WORKFLOW CANCELLED: {workflow_id}" + (f" (active run signaled: {signaled})" if active_run_id else "")

        elif name == "start_coder_workflow":
            mode = (args.get("mode") or "").strip().lower()
            task = (args.get("task") or "").strip()
            project_id = (args.get("project_id") or "").strip()
            language = (args.get("language") or "python").strip() or "python"
            if not task:
                return "ERROR: start_coder_workflow requires task"
            if mode not in {"build_from_prompt", "fix_uploaded_project", "ask_uploaded_project"}:
                return "ERROR: mode must be build_from_prompt, fix_uploaded_project, or ask_uploaded_project"

            if not project_id and mode != "build_from_prompt" and conv_id:
                active = await db.get_coding_project_by_conv(conv_id)
                if active:
                    project_id = active.get("openhands_project_id") or active.get("id") or ""

            workflow_id = f"cw-{uuid.uuid4().hex[:12]}"
            contract = {}
            if project_id:
                try:
                    active_project = await db.get_coding_project(project_id)
                    if active_project:
                        from agents import language_adapters
                        contract = language_adapters.detect_contract(
                            active_project.get("file_manifest") or [],
                            active_project.get("language") or language,
                        )
                except Exception as e:
                    print(f"[WORKFLOW] contract detection failed: {e}")
            await db.create_coder_workflow(
                workflow_id, conv_id, project_id=project_id, mode=mode,
                state="planning" if mode == "build_from_prompt" else ("fixing" if mode == "fix_uploaded_project" else "answering"),
                user_task=task, contract=contract,
                artifact_status="not_ready",
            )
            await events.emit(conv_id, "tool_start", {
                "tool": "start_coder_workflow", "icon": "activity",
                "status": f"Started workflow {workflow_id} ({mode})",
                "workflow_id": workflow_id,
            })

            if mode == "ask_uploaded_project":
                answer = await exec_tool(
                    http, events, "ask_project",
                    {"question": task, "project_dir": f"/root/projects/{project_id}" if project_id else ""},
                    conv_id,
                    custom_tool_map=custom_tool_map,
                    connector_tool_name_map=connector_tool_name_map,
                    conv_model=conv_model,
                    kb_ids=kb_ids,
                    artifact_message_id=artifact_message_id,
                )
                await db.update_coder_workflow(workflow_id, state="answering", artifact_status="not_applicable")
                return f"workflow_id: {workflow_id}\n\n{answer}"

            if mode == "fix_uploaded_project":
                result = await exec_tool(
                    http, events, "run_aider_fix",
                    {"task": task, "project_dir": f"/root/projects/{project_id}" if project_id else ""},
                    conv_id,
                    custom_tool_map=custom_tool_map,
                    connector_tool_name_map=connector_tool_name_map,
                    conv_model=conv_model,
                    kb_ids=kb_ids,
                    artifact_message_id=artifact_message_id,
                )
                return f"workflow_id: {workflow_id}\n\n{result}"

            plan = await exec_tool(
                http, events, "plan_project",
                {"task": task, "language": language, "constraints": "Return a machine-checkable project contract for OpenHands before build."},
                conv_id,
                custom_tool_map=custom_tool_map,
                connector_tool_name_map=connector_tool_name_map,
                conv_model=conv_model,
                kb_ids=kb_ids,
                artifact_message_id=artifact_message_id,
            )
            await db.update_coder_workflow(workflow_id, state="planning", artifact_status="not_ready")
            return (
                f"workflow_id: {workflow_id}\n\n"
                f"{plan}\n\n"
                "NEXT STEP: call generate_code with the planned task. OpenHands remains the greenfield Builder; "
                "normal delivery waits for run_review to pass and run_acceptance_review to accept. "
                "If the user later explicitly asks to ship/download despite known issues, disclose those issues and package the current state."
            )

        elif name == "run_aider_fix":
            from agents import aider_fixer, language_adapters

            if not getattr(config, "AIDER_ENABLED", True):
                issue_run_id = (args.get("issue_run_id") or args.get("reviewer_run_id") or "").strip()
                return await exec_tool(
                    http, events, "run_fixer",
                    {"reviewer_run_id": issue_run_id} if issue_run_id else {},
                    conv_id,
                    custom_tool_map=custom_tool_map,
                    connector_tool_name_map=connector_tool_name_map,
                    conv_model=conv_model,
                    kb_ids=kb_ids,
                    artifact_message_id=artifact_message_id,
                )

            task = (args.get("task") or args.get("description") or "").strip()
            project_dir = (args.get("project_dir") or "").strip()
            issue_run_id = (args.get("issue_run_id") or args.get("reviewer_run_id") or "").strip()
            allowed_files = args.get("allowed_files") or []
            if isinstance(allowed_files, str):
                allowed_files = [p.strip() for p in re.split(r"[,\n]", allowed_files) if p.strip()]
            if not task:
                return "ERROR: run_aider_fix requires task"

            issue_run = None
            if issue_run_id:
                try:
                    issue_run = await db.get_run(issue_run_id)
                except Exception:
                    issue_run = None
            if not issue_run and conv_id:
                runs = await db.get_runs_by_conversation(conv_id, limit=30)
                issue_run = next(
                    (r for r in runs
                     if r.get("role") in {"reviewer", "acceptance"}
                     and ((r.get("result_envelope") or {}).get("status") or "").lower() in {"issues", "error"}),
                    None,
                )
                if issue_run:
                    issue_run_id = issue_run.get("id", "")

            issue_env = (issue_run or {}).get("result_envelope") or {}
            if issue_run:
                issue_env = {**issue_env, "_source_role": issue_run.get("role", "")}
            if not project_dir:
                project_dir = (issue_env.get("project_dir") or "").strip()

            project_id = (args.get("project_id") or "").strip()
            project_id = project_id or (issue_run or {}).get("project_id") or ""
            active_project = None
            if conv_id and (not project_dir or not project_id):
                active_project = await db.get_coding_project_by_conv(conv_id)
                if active_project:
                    project_id = project_id or active_project.get("openhands_project_id") or active_project.get("id") or ""
                    project_dir = project_dir or (f"/root/projects/{project_id}" if project_id else "")

            if not project_dir:
                return "ERROR: run_aider_fix needs project_dir or an active uploaded project on this conversation."

            contract = {}
            workflow_id = ""
            if conv_id:
                latest_wf = await db.get_latest_coder_workflow(conv_id, project_id=project_id)
                if latest_wf:
                    workflow_id = latest_wf.get("id", "")
                    contract = latest_wf.get("contract_json") or {}
                elif active_project or project_id:
                    if not active_project and project_id:
                        active_project = await db.get_coding_project(project_id)
                    if active_project:
                        contract = language_adapters.detect_contract(
                            active_project.get("file_manifest") or [],
                            active_project.get("language") or "",
                        )
                    workflow_id = f"cw-{uuid.uuid4().hex[:12]}"
                    await db.create_coder_workflow(
                        workflow_id, conv_id, project_id=project_id,
                        mode="fix_uploaded_project", state="fixing",
                        user_task=task, contract=contract, artifact_status="not_ready",
                    )
                if workflow_id:
                    await db.update_coder_workflow(workflow_id, state="fixing", artifact_status="not_ready")

            # Prior fix attempts this turn — folded into the task text so the
            # Aider prompt (built worker-side) sees them without a worker change.
            _attempts_for_aider = await _prior_fix_attempts_context(conv_id)
            if _attempts_for_aider:
                task = (
                    f"{task}\n\n"
                    f"Previous fix attempts already made for this request — do NOT "
                    f"repeat an approach that already failed; try something different:\n"
                    f"{_attempts_for_aider}"
                )
                print(f"[run_aider_fix] injecting attempt history "
                      f"({len(_attempts_for_aider)} chars)")

            envelope = await aider_fixer.run_aider_fix(
                http, events, conv_id,
                project_dir=project_dir, task=task,
                issue_envelope=issue_env, contract=contract,
                model=config.AIDER_MODEL or config.FIXER_MODEL or config.CODER_MODEL or conv_model or config.DEFAULT_MODEL,
                test_cmd=contract.get("aider_test_cmd") or contract.get("test_cmd") or "",
                lint_cmd=contract.get("aider_lint_cmd") if contract.get("safe_lint") else "",
                allowed_files=allowed_files,
                project_id=project_id,
                parent_run_id=issue_run_id,
                workflow_id=workflow_id,
            )
            files = envelope.get("files_touched") or []
            status = envelope.get("status", "?")
            if status == "applied":
                return (
                    f"AIDER APPLIED EDITS to {len(files)} file(s).\n"
                    + "\n".join(f"  - {f}" for f in files[:12])
                    + f"\nworkflow_id: {workflow_id or '(none)'}\n"
                    "REQUIRED NEXT TOOL CALL: run_review. Aider only edits; Reviewer must verify build/tests before acceptance or delivery."
                    + await _fix_budget_note(conv_id, envelope.get("source_role"))
                )
            if status == "no_changes":
                return (
                    f"AIDER MADE NO CHANGES. {envelope.get('summary','')}\n"
                    "Run review if you need to verify current state, or explain to the user why no patch was produced."
                )
            return (
                f"AIDER FAILED ({status}): {envelope.get('summary','')}\n"
                f"stderr: {(envelope.get('stderr_tail') or '')[-800:]}\n"
                "REQUIRED NEXT TOOL CALL: run_review. Do not call read_file/write_file/run_shell "
                "or run_fixer for uploaded-project fixes; Reviewer must classify the remaining "
                "failure and then Aider gets the next scoped repair."
            )

        elif name == "run_review":
            # Coder Bot v2 — run the Reviewer agent on a project. Returns a structured
            # issue list the chat agent can react to (instead of doing 28 rounds of
            # manual file reading + rewriting). The Reviewer is read-only.
            from agents import reviewer
            project_dir = (args.get("project_dir") or "").strip()
            project_id = (args.get("project_id") or "").strip()

            # Validate the model-passed project_dir actually exists on Codebox.
            # qwen-style models occasionally fabricate project paths — inheriting
            # a name from training data or a prior conversation — instead of
            # using the one generate_code just built. Without this guard the
            # Reviewer runs against a non-existent path, fails with "no build
            # markers found", and the Fixer then inherits the bad envelope.
            # Same fabrication guard ask_project uses below.
            if project_dir and conv_id:
                try:
                    _existsr = await http.post(
                        f"{config.CODEBOX_URL}/command",
                        json={"command": f"test -d {shlex.quote(project_dir)} && echo OK || echo NO",
                              "timeout": 5},
                        timeout=10,
                    )
                    _on_disk = (_existsr.status_code == 200
                                and "OK" in (_existsr.json().get("stdout") or ""))
                except Exception:
                    _on_disk = False
                if not _on_disk:
                    print(f"[run_review] passed project_dir={project_dir} does not exist on "
                          f"Codebox — falling back to auto-resolve from builder run")
                    project_dir = ""

            # Preferred lookup: most recent succeeded builder run for this conv.
            # The builder envelope records the actual workspace path (e.g.
            # /root/projects/pong) — this is the one that matters. coding_projects
            # is a less reliable source because openhands_project_id is sometimes
            # blank when the run finished via a path we don't track.
            if not project_dir and conv_id:
                try:
                    _runs = await db.get_runs_by_conversation(conv_id, limit=20)
                    for _r in _runs:
                        if _r.get("role", "").startswith("builder") and _r.get("status") == "succeeded":
                            _env = _r.get("result_envelope") or {}
                            _pd = (_env.get("project_dir") or "").strip()
                            if _pd:
                                project_dir = _pd
                                if not project_id:
                                    project_id = (_env.get("project_id") or "").strip()
                                print(f"[run_review] resolved project_dir={project_dir} from builder run {_r.get('id')}")
                                break
                except Exception as _bre:
                    print(f"[run_review] builder run lookup failed: {_bre}")

            # Secondary fallback: coding_projects.
            if not project_dir and conv_id:
                try:
                    _active = await db.get_coding_project_by_conv(conv_id)
                    if _active:
                        # Active project's working dir on Codebox is /root/projects/{openhands_project_id}
                        _ohp = _active.get("openhands_project_id") or _active.get("id")
                        if _ohp:
                            project_dir = f"/root/projects/{_ohp}"
                            if not project_id:
                                project_id = _ohp
                except Exception as _ape:
                    print(f"[run_review] active project lookup failed: {_ape}")
            if not project_dir:
                return ("ERROR: run_review needs project_dir (e.g. '/root/projects/pong-game') "
                        "or an active project on this conversation. Pass it explicitly: "
                        "run_review(project_dir='/root/projects/<name>')")

            envelope = await reviewer.run_review(http, events, conv_id,
                                                  project_dir=project_dir,
                                                  project_id=project_id,
                                                  conv_model=conv_model)
            try:
                wf = await db.get_latest_coder_workflow(conv_id, project_id=project_id)
                if wf:
                    await db.update_coder_workflow(
                        wf["id"],
                        state="accepting" if (envelope.get("status") == "clean") else "fixing",
                        active_run_id=envelope.get("run_id", ""),
                        artifact_status="not_ready",
                    )
            except Exception as _wfe:
                print(f"[WORKFLOW] review state update failed: {_wfe}")
            # Format the envelope as a tool-result string the chat agent can read
            # without needing to know the JSON schema. Keep it compact.
            status = envelope.get("status", "?")
            summary = envelope.get("summary", "")
            issues = envelope.get("issues") or []
            reviewer_run_id = envelope.get("run_id", "")
            if status == "clean":
                return (f"REVIEW CLEAN. {summary}\n"
                        f"Build: `{envelope.get('build_cmd','')}` exit={envelope.get('build_exit','?')}. "
                        f"Tests: `{envelope.get('test_cmd','')}` exit={envelope.get('test_exit','?')}. "
                        f"reviewer_run_id: {reviewer_run_id}\n"
                        f"REQUIRED NEXT TOOL CALL: run_acceptance_review(reviewer_run_id='{reviewer_run_id}'). "
                        f"Do not call download_project until acceptance is accepted unless the latest user message explicitly asks to ship/download anyway.")
            lines = [f"REVIEW FOUND {len(issues)} ISSUE(S). {summary}",
                     f"Build: `{envelope.get('build_cmd','')}` exit={envelope.get('build_exit','?')}. "
                     f"Tests: `{envelope.get('test_cmd','')}` exit={envelope.get('test_exit','?')}.",
                     f"reviewer_run_id: {reviewer_run_id}",
                     ""]
            for i, iss in enumerate(issues, 1):
                lines.append(f"{i}. [{iss.get('severity','?')}] {iss.get('file','?')}"
                             + (f":{','.join(str(x) for x in iss.get('lines') or [])}" if iss.get('lines') else "")
                             + f" — {iss.get('summary','')}")
                scope = iss.get("suggested_fix_scope") or []
                if scope:
                    lines.append(f"   fix scope: {', '.join(scope[:5])}")
            lines.append("")
            _use_aider_next = False
            if getattr(config, "AIDER_ENABLED", True) and conv_id:
                try:
                    _wf_next = await db.get_latest_coder_workflow(conv_id, project_id=project_id)
                    if not _wf_next:
                        _wf_next = await db.get_latest_coder_workflow(conv_id)
                    _use_aider_next = bool(_wf_next and _wf_next.get("mode") == "fix_uploaded_project")
                except Exception as _wf_next_e:
                    print(f"[run_review] next-fix workflow lookup failed: {_wf_next_e}")
            if _use_aider_next:
                lines.append(
                    f"FIX PROCEDURE: your VERY NEXT tool call MUST be:\n"
                    f"  run_aider_fix(issue_run_id='{reviewer_run_id}', project_dir='{project_dir}', "
                    f"task='Fix the reviewer issues from {reviewer_run_id}')\n"
                    f"This runs Aider from the uploaded project root. AFTER run_aider_fix returns, "
                    f"call run_review again to verify the project is now CLEAN. Do NOT manually "
                    f"read_file / write_file or call run_fixer for uploaded-project reviewer issues."
                )
            else:
                lines.append(
                    f"FIX PROCEDURE: your VERY NEXT tool call MUST be:\n"
                    f"  run_fixer(reviewer_run_id='{reviewer_run_id}')\n"
                    f"This runs the Fixer agent which reads the fix-scope files, generates "
                    f"targeted edits, and writes them back. AFTER run_fixer returns, call "
                    f"run_review again to verify the project is now CLEAN. Do NOT manually "
                    f"read_file / write_file for these issues — that's the v1 antipattern that "
                    f"burns rounds. Hard cap: 3 review/fix cycles per user request."
                )
            return "\n".join(lines)

        elif name == "run_acceptance_review":
            # Coder Bot v2 final gate — static acceptance inspection after a
            # clean build/test/lint reviewer pass.
            from agents import acceptance
            project_dir = (args.get("project_dir") or "").strip()
            reviewer_run_id = (args.get("reviewer_run_id") or "").strip()
            project_id = (args.get("project_id") or "").strip()

            if conv_id:
                try:
                    _runs_pre = await db.get_runs_by_conversation(conv_id, limit=10)
                    _latest_pre = next(
                        (r for r in _runs_pre
                         if r.get("role") in {"fixer", "reviewer", "acceptance"}
                         and (r.get("status") or "").lower() in {"succeeded", "failed", "partial"}),
                        None,
                    )
                    if _latest_pre and _latest_pre.get("role") == "fixer":
                        _env_pre = _latest_pre.get("result_envelope") or {}
                        if not (_env_pre.get("source_role") == "acceptance"
                                and _env_pre.get("docs_only")):
                            return (
                                "ERROR: run_acceptance_review cannot run immediately after "
                                "source/test/manifest fixes. Call run_review first so build, "
                                "tests, and lint are clean for the current files."
                            )
                except Exception as _pre_e:
                    print(f"[run_acceptance_review] preflight failed (non-fatal): {_pre_e}")

            async def _latest_clean_reviewer() -> dict | None:
                if not conv_id:
                    return None
                try:
                    _runs = await db.get_runs_by_conversation(conv_id, limit=30)
                    for _r in _runs:
                        if _r.get("role") != "reviewer":
                            continue
                        # Only the MOST RECENT reviewer counts — an older clean
                        # review may predate later fixes, so don't scan past
                        # the newest one looking for a clean status.
                        _env = _r.get("result_envelope") or {}
                        if (_env.get("status") or "").lower() == "clean":
                            return _r
                        return None
                except Exception as _re:
                    print(f"[run_acceptance_review] reviewer lookup failed: {_re}")
                return None

            reviewer_run = None
            if reviewer_run_id:
                try:
                    reviewer_run = await db.get_run(reviewer_run_id)
                except Exception:
                    reviewer_run = None
                if not reviewer_run or reviewer_run.get("role") != "reviewer":
                    print(f"[run_acceptance_review] passed reviewer_run_id={reviewer_run_id} "
                          "not found/not reviewer — falling back to latest reviewer")
                    reviewer_run_id = ""
                    reviewer_run = None

            if not reviewer_run:
                reviewer_run = await _latest_clean_reviewer()
                if reviewer_run:
                    reviewer_run_id = reviewer_run.get("id", "")

            review_env = (reviewer_run or {}).get("result_envelope") or {}
            if not project_dir:
                project_dir = (review_env.get("project_dir") or "").strip()
            if not project_id:
                project_id = (review_env.get("project_id") or "").strip()

            if not reviewer_run_id or (review_env.get("status") or "").lower() != "clean":
                return (
                    "ERROR: run_acceptance_review requires a clean run_review first. "
                    "Call run_review(project_dir='/root/projects/<name>') and only run "
                    "acceptance after it returns REVIEW CLEAN."
                )
            if not project_dir:
                return "ERROR: run_acceptance_review needs project_dir or a clean reviewer envelope with project_dir."

            _prior_acc = await _prior_acceptance_issues_context(conv_id)
            if _prior_acc:
                print(f"[run_acceptance_review] injecting prior verdict "
                      f"({len(_prior_acc)} chars)")
            envelope = await acceptance.run_acceptance_review(
                http, events, conv_id,
                project_dir=project_dir,
                reviewer_run_id=reviewer_run_id,
                project_id=project_id,
                conv_model=conv_model,
                prior_acceptance_context=_prior_acc,
            )
            try:
                wf = await db.get_latest_coder_workflow(conv_id, project_id=project_id)
                if wf:
                    _ast = (envelope.get("status") or "").lower()
                    await db.update_coder_workflow(
                        wf["id"],
                        state="accepted" if _ast == "accepted" else "fixing",
                        active_run_id=envelope.get("run_id", ""),
                        artifact_status="accepted" if _ast == "accepted" else "not_ready",
                    )
            except Exception as _wfe:
                print(f"[WORKFLOW] acceptance state update failed: {_wfe}")

            a_status = envelope.get("status", "?")
            summary = envelope.get("summary", "")
            issues = envelope.get("issues") or []
            acceptance_run_id = envelope.get("run_id", "")
            if a_status == "accepted":
                return (
                    f"ACCEPTANCE ACCEPTED. {summary}\n"
                    f"acceptance_run_id: {acceptance_run_id}\n"
                    "Project is ready — package and deliver with download_project."
                )

            lines = [
                f"ACCEPTANCE FOUND {len(issues)} ISSUE(S). {summary}",
                f"acceptance_run_id: {acceptance_run_id}",
                "",
            ]
            for i, iss in enumerate(issues, 1):
                lines.append(f"{i}. [{iss.get('category','?')}] {iss.get('file','?')}"
                             + (f":{','.join(str(x) for x in iss.get('lines') or [])}" if iss.get('lines') else "")
                             + f" — {iss.get('summary','')}")
                scope = iss.get("suggested_fix_scope") or []
                if scope:
                    lines.append(f"   fix scope: {', '.join(scope[:5])}")
            lines.append("")
            lines.append(
                f"FIX PROCEDURE: your VERY NEXT tool call MUST be:\n"
                f"  run_fixer(reviewer_run_id='{acceptance_run_id}')\n"
                f"If the Fixer touches only docs, call run_acceptance_review again. "
                f"If it touches source, tests, or manifests, call run_review first, "
                f"then run_acceptance_review again. Do not call download_project until "
                f"acceptance is accepted unless the latest user message explicitly asks "
                f"to ship/download anyway; in that case disclose these issues and package "
                f"the current state."
            )
            return "\n".join(lines)

        elif name == "run_fixer":
            # Coder Bot v2 Phase 2 — apply targeted edits for issues from a prior
            # run_review. Stateless agent: reads scope files, generates JSON edits
            # via the coder model, writes them back. Does not re-run the build —
            # the chat agent calls run_review again afterwards to verify.
            from agents import fixer
            reviewer_run_id = (args.get("reviewer_run_id") or "").strip()

            # Helper: pull the most recent actionable reviewer/acceptance run id for this conv.
            async def _latest_actionable_run_id() -> str:
                if not conv_id:
                    return ""
                try:
                    _runs = await db.get_runs_by_conversation(conv_id, limit=20)
                    for _r in _runs:
                        if _r.get("role") not in {"reviewer", "acceptance"}:
                            continue
                        _env = _r.get("result_envelope") or {}
                        if (_env.get("status") or "").lower() in {"issues", "error"}:
                            return _r["id"]
                except Exception as _re:
                    print(f"[run_fixer] actionable run lookup failed: {_re}")
                return ""

            # If not given, resolve from runs.
            if not reviewer_run_id:
                reviewer_run_id = await _latest_actionable_run_id()
                if reviewer_run_id:
                    print(f"[run_fixer] resolved reviewer_run_id={reviewer_run_id} (omitted by model)")
            else:
                # Validate that the id passed by the model actually exists.
                # qwen3-coder occasionally fabricates run-ids instead of
                # copying from the prior run_review tool result; in that
                # case fall back to the most recent reviewer run rather
                # than failing the whole fix step.
                try:
                    _exists = await db.get_run(reviewer_run_id)
                except Exception:
                    _exists = None
                if not _exists:
                    print(f"[run_fixer] passed reviewer_run_id={reviewer_run_id} not found — "
                          f"falling back to most recent actionable review/acceptance run")
                    _fallback = await _latest_actionable_run_id()
                    if _fallback:
                        reviewer_run_id = _fallback
                        print(f"[run_fixer] using {reviewer_run_id} instead")

            if not reviewer_run_id:
                return ("ERROR: run_fixer needs reviewer_run_id (the run_id from a prior "
                        "run_review or run_acceptance_review call with issues), and no "
                        "actionable run was found on this conversation. Call run_review first.")

            await events.emit(conv_id, "tool_start", {
                "tool": "run_fixer", "icon": "wrench",
                "status": f"🛠 Starting Fixer for review {reviewer_run_id[:14]}…",
            })

            # If a recent deep_research call happened on this conv, hand its
            # report to the Fixer as supporting context. Cached at the call
            # site (see _stash_research_result), so this stays in-process and
            # doesn't break the Fixer's network-free invariant. Stale entries
            # (>10 min) are dropped on read by _get_recent_research.
            _research_for_fixer = ""
            try:
                _r_entry = _get_recent_research(conv_id)
                if _r_entry:
                    _research_for_fixer = _r_entry.get("report", "") or ""
                    if _research_for_fixer:
                        print(f"[run_fixer] injecting research_context "
                              f"({len(_research_for_fixer)} chars, "
                              f"topic={(_r_entry.get('topic') or '')[:60]!r})")
            except Exception as _re:
                print(f"[run_fixer] research lookup failed (non-fatal): {_re}")

            # Prior fix attempts this turn — so the Fixer doesn't repeat an
            # edit that already failed. Gathered orchestrator-side; fixer.py
            # stays network-free and reads no conversation state itself.
            _attempts_for_fixer = await _prior_fix_attempts_context(conv_id)
            if _attempts_for_fixer:
                print(f"[run_fixer] injecting attempt history "
                      f"({len(_attempts_for_fixer)} chars)")

            envelope = await fixer.run_fixer(http, events, conv_id,
                                              reviewer_run_id=reviewer_run_id,
                                              research_context=_research_for_fixer,
                                              attempt_history=_attempts_for_fixer)

            f_status = envelope.get("status", "?")
            files = envelope.get("files_touched") or []
            errors = envelope.get("errors") or []
            n_issues = envelope.get("issues_addressed", 0)

            if f_status == "applied":
                lines = [
                    f"FIXER APPLIED EDITS to {len(files)} file(s) across {n_issues} issue(s).",
                    "Files touched:",
                ] + [f"  - {f}" for f in files[:10]]
                if errors:
                    lines.append("")
                    lines.append("Non-fatal errors:")
                    lines += [f"  - {e}" for e in errors[:5]]
                lines.append("")
                if envelope.get("source_role") == "acceptance" and envelope.get("docs_only"):
                    lines.append("REQUIRED NEXT TOOL CALL: run_acceptance_review (docs-only fix; build review may be skipped).")
                else:
                    lines.append("REQUIRED NEXT TOOL CALL: run_review (no args needed — uses the "
                                 "active project). It will re-run the build/tests and tell you "
                                 "whether the fixes worked before acceptance runs again.")
                return "\n".join(lines) + await _fix_budget_note(conv_id, envelope.get("source_role"))
            elif f_status == "partial":
                lines = [
                    f"FIXER PARTIAL: applied {len(files)} edit(s) but {len(errors)} error(s) occurred.",
                    "Files touched:",
                ] + [f"  - {f}" for f in files[:10]]
                lines.append("")
                lines.append("Errors:")
                lines += [f"  - {e}" for e in errors[:5]]
                lines.append("")
                if envelope.get("source_role") == "acceptance" and envelope.get("docs_only"):
                    lines.append("REQUIRED NEXT TOOL CALL: run_acceptance_review to re-check docs-only acceptance fixes.")
                else:
                    lines.append("REQUIRED NEXT TOOL CALL: run_review to see the current state of the project.")
                return "\n".join(lines) + await _fix_budget_note(conv_id, envelope.get("source_role"))
            elif f_status == "skipped":
                return f"FIXER SKIPPED: {envelope.get('summary', 'no issues to fix')}."
            else:
                err_str = "; ".join(errors[:5]) or envelope.get("summary", "no edits applied")
                return (f"FIXER FAILED: no edits applied. Reasons: {err_str}\n\n"
                        f"You may need to fix manually with read_file + write_file (one round per "
                        f"file), then call run_review to verify.")

        elif name == "ask_project":
            # Coder Bot v2 Phase 4 — read-only Q&A over an existing project.
            # Greps the tree for keywords from the question, reads matching
            # snippets, asks the model to compose a grounded answer with
            # file:line citations. Detects change requests and flags them so
            # the chat agent can route follow-ups appropriately.
            from agents import project_qa
            question = (args.get("question") or "").strip()
            project_dir = (args.get("project_dir") or "").strip()
            if not question:
                return "ERROR: ask_project requires a `question` argument."

            language = ""

            async def _resolve_project_dir() -> tuple[str, str]:
                """Auto-resolve project_dir + language from runs/coding_projects."""
                _pd = ""
                _lang = ""
                if not conv_id:
                    return _pd, _lang
                try:
                    _runs = await db.get_runs_by_conversation(conv_id, limit=20)
                    for _r in _runs:
                        if _r.get("role", "").startswith("builder") and _r.get("status") == "succeeded":
                            _env = _r.get("result_envelope") or {}
                            _candidate = (_env.get("project_dir") or "").strip()
                            if _candidate:
                                _pd = _candidate
                                _lang = _env.get("language", "") or _lang
                                print(f"[ask_project] resolved project_dir={_pd} from builder run {_r.get('id')}")
                                return _pd, _lang
                except Exception as _bre:
                    print(f"[ask_project] builder run lookup failed: {_bre}")
                # coding_projects fallback
                try:
                    _active = await db.get_coding_project_by_conv(conv_id)
                    if _active:
                        _ohp = _active.get("openhands_project_id") or _active.get("id")
                        if _ohp:
                            _pd = f"/root/projects/{_ohp}"
                            _lang = _active.get("language") or _lang
                except Exception:
                    pass
                return _pd, _lang

            async def _project_dir_exists(path: str) -> bool:
                """Best-effort check that path is a real directory on Codebox."""
                if not path:
                    return False
                try:
                    _r = await http.post(
                        f"{config.CODEBOX_URL}/command",
                        json={"command": f"test -d {shlex.quote(path)} && echo OK || echo NO",
                              "timeout": 5},
                        timeout=10,
                    )
                    if _r.status_code == 200:
                        return "OK" in (_r.json().get("stdout") or "")
                except Exception:
                    pass
                return False

            if project_dir:
                # Validate that the passed path actually exists. qwen-style
                # models occasionally fabricate names like "/root/projects/
                # todo-api" by guessing from the user's description instead
                # of looking up the real path. If that happens, fall back to
                # auto-resolution from the latest builder run.
                if not await _project_dir_exists(project_dir):
                    print(f"[ask_project] passed project_dir={project_dir} does not exist — "
                          f"falling back to auto-resolve")
                    _real_pd, _real_lang = await _resolve_project_dir()
                    if _real_pd:
                        project_dir = _real_pd
                        language = _real_lang or language
                        print(f"[ask_project] using {project_dir} instead")
            else:
                project_dir, language = await _resolve_project_dir()

            if not project_dir:
                return ("ERROR: ask_project needs project_dir, and no active project was "
                        "found on this conversation. Pass it explicitly: "
                        "ask_project(question='...', project_dir='/root/projects/<name>')")

            await events.emit(conv_id, "tool_start", {
                "tool": "ask_project", "icon": "help-circle",
                "status": f"❓ Investigating: {question[:80]}",
            })

            envelope = await project_qa.run_project_qa(
                http, events, conv_id,
                project_dir=project_dir, question=question,
                language=language,
                conv_model=conv_model,
            )

            answer = envelope.get("answer", "")
            files = envelope.get("files_examined") or []
            looks_change = envelope.get("looks_like_change_request", False)

            if envelope.get("status") != "ok" or not answer:
                return (f"ASK_PROJECT FAILED: {envelope.get('summary', 'no answer produced')}.\n\n"
                        f"You can fall back to read_file + search_files manually if needed.")

            lines = [f"ANSWER (from {len(files)} file{'s' if len(files) != 1 else ''} examined):",
                     "",
                     answer]
            if looks_change:
                lines += [
                    "",
                    "**NOTE:** This question was flagged as a likely CHANGE REQUEST, not just a "
                    "question. If the user wants you to actually make the change, your next tool "
                    "call should be generate_code (for substantial changes) or read_file + "
                    "write_file (for 1-2 file edits) — NOT another ask_project.",
                ]
            else:
                # Make the terminal contract explicit. The model has a strong
                # bias to keep calling tools after every result; for Q&A this
                # is wrong — the user asked a question, the answer is above,
                # the next output should be a plain-text response.
                lines += [
                    "",
                    "**TERMINAL — STOP CALLING TOOLS.** This was a question, not a build "
                    "request. Your VERY NEXT output MUST be plain text relaying the answer "
                    "above to the user. Do NOT call run_review, run_fixer, generate_code, "
                    "read_file, write_file, run_shell, download_project, or any other tool. "
                    "If the user follows up with a new question, call ask_project again. "
                    "If they ask for a change, call generate_code or write_file then. "
                    "But for THIS response, just answer the user.",
                ]
            return "\n".join(lines)

        elif name == "plan_project":
            task = args.get("task", "")
            language = args.get("language", "python")
            constraints = args.get("constraints", "")
            if not task:
                return "ERROR: task is required"

            # ─── v2: route through structured Architect agent ─────────
            # The Architect produces a JSON manifest the Builder/Reviewer
            # consume directly, instead of the v1 prose plan that every
            # downstream agent has to re-parse from markdown. v1 personas
            # keep the existing prose path below.
            _is_v2_for_plan = bool(conv_id) and await _is_v2_persona(conv_id)

            if _is_v2_for_plan:
                from agents import architect
                await events.emit(conv_id, "tool_start", {
                    "tool": "plan_project", "icon": "activity",
                    "status": "🧠 Architect planning structured manifest...",
                })
                # KB chunks for the planning model: filter by language hints
                # using the existing rag plumbing. Best effort — if RAG isn't
                # configured or fails, the architect just runs without KB.
                _kb_chunks = []
                if kb_ids:
                    try:
                        import rag
                        _hints = _kb_filename_hints_for_language(language, task)
                        _kb_chunks = await rag.query(
                            kb_ids, query_text=task[:500], top_k=3,
                            prefer_filename_hints=_hints,
                        )
                        # Visibility for "is the KB actually being used" question.
                        # Logs the filenames + scores so you can see at a glance
                        # whether retrieval found anything relevant.
                        if _kb_chunks:
                            _kb_summary = ", ".join(
                                f"{c.get('filename','?')}({c.get('score',0):.2f})"
                                for c in _kb_chunks
                            )
                            print(f"[plan_project] KB chunks for architect: "
                                  f"kb_ids={kb_ids} hints={_hints} → {_kb_summary}",
                                  flush=True)
                        else:
                            print(f"[plan_project] No KB chunks returned "
                                  f"(kb_ids={kb_ids}, hints={_hints})", flush=True)
                    except Exception as _rge:
                        print(f"[plan_project] RAG query failed (non-fatal): {_rge}")
                plan = await architect.run_architect(
                    http, events, conv_id,
                    task=task, language_hint=language,
                    kb_chunks=_kb_chunks,
                    conv_model=conv_model,
                )
                return architect.format_plan_for_chat(plan)

            # ─── v1 path: existing prose plan_project ─────────────────
            planning_model = config.PLANNING_MODEL or conv_model or config.DEFAULT_MODEL
            await events.emit(conv_id, "tool_start", {"tool": "plan_project", "icon": "activity", "status": f"🧠 Planning architecture with {planning_model}..."})
            plan_prompt = f"""You are a senior software architect. Design a complete implementation plan for this project.

## Requirements
{task}

## Language
{language}

## Constraints
{constraints if constraints else "None specified"}

## Your plan MUST include:
1. **File Tree** — every file to create, with a one-line description of its purpose
2. **Dependencies** — packages/libraries needed with install commands
3. **Component Design** — how components interact (data flow, API contracts, imports)
4. **Build Order** — which files to create first (dependencies before dependents)
5. **Key Design Decisions** — why this architecture over alternatives
6. **Testing Strategy** — what to test and how

Be specific. Name actual files, functions, classes, and routes. This plan will be handed to a coding agent for implementation."""
            try:
                r = await http.post(
                    f"{config.OLLAMA_URL}/api/chat",
                    json={"model": planning_model, "messages": [{"role": "user", "content": plan_prompt}], "stream": False, "options": {"temperature": 0.3, "num_ctx": config.DEFAULT_NUM_CTX}},
                    timeout=180,
                )
                if r.status_code == 200:
                    data = r.json()
                    plan = data.get("message", {}).get("content", "")
                    if plan:
                        await events.emit(conv_id, "tool_end", {
                            "tool": "plan_project",
                            "icon": "activity",
                            "status": "🧠 Architecture plan ready",
                            "detail": json.dumps({"plan": plan[:12000], "language": language, "task": task[:200]}),
                        })
                        # Save plan to project memory
                        try:
                            proj_id = f"proj-{uuid.uuid4().hex[:12]}"
                            await db.upsert_coding_project(
                                project_id=proj_id, name=task[:60].strip().replace("\n", " "),
                                conversation_id=conv_id, description=task[:500],
                                language=language, last_plan=plan[:8000],
                            )
                        except Exception as proj_e:
                            print(f"[PLAN] Failed to save project: {proj_e}")
                        return f"## Architecture Plan\n\n{plan}\n\n---\n*Now implement this plan step by step using write_file, run_shell, and other tools.*"
                    else:
                        await events.emit(conv_id, "tool_end", {"tool": "plan_project", "icon": "activity", "status": "⚠ Planning returned empty"})
                        return "ERROR: Planning model returned empty response"
                else:
                    await events.emit(conv_id, "tool_end", {"tool": "plan_project", "icon": "activity", "status": f"⚠ Planning failed ({r.status_code})"})
                    return f"ERROR: Planning model returned HTTP {r.status_code}: {r.text[:200]}"
            except Exception as e:
                await events.emit(conv_id, "tool_end", {"tool": "plan_project", "icon": "activity", "status": "⚠ Planning failed"})
                return f"ERROR: Planning call failed: {e}"

        elif name == "deep_research":
            topic = args.get("topic", "")
            depth = args.get("depth", 3)
            if isinstance(depth, str):
                depth = {"quick": 1, "standard": 3, "deep": 5}.get(depth, 3)
            depth = max(1, min(5, depth))
            focus = args.get("focus", "")
            mode = args.get("mode", "research")
            topic_b = args.get("topic_b", "")

            # Pre-query KB for existing knowledge on this topic
            kb_prior = ""
            if kb_ids and topic:
                try:
                    import rag
                    chunks = await rag.query(kb_ids, topic, top_k=4)
                    if chunks:
                        kb_prior = rag.format_context(chunks, max_chars=3000)
                        print(f"[RESEARCH RAG] Pre-loaded {len(chunks)} KB chunks for deep_research: {topic[:60]}")
                except Exception as e:
                    print(f"[RESEARCH RAG] KB pre-query failed: {e}")

            depth_labels = {1: "Quick", 2: "Overview", 3: "Deep dive", 4: "Comprehensive", 5: "Exhaustive"}
            label = depth_labels.get(depth, f"D{depth}")

            if mode == "compare" and topic_b:
                status_msg = f"Comparing: {topic[:30]} vs {topic_b[:30]}"
            elif mode == "quick":
                status_msg = f"Quick search: {topic[:60]}"
            else:
                status_msg = f"{label}: {topic[:50]}..."

            await events.emit(conv_id, "tool_start", {
                "tool": "deep_research", "icon": "search", "status": status_msg,
            })

            try:
                result = await run_deep_research(http, config.OLLAMA_URL, config.DEFAULT_MODEL, events, topic, depth, focus, mode, topic_b, conv_id, kb_context=kb_prior)
            except Exception as e:
                await events.emit(conv_id, "tool_end", {"tool": "deep_research", "icon": "search", "status": f"Failed: {str(e)}"})
                return f"**Deep research failed:** {str(e)}"

            report = result.get("report", "")
            sources = result.get("sources", [])
            sc = result.get("source_count", 0)
            ss = result.get("total_searches", 0)
            pr = result.get("pages_read", 0)
            tm = result.get("elapsed", 0)
            entities = result.get("key_entities", [])

            await events.emit(conv_id, "tool_end", {
                "tool": "deep_research", "icon": "search",
                "status": f"{sc} sources, {ss} searches, {pr} pages ({tm:.0f}s)",
                "detail": json.dumps({"topic": topic, "depth": depth, "source_count": sc, "pages_read": pr, "key_entities": entities[:8]}),
            })
            if sources:
                await events.emit(conv_id, "search_results", {
                    "query": topic,
                    "results": [{"title": s["title"], "url": s["url"],
                                 "thumbnail": s.get("thumbnail", ""), "type": s.get("type", "web"),
                                 "snippet": s.get("snippet", "")} for s in sources[:12]]
                })

            parts = [f"# Agent Research: {topic}\n"]
            parts.append(f"*{sc} sources, {ss} searches, {pr} pages read ({tm:.0f}s)*\n")
            if entities:
                parts.append(f"**Key entities:** {', '.join(entities[:10])}\n")
            parts.append(report)
            if sources:
                parts.append("\n\n---\n## Sources\n")
                for s in sources[:20]:
                    parts.append(f"[{s.get('index','?')}] [{s.get('title','?')}]({s.get('url','')})")
            _full_report = "\n".join(parts)
            # Cache the result so the Fixer dispatcher can inject it into
            # the next run_fixer call without making the Fixer itself read
            # the conversation. Keeps fixer.py network-free.
            _stash_research_result(conv_id, topic, _full_report)
            return _full_report

        elif name == "conspiracy_research":
            topic = args.get("topic", "")
            angle = args.get("angle", "evidence")
            depth = max(3, min(5, int(args.get("depth", 4))))

            # Pre-query KB for existing knowledge on this topic
            kb_prior = ""
            if kb_ids and topic:
                try:
                    import rag
                    chunks = await rag.query(kb_ids, topic, top_k=4)
                    if chunks:
                        kb_prior = rag.format_context(chunks, max_chars=3000)
                        print(f"[RESEARCH RAG] Pre-loaded {len(chunks)} KB chunks for conspiracy_research: {topic[:60]}")
                except Exception as e:
                    print(f"[RESEARCH RAG] KB pre-query failed: {e}")

            return await run_conspiracy_research(http, config.OLLAMA_URL, config.DEFAULT_MODEL, config.SEARXNG_URL, events, topic, angle, depth, conv_id, kb_context=kb_prior)

        elif name == "generate_code":
            # Models sometimes use wrong arg names (description/code instead of task)
            task = args.get("task", "") or args.get("description", "") or args.get("prompt", "")
            language = args.get("language", "python")
            context = args.get("context", "")
            # If model stuffed actual code into args, append it as context
            if not task and args.get("code"):
                task = "Review, fix, and complete this code"
                context = (context + "\n\n" + args["code"]).strip()
            elif args.get("code") and task:
                context = (context + "\n\nReference code:\n" + args["code"]).strip()
            # Per-agent override (config.BUILDER_MODEL) wins if pinned; else
            # umbrella CODER_MODEL, then chat model, then default.
            coder_model = config.BUILDER_MODEL or config.CODER_MODEL or conv_model or config.DEFAULT_MODEL

            # Inject KB context so OpenHands agent has access to uploaded documentation.
            # Bias retrieval toward filenames matching the requested language/framework so a
            # Java task pulls java_/javafx_/spring_ chunks instead of competing for the same
            # top_k slots with unrelated languages (swift, ruby, etc.).
            if kb_ids and task:
                try:
                    import rag
                    chunks = await rag.query(
                        kb_ids, task, top_k=6,
                        prefer_filename_hints=_kb_filename_hints_for_language(language, task),
                    )
                    if chunks:
                        kb_prior = rag.format_context(chunks, max_chars=4500)
                        kb_section = (
                            "\n\n--- Knowledge Base (uploaded reference docs) ---\n"
                            + kb_prior
                        )
                        context = (context + kb_section) if context else kb_section.strip()
                        _matched = sum(1 for c in chunks
                                       if any(h in (c.get("filename") or "").lower()
                                              for h in (_kb_filename_hints_for_language(language, task) or [])))
                        print(f"[CODEGEN RAG] Pre-loaded {len(chunks)} KB chunks "
                              f"({_matched} lang-matched) for generate_code: {task[:60]}")
                except Exception as e:
                    print(f"[CODEGEN RAG] KB pre-query failed: {e}")

            # Query code memory for similar past projects
            try:
                import rag as _rag
                _code_matches = await _rag.query_code_memory(task, top_k=3, language=language)
                if _code_matches:
                    _code_ctx = "\n\n--- Similar Past Code (from code memory) ---\n"
                    for _cm in _code_matches:
                        if _cm.get("score", 0) > 0.3:
                            _code_ctx += f"\n# From: {_cm['filename']} (task: {_cm.get('task', '')[:80]})\n{_cm['text'][:1500]}\n"
                    if len(_code_ctx) > 60:
                        context = (context + _code_ctx) if context else _code_ctx.strip()
                        print(f"[CODEGEN] Injected {len(_code_matches)} code memory matches")
            except Exception as _cm_e:
                print(f"[CODEGEN] Code memory query failed (non-fatal): {_cm_e}")

            # Pre-scan for library/API mentions and auto-research
            _API_KEYWORDS = ["api", "sdk", "library", "framework", "package", "module"]
            _task_lower = task.lower()
            if any(kw in _task_lower for kw in _API_KEYWORDS) or re.search(r'(?:using|with)\s+\w+(?:\.\w+)*\s+(?:api|sdk|library)', _task_lower):
                try:
                    import urllib.parse
                    _lib_query = f"{task[:100]} {language} documentation tutorial"
                    _params = urllib.parse.urlencode({"q": _lib_query, "format": "json", "count": 5})
                    _sr = await http.get(f"{config.SEARXNG_URL}/search?{_params}", timeout=10)
                    if _sr.status_code == 200:
                        _results = _sr.json().get("results", [])[:3]
                        if _results:
                            _api_snippets = []
                            for _item in _results:
                                _api_snippets.append(f"- {_item.get('title', '')}: {_item.get('content', '')[:200]}")
                            _api_context = "\n\n--- API/Library Reference (auto-researched) ---\n" + "\n".join(_api_snippets)
                            context = (context + _api_context) if context else _api_context.strip()
                            print(f"[CODEGEN] Pre-researched {len(_results)} API references for: {task[:60]}")
                except Exception as _re:
                    print(f"[CODEGEN] API pre-research failed (non-fatal): {_re}")

            if not getattr(config, "OPENHANDS_ENABLED", True):
                return "ERROR: OpenHands is disabled in settings. Enable it or use write_file + run_shell directly."

            openhands_url = config.OPENHANDS_URL
            max_rounds = getattr(config, "OPENHANDS_MAX_ROUNDS", 20)
            num_ctx = getattr(config, "OPENHANDS_NUM_CTX", 16384)

            # Health check with retry (3 attempts, 1s between)
            _oh_healthy = False
            _oh_last_err = None
            for _attempt in range(3):
                try:
                    health = await http.get(f"{openhands_url}/health", timeout=3)
                    if health.status_code == 200:
                        _oh_healthy = True
                        break
                    _oh_last_err = f"Health check HTTP {health.status_code}"
                except Exception as oh_e:
                    _oh_last_err = str(oh_e)
                if _attempt < 2:
                    print(f"[CODEGEN:OH] Health check attempt {_attempt + 1} failed: {_oh_last_err}, retrying...")
                    await asyncio.sleep(1)
            if not _oh_healthy:
                await events.emit(conv_id, "tool_end", {
                    "tool": "generate_code", "icon": "code",
                    "status": f"OpenHands unavailable: {_oh_last_err}",
                })
                return (
                    f"ERROR: OpenHands worker is unavailable after 3 attempts ({_oh_last_err}). "
                    "You can still write code directly using write_file + run_shell to test it."
                )

            # Phase 0: durable run row for this generate_code invocation. The
            # actual role (builder.scaffold / continue / feature) gets set
            # later, after profile detection — but we reserve the run_id
            # immediately so the rest of the dispatch can reference it.
            # Phase 5: role is filled in once we know the profile.
            _run_id = f"run-{uuid.uuid4().hex[:12]}"
            _run_row_created = False

            async def _finalize_run(status: str, envelope: dict):
                """Mark the run terminal with the given status + envelope. Safe to no-op.

                Also stops the cancel-watcher task and removes the cancel
                event from the registry — every terminal path goes through
                here, so this is the right place to release those resources.
                """
                if not _run_id:
                    return
                try:
                    await db.update_run(_run_id, status=status, result_envelope=envelope, ended=True)
                except Exception as _fre:
                    print(f"[RUN] finalize failed (non-fatal): {_fre}")
                # Stop the cancel watcher (if it's still waiting) and drop
                # the registry entry. Defined later in this block but bound
                # via closure so we reference them by nonlocal lookup.
                _watcher = _cancel_watcher_ref[0] if _cancel_watcher_ref else None
                if _watcher and not _watcher.done():
                    _watcher.cancel()
                    try:
                        await _watcher
                    except (asyncio.CancelledError, BaseException):
                        pass
                cancel_registry.cleanup(_run_id)

            # Container so _finalize_run (defined above) can see the watcher
            # task even though it's created several blocks below.
            _cancel_watcher_ref: list = [None]

            await events.emit(conv_id, "tool_start", {
                "tool": "generate_code", "icon": "wand",
                "status": f"🤖 OpenHands agent building {language} project...",
                "run_id": _run_id,
            })
            print(f"[CODEGEN:OH] model={coder_model} lang={language} num_ctx={num_ctx} task={task[:100]!r} run_id={_run_id}")

            # Auto-resolve project_id: if model didn't pass one, check for an
            # active uploaded project on this conversation so OpenHands works
            # inside the user's uploaded project directory.
            #
            # Phase 5 — combined active-project + builder-profile detection.
            # The authoritative source for "is there an existing project" is
            # the runs table — specifically, the most recent succeeded
            # builder.* run, whose envelope carries the real project_dir on
            # disk. coding_projects.openhands_project_id is unreliable
            # (sometimes empty even when the project exists; a fresh row may
            # be created by plan_project before the build runs).
            #
            # Profile mapping:
            #   scaffold  — no prior builder run for this conv (fresh build)
            #   continue  — most recent builder ran partial / stuck
            #               (manifest_missing has unfinished files)
            #   feature   — most recent builder succeeded
            #               (user is asking for an additive change)
            _oh_project_id = args.get("project_id", "")
            _has_active_project = bool(_oh_project_id)
            _builder_profile = "scaffold"
            _profile_continue_missing: list[str] = []

            if conv_id:
                try:
                    _runs_for_profile = await db.get_runs_by_conversation(conv_id, limit=30)
                    # Find the most recent builder run with a MEANINGFUL state.
                    # Skip running/queued (in-flight, has no envelope yet),
                    # cancelled (operator killed before it produced anything),
                    # and failed (no usable project state to amend). What we
                    # want is the most recent builder that actually wrote
                    # files we can build on.
                    _MEANINGFUL = {"succeeded", "partial", "stuck"}
                    _last_builder = next(
                        (r for r in _runs_for_profile
                         if r.get("role", "").startswith("builder")
                         and (r.get("status") or "").lower() in _MEANINGFUL),
                        None,
                    )
                    if _last_builder:
                        _lb_env = _last_builder.get("result_envelope") or {}
                        _lb_status = (_last_builder.get("status") or "").lower()
                        _lb_pdir = (_lb_env.get("project_dir") or "").strip()
                        _lb_pid = (_lb_env.get("project_id") or "").strip()
                        if not _oh_project_id:
                            if _lb_pid:
                                _oh_project_id = _lb_pid
                            elif _lb_pdir.startswith("/root/projects/"):
                                _oh_project_id = _lb_pdir.split("/")[-1]
                        if _oh_project_id or _lb_pdir:
                            _has_active_project = True
                            print(f"[CODEGEN:OH] Auto-attached active project "
                                  f"{_oh_project_id or _lb_pdir} (from builder run "
                                  f"{_last_builder.get('id')} status={_lb_status})")
                        if _lb_status in ("partial", "stuck"):
                            _builder_profile = "continue"
                            _profile_continue_missing = (
                                _lb_env.get("manifest_missing") or []
                            )
                        elif _lb_status == "succeeded":
                            _builder_profile = "feature"
                except Exception as _bp_e:
                    print(f"[CODEGEN:OH] Profile detection failed (non-fatal): {_bp_e}")

            # Fallback to coding_projects only if the runs table didn't have
            # a builder run yet (truly fresh upload + first generate_code).
            if not _has_active_project and conv_id:
                try:
                    _active = await db.get_coding_project_by_conv(conv_id)
                    if _active:
                        _ohp = _active.get("openhands_project_id") or _active.get("id")
                        if _ohp:
                            _oh_project_id = _ohp
                            _has_active_project = True
                            # Promote profile to "feature": an active project
                            # via coding_projects means files exist on disk
                            # (uploaded by the user, or written by write_file
                            # without a generate_code run). Calling generate_code
                            # against it is amending an existing tree, NOT
                            # scaffolding a new one. Scaffold-mode would
                            # clobber the user's code (Bug 8).
                            _builder_profile = "feature"
                            print(f"[CODEGEN:OH] Active project {_oh_project_id} via coding_projects fallback (profile=feature)")
                except Exception as _ap_e:
                    print(f"[CODEGEN:OH] coding_projects lookup failed (non-fatal): {_ap_e}")

            print(f"[CODEGEN:OH] Profile: {_builder_profile} "
                  f"(active_project={_has_active_project}, project_id={_oh_project_id or '?'}"
                  + (f", missing={len(_profile_continue_missing)}" if _builder_profile == 'continue' else '')
                  + ")")

            # Phase 5.3 — feature-profile context prep. For an additive change
            # to an existing project, the Builder should not re-discover the
            # file tree by walking it manually. Instead, package the tree +
            # 1-2 directly relevant files into the context now so the Builder
            # can read them out of its prompt and edit minimally.
            if _builder_profile == "feature" and _oh_project_id:
                try:
                    from agents import project_qa as _pqa
                    _proj_dir = f"/root/projects/{_oh_project_id}"
                    _existing_files = await _pqa._list_files(http, _proj_dir)
                    if _existing_files:
                        _ctx_lines = [
                            "\n--- EXISTING PROJECT (do not rewrite working files) ---",
                            f"Project root: {_proj_dir}",
                            "File tree (truncated to 60 paths):",
                        ]
                        for _f in _existing_files[:60]:
                            _ctx_lines.append(f"  {_f}")
                        if len(_existing_files) > 60:
                            _ctx_lines.append(f"  ... ({len(_existing_files) - 60} more files)")

                        # If the user's task mentions specific filenames,
                        # inline a short head of those files. Reuses the QA
                        # filename-targeting helper so the heuristic is shared.
                        _targets = _pqa._extract_filename_targets(task)
                        _matched = await _pqa._resolve_filename_targets(_existing_files, _targets) if _targets else []
                        if _matched:
                            _ctx_lines.append("")
                            _ctx_lines.append(f"Files mentioned in the task ({len(_matched)}):")
                            for _f in _matched[:3]:
                                _head = await _pqa._read_file_full(http, _proj_dir, _f, max_bytes=8000)
                                if _head:
                                    _ctx_lines.append(f"\n### {_f}\n```\n{_head[:6000]}\n```")
                        _ctx_lines.append(
                            "\nRead these files BEFORE editing. Make the smallest "
                            "edit that satisfies the task. Do NOT rewrite the whole "
                            "project."
                        )
                        _feature_ctx = "\n".join(_ctx_lines)
                        context = (context + _feature_ctx) if context else _feature_ctx.strip()
                        print(f"[CODEGEN:OH] Feature context: {len(_existing_files)} files in tree, "
                              f"{len(_matched)} inlined")
                except Exception as _fce:
                    print(f"[CODEGEN:OH] Feature context prep failed (non-fatal): {_fce}")

            # Now that we know the profile, create the durable run row with
            # the matching role so the frontend RunCard renders the right
            # label (builder.scaffold / builder.continue / builder.feature).
            try:
                await db.create_run(_run_id, conv_id,
                                    role=f"builder.{_builder_profile}",
                                    project_id=_oh_project_id or "",
                                    status="running")
                _run_row_created = True
            except Exception as _re:
                print(f"[RUN] create_run failed (non-fatal): {_re}")
                _run_id = ""

            # Phase 3 — inject the most recent Architect manifest into the
            # context so OpenHands follows the structured plan (file list,
            # build/test commands, success criteria) instead of re-deriving
            # them from prose. Best effort: only fires if v2 plan_project
            # was called this conv and produced a successful architect run.
            if conv_id:
                try:
                    _runs_for_arch = await db.get_runs_by_conversation(conv_id, limit=20)
                    _arch_run = next(
                        (r for r in _runs_for_arch
                         if r.get("role") == "architect" and r.get("status") == "succeeded"),
                        None,
                    )
                    if _arch_run:
                        _arch_env = _arch_run.get("result_envelope") or {}
                        _manifest = _arch_env.get("manifest") or []
                        _success = _arch_env.get("success_criteria") or []
                        _build_cmd = _arch_env.get("build_cmd", "")
                        _test_cmd = _arch_env.get("test_cmd", "")
                        if _manifest:
                            _arch_section = ["\n\n--- Architect Manifest (FOLLOW THIS PLAN) ---"]
                            _arch_section.append(
                                f"Project: {_arch_env.get('project_id','?')} "
                                f"({_arch_env.get('language','?')}, "
                                f"build_system={_arch_env.get('build_system','?')})"
                            )
                            if _build_cmd:
                                _arch_section.append(f"Build command: {_build_cmd}")
                            if _test_cmd:
                                _arch_section.append(f"Test command: {_test_cmd}")
                            _arch_section.append("")
                            _arch_section.append(f"Files to create ({len(_manifest)}):")
                            for _m in _manifest[:30]:
                                _arch_section.append(
                                    f"  - {_m.get('path','?')} — {_m.get('purpose','')}"
                                )
                            if _success:
                                _arch_section.append("")
                                _arch_section.append("Success criteria (project is done when ALL pass):")
                                for _c in _success[:8]:
                                    _arch_section.append(f"  - {_c}")
                            _deps = _arch_env.get("external_deps") or []
                            if _deps:
                                _arch_section.append("")
                                _arch_section.append("External dependencies:")
                                for _d in _deps[:10]:
                                    _arch_section.append(
                                        f"  - {_d.get('name','?')} {_d.get('version','')}"
                                    )
                            _risks = _arch_env.get("risk_notes") or []
                            if _risks:
                                _arch_section.append("")
                                _arch_section.append("Risk notes:")
                                for _r in _risks[:5]:
                                    _arch_section.append(f"  - {_r}")
                            _arch_section.append(
                                "\nFollow the manifest exactly — create every listed file, "
                                "use the listed build/test commands, satisfy the success "
                                "criteria. The Reviewer will verify against this plan."
                            )
                            _arch_text = "\n".join(_arch_section)
                            context = (context + _arch_text) if context else _arch_text.strip()
                            print(f"[CODEGEN] Injected architect manifest from "
                                  f"{_arch_run.get('id')}: {len(_manifest)} files, "
                                  f"{len(_success)} criteria")
                except Exception as _ame:
                    print(f"[CODEGEN] Architect manifest injection failed (non-fatal): {_ame}")

            oh_payload = {
                "task": task, "model": coder_model,
                "ollama_url": config.OLLAMA_URL,
                "max_rounds": max_rounds,
                "num_ctx": num_ctx,
                "language": language,
                "context": context,
                "project_id": _oh_project_id,
                # Pass our run_id through so the worker registers the run under
                # this key. POST /cancel/{run_id} can then abort it cleanly.
                "run_id": _run_id or "",
                # Phase 5 — profile drives the worker's task-prompt template.
                # `manifest_missing` is only meaningful for the continue profile;
                # other profiles ignore it.
                "profile": _builder_profile,
                "manifest_missing": _profile_continue_missing,
                # User-tunable reasoning_effort — drops think-token overhead
                # significantly on slow local models when set to "low".
                "reasoning_effort": getattr(config, "OPENHANDS_REASONING_EFFORT", "medium"),
            }

            async def _signal_oh_cancel(reason: str):
                """Best-effort cancel signal to the OpenHands worker."""
                if not _run_id:
                    return
                try:
                    await http.post(
                        f"{openhands_url}/cancel/{_run_id}",
                        timeout=5.0,
                    )
                    print(f"[CODEGEN:OH] Cancel signal sent to worker for {_run_id} ({reason})", flush=True)
                except Exception as ce:
                    print(f"[CODEGEN:OH] Cancel signal failed for {_run_id}: {ce}", flush=True)

            # Register the cancel event for this run so POST
            # /api/runs/{id}/cancel from the frontend Stop button can fire
            # _signal_oh_cancel without needing the chat stream to disconnect
            # first. _finalize_run cancels the watcher + drops the registry
            # entry on every terminal path.
            _cancel_event = cancel_registry.register(_run_id) if _run_id else None
            if _cancel_event is not None:
                async def _on_user_cancel():
                    try:
                        await _cancel_event.wait()
                    except asyncio.CancelledError:
                        return
                    await _signal_oh_cancel("user pressed Stop (POST /api/runs/{id}/cancel)")
                _cancel_watcher_ref[0] = asyncio.create_task(_on_user_cancel())

            # Action → emoji mapping for progress pills
            _ACTION_ICONS = {
                "starting": "🚀", "terminal": "⚡", "terminal_result": "📤",
                "file_create": "📁", "file_edit": "✏️", "file_view": "👁️",
                "file_editor_result": "📄", "glob": "🔍", "glob_result": "🔍",
                "grep": "🔎", "grep_result": "🔎", "thinking": "🧠", "finish": "✅",
            }
            _ACTION_LABELS = {
                "starting": "Starting agent", "terminal": "Running command", "terminal_result": "Command output",
                "file_create": "Writing file", "file_edit": "Editing file", "file_view": "Reading file",
                "file_editor_result": "File saved", "glob": "Searching files", "grep": "Scanning code",
                "thinking": "Overseer planning", "finish": "Wrapping up",
            }
            _agent_steps = []  # Accumulate steps for expandable detail

            # Try SSE streaming first. Fall back to blocking /run ONLY if the
            # connect failed before the first event (transport issue) — never
            # if the stream dropped mid-run, which would resurrect a fresh
            # OpenHands session the user can't see or stop.
            result = None
            _sse_first_event = False
            try:
                import httpx
                print(f"[CODEGEN:OH] Attempting SSE stream to {openhands_url}/run-stream", flush=True)
                async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=600, write=10, pool=10)) as stream_client:
                    async with stream_client.stream("POST", f"{openhands_url}/run-stream", json=oh_payload) as sse_resp:
                        if sse_resp.status_code != 200:
                            raise ConnectionError(f"SSE HTTP {sse_resp.status_code}")
                        print(f"[CODEGEN:OH] SSE connected (HTTP {sse_resp.status_code})", flush=True)
                        async for line in sse_resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            try:
                                evt = json.loads(line[6:])
                            except (json.JSONDecodeError, ValueError):
                                continue
                            if not _sse_first_event:
                                _sse_first_event = True
                                print(f"[CODEGEN:OH] First SSE event received: type={evt.get('type')}", flush=True)
                                await events.emit(conv_id, "tool_progress", {
                                    "tool": "generate_code", "icon": "wand",
                                    "status": f"Step 0/{max_rounds}: 🚀 Connected to agent stream",
                                })
                            if evt.get("type") == "step":
                                step_num = evt.get("step", 0)
                                action = evt.get("action", "")
                                detail = re.sub(r'\x1b\[[^a-zA-Z]*[a-zA-Z]|\[\?[0-9]+[a-z]', '', evt.get("detail", ""))[:80]
                                icon = _ACTION_ICONS.get(action, "⏳")
                                label = _ACTION_LABELS.get(action, action.replace("_", " ").title())
                                _agent_steps.append({"step": step_num, "icon": icon, "label": label, "detail": detail})
                                await events.emit(conv_id, "tool_progress", {
                                    "tool": "generate_code", "icon": "wand",
                                    "status": f"Step {step_num}/{max_rounds}: {icon} {label} — {detail}",
                                    "detail": json.dumps({"steps": _agent_steps}),
                                    "run_id": _run_id,
                                })
                                # Append to durable run log for reconnect-time rebuild.
                                if _run_id:
                                    try:
                                        await db.append_run_event(_run_id, {
                                            "type": "step", "step": step_num,
                                            "action": action, "detail": detail,
                                        })
                                    except Exception:
                                        pass
                            elif evt.get("type") in ("done", "error", "cancelled"):
                                result = evt
                                break
            except asyncio.CancelledError:
                # User pressed stop (or browser closed). Tell the worker to
                # abort, then re-raise so the chat stream unwinds cleanly.
                # Critically: do NOT fall back to /run here.
                await _signal_oh_cancel("chat stream cancelled")
                await _finalize_run("cancelled", {"error": "Run cancelled by user"})
                raise
            except Exception as sse_err:
                if _sse_first_event:
                    # Stream dropped after work began. The OpenHands run on the
                    # other end is still alive; the right move is to tell it to
                    # stop, not to silently start a second concurrent run.
                    print(f"[CODEGEN:OH] SSE stream failed mid-run "
                          f"({type(sse_err).__name__}: {sse_err}); cancelling worker side, NOT falling back",
                          flush=True)
                    await _signal_oh_cancel("SSE dropped mid-run")
                    await events.emit(conv_id, "tool_end", {
                        "tool": "generate_code", "icon": "code",
                        "status": f"OpenHands stream interrupted: {sse_err}",
                        "run_id": _run_id,
                    })
                    await _finalize_run("failed", {"error": f"SSE stream interrupted: {sse_err}"})
                    return (f"ERROR: OpenHands stream interrupted mid-run ({type(sse_err).__name__}). "
                            f"The worker run was cancelled. Try generate_code again, or use write_file + run_shell.")
                # No first event yet → genuine connect/transport failure → fall back.
                print(f"[CODEGEN:OH] SSE stream failed before first event "
                      f"({type(sse_err).__name__}: {sse_err}), falling back to /run",
                      flush=True)
                await events.emit(conv_id, "tool_progress", {
                    "tool": "generate_code", "icon": "wand",
                    "status": "⚡ Running agent (non-streaming)...",
                })
                try:
                    oh_resp = await http.post(
                        f"{openhands_url}/run", json=oh_payload, timeout=600,
                    )
                    if oh_resp.status_code != 200:
                        await events.emit(conv_id, "tool_end", {
                            "tool": "generate_code", "icon": "code",
                            "status": f"OpenHands HTTP {oh_resp.status_code}",
                            "run_id": _run_id,
                        })
                        await _finalize_run("failed", {
                            "error": f"OpenHands returned HTTP {oh_resp.status_code}",
                            "stderr_tail": oh_resp.text[:500],
                        })
                        return f"ERROR: OpenHands returned HTTP {oh_resp.status_code}: {oh_resp.text[:200]}"
                    result = oh_resp.json()
                except asyncio.CancelledError:
                    await _signal_oh_cancel("chat stream cancelled during /run fallback")
                    await _finalize_run("cancelled", {"error": "Run cancelled by user"})
                    raise
                except Exception as oh_e:
                    await events.emit(conv_id, "tool_end", {
                        "tool": "generate_code", "icon": "code",
                        "status": f"OpenHands request failed: {oh_e}",
                        "run_id": _run_id,
                    })
                    await _finalize_run("failed", {"error": f"OpenHands request failed: {oh_e}"})
                    return f"ERROR: OpenHands request failed: {oh_e}. Try write_file + run_shell instead."

            if not result:
                await events.emit(conv_id, "tool_end", {
                    "tool": "generate_code", "icon": "code",
                    "status": "OpenHands returned no result",
                    "run_id": _run_id,
                })
                await _finalize_run("failed", {"error": "OpenHands returned no result"})
                return "ERROR: OpenHands returned no result. Try write_file + run_shell instead."
            if result.get("status") == "ok":
                files = result.get("files_created", [])
                duration = result.get("duration_seconds", 0)
                summary = result.get("summary", "")

                # If OpenHands returned 0 files, scan CodeBox filesystem as fallback
                if not files:
                    try:
                        scan_r = await http.post(f"{config.CODEBOX_URL}/command", json={
                            "command": "find /root/ -maxdepth 5 -type f -mmin -10 "
                                       "! -path '*/node_modules/*' ! -path '*/.git/*' "
                                       "! -path '*/__pycache__/*' ! -path '*/.cache/*' "
                                       "! -path '*/.npm/*' ! -path '*/venv/*' "
                                       "! -path '*/.openhands/*' ! -path '*/.bash_history' "
                                       "! -name '*.pyc' ! -name 'package-lock.json' "
                                       "2>/dev/null | sort",
                            "timeout": 10
                        }, timeout=15)
                        scan_out = scan_r.json().get("stdout", "").strip()
                        if scan_out:
                            files = [f for f in scan_out.splitlines() if f.strip()]
                            print(f"[CODEGEN:OH] Filesystem fallback found {len(files)} files")
                    except Exception as scan_e:
                        print(f"[CODEGEN:OH] Filesystem scan failed: {scan_e}")

                # Determine project directory from files
                # Prefer /root/projects/{name} workspace, then project-*, then /root
                project_dir = "/root"
                if files:
                    dirs = set()
                    workspace_dirs = set()
                    for f in files:
                        parts = f.split("/")
                        # /root/projects/{name}/... → ["", "root", "projects", "name", ...]
                        if len(parts) >= 5 and parts[2] == "projects":
                            workspace_dirs.add("/".join(parts[:4]))
                        # Legacy: /root/project-{id}/... → ["", "root", "project-xxx", ...]
                        elif len(parts) >= 4 and parts[2].startswith("project-"):
                            workspace_dirs.add("/".join(parts[:3]))
                        elif len(parts) >= 3:
                            dirs.add("/".join(parts[:3]))
                    if len(workspace_dirs) == 1:
                        project_dir = workspace_dirs.pop()
                        files = [f for f in files if f.startswith(project_dir)]
                    elif len(workspace_dirs) > 1:
                        # Multiple workspace dirs — pick the one with most files
                        best = max(workspace_dirs, key=lambda d: sum(1 for f in files if f.startswith(d)))
                        project_dir = best
                        files = [f for f in files if f.startswith(project_dir)]
                    elif len(dirs) == 1:
                        project_dir = dirs.pop()

                _project_id = result.get("project_id", "")
                file_list = "\n".join(f"  - {f}" for f in files) if files else "  (no files detected)"
                await events.emit(conv_id, "tool_end", {
                    "tool": "generate_code", "icon": "wand",
                    "status": f"🤖 OpenHands: {len(files)} file(s) built ({duration}s)",
                })
                print(f"[CODEGEN:OH] Done: {len(files)} files in {duration}s, project_dir={project_dir}, project_id={_project_id}")

                # Auto-packaging removed — the chat agent typically keeps working past
                # generate_code (filling in missing files, fixing critic-flagged bugs, etc.),
                # so a tarball produced here was almost always stale by the time the user
                # downloaded it. The agent must now call download_project / download_file
                # explicitly once the project is verified complete.
                download_result = ""

                # Format progress steps from OpenHands
                steps = result.get("steps", [])
                steps_summary = ""
                if steps:
                    step_lines = []
                    for i, s in enumerate(steps[-10:], 1):  # Last 10 steps
                        action = s.get("action", "unknown")
                        detail = s.get("detail", "")[:100]
                        step_lines.append(f"  {i}. [{action}] {detail}")
                    steps_summary = "\n".join(step_lines)

                # ── Read key source files for overseer review ──
                code_review = ""
                if files:
                    # Filter to source files only
                    _skip_patterns = {
                        "package-lock.json", ".gitignore", ".env", "node_modules",
                        ".ico", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".woff", ".ttf",
                        ".lock", ".map", "LICENSE", ".openhands",
                    }
                    _entry_points = [
                        "App.tsx", "App.jsx", "App.js", "app.py", "main.py", "main.ts",
                        "index.ts", "index.js", "index.tsx", "server.py", "server.js",
                        "index.html", "app.js", "app.ts",
                    ]
                    source_files = [
                        f for f in files
                        if not any(skip in f for skip in _skip_patterns)
                    ]
                    # Prioritize entry points first, then the rest
                    prioritized = []
                    remaining = []
                    for f in source_files:
                        basename = f.rsplit("/", 1)[-1] if "/" in f else f
                        if basename in _entry_points:
                            prioritized.append(f)
                        else:
                            remaining.append(f)
                    review_files = (prioritized + remaining)[:5]

                    if review_files:
                        review_parts = []
                        for rf in review_files:
                            try:
                                cat_r = await http.post(
                                    f"{config.CODEBOX_URL}/command",
                                    json={"command": f"cat {shlex.quote(rf)} 2>&1", "timeout": 10},
                                    timeout=15
                                )
                                content = cat_r.json().get("stdout", "").strip()
                                if content:
                                    if len(content) > 2000:
                                        content = content[:2000] + "\n... [truncated]"
                                    review_parts.append(f"### {rf}\n```\n{content}\n```")
                            except Exception as cat_e:
                                print(f"[CODEGEN:OH] Failed to read {rf} for review: {cat_e}")
                        if review_parts:
                            code_review = "\n\n".join(review_parts)

                # ── Plan-vs-actual undershoot check ──
                # If the task description listed expected files and the agent created
                # significantly fewer, flag the result as INCOMPLETE so the chat agent
                # fixes it before delivering. Catches coder models that call `finish` early.
                undershoot_warning = ""
                _expected_files = []
                try:
                    # Parse task body for filenames in "File structure:" / "- foo.java" / "├── foo.java" patterns
                    _task_text = task or ""
                    # Match basenames with common code extensions in list-like positions.
                    _ext_re = r"\.(?:java|py|js|ts|tsx|jsx|go|rs|c|cpp|h|hpp|rb|php|cs|kt|swift|html|css|sql|sh|toml|yaml|yml|json|md|xml)"
                    _name_re = re.compile(
                        r"(?:^|[\s├└│─\-\*•])([A-Za-z][\w\-]*" + _ext_re + r")\b",
                        re.MULTILINE,
                    )
                    for _m in _name_re.finditer(_task_text):
                        _fn = _m.group(1)
                        if _fn not in _expected_files:
                            _expected_files.append(_fn)
                except Exception as _ef_e:
                    print(f"[CODEGEN:OH] Expected-file parse failed (non-fatal): {_ef_e}")

                if _expected_files:
                    _actual_basenames = {(f.rsplit("/", 1)[-1] if "/" in f else f) for f in (files or [])}
                    _missing = [n for n in _expected_files if n not in _actual_basenames]
                    _present = len(_expected_files) - len(_missing)
                    print(f"[CODEGEN:OH] Manifest check: {_present}/{len(_expected_files)} expected files present, missing={_missing[:8]}")
                    # Trigger if any of: <50% present, or 3+ missing files
                    if _expected_files and (_present / len(_expected_files) < 0.5 or len(_missing) >= 3):
                        undershoot_warning = (
                            f"⚠ INCOMPLETE: agent created {_present}/{len(_expected_files)} expected files. "
                            f"Missing: {', '.join(_missing[:12])}{'…' if len(_missing) > 12 else ''}. "
                            f"DO NOT deliver the project to the user yet. Either: "
                            f"(a) call generate_code again with project_id='{_project_id}' and a task that "
                            f"explicitly lists ONLY the missing files to create, or "
                            f"(b) write each missing file directly with write_file. "
                            f"After all expected files exist, re-verify with run_shell (e.g. compile/parse) "
                            f"before download_project. If the user explicitly asks for the incomplete "
                            f"artifact anyway, disclose the missing files and package the current state."
                        )
                        await events.emit(conv_id, "tool_progress", {
                            "tool": "generate_code", "icon": "wand",
                            "status": f"⚠ Agent undershot: {_present}/{len(_expected_files)} files",
                        })

                # ── Independent critic pass: catch runtime bugs that pass syntax/compile checks ──
                # Uses a separate model from the coder so it can't rationalize its own mistakes.
                critique = ""
                _skip_inline_critic = bool(conv_id) and await _check_v2()
                if _skip_inline_critic:
                    print("[CODEGEN:CRITIC] Skipping inline critic for Coder Bot v2; run_review is the verification gate")
                if code_review and config.CRITIC_ENABLED and not _skip_inline_critic:
                    critic_model = config.CRITIC_MODEL or config.PLANNING_MODEL or conv_model or config.DEFAULT_MODEL
                    await events.emit(conv_id, "tool_progress", {
                        "tool": "generate_code", "icon": "wand",
                        "status": f"🔍 Reviewing code with {critic_model}...",
                    })
                    critic_prompt = f"""You are a senior code reviewer. An autonomous coding agent was asked to build:

"{task}"

Language: {language}

The agent says it's done. Your job: catch RUNTIME bugs the agent missed — issues that pass syntax/compile checks but break behavior. Read carefully. Ignore stylistic concerns; focus on correctness.

## Files
{code_review}

## What to look for
- Setter/getter mismatches (e.g. `setStroke(color)` then `fillRect(...)` — fill color is never set)
- Constants defined but ignored downstream (e.g. SPEED=5 but the move function hardcodes 500)
- Math/units that produce nonsense values at runtime (e.g. 5 px/sec at 60fps barely moves)
- Missing background fills, focus calls, init, lifecycle hooks, event listener registration
- Off-by-one, wrong sign, wrong variable used in a formula
- API misuse (wrong method, wrong order, missing required setup call)
- Missing wiring between components (handler exists but never registered)
- User requirements that don't appear in the code

## Output format
If you find issues, list them as numbered items:
1. **path/to/file.ext** — what's wrong (one sentence) — why it breaks at runtime

List every concrete bug you can find, max 10. Be specific: name the variable, function, or method.
If the code is genuinely correct, output exactly: NO RUNTIME ISSUES FOUND"""
                    try:
                        cr = await http.post(
                            f"{config.OLLAMA_URL}/api/chat",
                            json={
                                "model": critic_model,
                                "messages": [{"role": "user", "content": critic_prompt}],
                                "stream": False,
                                "options": {"temperature": 0.2, "num_ctx": config.DEFAULT_NUM_CTX},
                            },
                            timeout=600,
                        )
                        if cr.status_code == 200:
                            critique = cr.json().get("message", {}).get("content", "").strip()
                            # Strip <think> blocks if the critic model emits them
                            critique = re.sub(r"<think>[\s\S]*?</think>", "", critique).strip()
                            issues_found = bool(critique) and "NO RUNTIME ISSUES FOUND" not in critique.upper()
                            await events.emit(conv_id, "tool_progress", {
                                "tool": "generate_code", "icon": "wand",
                                "status": f"🔍 Review complete — {'issues found' if issues_found else 'no issues'}",
                                "detail": json.dumps({"method": "code_review", "args": {"reviewer": critic_model}, "result": critique[:4000]}),
                            })
                        else:
                            print(f"[CODEGEN:CRITIC] Critic call failed HTTP {cr.status_code}: {cr.text[:200]}")
                            await events.emit(conv_id, "tool_progress", {
                                "tool": "generate_code", "icon": "wand",
                                "status": f"⚠ Critic skipped (HTTP {cr.status_code})",
                            })
                    except Exception as critic_e:
                        print(f"[CODEGEN:CRITIC] Critic call failed ({type(critic_e).__name__}): {critic_e}")
                        await events.emit(conv_id, "tool_progress", {
                            "tool": "generate_code", "icon": "wand",
                            "status": f"⚠ Critic skipped ({type(critic_e).__name__})",
                        })

                if files:
                    _status_word = "INCOMPLETE" if undershoot_warning else "COMPLETE"
                    resp = (
                        f"PROJECT {_status_word}. OpenHands agent ran "
                        f"(model: {coder_model}, {duration}s, {len(steps)} steps, project_id: {_project_id}).\n\n"
                        f"**Files created ({len(files)}):**\n{file_list}\n"
                    )
                    if undershoot_warning:
                        resp += f"\n{undershoot_warning}\n"
                    if steps_summary:
                        resp += f"\n**Agent activity (last steps):**\n{steps_summary}\n"
                    # download_result is intentionally always empty now — see comment near line 1780.
                    # The chat agent is expected to call download_project explicitly after verification.
                    if summary:
                        resp += f"\n**Agent summary:** {summary[:300]}\n"
                    if code_review:
                        resp += f"\n**Key file contents for review:**\n{code_review}\n"
                    if critique:
                        _has_issues = "NO RUNTIME ISSUES FOUND" not in critique.upper()
                        resp += f"\n**Independent code review (reviewer model):**\n{critique}\n"
                        if _has_issues:
                            resp += (
                                f"\n⚠ The reviewer flagged runtime bugs that pass `mvn compile` / "
                                f"`tsc --noEmit` but break behavior. You MUST fix each one before "
                                f"delivering. For each issue: open the file with read_file, edit "
                                f"with write_file, and re-verify. Do NOT call download_file or "
                                f"download_project until the flagged bugs are addressed.\n"
                            )
                    resp += (
                        f"\nREVIEW the file contents above. Evaluate whether the code actually "
                        f"fulfills the user's request — not just scaffolding/boilerplate. "
                        f"If the output is incomplete or doesn't match what was asked, call "
                        f"generate_code again with project_id='{_project_id}' and a MORE DETAILED "
                        f"task description explaining exactly what's wrong and what to fix.\n\n"
                        f"## DELIVERY (REQUIRED — no auto-download)\n"
                        f"NO tarball has been packaged yet. When (and only when) the project is verified complete:\n"
                        f"1. Make sure all expected files exist and any reviewer-flagged issues are fixed.\n"
                        f"2. Verify it builds/runs (e.g. `mvn compile`, `python -c 'import ...'`, etc.) via run_shell.\n"
                        f"3. Call download_project(directory='{project_dir}') to package the FINAL sandbox state.\n"
                        f"4. THEN respond to the user with: what was built, the download link returned by step 3, and how to run it locally.\n"
                        f"Calling download_project before the project is complete will deliver a broken project.\n"
                    )
                    # Save project metadata to DB for resume_project
                    try:
                        proj_name = task[:60].strip().replace("\n", " ")
                        await db.upsert_coding_project(
                            project_id=_project_id or f"proj-{uuid.uuid4().hex[:12]}",
                            name=proj_name, conversation_id=conv_id,
                            description=task[:500], language=language,
                            file_manifest=files, openhands_project_id=_project_id,
                        )
                    except Exception as proj_e:
                        print(f"[CODEGEN] Failed to save project metadata: {proj_e}")
                    # Index generated code into code memory RAG
                    if code_review:
                        try:
                            import rag as _rag
                            # Parse code_review into {filepath: content} dict
                            _code_files = {}
                            _current_file = None
                            _current_lines = []
                            for _line in code_review.split("\n"):
                                if _line.startswith("### "):
                                    if _current_file and _current_lines:
                                        _code_files[_current_file] = "\n".join(_current_lines)
                                    _current_file = _line[4:].strip()
                                    _current_lines = []
                                elif _current_file:
                                    if not (_line.startswith("```") and len(_line) < 20):
                                        _current_lines.append(_line)
                            if _current_file and _current_lines:
                                _code_files[_current_file] = "\n".join(_current_lines)
                            if _code_files:
                                asyncio.create_task(_rag.index_generated_code(
                                    task=task, language=language, file_contents=_code_files,
                                    conv_id=conv_id, project_id=_project_id,
                                ))
                        except Exception as _rag_e:
                            print(f"[CODEGEN] Code RAG indexing failed (non-fatal): {_rag_e}")
                    # Finalize the durable run with a structured envelope summarising
                    # what got built. Mark partial when the agent undershot the manifest;
                    # the build itself can still be considered a success otherwise.
                    _final_status = "partial" if undershoot_warning else "succeeded"
                    await _finalize_run(_final_status, {
                        "files_written": files,
                        "manifest_satisfied": [n for n in _expected_files
                                                if n in {(f.rsplit("/", 1)[-1] if "/" in f else f)
                                                         for f in (files or [])}],
                        "manifest_missing": _missing if _expected_files else [],
                        "build_summary": summary,
                        "project_id": _project_id,
                        "project_dir": project_dir,
                        "duration_s": duration,
                        "critique": critique,
                        "model": coder_model,
                        "language": language,
                    })
                    try:
                        wf = await db.get_latest_coder_workflow(conv_id, project_id=_project_id)
                        if not wf:
                            # The worker may have dedupe-renamed the project so
                            # _project_id no longer matches the workflow row;
                            # fall back to the conversation's latest workflow
                            # (the update below writes the new project_id back).
                            wf = await db.get_latest_coder_workflow(conv_id)
                        if wf:
                            await db.update_coder_workflow(
                                wf["id"],
                                state="reviewing",
                                active_run_id=_run_id,
                                artifact_status="not_ready",
                                project_id=_project_id,
                            )
                    except Exception as _wfe:
                        print(f"[WORKFLOW] builder state update failed: {_wfe}")
                    return resp
                else:
                    # Agent ran but produced no files — treat as failure
                    print(f"[CODEGEN:OH] Agent finished but created 0 files — reporting as error")
                    await events.emit(conv_id, "tool_end", {
                        "tool": "generate_code", "icon": "wand",
                        "status": f"🤖 OpenHands: 0 files (model may not support tools)",
                        "run_id": _run_id,
                    })
                    error_detail = summary[:200] if summary else "Agent completed but produced no files"
                    await _finalize_run("failed", {
                        "error": "Agent finished but created 0 files",
                        "model": coder_model,
                        "language": language,
                        "duration_s": duration,
                        "summary": error_detail,
                    })
                    return (
                        f"ERROR: OpenHands agent finished but created 0 files "
                        f"(model: {coder_model}, {duration}s). "
                        f"The model may not support tool calling. "
                        f"Detail: {error_detail}\n\n"
                        f"The coding agent failed. You MUST now write the code yourself directly "
                        f"using write_file and run_shell tools. Do NOT call generate_code again."
                    )
            else:
                error = result.get("error", "Unknown error")[:300]
                status = result.get("status", "error")
                steps = result.get("steps", [])

                # ── Retry once with simplified task on stuck/error ──
                if status in ("stuck", "error") and not oh_payload.get("_retried"):
                    print(f"[CODEGEN:OH] Agent {status}, retrying with simplified task (+5 rounds)...")
                    oh_payload["_retried"] = True
                    oh_payload["max_rounds"] = max_rounds + 5
                    oh_payload["task"] = (
                        f"SIMPLE REQUEST — focus on writing code, not verifying:\n{task}"
                    )
                    await events.emit(conv_id, "tool_progress", {
                        "tool": "generate_code", "icon": "wand",
                        "status": f"🔄 Retrying with simplified approach...",
                    })
                    _agent_steps = []
                    _retry_first_event = False
                    try:
                        import httpx as _httpx
                        async with _httpx.AsyncClient(timeout=_httpx.Timeout(connect=10, read=600, write=10, pool=10)) as retry_client:
                            async with retry_client.stream("POST", f"{openhands_url}/run-stream", json=oh_payload) as retry_resp:
                                if retry_resp.status_code == 200:
                                    async for line in retry_resp.aiter_lines():
                                        if not line.startswith("data: "):
                                            continue
                                        try:
                                            evt = json.loads(line[6:])
                                        except (json.JSONDecodeError, ValueError):
                                            continue
                                        _retry_first_event = True
                                        if evt.get("type") == "step":
                                            step_num = evt.get("step", 0)
                                            action = evt.get("action", "")
                                            detail = re.sub(r'\x1b\[[^a-zA-Z]*[a-zA-Z]|\[\?[0-9]+[a-z]', '', evt.get("detail", ""))[:80]
                                            icon = _ACTION_ICONS.get(action, "⏳")
                                            label = _ACTION_LABELS.get(action, action.replace("_", " ").title())
                                            _agent_steps.append({"step": step_num, "icon": icon, "label": label, "detail": detail})
                                            await events.emit(conv_id, "tool_progress", {
                                                "tool": "generate_code", "icon": "wand",
                                                "status": f"Retry step {step_num}: {icon} {label} — {detail}",
                                            })
                                        elif evt.get("type") in ("done", "error", "cancelled"):
                                            result = evt
                                            break
                                else:
                                    result = None
                    except asyncio.CancelledError:
                        await _signal_oh_cancel("chat stream cancelled during retry")
                        await _finalize_run("cancelled", {"error": "Run cancelled by user"})
                        raise
                    except Exception as retry_err:
                        if _retry_first_event:
                            # Same rule: don't resurrect with /run after a mid-stream drop.
                            print(f"[CODEGEN:OH] Retry SSE dropped mid-run "
                                  f"({type(retry_err).__name__}: {retry_err}); cancelling worker side")
                            await _signal_oh_cancel("retry SSE dropped mid-run")
                            result = None
                        else:
                            print(f"[CODEGEN:OH] Retry SSE failed before first event "
                                  f"({type(retry_err).__name__}: {retry_err}), trying blocking /run")
                            try:
                                oh_resp = await http.post(f"{openhands_url}/run", json=oh_payload, timeout=600)
                                if oh_resp.status_code == 200:
                                    result = oh_resp.json()
                                else:
                                    result = None
                            except asyncio.CancelledError:
                                await _signal_oh_cancel("chat stream cancelled during retry /run")
                                await _finalize_run("cancelled", {"error": "Run cancelled by user"})
                                raise
                            except Exception:
                                result = None

                    # If retry produced a successful result, process it above
                    if result and result.get("status") == "ok":
                        files = result.get("files_created", [])
                        if files:
                            # Re-run the success path (simplified — just return the result)
                            duration = result.get("duration_seconds", 0)
                            _project_id = result.get("project_id", "")
                            file_list = "\n".join(f"  - {f}" for f in files)
                            await events.emit(conv_id, "tool_end", {
                                "tool": "generate_code", "icon": "wand",
                                "status": f"🤖 OpenHands (retry): {len(files)} file(s) built ({duration}s)",
                                "run_id": _run_id,
                            })
                            await _finalize_run("succeeded", {
                                "files_written": files,
                                "build_summary": "Retry succeeded after initial failure",
                                "project_id": _project_id,
                                "duration_s": duration,
                                "model": coder_model,
                                "language": language,
                                "retried": True,
                            })
                            return (
                                f"PROJECT COMPLETE (retry succeeded). OpenHands agent built the project "
                                f"(model: {coder_model}, {duration}s, project_id: {_project_id}).\n\n"
                                f"**Files created ({len(files)}):**\n{file_list}\n"
                            )
                    # Retry also failed — fall through to error response below
                    print(f"[CODEGEN:OH] Retry also failed")

                await events.emit(conv_id, "tool_end", {
                    "tool": "generate_code", "icon": "wand",
                    "status": f"🤖 OpenHands agent {status}",
                    "run_id": _run_id,
                })
                print(f"[CODEGEN:OH] Agent {status}: {error}")
                err_resp = f"ERROR: OpenHands agent {status}: {error}."
                if steps:
                    last_steps = [f"  - [{s.get('action','')}] {s.get('detail','')[:80]}" for s in steps[-5:]]
                    err_resp += f"\nLast agent steps:\n" + "\n".join(last_steps)
                err_resp += (
                    "\n\nThe coding agent failed. You MUST now write the code yourself directly "
                    "using write_file and run_shell tools. Do NOT call generate_code again."
                )
                await _finalize_run("failed", {
                    "error": error,
                    "agent_status": status,
                    "model": coder_model,
                    "language": language,
                    "last_steps": [{"action": s.get("action", ""), "detail": s.get("detail", "")[:200]}
                                    for s in (steps[-5:] if steps else [])],
                })
                return err_resp

        elif name in connector_tool_name_map:
            ct = connector_tool_name_map[name]
            await events.emit(conv_id, "tool_start", {
                "tool": name,
                "icon": "plug",
                "status": f"Calling {ct.get('display_name') or name}...",
            })
            try:
                result = await execute_connector_tool(http, ct, args or {})
                await events.emit(conv_id, "tool_end", {
                    "tool": name,
                    "icon": "plug",
                    "status": f"OK {ct.get('display_name') or name}",
                })
                return result or "No output"
            except Exception as exec_e:
                await events.emit(conv_id, "tool_error", {
                    "tool": name,
                    "icon": "plug",
                    "status": f"Error: {str(exec_e)}",
                })
                return f"**Connector tool error ({name}):** {str(exec_e)}"

        elif name in custom_tool_map:
            ct = custom_tool_map[name]
            await events.emit(conv_id, "tool_start", {"tool": name, "icon": "code", "status": f"Running {name}..."})
            if args:
                arg_parts = ", ".join(f"{k}={repr(v)}" for k, v in args.items())
            else:
                arg_parts = ""
            run_code = f"{ct['code']}\n\n_result = {name}({arg_parts})\nprint(_result if _result is not None else '')"
            try:
                r = await http.post(
                    f"{config.CODEBOX_URL}/execute",
                    json={"code": run_code, "language": "python"},
                    timeout=30,
                )
                result = r.json()
                stdout = result.get("stdout", "").strip()
                stderr = result.get("stderr", "").strip()
                success = result.get("exit_code", -1) == 0 or result.get("success", False)
                await events.emit(conv_id, "tool_end", {
                    "tool": name, "icon": "code",
                    "status": f"{'OK' if success else 'FAILED'} {name}",
                })
                return stdout or stderr or "No output"
            except Exception as exec_e:
                await events.emit(conv_id, "tool_error", {"tool": name, "icon": "code", "status": f"Error: {str(exec_e)}"})
                return f"**Custom tool error ({name}):** {str(exec_e)}"

        else:
            return f"Unknown tool: {name}"
    except Exception as e:
        await events.emit(conv_id, "tool_error", {"tool": name, "icon": "code", "status": f"Error: {str(e)}"})
        return f"**Tool error ({name}):** {str(e)}"
