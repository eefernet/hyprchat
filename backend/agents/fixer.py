"""
Fixer agent — Coder Bot v2 Phase 2.

Replaces the v1 pattern of "orchestrator manually reads files and rewrites
them in 28+ chat rounds to address reviewer findings" with a single stateless
Fixer run that:

  1. Loads the prior reviewer envelope from the run store.
  2. For each reviewer issue, reads the files in the issue's `suggested_fix_scope`.
  3. Asks a coder-class LLM for minimal targeted edits as structured JSON.
  4. Validates that the model only edits scope files, then writes them via Codebox.
  5. Returns an envelope listing files touched + diffs + any non-fatal errors.

The Fixer does NOT re-run the build itself. The chat agent is expected to
call `run_review` again afterwards to verify the fix worked. This keeps
responsibilities clean: Reviewer verifies, Fixer edits, neither does the
other's job.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import uuid

import config
import database as db
import cancel_registry


_FIXER_PROMPT = """You are the Fixer — a focused code-editing agent. You MUST only edit the files listed in 'allowed_paths' to address the issues described below. Do not touch any other files. Do not add new files. Do not refactor unrelated code.

## Project context
- Project root: {project_dir}
- Language: {language}
- Build command: {build_cmd}
- Test command: {test_cmd}
{research_section}
## Issues to fix
{issue_block}

## Files you may edit (allowed_paths)
{allowed_paths_list}

## Current contents of those files
{files_section}

## Output format — STRICT
For each file you want to change, write ONE section in this exact format:

### EDIT: <full file path from allowed_paths>
```{lang_tag}
<COMPLETE new file contents — every line of the file, not a diff>
```

After all your EDIT sections (zero or more), write ONE summary line:

### SUMMARY: <one short line describing what you changed and why>

If you genuinely cannot fix any issue (ambiguous, need info not provided, etc.), output ONLY:

### CANNOT_FIX: <one-line reason>

## Rules
1. The path after `### EDIT:` MUST exactly match one of allowed_paths. No relative paths.
2. The contents inside ``` fences MUST be the COMPLETE file (every line). Not a diff. Not a snippet.
3. NO prose, explanation, or commentary outside the EDIT sections. The first line of your response should be either `### EDIT:`, `### SUMMARY:`, or `### CANNOT_FIX:`.
4. If a file in allowed_paths needs no change, simply do not include an EDIT section for it.
5. Address ALL listed issues in a single pass. If two issues touch the same file, emit ONE EDIT section for that file with both fixes applied.

