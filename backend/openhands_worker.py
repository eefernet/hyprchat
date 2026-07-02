"""
OpenHands Worker — runs inside the CodeBox LXC container.
Receives coding tasks from HyprChat, runs an OpenHands agent loop,
and returns results with real-time progress. Listens on port 8586.

Deploy to CodeBox LXC at /opt/openhands-worker/openhands_worker.py
"""
import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


class _RunCancelled(Exception):
    """Raised inside on_event when the client requests cancellation."""


_ACTIVE_RUNS: dict[str, dict] = {}
_ACTIVE_RUNS_LOCK = threading.Lock()
_ACTIVE_AIDER_RUNS: dict[str, dict] = {}
_ACTIVE_AIDER_RUNS_LOCK = threading.Lock()
AIDER_MAX_SECONDS = int(os.environ.get("AIDER_MAX_SECONDS", "420"))
AIDER_REPEAT_LINE_LIMIT = int(os.environ.get("AIDER_REPEAT_LINE_LIMIT", "120"))
# OpenHands LLM call bounds. num_predict caps a single completion so it always
# terminates (an uncapped qwen3-coder response was observed at 14K+ tokens,
# which can never finish inside any sane timeout); the timeout then only fires
# on genuinely dead calls, not on long-but-healthy generations.
OH_LLM_TIMEOUT_SECONDS = int(os.environ.get("OH_LLM_TIMEOUT_SECONDS", "420"))
OH_NUM_PREDICT = int(os.environ.get("OH_NUM_PREDICT", "8192"))
# Thinking control for the builder LLM. Thinking-capable models (qwen3.5
# merges etc.) otherwise reason with an UNBOUNDED budget on every tool call —
# reasoning_effort never reaches Ollama's `think` switch through litellm.
# "" (default) = send think:false to models whose /api/show capabilities
# include "thinking"; "on" = never send it; "off" = same as default.
OH_THINK = os.environ.get("OH_THINK", "").strip().lower()


def _register_run(run_id: str, conversation, cancel_event: threading.Event) -> None:
    """conversation may be None during early registration (before model
    preload); re-registering with the real conversation reuses the same
    cancel_event so a cancel that landed in between is not lost."""
    if not run_id:
        return
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS[run_id] = {"conversation": conversation, "cancel": cancel_event}


def _deregister_run(run_id: str) -> None:
    if not run_id:
        return
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS.pop(run_id, None)


def _register_aider_run(run_id: str, process, cancel_event: threading.Event) -> None:
    """process may be None during early registration (before Popen)."""
    if not run_id:
        return
    with _ACTIVE_AIDER_RUNS_LOCK:
        _ACTIVE_AIDER_RUNS[run_id] = {"process": process, "cancel": cancel_event}


def _terminate_proc_tree(proc, sig=signal.SIGTERM) -> None:
    """Signal the whole process group — aider spawns --test-cmd shells that
    plain terminate()/kill() would orphan."""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except Exception:
        try:
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
        except Exception:
            pass


def _deregister_aider_run(run_id: str) -> None:
    if not run_id:
        return
    with _ACTIVE_AIDER_RUNS_LOCK:
        _ACTIVE_AIDER_RUNS.pop(run_id, None)

app = FastAPI(title="OpenHands Worker")

# Lazy imports — heavy SDK loaded on first request
_sdk_loaded = False
_LLM = None
_Agent = None
_Conversation = None
_Tool = None


def _ensure_sdk():
    global _sdk_loaded, _LLM, _Agent, _Conversation, _Tool
    if _sdk_loaded:
        return
    import openhands.tools.terminal.definition  # noqa: F401
    import openhands.tools.file_editor.definition  # noqa: F401
    import openhands.tools.glob.definition  # noqa: F401
    import openhands.tools.grep.definition  # noqa: F401
    from openhands.sdk import LLM, Agent, Conversation, Tool
    _LLM = LLM
    _Agent = Agent
    _Conversation = Conversation
    _Tool = Tool
    _sdk_loaded = True


PROJECTS_DIR = Path("/root/projects")


def _derive_project_name(task: str, language: str) -> str:
    """Derive a clean project folder name from the task description."""
    import re as _re
    text = task.lower()

    name_match = _re.search(r'''(?:called|named|titled)\s+['\"]?([a-z][a-z0-9_-]{1,30})['\"]?''', text)
    if name_match:
        return name_match.group(1).strip("-_")

    thing_match = _re.search(
        r'(?:build|create|make|write|develop)\s+(?:a|an)\s+'
        r'([a-z][a-z0-9 _-]{1,40}?)\s*'
        r'(?:tool|app|application|site|website|dashboard|bot|game|cli|script|program|api|server|service|library|package)',
        text,
    )
    if thing_match:
        name = thing_match.group(1).strip()
        words = name.split()[-3:]
        return "-".join(words).replace("_", "-").strip("-")[:30]

    words = _re.findall(r'[a-z]+', text[:80])
    skip = {"build", "create", "make", "write", "develop", "a", "an", "the", "that",
            "which", "using", "with", "for", "and", "python", "javascript", "typescript",
            "rust", "go", "java"}
    meaningful = [w for w in words if w not in skip and len(w) > 2][:3]
    if meaningful:
        return "-".join(meaningful)[:30]

    return f"{language}-project"


def _clean_project_name(name: str) -> str:
    """Return a safe single-directory project name."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", (name or "").strip())
    cleaned = cleaned.strip(".-_")
    return cleaned[:80]


def _workspace_for_request(req: "RunRequest") -> tuple[Path, str, bool]:
    """Choose the OpenHands workspace (pre-refactor behavior).

    A provided project_id is honored only as a genuine RESUME — i.e. when that
    directory already exists. A fresh build always lands in a new task-derived,
    uuid-uniquified directory so it can never inherit "continue working on the
    existing files" semantics from a colliding or stale project id (which is
    what made the Builder write a single file into a pre-existing dir).
    """
    import uuid

    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    requested = _clean_project_name(req.project_id)
    if requested and (PROJECTS_DIR / requested).is_dir():
        work_dir = PROJECTS_DIR / requested
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir, requested, True

    project_name = _derive_project_name(req.task, req.language)
    work_dir = PROJECTS_DIR / project_name
    if work_dir.exists():
        work_dir = PROJECTS_DIR / f"{project_name}-{uuid.uuid4().hex[:4]}"
        project_name = work_dir.name
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir, project_name, False


def _agent_finished(status_str: str, progress_log: list[dict]) -> bool:
    """True if the build agent reached a proper `finish`, not a truncation.

    A run that stops without finishing (model truncated mid-build, hit the round
    cap, errored) must not be reported as a complete success. Conservative:
    only returns False when NEITHER a FINISHED execution status NOR a finish
    action was observed, so genuinely-finished builds are never false-flagged.
    """
    if "finish" in (status_str or "").lower():
        return True
    for step in progress_log or []:
        if "finish" in str(step.get("action") or "").lower():
            return True
    return False


# Bounded same-conversation "you are not done" nudges. Local coder models
# routinely end a turn with plain text ("Now let me create the frontend
# files:") and no tool call, which the SDK treats as the agent finishing —
# leaving most planned files unwritten. send_message() on a FINISHED
# conversation resets it to IDLE, so a nudge resumes the SAME warm
# conversation instead of paying for a fresh worker pass.
# Observed live: qwen3-coder narrates-then-stops after almost every action,
# so a nudge typically yields ONE file in 2-10s off the warm prompt cache.
# The budget is therefore generous, and a single text-only reply doesn't end
# the loop — only OH_NUDGE_NO_PROGRESS_LIMIT consecutive no-progress nudges do.
OH_COMPLETION_NUDGES = int(os.environ.get("OH_COMPLETION_NUDGES", "12"))
OH_NUDGE_NO_PROGRESS_LIMIT = int(os.environ.get("OH_NUDGE_NO_PROGRESS_LIMIT", "2"))


def _normalize_required_path(path: str) -> str:
    """Mirror the backend's _normalize_manifest_path so both sides agree."""
    path = str(path or "").replace("\\", "/").strip()
    if not path:
        return ""
    path = re.sub(r"^/+", "", path)
    if path.startswith("root/projects/"):
        parts = path.split("/")
        path = "/".join(parts[3:]) if len(parts) > 3 else ""
    path = re.sub(r"/+", "/", path).strip("/")
    if not path or path.endswith("/"):
        return ""
    return path


def _missing_required_files(work_dir, required: list[str]) -> list[str]:
    """Planned files that do not exist under work_dir yet."""
    base = Path(work_dir)
    missing = []
    for raw in required or []:
        rel = _normalize_required_path(raw)
        if not rel:
            continue
        try:
            if not (base / rel).is_file():
                missing.append(rel)
        except OSError:
            missing.append(rel)
    return missing


