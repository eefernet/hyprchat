"""
Acceptance agent — Coder Bot v2 final quality gate.

The Reviewer proves build/test/lint health. Acceptance checks whether the
delivered project actually matches the user's request, has honest docs, sane
tests, and clean packaging. It is read-only and intentionally uses static
inspection by default.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import uuid

import config
import database as db
import cancel_registry
import model_providers


_ARTIFACT_DIRS = {
    ".git", ".gradle", ".mypy_cache", ".next", ".nox", ".npm",
    ".pytest_cache", ".ruff_cache", ".tox", ".venv", "__pycache__",
    "build", "coverage", "dist", "env", "htmlcov", "node_modules",
    "out", "target", "venv",
}
_ARTIFACT_DIR_PATTERNS = ("*.egg-info",)
_ARTIFACT_FILE_PATTERNS = ("*.pyc", "*.pyo")
_ARTIFACT_FILE_PATHS = ("*.egg-info/*",)

_MANIFEST_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "build.gradle.kts", "Makefile", "CMakeLists.txt",
}
_README_NAMES = {"README", "README.md", "README.rst", "README.txt"}
_SOURCE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
    ".c", ".cpp", ".cc", ".h", ".hpp", ".rb", ".php", ".swift", ".cs",
    ".html", ".css", ".vue", ".svelte",
}
_DOC_EXTS = {".md", ".rst", ".txt"}
_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.[A-Za-z0-9]+$"),
    re.compile(r"(^|/)[^/]+(_test|\.test|\.spec)\.[A-Za-z0-9]+$"),
    re.compile(r"(^|/)[^/]+Test\.(java|kt|cs)$"),
)
_README_CONTEXT_BYTES = 12000
_MANIFEST_CONTEXT_BYTES = 12000
_TEST_CONTEXT_BYTES = 12000
_SOURCE_CONTEXT_BYTES = 128000
_SOURCE_SECTION_CAP_MAX = 220000


async def _run_command(http, command: str, timeout: int = 15,
                       run_id: str = "") -> dict:
    """Run one allowed static-inspection command in Codebox."""
    try:
        coro = http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": command, "timeout": timeout},
            timeout=timeout + 10,
        )
        r = await (cancel_registry.await_cancellable(coro, run_id) if run_id else coro)
        if r.status_code != 200:
            return {"exit_code": -1, "stdout": "", "stderr": f"HTTP {r.status_code}: {r.text[:200]}"}
        d = r.json()
        return {
            "exit_code": int(d.get("exit_code", 0) or 0),
            "stdout": d.get("stdout") or "",
            "stderr": d.get("stderr") or "",
        }
    except cancel_registry.RunCancelled:
        raise
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}


def _find_name_expr(names) -> str:
    return " -o ".join(f"-name {shlex.quote(name)}" for name in sorted(names))


def _artifact_dir_expr() -> str:
    return _find_name_expr(set(_ARTIFACT_DIRS) | set(_ARTIFACT_DIR_PATTERNS))


def _artifact_file_expr() -> str:
    parts = [f"-name {shlex.quote(name)}" for name in _ARTIFACT_FILE_PATTERNS]
    parts.extend(f"-path {shlex.quote(path)}" for path in _ARTIFACT_FILE_PATHS)
    return " -o ".join(parts)


async def _list_files(http, project_dir: str, run_id: str) -> list[str]:
    ignored_dirs = _artifact_dir_expr()
    cmd = (
        f"cd {shlex.quote(project_dir)} && "
        f"find . -maxdepth 6 \\( -type d \\( {ignored_dirs} \\) -prune \\) "
        "-o -type f -print "
        "| sort | head -300"
    )
    r = await _run_command(http, cmd, timeout=20, run_id=run_id)
    return [line.strip() for line in (r.get("stdout") or "").splitlines() if line.strip()]


async def _scan_artifacts(http, project_dir: str, run_id: str) -> list[str]:
    ignored_dirs = _artifact_dir_expr()
    ignored_files = _artifact_file_expr()
    cmd = (
        f"cd {shlex.quote(project_dir)} && "
        f"find . \\( -type d \\( {ignored_dirs} \\) -print -prune \\) "
        f"-o \\( -type f \\( {ignored_files} \\) -print \\) "
        "| sort | head -80"
    )
    r = await _run_command(http, cmd, timeout=20, run_id=run_id)
    return [line.strip() for line in (r.get("stdout") or "").splitlines() if line.strip()]


async def _read_head(http, project_dir: str, rel_path: str, run_id: str,
                     max_bytes: int = 6000) -> str:
    rel = rel_path[2:] if rel_path.startswith("./") else rel_path
    full = f"{project_dir.rstrip('/')}/{rel}"
    n = int(max_bytes)
    qfull = shlex.quote(full)
    cmd = (
        f"bytes=$(wc -c < {qfull} 2>/dev/null || echo 0); "
        f"head -c {n} {qfull}; "
        f"if [ \"$bytes\" -gt {n} ]; then "
        f"printf '\\n\\n[... TRUNCATED: showing first {n} of %s bytes; file continues ...]\\n' \"$bytes\"; "
        "fi"
    )
    r = await _run_command(http, cmd, timeout=10, run_id=run_id)
    if r.get("exit_code") == 0:
        return (r.get("stdout") or "")[:n + 200]
    return ""


def _is_test_file(path: str) -> bool:
    return any(p.search(path) for p in _TEST_PATTERNS)


def _pick_context_files(files: list[str]) -> dict[str, list[str]]:
    readmes = [f for f in files if os.path.basename(f) in _README_NAMES][:3]
    manifests = [f for f in files if os.path.basename(f) in _MANIFEST_NAMES][:8]
    tests = [f for f in files if _is_test_file(f)][:8]

    entry_candidates = []
    for f in files:
        base = os.path.basename(f)
        if base in {"main.py", "__main__.py", "app.py", "cli.py", "index.js",
                    "main.js", "main.ts", "server.js", "main.go", "main.rs",
                    "Main.java"}:
            entry_candidates.append(f)
    source = []
    for f in files:
        if f in tests or f in readmes or f in manifests:
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext in _SOURCE_EXTS:
            source.append(f)

    selected_source = []
    for f in entry_candidates + source:
        if f not in selected_source:
            selected_source.append(f)
        if len(selected_source) >= 10:
            break

    return {
        "readmes": readmes,
        "manifests": manifests,
        "tests": tests,
        "source": selected_source,
    }


def _extract_console_scripts(manifest_sections: dict[str, str]) -> list[str]:
    """Best-effort console entrypoint extraction from manifests.

    The first-pass acceptance reviewer is static by default, so callers may
    choose not to run these. Keeping extraction separate makes any future
    `--help` smoke command explicit and limited to declared entrypoints.
    """
    scripts: list[str] = []
    for path, content in manifest_sections.items():
        base = os.path.basename(path)
        if base == "pyproject.toml":
            in_scripts = False
            for line in content.splitlines():
                s = line.strip()
                if s.startswith("["):
                    in_scripts = s in {"[project.scripts]", "[tool.poetry.scripts]"}
                    continue
                if in_scripts:
                    m = re.match(r"([A-Za-z0-9_.-]+)\s*=", s)
                    if m:
                        scripts.append(m.group(1))
        elif base == "package.json":
            try:
                data = json.loads(content)
                bin_field = data.get("bin")
                if isinstance(bin_field, str) and data.get("name"):
                    scripts.append(str(data["name"]))
                elif isinstance(bin_field, dict):
                    scripts.extend(str(k) for k in bin_field.keys())
            except Exception:
                pass
    out = []
    seen = set()
    for s in scripts:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:5]


def _try_parse_json(text: str) -> dict | None:
    if not text:
        return None
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", cleaned)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    depth = 0
    start = -1
    for i, c in enumerate(cleaned):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(cleaned[start:i + 1])
                except Exception:
                    start = -1
    return None


def _configured_num_ctx() -> int:
    """Use HyprChat's configured context window, never Ollama's model default."""
    try:
        n = int(getattr(config, "DEFAULT_NUM_CTX", 0) or 0)
    except Exception:
        n = 0
    # "Auto" in the UI stores 0. For agent calls, still pass an explicit
    # bounded context so Ollama does not allocate a model-level default KV cache.
    return n if n > 0 else 16384


def _section_budgets(num_ctx: int) -> dict:
    """Derive per-section character caps from the configured token context.

    Whole-prompt budget ≈ num_ctx × 3 chars; the remainder after these
    sections covers the task/plan/file-list/prompt scaffold. The old fixed
    caps (90K-char source floor plus 84K for the other sections) overflowed
    a 16K-token window several times over and Ollama silently truncated the
    prompt — usually dropping the instructions at one end.
    """
    n = config.coerce_num_ctx(num_ctx, fallback=16384)
    budget = n * 3
    return {
        "readme": max(4000, int(budget * 0.10)),
        "manifest": max(4000, int(budget * 0.10)),
        "tests": max(4000, int(budget * 0.15)),
        "source": max(4000, min(_SOURCE_SECTION_CAP_MAX, int(budget * 0.50))),
    }


def _normalize_issue(issue: dict, project_dir: str) -> dict:
    category = (issue.get("category") or issue.get("severity") or "spec").lower()
    if category not in {"spec", "docs", "packaging", "tests", "runtime"}:
        category = "spec"
    scope = issue.get("suggested_fix_scope") or []
    if isinstance(scope, str):
        scope = [scope]
    clean_scope = []
    for p in scope:
        if not isinstance(p, str) or not p.strip():
            continue
        p = p.strip()
        if p.startswith(project_dir + "/"):
            p = p[len(project_dir) + 1:]
        clean_scope.append(p)
    file = (issue.get("file") or "").strip()
    if file.startswith(project_dir + "/"):
        file = file[len(project_dir) + 1:]
    lines = issue.get("lines") or []
    if not isinstance(lines, list):
        lines = [lines]
    norm_lines = []
    for line in lines[:10]:
        try:
            norm_lines.append(int(line))
        except Exception:
            pass
    return {
        "category": category,
        "file": file,
        "lines": norm_lines,
        "summary": (issue.get("summary") or "").strip()[:500],
        "suggested_fix_scope": clean_scope[:8],
    }


def _latest_user_task(conv: dict | None) -> tuple[str, str]:
    msgs = (conv or {}).get("messages") or []
    user_msgs = [m for m in msgs if m.get("role") == "user" and (m.get("content") or "").strip()]
    if not user_msgs:
        return "", ""
    original = (user_msgs[0].get("content") or "").strip()
    latest = (user_msgs[-1].get("content") or "").strip()
    return original[:4000], latest[:3000]


async def _project_plan(conv_id: str, project_id: str = "") -> str:
    try:
        if project_id:
            p = await db.get_coding_project(project_id)
            if p and p.get("last_plan"):
                return str(p["last_plan"])[:6000]
        p = await db.get_coding_project_by_conv(conv_id)
        if p and p.get("last_plan"):
            return str(p["last_plan"])[:6000]
    except Exception:
        pass
    try:
        runs = await db.get_runs_by_conversation(conv_id, limit=30)
        for r in runs:
            if r.get("role") == "architect":
                env = r.get("result_envelope") or {}
                if env:
                    return json.dumps(env, indent=2)[:6000]
    except Exception:
        pass
    return ""


_ACCEPTANCE_PROMPT = """You are the Acceptance Reviewer for an autonomous coding agent's completed project.

