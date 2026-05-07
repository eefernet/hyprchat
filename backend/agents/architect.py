"""
Architect agent — Coder Bot v2 Phase 3.

Replaces v1's prose-output `plan_project` with a stateless single-shot LLM
call that produces a STRUCTURED PLAN as JSON. The plan is consumed directly
by downstream agents (Builder/Reviewer/Overseer) instead of being re-parsed
from markdown each step — eliminating "model interprets prose" handoffs.

Responsibilities:
  1. Single LLM call to PLANNING_MODEL (or fallback) with strict JSON schema.
  2. Validate against required fields. Retry once on parse failure with the
     parse error fed back into the prompt. Fail loudly on second miss.
  3. Persist the plan as a `runs` row with role='architect' so downstream
     tools can pull it via the conversation's run history.

The Architect does NOT execute or write code. It produces a plan; that's it.
"""

from __future__ import annotations

import json
import re
import uuid

import config
import database as db


# Required top-level keys in the validated plan.
_REQUIRED_KEYS = {"project_id", "language", "build_system", "build_cmd",
                  "test_cmd", "manifest", "success_criteria"}


_ARCHITECT_PROMPT = """You are the Architect — a focused project planning agent. Given the user's task, produce a structured plan as a JSON object that the Builder, Reviewer, and Overseer agents will consume directly. No prose, no fences, no commentary — JSON only.

## User task
{task}

## Language hint
{language}

## Reference docs (filtered to this language; may be empty)
{kb_section}

## Output — STRICT JSON, schema below

{{
  "project_id": "<short kebab-case slug, 2-3 words from the task, e.g. 'pong-game'>",
  "language": "<lowercase language name: java, python, rust, go, javascript, typescript, c, cpp, kotlin, etc.>",
  "build_system": "<one of: maven, gradle, cargo, npm, pyproject, cmake, make, none>",
  "build_cmd": "<exact shell command, e.g. 'mvn -q -DskipTests compile'>",
  "test_cmd": "<exact shell command, e.g. 'mvn -q test', or empty string if no tests>",
  "lint_cmd": "<exact shell command, or empty string if not applicable>",
  "manifest": [
    {{"path": "<path relative to project root>", "purpose": "<one-line description>", "estimated_loc": <integer>}}
  ],
  "tests_required": [
    {{"path": "<test file path relative to project root>", "covers": "<what behavior is tested>"}}
  ],
  "external_deps": [
    {{"name": "<dependency name>", "version": "<version or 'latest'>"}}
  ],
  "risk_notes": [
    "<one-line bullet about something that could fail or needs care>"
  ],
  "success_criteria": [
    "<one-line measurable success condition, e.g. 'mvn compile exits 0'>"
  ]
}}

## Hard rules
1. Output ONLY the JSON object. First character is `{{`, last character is `}}`. No markdown fences. No prose before or after.
2. All strings must escape backslashes, quotes, and newlines correctly.
3. `manifest` should list 3-25 source/config files for typical projects. Be concrete (real paths, not placeholders).
4. `success_criteria` MUST include "build_cmd exits 0" or equivalent — the Reviewer uses these to gate completion.
5. If the language is uncertain, pick the most likely one and add a `risk_notes` entry.

Output the JSON now:"""


def _try_parse_architect_json(text: str) -> tuple[dict | None, str]:
    """Parse a JSON object out of the model's response. Tolerates leading/trailing
    prose and stray code fences. Returns (parsed_dict_or_None, error_string)."""
    if not text:
        return None, "empty model output"
    # Strip code fences if present.
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    try:
        return json.loads(cleaned), ""
    except (json.JSONDecodeError, ValueError) as e:
        # Try to extract the first {...} block.
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0)), ""
            except (json.JSONDecodeError, ValueError) as e2:
                return None, f"JSON parse error in extracted block: {e2}"
        return None, f"JSON parse error: {e}"


def _validate_plan(plan: dict) -> tuple[bool, str]:
    """Check that the plan has the required keys and types we depend on."""
    missing = [k for k in _REQUIRED_KEYS if k not in plan]
    if missing:
        return False, f"missing required keys: {missing}"
    if not isinstance(plan.get("manifest"), list):
        return False, "manifest must be a list"
    if not plan["manifest"]:
        return False, "manifest cannot be empty"
    for i, entry in enumerate(plan["manifest"]):
        if not isinstance(entry, dict) or "path" not in entry:
            return False, f"manifest[{i}] must be an object with a 'path' field"
    if not isinstance(plan.get("success_criteria"), list):
        return False, "success_criteria must be a list of strings"
    if not plan["success_criteria"]:
        return False, "success_criteria cannot be empty"
    return True, ""


