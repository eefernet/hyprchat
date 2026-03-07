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
from research import run_deep_research, run_conspiracy_research


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
            "description": "Search the web for documentation, APIs, error solutions, or current information.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Search query"},
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
    "research_error": {
        "type": "function",
        "function": {
            "name": "research_error",
            "description": "Search the web for help with a specific error message.",
            "parameters": {"type": "object", "properties": {
                "error_message": {"type": "string", "description": "Error message or traceback"},
                "language": {"type": "string", "description": "Programming language"},
            }, "required": ["error_message"]},
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
            "description": "Investigative research using leaked documents, FOIA, WikiLeaks, alt-media, and court records.",
            "parameters": {"type": "object", "properties": {
                "topic": {"type": "string", "description": "Topic to investigate"},
                "angle": {"type": "string", "description": "Angle: evidence, key_players, timeline, debunk, documents, connections"},
                "depth": {"type": "integer", "description": "Depth 3-5 (default 4)"},
            }, "required": ["topic"]},
        },
    },
    "generate_code": {
        "type": "function",
        "function": {
            "name": "generate_code",
            "description": "Generate code by delegating to a code-specialized model. Use for substantial code generation tasks. Returns clean code ready to write_file or execute_code.",
            "parameters": {"type": "object", "properties": {
                "task": {"type": "string", "description": "What the code should do — be specific about requirements, inputs, outputs"},
                "language": {"type": "string", "description": "Programming language: python, javascript, rust, go, etc."},
                "context": {"type": "string", "description": "Optional context: error messages to fix, existing code to modify, constraints"},
            }, "required": ["task", "language"]},
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

    # Strip markdown code fences for parsing
    stripped = re.sub(r'```(?:json|tool_call|tool)?\s*\n?', '', content).strip().rstrip('`')

    # 1. Entire response is a single JSON tool call
    for _try_str in (stripped, _fix_json_newlines(stripped)):
        try:
            obj = json.loads(_try_str)
            if isinstance(obj, dict) and obj.get("name") in available_names and "arguments" in obj:
                return [{"function": {"name": obj["name"], "arguments": _normalize_tool_args(obj["arguments"])}}]
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    # 2. <tool_call>JSON</tool_call> tags (Qwen native format)
    tag_matches = re.findall(
        r'<tool[_\-]?call[s]?>\s*(.*?)\s*</tool[_\-]?call[s]?>',
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


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences from model output, returning clean code."""
    text = text.strip()
    # Remove ```lang ... ``` wrapper
    m = re.match(r'^```\w*\s*\n(.*?)```\s*$', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Remove leading ```lang and trailing ```
    text = re.sub(r'^```\w*\s*\n?', '', text)
    text = re.sub(r'\n?```\s*$', '', text)
    return text.strip()


# ── Tool execution dispatcher ──

async def exec_tool(http, events, name: str, args: dict, conv_id: str, custom_tool_map: dict = None, conv_model: str = "") -> str:
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
            stdout = result.get("stdout", "").strip()
            stderr = result.get("stderr", "").strip()
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
                elif "no such file" in combined_err or "not found" in combined_err and "command" not in combined_err:
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
            await events.emit(conv_id, "tool_end", {"tool": "research", "icon": "search", "status": f'{len(results)} results',
                "detail": json.dumps({"query": query, "results": [{"title": r.get("title",""), "url": r.get("url","")} for r in results[:5]]}),
            })
            parts = [f"**Search: {query}**\n"]
            for i, res in enumerate(results, 1):
                parts.append(f"{i}. **[{res.get('title', '')}]({res.get('url', '')})**\n   {res.get('content', '')}\n")
            return "\n".join(parts)

        elif name == "fetch_url":
            url = args.get("url", "")
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
            stdout = result.get("stdout", "").strip()
            stderr = result.get("stderr", "").strip()
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
            result_text = f"exit code: {exit_code}\n{out}{err}" or f"(exit code: {exit_code}, no output)"
            if not success:
                result_text += "\n---\nCommand failed. Check the error above and try a different approach or fix the command."
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

        elif name == "research_error":
            error_msg = args.get("error_message", "")
            language = args.get("language", "python")
            query = f"{language} {error_msg[:200]}"
            return await exec_tool(http, events, "research", {"query": query}, conv_id, custom_tool_map)

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
                await events.emit(conv_id, "file_ready", {
                    "filename": filename, "url": download_url,
                })
                try:
                    cf_id = f"cf-{uuid.uuid4().hex[:8]}"
                    await db.add_conversation_file(cf_id, conv_id, filename, download_url)
                except Exception as e:
                    print(f"[FileTrack] {e}")
                return f"**[Download {filename}]({download_url})**"
            else:
                await events.emit(conv_id, "tool_end", {"tool": "download_file", "icon": "code", "status": f"File not found: {path}"})
                return f"ERROR: File not found or could not read: {path}"

        elif name == "download_project":
            directory = args.get("directory", "/root")
            await events.emit(conv_id, "tool_start", {"tool": "download_project", "icon": "code", "status": f"Packaging: {directory}"})
            dirname = directory.rstrip("/").split("/")[-1] or "project"
            tarname = f"{dirname}.tar.gz"
            qdir = shlex.quote(directory)
            qtarname = shlex.quote(f"/tmp/{tarname}")
            r = await http.post(f"{config.CODEBOX_URL}/command", json={
                "command": f"cd {qdir} && tar czf {qtarname} . 2>&1 && base64 -w0 {qtarname}",
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

        elif name == "deep_research":
            topic = args.get("topic", "")
            depth = args.get("depth", 3)
            if isinstance(depth, str):
                depth = {"quick": 1, "standard": 3, "deep": 5}.get(depth, 3)
            depth = max(1, min(5, depth))
            focus = args.get("focus", "")
            mode = args.get("mode", "research")
            topic_b = args.get("topic_b", "")

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
                result = await run_deep_research(http, config.OLLAMA_URL, config.DEFAULT_MODEL, events, topic, depth, focus, mode, topic_b, conv_id)
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
            return await run_conspiracy_research(http, config.OLLAMA_URL, config.DEFAULT_MODEL, config.SEARXNG_URL, events, topic, angle, depth, conv_id)

        elif name == "generate_code":
            task = args.get("task", "")
            language = args.get("language", "python")
            context = args.get("context", "")
            await events.emit(conv_id, "tool_start", {
                "tool": "generate_code", "icon": "code",
                "status": f"Generating {language} code...",
            })
            coder_model = config.CODER_MODEL or conv_model or config.DEFAULT_MODEL
            print(f"[CODEGEN] model={coder_model} lang={language} task={task[:100]!r}")

            sys_prompt = (
                f"You are a code generator. Output ONLY {language} code. "
                "No markdown fences. No explanations before or after. No commentary. "
                "Write clean, working code with error handling and helpful inline comments."
            )
            user_prompt = f"Write {language} code for: {task}"
            if context:
                user_prompt += f"\n\nContext:\n{context}"

            try:
                r = await http.post(
                    f"{config.OLLAMA_URL}/api/chat",
                    json={
                        "model": coder_model,
                        "messages": [
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "stream": False,
                        "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 4096},
                    },
                    timeout=180,
                )
                if r.status_code != 200:
                    await events.emit(conv_id, "tool_end", {
                        "tool": "generate_code", "icon": "code",
                        "status": f"Coder model error (HTTP {r.status_code})",
                    })
                    return f"ERROR: Code generation failed — coder model returned HTTP {r.status_code}: {r.text[:200]}"
                resp = r.json()
                raw_code = resp.get("message", {}).get("content", "")
                code = _strip_code_fences(raw_code)
                line_count = code.count("\n") + 1
                await events.emit(conv_id, "tool_end", {
                    "tool": "generate_code", "icon": "code",
                    "status": f"Generated {line_count} lines of {language} (model: {coder_model})",
                })
                print(f"[CODEGEN] Done: {line_count} lines, {len(code)} chars")
                return f"**Generated {language} code** ({line_count} lines, model: {coder_model}):\n```{language}\n{code}\n```"
            except Exception as gen_e:
                await events.emit(conv_id, "tool_end", {
                    "tool": "generate_code", "icon": "code",
                    "status": f"Code generation failed: {str(gen_e)[:80]}",
                })
                return f"ERROR: Code generation failed: {gen_e}"

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