def _run_completion_nudges(conversation, work_dir, required: list[str],
                           cancel_event=None, on_nudge=None,
                           extra_note: str = "") -> tuple[int, list[str]]:
    """Resume a finished conversation until every required file exists.

    Returns (nudges_used, still_missing). A single no-progress nudge (the
    model replied with narration text and no tool call — its signature
    failure) gets ONE insistent retry naming a concrete file; only
    OH_NUDGE_NO_PROGRESS_LIMIT consecutive no-progress nudges end the loop.
    """
    missing = _missing_required_files(work_dir, required)
    nudges = 0
    no_progress = 0
    while missing and nudges < OH_COMPLETION_NUDGES:
        if cancel_event is not None and cancel_event.is_set():
            raise _RunCancelled()
        nudges += 1
        before = set(missing)
        if on_nudge:
            try:
                on_nudge(nudges, missing)
            except Exception:
                pass
        print(f"[OH-Worker] Completion nudge {nudges}/{OH_COMPLETION_NUDGES}: "
              f"{len(missing)} required file(s) missing — resuming conversation")
        listing = "\n".join(f"  - {p}" for p in missing[:30])
        if no_progress:
            msg = (
                f"STOP narrating. Your previous reply was plain text with no tool "
                f"call — that does nothing. Use the file_editor tool RIGHT NOW to "
                f"create `{work_dir}/{missing[0]}` with its complete contents. "
                f"Then create the rest, one tool call after another, until every "
                f"file below exists:\n{listing}\n\n"
                f"Only call `finish` when all of them exist."
            )
        else:
            msg = (
                f"You are NOT done. These planned files are still missing from "
                f"`{work_dir}`:\n{listing}\n\n"
                f"Create EVERY one of them now with their full contents, re-run the "
                f"verification steps, and only then call `finish`. Do not respond "
                f"with a plain text message — every response must be a tool call."
            )
        if extra_note:
            msg += extra_note
        conversation.send_message(msg)
        conversation.run()
        missing = _missing_required_files(work_dir, required)
        if set(missing) == before:
            no_progress += 1
            if no_progress >= OH_NUDGE_NO_PROGRESS_LIMIT:
                print(f"[OH-Worker] Nudge {nudges}: {no_progress} consecutive "
                      f"no-progress nudges on {len(missing)} missing file(s); "
                      f"stopping nudge loop")
                break
            print(f"[OH-Worker] Nudge {nudges} made no progress "
                  f"({len(missing)} still missing) — retrying with insistent nudge")
        else:
            no_progress = 0
    return nudges, missing


class RunRequest(BaseModel):
    task: str
    model: str = "qwen2.5:14b"
    ollama_url: str = "http://127.0.0.1:11434"
    max_rounds: int = 20
    num_ctx: int = 16384
    language: str = "python"
    context: str = ""
    project_id: str = ""
    run_id: str = ""
    # Phase 5 — Builder profile. Default "scaffold" preserves prior behavior
    # for any caller that doesn't pass one.
    profile: str = "scaffold"  # scaffold | continue | feature
    # Reasoning effort passed to the SDK LLM. Default "medium"; "high" is
    # heavy-think-token mode and slow on local Ollama models, "low" is fastest.
    reasoning_effort: str = "medium"  # low | medium | high
    # Send `think: false` to thinking-capable models (qwen3.5 merges reason
    # with an UNBOUNDED budget per tool call otherwise — reasoning_effort
    # never reaches Ollama's think switch through litellm). Non-thinking
    # models are unaffected (the flag is capability-gated worker-side).
    disable_thinking: bool = True
    # Only meaningful when profile == "continue": list of manifest files the
    # last builder run failed to write. The worker focuses on these.
    manifest_missing: list[str] = []
    # Full Architect manifest (project-root-relative paths). When set, the
    # worker nudges the agent (same conversation) until every file exists or
    # the nudge budget runs out, and reports what is still missing.
    required_files: list[str] = []


class RunResponse(BaseModel):
    status: str          # "ok", "error", "stuck"
    files_created: list[str] = []
    summary: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    steps: list[dict] = []
    project_id: str = ""
    # False when the agent stopped without a proper `finish` (truncated /
    # hit the round cap). Lets the backend flag a one-file build incomplete.
    agent_finished: bool = True
    # required_files entries still absent after the completion-nudge loop.
    missing_required_files: list[str] = []
    # True when the model repeatedly emitted file_editor calls with the
    # file_text argument missing (≥3 in this run) — a native-tool-arg
    # reliability failure, not a task failure.
    arg_drop_detected: bool = False


class AiderRunRequest(BaseModel):
    project_dir: str
    task: str
    issue_envelope: dict = {}
    contract: dict = {}
    model: str = "qwen2.5-coder:14b"
    ollama_url: str = "http://127.0.0.1:11434"
    num_ctx: int = 16384
    test_cmd: str = ""
    lint_cmd: str = ""
    allowed_files: list[str] = []
    run_id: str = ""
    auto_test: bool = True


class AiderRunResponse(BaseModel):
    status: str
    summary: str = ""
    project_dir: str = ""
    files_touched: list[str] = []
    diff: str = ""
    test_exit: int | None = None
    test_stdout_tail: str = ""
    test_stderr_tail: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""
    exit_code: int = 0
    duration_seconds: float = 0.0


CACHE_PATH = Path("/opt/openhands-worker/.tool_cache.json")
# Bump to invalidate persisted entries when the detection logic changes.
# v2: Ollama 0.30 capabilities-aware check — pre-0.30 entries misclassified
# llama.cpp-backend models (degenerate template, no .Tools) as prompt-based.
# v3: multi-line arg round-trip probe — v2 trusted capabilities alone, which
# passed models (Qwopus3.6 merge) that emit tool calls with the file_text
# argument MISSING. v3 verifies a multi-line string arg survives intact.
_TOOL_CACHE_VERSION = 3
_tool_support_cache: dict[str, bool] = {}

# /api/show capabilities per model (in-process only — cheap call, and
# capabilities change when the user re-imports a model).
_caps_cache: dict[str, list] = {}


def _model_capabilities(ollama_base: str, model: str) -> list:
    """Cached /api/show capabilities list for a model. [] on any failure
    (transient failures are NOT cached)."""
    key = f"{ollama_base}:{model}"
    if key in _caps_cache:
        return _caps_cache[key]
    try:
        import requests
        r = requests.post(f"{ollama_base}/api/show", json={"name": model}, timeout=5)
        if not r.ok:
            return []
        caps = r.json().get("capabilities") or []
    except Exception:
        return []
    _caps_cache[key] = caps
    return caps


def _resolve_think_flag(ollama_base: str, model: str, disable_thinking: bool) -> bool | None:
    """False → send `think: false` to Ollama; None → send nothing.

    Only thinking-capable models get the flag (Ollama 400s otherwise). The
    OH_THINK env var overrides the per-request setting: "on" never disables,
    "off" always disables (for thinking-capable models).
    """
    if OH_THINK == "on":
        return None
    if not disable_thinking and OH_THINK != "off":
        return None
    if "thinking" in _model_capabilities(ollama_base, model):
        return False
    return None


# file_editor's observation text when the model omitted the content arg.
_ARG_DROP_MARKER = "file_text` is required"
_ARG_DROP_NOTE = (
    "\n\nCRITICAL: when calling file_editor `create` you MUST include the "
    "complete file content in the `file_text` parameter of the SAME call — "
    "never a path alone. If the tool keeps rejecting your call, write the "
    "file with a terminal heredoc instead: cat > /path/file << 'EOF' ... EOF"
)


def _maybe_demote_native_tc(ollama_base: str, model: str, drops: int,
                            still_missing: list) -> None:
    """Persist native_tool_calling=False for a model that dropped file_text
    args ≥3 times in a run that still failed to complete its manifest —
    future runs (and this build's continue passes) use the prompt-based path."""
    if drops < 3 or not still_missing:
        return
    ck = f"{ollama_base}:{model}"
    if _tool_support_cache.get(ck) is False:
        return
    _tool_support_cache[ck] = False
    _persist_tool_cache()
    print(f"[OH-Worker] {model}: dropped file_text args {drops}x this run with "
          f"{len(still_missing)} planned file(s) still missing — DEMOTED to "
          f"prompt-based tool calling (persisted)")


def _make_llm_and_agent(req: "RunRequest", ollama_base: str, native_tc: bool,
                        tag: str = ""):
    """LLM + Agent construction shared by /run and /run-stream.

    Keeping one copy prevents the paths from drifting (the stream path
    historically missed reasoning_effort → SDK default "high").
    """
    _re = (req.reasoning_effort or "medium").strip().lower()
    if _re not in ("low", "medium", "high"):
        _re = "medium"
    # num_ctx/num_predict/temperature must ride inside "options" — the
    # ollama_chat litellm path drops top-level kwargs (bare num_ctx is
    # silently ignored → Modelfile default, often 131K+, blows VRAM), and
    # num_predict bounds each completion so it always terminates.
    extra_body = {"options": {
        "num_ctx": req.num_ctx,
        "num_predict": OH_NUM_PREDICT,
        "temperature": 0.3,
    }}
    think_flag = _resolve_think_flag(ollama_base, req.model, req.disable_thinking)
    if think_flag is False:
        # Top-level sibling of "options" — merged into the Ollama request
        # body by the same extra_body mechanism that delivers options.num_ctx.
        extra_body["think"] = False
    _llm_kwargs = dict(
        model=f"ollama_chat/{req.model}",
        api_key="ollama",
        base_url=ollama_base,
        temperature=0.3,
        timeout=OH_LLM_TIMEOUT_SECONDS,
        num_retries=2,
        drop_params=True,
        native_tool_calling=native_tc,
        litellm_extra_body=extra_body,
    )
    # SDK accepts `reasoning_effort` as a top-level kwarg; older builds may
    # not. Try with it; if rejected, retry without (SDK default is "high").
    try:
        llm = _LLM(reasoning_effort=_re, **_llm_kwargs)
        print(f"[OH-Worker] reasoning_effort={_re}"
              f" think={'off' if think_flag is False else 'model-default'}{tag}")
    except TypeError:
        print(f"[OH-Worker] SDK rejected reasoning_effort kwarg — using default{tag}")
        llm = _LLM(**_llm_kwargs)

    tools = [
        _Tool(name="terminal"),
        _Tool(name="file_editor"),
        _Tool(name="glob"),
        _Tool(name="grep"),
    ]
    # Exclude the SDK-default ThinkTool: reasoning models double-think with
    # it, and a live build burned 4 straight rounds on think spam before
    # writing a single file. FinishTool must stay (only way a run ends).
    try:
        agent = _Agent(llm=llm, tools=tools, include_default_tools=["FinishTool"])
    except Exception as agent_err:
        print(f"[OH-Worker] include_default_tools kwarg rejected "
              f"({type(agent_err).__name__}) — using SDK default toolset{tag}")
        agent = _Agent(llm=llm, tools=tools)
    return llm, agent