async def _call_planning_model(http, model: str, prompt: str,
                                num_ctx: int = 16384) -> str:
    """Single non-streaming call to Ollama, returns the message content."""
    try:
        r = await http.post(
            f"{config.OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.2, "num_ctx": num_ctx},
            },
            timeout=600,
        )
        if r.status_code == 200:
            return (r.json().get("message", {}).get("content") or "").strip()
        return ""
    except Exception as e:
        print(f"[ARCHITECT] LLM call failed: {e}")
        return ""


async def run_architect(http, events, conv_id: str, *,
                        task: str, language_hint: str = "",
                        kb_chunks: list | None = None,
                        parent_run_id: str = "",
                        conv_model: str = "") -> dict:
    """Execute an Architect run. Returns the structured plan envelope.

    Side effects:
      - Creates a `runs` row with role='architect' and persists the plan.
      - Emits tool_progress events on the conversation EventBus.
    """
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    try:
        await db.create_run(run_id, conv_id, role="architect",
                            parent_run_id=parent_run_id, status="running")
    except Exception as e:
        print(f"[ARCHITECT] create_run failed (non-fatal): {e}")
        run_id = ""

    # Step counter for the durable runs.events_log — frontend RunCard sorts
    # and renders step events in order, so each event needs a stable step #.
    _step_n = [0]

    async def _step(action: str, detail: str = ""):
        _step_n[0] += 1
        await events.emit(conv_id, "tool_progress", {
            "tool": "plan_project", "icon": "compass",
            "status": f"📐 {action}{': ' + detail[:100] if detail else ''}",
            "run_id": run_id,
            "step": _step_n[0],
        })
        if run_id:
            try:
                await db.append_run_event(run_id, {
                    "type": "step", "step": _step_n[0],
                    "action": action, "detail": detail[:400],
                })
            except Exception:
                pass

    model = (config.PLANNING_MODEL or conv_model or config.DEFAULT_MODEL or "")
    if not model:
        envelope = {"status": "error",
                    "summary": "No planning model configured for Architect",
                    "manifest": [], "success_criteria": []}
        if run_id:
            try: await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
            except Exception: pass
        return envelope

    # Build the kb_section. Each chunk is rendered as a labelled block, capped
    # so the prompt stays reasonable.
    kb_section_parts = []
    for chunk in (kb_chunks or [])[:3]:
        if isinstance(chunk, dict):
            label = chunk.get("source") or chunk.get("filename") or "(reference)"
            text = chunk.get("text") or chunk.get("content") or ""
            if text:
                kb_section_parts.append(f"### {label}\n{text[:1200]}")
        elif isinstance(chunk, str):
            kb_section_parts.append(chunk[:1200])
    kb_section = "\n\n".join(kb_section_parts) if kb_section_parts else "(no reference docs available)"

    base_prompt = _ARCHITECT_PROMPT.format(
        task=task[:2500],
        language=language_hint or "(detect from task)",
        kb_section=kb_section,
    )

    await _step("planning", f"calling {model}")
    text = await _call_planning_model(http, model, base_prompt)
    plan, parse_err = _try_parse_architect_json(text)

    if plan:
        valid, validation_err = _validate_plan(plan)
        if not valid:
            parse_err = validation_err
            plan = None

    # Single retry with parse error fed back, per the v2 plan.
    if not plan:
        await _step("retry", f"first attempt failed: {parse_err[:80]}")
        retry_prompt = (
            base_prompt
            + f"\n\nYour previous output failed validation: {parse_err}\n"
            + "Output ONLY a valid JSON object that satisfies the schema above. "
            + "No prose, no fences. Try again now:"
        )
        text = await _call_planning_model(http, model, retry_prompt)
        plan, parse_err = _try_parse_architect_json(text)
        if plan:
            valid, validation_err = _validate_plan(plan)
            if not valid:
                parse_err = validation_err
                plan = None

    if not plan:
        envelope = {
            "status": "error",
            "summary": (f"Architect failed to produce a valid plan after 2 attempts: "
                        f"{parse_err}"),
            "raw_output": text[:600],
            "architect_model": model,
            "manifest": [],
            "success_criteria": [],
        }
        await events.emit(conv_id, "tool_end", {
            "tool": "plan_project", "icon": "compass",
            "status": f"⚠ Architect failed: {parse_err[:80]}",
            "run_id": run_id,
        })
        if run_id:
            try: await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
            except Exception: pass
        return envelope

    # Successful plan. Annotate envelope with metadata downstream tools rely on.
    plan["status"] = "ok"
    plan["architect_model"] = model
    plan["run_id"] = run_id
    plan["summary"] = (
        f"Plan ready: {plan.get('language', '?')} project "
        f"({plan.get('build_system', '?')}), "
        f"{len(plan.get('manifest', []))} files in manifest"
    )

    # Emit step events that lay out the plan's content so the user can see
    # the manifest in the run card while builder/reviewer/fixer agents work.
    # Each step ends up in runs.events_log and renders as a labelled line in
    # the frontend RunCard timeline.
    await _step("language", f"{plan.get('language','?')} (build_system: {plan.get('build_system','?')})")
    if plan.get("build_cmd"):
        await _step("build_cmd", plan["build_cmd"])
    if plan.get("test_cmd"):
        await _step("test_cmd", plan["test_cmd"])
    if plan.get("lint_cmd"):
        await _step("lint_cmd", plan["lint_cmd"])

    _manifest = plan.get("manifest") or []
    await _step("manifest", f"{len(_manifest)} file(s) planned")
    for _m in _manifest[:30]:
        _path = _m.get("path", "?")
        _purpose = _m.get("purpose", "")
        _loc = _m.get("estimated_loc")
        _line = f"{_path} — {_purpose}" if _purpose else _path
        if _loc:
            _line += f" (~{_loc} LOC)"
        await _step("file", _line)
    if len(_manifest) > 30:
        await _step("file", f"... ({len(_manifest) - 30} more files in manifest)")

    _tests = plan.get("tests_required") or []
    if _tests:
        await _step("tests_required", f"{len(_tests)} test file(s)")
        for _t in _tests[:10]:
            await _step("test", f"{_t.get('path','?')} — covers {_t.get('covers','')}")

    _deps = plan.get("external_deps") or []
    if _deps:
        await _step("dependencies", f"{len(_deps)} external dep(s)")
        for _d in _deps[:10]:
            await _step("dep", f"{_d.get('name','?')} {_d.get('version','')}".strip())

    _risks = plan.get("risk_notes") or []
    for _r in _risks[:5]:
        await _step("risk", _r)

    _crit = plan.get("success_criteria") or []
    if _crit:
        await _step("success_criteria", f"{len(_crit)} criterion(s) — project done when all pass")
        for _c in _crit[:8]:
            await _step("criterion", _c)

    # Emit the final tool_end with `detail.plan` set to the formatted plan
    # markdown — the frontend's PlanPanel keys off this field and renders the
    # plan as a styled "Architecture Plan" panel above the agent run cards.
    # This restores the v1 plan-visualization behavior for v2.
    _plan_md = format_plan_for_chat(plan)
    await events.emit(conv_id, "tool_end", {
        "tool": "plan_project", "icon": "compass",
        "status": (f"📐 Plan ready — {len(_manifest)} files, "
                   f"{plan.get('build_system', '?')}"),
        "detail": json.dumps({
            "plan": _plan_md[:12000],
            "language": plan.get("language", ""),
            "project_id": plan.get("project_id", ""),
        }),
        "run_id": run_id,
    })
    if run_id:
        try:
            await db.update_run(run_id, status="succeeded",
                                result_envelope=plan, ended=True)
        except Exception as e:
            print(f"[ARCHITECT] update_run failed (non-fatal): {e}")

    return plan


