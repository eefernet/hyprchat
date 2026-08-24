"""Codebox-backed utility tool handlers used by tools.exec_tool."""
from __future__ import annotations

import asyncio
import ast
import base64
import json
import re
import shlex
import sys
import time

import config


CODEBOX_TOOL_NAMES = {
    "execute_code",
    "run_shell",
    "write_file",
    "read_file",
    "list_files",
    "delete_file",
    "search_files",
    "diff_files",
    "git_init",
    "git_diff",
    "git_commit",
    "lint_code",
}


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_sandbox_venv_ready = False


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


async def _ensure_venv(http):
    """Lazily create a Python venv in the CodeBox sandbox."""
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
            "timeout": 30,
        }, timeout=35)
        result = r.json()
        if "VENV_OK" in result.get("stdout", ""):
            _sandbox_venv_ready = True
            print("[SANDBOX] venv ready at /root/venv")
            await ensure_data_stack(http)
            return True
    except Exception as e:
        print(f"[SANDBOX] venv setup error: {e}")
    return False


_data_stack_ready = False
_data_stack_attempted = False
_data_stack_lock: asyncio.Lock | None = None


async def ensure_data_stack(http):
    """One-time install of pandas/openpyxl/sympy in the sandbox venv.

    Staged chat data files (/root/chat_files/...) are analyzed via
    execute_code, which has no pip access in plain chats — so the stack must
    already be importable. Cheap no-op once installed (import probe only);
    the staging route pre-warms this in the background so the install usually
    finishes before the model's first execute_code call.
    """
    global _data_stack_ready, _data_stack_attempted, _data_stack_lock
    if _data_stack_ready:
        return True
    if _data_stack_lock is None:
        _data_stack_lock = asyncio.Lock()
    async with _data_stack_lock:
        if _data_stack_ready or _data_stack_attempted:
            return _data_stack_ready
        _data_stack_attempted = True
        try:
            r = await http.post(f"{config.CODEBOX_URL}/command", json={
                "command": (
                    "/root/venv/bin/python3 -c 'import pandas, openpyxl, sympy' 2>/dev/null || "
                    "/root/venv/bin/pip3 install -q pandas openpyxl sympy >/dev/null 2>&1; "
                    "/root/venv/bin/python3 -c 'import pandas, openpyxl, sympy' 2>/dev/null && echo DATA_STACK_OK"
                ),
                "timeout": 300,
            }, timeout=310)
            if "DATA_STACK_OK" in r.json().get("stdout", ""):
                _data_stack_ready = True
                print("[SANDBOX] data stack ready (pandas, openpyxl, sympy)")
                return True
            print("[SANDBOX] data stack install did not complete (pandas/openpyxl/sympy unavailable)")
        except Exception as e:
            print(f"[SANDBOX] data stack setup error: {e}")
        return False


# Common import-name → pip-package mismatches for custom-tool auto-install.
_IMPORT_TO_PIP = {
    "bs4": "beautifulsoup4",
    "PIL": "pillow",
    "yaml": "PyYAML",
    "cv2": "opencv-python-headless",
    "dateutil": "python-dateutil",
    "sklearn": "scikit-learn",
    "dotenv": "python-dotenv",
}