# Load persisted cache on startup; discard stale-version caches wholesale.
try:
    if CACHE_PATH.exists():
        _raw_cache = json.loads(CACHE_PATH.read_text())
        if isinstance(_raw_cache, dict) and _raw_cache.get("_v") == _TOOL_CACHE_VERSION:
            _tool_support_cache = _raw_cache.get("entries", {})
            print(f"[OH-Worker] Loaded {len(_tool_support_cache)} cached tool support entries")
        else:
            print("[OH-Worker] Tool cache version mismatch — discarding stale entries")
except Exception as e:
    print(f"[OH-Worker] Failed to load tool cache: {e}")


def _persist_tool_cache():
    """Write tool support cache to disk."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps({"_v": _TOOL_CACHE_VERSION, "entries": _tool_support_cache}))
    except Exception as e:
        print(f"[OH-Worker] Failed to persist tool cache: {e}")


# Serializes evict+preload: without it, two overlapping runs with different
# num_ctx (or models) evict each other's actively-used model mid-run.
_ENSURE_LOADED_LOCK = threading.Lock()


def _ensure_loaded(ollama_base: str, model: str, num_ctx: int) -> None:
    """Force-load `model` into Ollama's VRAM with exactly `num_ctx` context.

    Why this exists: litellm's ollama_chat provider doesn't reliably forward
    options.num_ctx, and once Ollama has a model loaded with one num_ctx, it
    keeps that loaded instance for subsequent requests regardless of what they
    ask for. To make the user-set num_ctx authoritative, we evict any existing
    load with the wrong context and preload with the desired value before the
    agent run starts. Best-effort — failures don't block the run.
    """
    import requests

    with _ENSURE_LOADED_LOCK:
        _ensure_loaded_inner(ollama_base, model, num_ctx, requests)


def _ensure_loaded_inner(ollama_base: str, model: str, num_ctx: int, requests) -> None:
    try:
        ps = requests.get(f"{ollama_base}/api/ps", timeout=5).json()
        for m in (ps.get("models") or []):
            if m.get("name") == model:
                cur = m.get("context_length")
                if cur == num_ctx:
                    print(f"[OH-Worker] {model} already loaded at num_ctx={num_ctx}, skipping preload")
                    return
                print(f"[OH-Worker] {model} loaded at num_ctx={cur}, evicting to reload at {num_ctx}")
                break
    except Exception as e:
        print(f"[OH-Worker] _ensure_loaded ps check failed (non-fatal): {e}")

    # Evict (best-effort) so the next load picks up the new num_ctx.
    try:
        requests.post(
            f"{ollama_base}/api/generate",
            json={"model": model, "keep_alive": 0},
            timeout=30,
        )
    except Exception as e:
        print(f"[OH-Worker] _ensure_loaded evict failed (non-fatal): {e}")

    # Preload with empty prompt + the user's num_ctx. This is what binds the
    # loaded instance to the requested context — Ollama caches options on first
    # load. Empty prompt makes this nearly free aside from the model swap.
    try:
        requests.post(
            f"{ollama_base}/api/generate",
            json={
                "model": model,
                "prompt": "",
                "options": {"num_ctx": num_ctx},
                "keep_alive": "10m",
            },
            timeout=180,
        )
        print(f"[OH-Worker] Preloaded {model} at num_ctx={num_ctx}")
    except Exception as e:
        print(f"[OH-Worker] _ensure_loaded preload failed (non-fatal): {e}")


def _aider_bin() -> str:
    configured = os.getenv("AIDER_BIN", "").strip()
    if configured:
        return configured
    candidates = [
        "/opt/openhands-worker/aider-venv/bin/aider",
        "/root/aider-venv/bin/aider",
        shutil.which("aider") or "",
    ]
    return next((c for c in candidates if c and Path(c).exists()), "aider")


def _tail(text: str, limit: int = 6000) -> str:
    text = text or ""
    return text[-limit:] if len(text) > limit else text


def _run_git(project_dir: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _ensure_git_baseline(project_dir: Path) -> None:
    """Create a local-only baseline commit if the uploaded project lacks git."""
    _run_git(project_dir, ["config", "--global", "--add", "safe.directory", str(project_dir)], timeout=10)
    if (project_dir / ".git").is_dir():
        return
    _run_git(project_dir, ["init"], timeout=20)
    _run_git(project_dir, ["config", "user.email", "hyprchat.local"], timeout=10)
    _run_git(project_dir, ["config", "user.name", "HyprChat"], timeout=10)
    _run_git(project_dir, ["add", "-A"], timeout=30)
    _run_git(project_dir, ["commit", "--allow-empty", "-m", "Baseline upload"], timeout=60)


def _changed_files(project_dir: Path) -> list[str]:
    try:
        r = _run_git(project_dir, ["status", "--porcelain"], timeout=20)
    except Exception:
        return []
    files = []
    for line in (r.stdout or "").splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path and path not in files:
            files.append(path)
    return files


def _git_diff(project_dir: Path) -> str:
    parts = []
    for args in (["diff", "--no-ext-diff"], ["diff", "--cached", "--no-ext-diff"]):
        try:
            r = _run_git(project_dir, args, timeout=60)
            if r.stdout:
                parts.append(r.stdout)
        except Exception as e:
            parts.append(f"(git {' '.join(args)} failed: {e})")
    return _tail("\n".join(parts), 50000)


def _run_shell_capture(project_dir: Path, command: str, env: dict,
                       timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=str(project_dir),
        env=env,
        shell=True,
        executable="/bin/bash",
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _safe_allowed_files(project_dir: Path, files: list[str]) -> list[str]:
    out = []
    root = project_dir.resolve()

    def _add_resolved(path: Path) -> None:
        try:
            resolved = path.resolve()
        except Exception:
            return
        if resolved == root or root in resolved.parents:
            rel = str(resolved.relative_to(root))
            if rel and rel not in out and resolved.is_file():
                out.append(rel)

    for raw in files or []:
        if not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = project_dir / candidate
        _add_resolved(candidate)
        if str(raw) in out:
            continue
        # Reviewer summaries often mention just "cli.py" or "commands.py".
        # Expand those basenames to real project paths so Aider can actually
        # load the relevant files instead of looping on "file is not uploaded".
        raw_name = Path(str(raw)).name
        if raw_name and raw_name == str(raw):
            for match in root.rglob(raw_name):
                if any(part in {".git", "__pycache__", ".venv", "venv"} for part in match.parts):
                    continue
                _add_resolved(match)
                if len(out) >= 80:
                    break
    return out


def _worker_python() -> str:
    preferred = Path("/root/venv/bin/python3")
    if preferred.exists():
        return str(preferred)
    return shutil.which("python3") or "python3"


def _normalize_worker_command(cmd: str) -> str:
    if not cmd:
        return ""
    py = _worker_python()
    # Stale upload contracts used bare `python`, but the worker only guarantees
    # python3/the Codebox venv. Replace shell command tokens without touching
    # python3, paths like /usr/bin/python, or prose in file names.
    return re.sub(r"(?<![\w./-])python(?=\s)", py, cmd)


def _missing_runner_failure(exit_code: int | None, stdout: str, stderr: str) -> bool:
    if exit_code not in {126, 127}:
        return False
    combined = f"{stdout}\n{stderr}".lower()
    return "python: not found" in combined or "command not found" in combined or "no such file" in combined


def _known_test_root_section(req: AiderRunRequest) -> str:
    issue_env = req.issue_envelope or {}
    stale_paths: list[str] = []
    for path in issue_env.get("stale_project_paths") or []:
        if path and path not in stale_paths:
            stale_paths.append(path)
    for issue in issue_env.get("issues") or []:
        for path in issue.get("stale_project_paths") or []:
            if path and path not in stale_paths:
                stale_paths.append(path)
        path = issue.get("stale_project_path")
        if path and path not in stale_paths:
            stale_paths.append(path)

    tail = (
        issue_env.get("test_stdout_tail")
        or issue_env.get("test_stderr_tail")
        or issue_env.get("stdout_tail")
        or issue_env.get("stderr_tail")
        or ""
    )
    stale_block = "\n".join(f"- {p}" for p in stale_paths) or "- (none detected)"
    tail_block = _tail(tail, 2500) if tail else "(none captured)"
    test_cmd = req.test_cmd or (req.contract or {}).get("aider_test_cmd") or (req.contract or {}).get("test_cmd") or "(none)"
    return f"""## Known Test Root
- Active project root: `{req.project_dir}`
- Test command: `{test_cmd}`
- Detected stale absolute project paths:
{stale_block}
- Latest test failure tail:
```
{tail_block}
```

If tests reference a different `/root/projects/...` directory than the active
project root, fix the test path or make the tests derive paths from the current
project root. Do not patch source code to satisfy behavior from a stale copied
project directory.
"""


def _test_state_isolation_section(req: AiderRunRequest) -> str:
    issue_env = req.issue_envelope or {}
    text_bits = [
        issue_env.get("summary") or "",
        issue_env.get("test_stdout_tail") or "",
        issue_env.get("test_stderr_tail") or "",
        issue_env.get("stdout_tail") or "",
        issue_env.get("stderr_tail") or "",
        " ".join(str(s) for s in issue_env.get("state_error_signals") or []),
    ]
    state_paths: list[str] = []
    for path in issue_env.get("state_paths") or []:
        if path and path not in state_paths:
            state_paths.append(path)
    for issue in issue_env.get("issues") or []:
        text_bits.extend([
            issue.get("summary") or "",
            " ".join(str(s) for s in issue.get("state_error_signals") or []),
            "test_isolation_suspected" if issue.get("test_isolation_suspected") else "",
        ])
        for path in issue.get("state_paths") or []:
            if path and path not in state_paths:
                state_paths.append(path)

    combined = "\n".join(text_bits)
    signals = []
    for pat in (
        r"no such column", r"no such table", r"database schema", r"schema mismatch",
        r"no item with that key", r"keyerror", r"duplicate key", r"unique constraint",
        r"stale (?:database|db|cache|state)", r"shared (?:database|db|cache|state)",
        r"test_isolation_suspected",
    ):
        if re.search(pat, combined, re.I):
            signals.append(pat.replace("(?:", "").replace("|", "/").replace(")", ""))
    if not signals and not state_paths:
        signals_block = "- (none detected)"
        paths_block = "- (none detected)"
    else:
        signals_block = "\n".join(f"- {s}" for s in signals[:8]) or "- (none detected)"
        paths_block = "\n".join(f"- {p}" for p in state_paths[:8]) or "- (none detected)"

    return f"""## Test State Isolation
