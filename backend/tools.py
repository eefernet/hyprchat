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

import config
import database as db
from research import run_deep_research, run_conspiracy_research, _fetch_page, _source_tier

# Strip ANSI escape codes from terminal output before feeding back to the model
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


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
            "description": "Multi-source deep research with AI synthesis. Runs parallel searches, reads top pages, and produces a comprehensive report.",
            "parameters": {"type": "object", "properties": {
                "topic": {"type": "string", "description": "Research topic"},
                "depth": {"type": "integer", "description": "Depth 1-5 (1=quick, 3=standard, 5=exhaustive)"},
                "focus": {"type": "string", "description": "Optional focus area"},
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

async def exec_tool(http, events, name: str, args: dict, conv_id: str, custom_tool_map: dict = None, conv_model: str = "", kb_ids: list = None) -> str:
    """Execute a built-in or custom tool and return the result string."""
    custom_tool_map = custom_tool_map or {}
    try:
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
            r = await http.get(url, timeout=15, follow_redirects=True)
            if r.status_code >= 400:
                await events.emit(conv_id, "tool_end", {"tool": "fetch_url", "icon": "globe", "status": f"HTTP {r.status_code}: {url[:40]}"})
                return f"ERROR: HTTP {r.status_code} fetching {url}"
            text = r.text[:config.MAX_FETCH_CHARS]
            text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            await events.emit(conv_id, "tool_end", {"tool": "fetch_url", "icon": "globe", "status": f"Read {len(text)} chars"})
            return f"**Content from {url}:**\n\n{text[:config.MAX_FETCH_CHARS]}"

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
                download_url = f"/api/downloads/{filename}"
                await events.emit(conv_id, "tool_end", {"tool": "download_file", "icon": "code",
                    "status": f"{filename} ready",
                    "detail": json.dumps({"file": filename, "path": path, "download_url": download_url}),
                })
                _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
                _ext = os.path.splitext(filename)[1].lower()
                await events.emit(conv_id, "file_ready", {
                    "filename": filename, "url": download_url,
                    "is_image": _ext in _IMAGE_EXTS,
                })
                try:
                    cf_id = f"cf-{uuid.uuid4().hex[:8]}"
                    await db.add_conversation_file(cf_id, conv_id, filename, download_url)
                except Exception as e:
                    print(f"[FileTrack] {e}")
                if _ext in _IMAGE_EXTS:
                    return f"![{filename}]({download_url})\n\n**[Download {filename}]({download_url})**"
                return f"**[Download {filename}]({download_url})**"
            else:
                await events.emit(conv_id, "tool_end", {"tool": "download_file", "icon": "code", "status": f"File not found: {path}"})
                return f"ERROR: File not found or could not read: {path}"

        elif name == "download_project":
            directory = args.get("directory", "/root")
            await events.emit(conv_id, "tool_start", {"tool": "download_project", "icon": "code", "status": f"Packaging: {directory}"})
            dirname = directory.rstrip("/").split("/")[-1] or "project"
            # Clean up auto-generated UUIDs from directory names (project-abc12345 → project)
            # But keep meaningful names like "portscout" or "weather-dashboard"
            if re.match(r'^project-[a-f0-9]{4,8}$', dirname):
                dirname = "project"
            tarname = f"{dirname}.tar.gz"
            qdir = shlex.quote(directory)
            qtarname = shlex.quote(f"/tmp/{tarname}")
            r = await http.post(f"{config.CODEBOX_URL}/command", json={
                "command": f"cd {qdir} && tar czf {qtarname} --exclude='node_modules' --exclude='.git' --exclude='__pycache__' --exclude='venv' --exclude='.cache' --exclude='.npm' --exclude='package-lock.json' . 2>&1 && base64 -w0 {qtarname}",
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
                download_url = f"/api/downloads/{tarname}"
                await events.emit(conv_id, "tool_end", {"tool": "download_project", "icon": "code",
                    "status": f"{tarname} ready",
                    "detail": json.dumps({"file": tarname, "directory": directory, "download_url": download_url}),
                })
                await events.emit(conv_id, "file_ready", {
                    "filename": tarname, "url": download_url,
                })
                try:
                    cf_id = f"cf-{uuid.uuid4().hex[:8]}"
                    await db.add_conversation_file(cf_id, conv_id, tarname, download_url)
                except Exception as e:
                    print(f"[FileTrack] {e}")
                return f"**[Download {tarname}]({download_url})**"
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

        elif name == "plan_project":
            task = args.get("task", "")
            language = args.get("language", "python")
            constraints = args.get("constraints", "")
            if not task:
                return "ERROR: task is required"
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
                    json={"model": planning_model, "messages": [{"role": "user", "content": plan_prompt}], "stream": False, "options": {"temperature": 0.3, "num_ctx": 16384}},
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

            parts = [f"# Deep Research: {topic}\n"]
            parts.append(f"*{sc} sources, {ss} searches, {pr} pages read ({tm:.0f}s)*\n")
            if entities:
                parts.append(f"**Key entities:** {', '.join(entities[:10])}\n")
            parts.append(report)
            if sources:
                parts.append("\n\n---\n## Sources\n")
                for s in sources[:20]:
                    parts.append(f"[{s.get('index','?')}] [{s.get('title','?')}]({s.get('url','')})")
            return "\n".join(parts)

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
            coder_model = config.CODER_MODEL or conv_model or config.DEFAULT_MODEL

            # Inject KB context so OpenHands agent has access to uploaded documentation
            if kb_ids and task:
                try:
                    import rag
                    chunks = await rag.query(kb_ids, task, top_k=4)
                    if chunks:
                        kb_prior = rag.format_context(chunks, max_chars=3000)
                        kb_section = (
                            "\n\n--- Knowledge Base (uploaded reference docs) ---\n"
                            + kb_prior
                        )
                        context = (context + kb_section) if context else kb_section.strip()
                        print(f"[CODEGEN RAG] Pre-loaded {len(chunks)} KB chunks for generate_code: {task[:60]}")
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

            await events.emit(conv_id, "tool_start", {
                "tool": "generate_code", "icon": "wand",
                "status": f"🤖 OpenHands agent building {language} project...",
            })
            print(f"[CODEGEN:OH] model={coder_model} lang={language} num_ctx={num_ctx} task={task[:100]!r}")

            # Auto-resolve project_id: if model didn't pass one, check for an
            # active uploaded project on this conversation so OpenHands works
            # inside the user's uploaded project directory.
            _oh_project_id = args.get("project_id", "")
            if not _oh_project_id and conv_id:
                try:
                    _active = await db.get_coding_project_by_conv(conv_id)
                    if _active and _active.get("openhands_project_id"):
                        _oh_project_id = _active["openhands_project_id"]
                        print(f"[CODEGEN:OH] Auto-attached active project {_oh_project_id} for conv {conv_id}")
                except Exception as _ap_e:
                    print(f"[CODEGEN:OH] Active project lookup failed (non-fatal): {_ap_e}")

            oh_payload = {
                "task": task, "model": coder_model,
                "ollama_url": config.OLLAMA_URL,
                "max_rounds": max_rounds,
                "num_ctx": num_ctx,
                "language": language,
                "context": context,
                "project_id": _oh_project_id,
            }

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

            # Try SSE streaming first, fall back to blocking /run
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
                                })
                            elif evt.get("type") in ("done", "error"):
                                result = evt
                                break
            except Exception as sse_err:
                print(f"[CODEGEN:OH] SSE stream failed ({type(sse_err).__name__}: {sse_err}), falling back to /run", flush=True)
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
                        })
                        return f"ERROR: OpenHands returned HTTP {oh_resp.status_code}: {oh_resp.text[:200]}"
                    result = oh_resp.json()
                except Exception as oh_e:
                    await events.emit(conv_id, "tool_end", {
                        "tool": "generate_code", "icon": "code",
                        "status": f"OpenHands request failed: {oh_e}",
                    })
                    return f"ERROR: OpenHands request failed: {oh_e}. Try write_file + run_shell instead."

            if not result:
                await events.emit(conv_id, "tool_end", {
                    "tool": "generate_code", "icon": "code",
                    "status": "OpenHands returned no result",
                })
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

                # Auto-package project for download
                download_result = ""
                if files:
                    try:
                        if len(files) == 1:
                            # Single file — use download_file
                            await events.emit(conv_id, "tool_start", {
                                "tool": "download_file", "icon": "code",
                                "status": f"Preparing: {files[0]}",
                            })
                            download_result = await exec_tool(
                                http, events, "download_file",
                                {"path": files[0]}, conv_id, {}, conv_model=conv_model
                            )
                        else:
                            # Multi-file — package as tar.gz
                            await events.emit(conv_id, "tool_start", {
                                "tool": "download_project", "icon": "package",
                                "status": "Packaging project for download...",
                            })
                            download_result = await exec_tool(
                                http, events, "download_project",
                                {"directory": project_dir}, conv_id, {}, conv_model=conv_model
                            )
                        print(f"[CODEGEN:OH] Auto-download: {download_result[:100]}")
                    except Exception as dl_e:
                        print(f"[CODEGEN:OH] Auto-download failed: {dl_e}")

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

                if files:
                    resp = (
                        f"PROJECT COMPLETE. OpenHands agent built the project "
                        f"(model: {coder_model}, {duration}s, {len(steps)} steps, project_id: {_project_id}).\n\n"
                        f"**Files created ({len(files)}):**\n{file_list}\n"
                    )
                    if steps_summary:
                        resp += f"\n**Agent activity (last steps):**\n{steps_summary}\n"
                    if download_result and "Download" in download_result:
                        resp += f"\n{download_result}\n"
                    if summary:
                        resp += f"\n**Agent summary:** {summary[:300]}\n"
                    if code_review:
                        resp += f"\n**Key file contents for review:**\n{code_review}\n"
                    resp += (
                        f"\nREVIEW the file contents above. Evaluate whether the code actually "
                        f"fulfills the user's request — not just scaffolding/boilerplate. "
                        f"If the output is incomplete or doesn't match what was asked, call "
                        f"generate_code again with project_id='{_project_id}' and a MORE DETAILED "
                        f"task description explaining exactly what's wrong and what to fix.\n"
                        f"If the output looks good, respond to the user with:\n"
                        f"1. What was built and its features\n"
                        f"2. The download link\n"
                        f"3. How to run it locally\n"
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
                    return resp
                else:
                    # Agent ran but produced no files — treat as failure
                    print(f"[CODEGEN:OH] Agent finished but created 0 files — reporting as error")
                    await events.emit(conv_id, "tool_end", {
                        "tool": "generate_code", "icon": "wand",
                        "status": f"🤖 OpenHands: 0 files (model may not support tools)",
                    })
                    error_detail = summary[:200] if summary else "Agent completed but produced no files"
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
                                        elif evt.get("type") in ("done", "error"):
                                            result = evt
                                            break
                                else:
                                    result = None
                    except Exception as retry_err:
                        print(f"[CODEGEN:OH] SSE retry failed, trying blocking /run: {retry_err}")
                        try:
                            oh_resp = await http.post(f"{openhands_url}/run", json=oh_payload, timeout=600)
                            if oh_resp.status_code == 200:
                                result = oh_resp.json()
                            else:
                                result = None
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
                return err_resp

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