The build/test/lint Reviewer has already passed clean. Your job is different:
verify the project actually satisfies the user's request and is deliverable.
Use only the static project evidence below. Do not invent runtime results.

## Original user task
{original_task}

## Latest user request in this conversation
{latest_task}

## Project plan, if available
{plan}

## Clean build/test/lint review
{review_summary}
{prior_acceptance_section}
## File manifest ({file_count} files shown)
{manifest}

## Generated/cache/build artifacts found
{artifact_block}

## Readme/docs
{readme_sections}

## Package/build manifests
{manifest_sections}

## Tests
{test_sections}

## Entrypoints and selected source
{source_sections}

## Task
Return ONLY a JSON object with this exact shape:

{{
  "status": "accepted" | "issues" | "error",
  "summary": "one-sentence acceptance summary",
  "issues": [
    {{
      "category": "spec" | "docs" | "packaging" | "tests" | "runtime",
      "file": "relative/path",
      "lines": [1],
      "summary": "what is wrong and why it blocks delivery",
      "suggested_fix_scope": ["relative/path"]
    }}
  ]
}}

Acceptance criteria:
- spec: implemented behavior must match the user's request and plan.
- docs: README and usage claims must be accurate for files/manifests present.
- packaging: package metadata, entrypoints, generated artifacts, and archive hygiene must be sane. If the review summary lists smoke_new_files (state files the program created when run), flag any that sit in the project tree as a packaging issue — runtime state must not ship in the deliverable.
- tests: tests must be meaningful and isolated; they must not write to a real home directory or depend on host state.
- runtime: flag likely user-visible behavior bugs that static inspection reveals, but do not speculate.
- verification: accept static-only projects when the user's requested app type has no build step, but flag docs or delivery claims that imply stronger verification than the Reviewer actually performed. If isolated_required is true, normal delivery requires isolated_status "passed"; if isolated_status is missing, failed, or not-run for an app/game/CLI/server project with a manifest, flag a packaging/runtime issue.
- install docs: shell commands that include package version specifiers with `>`, `<`, or `>=` must quote the specifier, e.g. `pip install "pygame-ce>=2.5.0"`; unquoted examples can be interpreted as shell redirection and should be flagged as docs/packaging issues.