Detected persistent-state/schema signals:
{signals_block}
Detected persistent state paths:
{paths_block}

When tests fail because a CLI, server, or integration path is reading old or
shared state, fix isolation at the storage/config/test boundary:
- Make the data/cache/state path configurable for tests (environment variable,
  config option, temp directory, in-memory store, or per-test database URL).
- Update tests or subprocess helpers to pass a fresh per-test state path.
- Make schema creation/migration idempotent for an existing store when that is
  part of the real app behavior.
- Do not rename existing command/business functions just to work around a stale
  database, cache, or fixture.
- Do not delete or depend on global user state such as home-directory app data.
"""


def _write_aider_prompt(req: AiderRunRequest, prompt_dir: Path) -> Path:
    prompt_dir.mkdir(parents=True, exist_ok=True)
    path = prompt_dir / f"{req.run_id or 'aider'}-prompt.md"
    issue_json = json.dumps(req.issue_envelope or {}, indent=2)[:12000]
    contract_json = json.dumps(req.contract or {}, indent=2)[:12000]
    allowed = "\n".join(f"- {p}" for p in (req.allowed_files or [])[:80]) or "(none specified)"
    known_root = _known_test_root_section(req)
    state_isolation = _test_state_isolation_section(req)
    content = f"""You are editing an existing project. Make the smallest coherent patch that satisfies the task.

## User Task
{req.task}

## Project Contract
```json
{contract_json}
```

## Issue Envelope / Prior Review
```json
{issue_json}
```

{known_root}

{state_isolation}

## Allowed / Relevant Files
{allowed}

Rules:
- Work from the existing repository. Do not rebuild the project from scratch.
- Prefer focused edits over broad rewrites.
- Preserve existing behavior unless the task or review envelope requires a change.
- If tests fail, fix the root cause and rerun the relevant command when possible.
- When the review reports stale `/root/projects/...` paths, prioritize the
  reviewer-provided test/path files in the allowed list before changing source.
- When the review reports storage/schema/state-isolation errors, prefer making
  state paths configurable and tests isolated over broad source/API renames.