def tool_import_names(code: str) -> set[str]:
    """Root module names imported anywhere in the code, minus the stdlib."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.level:
                roots.add(node.module.split(".")[0])
    return {r for r in roots if r and r not in sys.stdlib_module_names}


def pip_name_for_import(module_root: str) -> str:
    return _IMPORT_TO_PIP.get(module_root, module_root)


def build_custom_tool_code(tool_code: str, func_name: str, args: dict) -> str:
    """Assemble the runnable script for a custom tool.

    Args travel as b64-encoded JSON and are splatted as **kwargs, so quoting,
    newlines, and non-identifier keys can't break the generated script.
    """
    args_b64 = base64.b64encode(json.dumps(args or {}).encode("utf-8")).decode("ascii")
    return (
        f"{tool_code}\n\n"
        "import base64 as _hc_b64, json as _hc_json\n"
        f"_hc_args = _hc_json.loads(_hc_b64.b64decode('{args_b64}').decode('utf-8'))\n"
        f"_hc_result = {func_name}(**_hc_args)\n"
        "print(_hc_result if _hc_result is not None else '')\n"
    )


async def run_custom_tool_code(http, run_code: str, timeout: int = 30) -> dict:
    """Run assembled custom-tool code through the shared sandbox venv."""
    await _ensure_venv(http)
    b64 = base64.b64encode(run_code.encode()).decode()
    cmd = (
        f"cd /root && printf '%s' {shlex.quote(b64)} | base64 -d > /tmp/_hc_custom_tool.py && "
        f"/root/venv/bin/python3 /tmp/_hc_custom_tool.py"
    )
    r = await http.post(
        f"{config.CODEBOX_URL}/command",
        json={"command": cmd, "timeout": timeout},
        timeout=timeout + 10,
    )
    return r.json()


async def pip_install_in_venv(http, package: str, timeout: int = 120) -> bool:
    """Best-effort pip install into the shared sandbox venv."""
    await _ensure_venv(http)
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": f"/root/venv/bin/pip3 install -q {shlex.quote(package)} && echo PIP_OK",
                  "timeout": timeout},
            timeout=timeout + 10,
        )
        return "PIP_OK" in (r.json().get("stdout") or "")
    except Exception as e:
        print(f"[SANDBOX] pip install {package} failed: {e}")
        return False


async def run_codebox_tool(name: str, args: dict, *, http, events, conv_id: str) -> str:
    if name == "execute_code":
        code = args.get("code", "")
        language = args.get("language", "python")
        await events.emit(conv_id, "tool_start", {
            "tool": "execute_code", "icon": "code",
            "status": f"Running {language} code...",
        })
        start_time = time.time()

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

    if name == "run_shell":
        command = args.get("command", "")
        shell_timeout = config.EXECUTION_TIMEOUT
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
        if r.status_code != 200:
            # e.g. codebox deny-list 400 ({"detail": "Blocked dangerous command
            # pattern"}) — the command never ran; never report exit 0.
            try:
                _detail = str(r.json().get("detail", ""))[:200]
            except Exception:
                _detail = ""
            _detail = _detail or f"HTTP {r.status_code}"
            await events.emit(conv_id, "tool_end", {
                "tool": name, "icon": "terminal",
                "status": f"FAILED (rejected): {command[:50]}",
                "detail": json.dumps({"command": command, "error": _detail}),
            })
            return f"ERROR: Codebox rejected the command ({_detail}). The command did not run."
        result = r.json()
        stdout = _strip_ansi(result.get("stdout", "")).strip()
        stderr = _strip_ansi(result.get("stderr", "")).strip()
        exit_code = result.get("exit_code", result.get("returncode", 1))
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

        dev_server_cmds = ("npm start", "npm run dev", "npm run serve", "npx vite", "yarn dev", "yarn start", "python3 -m http.server", "python -m http.server", "flask run", "uvicorn")
        if any(ds in cmd_stripped for ds in dev_server_cmds) and (not stdout.strip() or len(stdout.strip()) < 50):
            result_text += (
                "\n---\n⚠️ This looks like a dev server command. Dev servers run forever and WILL time out in this sandbox. "
                "Do NOT retry this command. The project files are already built and ready — "
                "use download_project to deliver them to the user instead."
            )
        return result_text

    if name == "write_file":
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

    if name == "read_file":
        path = args.get("path", "/root")
        await events.emit(conv_id, "tool_start", {"tool": "read_file", "icon": "code", "status": f"Reading: {path}"})
        r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": f"cat {shlex.quote(path)} 2>&1", "timeout": 10}, timeout=15)
        result = r.json()
        content_out = result.get("stdout", "")
        await events.emit(conv_id, "tool_end", {"tool": "read_file", "icon": "code", "status": f"Read {len(content_out)} chars: {path}"})
        return f"**{path}** ({len(content_out)} chars):\n```\n{content_out[:10000]}\n```"

    if name == "list_files":
        path = args.get("path", "/root")
        await events.emit(conv_id, "tool_start", {"tool": "list_files", "icon": "terminal", "status": f"ls {path}"})
        r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": f"ls -lahF {shlex.quote(path)} 2>&1", "timeout": 10}, timeout=15)
        result = r.json()
        await events.emit(conv_id, "tool_end", {"tool": "list_files", "icon": "terminal", "status": f"Listed: {path}"})
        return f"```\n{result.get('stdout', '(empty)')}\n```"

    if name == "delete_file":
        path = args.get("path", "")
        if not path or path in ("/", "/root", "/etc", "/usr", "/bin", "/tmp"):
            return f"ERROR: Refusing to delete protected path: {path}"
        await events.emit(conv_id, "tool_start", {"tool": "delete_file", "icon": "terminal", "status": f"Deleting: {path}"})
        # `--` breaks the codebox deny-list's "rm -rf /" substring match, which
        # otherwise 400-blocks EVERY absolute-path delete (see the
        # language_adapters.py note on the same filter).
        r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": f"rm -rf -- {shlex.quote(path)}", "timeout": 10}, timeout=15)
        try:
            result = r.json()
        except Exception:
            result = {}
        exit_code = result.get("exit_code", result.get("returncode", 1)) if r.status_code == 200 else 1
        ok = exit_code == 0
        await events.emit(conv_id, "tool_end", {"tool": "delete_file", "icon": "terminal", "status": f"{'Deleted' if ok else 'Failed'}: {path}"})
        return f"Deleted: {path}" if ok else f"ERROR: Delete failed (exit {exit_code}): {result.get('stderr', '')[:200]}"

    if name == "search_files":
        pattern = args.get("pattern", "")
        if not pattern:
            return "ERROR: pattern is required"
        search_path = args.get("path", "/root")
        file_pattern = args.get("file_pattern", "")
        await events.emit(conv_id, "tool_start", {"tool": "search_files", "icon": "search", "status": f"Searching: {pattern[:40]}"})
        cmd = "grep -rn"
        if file_pattern:
            cmd += f" --include={shlex.quote(file_pattern)}"
        cmd += f" {shlex.quote(pattern)} {shlex.quote(search_path)} 2>/dev/null | head -60"
        r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 15}, timeout=20)
        result = r.json()
        output = result.get("stdout", "").strip()
        match_count = len(output.splitlines()) if output else 0
        await events.emit(conv_id, "tool_end", {"tool": "search_files", "icon": "search", "status": f"Found {match_count} matches"})
        return output if output else f"No matches found for '{pattern}' in {search_path}"

    if name == "diff_files":
        path_a = args.get("path_a", "")
        path_b = args.get("path_b", "")
        if not path_a or not path_b:
            return "ERROR: path_a and path_b are required"
        await events.emit(conv_id, "tool_start", {"tool": "diff_files", "icon": "terminal", "status": "Diffing files..."})
        cmd = f"diff -u {shlex.quote(path_a)} {shlex.quote(path_b)}"
        r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 10}, timeout=15)
        result = r.json()
        output = result.get("stdout", "").strip()
        exit_code = result.get("exit_code", 0)
        await events.emit(conv_id, "tool_end", {"tool": "diff_files", "icon": "terminal", "status": "Diff complete"})
        if exit_code == 0:
            return "Files are identical."
        if exit_code == 1:
            return output[:8000] if output else "Files differ but no diff output."
        return f"ERROR: diff failed: {result.get('stderr', '')[:200]}"

    if name == "git_init":
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

    if name == "git_diff":
        path = args.get("path", "/root")
        await events.emit(conv_id, "tool_start", {"tool": "git_diff", "icon": "terminal", "status": "Checking changes..."})
        cmd = f"cd {shlex.quote(path)} && git diff && git diff --cached && git status --short"
        r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 10}, timeout=15)
        result = r.json()
        output = result.get("stdout", "").strip()
        await events.emit(conv_id, "tool_end", {"tool": "git_diff", "icon": "terminal", "status": "Done"})
        return output[:8000] if output else "No changes detected."

    if name == "git_commit":
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

    if name == "lint_code":
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

    raise ValueError(f"Unhandled Codebox tool: {name}")