def _render_file_tree(manifest: list[dict], project_id: str = "project") -> str:
    """Render the manifest's file paths as a box-drawing tree.

    Produces output like:
        project-root/
        ├── pom.xml                     # Maven build config (~50 LOC)
        ├── src/main/java/
        │   ├── Main.java               # Entry point (~20 LOC)
        │   └── pong/
        │       ├── Ball.java           # Ball physics (~80 LOC)
        │       └── Paddle.java         # Paddle logic (~60 LOC)
        └── README.md                   # Usage instructions (~30 LOC)
    """
    if not manifest:
        return "(no files)"

    # Build a nested dict of directory → children. Leaves are dicts with the
    # original manifest entry attached so we can render purpose/LOC in comments.
    root: dict = {}
    for entry in manifest:
        path = (entry.get("path") or "").strip().lstrip("./")
        if not path:
            continue
        parts = path.split("/")
        node = root
        for i, part in enumerate(parts):
            is_leaf = (i == len(parts) - 1)
            if part not in node:
                node[part] = {"_children": {}, "_entry": None, "_is_leaf": is_leaf}
            if is_leaf:
                node[part]["_entry"] = entry
                node[part]["_is_leaf"] = True
            else:
                # Mark as directory; might also be a leaf if both a file and
                # subtree share a name (unusual but harmless)
                pass
            node = node[part]["_children"]

    # Find the longest "name" line so we can pad comments to the same column.
    def _walk_for_width(d: dict, depth: int = 0) -> int:
        widest = 0
        for name, info in d.items():
            prefix = "│   " * depth + "├── "
            line = prefix + name + ("/" if info["_children"] else "")
            widest = max(widest, len(line))
            widest = max(widest, _walk_for_width(info["_children"], depth + 1))
        return widest

    target_col = min(_walk_for_width(root) + 2, 50)

    lines: list[str] = [f"{project_id}/"]

    def _render(d: dict, prefix: str = ""):
        items = list(d.items())
        for i, (name, info) in enumerate(items):
            last = (i == len(items) - 1)
            connector = "└── " if last else "├── "
            display_name = name + ("/" if info["_children"] else "")
            base_line = prefix + connector + display_name
            comment = ""
            if info.get("_entry"):
                purpose = info["_entry"].get("purpose", "")
                loc = info["_entry"].get("estimated_loc")
                if purpose or loc:
                    comment = "  # " + purpose
                    if loc:
                        comment += f" (~{loc} LOC)"
            # Pad base_line so comments line up
            if comment:
                pad = max(target_col - len(base_line), 1)
                lines.append(base_line + " " * pad + comment)
            else:
                lines.append(base_line)
            if info["_children"]:
                child_prefix = prefix + ("    " if last else "│   ")
                _render(info["_children"], child_prefix)

    _render(root)
    return "\n".join(lines)