- Do not create commits. Leave changes in the working tree for HyprChat to inspect.
"""
    path.write_text(content)
    return path


def _write_aider_model_settings(req: AiderRunRequest, prompt_dir: Path) -> Path:
    prompt_dir.mkdir(parents=True, exist_ok=True)
    path = prompt_dir / f"{req.run_id or 'aider'}-model-settings.yml"
    model = f"ollama_chat/{req.model}"
    path.write_text(
        "- name: aider/extra_params\n"
        "  extra_params:\n"
        f"    num_ctx: {int(req.num_ctx or 16384)}\n"
        f"- name: {model}\n"
        "  edit_format: diff\n"
        "  use_repo_map: true\n"
    )
    return path


def _build_aider_command(req: AiderRunRequest, prompt_file: Path,
                         model_settings_file: Path, allowed_files: list[str]) -> list[str]:
    cmd = [
        _aider_bin(),
        "--model", f"ollama_chat/{req.model}",
        "--yes",
        "--message-file", str(prompt_file),
        "--no-auto-commits",
        "--model-settings-file", str(model_settings_file),
    ]
    test_cmd = _normalize_worker_command(req.test_cmd)
    lint_cmd = _normalize_worker_command(req.lint_cmd)
    if req.auto_test and test_cmd:
        cmd += ["--test-cmd", test_cmd, "--auto-test"]
    if lint_cmd:
        cmd += ["--lint-cmd", lint_cmd]
    cmd += allowed_files
    return cmd


def _run_aider_blocking(req: AiderRunRequest, stream_cb=None) -> dict:
    start = time.time()
    project_dir = Path(req.project_dir)
    if not project_dir.is_dir():
        return {
            "status": "error", "summary": f"Project directory not found: {req.project_dir}",
            "project_dir": req.project_dir, "exit_code": -1,
            "duration_seconds": round(time.time() - start, 1),
        }

    try:
        _ensure_git_baseline(project_dir)
    except Exception as e:
        return {
            "status": "error", "summary": f"Could not initialize git baseline: {e}",
            "project_dir": str(project_dir), "exit_code": -1,
            "duration_seconds": round(time.time() - start, 1),
        }

    prompt_dir = Path("/tmp/hyprchat-aider")
    req.test_cmd = _normalize_worker_command(req.test_cmd)
    req.lint_cmd = _normalize_worker_command(req.lint_cmd)
    if isinstance(req.contract, dict):
        for key in ("build_cmd", "test_cmd", "smoke_cmd", "aider_test_cmd", "aider_lint_cmd"):
            if isinstance(req.contract.get(key), str):
                req.contract[key] = _normalize_worker_command(req.contract[key])
        if isinstance(req.contract.get("smoke_cmds"), list):
            req.contract["smoke_cmds"] = [
                _normalize_worker_command(c) if isinstance(c, str) else c
                for c in req.contract["smoke_cmds"]
            ]
    req.allowed_files = _safe_allowed_files(project_dir, req.allowed_files)
    prompt_file = _write_aider_prompt(req, prompt_dir)
    model_settings_file = _write_aider_model_settings(req, prompt_dir)
    cmd = _build_aider_command(req, prompt_file, model_settings_file, req.allowed_files)

    env = os.environ.copy()
    env["OLLAMA_API_BASE"] = req.ollama_url.rstrip("/")
    env.setdefault("AIDER_ANALYTICS_DISABLE", "true")

    cancel_event = threading.Event()
    # Register before Popen so a cancel arriving in the setup window isn't
    # lost as not_found; re-registered with the real process below.
    _register_aider_run(req.run_id, None, cancel_event)
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    repeat_counts: dict[str, int] = {}
    internal_stop = {"reason": ""}
    proc = None

    try:
        if stream_cb:
            stream_cb({"type": "step", "action": "starting", "detail": " ".join(shlex.quote(c) for c in cmd[:8])})
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        _register_aider_run(req.run_id, proc, cancel_event)

        def _reader(pipe, dest, label):
            try:
                for line in iter(pipe.readline, ""):
                    dest.append(line)
                    if stream_cb:
                        stream_cb({"type": "step", "action": label, "detail": line.rstrip()[:240]})
                    clean = line.strip()
                    if clean:
                        key = clean[:200]
                        repeat_counts[key] = repeat_counts.get(key, 0) + 1
                        if repeat_counts[key] > AIDER_REPEAT_LINE_LIMIT and not internal_stop["reason"]:
                            internal_stop["reason"] = f"repeated output: {key[:120]}"
                            cancel_event.set()
                            if stream_cb:
                                stream_cb({
                                    "type": "step",
                                    "action": "stopping",
                                    "detail": internal_stop["reason"],
                                })
                            break
                    if cancel_event.is_set():
                        break
            finally:
                try:
                    pipe.close()
                except Exception:
                    pass

        threads = [
            threading.Thread(target=_reader, args=(proc.stdout, stdout_lines, "stdout"), daemon=True),
            threading.Thread(target=_reader, args=(proc.stderr, stderr_lines, "stderr"), daemon=True),
        ]
        for t in threads:
            t.start()

        while proc.poll() is None:
            if time.time() - start > AIDER_MAX_SECONDS and not internal_stop["reason"]:
                internal_stop["reason"] = f"timeout after {AIDER_MAX_SECONDS}s"
                cancel_event.set()
            if cancel_event.is_set():
                _terminate_proc_tree(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _terminate_proc_tree(proc, signal.SIGKILL)
                break
            time.sleep(0.2)

        for t in threads:
            t.join(timeout=2)

        exit_code = proc.returncode if proc.returncode is not None else -1
        files = _changed_files(project_dir)
        diff = _git_diff(project_dir)
        test_exit = None
        test_stdout_tail = ""
        test_stderr_tail = ""
        if req.auto_test and req.test_cmd and exit_code == 0 and not cancel_event.is_set():
            if stream_cb:
                stream_cb({"type": "step", "action": "test", "detail": req.test_cmd[:240]})
            try:
                test_r = _run_shell_capture(project_dir, req.test_cmd, env, timeout=300)
                test_exit = test_r.returncode
                test_stdout_tail = _tail(test_r.stdout or "")
                test_stderr_tail = _tail(test_r.stderr or "")
                if stream_cb:
                    stream_cb({
                        "type": "step",
                        "action": "test_result",
                        "detail": f"exit={test_exit}",
                    })
            except subprocess.TimeoutExpired as e:
                test_exit = -1
                test_stdout_tail = _tail(e.stdout or "")
                test_stderr_tail = _tail((e.stderr or "") + "\nTest command timed out after 300s.")

        infra_test_failure = (
            bool(files)
            and exit_code == 0
            and _missing_runner_failure(test_exit, test_stdout_tail, test_stderr_tail)
        )
        status = (
            "error" if internal_stop["reason"]
            else "cancelled" if cancel_event.is_set()
            else "error" if exit_code != 0
            else "error" if test_exit is not None and test_exit != 0 and not infra_test_failure
            else "ok" if files
            else "no_changes"
        )
        summary = (
            f"Aider changed {len(files)} file(s)." if status == "ok"
            else "Aider completed without changes." if status == "no_changes"
            else "Aider cancelled by user." if status == "cancelled"
            else f"Aider stopped: {internal_stop['reason']}." if internal_stop["reason"]
            else f"Aider failed with exit {exit_code}." if exit_code != 0
            else f"Aider edits left test command failing with exit {test_exit}."
        )
        if infra_test_failure:
            summary = (
                f"Aider changed {len(files)} file(s); auto-test runner was unavailable "
                f"(exit {test_exit}), so Reviewer verification is still required."
            )
        return {
            "status": status,
            "summary": summary,
            "project_dir": str(project_dir),
            "files_touched": files,
            "diff": diff,
            "test_exit": test_exit,
            "test_stdout_tail": test_stdout_tail,
            "test_stderr_tail": test_stderr_tail,
            "stdout_tail": _tail("".join(stdout_lines)),
            "stderr_tail": _tail("".join(stderr_lines)),
            "exit_code": exit_code,
            "duration_seconds": round(time.time() - start, 1),
        }
    except FileNotFoundError:
        return {
            "status": "error",
            "summary": f"Aider executable not found: {_aider_bin()}",
            "project_dir": str(project_dir),
            "files_touched": [],
            "diff": "",
            "stdout_tail": "",
            "stderr_tail": "Install aider in the Codebox/OpenHands worker venv.",
            "exit_code": 127,
            "duration_seconds": round(time.time() - start, 1),
        }
    except Exception as e:
        return {
            "status": "error",
            "summary": f"{type(e).__name__}: {e}",
            "project_dir": str(project_dir),
            "files_touched": _changed_files(project_dir),
            "diff": _git_diff(project_dir),
            "stdout_tail": _tail("".join(stdout_lines)),
            "stderr_tail": _tail("".join(stderr_lines) + "\n" + traceback.format_exc()),
            "exit_code": -1,
            "duration_seconds": round(time.time() - start, 1),
        }
    finally:
        _deregister_aider_run(req.run_id)


@app.get("/aider/health")
def aider_health():
    binary = _aider_bin()
    installed = bool(shutil.which(binary) or Path(binary).exists())
    version = ""
    if installed:
        try:
            r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
            version = (r.stdout or r.stderr or "").strip()
        except Exception as e:
            version = f"version check failed: {e}"
    return {"status": "ok" if installed else "missing", "installed": installed, "binary": binary, "version": version}


@app.post("/aider/run", response_model=AiderRunResponse)
def run_aider(req: AiderRunRequest):
    return AiderRunResponse(**_run_aider_blocking(req))


@app.post("/aider/cancel/{run_id}")
def cancel_aider_run(run_id: str):
    with _ACTIVE_AIDER_RUNS_LOCK:
        entry = _ACTIVE_AIDER_RUNS.get(run_id)
    if not entry:
        return {"status": "not_found", "run_id": run_id}
    entry["cancel"].set()
    proc = entry.get("process")
    try:
        if proc and proc.poll() is None:
            _terminate_proc_tree(proc, signal.SIGTERM)
    except Exception as e:
        print(f"[Aider-Worker] terminate during cancel failed: {e}")
    return {"status": "cancelled", "run_id": run_id}


@app.post("/aider/run-stream")
async def run_aider_stream(req: AiderRunRequest):
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    result_holder = [None]

    def _send(data):
        try:
            loop.call_soon_threadsafe(queue.put_nowait, data)
        except Exception:
            pass

    def _run_sync():
        try:
            result_holder[0] = _run_aider_blocking(req, stream_cb=_send)
        finally:
            _send(None)

    async def generate():
        fut = loop.run_in_executor(None, _run_sync)
        finished = False
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Keepalive during silent Aider phases so proxies/read
                    # timeouts don't drop the stream (same as /run-stream).
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    finished = True
                    break
                yield f"data: {json.dumps(item)}\n\n"
            await fut
            if result_holder[0]:
                payload = {"type": "done", **result_holder[0]}
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            # Client disconnected mid-run (Stop, orchestrator crash/kill):
            # nobody is consuming, so cancel — the blocking runner's poll
            # loop sees the event and kills the aider process group.
            if not finished and not cancel_event.is_set():
                print(f"[Aider-Worker] stream consumer vanished for {req.run_id} — cancelling run")
                cancel_event.set()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _check_tool_support(ollama_base: str, model: str, num_ctx: int = 8192) -> bool:
    """Check if an Ollama model reliably returns structured tool_calls.

    Two stages:
      1. /api/show capabilities — no "tools" means prompt-based, full stop.
      2. Multi-line arg round-trip: ask the model to call a write_file tool
         whose file_text argument is a known 3-line body. Capabilities alone
         are NOT enough — merge models (Qwopus3.6) advertise tools yet emit
         `create` calls with the file_text argument missing, which burns
         builder rounds on "Parameter `file_text` is required" errors.

    `num_ctx` should be the run's context size so a cold probe doesn't load
    the model at the Modelfile default (262K on some imports → huge KV
    allocation) or at a size _ensure_loaded would immediately evict.
    Results are cached per model so the live test only runs once.
    """
    import requests

    cache_key = f"{ollama_base}:{model}"
    if cache_key in _tool_support_cache:
        cached = _tool_support_cache[cache_key]
        print(f"[OH-Worker] {model}: native_tool_calling={cached} (cached)")
        return cached

    # Quick capability/template check first — skip the live test if the model
    # clearly has no tool support. Ollama 0.30+ publishes a `capabilities`
    # list on /api/show; prefer it, because llama.cpp-backend models return a
    # degenerate "{{ .Prompt }}" template with no .Tools even when they fully
    # support native tool calling (e.g. gemma4).
    try:
        r = requests.post(f"{ollama_base}/api/show", json={"name": model}, timeout=5)
        if r.ok:
            body = r.json()
            caps = body.get("capabilities") or []
            if caps:
                if "tools" not in caps:
                    print(f"[OH-Worker] {model}: capabilities={caps} → prompt-based")
                    _tool_support_cache[cache_key] = False
                    _persist_tool_cache()
                    return False
                _caps_cache[f"{ollama_base}:{model}"] = caps
            elif ".Tools" not in body.get("template", ""):
                print(f"[OH-Worker] {model}: no .Tools in template → prompt-based")
                _tool_support_cache[cache_key] = False
                _persist_tool_cache()
                return False
    except Exception as e:
        # Transient (Ollama restarting, timeout): fall back to prompt-based
        # for THIS run but don't poison the persisted cache.
        print(f"[OH-Worker] Template check failed for {model}: {e}")
        return False

    # Live multi-line round-trip: the model must produce a structured
    # tool_call AND the multi-line file_text argument must survive intact.
    probe_body = "alpha line one\nbeta line two\ngamma line three"
    test_payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": (
                "Use the write_file tool to create /tmp/probe.txt. The "
                "file_text argument must be exactly these three lines:\n"
                f"{probe_body}\n"
                "Call the tool now — do not reply with text."
            ),
        }],
        "tools": [{
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write a text file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "file_text": {"type": "string",
                                      "description": "Complete file contents"},
                    },
                    "required": ["path", "file_text"],
                },
            },
        }],
        "stream": False,
        "options": {"num_ctx": max(2048, int(num_ctx or 8192)),
                    "num_predict": 512},
    }
    if "thinking" in _model_capabilities(ollama_base, model):
        test_payload["think"] = False

    saw_tool_call = False
    for attempt in (1, 2):
        try:
            r = requests.post(f"{ollama_base}/api/chat", json=test_payload, timeout=120)
            if not r.ok:
                print(f"[OH-Worker] {model}: probe HTTP {r.status_code} (attempt {attempt})")
                continue
            calls = (r.json().get("message") or {}).get("tool_calls") or []
            if not calls:
                continue
            saw_tool_call = True
            args = (calls[0].get("function") or {}).get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            file_text = str(args.get("file_text") or "")
            if "alpha line one" in file_text and file_text.count("\n") >= 2:
                print(f"[OH-Worker] {model}: multi-line arg probe OK → native")
                _tool_support_cache[cache_key] = True
                _persist_tool_cache()
                return True
            print(f"[OH-Worker] {model}: tool_call arrived but file_text was "
                  f"{'MISSING' if not file_text else 'mangled'} "
                  f"(attempt {attempt}) — args keys: {sorted(args)}")
        except Exception as e:
            # Transient failure — don't persist a False verdict.
            print(f"[OH-Worker] Probe attempt {attempt} failed for {model}: {e}")
            if attempt == 2 and not saw_tool_call:
                return False

    if saw_tool_call:
        # Structured calls arrive but multi-line args get dropped — the
        # exact failure that stalls builds. Persist prompt-based.
        print(f"[OH-Worker] {model}: drops multi-line tool args → prompt-based (persisted)")
        _tool_support_cache[cache_key] = False
        _persist_tool_cache()
        return False

    # No tool_calls at all on a capabilities-advertised model: known false
    # negative (some models answer probes in text but tool-call fine in real
    # agent loops). Trust capabilities, but don't persist — re-probe next
    # restart, and the runtime arg-drop demotion catches real offenders.
    print(f"[OH-Worker] {model}: probe got no tool_calls; capabilities say tools → "
          f"native (unpersisted)")
    _tool_support_cache[cache_key] = True
    return True


@app.post("/run", response_model=RunResponse)
def run_task(req: RunRequest):
    """Run an OpenHands coding agent on a task inside the sandbox."""
    global _run_counter
    _run_counter += 1
    if _run_counter % 10 == 0:
        _auto_cleanup_stale()
    _ensure_sdk()
    start = time.time()
    progress_log = []
    cancel_event = threading.Event()
    conversation = None
    # Register before the tool-support probe + model preload (can take
    # minutes) so a cancel in that window isn't lost as not_found.
    _register_run(req.run_id, None, cancel_event)

    try:
        ollama_base = req.ollama_url.rstrip("/")

        # ── Detect native tool support via live test ──
        # Some models (qwen3) return structured tool_calls → use native mode.
        # Others (qwen2.5-coder) put JSON in content text → use prompt-based.
        native_tc = _check_tool_support(ollama_base, req.model, num_ctx=req.num_ctx)

        # ── LLM + Agent (shared factory) ──
        # The user's num_ctx from HyprChat settings is authoritative — we don't
        # second-guess it based on task length / keywords. Force-load Ollama at
        # exactly that value; the factory also passes it nested under "options"
        # so litellm forwards it as options.num_ctx.
        _ensure_loaded(ollama_base, req.model, req.num_ctx)
        llm, agent = _make_llm_and_agent(req, ollama_base, native_tc)

        # ── Workspace setup ──
        work_dir, project_name, _reusing = _workspace_for_request(req)
        if _reusing:
            print(f"[OH-Worker] Reusing existing workspace: {work_dir}")
        else:
            print(f"[OH-Worker] Workspace: {work_dir} (project: {project_name})")

        # ── Build task prompt (concise — SDK provides its own system prompt) ──
        full_task = _build_task_prompt(req, str(work_dir), continuing=_reusing)

        # Snapshot filesystem before run
        pre_snapshot = _snapshot_workspace(work_dir)

        # ── Event callback for live progress tracking ──
        arg_drop = {"total": 0}

        def on_event(event):
            if cancel_event.is_set():
                raise _RunCancelled()
            try:
                step_info = _parse_event(event)
                if step_info:
                    progress_log.append(step_info)
                    action = step_info.get("action", "")
                    detail = step_info.get("detail", "")[:80]
                    if (action == "file_editor_result"
                            and _ARG_DROP_MARKER in step_info.get("detail", "")):
                        arg_drop["total"] += 1
                    print(f"[OH-Worker]   Step {len(progress_log)}: {action} — {detail}")
            except Exception:
                pass

        # ── Stuck detection — relaxed for slow local models ──
        _stuck_thresholds = None
        try:
            from openhands.sdk.conversation.types import StuckDetectionThresholds
            _stuck_thresholds = StuckDetectionThresholds(
                action_observation=8,
                action_error=6,
                monologue=8,
                alternating_pattern=12,
            )
        except ImportError:
            pass  # Older SDK version without threshold support

        # ── Run conversation ──
        conv_kwargs = dict(
            agent=agent,
            workspace=str(work_dir),
            max_iteration_per_run=req.max_rounds,
            stuck_detection=True,
            callbacks=[on_event],
            visualizer=None,
        )
        if _stuck_thresholds is not None:
            conv_kwargs["stuck_detection_thresholds"] = _stuck_thresholds
        conversation = _Conversation(**conv_kwargs)
        _register_run(req.run_id, conversation, cancel_event)

        conversation.send_message(full_task)
        print(f"[OH-Worker] Starting run (run_id={req.run_id or '-'}, max_rounds={req.max_rounds}, num_ctx={req.num_ctx})...")
        conversation.run()

        _nudges_used, _still_missing = _run_completion_nudges(
            conversation, work_dir, req.required_files, cancel_event=cancel_event,
            extra_note=_ARG_DROP_NOTE if arg_drop["total"] >= 2 else "",
        )
        _maybe_demote_native_tc(ollama_base, req.model, arg_drop["total"], _still_missing)

        status_str = str(conversation.state.execution_status)
        event_count = len(list(conversation.state.events))
        print(f"[OH-Worker] Run finished. Status: {status_str}, Events: {event_count}")

        # ── Check stuck ──
        stuck = False
        stuck_reason = ""
        if conversation.stuck_detector:
            try:
                if conversation.stuck_detector.is_stuck():
                    stuck = True
                    stuck_reason = getattr(conversation.stuck_detector, 'reason',
                                           'Agent detected stuck pattern')
                    print(f"[OH-Worker] Agent got STUCK: {stuck_reason}")
            except Exception as stuck_e:
                print(f"[OH-Worker] Stuck detection check failed: {stuck_e}")

        # ── Detect files created/modified ──
        files_created = _diff_snapshot(work_dir, pre_snapshot)

        all_workspace_files = _list_all_files(work_dir)
        print(f"[OH-Worker] All files in workspace ({len(all_workspace_files)}):")
        for wf in all_workspace_files[:30]:
            in_detected = "✓" if wf in files_created else "✗"
            print(f"[OH-Worker]   {in_detected} {wf}")

        # Fallback: use full listing if diff missed files
        if len(files_created) <= 1 and len(all_workspace_files) > len(files_created):
            print(f"[OH-Worker] Diff found {len(files_created)} but workspace has "
                  f"{len(all_workspace_files)} — using full listing")
            files_created = all_workspace_files

        # Second fallback: move stray files from /root/ into workspace
        if not files_created:
            import shutil
            root_files = []
            cutoff = start - 5
            for item in Path("/root").iterdir():
                if item.is_file() and not item.name.startswith("."):
                    try:
                        if item.stat().st_mtime >= cutoff:
                            root_files.append(item)
                    except OSError:
                        pass
            if root_files:
                print(f"[OH-Worker] Moving {len(root_files)} stray file(s) into {work_dir}")
                moved = []
                for rf in root_files:
                    dest = work_dir / rf.name
                    shutil.move(str(rf), str(dest))
                    moved.append(str(dest))
                    print(f"[OH-Worker]   Moved: {rf.name}")
                files_created = moved
                all_workspace_files = _list_all_files(work_dir)

        summary = _extract_summary(conversation)
        duration = time.time() - start
        # A run only counts as finished when the agent reached `finish` AND no
        # planned file is still missing after the nudge loop.
        agent_finished = _agent_finished(status_str, progress_log) and not _still_missing
        print(f"[OH-Worker] Done in {duration:.1f}s — {len(files_created)} files, "
              f"{len(progress_log)} steps, stuck={stuck}, finished={agent_finished}, "
              f"nudges={_nudges_used}, still_missing={len(_still_missing)}")

        return RunResponse(
            status="stuck" if stuck and not files_created else "ok",
            files_created=files_created,
            summary=summary,
            error=stuck_reason if stuck else "",
            duration_seconds=round(duration, 1),
            steps=progress_log[-20:],
            project_id=project_name,
            agent_finished=agent_finished,
            missing_required_files=_still_missing,
            arg_drop_detected=arg_drop["total"] >= 3,
        )

    except _RunCancelled:
        duration = time.time() - start
        print(f"[OH-Worker] Run cancelled (run_id={req.run_id or '-'}) after {duration:.1f}s")
        return RunResponse(
            status="cancelled",
            error="Run cancelled by client",
            duration_seconds=round(duration, 1),
            steps=progress_log[-20:],
        )
    except Exception as e:
        duration = time.time() - start
        tb = traceback.format_exc()
        print(f"[OH-Worker] Error after {duration:.1f}s: {e}")
        return RunResponse(
            status="error",
            error=f"{type(e).__name__}: {e}\n{tb}",
            duration_seconds=round(duration, 1),
            steps=progress_log[-20:],
        )
    finally:
        _deregister_run(req.run_id)
        if conversation is not None:
            try:
                conversation.close()
            except Exception as ce:
                print(f"[OH-Worker] conversation.close() failed (non-fatal): {ce}")


@app.post("/cancel/{run_id}")
def cancel_run(run_id: str):
    """Cancel an in-flight run by run_id. Idempotent: returns 200 even if not found."""
    with _ACTIVE_RUNS_LOCK:
        entry = _ACTIVE_RUNS.get(run_id)
    if not entry:
        return {"status": "not_found", "run_id": run_id}
    entry["cancel"].set()
    # Also call pause() so the SDK exits at the next iteration even if the
    # callback hasn't fired yet (e.g., model is mid-stream). close() runs in
    # the worker thread's finally block. conversation is None while the run
    # is still in model-preload — the event alone is enough then.
    try:
        if entry.get("conversation") is not None:
            entry["conversation"].pause()
    except Exception as e:
        print(f"[OH-Worker] pause() during cancel failed (non-fatal): {e}")
    print(f"[OH-Worker] Cancel signalled for run_id={run_id}")
    return {"status": "cancelled", "run_id": run_id}


@app.post("/run-stream")
async def run_task_stream(req: RunRequest):
    """SSE streaming version of /run — emits real-time progress events."""
    global _run_counter
    _run_counter += 1
    if _run_counter % 10 == 0:
        _auto_cleanup_stale()

    _ensure_sdk()
    start = time.time()
    progress_log = []
    step_counter = [0]
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    cancel_event = threading.Event()
    conversation_holder = [None]  # populated once Conversation is built
    # Register before the tool-support probe + model preload (can take
    # minutes) so a cancel in that window isn't lost as not_found.
    _register_run(req.run_id, None, cancel_event)

    def _send_sse(data):
        try:
            loop.call_soon_threadsafe(queue.put_nowait, data)
        except Exception:
            pass

    result_holder = [None]

    def _run_sync():
        """Synchronous function that runs in a thread."""
        try:
            ollama_base = req.ollama_url.rstrip("/")
            native_tc = _check_tool_support(ollama_base, req.model, num_ctx=req.num_ctx)
            # Force the loaded instance to match req.num_ctx — user setting wins
            # over Modelfile defaults and over whatever litellm forwards (or fails to forward).
            _ensure_loaded(ollama_base, req.model, req.num_ctx)

            llm, agent = _make_llm_and_agent(req, ollama_base, native_tc, tag=" (stream)")

            work_dir, project_name, _reusing = _workspace_for_request(req)

            full_task = _build_task_prompt(req, str(work_dir), continuing=_reusing)
            pre_snapshot = _snapshot_workspace(work_dir)

            arg_drop = {"total": 0}

            def on_event(event):
                # Cancellation check happens BEFORE the inner try/except so
                # _RunCancelled propagates out of conversation.run().
                if cancel_event.is_set():
                    raise _RunCancelled()
                try:
                    step_info = _parse_event(event)
                    if step_info:
                        step_counter[0] += 1
                        step_info["step"] = step_counter[0]
                        progress_log.append(step_info)
                        if (step_info.get("action") == "file_editor_result"
                                and _ARG_DROP_MARKER in step_info.get("detail", "")):
                            arg_drop["total"] += 1
                        _send_sse({
                            "type": "step",
                            "action": step_info.get("action", ""),
                            "detail": step_info.get("detail", "")[:120],
                            "step": step_counter[0],
                        })
                except Exception:
                    pass

            _stuck_thresholds = None
            try:
                from openhands.sdk.conversation.types import StuckDetectionThresholds
                _stuck_thresholds = StuckDetectionThresholds(
                    action_observation=8, action_error=6,
                    monologue=8, alternating_pattern=12,
                )
            except ImportError:
                pass

            conv_kwargs = dict(
                agent=agent,
                workspace=str(work_dir),
                max_iteration_per_run=req.max_rounds,
                stuck_detection=True,
                callbacks=[on_event],
                visualizer=None,
            )
            if _stuck_thresholds is not None:
                conv_kwargs["stuck_detection_thresholds"] = _stuck_thresholds
            conversation = _Conversation(**conv_kwargs)
            conversation_holder[0] = conversation
            _register_run(req.run_id, conversation, cancel_event)

            conversation.send_message(full_task)
            _send_sse({"type": "step", "action": "starting", "detail": f"Agent starting (max {req.max_rounds} rounds)...", "step": 0})

            print(f"[OH-Worker] Starting streamed run (run_id={req.run_id or '-'}, max_rounds={req.max_rounds}, num_ctx={req.num_ctx})...")
            conversation.run()

            def _nudge_sse(n, miss):
                _send_sse({
                    "type": "step", "action": "nudge",
                    "detail": (f"Completion nudge {n}/{OH_COMPLETION_NUDGES}: "
                               f"{len(miss)} planned file(s) missing — continuing build"),
                    "step": step_counter[0],
                })
            _nudges_used, _still_missing = _run_completion_nudges(
                conversation, work_dir, req.required_files,
                cancel_event=cancel_event, on_nudge=_nudge_sse,
                extra_note=_ARG_DROP_NOTE if arg_drop["total"] >= 2 else "",
            )
            _maybe_demote_native_tc(ollama_base, req.model, arg_drop["total"], _still_missing)
            _stream_status_str = str(conversation.state.execution_status)

            # Post-run processing
            stuck = False
            stuck_reason = ""
            if conversation.stuck_detector:
                try:
                    if conversation.stuck_detector.is_stuck():
                        stuck = True
                        stuck_reason = getattr(conversation.stuck_detector, 'reason',
                                               'Agent detected stuck pattern')
                except Exception:
                    pass

            files_created = _diff_snapshot(work_dir, pre_snapshot)
            all_workspace_files = _list_all_files(work_dir)
            if len(files_created) <= 1 and len(all_workspace_files) > len(files_created):
                files_created = all_workspace_files

            if not files_created:
                import shutil
                root_files = []
                cutoff = start - 5
                for item in Path("/root").iterdir():
                    if item.is_file() and not item.name.startswith("."):
                        try:
                            if item.stat().st_mtime >= cutoff:
                                root_files.append(item)
                        except OSError:
                            pass
                if root_files:
                    moved = []
                    for rf in root_files:
                        dest = work_dir / rf.name
                        shutil.move(str(rf), str(dest))
                        moved.append(str(dest))
                    files_created = moved
                    all_workspace_files = _list_all_files(work_dir)

            summary = _extract_summary(conversation)
            duration = time.time() - start
            # Finished = reached `finish` AND no planned file missing post-nudges.
            agent_finished = (_agent_finished(_stream_status_str, progress_log)
                              and not _still_missing)
            print(f"[OH-Worker] Streamed run done in {duration:.1f}s — {len(files_created)} files, "
                  f"stuck={stuck}, finished={agent_finished}, "
                  f"nudges={_nudges_used}, still_missing={len(_still_missing)}")

            result_holder[0] = {
                "type": "done",
                "status": "stuck" if stuck and not files_created else "ok",
                "files_created": files_created,
                "summary": summary,
                "error": stuck_reason if stuck else "",
                "duration_seconds": round(duration, 1),
                "steps": progress_log[-20:],
                "project_id": project_name,
                "agent_finished": agent_finished,
                "missing_required_files": _still_missing,
                "arg_drop_detected": arg_drop["total"] >= 3,
            }

        except _RunCancelled:
            duration = time.time() - start
            print(f"[OH-Worker] Run cancelled (run_id={req.run_id or '-'}) after {duration:.1f}s")
            result_holder[0] = {
                "type": "cancelled",
                "status": "cancelled",
                "error": "Run cancelled by client",
                "duration_seconds": round(duration, 1),
                "steps": progress_log[-20:],
            }
        except Exception as e:
            duration = time.time() - start
            result_holder[0] = {
                "type": "error",
                "error": f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
                "duration_seconds": round(duration, 1),
                "steps": progress_log[-20:],
            }
        finally:
            _deregister_run(req.run_id)
            conv = conversation_holder[0]
            if conv is not None:
                try:
                    conv.close()
                except Exception as ce:
                    print(f"[OH-Worker] conversation.close() failed (non-fatal): {ce}")

        # Signal completion
        _send_sse(None)

    async def generate():
        # Start the blocking run in a background thread
        run_future = loop.run_in_executor(None, _run_sync)

        finished = False
        try:
            # Stream events as they arrive
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                if item is None:
                    finished = True
                    break

                yield f"data: {json.dumps(item)}\n\n"

            await run_future

            if result_holder[0]:
                yield f"data: {json.dumps(result_holder[0])}\n\n"
        finally:
            # Client disconnected mid-run (Stop, orchestrator crash/kill):
            # cancel so the SDK loop exits at its next iteration instead of
            # running the build to completion for nobody.
            if not finished and not cancel_event.is_set():
                print(f"[OH-Worker] stream consumer vanished for {req.run_id} — cancelling run")
                cancel_event.set()
                conv = conversation_holder[0]
                if conv is not None:
                    try:
                        conv.pause()
                    except Exception as e:
                        print(f"[OH-Worker] pause() on disconnect failed (non-fatal): {e}")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _build_task_prompt(req: RunRequest, work_dir: str = "/root", continuing: bool = False) -> str:
    """Build the task prompt with verification requirements.

    Phase 5 — the prompt branches on `req.profile`:
      scaffold (default): create everything from scratch.
      continue:           resume — focus on the manifest_missing files.
      feature:            add to an existing tree — read before editing,
                          edit minimally, do NOT rewrite working files.
    """

    lang = req.language.lower()

    # Language-specific build/check commands
    _VERIFY_CMDS = {
        "java": "If using Maven: `mvn compile`. If plain javac: `javac -d out $(find . -name '*.java')`. Ensure pom.xml includes all needed dependencies with correct versions and native classifiers where required (e.g. LWJGL needs platform-specific natives).",
        "python": "`python -c \"import py_compile; import glob; [py_compile.compile(f, doraise=True) for f in glob.glob('**/*.py', recursive=True)]\"` to syntax-check all files. Then run the entry point or tests if present.",
        "javascript": "`node --check *.js` for syntax. If package.json exists: `npm install && npm run build` (or `npm test` if tests exist).",
        "typescript": "`npx tsc --noEmit` to type-check. If package.json exists: `npm install && npm run build`.",
        "c": "`gcc -Wall -Wextra -fsyntax-only *.c` to check, then `make` or compile and run.",
        "cpp": "`g++ -Wall -Wextra -fsyntax-only *.cpp` to check, then `make` or compile and run.",
        "csharp": "`export PATH=\"$PATH:/root/.dotnet\" && dotnet build -nologo -v q` must exit 0. Run `dotnet test` if a test project exists.",
        "rust": "`cargo check` (or `cargo build` if no Cargo.toml, use `rustc --edition 2021 *.rs`).",
        "go": "`go build ./...` to compile all packages.",
    }

    verify_cmd = _VERIFY_CMDS.get(lang, f"Run the appropriate syntax check / compiler for {lang}.")

    profile = (req.profile or "scaffold").lower()

    # Profile-specific opening directives. The base WORKSPACE/CONTEXT/TASK/
    # VERIFICATION blocks are shared.
    if profile == "continue":
        missing_block = ""
        if req.manifest_missing:
            missing_lines = "\n".join(f"  - {p}" for p in req.manifest_missing[:30])
            missing_block = (
                f"\n## MANIFEST FILES STILL MISSING (focus on these)\n"
                f"{missing_lines}\n"
            )
        opening = f"""## PROFILE: continue