Output your sections now:"""


async def _read_file_sandbox(http, path: str) -> str:
    """Read a file via Codebox /command. Empty string on failure."""
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": f"cat {shlex.quote(path)}", "timeout": 10},
            timeout=15,
        )
        if r.status_code == 200:
            j = r.json()
            if int(j.get("exit_code", 0) or 0) == 0:
                return j.get("stdout") or ""
    except Exception as e:
        print(f"[FIXER] read failed for {path}: {e}")
    return ""


async def _write_file_sandbox(http, path: str, content: str) -> tuple[bool, str]:
    """Write a file via Codebox using base64 to avoid shell-quoting issues.

    Mirrors the pattern used by the existing `write_file` tool dispatch in
    tools.py (which is known to work reliably): the command ends with
    `&& echo OK`, and we treat the write as successful if either the exit
    code is 0 OR "OK" shows up in stdout. Codebox's exit_code reporting is
    occasionally noisy on stdout-redirected commands; the OK marker is the
    authoritative success signal.

    Returns (ok, error_detail). On success error_detail is "".
    """
    try:
        b64 = base64.b64encode(content.encode("utf-8", errors="replace")).decode()
        qp = shlex.quote(path)
        cmd = (
            f"mkdir -p $(dirname {qp}) && "
            f"printf '%s' {shlex.quote(b64)} | base64 -d > {qp} && echo OK"
        )
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": cmd, "timeout": 30},
            timeout=35,
        )
        if r.status_code != 200:
            return (False, f"codebox HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        stdout = j.get("stdout") or ""
        if "OK" in stdout:
            return (True, "")
        rc = int(j.get("exit_code", 1) or 1)
        if rc == 0:
            return (True, "")
        err = (j.get("stderr") or stdout or "")[:300]
        return (False, f"exit={rc}: {err.strip()}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


# Matches:
#   ### EDIT: <path>
#   ```[lang]
#   <content lines>
#   ```
# - path captured (no newline allowed)
# - content captured lazily, terminated by closing fence
# - tolerates an optional language tag after the opening fence
_EDIT_RE = re.compile(
    r"###\s*EDIT\s*:\s*([^\n]+?)\s*\n```[A-Za-z0-9_.+#\-]*\s*\n(.*?)\n```",
    re.DOTALL,
)
_SUMMARY_RE = re.compile(r"###\s*SUMMARY\s*:?\s*(.+?)(?=\n\s*###|\Z)", re.DOTALL)
_CANNOT_FIX_RE = re.compile(r"###\s*CANNOT[_\s]?FIX\s*:?\s*(.+?)(?=\n\s*###|\Z)", re.DOTALL)


def _parse_fixer_output(text: str) -> dict:
    """Parse marker-delimited fixer output into {edits, summary, cannot_fix}.

    Tolerates leading/trailing prose because the model occasionally adds
    a sentence even when told not to — we just look for the markers.
    """
    text = text or ""

    # CANNOT_FIX is a hard exit — return early with no edits.
    cf = _CANNOT_FIX_RE.search(text)
    if cf and not _EDIT_RE.search(text):
        return {"edits": [], "summary": f"Cannot fix — {cf.group(1).strip()[:200]}",
                "cannot_fix": True}

    edits = []
    for m in _EDIT_RE.finditer(text):
        path = m.group(1).strip()
        content = m.group(2)
        # Strip a single trailing newline if the model added one inside the fence.
        if content.endswith("\n") and not content.endswith("\n\n"):
            content = content[:-1]
        edits.append({"path": path, "content": content})

    s = _SUMMARY_RE.search(text)
    summary = s.group(1).strip()[:200] if s else ""
    if not summary and edits:
        summary = f"Applied {len(edits)} edit(s)"
    return {"edits": edits, "summary": summary, "cannot_fix": False}


async def run_fixer(http, events, conv_id: str, *,
                    reviewer_run_id: str = "",
                    parent_run_id: str = "",
                    research_context: str = "") -> dict:
    """Execute a Fixer run that addresses every issue in a prior Reviewer envelope.

    Returns a structured envelope. Creates a `runs` row with role='fixer' and
    parent_run_id set to the reviewer run, so the run graph stays linked.

    `research_context`, if provided, is the (possibly-truncated) text of a
    recent deep_research call on this conversation. The orchestrator-side
    dispatcher in tools.py grabs it from a per-conv cache and passes it in
    so the Fixer's coder LLM has additional grounding for tricky errors.
    The Fixer itself does NOT make any network calls — research happens
    entirely on the orchestrator side; this is just an injected reference.
    """

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    try:
        await db.create_run(run_id, conv_id, role="fixer",
                            parent_run_id=(parent_run_id or reviewer_run_id),
                            status="running")
    except Exception as e:
        print(f"[FIXER] create_run failed (non-fatal): {e}")
        run_id = ""

    # Register cancel event so POST /api/runs/{id}/cancel can interrupt the
    # in-flight per-issue Ollama calls. Cleaned up at every return below.
    if run_id:
        cancel_registry.register(run_id)

    async def _step(action: str, detail: str = ""):
        await events.emit(conv_id, "tool_progress", {
            "tool": "run_fixer", "icon": "wrench",
            "status": f"🛠 {action}{': ' + detail[:100] if detail else ''}",
            "run_id": run_id,
        })
        if run_id:
            try:
                await db.append_run_event(run_id, {
                    "type": "step", "action": action, "detail": detail[:300],
                })
            except Exception:
                pass

    # 1. Pull the reviewer envelope.
    if not reviewer_run_id:
        envelope = {"status": "error", "summary": "run_fixer needs reviewer_run_id",
                    "issues_addressed": 0, "files_touched": [], "errors": []}
        if run_id:
            try: await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
            except Exception: pass
            cancel_registry.cleanup(run_id)
        return envelope

    reviewer = await db.get_run(reviewer_run_id)
    if not reviewer:
        envelope = {"status": "error",
                    "summary": f"reviewer_run_id '{reviewer_run_id}' not found",
                    "issues_addressed": 0, "files_touched": [], "errors": []}
        if run_id:
            try: await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
            except Exception: pass
            cancel_registry.cleanup(run_id)
        return envelope

    rev_env = reviewer.get("result_envelope") or {}
    issues = rev_env.get("issues") or []

    project_dir = (rev_env.get("project_dir") or "").strip()
    if not project_dir:
        first_file = (issues[0].get("file") if issues else "") or ""
        if first_file.startswith("/root/projects/"):
            parts = first_file.split("/")
            if len(parts) >= 4:
                project_dir = "/".join(parts[:4])

    language = rev_env.get("language", "")
    build_cmd = rev_env.get("build_cmd", "")
    test_cmd = rev_env.get("test_cmd", "")

    # Validate project_dir exists on Codebox before proceeding (#14).
    if project_dir:
        try:
            _chk = await http.post(
                f"{config.CODEBOX_URL}/command",
                json={"command": f"test -d {shlex.quote(project_dir)} && echo OK || echo NO",
                      "timeout": 5},
                timeout=10,
            )
            if _chk.status_code != 200 or "OK" not in (_chk.json().get("stdout") or ""):
                print(f"[FIXER] project_dir={project_dir} does not exist on Codebox")
                envelope = {
                    "status": "error",
                    "summary": f"project_dir '{project_dir}' not found on Codebox (workspace may have been cleaned)",
                    "issues_addressed": 0, "files_touched": [], "errors": [],
                    "project_dir": project_dir, "language": language,
                }
                if run_id:
                    try: await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
                    except Exception: pass
                    cancel_registry.cleanup(run_id)
                return envelope
        except Exception as _e:
            print(f"[FIXER] project_dir check failed (non-fatal, proceeding): {_e}")

    if not issues:
        envelope = {"status": "skipped", "summary": "No issues in reviewer envelope — nothing to fix",
                    "issues_addressed": 0, "files_touched": [], "errors": [],
                    "project_dir": project_dir, "language": language}
        if run_id:
            try: await db.update_run(run_id, status="skipped", result_envelope=envelope, ended=True)
            except Exception: pass
            cancel_registry.cleanup(run_id)
        await events.emit(conv_id, "tool_end", {
            "tool": "run_fixer", "icon": "wrench",
            "status": "🛠 No issues to fix — skipped",
            "run_id": run_id,
        })
        return envelope

    fixer_model = (config.FIXER_MODEL or config.CODER_MODEL or config.PLANNING_MODEL
                   or config.DEFAULT_MODEL or "")
    if not fixer_model:
        envelope = {"status": "error",
                    "summary": "No coder/planning model configured for Fixer",
                    "issues_addressed": 0, "files_touched": [], "errors": []}
        if run_id:
            try: await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
            except Exception: pass
            cancel_registry.cleanup(run_id)
        return envelope

    await _step("start", f"{len(issues)} issue(s) to address with {fixer_model}")

    files_touched: set[str] = set()
    diffs: list[dict] = []
    errors: list[str] = []

    if research_context:
        _research_excerpt = research_context.strip()
        if len(_research_excerpt) > 3000:
            _research_excerpt = _research_excerpt[:3000] + "\n\n[... truncated ...]"
        research_section = (
            "\n## Reference (recent web research from this conversation)\n"
            "Use this as supporting context for HOW to fix the issue when training-cutoff "
            "knowledge may be wrong (recent library versions, API changes, deprecations, "
            "obscure errors). The issue + scoped file contents below are still the "
            "primary signal — the research is just additional grounding.\n\n"
            f"{_research_excerpt}\n"
        )
        await _step("research-context-injected", f"{len(_research_excerpt)} chars")
    else:
        research_section = ""

    def _normalize_path(p: str) -> str:
        if not p:
            return ""
        p = p.strip()
        if p.startswith("/"):
            return p
        if p.startswith("./"):
            p = p[2:]
        if ".." in p.split("/"):
            return ""
        return f"{project_dir.rstrip('/')}/{p}"

    # 2. Collect all scope files across all issues, dedup, then batch into
    # a single LLM call. This replaces the old per-issue sequential loop
    # that made N separate Ollama calls (one per issue).
    _LANG_TAG = {
        "java": "java", "python": "python", "go": "go", "rust": "rust",
        "javascript": "javascript", "typescript": "typescript",
        "c": "c", "cpp": "cpp", "kotlin": "kotlin",
    }
    lang_tag = _LANG_TAG.get((language or "").lower(), "")

    # Build per-issue scopes and a union scope for the prompt.
    all_scope: list[str] = []  # ordered, deduped union of scope files
    _scope_set: set[str] = set()
    _norm_pdir = os.path.normpath(project_dir)
    issue_blocks: list[str] = []
    skipped = 0
    for i, issue in enumerate(issues, 1):
        raw_scope = issue.get("suggested_fix_scope") or []
        if issue.get("file") and issue["file"] not in raw_scope:
            raw_scope = [issue["file"]] + raw_scope
        scope: list[str] = []
        for p in raw_scope:
            ap = _normalize_path(p)
            if not ap:
                continue
            try:
                if os.path.commonpath([_norm_pdir, os.path.normpath(ap)]) != _norm_pdir:
                    continue
            except ValueError:
                continue
            scope.append(ap)
            if ap not in _scope_set:
                _scope_set.add(ap)
                all_scope.append(ap)
        if not scope:
            errors.append(f"Issue {i}: no in-scope files to edit "
                          f"(raw={raw_scope}, project_dir={project_dir}); skipping "
                          f"(severity={issue.get('severity','?')})")
            skipped += 1
            continue

        await _step(f"issue {i}/{len(issues)}",
                    f"[{issue.get('severity','?')}] {issue.get('summary','')[:80]}")

        issue_blocks.append(
            f"### Issue {i}\n"
            f"- Severity: {issue.get('severity','?')}\n"
            f"- File: {issue.get('file','?')}"
            + (f":{','.join(str(x) for x in issue.get('lines') or [])}" if issue.get('lines') else "")
            + f"\n- Summary: {issue.get('summary','')}\n"
            f"- Fix scope: {', '.join(scope[:5])}"
        )

    if not issue_blocks:
        envelope = {"status": "no_op",
                    "summary": "All issues had no in-scope files — nothing to fix",
                    "issues_addressed": 0, "files_touched": [], "errors": errors[:10],
                    "project_dir": project_dir, "language": language}
        if run_id:
            try: await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
            except Exception: pass
            cancel_registry.cleanup(run_id)
        await events.emit(conv_id, "tool_end", {
            "tool": "run_fixer", "icon": "wrench",
            "status": f"🛠 No in-scope files — {len(errors)} error(s)",
            "run_id": run_id,
        })
        return envelope

    # Cap total scope files to keep prompt size bounded.
    capped_scope = all_scope[:15]
    _allowed_set = set(capped_scope)
    if len(all_scope) > 15:
        errors.append(f"Scope truncated from {len(all_scope)} to 15 files — "
                      f"{len(all_scope) - 15} file(s) not shown to fixer")

    issue_block = "\n\n".join(issue_blocks)

    # Compute per-file character budget so the prompt fits the context window.
    _fixer_ctx = config.DEFAULT_NUM_CTX or 16384
    _char_budget = _fixer_ctx * 3
    _overhead = len(issue_block) + len(research_section) + 2000
    _file_budget = max(_char_budget - _overhead, 8000)
    _per_file_cap = min(max(_file_budget // max(len(capped_scope), 1), 1000), 8000)
    print(f"[FIXER] budget: num_ctx={_fixer_ctx}, files={len(capped_scope)}, "
          f"per_file_cap={_per_file_cap}, file_budget={_file_budget}")

    # 2a. Read all scope files in parallel.
    await _step("reading", f"{len(capped_scope)} scope file(s)")
    _read_tasks = [_read_file_sandbox(http, path) for path in capped_scope]
    _read_results = await asyncio.gather(*_read_tasks, return_exceptions=True)

    files_section_lines = []
    for path, content in zip(capped_scope, _read_results):
        if isinstance(content, str) and content:
            snippet = content[:_per_file_cap]
            if len(content) > _per_file_cap:
                snippet += f"\n\n... [truncated; full file is {len(content)} chars]"
            files_section_lines.append(f"### {path}\n```\n{snippet}\n```")
        else:
            files_section_lines.append(f"### {path}\n(file is empty or unreadable)")

    prompt = _FIXER_PROMPT.format(
        project_dir=project_dir,
        language=language or "(unknown)",
        build_cmd=build_cmd or "(none)",
        test_cmd=test_cmd or "(none)",
        research_section=research_section,
        issue_block=issue_block,
        allowed_paths_list="\n".join(f"  - {p}" for p in capped_scope),
        files_section="\n\n".join(files_section_lines),
        lang_tag=lang_tag,
    )

    # 2b. Single LLM call for all issues.
    await _step(f"calling {fixer_model}", f"{len(issue_blocks)} issue(s) batched")
    text = ""
    try:
        coro = http.post(
            f"{config.OLLAMA_URL}/api/chat",
            json={
                "model": fixer_model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.2,
                            "num_ctx": config.DEFAULT_NUM_CTX},
            },
            timeout=600,
        )
        r = await cancel_registry.await_cancellable(coro, run_id)
        if r.status_code == 200:
            text = (r.json().get("message", {}).get("content") or "").strip()
        else:
            errors.append(f"Model HTTP {r.status_code}")
    except cancel_registry.RunCancelled:
        cancelled_env = {
            "status": "cancelled",
            "summary": (f"Fixer cancelled by user during LLM call; "
                        f"{len(files_touched)} file(s) already written before cancel."),
            "issues_addressed": 0,
            "files_touched": sorted(files_touched),
            "diffs": diffs[:20],
            "errors": errors[:10],
            "fixer_model": fixer_model,
            "project_dir": project_dir,
            "language": language,
            "reviewer_run_id": reviewer_run_id,
            "research_used": bool(research_context),
        }
        try:
            await events.emit(conv_id, "tool_end", {
                "tool": "run_fixer", "icon": "wrench",
                "status": "⛔ Fixer cancelled by user",
                "run_id": run_id,
            })
        except Exception:
            pass
        if run_id:
            try:
                await db.update_run(run_id, status="cancelled",
                                    result_envelope=cancelled_env, ended=True)
            except Exception:
                pass
            cancel_registry.cleanup(run_id)
        return cancelled_env
    except Exception as e:
        errors.append(f"Model call failed: {e}")

    # 2c. Parse and apply edits.
    if text:
        parsed = _parse_fixer_output(text)
        edits = parsed.get("edits") or []
        edit_summary = (parsed.get("summary") or "")[:140]

        if parsed.get("cannot_fix"):
            errors.append(f"Fixer declined — {edit_summary}")
        elif not edits:
            sample = text[:200].replace("\n", " \\n ")
            errors.append(
                f"Model output had no `### EDIT:` sections "
                f"({len(text)} chars). Sample: {sample!r}"
            )
        else:
            for edit in edits[:20]:  # safety cap
                path = (edit.get("path") or "").strip()
                content = edit.get("content")
                if not path or content is None:
                    continue
                if path not in _allowed_set:
                    errors.append(f"Model tried out-of-scope edit on {path} — refused")
                    continue
                rel = path[len(project_dir) + 1:] if path.startswith(project_dir + "/") else path
                await _step("writing", rel)
                ok, err_detail = await _write_file_sandbox(http, path, content)
                if ok:
                    files_touched.add(path)
                    diffs.append({"path": path, "summary": edit_summary or "(no summary)"})
                else:
                    errors.append(f"Write failed for {path} — {err_detail}")

    # 3. Build envelope.
    if files_touched and not errors:
        status = "applied"
    elif files_touched:
        status = "partial"
    else:
        status = "no_op"

    envelope = {
        "status": status,
        "summary": (f"Applied edits to {len(files_touched)} file(s) across "
                    f"{len(issues)} issue(s)") if files_touched
                   else "No edits applied — see errors",
        "issues_addressed": len(issues) - skipped,
        "files_touched": sorted(files_touched),
        "diffs": diffs[:20],
        "errors": errors[:10],
        "fixer_model": fixer_model,
        "project_dir": project_dir,
        "language": language,
        "reviewer_run_id": reviewer_run_id,
        "research_used": bool(research_context),
    }

    await events.emit(conv_id, "tool_end", {
        "tool": "run_fixer", "icon": "wrench",
        "status": (f"🛠 Fixer applied {len(files_touched)} edit(s) "
                   f"({len(errors)} non-fatal error(s))" if files_touched
                   else f"🛠 Fixer made no changes ({len(errors)} error(s))"),
        "run_id": run_id,
    })
    if run_id:
        try:
            await db.update_run(
                run_id,
                status=("succeeded" if files_touched else "failed"),
                result_envelope=envelope, ended=True,
            )
        except Exception as e:
            print(f"[FIXER] update_run failed (non-fatal): {e}")
        cancel_registry.cleanup(run_id)

    return envelope
