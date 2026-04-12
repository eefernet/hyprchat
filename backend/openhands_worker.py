"""
OpenHands Worker — runs inside the CodeBox LXC container.
Receives coding tasks from HyprChat, runs an OpenHands agent loop,
and returns results with real-time progress. Listens on port 8586.

Deploy to CodeBox LXC at /opt/openhands-worker/openhands_worker.py
"""
import asyncio
import json
import os
import time
import traceback
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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


class RunRequest(BaseModel):
    task: str
    model: str = "qwen2.5:14b"
    ollama_url: str = "http://192.168.1.110:11434"
    max_rounds: int = 20
    num_ctx: int = 16384
    language: str = "python"
    context: str = ""
    project_id: str = ""


class RunResponse(BaseModel):
    status: str          # "ok", "error", "stuck"
    files_created: list[str] = []
    summary: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    steps: list[dict] = []
    project_id: str = ""


CACHE_PATH = Path("/opt/openhands-worker/.tool_cache.json")
_tool_support_cache: dict[str, bool] = {}

# Load persisted cache on startup
try:
    if CACHE_PATH.exists():
        _tool_support_cache = json.loads(CACHE_PATH.read_text())
        print(f"[OH-Worker] Loaded {len(_tool_support_cache)} cached tool support entries")
except Exception as e:
    print(f"[OH-Worker] Failed to load tool cache: {e}")