This is a CONTINUATION of an earlier scaffold run that didn't finish. The
project tree at `{work_dir}` already has most files written; you are filling
in what's missing. Do NOT rewrite or restructure files that already exist.
Read them with `cat` if you need to understand interfaces, then write only
the missing pieces.{missing_block}
"""
    elif profile == "feature":
        opening = f"""## PROFILE: feature
This is an EXISTING, working project at `{work_dir}`. The user is asking for
an additive change. Your job is to make the SMALLEST coherent edit that
satisfies the task — not to rebuild or refactor the project.

Required first steps:
  1. `find {work_dir} -type f -not -path '*/target/*' -not -path '*/build/*' \\
        -not -path '*/node_modules/*' -not -path '*/.git/*' | head -50`
     to see the existing tree.
  2. Read the files most relevant to the task before editing them.
  3. Edit only what's needed. Add new files only if the change genuinely
     requires new modules. Do NOT regenerate working files just to "tidy" them.
  4. Re-run the project's build/test command after your edits to confirm
     nothing regressed.

If the task is genuinely large enough that a refactor is required, say so
in your finish message and identify the affected files explicitly.
"""
    else:  # scaffold (default)
        opening = f"""## PROFILE: scaffold
Create ALL files from scratch at `{work_dir}`. The TASK below describes the
target project; build it.
"""

    prompt = opening + f"""