Rules:
- If there are no blocking delivery issues, emit status "accepted" and issues [].
- If static inspection is insufficient because critical files are missing/unreadable, emit status "error".
- Some file excerpts may end with a TRUNCATED marker. That means the prompt excerpt ended, not that the real file ended.
- The clean Reviewer result is authoritative for syntax/build/test/lint status. Do not report SyntaxError or EOF from an excerpt boundary when lint/build already passed.
- Do not complain about preferences or nice-to-have polish.
- Every issue must include a minimal suggested_fix_scope. Scope docs-only issues to docs only when possible.
- Return JSON only. No markdown fences or prose.
"""


async def run_acceptance_review(http, events, conv_id: str, *,
                                project_dir: str = "",
                                reviewer_run_id: str = "",
                                project_id: str = "",
                                parent_run_id: str = "",
                                conv_model: str = "",
                                prior_acceptance_context: str = "") -> dict:
    """Run the final acceptance gate after a clean Reviewer run."""
    run_id = f"run-{uuid.uuid4().hex[:12]}"

    if reviewer_run_id:
        parent_run_id = parent_run_id or reviewer_run_id

    try:
        await db.create_run(run_id, conv_id, role="acceptance",
                            project_id=project_id, parent_run_id=parent_run_id,
                            status="running")
    except Exception as e:
        print(f"[ACCEPTANCE] create_run failed (non-fatal): {e}")
        run_id = ""

    if run_id:
        cancel_registry.register(run_id)

    async def _step(action: str, detail: str = ""):
        await events.emit(conv_id, "tool_progress", {
            "tool": "run_acceptance_review",
            "icon": "clipboard-check",
            "status": f"✅ {action}: {detail[:120]}" if detail else f"✅ {action}",
            "run_id": run_id,
        })
        if run_id:
            try:
                await db.append_run_event(run_id, {
                    "type": "step", "action": action, "detail": detail[:300],
                })
            except Exception:
                pass

    try:
        await events.emit(conv_id, "tool_start", {
            "tool": "run_acceptance_review",
            "icon": "clipboard-check",
            "status": f"✅ Acceptance reviewing {project_dir or '(auto)'}",
            "run_id": run_id,
        })

        review_env = {}
        if reviewer_run_id:
            rr = await db.get_run(reviewer_run_id)
            if rr:
                review_env = rr.get("result_envelope") or {}
        if not review_env and conv_id:
            runs = await db.get_runs_by_conversation(conv_id, limit=30)
            for r in runs:
                if r.get("role") == "reviewer":
                    review_env = r.get("result_envelope") or {}
                    reviewer_run_id = r.get("id", "")
                    parent_run_id = parent_run_id or reviewer_run_id
                    break

        if not project_dir:
            project_dir = (review_env.get("project_dir") or "").strip()
        if not project_id and review_env:
            project_id = review_env.get("project_id", "") or project_id

        if (review_env.get("status") or "").lower() != "clean":
            envelope = {
                "status": "error",
                "summary": "Acceptance requires the latest run_review to be clean first.",
                "issues": [{
                    "category": "runtime",
                    "file": project_dir or "",
                    "lines": [],
                    "summary": "run_review has not passed clean, so acceptance cannot evaluate delivery quality.",
                    "suggested_fix_scope": [],
                }],
                "project_dir": project_dir,
                "reviewer_run_id": reviewer_run_id,
                "run_id": run_id,
            }
            if run_id:
                await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
            await events.emit(conv_id, "tool_end", {
                "tool": "run_acceptance_review", "icon": "clipboard-check",
                "status": "⚠ Acceptance blocked — run_review is not clean",
                "run_id": run_id,
            })
            return envelope

        if not project_dir:
            envelope = {
                "status": "error",
                "summary": "Acceptance needs project_dir or a clean reviewer envelope with project_dir.",
                "issues": [],
                "project_dir": "",
                "reviewer_run_id": reviewer_run_id,
                "run_id": run_id,
            }
            if run_id:
                await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
            return envelope

        await _step("manifest", project_dir)
        files = await _list_files(http, project_dir, run_id)
        artifacts = await _scan_artifacts(http, project_dir, run_id)
        picked = _pick_context_files(files)

        async def _read_group(paths: list[str], max_bytes: int) -> dict[str, str]:
            tasks = [_read_head(http, project_dir, p, run_id, max_bytes=max_bytes) for p in paths]
            vals = await asyncio.gather(*tasks, return_exceptions=True)
            out = {}
            for p, v in zip(paths, vals):
                if isinstance(v, str):
                    out[p] = v
            return out

        await _step("read_context", "README, manifests, tests, and selected source")
        readmes, manifests, tests, source = await asyncio.gather(
            _read_group(picked["readmes"], _README_CONTEXT_BYTES),
            _read_group(picked["manifests"], _MANIFEST_CONTEXT_BYTES),
            _read_group(picked["tests"], _TEST_CONTEXT_BYTES),
            _read_group(picked["source"], _SOURCE_CONTEXT_BYTES),
        )
        console_scripts = _extract_console_scripts(manifests)

        def _sections(title_map: dict[str, str], cap: int = 18000,
                      per_file_cap: int = 5000) -> str:
            parts = []
            used = 0
            for p, content in title_map.items():
                block = f"### {p}\n```\n{content[:per_file_cap]}\n```"
                if used + len(block) > cap:
                    break
                parts.append(block)
                used += len(block)
            return "\n\n".join(parts) if parts else "(none)"

        conv = await db.get_conversation(conv_id) if conv_id else None
        original_task, latest_task = _latest_user_task(conv)
        plan = await _project_plan(conv_id, project_id=project_id)
        num_ctx = _configured_num_ctx()
        _budgets = _section_budgets(num_ctx)

        review_summary = json.dumps({
            "reviewer_run_id": reviewer_run_id,
            "summary": review_env.get("summary", ""),
            "build_cmd": review_env.get("build_cmd", ""),
            "test_cmd": review_env.get("test_cmd", ""),
            "lint_cmd": review_env.get("lint_cmd", ""),
            "language": review_env.get("language", ""),
            "marker": review_env.get("marker", ""),
            "verification_level": review_env.get("verification_level", ""),
            "verification_profile": review_env.get("verification_profile", ""),
            "verification_confidence": review_env.get("verification_confidence", ""),
            "smoke_cmds": review_env.get("smoke_cmds", []),
            "smoke_new_files": review_env.get("smoke_new_files", []),
            "isolated_required": review_env.get("isolated_required", False),
            "isolated_status": review_env.get("isolated_status", ""),
            "isolated_cmds": review_env.get("isolated_cmds", []),
            "isolated_exit": review_env.get("isolated_exit", None),
            "isolated_output_tail": review_env.get("isolated_output_tail", ""),
        }, indent=2)

        if prior_acceptance_context:
            prior_acceptance_section = (
                "\n## Your previous acceptance verdict for this request\n"
                "You already reviewed this project and flagged the issues below; "
                "the fix loop then addressed them. FIRST verify each one is "
                "resolved. Do NOT flag new minor nitpicks you did not previously "
                "raise — only report a NEW issue if it is a genuine defect that "
                "blocks the user's request.\n\n"
                + prior_acceptance_context.strip()[:2000] + "\n"
            )
        else:
            prior_acceptance_section = ""

        artifact_block = "\n".join(artifacts[:80]) if artifacts else "(none)"
        if console_scripts:
            artifact_block += "\n\nDeclared console entrypoints: " + ", ".join(console_scripts)

        prompt = _ACCEPTANCE_PROMPT.format(
            original_task=original_task or "(not available)",
            latest_task=latest_task or "(not available)",
            plan=plan or "(not available)",
            review_summary=review_summary,
            prior_acceptance_section=prior_acceptance_section,
            file_count=len(files),
            manifest="\n".join(files[:300]) or "(no files found)",
            artifact_block=artifact_block,
            readme_sections=_sections(readmes, cap=_budgets["readme"], per_file_cap=_README_CONTEXT_BYTES),
            manifest_sections=_sections(manifests, cap=_budgets["manifest"], per_file_cap=_MANIFEST_CONTEXT_BYTES),
            test_sections=_sections(tests, cap=_budgets["tests"], per_file_cap=_TEST_CONTEXT_BYTES),
            source_sections=_sections(source, cap=_budgets["source"], per_file_cap=_SOURCE_CONTEXT_BYTES),
        )

        # Cloud-prefixed models are honored only from explicit Settings values
        # (per-agent override / Planning Model) — never inherited from the
        # chat model.
        model = (getattr(config, "ACCEPTANCE_MODEL", "")
                 or config.PLANNING_MODEL
                 or model_providers.reject_cloud(conv_model)
                 or model_providers.reject_cloud(config.DEFAULT_MODEL) or "")
        if not model:
            envelope = {
                "status": "error",
                "summary": "No planning/reviewer model configured for Acceptance.",
                "issues": [],
                "project_dir": project_dir,
                "reviewer_run_id": reviewer_run_id,
                "run_id": run_id,
            }
            if run_id:
                await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
            return envelope

        await _step("analyze", f"calling {model}")

        async def _progress_ticker():
            elapsed = 0
            while True:
                await asyncio.sleep(15)
                elapsed += 15
                try:
                    await _step("analyzing", f"{elapsed}s elapsed…")
                except Exception:
                    pass

        _ticker = asyncio.create_task(_progress_ticker())
        try:
            coro = model_providers.complete_chat(
                http, model, prompt,
                temperature=0.2, num_ctx=num_ctx, num_predict=4096,
                timeout=600, ollama_url=config.OLLAMA_URL,
            )
            text = await cancel_registry.await_cancellable(coro, run_id)
        finally:
            _ticker.cancel()
            try:
                await _ticker
            except asyncio.CancelledError:
                pass
        parsed = _try_parse_json(text)
        if not parsed or "status" not in parsed:
            parsed = {
                "status": "error",
                "summary": "Acceptance model did not return valid JSON.",
                "issues": [{
                    "category": "runtime",
                    "file": "",
                    "lines": [],
                    "summary": "Acceptance reviewer output could not be parsed.",
                    "suggested_fix_scope": [],
                }],
            }

        status = (parsed.get("status") or "error").lower()
        # Local models emit natural synonyms for a passing verdict (the
        # Reviewer's own schema uses "clean", so the confusion is baked in);
        # hard-downgrading them to "error" blocked delivery on passing projects.
        if status in {"clean", "pass", "passed", "ok", "approved", "accept"}:
            status = "accepted"
        elif status in {"issue", "failed", "rejected"}:
            status = "issues"
        if status not in {"accepted", "issues", "error"}:
            status = "error"
        issues = [_normalize_issue(i, project_dir) for i in (parsed.get("issues") or []) if isinstance(i, dict)]
        if status == "accepted":
            issues = []
        elif status == "issues" and not issues:
            status = "error"

        envelope = {
            "status": status,
            "summary": (parsed.get("summary") or "").strip()[:500],
            "issues": issues,
            "project_dir": project_dir,
            "project_id": project_id,
            "reviewer_run_id": reviewer_run_id,
            "acceptance_model": model,
            "acceptance_num_ctx": num_ctx,
            "file_count": len(files),
            "artifact_examples": artifacts[:20],
            "context_files": picked,
            "run_id": run_id,
        }
        await events.emit(conv_id, "tool_end", {
            "tool": "run_acceptance_review",
            "icon": "clipboard-check",
            "status": ("✅ Acceptance accepted" if status == "accepted"
                       else f"⚠ Acceptance found {len(issues)} issue{'s' if len(issues) != 1 else ''}"),
            "run_id": run_id,
        })
        if run_id:
            await db.update_run(run_id, status=("succeeded" if status != "error" else "failed"),
                                result_envelope=envelope, ended=True)
        return envelope
    except cancel_registry.RunCancelled:
        envelope = {
            "status": "cancelled",
            "summary": "Acceptance review cancelled by user.",
            "issues": [],
            "project_dir": project_dir,
            "reviewer_run_id": reviewer_run_id,
            "run_id": run_id,
        }
        try:
            await events.emit(conv_id, "tool_end", {
                "tool": "run_acceptance_review",
                "icon": "clipboard-check",
                "status": "⛔ Acceptance cancelled by user",
                "run_id": run_id,
            })
            if run_id:
                await db.update_run(run_id, status="cancelled", result_envelope=envelope, ended=True)
        except Exception:
            pass
        return envelope
    except Exception as e:
        # Ollama timeouts / transport errors must not strand the run at 'running' —
        # the v2 gate treats an in-flight active_run_id as blocking forever.
        envelope = {
            "status": "error",
            "summary": f"Acceptance review failed: {type(e).__name__}: {e}",
            "issues": [],
            "project_dir": project_dir,
            "reviewer_run_id": reviewer_run_id,
            "run_id": run_id,
        }
        print(f"[ACCEPTANCE] run failed: {type(e).__name__}: {e}")
        try:
            await events.emit(conv_id, "tool_end", {
                "tool": "run_acceptance_review",
                "icon": "clipboard-check",
                "status": f"⚠ Acceptance failed: {type(e).__name__}",
                "run_id": run_id,
            })
            if run_id:
                await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
        except Exception:
            pass
        return envelope
    finally:
        if run_id:
            cancel_registry.cleanup(run_id)
