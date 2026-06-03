"""
Aider fixer agent for uploaded projects.

This module owns the HyprChat-side durable run. The actual Aider process runs
inside the Codebox/OpenHands worker via /aider/run-stream, so the main service
does not need Aider installed locally.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

import httpx

import cancel_registry
import config
import database as db


_STATE_FIX_HINT_RE = re.compile(
    r"(no such column|no such table|database schema|schema mismatch|"
    r"no item with that key|keyerror|duplicate key|unique constraint|"
    r"stale (?:database|db|cache|state)|shared (?:database|db|cache|state)|"
    r"test_isolation_suspected)",
    re.I,
)
_STORAGE_BASENAME_HINTS = [
    "db.py", "database.py", "storage.py", "store.py", "repository.py",
    "db.js", "database.js", "storage.js", "store.js", "repository.js",
    "db.ts", "database.ts", "storage.ts", "store.ts", "repository.ts",
    "db.go", "database.go", "storage.go", "store.go", "repository.go",
    "db.rs", "database.rs", "storage.rs", "store.rs", "repository.rs",
    "Database.java", "Storage.java", "Repository.java",
]
_TEST_BASENAME_HINTS = [
    "test_cli.py", "test_db.py", "test_storage.py", "test_database.py",
    "cli.test.js", "db.test.js", "storage.test.js", "database.test.js",
    "cli.spec.ts", "db.spec.ts", "storage.spec.ts", "database.spec.ts",
]
_TASK_FILE_RE = re.compile(
    r"(?<![\w./-])(?:[\w.-]+/)*[\w.-]+"
    r"\.(?:py|js|jsx|ts|tsx|go|rs|java|kt|cs|php|rb|c|cc|cpp|h|hpp|json|toml|yaml|yml)\b"
)
_ENTRYPOINT_FIX_RE = re.compile(
    r"\b(import|entry\s*point|main|cli|command|commands|function\s+is\s+named|"
    r"cannot import|undefined|not defined|name mismatch)\b",
    re.I,
)
_DOTTED_MODULE_RE = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b")
_ENTRYPOINT_BASENAME_HINTS = [
    "__main__.py", "main.py", "cli.py", "commands.py",
    "index.js", "main.js", "cli.js", "commands.js",
    "index.ts", "main.ts", "cli.ts", "commands.ts",
    "main.go", "main.rs", "Main.java",
]


def _append_unique(paths: list[str], path: str) -> None:
    path = (path or "").strip()
    if path and path not in paths:
        paths.append(path)


def _allowed_files_from_issues(issue_envelope: dict) -> list[str]:
    out = []
    text_bits = [
        issue_envelope.get("summary") or "",
        issue_envelope.get("test_stdout_tail") or "",
        issue_envelope.get("test_stderr_tail") or "",
        issue_envelope.get("stdout_tail") or "",
        issue_envelope.get("stderr_tail") or "",
    ]
    for issue in (issue_envelope or {}).get("issues") or []:
        text_bits.extend([
            issue.get("summary") or "",
            issue.get("details") or "",
            issue.get("message") or "",
            " ".join(str(s) for s in issue.get("state_error_signals") or []),
            "test_isolation_suspected" if issue.get("test_isolation_suspected") else "",
        ])
        for path in issue.get("suggested_fix_scope") or []:
            if path and path not in out:
                out.append(path)
        path = issue.get("file")
        if path and path not in out:
            out.append(path)
    text = "\n".join(text_bits)
    for mention in re.findall(r"(?<![\w./-])[\w.-]+\.py\b", text):
        if mention and mention not in out:
            out.append(mention)
    # Python CLI import failures are often reported at __main__.py even though
    # the real dispatch/import mismatch lives in sibling cli.py or commands.py.
    if "__main__.py" in text or any(str(p).endswith("__main__.py") for p in out):
        for sibling in ("cli.py", "commands.py"):
            if sibling not in out:
                out.append(sibling)
    if _STATE_FIX_HINT_RE.search(text):
        for hint in _STORAGE_BASENAME_HINTS + _TEST_BASENAME_HINTS:
            if hint not in out:
                out.append(hint)
    return out


def _allowed_files_from_task(task: str) -> list[str]:
    """Infer a small initial Aider scope from the user's uploaded-project task."""
    out: list[str] = []
    text = task or ""
    for mention in _TASK_FILE_RE.findall(text):
        _append_unique(out, mention)

    if _ENTRYPOINT_FIX_RE.search(text):
        for dotted in _DOTTED_MODULE_RE.findall(text):
            parts = dotted.split(".")
            if len(parts) >= 2 and all(part and part[0].isalpha() or part.startswith("_") for part in parts):
                _append_unique(out, "/".join(parts) + ".py")
        for hint in _ENTRYPOINT_BASENAME_HINTS:
            _append_unique(out, hint)

    if _STATE_FIX_HINT_RE.search(text):
        for hint in _STORAGE_BASENAME_HINTS + _TEST_BASENAME_HINTS:
            _append_unique(out, hint)
    return out