## WORKSPACE
Working directory: `{work_dir}` — {'continue working on existing files here' if (continuing or profile in ('continue', 'feature')) else 'create ALL files here'}.
Language: {req.language}
IMPORTANT: ALL files MUST live inside `{work_dir}`. Do NOT create files in /root/ directly. Use `{work_dir}/` as the base path for everything.
"""

    if req.context:
        prompt += f"\n## CONTEXT\n{req.context}\n"

    git_step = (
        "5. **Git**: `git init && git add -A && git commit -m 'Initial commit'` "
        "after the project compiles."
        if profile == "scaffold" else ""
    )

    prompt += f"""
## TASK
{req.task}

## VERIFICATION (REQUIRED BEFORE finish)
You MUST complete every step before calling `finish`. `finish` is a gate, not a goal.

1. **Inventory**: Run `ls -R {work_dir}` and confirm every file the TASK calls for exists. Missing any → you are not done.
2. **Compile / parse**: {verify_cmd}
3. **Resolve missing references**: If step 2 fails because a referenced class, module, function, type, or import does not exist, you MUST create the missing file(s) with their full contents and re-run step 2. Do NOT stub them out, do NOT call `finish` with unresolved references.
4. **Dependencies**: Make sure imports reference real libraries (declared in pom.xml/package.json/requirements.txt etc.) or files you created.
{git_step}

