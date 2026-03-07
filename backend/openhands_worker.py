"""
OpenHands Worker — runs inside the CodeBox LXC container.
Receives coding tasks from HyprChat, runs an OpenHands agent loop,
and returns results with real-time progress. Listens on port 8586.

Deploy to CodeBox LXC at /opt/openhands-worker/openhands_worker.py
"""
import json
import os
import time
import traceback
from pathlib import Path

from fastapi import FastAPI
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
    from openhands.sdk import LLM, Agent, Conversation, Tool
    _LLM = LLM
    _Agent = Agent
    _Conversation = Conversation
    _Tool = Tool
    _sdk_loaded = True


class RunRequest(BaseModel):
    task: str
    model: str = "qwen2.5:14b"
    ollama_url: str = "http://192.168.1.110:11434"
    max_rounds: int = 12
    language: str = "python"
    context: str = ""


class RunResponse(BaseModel):
    status: str          # "ok", "error", "stuck"
    files_created: list[str] = []
    summary: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    steps: list[dict] = []  # Live progress log: [{step, action, detail}]


@app.post("/run", response_model=RunResponse)
def run_task(req: RunRequest):
    """Run an OpenHands coding agent on a task inside the sandbox."""
    _ensure_sdk()
    start = time.time()
    progress_log = []  # Accumulate agent steps for the response

    try:
        # ── LLM with tuned parameters for code generation ──
        llm = _LLM(
            model=f"openai/{req.model}",
            api_key="ollama",
            base_url=f"{req.ollama_url}/v1",
            temperature=0.15,       # Low for deterministic code
            timeout=180,
            num_retries=2,
            drop_params=True,
        )

        # ── Agent with all available tools ──
        tools = [
            _Tool(name="terminal"),
            _Tool(name="file_editor"),
        ]
        # Try to add task tracker if available
        try:
            tools.append(_Tool(name="task_tracker"))
        except Exception:
            pass

        agent = _Agent(llm=llm, tools=tools)

        # ── Expert system prompt tailored to the task ──
        full_task = _build_task_prompt(req)

        # Create workspace
        work_dir = Path("/root")
        work_dir.mkdir(parents=True, exist_ok=True)

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

        # ── Run conversation ──
        conv_kwargs = dict(
            agent=agent,
            workspace=str(work_dir),
            max_iteration_per_run=req.max_rounds,
            stuck_detection=True,
        )

        conversation = _Conversation(**conv_kwargs)

        # Register event callback if supported
        if hasattr(conversation, 'register_event_callback'):
            conversation.register_event_callback(on_event)

        conversation.send_message(full_task)
        conversation.run()

        # ── Check if agent got stuck ──
        stuck = False
        stuck_reason = ""
        if hasattr(conversation, 'stuck_detector') and conversation.stuck_detector:
            try:
                if conversation.stuck_detector.is_stuck():
                    stuck = True
                    stuck_reason = getattr(conversation.stuck_detector, 'reason', 'Agent detected stuck pattern')
                    print(f"[OH-Worker] Agent got STUCK: {stuck_reason}")
            except Exception:
                pass

        # ── Detect files created/modified ──
        files_created = _diff_snapshot(work_dir, pre_snapshot)
        summary = _extract_summary(conversation)

        duration = time.time() - start
        print(f"[OH-Worker] Done in {duration:.1f}s — {len(files_created)} files, {len(progress_log)} steps, stuck={stuck}")

        return RunResponse(
            status="stuck" if stuck and not files_created else "ok",
            files_created=files_created,
            summary=summary,
            error=stuck_reason if stuck else "",
            duration_seconds=round(duration, 1),
            steps=progress_log[-20:],  # Last 20 steps
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


def _build_task_prompt(req: RunRequest) -> str:
    """Build an expert-level task prompt for the OpenHands agent."""

    # Language-specific setup hints
    _LANG_HINTS = {
        "python": "Use pip3 to install packages. Use /root/venv if available. Test with: python3 <file>.",
        "javascript": "Use npm init -y, then npm install deps. For React: use Vite (npm create vite@latest). Build with: npm run build. Do NOT start dev servers.",
        "typescript": "Use npm init -y, add typescript + ts-node. Compile with tsc. For React: Vite + @vitejs/plugin-react.",
        "rust": "Use cargo init. Build with: cargo build. Test with: cargo run.",
        "go": "Use go mod init. Build with: go build. Test with: go run .",
        "java": "Create .java files. Compile: javac *.java. Run: java Main.",
        "c": "Write .c files. Compile: gcc -o output file.c. Run: ./output.",
        "cpp": "Write .cpp files. Compile: g++ -o output file.cpp -std=c++17. Run: ./output.",
    }

    lang = req.language.lower()
    lang_hint = _LANG_HINTS.get(lang, f"Use standard {req.language} tools to build and test.")

    full_task = f"""You are an expert {req.language} developer building a project from scratch.

## TASK
{req.task}

## WORKSPACE
All files go under /root/. Create a project subdirectory if building a multi-file project.

## INSTRUCTIONS
1. PLAN first: decide the file structure before writing any code
2. Create ALL necessary files: source code, config files, package manifests, etc.
3. Install dependencies: {lang_hint}
4. Build/compile the project if needed
5. TEST by running the code — if errors occur, read them carefully and FIX immediately
6. Verify the fix works before moving on
7. When everything works, you're DONE — do not start dev servers or interactive processes

## RULES
- NO interactive input: no input(), no readline, no prompts — use hardcoded demo values
- NO long-running servers: do NOT run npm start, npm run dev, flask run, uvicorn, etc.
- Create self-contained code that demonstrates all features when run
- Write clean, production-quality code with proper error handling
- If a test fails, fix the root cause — don't just suppress the error
"""

    if req.context:
        full_task += f"\n## ADDITIONAL CONTEXT\n{req.context}\n"

    return full_task


def _parse_event(event) -> dict | None:
    """Parse an OpenHands event into a progress step dict."""
    info = {}

    # Terminal tool calls
    if hasattr(event, 'tool_name'):
        if event.tool_name in ("TerminalTool", "terminal"):
            args = event.arguments if hasattr(event, 'arguments') and isinstance(event.arguments, dict) else {}
            cmd = args.get("command", "")
            info = {"action": "terminal", "detail": cmd[:200]}
        elif event.tool_name in ("FileEditorTool", "file_editor"):
            args = event.arguments if hasattr(event, 'arguments') and isinstance(event.arguments, dict) else {}
            path = args.get("path", "")
            command = args.get("command", "create")
            info = {"action": f"file_{command}", "detail": path}
        else:
            info = {"action": event.tool_name, "detail": str(getattr(event, 'arguments', ''))[:200]}

    # LLM messages (agent thinking)
    elif hasattr(event, 'llm_message'):
        try:
            content = event.llm_message.content
            if content:
                text = ""
                for block in content:
                    if hasattr(block, 'text') and block.text:
                        text = block.text[:200]
                        break
                if text:
                    info = {"action": "thinking", "detail": text}
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
    """Check if a file path should be ignored in scanning."""
    if any(p in _IGNORE_DIRS for p in parts):
        return True
    if any(p.endswith(".dist-info") for p in parts):
        return True
    # Ignore top-level hidden files and pip-installed packages
    if parts and parts[0].startswith("."):
        return True
    if any(name.endswith(s) for s in _IGNORE_SUFFIXES):
        return True
    if name in _IGNORE_NAMES:
        return True
    return False


def _snapshot_workspace(work_dir: Path) -> dict[str, float]:
    """Take a snapshot of file modification times in the workspace."""
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
    """Find files that were created or modified since the pre-snapshot."""
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


def _extract_summary(conversation) -> str:
    """Extract a brief summary from the last agent message."""
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


@app.get("/health")
def health():
    return {"status": "ok", "sdk_loaded": _sdk_loaded}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8586)