async def run_aider_fix(http, events, conv_id: str, *,
                        project_dir: str, task: str,
                        issue_envelope: dict | None = None,
                        contract: dict | None = None,
                        model: str = "", test_cmd: str = "",
                        lint_cmd: str = "", allowed_files: list[str] | None = None,
                        project_id: str = "", parent_run_id: str = "",
                        workflow_id: str = "") -> dict:
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    started = time.time()
    issue_envelope = issue_envelope or {}
    contract = contract or {}
    seeded_allowed_files = list(allowed_files or [])
    for path in _allowed_files_from_issues(issue_envelope) + _allowed_files_from_task(task):
        _append_unique(seeded_allowed_files, path)
    allowed_files = seeded_allowed_files
    try:
        recent_runs = await db.get_runs_by_conversation(conv_id, limit=12) if conv_id else []
        for prior in recent_runs:
            if prior.get("role") != "aider.fix":
                continue
            if project_id and prior.get("project_id") and prior.get("project_id") != project_id:
                continue
            prior_env = prior.get("result_envelope") or {}
            for path in prior_env.get("files_touched") or []:
                if path.endswith(".py") and path not in allowed_files:
                    allowed_files.append(path)
    except Exception as e:
        print(f"[AIDER] recent touched-file scope lookup failed (non-fatal): {e}")
    aider_model = model or config.AIDER_MODEL or config.FIXER_MODEL or config.CODER_MODEL or config.DEFAULT_MODEL
    worker_url = (config.AIDER_WORKER_URL or config.OPENHANDS_URL).rstrip("/")
    source_role = issue_envelope.get("_source_role") or (
        "acceptance" if issue_envelope.get("acceptance_model") else "reviewer"
    )

    try:
        await db.create_run(run_id, conv_id, role="aider.fix",
                            project_id=project_id, parent_run_id=parent_run_id,
                            status="running")
    except Exception as e:
        print(f"[AIDER] create_run failed (non-fatal): {e}")
        run_id = ""

    if run_id:
        cancel_event = cancel_registry.register(run_id)
    else:
        cancel_event = None

    if workflow_id and run_id:
        try:
            await db.update_coder_workflow(
                workflow_id,
                state="fixing",
                active_run_id=run_id,
                artifact_status="not_ready",
            )
        except Exception as e:
            print(f"[AIDER] workflow start update failed (non-fatal): {e}")

    async def _step(action: str, detail: str = "", step: int | None = None):
        await events.emit(conv_id, "tool_progress", {
            "tool": "run_aider_fix", "icon": "wrench",
            "status": f"Aider {action}: {detail[:120]}" if detail else f"Aider {action}",
            "run_id": run_id, "step": step,
        })
        if run_id:
            try:
                await db.append_run_event(run_id, {
                    "type": "step", "step": step or int(time.time()),
                    "action": action, "detail": detail[:500],
                })
            except Exception:
                pass

    async def _finalize(run_status: str, envelope: dict):
        if run_id:
            try:
                await db.update_run(run_id, status=run_status,
                                    result_envelope=envelope, ended=True)
            except Exception as e:
                print(f"[AIDER] update_run failed (non-fatal): {e}")
            cancel_registry.cleanup(run_id)
        if workflow_id:
            try:
                state = "reviewing" if run_status == "succeeded" else "blocked"
                await db.update_coder_workflow(
                    workflow_id,
                    state=state,
                    active_run_id=run_id,
                    artifact_status="not_ready",
                )
            except Exception as e:
                print(f"[AIDER] workflow update failed (non-fatal): {e}")

    await events.emit(conv_id, "tool_start", {
        "tool": "run_aider_fix", "icon": "wrench",
        "status": f"Aider fixing uploaded project with {aider_model}",
        "run_id": run_id,
    })

    async def _signal_worker_cancel(reason: str):
        if not run_id:
            return
        try:
            await http.post(f"{worker_url}/aider/cancel/{run_id}", timeout=5)
            print(f"[AIDER] cancel signal sent to worker for {run_id} ({reason})")
        except Exception as e:
            print(f"[AIDER] cancel signal failed for {run_id}: {e}")

    cancel_watcher = None
    if cancel_event is not None:
        async def _watch_cancel():
            try:
                await cancel_event.wait()
            except asyncio.CancelledError:
                return
            await _signal_worker_cancel("user pressed Stop")
        cancel_watcher = asyncio.create_task(_watch_cancel())

    payload = {
        "project_dir": project_dir,
        "task": task,
        "issue_envelope": issue_envelope,
        "contract": contract,
        "model": aider_model,
        "ollama_url": config.OLLAMA_URL,
        "num_ctx": config.AIDER_NUM_CTX,
        "test_cmd": test_cmd or contract.get("aider_test_cmd") or contract.get("test_cmd") or "",
        "lint_cmd": lint_cmd or (contract.get("aider_lint_cmd") if contract.get("safe_lint") else "") or "",
        "allowed_files": allowed_files,
        "run_id": run_id,
        "auto_test": bool(config.AIDER_AUTO_TEST),
    }

    result = None
    step_n = 0
    try:
        await _step("health", worker_url, step=0)
        health = await http.get(f"{worker_url}/aider/health", timeout=8)
        if health.status_code != 200 or not (health.json().get("installed")):
            msg = health.text[:300] if health.status_code != 200 else "Aider is not installed in the worker venv"
            envelope = {
                "status": "error",
                "summary": msg,
                "project_dir": project_dir,
                "files_touched": [],
                "diff": "",
                "test_exit": None,
                "test_stdout_tail": "",
                "test_stderr_tail": "",
                "needs_review": False,
                "run_id": run_id,
            }
            await events.emit(conv_id, "tool_end", {
                "tool": "run_aider_fix", "icon": "wrench",
                "status": "Aider unavailable",
                "run_id": run_id,
            })
            await _finalize("failed", envelope)
            return envelope

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=900, write=10, pool=10)) as client:
                async with client.stream("POST", f"{worker_url}/aider/run-stream", json=payload) as resp:
                    if resp.status_code != 200:
                        raise ConnectionError(f"Aider stream HTTP {resp.status_code}: {await resp.aread()}")
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        try:
                            evt = json.loads(line[6:])
                        except Exception:
                            continue
                        if evt.get("type") == "step":
                            step_n += 1
                            detail = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", evt.get("detail", ""))
                            await _step(evt.get("action", "step"), detail, step=step_n)
                        elif evt.get("type") == "done":
                            result = evt
                            break
        except asyncio.CancelledError:
            await _signal_worker_cancel("chat stream cancelled")
            raise
        except Exception as e:
            await _step("fallback", f"stream failed: {type(e).__name__}: {e}", step=step_n + 1)
            resp = await http.post(f"{worker_url}/aider/run", json=payload, timeout=900)
            if resp.status_code != 200:
                raise RuntimeError(f"Aider worker HTTP {resp.status_code}: {resp.text[:300]}")
            result = resp.json()

        if not result:
            raise RuntimeError("Aider worker returned no result")

        worker_status = (result.get("status") or "error").lower()
        files = result.get("files_touched") or []
        envelope = {
            "status": "applied" if worker_status == "ok" else worker_status,
            "summary": result.get("summary") or "",
            "project_dir": result.get("project_dir") or project_dir,
            "files_touched": files,
            "diff": result.get("diff") or "",
            "test_exit": result.get("test_exit"),
            "test_stdout_tail": result.get("test_stdout_tail") or "",
            "test_stderr_tail": result.get("test_stderr_tail") or "",
            "stdout_tail": result.get("stdout_tail") or "",
            "stderr_tail": result.get("stderr_tail") or "",
            "exit_code": result.get("exit_code"),
            "duration_s": result.get("duration_seconds", round(time.time() - started, 1)),
            "needs_review": worker_status == "ok",
            "model": aider_model,
            "run_id": run_id,
            "workflow_id": workflow_id,
            "source_role": source_role,
            "source": "aider",
        }
        run_status = "succeeded" if worker_status == "ok" else ("cancelled" if worker_status == "cancelled" else "failed")
        await events.emit(conv_id, "tool_end", {
            "tool": "run_aider_fix", "icon": "wrench",
            "status": f"Aider {envelope['status']} ({len(files)} file(s))",
            "run_id": run_id,
        })
        await _finalize(run_status, envelope)
        return envelope

    except cancel_registry.RunCancelled:
        envelope = {
            "status": "cancelled",
            "summary": "Aider fix cancelled by user.",
            "project_dir": project_dir,
            "files_touched": [],
            "diff": "",
            "test_exit": None,
            "test_stdout_tail": "",
            "test_stderr_tail": "",
            "needs_review": False,
            "run_id": run_id,
            "workflow_id": workflow_id,
        }
        await _signal_worker_cancel("cancel_registry")
        await _finalize("cancelled", envelope)
        return envelope
    except asyncio.CancelledError:
        envelope = {
            "status": "cancelled",
            "summary": "Aider fix cancelled by stream shutdown.",
            "project_dir": project_dir,
            "files_touched": [],
            "diff": "",
            "test_exit": None,
            "test_stdout_tail": "",
            "test_stderr_tail": "",
            "needs_review": False,
            "run_id": run_id,
            "workflow_id": workflow_id,
        }
        await _finalize("cancelled", envelope)
        raise
    except Exception as e:
        envelope = {
            "status": "error",
            "summary": f"{type(e).__name__}: {e}",
            "project_dir": project_dir,
            "files_touched": [],
            "diff": "",
            "test_exit": None,
            "test_stdout_tail": "",
            "test_stderr_tail": "",
            "needs_review": False,
            "run_id": run_id,
            "workflow_id": workflow_id,
        }
        await events.emit(conv_id, "tool_end", {
            "tool": "run_aider_fix", "icon": "wrench",
            "status": f"Aider failed: {type(e).__name__}",
            "run_id": run_id,
        })
        await _finalize("failed", envelope)
        return envelope
    finally:
        if cancel_watcher and not cancel_watcher.done():
            cancel_watcher.cancel()
            try:
                await cancel_watcher
            except BaseException:
                pass