Only call `finish` after step 2 exits 0 with every expected file present. Calling `finish` early — with compile errors, missing files, or unresolved references — counts as task failure.

NEVER end your turn with a plain text message. Every response must be a tool call; the ONLY way to stop is the `finish` tool. A text-only reply (e.g. "Now let me create the frontend files:") aborts the build.

When creating a file with `file_editor`, you MUST pass the complete file content in the `file_text` parameter of that same call — never call `create` with a path alone.
"""

    return prompt




def _parse_event(event) -> dict | None:
    """Parse an OpenHands event into a progress step dict."""
    info = {}

    if hasattr(event, 'tool_name') and event.tool_name:
        tool = event.tool_name
        if hasattr(event, 'action') and event.action:
            # ActionEvent — tool call being made
            action_obj = event.action
            if tool in ("TerminalTool", "terminal"):
                cmd = getattr(action_obj, 'command', '') or ''
                info = {"action": "terminal", "detail": cmd[:200]}
            elif tool in ("FileEditorTool", "file_editor"):
                path = getattr(action_obj, 'path', '') or ''
                command = getattr(action_obj, 'command', 'create') or 'create'
                info = {"action": f"file_{command}", "detail": str(path)}
            elif tool in ("GlobTool", "glob"):
                pattern = getattr(action_obj, 'pattern', '') or ''
                info = {"action": "glob", "detail": pattern[:200]}
            elif tool in ("GrepTool", "grep"):
                pattern = getattr(action_obj, 'pattern', '') or ''
                info = {"action": "grep", "detail": pattern[:200]}
            elif tool in ("finish",):
                msg = getattr(action_obj, 'message', '') or ''
                info = {"action": "finish", "detail": msg[:200]}
            else:
                info = {"action": tool, "detail": str(action_obj)[:200]}
        elif hasattr(event, 'observation') and event.observation:
            # ObservationEvent — tool result
            obs = event.observation
            text = getattr(obs, 'text', '') or str(obs)
            info = {"action": f"{tool}_result", "detail": text[:200]}

    elif hasattr(event, 'llm_message'):
        try:
            content = event.llm_message.content
            if content:
                for block in content:
                    if hasattr(block, 'text') and block.text:
                        text = block.text[:200]
                        if text.strip():
                            info = {"action": "thinking", "detail": text}
                        break
        except Exception:
            pass

    if info:
        info["step"] = int(time.time())
        return info
    return None


# ── Filesystem snapshot for file detection ──

_IGNORE_DIRS = {
    "node_modules", ".git", "__pycache__", ".cache", ".npm", ".cargo",
    ".rustup", "venv", ".venv", ".dotnet", ".nuget", ".julia", ".juliaup",
    ".choosenim", ".nimble", ".vmodules", ".m2", ".dart-tool", ".scalac",
    ".openhands", ".ssh", ".config", "bin",
}
_IGNORE_SUFFIXES = (".pyc", ".pyo", ".o", ".so", ".dylib")
_IGNORE_NAMES = (".bash_history", ".lesshst", ".wget-hsts", ".selected_editor",
                  "package-lock.json", ".install-*.log")


def _should_ignore(parts: tuple[str, ...], name: str) -> bool:
    if any(p in _IGNORE_DIRS for p in parts):
        return True
    if any(p.endswith(".dist-info") for p in parts):
        return True
    if parts and parts[0].startswith("."):
        return True
    if any(name.endswith(s) for s in _IGNORE_SUFFIXES):
        return True
    if name in _IGNORE_NAMES:
        return True
    return False


def _snapshot_workspace(work_dir: Path) -> dict[str, float]:
    snapshot = {}
    try:
        for item in work_dir.rglob("*"):
            if item.is_file():
                parts = item.relative_to(work_dir).parts
                if not parts or _should_ignore(parts, item.name):
                    continue
                try:
                    snapshot[str(item)] = item.stat().st_mtime
                except OSError:
                    pass
    except Exception as e:
        print(f"[OH-Worker] Snapshot error: {e}")
    return snapshot


def _diff_snapshot(work_dir: Path, pre_snapshot: dict[str, float]) -> list[str]:
    new_files = []
    try:
        for item in work_dir.rglob("*"):
            if item.is_file():
                parts = item.relative_to(work_dir).parts
                if not parts or _should_ignore(parts, item.name):
                    continue
                path_str = str(item)
                try:
                    mtime = item.stat().st_mtime
                except OSError:
                    continue
                if path_str not in pre_snapshot or mtime > pre_snapshot[path_str]:
                    new_files.append(path_str)
    except Exception as e:
        print(f"[OH-Worker] Diff error: {e}")
    return sorted(new_files)


def _list_all_files(scan_dir: Path, exclude_dir: Path | None = None) -> list[str]:
    files = []
    try:
        for item in scan_dir.rglob("*"):
            if not item.is_file():
                continue
            if exclude_dir and str(item).startswith(str(exclude_dir)):
                continue
            try:
                parts = item.relative_to(scan_dir).parts
            except ValueError:
                continue
            if not parts or _should_ignore(parts, item.name):
                continue
            files.append(str(item))
    except Exception as e:
        print(f"[OH-Worker] List files error: {e}")
    return sorted(files)


def _extract_summary(conversation) -> str:
    try:
        for event in reversed(list(conversation.state.events)):
            if hasattr(event, "llm_message") and hasattr(event.llm_message, "content"):
                for content_block in event.llm_message.content:
                    if hasattr(content_block, "text") and content_block.text:
                        text = content_block.text.strip()
                        if len(text) > 20:
                            return text[:500]
    except Exception:
        pass
    return "Code generated and tested."


_run_counter = 0


def _auto_cleanup_stale():
    """Delete project directories older than 24 hours."""
    import shutil
    cutoff = time.time() - 86400  # 24 hours
    deleted = 0
    try:
        if PROJECTS_DIR.exists():
            for item in PROJECTS_DIR.iterdir():
                if item.is_dir():
                    try:
                        if item.stat().st_mtime < cutoff:
                            shutil.rmtree(item)
                            deleted += 1
                    except Exception as e:
                        print(f"[OH-Worker] Stale cleanup error for {item.name}: {e}")
    except Exception as e:
        print(f"[OH-Worker] Stale cleanup scan error: {e}")
    if deleted:
        print(f"[OH-Worker] Auto-cleaned {deleted} stale project(s)")
    return deleted


@app.post("/clean-stale")
def clean_stale():
    """Delete projects older than 24 hours."""
    deleted = _auto_cleanup_stale()
    return {"deleted": deleted}


@app.get("/health")
def health():
    return {"status": "ok", "sdk_loaded": _sdk_loaded}


@app.post("/clean")
def clean_workspace():
    """Delete all project files from /root/projects/ and stray project files from /root/."""
    import shutil
    deleted = 0
    freed = 0
    errors = []

    # Dotfiles and system dirs to NEVER delete
    _KEEP = {
        ".bash_history", ".bashrc", ".profile", ".selected_editor",
        ".wget-hsts", ".lesshst", ".install-29c4ce8f.log",
        ".ssh", ".cache", ".config", ".local", ".cargo", ".rustup",
        ".choosenim", ".nimble", ".vmodules", ".dart-tool", ".dotnet",
        ".julia", ".juliaup", ".m2", ".npm", ".nuget", ".openhands",
        ".scalac", "bin", "venv",
    }

    # 1) Clean /root/projects/
    try:
        if PROJECTS_DIR.exists():
            for item in PROJECTS_DIR.iterdir():
                try:
                    size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file()) if item.is_dir() else item.stat().st_size
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                    deleted += 1
                    freed += size
                except Exception as e:
                    errors.append(f"projects/{item.name}: {e}")
    except Exception as e:
        errors.append(f"projects: {e}")

    # 2) Clean stray user files from /root/ (not dotfiles/system)
    root = Path("/root")
    try:
        for item in root.iterdir():
            if item.name in _KEEP or item.name.startswith("."):
                continue
            if item.name == "projects":
                continue  # already handled above
            try:
                size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file()) if item.is_dir() else item.stat().st_size
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
                deleted += 1
                freed += size
            except Exception as e:
                errors.append(f"{item.name}: {e}")
    except Exception as e:
        errors.append(f"root: {e}")

    print(f"[OH-Worker] Clean complete: {deleted} items, freed {freed // 1024} KB")
    return {"deleted": deleted, "freed_bytes": freed, "errors": errors}


if __name__ == "__main__":
    import uvicorn
    # Intentional 0.0.0.0 bind: this worker runs inside the Codebox LXC and must be
    # reachable from the HyprChat host. Lock down at the network/firewall layer instead.
    uvicorn.run(app, host="0.0.0.0", port=8586)
