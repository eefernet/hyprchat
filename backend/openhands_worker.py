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

        # Create isolated workspace per run (avoids stale files from previous tasks)
        import uuid
        run_id = uuid.uuid4().hex[:8]
        work_dir = Path(f"/root/project-{run_id}")
        work_dir.mkdir(parents=True, exist_ok=True)
        print(f"[OH-Worker] Workspace: {work_dir}")

        # ── Expert system prompt tailored to the task ──
        full_task = _build_task_prompt(req, str(work_dir))

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

        # Debug: list ALL files in workspace (helps diagnose detection issues)
        all_workspace_files = _list_all_files(work_dir)
        print(f"[OH-Worker] All files in workspace ({len(all_workspace_files)}):")
        for wf in all_workspace_files[:30]:
            in_detected = "✓" if wf in files_created else "✗"
            print(f"[OH-Worker]   {in_detected} {wf}")
        if len(all_workspace_files) > 30:
            print(f"[OH-Worker]   ... and {len(all_workspace_files) - 30} more")

        # Fallback: if diff found very few files but workspace has more,
        # use the full listing (something went wrong with mtime detection)
        if len(files_created) <= 1 and len(all_workspace_files) > len(files_created):
            print(f"[OH-Worker] Diff found {len(files_created)} but workspace has {len(all_workspace_files)} — using full listing as fallback")
            files_created = all_workspace_files

        # Second fallback: if workspace is empty, scan /root/ for files
        # the agent may have created (ignored cd instruction)
        if not files_created:
            root_files = _list_all_files(Path("/root"), exclude_dir=work_dir)
            if root_files:
                print(f"[OH-Worker] Workspace empty but found {len(root_files)} files in /root/")
                files_created = root_files

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


