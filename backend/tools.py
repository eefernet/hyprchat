"""
Tool definitions and execution dispatch for HyprChat's integrated CodeAgent.
"""
import asyncio
import base64
import json
import os
import re
import shlex
import time
import uuid
from datetime import datetime

import comfyui
import config
import context_policy
import database as db
import image_prompt_enhancer
import persona_images
import cancel_registry
from document_tools import DOCUMENT_TOOLS
from artifact_files import artifact_file_metadata as _artifact_file_metadata
from artifact_files import extract_indexable_text
from connectors import execute_connector_tool
from research import fetch_bytes_safely, run_deep_research, run_conspiracy_research, _fetch_page, _source_tier
from tooling.codebox_tools import (
    CODEBOX_TOOL_NAMES,
    build_custom_tool_code,
    pip_install_in_venv,
    pip_name_for_import,
    run_codebox_tool,
    run_custom_tool_code,
    tool_import_names,
)

# Strip ANSI escape codes from terminal output before feeding back to the model
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)

def _artifact_index_text(path: str, filename: str, kind: str, mime_type: str = "", max_chars: int = 500000) -> str:
    return extract_indexable_text(path, kind, mime_type, max_chars, filename=filename)

_PROJECT_ARCHIVE_TAR_EXCLUDE_PATTERNS = (
    ".pytest_cache", "*/.pytest_cache/*",
    "__pycache__", "*/__pycache__/*",
    "*.pyc",
    "*.egg-info", "*.egg-info/*",
    ".mypy_cache", "*/.mypy_cache/*",
    ".ruff_cache", "*/.ruff_cache/*",
    ".cache", "*/.cache/*",
    ".git", "*/.git/*",
    "node_modules", "*/node_modules/*",
    "dist", "*/dist/*",
    "build", "*/build/*",
    "target", "*/target/*",
    ".next", "*/.next/*",
    "venv", "*/venv/*",
    ".venv", "*/.venv/*",
    ".npm", "*/.npm/*",
    # Aider repair metadata is useful for the agent loop but must not ship.
    ".aider*", "./.aider*", "*/.aider*", "*/.aider*/*",
)

_PROJECT_ARCHIVE_FIND_EXCLUDE_TESTS = (
    ("path", "*/.pytest_cache/*"),
    ("path", "*/__pycache__/*"),
    ("name", "*.pyc"),
    ("path", "*.egg-info/*"),
    ("path", "*/.mypy_cache/*"),
    ("path", "*/.ruff_cache/*"),
    ("path", "*/.cache/*"),
    ("path", "*/.git/*"),
    ("path", "*/node_modules/*"),
    ("path", "*/dist/*"),
    ("path", "*/build/*"),
    ("path", "*/target/*"),
    ("path", "*/.next/*"),
    ("path", "*/venv/*"),
    ("path", "*/.venv/*"),
    ("path", "*/.npm/*"),
    ("path", "*/.aider*"),
    ("path", "*/.aider*/*"),
)

def _project_archive_tar_exclude_args() -> str:
    return " ".join(
        f"--exclude={shlex.quote(pattern)}"
        for pattern in _PROJECT_ARCHIVE_TAR_EXCLUDE_PATTERNS
    )

def _project_archive_find_include_filter() -> str:
    return " ".join(
        f"! -{kind} {shlex.quote(pattern)}"
        for kind, pattern in _PROJECT_ARCHIVE_FIND_EXCLUDE_TESTS
    )

def _project_archive_find_exclude_expr() -> str:
    return " -o ".join(
        f"-{kind} {shlex.quote(pattern)}"
        for kind, pattern in _PROJECT_ARCHIVE_FIND_EXCLUDE_TESTS
    )

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
_DELIVERY_SHIP_TOOLS = {"download_project", "download_file"}

async def _fire_artifact_created_event() -> None:
    """artifact_created event for event-triggered scheduled tasks. Call-time
    scheduler import (tools → scheduler is the safe direction); never raises."""
    try:
        import scheduler
        await scheduler.fire_event("artifact_created", user_id=db.current_user_id())
    except Exception as e:
        print(f"[FileTrack] fire_event(artifact_created) failed: {e}")

# Manual work tools that get AUTO-DISPATCHED to the repair path (Aider first,
# Fixer fallback) when a reviewer/acceptance issue is pending. Local chat
# models flail on these instead of calling run_aider_fix; converting the call
# turns a burned round into repair progress.
_FIX_LOOP_MANUAL_TOOLS = {"read_file", "list_files", "write_file", "run_shell"}

_CAP_RELEASE_DIAGNOSTIC_TOOLS = {"read_file", "list_files"}
_AIDER_HEALTH_CACHE = {"url": "", "ts": 0.0, "healthy": False}
_AIDER_HEALTH_TTL_SECONDS = 30.0

# Fire-and-forget background tasks need a strong reference or the event loop
# may GC-cancel them mid-run (same pattern as main._track_bg / chat._spawn_bg).
_BG_TASKS: set = set()

def _spawn_bg(coro) -> "asyncio.Task":
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task

# Manifest entries for binary/generated assets the coder model genuinely cannot
# author as text (raw audio, raster images, fonts, video, archives/compiled, PDFs).
# Requiring these in the build-completeness gate would falsely mark every planned
# build "incomplete", so they're excluded. Everything else — including .svg (XML
# text), .json, .toml, .yaml, .md, .txt, .cfg, .ini, etc. — is a real deliverable
# the model can write and MUST stay required.
_BINARY_MANIFEST_EXTS = frozenset({
    ".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff",
    ".ttf", ".otf", ".woff", ".woff2",
    ".mp4", ".webm", ".mov", ".avi", ".mkv",
    ".bin", ".so", ".dll", ".dylib", ".o", ".a", ".class", ".jar",
    ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".pdf",
})