def _render_deps_block(build_system: str, deps: list[dict]) -> str:
    """Render external deps as a copy-pasteable snippet for the build system."""
    if not deps:
        return ""
    bs = (build_system or "").lower()
    if bs == "maven":
        lines = ["```xml", "<!-- pom.xml -->"]
        for d in deps[:15]:
            name = d.get("name", "")
            ver = d.get("version", "")
            # Try to split "group:artifact" if model gave it that form, else
            # just place the name as artifact and leave group blank.
            if ":" in name:
                grp, art = name.split(":", 1)
            else:
                grp, art = "<group>", name or "<artifact>"
            lines.append("<dependency>")
            lines.append(f"  <groupId>{grp}</groupId>")
            lines.append(f"  <artifactId>{art}</artifactId>")
            if ver:
                lines.append(f"  <version>{ver}</version>")
            lines.append("</dependency>")
        lines.append("```")
        return "\n".join(lines)
    if bs == "npm":
        lines = ["```bash"]
        for d in deps[:15]:
            name = d.get("name", "")
            ver = d.get("version", "")
            spec = f"{name}@{ver}" if ver and ver != "latest" else name
            lines.append(f"npm install {spec}")
        lines.append("```")
        return "\n".join(lines)
    if bs in ("cargo", "rust"):
        lines = ["```toml", "# Cargo.toml [dependencies]"]
        for d in deps[:15]:
            name = d.get("name", "")
            ver = d.get("version", "")
            lines.append(f'{name} = "{ver}"' if ver else f'{name} = "*"')
        lines.append("```")
        return "\n".join(lines)
    if bs in ("pyproject", "pip", "python"):
        lines = ["```bash"]
        for d in deps[:15]:
            name = d.get("name", "")
            ver = d.get("version", "")
            spec = f"{name}=={ver}" if ver and ver != "latest" else name
            lines.append(f"pip install {spec}")
        lines.append("```")
        return "\n".join(lines)
    if bs == "gradle":
        lines = ["```groovy", "// build.gradle dependencies"]
        for d in deps[:15]:
            name = d.get("name", "")
            ver = d.get("version", "")
            if ":" in name and ver:
                lines.append(f"implementation '{name}:{ver}'")
            else:
                lines.append(f"implementation '{name}'  // version: {ver or 'latest'}")
        lines.append("```")
        return "\n".join(lines)
    # Fallback: plain bullet list
    return "\n".join(f"- **{d.get('name','?')}** {d.get('version','')}" for d in deps[:15])


