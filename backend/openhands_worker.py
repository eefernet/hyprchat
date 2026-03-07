"""
OpenHands Worker — runs inside the CodeBox LXC container.
Receives coding tasks from HyprChat, runs an OpenHands agent loop,
and returns results. Listens on port 8586.

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
    # Import tool definitions to trigger registration
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
    max_rounds: int = 8
    language: str = "python"
    filename: str = ""
    context: str = ""


class RunResponse(BaseModel):
    status: str
    files_created: list[str] = []
    summary: str = ""
    error: str = ""
    duration_seconds: float = 0.0


@app.post("/run", response_model=RunResponse)
def run_task(req: RunRequest):
    """Run an OpenHands coding agent on a task inside the sandbox."""
    _ensure_sdk()
    start = time.time()

    try:
        # Create LLM pointing at Ollama
        llm = _LLM(
            model=f"openai/{req.model}",
            api_key="ollama",
            base_url=f"{req.ollama_url}/v1",
            temperature=0.2,
            timeout=180,
            num_retries=2,
            drop_params=True,
        )

        # Create agent with terminal + file editor
        agent = _Agent(
            llm=llm,
            tools=[
                _Tool(name="terminal"),
                _Tool(name="file_editor"),
            ],
        )

        # Build task prompt
        filename = req.filename or f"generated_{int(time.time())}.py"
        if not filename.startswith("/"):
            filename = f"/root/{filename}"

        full_task = (
            f"Write {req.language} code for this task: {req.task}\n\n"
            f"Save the code to: {filename}\n"
        )
        if req.context:
            full_task += f"\nContext/constraints: {req.context}\n"
        full_task += (
            "\nRules:\n"
            "- The script MUST work when run with NO arguments (no input(), no interactive)\n"
            "- Use hardcoded demo values that showcase all features\n"
            "- Install any needed packages with pip3\n"
            "- After writing the code, TEST it by running it\n"
            "- If there are errors, FIX them and test again\n"
            "- When the code works correctly, you're done\n"
        )

        # Create workspace dir
        work_dir = Path("/root")
        work_dir.mkdir(parents=True, exist_ok=True)

        # Run conversation
        conversation = _Conversation(
            agent=agent,
            workspace=str(work_dir),
            max_iteration_per_run=req.max_rounds,
            visualizer=None,
            stuck_detection=True,
        )
        conversation.send_message(full_task)
        conversation.run()

        # Extract files created — scan events for file writes
        files_created = _extract_files_created(conversation, filename)
        summary = _extract_summary(conversation)

        duration = time.time() - start
        return RunResponse(
            status="ok",
            files_created=files_created,
            summary=summary,
            duration_seconds=round(duration, 1),
        )

    except Exception as e:
        duration = time.time() - start
        tb = traceback.format_exc()
        return RunResponse(
            status="error",
            error=f"{type(e).__name__}: {e}\n{tb}",
            duration_seconds=round(duration, 1),
        )


def _extract_files_created(conversation, expected_file: str) -> list[str]:
    """Extract file paths that were created during the agent run."""
    files = set()
    # Always include the expected file
    files.add(expected_file)

    # Scan events for terminal commands that wrote files
    try:
        for event in conversation.state.events:
            # Look for file editor actions or write commands
            if hasattr(event, "tool_name") and event.tool_name == "FileEditorTool":
                if hasattr(event, "arguments"):
                    args = event.arguments if isinstance(event.arguments, dict) else {}
                    path = args.get("path", "")
                    if path:
                        files.add(path)
    except Exception:
        pass

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