# NOTE: the Architect→Builder single-turn auto-handoff helpers were removed.
# Daedalus uses the pre-refactor two-turn flow: plan_project runs the Architect
# and returns the plan; the model then calls generate_code, which injects the
# most recent succeeded Architect manifest from the DB on its own.

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
    "generate_image": {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an image from a text description using local Stable Diffusion (SDXL). For pictures/art/photos — NOT charts or diagrams. Dimensions come from the global chat image settings.",
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string", "description": "What the image should show, descriptive and specific"},
                "negative_prompt": {"type": "string", "description": "Optional: things to avoid in the image"},
                "steps": {"type": "integer", "description": "Sampling steps (1-60; ignored when a default image model with saved presets is configured)"},
                "seed": {"type": "integer", "description": "Optional seed for reproducible results"},
            }, "required": ["prompt"]},
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
    "search_history": {
        "type": "function",
        "function": {
            "name": "search_history",
            "description": "Search the user's past conversations (memory-enabled chats only) for relevant prior discussions, decisions, or facts. Use when the user references something discussed before ('what did we decide about X', 'as we talked about last week').",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "What to look for in past conversations"},
                "include_current": {"type": "boolean", "description": "Also search the current conversation (default false)"},
            }, "required": ["query"]},
        },
    },
    "save_memory": {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save a durable memory when the user explicitly asks you to remember something, or clearly states a lasting fact, preference, or standing instruction worth keeping. Saves immediately. Never save secrets, passwords, API keys, or tokens.",
            "parameters": {"type": "object", "properties": {
                "content": {"type": "string", "description": "The fact to remember, as one self-contained sentence"},
                "memory_type": {"type": "string", "description": "semantic (facts/preferences), episodic (dated events/decisions), or procedural (workflows/instructions). Default semantic"},
                "scope": {"type": "string", "description": "global (all future chats, default) or workspace (only this workspace)"},
                "importance": {"type": "integer", "description": "1-5, default 4"},
            }, "required": ["content"]},
        },
    },
    "manage_tasks": {
        "type": "function",
        "function": {
            "name": "manage_tasks",
            "description": "Create, list, update, pause, resume, delete, or run the user's scheduled tasks. When the user asks for anything recurring ('every morning', 'each Friday', 'remind me daily'), create a scheduled task instead of doing it once. schedule_kind=event runs the task when something happens ('when an urgent email arrives, draft a reply'). Results are delivered to this conversation plus a notification.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "description": "create, list, get, update, pause, resume, delete, or run_now"},
                "task_id": {"type": "string", "description": "Task id (required for get/update/pause/resume/delete/run_now)"},
                "title": {"type": "string", "description": "Short task title"},
                "prompt": {"type": "string", "description": "What the task should do each run, phrased as an instruction"},
                "schedule_kind": {"type": "string", "description": "once, daily, weekly, monthly, cron, or event"},
                "time": {"type": "string", "description": "Local time HH:MM for daily/weekly/monthly"},
                "weekday": {"type": "integer", "description": "0-6 (0=Monday) for weekly"},
                "day": {"type": "integer", "description": "1-31 for monthly"},
                "cron": {"type": "string", "description": "Cron expression for schedule_kind=cron"},
                "run_at": {"type": "string", "description": "Local datetime YYYY-MM-DDTHH:MM for once"},
                "event": {"type": "string", "description": "For schedule_kind=event: email_received, urgent_email_received, research_completed, or artifact_created"},
                "every": {"type": "integer", "description": "For schedule_kind=event: fire on every Nth occurrence (default 1)"},
            }, "required": ["action"]},
        },
    },
    "manage_notes": {
        "type": "function",
        "function": {
            "name": "manage_notes",
            "description": "Create, list, complete, update, or delete the user's notes and todos. Use for 'note this down', 'add to my todo list', 'what's on my list', 'mark X done'. Todos support due dates and reminder times.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "description": "create, list, update, complete, or delete"},
                "note_id": {"type": "string", "description": "Note id (for update/complete/delete)"},
                "kind": {"type": "string", "description": "note or todo (default note; list filters by kind when given)"},
                "title": {"type": "string", "description": "Short title"},
                "content": {"type": "string", "description": "Body text (optional)"},
                "due": {"type": "string", "description": "Due datetime YYYY-MM-DDTHH:MM in the user's local time (todos)"},
                "remind": {"type": "string", "description": "Reminder datetime YYYY-MM-DDTHH:MM local — fires a notification"},
            }, "required": ["action"]},
        },
    },
    "manage_calendar": {
        "type": "function",
        "function": {
            "name": "manage_calendar",
            "description": "Create, list, update, or delete calendar events. Use when the user mentions appointments, meetings, or anything with a date/time ('dentist Tuesday 3pm'). Events sync to the user's phone via CalDAV when configured.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "description": "create, list, update, or delete"},
                "event_id": {"type": "string", "description": "Event id (for update/delete)"},
                "title": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "Start YYYY-MM-DDTHH:MM in the user's local time"},
                "end": {"type": "string", "description": "End YYYY-MM-DDTHH:MM local (default: start + 1h)"},
                "location": {"type": "string", "description": "Location (optional)"},
                "description": {"type": "string", "description": "Details (optional)"},
                "remind_minutes": {"type": "integer", "description": "Minutes before start to notify (optional)"},
                "days": {"type": "integer", "description": "For list: how many days ahead (default 7)"},
            }, "required": ["action"]},
        },
    },
    "get_weather": {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current conditions plus a today/tomorrow forecast for a location (Open-Meteo, no key). Defaults to the user's saved assistant location when no location is given.",
            "parameters": {"type": "object", "properties": {
                "location": {"type": "string", "description": "City or place name, e.g. 'Austin, TX' (optional — defaults to the assistant profile location)"},
            }},
        },
    },
    "list_emails": {
        "type": "function",
        "function": {
            "name": "list_emails",
            "description": "List the user's recent emails (cached inbox). Shows sender, subject, urgency, and a snippet. Use before read_email or reply_to_email.",
            "parameters": {"type": "object", "properties": {
                "unread_only": {"type": "boolean", "description": "Only unread (default false)"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            }},
        },
    },
    "read_email": {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": "Fetch the full body of one email live from the mail server.",
            "parameters": {"type": "object", "properties": {
                "message_id": {"type": "string", "description": "Email id from list_emails"},
            }, "required": ["message_id"]},
        },
    },
    "send_email": {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send a new email from the user's account. If autonomous sending is disabled for the account, this saves a DRAFT for the user to review instead — that is expected behavior, not an error.",
            "parameters": {"type": "object", "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Subject line"},
                "body": {"type": "string", "description": "Plain-text body"},
                "account": {"type": "string", "description": "Which account to send from when the user has several: match by label, address, or username (default: the first account)"},
            }, "required": ["to", "subject", "body"]},
        },
    },
    "reply_to_email": {
        "type": "function",
        "function": {
            "name": "reply_to_email",
            "description": "Reply to an email from list_emails. If autonomous sending is disabled for the account, the reply is saved as a DRAFT for the user to review — expected behavior, not an error.",
            "parameters": {"type": "object", "properties": {
                "message_id": {"type": "string", "description": "Email id from list_emails"},
                "body": {"type": "string", "description": "Plain-text reply body"},
            }, "required": ["message_id", "body"]},
        },
    },
    "archive_email": {
        "type": "function",
        "function": {
            "name": "archive_email",
            "description": "Archive an email (moves it out of the inbox).",
            "parameters": {"type": "object", "properties": {
                "message_id": {"type": "string", "description": "Email id from list_emails"},
            }, "required": ["message_id"]},
        },
    },
    "mark_email_read": {
        "type": "function",
        "function": {
            "name": "mark_email_read",
            "description": "Mark an email as read.",
            "parameters": {"type": "object", "properties": {
                "message_id": {"type": "string", "description": "Email id from list_emails"},
            }, "required": ["message_id"]},
        },
    },
    "delete_email": {
        "type": "function",
        "function": {
            "name": "delete_email",
            "description": "Delete an email (moves it to the server's Trash folder). Use ONLY when the user explicitly asks to delete — prefer archive_email otherwise. If autonomous deletion is disabled for the account, this refuses and asks you to confirm with the user — expected behavior, not an error.",
            "parameters": {"type": "object", "properties": {
                "message_id": {"type": "string", "description": "Email id from list_emails"},
            }, "required": ["message_id"]},
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
            "description": "Deep investigative research across direct archives — WikiLeaks/Cryptome/ICIJ (leaked docs), FBI Vault/DOJ/CIA Reading Room (gov records), CourtListener (court filings), MuckRock/National Security Archive/Archive.org (FOIA archives) — plus alt-media and the open web. Results come back grouped by source class. Use for any topic where official narratives may be incomplete.",
            "parameters": {"type": "object", "properties": {
                "topic": {"type": "string", "description": "What to investigate — a person, event, organization, or claim"},
                "angle": {"type": "string", "description": "Focus: evidence (default), key_players, timeline, debunk, documents, connections"},
                "depth": {"type": "integer", "description": "Search depth 3-5 (default 4). Higher = more sources searched"},
            }, "required": ["topic"]},
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
# Public parser names stay available from tools.py for existing imports.
from tooling.parser import (
    TOOL_PARAMS,
    _LOOSE_PARSE_SAFE_TOOLS,
    _extract_balanced_parens,
    _extract_json_objects,
    _fix_json_newlines,
    _normalize_tool_args,
    _parse_loose_args,
    _parse_loose_tool_calls,
    _parse_python_args,
    _parse_python_tool_calls,
    parse_text_tool_calls,
    strip_tool_calls,
)

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

def _clean_text(value) -> str:
    return str(value or "").strip()

def _profile_text(profile: dict | None, *keys: str) -> str:
    for key in keys:
        value = _clean_text((profile or {}).get(key))
        if value:
            return value
    return ""

def _profile_value(profile: dict | None, *keys: str):
    for key in keys:
        if isinstance(profile, dict) and profile.get(key) not in (None, ""):
            return profile.get(key)
    return None

def _join_prompt_parts(*parts: str) -> str:
    return ", ".join(p.strip(" ,") for p in parts if str(p or "").strip(" ,"))

def _dedupe_prompt_tags(text: str) -> str:
    """Drop repeated comma-separated tags (case-insensitive, order kept).
    Negatives join baseline + preset + enhancer output, which share tags."""
    seen = set()
    tags = []
    for tag in (text or "").split(","):
        tag = tag.strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return ", ".join(tags)

def _image_chat_resolution() -> tuple[int, int]:
    try:
        w, h = (int(x) for x in (config.IMAGE_CHAT_RESOLUTION or "1024x1024").split("x"))
        return w, h
    except Exception:
        return 1024, 1024

def _profile_loras(profile: dict | None) -> list:
    loras = (profile or {}).get("loras")
    if loras is None:
        loras = (profile or {}).get("lora")
    if loras is None:
        return []
    if isinstance(loras, (str, dict)):
        return [loras]
    if isinstance(loras, list):
        return loras
    return []

def _profile_metadata(selection: dict | None, *, active: bool, fallback_reason: str = "") -> dict:
    if not selection:
        return {}
    profile = selection.get("profile") or {}
    meta = {
        "active": active,
        "profile": selection.get("key") or "",
        "intent": selection.get("intent") or "",
        "adult_request": bool(selection.get("adult_request")),
        "workflow_configured": bool(_profile_text(profile, "workflow", "workflow_name", "saved_workflow")),
        "checkpoint_configured": bool(_profile_text(profile, "checkpoint", "ckpt", "ckpt_name")),
        "vae_configured": bool(_profile_text(profile, "vae", "vae_name")),
        "lora_count": len(_profile_loras(profile)),
    }
    if fallback_reason:
        meta["fallback_reason"] = fallback_reason
    return meta

def _persona_visual_context(args: dict, persona_context: dict) -> dict:
    visual_context = persona_context.get("visual_context")
    if isinstance(visual_context, dict):
        return visual_context
    return persona_images.build_visual_context(
        persona_id=_clean_text(persona_context.get("persona_id")),
        persona_name=_clean_text(persona_context.get("persona_name")),
        appearance=_clean_text(persona_context.get("appearance")),
        scenario=_clean_text(persona_context.get("scenario")),
        lore=_clean_text(persona_context.get("lore")),
        rating=_clean_text(persona_context.get("persona_rating") or "PG-13"),
        user_request=_clean_text(persona_context.get("user_request")),
        tool_prompt=_clean_text(args.get("prompt")),
        current_reply=_clean_text(persona_context.get("current_reply")),
        recent_messages=persona_context.get("recent_messages") if isinstance(persona_context.get("recent_messages"), list) else [],
        prior_images=persona_context.get("prior_images") if isinstance(persona_context.get("prior_images"), list) else [],
    )

def _resolve_chat_image_recipe(args: dict, persona_context: dict | None = None) -> dict:
    """Resolve chat generate_image settings without submitting to ComfyUI."""
    args = args or {}
    persona_context = persona_context or {}
    is_persona = bool(persona_context.get("persona_id"))
    gi_prompt = _clean_text(args.get("prompt"))
    gi_negative_arg = _clean_text(args.get("negative_prompt"))
    default_w, default_h = _image_chat_resolution()

    selection = None
    profile = None
    template = None
    workflow_name = ""
    fallback_reason = ""
    visual_context = {}
    prompt_payload = {}
    if persona_context.get("persona_id"):
        visual_context = _persona_visual_context(args, persona_context)
        # The model's own tool prompt goes through the structured path so its
        # scene wording survives (validated + leakage-scrubbed); the
        # deterministic rebuild remains the fallback when it doesn't validate.
        prompt_payload = persona_images.compose_persona_image_prompt(
            raw_prompt=gi_prompt,
            negative_prompt=gi_negative_arg,
            visual_context=visual_context,
            structured={"prompt": gi_prompt} if gi_prompt else None,
        )
        gi_prompt = prompt_payload.get("prompt") or gi_prompt
        gi_negative_arg = prompt_payload.get("negative_prompt") or gi_negative_arg
        config_data = persona_images.load_persona_image_profiles()
        # Selection reads the RAW model prompt (not the composed one, which
        # starts with appearance text) and the appearance-aware adult signal —
        # otherwise adult words in the persona's appearance flip every request.
        selection = persona_images.select_persona_image_profile(
            config_data,
            persona_id=_clean_text(persona_context.get("persona_id")),
            persona_name=_clean_text(persona_context.get("persona_name")),
            persona_rating=_clean_text(persona_context.get("persona_rating") or "PG-13"),
            prompt=_clean_text(args.get("prompt")),
            user_request=_clean_text(persona_context.get("user_request")),
            adult_request=visual_context.get("adult_request") if isinstance(visual_context, dict) else None,
        )
        if selection:
            profile = selection.get("profile") or {}
            workflow_name = _profile_text(profile, "workflow", "workflow_name", "saved_workflow")
            if workflow_name:
                template = comfyui.load_workflow(workflow_name)
                if template is None:
                    fallback_reason = "missing_workflow"
                    profile = None
                    workflow_name = ""

    active_profile = profile if isinstance(profile, dict) else None
    # Global workflow fallback: when no persona profile applied, render through
    # the admin-selected saved workflow (Settings → Chat Image Generation).
    # Persona workflows already won above; this only fills the empty slot. A
    # missing file leaves template=None so generate_image falls back to the
    # built-in SDXL template.
    if template is None and not active_profile and (config.IMAGE_CHAT_WORKFLOW or "").strip():
        template = comfyui.load_workflow(config.IMAGE_CHAT_WORKFLOW.strip())
        if template is not None:
            workflow_name = config.IMAGE_CHAT_WORKFLOW.strip()
    gi_ckpt = (
        _profile_text(active_profile, "checkpoint", "ckpt", "ckpt_name")
        if active_profile else ""
    ) or (config.IMAGE_CHAT_CHECKPOINT or "").strip()
    gi_preset = comfyui.settings_for_checkpoint(gi_ckpt) if gi_ckpt else {}

    # Chat image dimensions are a global admin preference. Persona profiles and
    # model-supplied tool args may steer workflow/model/style, but not size.
    gi_width = default_w
    gi_height = default_h
    gi_steps = (
        _profile_value(active_profile, "steps")
        if active_profile else None
    ) or gi_preset.get("steps") or args.get("steps") or 25
    gi_cfg = (
        _profile_value(active_profile, "cfg", "guidance")
        if active_profile else None
    ) or gi_preset.get("cfg") or 7.0
    gi_sampler = (
        _profile_text(active_profile, "sampler", "sampler_name")
        if active_profile else ""
    ) or gi_preset.get("sampler") or ""
    gi_scheduler = (
        _profile_text(active_profile, "scheduler")
        if active_profile else ""
    ) or gi_preset.get("scheduler") or ""
    gi_model_sampling = (
        _profile_text(active_profile, "model_sampling", "sampling")
        if active_profile else ""
    ) or gi_preset.get("model_sampling") or ""
    gi_vae = (
        _profile_text(active_profile, "vae", "vae_name")
        if active_profile else ""
    ) or (config.IMAGE_CHAT_VAE or "").strip()

    # Non-persona baseline: with no preset and no configured global prefix, a
    # bare prompt would hit SDXL with zero quality tags and an empty negative.
    # Persona prompts are composed upstream and profiles carry their own tags.
    base_prompt_prefix = (
        (gi_preset.get("prompt_prefix") or config.IMAGE_CHAT_PROMPT_PREFIX or "").strip()
        or ("" if is_persona else image_prompt_enhancer.DEFAULT_QUALITY_PREFIX)
    )
    profile_prompt_prefix = (
        _profile_text(active_profile, "prompt_prefix", "positive_prefix")
        if active_profile else ""
    )
    profile_prompt_suffix = (
        _profile_text(active_profile, "prompt_suffix", "positive_suffix")
        if active_profile else ""
    )
    gi_full_prompt = _dedupe_prompt_tags(_join_prompt_parts(base_prompt_prefix, profile_prompt_prefix, gi_prompt, profile_prompt_suffix))
    gi_negative = _dedupe_prompt_tags(_join_prompt_parts(
        (gi_preset.get("negative_prefix") or config.IMAGE_CHAT_NEGATIVE or "").strip()
        or ("" if is_persona else image_prompt_enhancer.DEFAULT_NEGATIVE_PROMPT),
        _profile_text(active_profile, "negative_prefix", "negative_prompt") if active_profile else "",
        gi_negative_arg,
    ))
    # Continuity seed: "same face / keep your look" requests reuse the newest
    # prior image's seed so the character stays recognizable. "another/different
    # one" intents keep a fresh seed — those want a new composition.
    gi_seed = args.get("seed")
    continuity_seed = False
    if is_persona and not gi_seed and "consistency" in (
        visual_context.get("intents") or [] if isinstance(visual_context, dict) else []
    ):
        for _prior in (persona_context.get("prior_images") or []):
            _prior_meta = _prior.get("metadata") if isinstance(_prior, dict) else None
            _prior_seed = (_prior_meta or {}).get("seed") if isinstance(_prior_meta, dict) else None
            if _prior_seed:
                gi_seed = _prior_seed
                continuity_seed = True
                break

    profile_metadata = _profile_metadata(selection, active=bool(active_profile), fallback_reason=fallback_reason)
    if continuity_seed:
        profile_metadata["continuity_seed"] = True
    if prompt_payload:
        profile_metadata.update({
            "prompt_intent": prompt_payload.get("primary_intent") or "",
            "intents": prompt_payload.get("intents") or [],
            "framing": prompt_payload.get("framing") or "",
            "continuity_notes": prompt_payload.get("continuity_notes") or "",
            "prompt_fallback": bool(prompt_payload.get("fallback_used")),
            "prior_image_count": int(
                visual_context.get("prior_image_count")
                if isinstance(visual_context, dict) and visual_context.get("prior_image_count") is not None
                else (len(visual_context.get("prior_images") or []) if isinstance(visual_context, dict) else 0)
            ),
        })

    return {
        "prompt": gi_full_prompt,
        "negative_prompt": gi_negative,
        "width": gi_width,
        "height": gi_height,
        "steps": gi_steps,
        "cfg": gi_cfg,
        "seed": gi_seed,
        "checkpoint": gi_ckpt,
        "sampler_name": gi_sampler,
        "scheduler": gi_scheduler,
        "model_sampling": gi_model_sampling,
        "vae": gi_vae,
        "template": template,
        "workflow_name": workflow_name,
        "loras": _profile_loras(active_profile) if active_profile else [],
        "profile_metadata": profile_metadata,
        "profile_active": bool(active_profile),
    }

def _image_recipe_event_detail(recipe: dict) -> dict:
    """Return prompt-visible generate_image details without local profile names."""
    profile_meta = recipe.get("profile_metadata") if isinstance(recipe.get("profile_metadata"), dict) else {}
    workflow_active = bool(recipe.get("workflow_name"))
    profile_active = bool(recipe.get("profile_active"))
    return {
        "tool": "generate_image",
        "prompt": recipe.get("prompt") or "",
        "negative_prompt": recipe.get("negative_prompt") or "",
        "width": recipe.get("width"),
        "height": recipe.get("height"),
        "steps": recipe.get("steps"),
        "cfg": recipe.get("cfg"),
        "sampler": recipe.get("sampler_name") or "",
        "scheduler": recipe.get("scheduler") or "",
        "model_sampling": recipe.get("model_sampling") or "",
        "profile_active": profile_active,
        "workflow_active": workflow_active,
        "profile_workflow": bool(profile_active and workflow_active),
        "global_workflow": bool(workflow_active and not profile_active),
        "loras_active": bool(recipe.get("loras")),
        "prompt_fallback": bool(profile_meta.get("prompt_fallback")),
        "adult_request": bool(profile_meta.get("adult_request")),
        "auto_enhanced": bool(recipe.get("auto_enhanced")),
    }

# ── Tool execution dispatcher ──

async def exec_tool(http, events, name, args, conv_id, *positional, **kwargs):
    from hephaestus_scope import scope, conversation_metadata
    metadata=await conversation_metadata(conv_id)
    if os.environ.get('HEPHAESTUS_TOKEN') and name=='generate_code' and not metadata.get('project'):
        # Allocate the namespace before the builder starts, so post-build reads use it too.
        metadata['project']='proj-'+uuid.uuid4().hex[:12]
        args={**args,'_hephaestus_project_id':metadata['project']}
    with scope(**metadata):
        return await _exec_tool_scoped(http, events, name, args, conv_id, *positional, **kwargs)

async def _exec_tool_scoped(
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
    persona_context: dict | None = None,
    workspace_id: str | None = None,
) -> str:
    """Execute a built-in or custom tool and return the result string."""
    custom_tool_map = custom_tool_map or {}
    connector_tool_name_map = connector_tool_name_map or {}
    try:
        if name in DOCUMENT_TOOLS:
            from documents import tool as document_tool
            return await document_tool(name, args or {}, http=http, conv_id=conv_id)
        from coder_jobs import route_tool as route_coder_job
        routed = await route_coder_job(name, args, conv_id, events)
        if routed is not None:
            return routed
        if name in {'start_coder_workflow', 'plan_project', 'generate_code', 'ask_project'}:
            return 'ERROR: A conversation is required to start a Daedalus job.'
        if name in {'run_aider_fix', 'run_review', 'run_acceptance_review', 'run_fixer'}:
            return 'ERROR: This workflow tool was retired. Use start_coder_workflow; the controller owns coding, checks and repairs.'

        if name in CODEBOX_TOOL_NAMES:
            return await run_codebox_tool(
                name, args, http=http, events=events, conv_id=conv_id,
            )

        elif name == "research":
            from quick_search import run_quick_search_for_chat
            query = str(args.get("query") or "").strip()
            if not query:
                return "ERROR: research requires a query."
            result = await run_quick_search_for_chat(
                http, config.OLLAMA_URL, config.WORKSPACE_MODEL or config.DEFAULT_MODEL,
                events, conv_id, [{"role": "user", "content": query}],
                default_model=config.DEFAULT_MODEL, force_search=True, tool_name="research",
            )
            return result["context"]

        elif name == "search_history":
            import rag  # lazy: keeps tools.py import light
            query = str(args.get("query") or "").strip()
            if not query:
                return "ERROR: search_history requires a 'query'."
            await events.emit(conv_id, "tool_start", {
                "tool": "history", "icon": "brain", "status": f"Searching past conversations: {query[:60]}",
            })
            exclude = "" if args.get("include_current") else (conv_id or "")
            hits = await rag.query_history(db.current_user_id(), query, top_k=6, exclude_conv_id=exclude)
            await events.emit(conv_id, "tool_end", {
                "tool": "history", "icon": "brain", "status": f"Found {len(hits)} past-conversation match(es)",
            })
            if not hits:
                return ("No matching past conversations found. Only memory-enabled chats are indexed — "
                        "the user may need to enable memory on the relevant conversations.")
            parts = ["Relevant excerpts from the user's past conversations (most similar first):\n"]
            total = 0
            for h in hits:
                title = h.get("conv_title") or h.get("conversation_id") or "untitled chat"
                date = (h.get("created_at") or "")[:10]
                head = f"[{title}{' — ' + date if date else ''}] ({h.get('role','')})"
                body = (h.get("text") or "").strip()
                seg = f"{head}\n{body}\n"
                if total + len(seg) > 3000:
                    break
                parts.append(seg)
                total += len(seg)
            return "\n".join(parts)

        elif name == "save_memory":
            content = re.sub(r"\s+", " ", str(args.get("content") or "").strip())
            if len(content) < 8:
                return "ERROR: save_memory requires a 'content' sentence describing what to remember."
            content = content[:800]
            if re.search(r"(password|api[_ -]?key|secret|private key|token)\s*[:=]", content, re.I):
                return "ERROR: refusing to save what looks like a secret or credential."
            mem_type = str(args.get("memory_type") or "semantic").strip().lower()
            scope = str(args.get("scope") or "global").strip().lower()
            try:
                importance = max(1, min(5, int(args.get("importance") or 4)))
            except Exception:
                importance = 4
            ws = None
            if scope == "workspace":
                try:
                    import workspace_service
                    ws = await workspace_service.resolve(conv_id, workspace_id)
                except Exception:
                    ws = None
                if not ws or not int(ws.get("memory_enabled") or 0):
                    return "ERROR: No active memory-enabled workspace. Choose a workspace and enable its memory before saving workspace memories."
            await events.emit(conv_id, "tool_start", {
                "tool": "memory", "icon": "brain", "status": "Saving memory...",
            })
            try:
                if ws:
                    existing = await db.list_workspace_memories(ws["id"], status="all")
                else:
                    existing = await db.list_global_memories(status="all")
                norm = content.lower()
                dup = next((m for m in existing
                            if re.sub(r"\s+", " ", (m.get("content") or "").strip().lower()) == norm), None)
                where = f"workspace \"{ws.get('name') or ws['id']}\" memory" if ws else "HyprChat memory"
                if dup:
                    if (dup.get("status") or "") != "accepted":
                        if ws:
                            await db.update_workspace_memory(dup["id"], ws["id"], status="accepted")
                        else:
                            await db.update_global_memory(dup["id"], status="accepted")
                        await events.emit(conv_id, "tool_end", {
                            "tool": "memory", "icon": "brain", "status": "Existing memory activated",
                        })
                        return f"That was already suggested — now saved and active in {where}: \"{content}\""
                    await events.emit(conv_id, "tool_end", {
                        "tool": "memory", "icon": "brain", "status": "Already remembered",
                    })
                    return f"Already remembered in {where}: \"{content}\""
                if ws:
                    mem = await db.create_workspace_memory(
                        ws["id"], content=content, memory_type=mem_type, status="accepted",
                        importance=importance, source_conv_id=conv_id,
                        source_conversation_id=conv_id, confidence=1,
                        metadata={"suggested_by": "save_memory_tool"},
                    )
                else:
                    mem = await db.create_global_memory(
                        content=content, memory_type=mem_type, status="accepted",
                        importance=importance, source_conv_id=conv_id,
                        source_conversation_id=conv_id, confidence=1,
                        metadata={"suggested_by": "save_memory_tool"},
                    )
                await events.emit(conv_id, "tool_end", {
                    "tool": "memory", "icon": "brain", "status": f"Saved to {where}",
                })
                return f"Saved to {where}: \"{content}\". It will be recalled in future memory-enabled chats."
            except Exception as e:
                await events.emit(conv_id, "tool_error", {
                    "tool": "memory", "icon": "brain", "status": f"Save failed: {e}",
                })
                return f"ERROR: could not save memory: {e}"

        elif name == "manage_tasks":
            # Lazy import: scheduler → agents.assistant → agents.chat → tools
            # would cycle at module import time.
            import scheduler as _scheduler
            action = str(args.get("action") or "").strip().lower()
            task_id = str(args.get("task_id") or "").strip()

            def _fmt_task(t: dict) -> str:
                state = "enabled" if t["enabled"] else "paused"
                nxt = f", next run {t['next_run']} UTC" if t.get("next_run") else ""
                last = f", last: {t['last_status']}" if t.get("last_status") else ""
                return f"- {t['id']}: \"{t['title']}\" [{t['schedule_kind']}, {state}{nxt}{last}]"

            try:
                if action == "list":
                    tasks = await db.list_scheduled_tasks()
                    if not tasks:
                        return "No scheduled tasks yet."
                    return "Scheduled tasks:\n" + "\n".join(_fmt_task(t) for t in tasks[:30])

                if action == "create":
                    title = str(args.get("title") or "").strip()[:200]
                    prompt = str(args.get("prompt") or "").strip()
                    kind = str(args.get("schedule_kind") or "once").strip().lower()
                    if not title or not prompt:
                        return "ERROR: manage_tasks create requires 'title' and 'prompt'."
                    if kind not in ("once", "daily", "weekly", "monthly", "cron", "event"):
                        return "ERROR: schedule_kind must be once, daily, weekly, monthly, cron, or event."
                    profile = await db.get_assistant_profile()
                    tz = (profile or {}).get("timezone") or "UTC"
                    event_trigger_json: dict = {}
                    schedule_json: dict = {}
                    if kind == "event":
                        event_name = str(args.get("event") or "").strip().lower()
                        if event_name not in _scheduler.EVENT_NAMES:
                            return (f"ERROR: event must be one of "
                                    f"{', '.join(_scheduler.EVENT_NAMES)}.")
                        event_trigger_json = {
                            "event": event_name,
                            "every": args.get("every", 1),
                        }
                    else:
                        if args.get("time"):
                            schedule_json["time"] = str(args["time"]).strip()
                        if args.get("weekday") is not None:
                            schedule_json["weekday"] = args["weekday"]
                        if args.get("day") is not None:
                            schedule_json["day"] = args["day"]
                        if args.get("cron"):
                            schedule_json["cron"] = str(args["cron"]).strip()
                        if args.get("run_at"):
                            schedule_json["run_at"] = str(args["run_at"]).strip()
                    next_run = await _scheduler.validated_next_run(
                        kind, schedule_json, tz, event_trigger_json=event_trigger_json)
                    if kind == "event":
                        event_trigger_json["every"] = int(event_trigger_json["every"])
                    task = await db.create_scheduled_task(
                        f"task-{uuid.uuid4().hex[:10]}", title=title, prompt=prompt,
                        task_type="llm", schedule_kind=kind,
                        schedule_json=schedule_json if kind != "event" else {},
                        event_trigger_json=event_trigger_json,
                        timezone=tz, next_run=next_run, conversation_id=conv_id or "",
                        delivery_json={"conversation": bool(conv_id), "notify": True},
                    )
                    if kind == "event":
                        return (f"Created event-triggered task {task['id']} (\"{title}\", fires on "
                                f"{event_trigger_json['event']}"
                                + (f" every {event_trigger_json['every']} occurrences"
                                   if event_trigger_json['every'] > 1 else "")
                                + "). Results will be posted in this conversation.")
                    return (f"Created scheduled task {task['id']} (\"{title}\", {kind}, "
                            f"next run {task['next_run']} UTC, timezone {tz}). Results will be "
                            f"posted in this conversation.")

                if not task_id:
                    return f"ERROR: manage_tasks {action or '?'} requires 'task_id'."
                task = await db.get_scheduled_task(task_id)
                if not task:
                    return f"ERROR: no task with id {task_id}."

                if action == "get":
                    runs = await db.list_task_runs(task_id, limit=3)
                    run_lines = "\n".join(
                        f"  run {r['id']}: {r['status']} at {r['started_at']}" for r in runs)
                    return _fmt_task(task) + (f"\nRecent runs:\n{run_lines}" if run_lines else "")
                if action == "pause":
                    await db.update_scheduled_task(task_id, {"enabled": False})
                    return f"Paused task \"{task['title']}\"."
                if action == "resume":
                    nxt = await _scheduler.validated_next_run(
                        task["schedule_kind"], task["schedule_json"],
                        task.get("timezone") or "", event_trigger_json=task.get("event_trigger_json"))
                    await db.update_scheduled_task(task_id, {"enabled": True, "next_run": nxt})
                    return f"Resumed task \"{task['title']}\" (next run {nxt} UTC)."
                if action == "delete":
                    await db.delete_scheduled_task(task_id)
                    return f"Deleted task \"{task['title']}\"."
                if action == "run_now":
                    run_id = await _scheduler.run_task_now(task_id)
                    return f"Started task \"{task['title']}\" now (run {run_id})."
                if action == "update":
                    fields: dict = {}
                    if args.get("title"):
                        fields["title"] = str(args["title"]).strip()[:200]
                    if args.get("prompt"):
                        fields["prompt"] = str(args["prompt"]).strip()
                    schedule_json = dict(task.get("schedule_json") or {})
                    changed_schedule = False
                    for key in ("time", "weekday", "day", "cron", "run_at"):
                        if args.get(key) is not None and args.get(key) != "":
                            schedule_json[key] = args[key]
                            changed_schedule = True
                    kind = str(args.get("schedule_kind") or task["schedule_kind"]).strip().lower()
                    event_trigger = dict(task.get("event_trigger_json") or {})
                    if kind == "event":
                        for key in ("event", "every"):
                            if args.get(key) is not None:
                                event_trigger[key] = args[key]
                                changed_schedule = True
                    if kind == "webhook" and task["schedule_kind"] != "webhook":
                        return "ERROR: create webhook tasks through the Tasks API."
                    if kind != task["schedule_kind"]:
                        changed_schedule = True
                    if changed_schedule:
                        fields["schedule_kind"] = kind
                        fields["schedule_json"] = schedule_json
                        fields["next_run"] = await _scheduler.validated_next_run(
                            kind, schedule_json, task.get("timezone") or "",
                            event_trigger_json=event_trigger)
                        if kind == "event":
                            fields["event_trigger_json"] = event_trigger
                    if not fields:
                        return "Nothing to update — pass title, prompt, or schedule fields."
                    updated = await db.update_scheduled_task(task_id, fields)
                    return f"Updated task:\n{_fmt_task(updated)}"

                return "ERROR: action must be create, list, get, update, pause, resume, delete, or run_now."
            except Exception as e:
                return f"ERROR: manage_tasks failed: {e}"

        elif name == "get_weather":
            import weather as _weather
            location = str(args.get("location") or "").strip()
            if not location:
                _profile = await db.get_assistant_profile()
                location = ((_profile or {}).get("location") or "").strip()
            if not location:
                return ("ERROR: no location given and no assistant location saved — "
                        "pass location, or set one in the Assistant panel.")
            try:
                return await _weather.get_weather_text(location)
            except Exception as e:
                return f"ERROR: weather lookup failed: {e}"

        elif name == "manage_notes":
            import pim as _pim
            action = str(args.get("action") or "").strip().lower()
            try:
                if action == "list":
                    kind = str(args.get("kind") or "").strip().lower()
                    notes = await db.list_notes(kind=kind if kind in ("note", "todo") else "",
                                                include_done=False)
                    if not notes:
                        return "No open notes or todos."
                    tz = await _pim.user_timezone()
                    lines = []
                    for n in notes[:30]:
                        due = f" (due {_pim.utc_to_local(n['due_at'], tz)})" if n.get("due_at") else ""
                        body = f" — {(n.get('content') or '')[:60]}" if n.get("content") else ""
                        lines.append(f"- [{n['kind']}] {n['id']}: {n['title']}{due}{body}")
                    return "Notes and todos:\n" + "\n".join(lines)
                if action == "create":
                    title = str(args.get("title") or "").strip()
                    if not title:
                        return "ERROR: manage_notes create requires 'title'."
                    note = await _pim.create_note(
                        kind=str(args.get("kind") or "note").strip().lower(),
                        title=title, content=str(args.get("content") or ""),
                        due_local=str(args.get("due") or ""),
                        remind_local=str(args.get("remind") or ""))
                    extra = " with a reminder" if args.get("remind") else ""
                    return f"Saved {note['kind']} {note['id']}: \"{title}\"{extra}."
                note_id = str(args.get("note_id") or "").strip()
                if not note_id:
                    return f"ERROR: manage_notes {action or '?'} requires 'note_id'."
                note = await db.get_note(note_id)
                if not note:
                    return f"ERROR: no note with id {note_id}."
                if action == "complete":
                    await db.update_note(note_id, {"done": True})
                    return f"Marked \"{note['title']}\" done."
                if action == "delete":
                    await db.delete_note(note_id)
                    return f"Deleted \"{note['title']}\"."
                if action == "update":
                    tz = await _pim.user_timezone()
                    fields: dict = {}
                    if args.get("title"):
                        fields["title"] = str(args["title"]).strip()[:300]
                    if args.get("content") is not None and args.get("content") != "":
                        fields["content"] = str(args["content"])
                    if args.get("due"):
                        fields["due_at"] = _pim.local_to_utc(str(args["due"]), tz)
                    if args.get("remind"):
                        fields["remind_at"] = _pim.local_to_utc(str(args["remind"]), tz)
                        fields["reminded"] = False
                    if not fields:
                        return "Nothing to update — pass title, content, due, or remind."
                    updated = await db.update_note(note_id, fields)
                    return f"Updated \"{updated['title']}\"."
                return "ERROR: action must be create, list, update, complete, or delete."
            except Exception as e:
                return f"ERROR: manage_notes failed: {e}"

        elif name == "manage_calendar":
            import pim as _pim
            action = str(args.get("action") or "").strip().lower()
            try:
                tz = await _pim.user_timezone()
                if action == "list":
                    days = max(1, min(90, int(args.get("days") or 7)))
                    from datetime import datetime as _dt, timedelta as _td
                    start = _dt.utcnow().isoformat()
                    end = (_dt.utcnow() + _td(days=days)).isoformat()
                    events = await db.list_calendar_events(start=start, end=end)
                    if not events:
                        return f"No events in the next {days} days."
                    lines = []
                    for e in events[:30]:
                        when = _pim.utc_to_local(e.get("start_at") or "", tz)
                        loc = f" @ {e['location']}" if e.get("location") else ""
                        lines.append(f"- {e['id']}: {when} {e['title']}{loc}")
                    return f"Events (next {days} days, local time):\n" + "\n".join(lines)
                if action == "create":
                    title = str(args.get("title") or "").strip()
                    start = str(args.get("start") or "").strip()
                    if not title or not start:
                        return "ERROR: manage_calendar create requires 'title' and 'start' (YYYY-MM-DDTHH:MM local)."
                    try:
                        remind = int(args["remind_minutes"]) if args.get("remind_minutes") is not None else None
                    except (TypeError, ValueError):
                        remind = None
                    event = await _pim.create_event(
                        title=title, start_local=start,
                        end_local=str(args.get("end") or ""),
                        description=str(args.get("description") or ""),
                        location=str(args.get("location") or ""),
                        remind_minutes=remind)
                    return (f"Added event {event['id']}: \"{title}\" at "
                            f"{_pim.utc_to_local(event['start_at'], tz)} (local). "
                            f"It will sync to CalDAV if configured.")
                event_id = str(args.get("event_id") or "").strip()
                if not event_id:
                    return f"ERROR: manage_calendar {action or '?'} requires 'event_id'."
                event = await db.get_calendar_event(event_id)
                if not event:
                    return f"ERROR: no event with id {event_id}."
                if action == "delete":
                    await _pim.delete_event(event_id)
                    return f"Deleted event \"{event['title']}\"."
                if action == "update":
                    fields = {"title": args.get("title") or None,
                              "description": args.get("description"),
                              "location": args.get("location"),
                              "start_local": args.get("start") or "",
                              "end_local": args.get("end") or ""}
                    if args.get("remind_minutes") is not None:
                        try:
                            fields["remind_minutes"] = int(args["remind_minutes"])
                        except (TypeError, ValueError):
                            pass
                    updated = await _pim.update_event(event_id, fields)
                    return f"Updated event \"{updated['title']}\" ({_pim.utc_to_local(updated['start_at'], tz)} local)."
                return "ERROR: action must be create, list, update, or delete."
            except ValueError as e:
                return f"ERROR: {e}"
            except Exception as e:
                return f"ERROR: manage_calendar failed: {e}"

        elif name in ("list_emails", "read_email", "send_email", "reply_to_email",
                      "archive_email", "mark_email_read", "delete_email"):
            import email_client as _email_client
            try:
                accounts = await db.list_email_accounts()
                if not accounts:
                    return "No email account is configured. The user can add one in the Email panel."
                account_id = accounts[0]["id"]

                if name == "list_emails":
                    limit = max(1, min(50, int(args.get("limit") or 20)))
                    msgs = await db.list_email_messages(
                        limit=limit, unread_only=bool(args.get("unread_only")))
                    if not msgs:
                        return "No emails in the cached inbox yet (the poller checks every ~5 minutes)."
                    lines = []
                    for m in msgs:
                        flags = []
                        if m["unread"]:
                            flags.append("unread")
                        if m.get("urgency") and m["urgency"] != "normal":
                            flags.append(m["urgency"])
                        tag = f" [{'/'.join(flags)}]" if flags else ""
                        lines.append(f"- {m['id']}{tag} {m['from_addr']}: {m['subject']}"
                                     f" — {(m.get('summary') or m.get('snippet') or '')[:100]}")
                    return "Inbox:\n" + "\n".join(lines)

                if name == "send_email":
                    to = _email_client.clean_address(str(args.get("to") or ""))
                    subject = str(args.get("subject") or "").strip()
                    body = str(args.get("body") or "").strip()
                    if not to or "@" not in to or not subject or not body:
                        return "ERROR: send_email requires a valid 'to' address, 'subject', and 'body'."
                    # Multi-account: honor an explicit account hint (label /
                    # address / username substring) instead of always sending
                    # from the oldest account.
                    wanted = str(args.get("account") or "").strip().lower()
                    if wanted:
                        matches = [a for a in accounts if wanted in " ".join(
                            (a.get("label") or "", a.get("from_address") or "",
                             a.get("username") or "")).lower()]
                        if not matches:
                            known = ", ".join(a.get("label") or a.get("username") or a["id"]
                                              for a in accounts)
                            return (f"ERROR: no email account matches '{wanted}'. "
                                    f"Configured accounts: {known}.")
                        account_id = matches[0]["id"]
                    account = await db.get_email_account(account_id, with_password=True)
                    # SERVER-SIDE autonomy gate: prompt rules alone don't count.
                    # Per-account flag ANDed with the assistant-profile master
                    # switch (no profile row = master on, so email works
                    # without ever opening the Assistant panel).
                    _profile = await db.get_assistant_profile()
                    _master_ok = _profile is None or _profile.get("allow_autonomous_email")
                    if not account.get("allow_autonomous_send") or not _master_ok:
                        note = await db.create_note(
                            f"note-{uuid.uuid4().hex[:10]}", kind="note",
                            title=f"Email draft to {to}: {subject}"[:290],
                            content=body, tags=["email-draft"])
                        why = ("for this account" if not account.get("allow_autonomous_send")
                               else "by the assistant profile's master switch (Assistant panel)")
                        return (f"Autonomous sending is disabled {why}, so the email was "
                                f"saved as a draft note ({note['id']}) instead. The user can review "
                                f"and send it from the Email panel, or enable autonomous send in "
                                f"Email settings.")
                    await _email_client.send_mail(account, to=to, subject=subject, body=body)
                    return f"Email sent to {to}: \"{subject}\"."

                message_id = str(args.get("message_id") or "").strip()
                if not message_id:
                    return f"ERROR: {name} requires 'message_id' (use list_emails first)."
                msg = await db.get_email_message(message_id)
                if not msg:
                    return f"ERROR: no email with id {message_id}."
                account = await db.get_email_account(msg["account_id"], with_password=True)
                if not account:
                    return "ERROR: the email account for this message no longer exists."

                if name == "read_email":
                    body = await _email_client.fetch_body(account, msg["uid"])
                    await db.update_email_message(message_id, {"unread": False})
                    try:
                        await _email_client.set_flags(account, msg["uid"], seen=True)
                    except Exception:
                        pass
                    return (f"From: {body['from']}\nTo: {body['to']}\nDate: {body['date']}\n"
                            f"Subject: {body['subject']}\n\n{body['body'][:12000]}")

                if name == "reply_to_email":
                    body = str(args.get("body") or "").strip()
                    if not body:
                        return "ERROR: reply_to_email requires 'body'."
                    _profile = await db.get_assistant_profile()
                    _master_ok = _profile is None or _profile.get("allow_autonomous_email")
                    if not account.get("allow_autonomous_send") or not _master_ok:
                        await db.update_email_message(message_id, {"draft_reply": body})
                        return (f"Autonomous sending is disabled, so the reply to "
                                f"\"{msg['subject']}\" was saved as a draft. The user can review "
                                f"and send it from the Email panel.")
                    to = _email_client.clean_address(msg["from_addr"])
                    subject = msg["subject"] or ""
                    if not subject.lower().startswith("re:"):
                        subject = f"Re: {subject}"
                    await _email_client.send_mail(
                        account, to=to, subject=subject, body=body,
                        in_reply_to=msg.get("message_id") or "")
                    await db.update_email_message(message_id, {"draft_reply": ""})
                    return f"Reply sent to {to}."

                if name == "archive_email":
                    moved = await _email_client.set_flags(account, msg["uid"], archive=True)
                    await db.update_email_message(message_id, {"archived": True, "unread": False})
                    if moved:
                        return f"Archived \"{msg['subject']}\"."
                    return (f"Archived \"{msg['subject']}\" in HyprChat, but the mail "
                            f"server has no Archive folder — on the server it was only "
                            f"marked read and stays in the inbox.")

                if name == "mark_email_read":
                    await _email_client.set_flags(account, msg["uid"], seen=True)
                    await db.update_email_message(message_id, {"unread": False})
                    return f"Marked \"{msg['subject']}\" as read."

                if name == "delete_email":
                    # SERVER-SIDE autonomy gate (like allow_autonomous_send):
                    # prompt rules alone must not authorize destroying mail.
                    if not account.get("allow_autonomous_delete"):
                        return (f"Autonomous deletion is disabled for this account, so "
                                f"\"{msg['subject']}\" was NOT deleted. Ask the user to "
                                f"delete it from the Email panel, or to enable autonomous "
                                f"delete in Email settings. Consider archive_email instead.")
                    await _email_client.delete_message(account, msg["uid"])
                    await db.delete_email_message(message_id)
                    return f"Deleted \"{msg['subject']}\" (moved to the server's Trash folder)."
            except Exception as e:
                return f"ERROR: {name} failed: {e}"

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

        elif name == "download_file":
            path = args.get("path", "")
            await events.emit(conv_id, "tool_start", {"tool": "download_file", "icon": "code", "status": f"Preparing: {path}"})
            qpath = shlex.quote(path)
            if os.environ.get('HEPHAESTUS_TOKEN'):
                from hephaestus_scope import download_bytes
                data=await download_bytes(http,path,config.MAX_UPLOAD_SIZE_MB*1024*1024)
                stdout=base64.b64encode(data).decode()+'|||SEPARATOR|||'+os.path.basename(path)
            else:
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
                if artifact:
                    await _fire_artifact_created_event()
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

        elif name == "generate_image":
            if not config.COMFYUI_URL:
                return "ERROR: Image generation is not configured. Set the ComfyUI URL in Settings → Connections."
            gi_prompt = (args.get("prompt") or "").strip()
            if not gi_prompt:
                return "ERROR: generate_image requires a prompt describing the image."
            # Optional LLM pass expanding terse prompts into full SDXL prompts.
            # Non-persona only: persona prompts are composed by persona_images.
            # Fail-open — a slow or dead helper model never blocks the render.
            gi_auto_enhanced = False
            if config.IMAGE_CHAT_AUTO_ENHANCE and not (persona_context or {}).get("persona_id"):
                await events.emit(conv_id, "tool_progress", {
                    "tool": "generate_image", "icon": "image",
                    "status": "Enhancing prompt",
                })
                try:
                    _enh = await asyncio.wait_for(
                        image_prompt_enhancer.enhance_prompt(None, gi_prompt, timeout=20),
                        timeout=25)
                except Exception:
                    _enh = None
                if _enh and _enh[0]:
                    gi_auto_enhanced = True
                    args = {**args, "prompt": _enh[0],
                            "negative_prompt": _join_prompt_parts(
                                _clean_text(args.get("negative_prompt")), _enh[1])}
            # Settings-driven defaults plus persona-only local profile routing.
            # The profile file lives in ignored data/ and only selects known
            # saved workflows/checkpoints/LoRAs; the model never invents graphs.
            gi_recipe = _resolve_chat_image_recipe(args, persona_context=persona_context)
            gi_recipe["auto_enhanced"] = gi_auto_enhanced
            gi_detail = _image_recipe_event_detail(gi_recipe)
            gi_detail_json = json.dumps(gi_detail, ensure_ascii=True)
            await events.emit(conv_id, "tool_start", {
                "tool": "generate_image", "icon": "image",
                "status": "Generating image",
                "detail": gi_detail_json,
            })
            gi_full_prompt = gi_recipe["prompt"]
            gi_negative = gi_recipe["negative_prompt"]
            gi_width = gi_recipe["width"]
            gi_height = gi_recipe["height"]
            gi_steps = gi_recipe["steps"]
            gi_cfg = gi_recipe["cfg"]
            gi_ckpt = gi_recipe["checkpoint"]
            gi_vae = gi_recipe["vae"]
            try:
                workflow, gi_seed = comfyui.build_workflow(
                    gi_recipe["template"] or comfyui.load_template(),
                    prompt=gi_full_prompt,
                    negative_prompt=gi_negative,
                    width=gi_width,
                    height=gi_height,
                    steps=gi_steps,
                    cfg=gi_cfg,
                    seed=gi_recipe["seed"],
                    checkpoint=gi_ckpt,
                    sampler_name=gi_recipe["sampler_name"],
                    scheduler=gi_recipe["scheduler"],
                    model_sampling=gi_recipe["model_sampling"],
                    vae=gi_vae,
                    loras=gi_recipe["loras"],
                )
                prompt_id = await comfyui.submit(workflow)
            except Exception as e:
                await events.emit(conv_id, "tool_error", {
                    "tool": "generate_image", "icon": "image",
                    "status": f"Submit failed: {str(e)[:160]}",
                    "detail": gi_detail_json,
                })
                return f"ERROR: Could not start image generation: {str(e)[:300]}"
            # Poll: 300s budget covers a 30-60s cold checkpoint load
            _gi_t0 = time.time()
            history = None
            _last_progress = 0.0
            while time.time() - _gi_t0 < 300:
                await asyncio.sleep(1.5)
                try:
                    history = await comfyui.get_history(prompt_id)
                except Exception:
                    history = None
                if history and (history.get("outputs") or {}):
                    break
                if history and history.get("status", {}).get("status_str") == "error":
                    await events.emit(conv_id, "tool_error", {
                        "tool": "generate_image", "icon": "image",
                        "status": "ComfyUI reported a workflow error",
                        "detail": gi_detail_json,
                    })
                    comfyui.finish_job(prompt_id)
                    return "ERROR: ComfyUI failed to execute the workflow (check checkpoint name and VRAM)."
                history = None
                if time.time() - _last_progress >= 6:
                    _last_progress = time.time()
                    elapsed = int(time.time() - _gi_t0)
                    qpos = await comfyui.queue_position(prompt_id)
                    hint = f"queued behind {qpos}" if qpos else ("rendering" if qpos == 0 else "loading model")
                    await events.emit(conv_id, "tool_progress", {
                        "tool": "generate_image", "icon": "image",
                        "status": f"Rendering image {elapsed}s ({hint})",
                        "detail": gi_detail_json,
                    })
            if not history:
                await comfyui.cancel(prompt_id)
                comfyui.finish_job(prompt_id)
                await events.emit(conv_id, "tool_error", {
                    "tool": "generate_image", "icon": "image",
                    "status": "Timed out after 300s",
                    "detail": gi_detail_json,
                })
                return "ERROR: Image generation timed out after 300 seconds."
            images = comfyui.outputs_from_history(history)
            if not images:
                await events.emit(conv_id, "tool_error", {
                    "tool": "generate_image", "icon": "image",
                    "status": "No image produced",
                    "detail": gi_detail_json,
                })
                comfyui.finish_job(prompt_id)
                return "ERROR: ComfyUI finished but produced no output images."
            os.makedirs(config.SANDBOX_OUTPUTS_DIR, exist_ok=True)
            md_lines = []
            for gi_i, img in enumerate(images):
                try:
                    img_bytes = await comfyui.fetch_image(img)
                except Exception as e:
                    print(f"[IMAGE] fetch failed: {e}")
                    continue
                filename = f"comfy_{prompt_id[:8]}_{gi_i}.png"
                filepath = os.path.join(config.SANDBOX_OUTPUTS_DIR, filename)
                with open(filepath, "wb") as f:
                    f.write(img_bytes)
                file_meta = _artifact_file_metadata(filepath)
                download_url = f"/api/downloads/{filename}"
                artifact = None
                try:
                    cf_id = f"cf-{uuid.uuid4().hex[:8]}"
                    artifact = await db.add_conversation_file(
                        cf_id,
                        conv_id,
                        filename,
                        download_url,
                        message_id=artifact_message_id,
                        kind="image",
                        mime_type="image/png",
                        storage_path=filepath,
                        size_bytes=file_meta["size_bytes"],
                        sha256=file_meta["sha256"],
                        exists_status="present",
                        status="draft",
                        metadata={
                            "source_tool": "generate_image",
                            "prompt": gi_full_prompt[:500],
                            "negative_prompt": gi_negative[:300],
                            "seed": gi_seed,
                            "steps": gi_steps,
                            "cfg": gi_cfg,
                            "width": gi_width,
                            "height": gi_height,
                            "checkpoint": "[profile]" if gi_recipe["profile_active"] and gi_ckpt else gi_ckpt,
                            "sampler": gi_recipe["sampler_name"],
                            "scheduler": gi_recipe["scheduler"],
                            "model_sampling": gi_recipe["model_sampling"],
                            "vae": "[profile]" if gi_recipe["profile_active"] and gi_vae else gi_vae,
                            "workflow": "[profile]" if gi_recipe["profile_active"] and gi_recipe["workflow_name"] else "",
                            "persona_image_profile": gi_recipe["profile_metadata"],
                            "auto_enhanced": gi_auto_enhanced,
                            "size_bytes": file_meta["size_bytes"],
                            "sha256": file_meta["sha256"],
                        },
                    )
                except Exception as e:
                    print(f"[FileTrack] {e}")
                if artifact:
                    await _fire_artifact_created_event()
                await events.emit(conv_id, "file_ready", {
                    "filename": filename, "url": download_url,
                    "is_image": True,
                    "artifact_id": (artifact or {}).get("id"),
                    "kind": "image",
                    "mime_type": "image/png",
                })
                md_lines.append(f"![{filename}]({download_url})")
                md_lines.append(f"**[Download {filename}]({download_url})**")
            if not md_lines:
                await events.emit(conv_id, "tool_error", {
                    "tool": "generate_image", "icon": "image",
                    "status": "Image fetch failed",
                    "detail": gi_detail_json,
                })
                comfyui.finish_job(prompt_id)
                return "ERROR: Generated image could not be fetched from ComfyUI."
            await events.emit(conv_id, "tool_end", {
                "tool": "generate_image", "icon": "image",
                "status": f"Image ready ({int(time.time() - _gi_t0)}s)",
                "detail": gi_detail_json,
            })
            # Hand GPU 1 back to Ollama between generations
            await comfyui.free_memory()
            # HyprChat downloaded the images above — erase ComfyUI's traces
            # (history entry + file copies when the cleanup node is installed)
            await comfyui.forget_job(prompt_id)
            md_lines.append(f"Seed: `{gi_seed}` · steps {gi_steps} · "
                            f"{gi_width}×{gi_height} "
                            f"(reuse the seed for reproducible variations)")
            return "\n\n".join(md_lines)

        elif name == "download_project":
            directory = args.get("directory", "/root")
            _download_warning_lines = []
            await events.emit(conv_id, "tool_start", {"tool": "download_project", "icon": "code", "status": f"Packaging: {directory}"})
            dirname = directory.rstrip("/").split("/")[-1] or "project"
            # Clean up auto-generated UUIDs from directory names (project-abc12345 → project)
            # But keep meaningful names like "portscout" or "weather-dashboard"
            if re.match(r'^project-[a-f0-9]{4,8}$', dirname):
                dirname = "project"
            tarname = f"{dirname}.tar.gz"
            qdir = shlex.quote(directory)
            qtarname = shlex.quote(f"/tmp/{tarname}")
            exclude_args = _project_archive_tar_exclude_args()
            include_filter = _project_archive_find_include_filter()
            exclude_expr = _project_archive_find_exclude_expr()
            summary_cmd = (
                f"cd {qdir} && "
                f"included=$(find . -type f {include_filter} | wc -l); "
                f"excluded=$(find . \\( {exclude_expr} \\) -print | wc -l); "
                f"excluded_list=$(find . \\( {exclude_expr} \\) -print | sort | head -20); "
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
                "command": f"cd {qdir} && tar czf {qtarname} {exclude_args} . 2>&1" + ("" if os.environ.get("HEPHAESTUS_TOKEN") else f" && base64 -w0 {qtarname}"),
                "timeout": 60
            }, timeout=70)
            result = r.json()
            if os.environ.get('HEPHAESTUS_TOKEN'):
                if result.get('exit_code')!=0: raise ValueError('Project packaging failed: '+result.get('stdout','')[-2000:])
                from hephaestus_scope import download_bytes
                raw=base64.b64encode(await download_bytes(http,f'/tmp/{tarname}',config.MAX_UPLOAD_SIZE_MB*1024*1024)).decode()
            else: raw = result.get("stdout", "").strip()
            b64_match = re.search(r'([A-Za-z0-9+/\n]{100,}={0,2})$', raw)
            b64_data = b64_match.group(1).replace("\n", "").strip() if b64_match else ""
            if b64_data:
                estimated_size = len(b64_data) * 3 // 4
                if estimated_size > config.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
                    await events.emit(conv_id, "tool_end", {"tool": "download_project", "icon": "code",
                        "status": f"Archive too large ({estimated_size // (1024*1024)}MB > {config.MAX_UPLOAD_SIZE_MB}MB limit)"})
                    return f"ERROR: Project archive too large (exceeds {config.MAX_UPLOAD_SIZE_MB}MB limit)"
                os.makedirs(config.SANDBOX_OUTPUTS_DIR, exist_ok=True)
                # Immutable per-artifact storage: each delivery gets a unique
                # on-disk path so a later repackage never overwrites a prior
                # artifact's bytes. We ALSO (re)write the friendly {tarname} copy
                # as the "latest" file so back-compat /api/downloads/{tarname}
                # markdown links in chat keep resolving to the newest package.
                cf_id = f"cf-{uuid.uuid4().hex[:8]}"
                unique_name = f"{dirname}-{cf_id[3:]}.tar.gz"
                filepath = os.path.join(config.SANDBOX_OUTPUTS_DIR, unique_name)
                _decoded = base64.b64decode(b64_data)
                with open(filepath, "wb") as f:
                    f.write(_decoded)
                latest_path = os.path.join(config.SANDBOX_OUTPUTS_DIR, tarname)
                try:
                    with open(latest_path, "wb") as f:
                        f.write(_decoded)
                except Exception as _le:
                    print(f"[download_project] latest-copy write failed: {_le}")
                file_meta = _artifact_file_metadata(filepath)
                # Staleness anchor: record the project's current git commit so a
                # later edit cycle (which advances HEAD via _git_checkpoint) marks
                # this artifact stale. Best-effort; DB-time detection works without it.
                _source_commit = ""
                try:
                    _gc = await http.post(f"{config.CODEBOX_URL}/command", json={
                        "command": f"cd {qdir} && git rev-parse HEAD 2>/dev/null",
                        "timeout": 10,
                    }, timeout=15)
                    if _gc.status_code == 200:
                        _source_commit = (_gc.json().get("stdout", "") or "").strip().split("\n")[0][:40]
                except Exception as _gce:
                    print(f"[download_project] source commit lookup failed: {_gce}")
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
                            "source_commit": _source_commit,
                        },
                    )
                except Exception as e:
                    print(f"[FileTrack] {e}")
                if artifact:
                    await _fire_artifact_created_event()
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

            # Project .venv first (per-project isolation); fall back to the
            # shared scratch venv for pre-.venv projects and loose test dirs.
            _pytest_py = ("if [ -x .venv/bin/python3 ]; then PY=.venv/bin/python3; "
                          "else PY=/root/venv/bin/python3; fi; $PY")
            test_cmds = {
                "pytest": f"cd {shlex.quote(path)} && {_pytest_py} -m pytest -v --tb=short 2>&1",
                "jest": f"cd {shlex.quote(path)} && npx jest --verbose 2>&1",
                "vitest": f"cd {shlex.quote(path)} && npx vitest run 2>&1",
                "npm": f"cd {shlex.quote(path)} && npm test 2>&1",
                "cargo": f"cd {shlex.quote(path)} && cargo test 2>&1",
                "go": f"cd {shlex.quote(path)} && go test ./... -v 2>&1",
            }
            from agents.language_adapters import hermetic_prefix as _hp
            cmd = _hp() + test_cmds.get(framework, test_cmds["pytest"])
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
            return "This workflow uses a retired engine. Its history is read-only; start a new modern project from retained source."

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
            return _full_report

        elif name == "conspiracy_research":
            topic = args.get("topic", "")
            angle = args.get("angle", "evidence")
            depth = args.get("depth", 4)
            if isinstance(depth, str):
                label = depth.strip().lower()
                depth = {"quick": 3, "standard": 4, "deep": 5}.get(label, depth)
            try:
                depth = int(depth)
            except (TypeError, ValueError):
                depth = 4
            depth = max(3, min(5, depth))

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
            run_code = build_custom_tool_code(ct.get("code") or "", name, args)
            try:
                result = await run_custom_tool_code(http, run_code)
                stdout = (result.get("stdout") or "").strip()
                stderr = (result.get("stderr") or "").strip()

                # Auto-install only modules the tool's own code imports, then retry once.
                mod_match = re.search(r"ModuleNotFoundError: No module named '([\w.]+)'", stderr)
                if mod_match:
                    mod_root = mod_match.group(1).split(".")[0]
                    if mod_root in tool_import_names(ct.get("code") or ""):
                        pkg = pip_name_for_import(mod_root)
                        await events.emit(conv_id, "tool_start", {
                            "tool": name, "icon": "code",
                            "status": f"Installing {pkg} for {name}...",
                        })
                        if await pip_install_in_venv(http, pkg):
                            result = await run_custom_tool_code(http, run_code)
                            stdout = (result.get("stdout") or "").strip()
                            stderr = (result.get("stderr") or "").strip()
                        else:
                            stderr += f"\n(Automatic install of '{pkg}' into the sandbox venv failed.)"

                success = result.get("exit_code", -1) == 0 or result.get("success", False)
                await events.emit(conv_id, "tool_end", {
                    "tool": name, "icon": "code",
                    "status": f"{'OK' if success else 'FAILED'} {name}",
                })
                if not success and f"NameError: name '{name}' is not defined" in stderr:
                    return (
                        f"Custom tool '{name}' failed: the code does not define a function named '{name}'. "
                        f"The tool name must match the function name — edit the tool in the Tools panel.\n\n{stderr[-500:]}"
                    )
                return stdout or stderr or "No output"
            except Exception as exec_e:
                await events.emit(conv_id, "tool_error", {"tool": name, "icon": "code", "status": f"Error: {str(exec_e)}"})
                return f"**Custom tool error ({name}):** {str(exec_e)}"

        else:
            return f"Unknown tool: {name}"
    except Exception as e:
        await events.emit(conv_id, "tool_error", {"tool": name, "icon": "code", "status": f"Error: {str(e)}"})
        return f"**Tool error ({name}):** {str(e)}"

def _v2_name_match(name):
    """Recognize the maintained persona and its historical display name."""
    return "daedalus" in (name or "").lower() or re.search(r"\bv2\b", name or "", re.I) is not None