def format_plan_for_chat(plan: dict) -> str:
    """Render a successful Architect plan as rich markdown for the PlanPanel.

    Restores the visual richness of the v1 prose plan: a file tree drawn with
    box characters in a code fence, build-system-specific dependency snippets,
    success criteria as a checklist, tests as a table. The output is consumed
    by the frontend's PlanPanel via markdown rendering.
    """
    if plan.get("status") != "ok":
        return (f"# Plan failed\n\n{plan.get('summary', 'unknown error')}\n\n"
                f"```\n{plan.get('raw_output', '')[:600]}\n```")

    project_id = plan.get("project_id", "project")
    title = project_id.replace("-", " ").replace("_", " ").title()
    lang = plan.get("language", "?")
    build_system = plan.get("build_system", "?")

    sections: list[str] = []

    # Header
    sections.append(f"# {title} — Implementation Plan")
    sections.append("")
    sections.append(
        f"**Language:** {lang}  •  **Build system:** {build_system}  •  "
        f"**Project ID:** `{project_id}`"
    )

    # 1. File tree
    manifest = plan.get("manifest") or []
    if manifest:
        sections.append("")
        sections.append(f"## 1. File Tree ({len(manifest)} files)")
        sections.append("")
        sections.append("```text")
        sections.append(_render_file_tree(manifest, project_id))
        sections.append("```")

    # 2. Build / test / lint commands
    sections.append("")
    sections.append("## 2. Build & Test")
    sections.append("")
    cmd_lines = ["```bash"]
    if plan.get("build_cmd"):
        cmd_lines.append("# Build")
        cmd_lines.append(plan["build_cmd"])
    if plan.get("test_cmd"):
        if len(cmd_lines) > 1:
            cmd_lines.append("")
        cmd_lines.append("# Test")
        cmd_lines.append(plan["test_cmd"])
    if plan.get("lint_cmd"):
        cmd_lines.append("")
        cmd_lines.append("# Lint")
        cmd_lines.append(plan["lint_cmd"])
    cmd_lines.append("```")
    sections.append("\n".join(cmd_lines))

    # 3. External dependencies
    deps = plan.get("external_deps") or []
    if deps:
        sections.append("")
        sections.append(f"## 3. Dependencies ({len(deps)})")
        sections.append("")
        sections.append(_render_deps_block(build_system, deps))

    # 4. Tests required
    tests = plan.get("tests_required") or []
    if tests:
        sections.append("")
        sections.append(f"## 4. Tests Required ({len(tests)})")
        sections.append("")
        sections.append("| File | Covers |")
        sections.append("|---|---|")
        for t in tests[:15]:
            path = t.get("path", "?")
            covers = (t.get("covers", "") or "").replace("|", "\\|")
            sections.append(f"| `{path}` | {covers} |")

    # 5. Risk notes
    risks = plan.get("risk_notes") or []
    if risks:
        sections.append("")
        sections.append("## 5. Risk Notes")
        sections.append("")
        for r in risks[:8]:
            sections.append(f"- ⚠ {r}")

    # 6. Success criteria as checkbox list
    crit = plan.get("success_criteria") or []
    if crit:
        sections.append("")
        sections.append("## 6. Success Criteria")
        sections.append("")
        for c in crit[:10]:
            sections.append(f"- [ ] {c}")

    # Footer with run_id (small italic line at the end)
    sections.append("")
    sections.append("---")
    sections.append(
        f"_Architect run: `{plan.get('run_id', '?')}`. The Builder will read "
        f"this manifest from the runs store and follow it._"
    )

    return "\n".join(sections)