def _persist_tool_cache():
    """Write tool support cache to disk."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(_tool_support_cache))
    except Exception as e:
        print(f"[OH-Worker] Failed to persist tool cache: {e}")


def _check_tool_support(ollama_base: str, model: str) -> bool:
    """Check if an Ollama model actually returns structured tool_calls.

    Sends a minimal test request with a dummy tool. If the response contains
    a 'tool_calls' field, native tool calling works. If the model puts the
    tool call as JSON text in 'content' instead, it doesn't truly support
    structured tool calls and we fall back to prompt-based.
    Results are cached per model so the live test only runs once.
    """
    import requests

    cache_key = f"{ollama_base}:{model}"
    if cache_key in _tool_support_cache:
        cached = _tool_support_cache[cache_key]
        print(f"[OH-Worker] {model}: native_tool_calling={cached} (cached)")
        return cached

    # Quick template check first — skip the live test if no .Tools at all
    try:
        r = requests.post(f"{ollama_base}/api/show", json={"name": model}, timeout=5)
        if r.ok:
            template = r.json().get("template", "")
            if ".Tools" not in template:
                print(f"[OH-Worker] {model}: no .Tools in template → prompt-based")
                _tool_support_cache[cache_key] = False
                _persist_tool_cache()
                return False
    except Exception as e:
        print(f"[OH-Worker] Template check failed for {model}: {e}")
        _tool_support_cache[cache_key] = False
        _persist_tool_cache()
        return False

    # Live test: send a trivial tool call and check for structured response
    try:
        test_payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Say hello"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "say",
                    "description": "Say a message",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            }],
            "stream": False,
        }
        r = requests.post(f"{ollama_base}/api/chat", json=test_payload, timeout=30)
        if r.ok:
            msg = r.json().get("message", {})
            has_tool_calls = bool(msg.get("tool_calls"))
            print(f"[OH-Worker] {model}: live tool test → "
                  f"{'structured tool_calls' if has_tool_calls else 'text JSON (no tool_calls)'}")
            _tool_support_cache[cache_key] = has_tool_calls
            _persist_tool_cache()
            return has_tool_calls
    except Exception as e:
        print(f"[OH-Worker] Live tool test failed for {model}: {e}")

    _tool_support_cache[cache_key] = False
    _persist_tool_cache()
    return False


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

    try:
        ollama_base = req.ollama_url.rstrip("/")

        # ── Detect native tool support via live test ──
        # Some models (qwen3) return structured tool_calls → use native mode.
        # Others (qwen2.5-coder) put JSON in content text → use prompt-based.
        native_tc = _check_tool_support(ollama_base, req.model)

        # ── LLM config (scale context for complex tasks) ──
        effective_ctx = _scale_num_ctx(req)
        llm = _LLM(
            model=f"ollama_chat/{req.model}",
            api_key="ollama",
            base_url=ollama_base,
            temperature=0.3,
            timeout=180,
            num_retries=2,
            drop_params=True,
            native_tool_calling=native_tc,
            litellm_extra_body={"num_ctx": effective_ctx},
        )
        if effective_ctx != req.num_ctx:
            print(f"[OH-Worker] Scaled num_ctx: {req.num_ctx} → {effective_ctx} (complex task)")

        # ── Agent with core tools ──
        tools = [
            _Tool(name="terminal"),
            _Tool(name="file_editor"),
            _Tool(name="glob"),
            _Tool(name="grep"),
        ]

        agent = _Agent(llm=llm, tools=tools)

        # ── Workspace setup ──
        import uuid
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        _reusing = False
        if req.project_id and (PROJECTS_DIR / req.project_id).is_dir():
            work_dir = PROJECTS_DIR / req.project_id
            project_name = req.project_id
            _reusing = True
            print(f"[OH-Worker] Reusing existing workspace: {work_dir}")
        else:
            project_name = _derive_project_name(req.task, req.language)
            work_dir = PROJECTS_DIR / project_name
            if work_dir.exists():
                work_dir = PROJECTS_DIR / f"{project_name}-{uuid.uuid4().hex[:4]}"
            work_dir.mkdir(parents=True, exist_ok=True)
            print(f"[OH-Worker] Workspace: {work_dir} (project: {project_name})")

        # ── Build task prompt (concise — SDK provides its own system prompt) ──
        full_task = _build_task_prompt(req, str(work_dir), continuing=_reusing)

        # Snapshot filesystem before run
        pre_snapshot = _snapshot_workspace(work_dir)

        # ── Event callback for live progress tracking ──
        def on_event(event):
            try:
                step_info = _parse_event(event)
                if step_info:
                    progress_log.append(step_info)
                    action = step_info.get("action", "")
                    detail = step_info.get("detail", "")[:80]
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

        conversation.send_message(full_task)
        print(f"[OH-Worker] Starting run (max_rounds={req.max_rounds}, num_ctx={req.num_ctx})...")
        conversation.run()

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

        summary = _extract_summary(conversation)
        duration = time.time() - start
        print(f"[OH-Worker] Done in {duration:.1f}s — {len(files_created)} files, "
              f"{len(progress_log)} steps, stuck={stuck}")

        return RunResponse(
            status="stuck" if stuck and not files_created else "ok",
            files_created=files_created,
            summary=summary,
            error=stuck_reason if stuck else "",
            duration_seconds=round(duration, 1),
            steps=progress_log[-20:],
            project_id=project_name,
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
            native_tc = _check_tool_support(ollama_base, req.model)

            llm = _LLM(
                model=f"ollama_chat/{req.model}",
                api_key="ollama",
                base_url=ollama_base,
                temperature=0.3,
                timeout=180,
                num_retries=2,
                drop_params=True,
                native_tool_calling=native_tc,
                litellm_extra_body={"num_ctx": req.num_ctx},
            )

            tools = [
                _Tool(name="terminal"),
                _Tool(name="file_editor"),
                _Tool(name="glob"),
                _Tool(name="grep"),
            ]
            agent = _Agent(llm=llm, tools=tools)

            import uuid
            PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
            _reusing = False
            if req.project_id and (PROJECTS_DIR / req.project_id).is_dir():
                work_dir = PROJECTS_DIR / req.project_id
                project_name = req.project_id
                _reusing = True
            else:
                project_name = _derive_project_name(req.task, req.language)
                work_dir = PROJECTS_DIR / project_name
                if work_dir.exists():
                    work_dir = PROJECTS_DIR / f"{project_name}-{uuid.uuid4().hex[:4]}"
                work_dir.mkdir(parents=True, exist_ok=True)

            full_task = _build_task_prompt(req, str(work_dir), continuing=_reusing)
            pre_snapshot = _snapshot_workspace(work_dir)

            def on_event(event):
                try:
                    step_info = _parse_event(event)
                    if step_info:
                        step_counter[0] += 1
                        step_info["step"] = step_counter[0]
                        progress_log.append(step_info)
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

            conversation.send_message(full_task)
            _send_sse({"type": "step", "action": "starting", "detail": f"Agent starting (max {req.max_rounds} rounds)...", "step": 0})

            print(f"[OH-Worker] Starting streamed run (max_rounds={req.max_rounds}, num_ctx={req.num_ctx})...")
            conversation.run()

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

            summary = _extract_summary(conversation)
            duration = time.time() - start

            result_holder[0] = {
                "type": "done",
                "status": "stuck" if stuck and not files_created else "ok",
                "files_created": files_created,
                "summary": summary,
                "error": stuck_reason if stuck else "",
                "duration_seconds": round(duration, 1),
                "steps": progress_log[-20:],
                "project_id": project_name,
            }

        except Exception as e:
            duration = time.time() - start
            result_holder[0] = {
                "type": "error",
                "error": f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
                "duration_seconds": round(duration, 1),
                "steps": progress_log[-20:],
            }

        # Signal completion
        _send_sse(None)

    async def generate():
        # Start the blocking run in a background thread
        run_future = loop.run_in_executor(None, _run_sync)

        # Stream events as they arrive
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue

            if item is None:
                break

            yield f"data: {json.dumps(item)}\n\n"

        await run_future

        if result_holder[0]:
            yield f"data: {json.dumps(result_holder[0])}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _build_task_prompt(req: RunRequest, work_dir: str = "/root", continuing: bool = False) -> str:
    """Build the task prompt with verification requirements."""

    lang = req.language.lower()

    # Language-specific build/check commands
    _VERIFY_CMDS = {
        "java": "If using Maven: `mvn compile`. If plain javac: `javac -d out $(find . -name '*.java')`. Ensure pom.xml includes all needed dependencies with correct versions and native classifiers where required (e.g. LWJGL needs platform-specific natives).",
        "python": "`python -c \"import py_compile; import glob; [py_compile.compile(f, doraise=True) for f in glob.glob('**/*.py', recursive=True)]\"` to syntax-check all files. Then run the entry point or tests if present.",
        "javascript": "`node --check *.js` for syntax. If package.json exists: `npm install && npm run build` (or `npm test` if tests exist).",
        "typescript": "`npx tsc --noEmit` to type-check. If package.json exists: `npm install && npm run build`.",
        "c": "`gcc -Wall -Wextra -fsyntax-only *.c` to check, then `make` or compile and run.",
        "cpp": "`g++ -Wall -Wextra -fsyntax-only *.cpp` to check, then `make` or compile and run.",
        "rust": "`cargo check` (or `cargo build` if no Cargo.toml, use `rustc --edition 2021 *.rs`).",
        "go": "`go build ./...` to compile all packages.",
    }

    verify_cmd = _VERIFY_CMDS.get(lang, f"Run the appropriate syntax check / compiler for {lang}.")

    prompt = f"""## WORKSPACE