_AGENT_SYSTEM_PROMPT = """\
You are CodeBot — an elite software engineer agent. You write production-quality code, \
test it, fix errors, and deliver complete working projects. You never explain what you \
will do — you just do it. You never put code in chat — you use the file editor and terminal.

## METHODOLOGY
1. PLAN the file structure first (mentally — don't waste a step writing plans)
2. WRITE all source files using the file editor — every single file the project needs
3. Create config/manifest files (package.json, Cargo.toml, go.mod, etc.)
4. Install dependencies ONLY AFTER all source files exist
5. Build/compile the project
6. TEST by running it — read errors carefully, fix root causes, re-test
7. Create a README.md with setup and run instructions
8. DONE — never start dev servers or long-running processes

## LANGUAGE REFERENCE

### Python
```
project/
├── main.py              # Entry point
├── requirements.txt     # pip dependencies
├── src/
│   ├── __init__.py
│   ├── models.py
│   └── utils.py
└── tests/
    └── test_main.py
```
- Install: `pip3 install -r requirements.txt`
- Test: `python3 main.py` or `pytest`
- For Flask/FastAPI: write the app but do NOT run the server
- For CLI tools: use argparse with sensible defaults, demo with hardcoded args

### JavaScript (Node.js / React / Vue)
```
project/
├── package.json         # MUST include all deps + scripts
├── vite.config.js       # Vite config with framework plugin
├── index.html           # MUST have <script type="module" src="/src/main.jsx">
├── src/
│   ├── main.jsx         # REQUIRED: ReactDOM.createRoot(document.getElementById('root')).render(<App />)
│   ├── App.jsx          # Root component
│   ├── App.css          # Styles (or use Tailwind via CDN/PostCSS)
│   └── components/
│       └── Widget.jsx
└── public/              # Static assets
```
- **React entry point is MANDATORY**: `src/main.jsx` must import React, ReactDOM, and App, then mount it
- **index.html script tag is MANDATORY**: `<script type="module" src="/src/main.jsx"></script>` inside `<body>`
- Install: `npm install`
- Build: `npm run build`
- Do NOT use `npm create`, `npx create-react-app`, or any scaffolding — write every file yourself
- Do NOT start dev servers (`npm run dev`, `npm start`)
- For vanilla JS (no framework): use a single HTML file with `<script>` tags, no bundler needed

### TypeScript
```
project/
├── package.json
├── tsconfig.json        # strict: true, jsx: react-jsx, module: ESNext
├── vite.config.ts
├── index.html           # <script type="module" src="/src/main.tsx">
├── src/
│   ├── main.tsx         # REQUIRED: mount React
│   ├── App.tsx
│   └── components/
└── public/
```
- Same rules as JavaScript but with .tsx/.ts extensions
- Include @types/react and @types/react-dom in devDependencies
- Pick ONE extension set: .ts/.tsx everywhere (never mix .js and .ts)

### Rust
```
project/
├── Cargo.toml
├── src/
│   ├── main.rs          # Entry point (fn main)
│   ├── lib.rs           # Library code (optional)
│   └── models.rs
└── tests/
    └── integration.rs
```
- Init: `cargo init` (creates Cargo.toml + src/main.rs)
- Add deps: edit Cargo.toml `[dependencies]` section
- Build: `cargo build`
- Test: `cargo run` then `cargo test`

### Go
```
project/
├── go.mod
├── main.go              # package main, func main()
├── internal/
│   └── handler.go
└── pkg/
    └── utils.go
```
- Init: `go mod init project`
- Add deps: `go get <package>` or they auto-resolve
- Build: `go build -o app .`
- Test: `go run .` then `go test ./...`

### C
```
project/
├── Makefile
├── src/
│   ├── main.c
│   ├── utils.c
│   └── utils.h
└── include/
    └── project.h
```
- Compile: `gcc -o app src/*.c -I include -Wall -Wextra`
- Or use Makefile: `make`
- Test: `./app`
- Link math: add `-lm`, pthreads: add `-lpthread`

### C++
```
project/
├── CMakeLists.txt       # Or Makefile
├── src/
│   ├── main.cpp
│   ├── app.cpp
│   └── app.hpp
└── include/
    └── project.hpp
```
- Compile: `g++ -o app src/*.cpp -I include -std=c++17 -Wall`
- Or CMake: `cmake -B build && cmake --build build`
- Test: `./app` or `./build/app`

### Java
```
project/
├── pom.xml              # Or build.gradle
├── src/main/java/
│   └── com/app/
│       ├── Main.java    # public static void main
│       └── Service.java
└── src/test/java/
```
- Simple: `javac src/main/java/com/app/*.java -d out && java -cp out com.app.Main`
- Maven: `mvn compile exec:java`
- Keep it simple — single-dir compilation unless Maven/Gradle is needed

## HARD RULES
1. NEVER use interactive scaffolding tools (npm create, create-react-app, cargo-generate, cookiecutter)
2. NEVER start long-running servers or processes
3. NEVER use interactive input (input(), readline, stdin prompts) — use hardcoded demo values
4. NEVER skip entry point files — every app needs a main (main.py, main.jsx, main.rs, main.go, Main.java, main.c)
5. ALWAYS install every dependency the code imports
6. ALWAYS test by running the code — if it fails, fix and re-test until it works
7. ALWAYS write files using the file editor, never echo/cat into files
8. Pick ONE language variant per project (.jsx OR .tsx, not both; .js config OR .ts config, not both)
9. For web apps: verify the build succeeds (`npm run build`, `cargo build`, `go build`) before finishing
10. Fix root causes — don't suppress errors with try/except or empty catch blocks
"""


def _build_task_prompt(req: RunRequest, work_dir: str = "/root") -> str:
    """Build the full prompt: system persona + task-specific instructions."""

    full_task = f"""{_AGENT_SYSTEM_PROMPT}

## YOUR TASK
{req.task}

## WORKSPACE
Your workspace is: {work_dir}
ALL files MUST go in this directory. `cd {work_dir}` first.
Language: {req.language}
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


def _list_all_files(scan_dir: Path, exclude_dir: Path | None = None) -> list[str]:
    """List all non-ignored files in a directory (ignores node_modules, .git, etc.)."""
    files = []
    try:
        for item in scan_dir.rglob("*"):
            if not item.is_file():
                continue
            # Skip files in the exclude directory
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