Working directory: `{work_dir}` — {'continue working on existing files here' if continuing else 'create ALL files here'}.
Language: {req.language}
IMPORTANT: ALL files MUST be created inside `{work_dir}`. Do NOT create files in /root/ directly. Use `{work_dir}/` as the base path for everything.
"""

    if req.context:
        prompt += f"\n## CONTEXT\n{req.context}\n"

    prompt += f"""
## TASK
{req.task}

## VERIFICATION
After writing all code:
1. **Syntax check**: {verify_cmd}
2. **Fix errors**: If the check fails, fix the code and re-check.
3. **Dependencies**: Make sure all imports reference real libraries or files you created.
4. **Git**: Initialize a git repo with `git init && git add -A && git commit -m 'Initial commit'` after the project works.

Call `finish` once the code compiles/parses without errors.
"""

    return prompt


def _scale_num_ctx(req: RunRequest) -> int:
    """Scale num_ctx based on task complexity."""
    base = req.num_ctx
    task_len = len(req.task) + len(req.context)
    # Multi-file keywords suggest more context needed
    complex_keywords = ["multi-file", "full-stack", "microservice", "monorepo",
                        "database", "authentication", "frontend and backend",
                        "react", "vue", "angular", "django", "flask", "express"]
    is_complex = any(kw in req.task.lower() for kw in complex_keywords)
    if is_complex or task_len > 2000:
        return max(base, 32768)
    elif task_len > 1000:
        return max(base, 24576)
    return base


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
    uvicorn.run(app, host="0.0.0.0", port=8586)
