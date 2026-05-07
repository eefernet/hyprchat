"""
Reviewer agent — Coder Bot v2 Phase 1.

Replaces the v1 pattern of "orchestrator manually reads files and rewrites them
in 28+ chat rounds" with a single stateless Reviewer run that:

  1. Detects the project's build/test commands from on-disk markers.
  2. Runs build, test, and (when available) lint in the Codebox sandbox.
  3. Reads files referenced in failure output for context.
  4. Asks a planning-model LLM to produce a structured issue list.
  5. Returns that list as a result envelope.

The Reviewer is read-only — it never modifies files. Fixing is the Fixer's job
(Phase 2). This module lives alongside chat.py / personas.py and reuses the
existing run-store, EventBus, and Codebox plumbing.
"""

from __future__ import annotations

import json
import re
import shlex
import uuid

import config
import database as db


# Project-marker → (build_cmd, test_cmd, lint_cmd, language).
# Order matters: more-specific markers first so monorepos hit the right one.
# Each cmd runs at the project root via Codebox /command.
_PROJECT_MARKERS = [
    ("pom.xml",         "mvn -q -DskipTests compile",      "mvn -q test",            "",                   "java"),
    ("build.gradle",    "./gradlew build -q -x test || gradle build -q -x test",
                                                            "./gradlew test -q || gradle test -q",
                                                                                      "",                   "java"),
    ("build.gradle.kts","./gradlew build -q -x test",      "./gradlew test -q",      "",                   "kotlin"),
    ("Cargo.toml",      "cargo build --quiet",             "cargo test --quiet",     "cargo clippy --quiet", "rust"),
    ("go.mod",          "go build ./...",                  "go test ./...",          "go vet ./...",       "go"),
    ("package.json",    "npm install --silent && npm run build --if-present",
                                                            "npm test --if-present",  "",                   "javascript"),
    ("pyproject.toml",  "python3 -m pip install -q -e . 2>/dev/null || true",
                                                            "python3 -m pytest -q --no-header 2>/dev/null || python3 -m unittest discover -q",
                                                                                      "python3 -m py_compile $(find . -name '*.py' -not -path '*/venv/*')",
                                                                                                            "python"),
    ("requirements.txt","python3 -m pip install -q -r requirements.txt 2>/dev/null || true",
                                                            "python3 -m pytest -q --no-header 2>/dev/null || python3 -m unittest discover -q",
                                                                                      "python3 -m py_compile $(find . -name '*.py' -not -path '*/venv/*')",
                                                                                                            "python"),
    ("Makefile",        "make -s 2>&1 | head -200",         "make -s test 2>&1 | head -200", "",            ""),
    ("CMakeLists.txt",  "cmake -B build -S . && cmake --build build --quiet",
                                                            "cd build && ctest --output-on-failure",
                                                                                      "",                   "cpp"),
]


# Plain-source fallbacks when no formal build file exists. Order matters: we
# pick the language with the most source files in the tree.
_PLAIN_LANG_PROFILES = {
    "java":   {"glob": "*.java",
               # Compile production sources only — exclude ./test, ./tests,
               # *Test.java, *Tests.java. JUnit is rarely on the bare classpath
               # without a build file, so trying to compile tests blows up the
               # whole build. This matches `mvn compile` semantics.
               "build": ("rm -rf out && mkdir -p out && "
                         "find . -path ./out -prune -o "
                         "-path ./test -prune -o -path ./tests -prune -o "
                         "-name '*Test.java' -prune -o -name '*Tests.java' -prune -o "
                         "-name '*.java' -print | xargs -r javac -d out 2>&1"),
               "test":  "(test -d out && find out -name '*Test*.class' -print | head -1 | grep -q . "
                        "&& cd out && java -cp . org.junit.runner.JUnitCore $(find . -name '*Test*.class' "
                        "| sed 's|^\\./||;s|\\.class$||;s|/|.|g')) || echo '(no JUnit tests)'",
               "lint":  ""},
    "python": {"glob": "*.py",
               "build": "python3 -m py_compile $(find . -name '*.py' -not -path '*/venv/*' -not -path '*/.git/*' -not -path '*/__pycache__/*')",
               "test":  "python3 -m pytest -q 2>/dev/null || python3 -m unittest discover -q 2>/dev/null || echo '(no tests)'",
               "lint":  ""},
    "go":     {"glob": "*.go",
               "build": "go build ./... 2>&1 || (find . -name '*.go' -exec gofmt -l {} \\;)",
               "test":  "go test ./... 2>&1 || echo '(no go.mod — no tests)'",
               "lint":  "go vet ./... 2>&1 || true"},
    "rust":   {"glob": "*.rs",
               "build": "rustc --edition 2021 --crate-type bin $(find . -name 'main.rs' | head -1) 2>&1 "
                        "|| find . -name '*.rs' -exec rustc --edition 2021 --emit=metadata {} \\; 2>&1",
               "test":  "echo '(no Cargo.toml — tests not run)'",
               "lint":  ""},
    "javascript": {"glob": "*.js",
                   "build": "find . -name '*.js' -not -path '*/node_modules/*' -exec node --check {} \\; 2>&1",
                   "test":  "echo '(no package.json — tests not run)'",
                   "lint":  ""},
    "typescript": {"glob": "*.ts",
                   "build": "npx --yes tsc --noEmit $(find . -name '*.ts' -not -path '*/node_modules/*') 2>&1",
                   "test":  "echo '(no package.json — tests not run)'",
                   "lint":  ""},
    "c":      {"glob": "*.c",
               "build": "gcc -Wall -fsyntax-only $(find . -name '*.c') 2>&1",
               "test":  "echo '(plain C — no test runner)'",
               "lint":  ""},
    "cpp":    {"glob": "*.cpp",
               "build": "g++ -Wall -fsyntax-only $(find . -name '*.cpp' -o -name '*.cc') 2>&1",
               "test":  "echo '(plain C++ — no test runner)'",
               "lint":  ""},
}


async def _detect_project(http, project_dir: str) -> dict:
    """Look at the project's top-level files and pick build/test commands.

    Three layers:
      1. Match a top-level build marker (pom.xml, Cargo.toml, etc.) → use it.
      2. Walk the tree for plain source files (any depth) and pick the language
         with the most sources, e.g. a `src/main/java/...` directory of .java
         files → run javac directly. Catches projects where the model wrote
         sources but skipped the build file.
      3. Give up with marker="(none)".
    """
    listing = ""
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": f"ls -1 {shlex.quote(project_dir)}", "timeout": 5},
            timeout=10,
        )
        if r.status_code == 200:
            listing = (r.json().get("stdout") or "")
    except Exception as e:
        return {"error": f"Cannot list {project_dir}: {e}",
                "build_cmd": "", "test_cmd": "", "lint_cmd": "", "language": ""}

    files = set(line.strip() for line in listing.splitlines() if line.strip())
    for marker, build, test, lint, lang in _PROJECT_MARKERS:
        if marker in files:
            return {"marker": marker, "build_cmd": build, "test_cmd": test,
                    "lint_cmd": lint, "language": lang, "files": sorted(files)[:30]}

    # Layer 2: tree-walk for plain source files. Run a single find that emits
    # extension counts so we don't make N round trips.
    tree_cmd = (
        f"cd {shlex.quote(project_dir)} && "
        "find . -type f \\( "
        "-name '*.java' -o -name '*.py' -o -name '*.go' -o -name '*.rs' "
        "-o -name '*.js' -o -name '*.ts' -o -name '*.c' -o -name '*.cpp' -o -name '*.cc' "
        "\\) "
        "-not -path '*/node_modules/*' -not -path '*/venv/*' -not -path '*/.git/*' "
        "-not -path '*/out/*' -not -path '*/build/*' -not -path '*/target/*' -not -path '*/dist/*' "
        "| sed -E 's/.*\\.(java|py|go|rs|js|ts|c|cpp|cc)$/\\1/' "
        "| sort | uniq -c | sort -rn"
    )
    counts: dict[str, int] = {}
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": tree_cmd, "timeout": 10},
            timeout=15,
        )
        if r.status_code == 200:
            for line in (r.json().get("stdout") or "").splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and parts[0].isdigit():
                    counts[parts[1].strip()] = int(parts[0])
    except Exception:
        pass

    # Map ext → language profile key (cc → cpp).
    _EXT_TO_LANG = {
        "java": "java", "py": "python", "go": "go", "rs": "rust",
        "js": "javascript", "ts": "typescript",
        "c": "c", "cpp": "cpp", "cc": "cpp",
    }
    if counts:
        # Pick the language with the most files (resolve cc→cpp into cpp's bucket).
        lang_counts: dict[str, int] = {}
        for ext, n in counts.items():
            lang = _EXT_TO_LANG.get(ext)
            if lang:
                lang_counts[lang] = lang_counts.get(lang, 0) + n
        winner = max(lang_counts, key=lang_counts.get) if lang_counts else None
        if winner and winner in _PLAIN_LANG_PROFILES:
            prof = _PLAIN_LANG_PROFILES[winner]
            return {"marker": f"{lang_counts[winner]} {prof['glob']} files",
                    "build_cmd": prof["build"], "test_cmd": prof["test"],
                    "lint_cmd": prof["lint"], "language": winner,
                    "files": sorted(files)[:30]}

    return {"marker": "(none)", "build_cmd": "", "test_cmd": "", "lint_cmd": "",
            "language": "", "files": sorted(files)[:30]}


async def _run_in_sandbox(http, project_dir: str, command: str, timeout: int = 300) -> dict:
    """Run a shell command at project_dir via Codebox. Returns truncated stdout/stderr."""
    if not command:
        return {"exit_code": -1, "stdout": "", "stderr": "(no command)"}
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": f"cd {shlex.quote(project_dir)} && ({command}) 2>&1",
                  "timeout": timeout},
            timeout=timeout + 30,
        )
        if r.status_code != 200:
            return {"exit_code": -1, "stdout": "",
                    "stderr": f"Codebox HTTP {r.status_code}: {r.text[:200]}"}
        d = r.json()
        out = (d.get("stdout") or "")
        # Truncate to 4 KB tail — that's where errors live.
        if len(out) > 4096:
            out = "... [truncated head] ...\n" + out[-4096:]
        return {"exit_code": d.get("exit_code", 0), "stdout": out, "stderr": d.get("stderr", "")}
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": f"Exception: {e}"}


# Files referenced in error output (Java, Python, Rust, Go, JS/TS).
_FILE_REFS_RE = re.compile(
    r"(?:^|[\s\(\[])"
    r"((?:[\w./-]*?/)?[A-Za-z][\w-]*\.(?:java|py|rs|go|js|jsx|ts|tsx|cpp|c|h|hpp|kt|rb|php|cs|swift|scala))"
    r"(?::(\d+))?",
    re.MULTILINE,
)


def _extract_file_refs(text: str, max_refs: int = 8) -> list[dict]:
    """Pull out filename[:line] references from compiler/test output.

    Returns the most-frequent N — duplicates often signal where the actual fault is.
    """
    counts = {}
    for m in _FILE_REFS_RE.finditer(text or ""):
        path, line = m.group(1), m.group(2)
        if path.startswith(("http://", "https://")):
            continue
        key = (path, int(line) if line else 0)
        counts[key] = counts.get(key, 0) + 1
    # Sort: most frequent first, then by path length (shorter = more likely root).
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], len(kv[0][0])))
    return [{"file": p, "line": l, "hits": c} for (p, l), c in ranked[:max_refs]]


async def _read_file_snippet(http, project_dir: str, path: str, line: int = 0,
                              window: int = 40) -> str:
    """Read a window of lines around `line` from a file in the sandbox.
    Returns full file (capped) when line=0.
    """
    rel = path
    if not path.startswith("/"):
        rel = f"{project_dir.rstrip('/')}/{path}"
    if line and line > 0:
        start = max(1, line - window // 2)
        end = line + window // 2
        cmd = f"sed -n '{start},{end}p' {shlex.quote(rel)}"
    else:
        cmd = f"head -c 4000 {shlex.quote(rel)}"
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": cmd + " 2>&1", "timeout": 10},
            timeout=15,
        )
        if r.status_code == 200:
            return (r.json().get("stdout") or "")[:4000]
    except Exception:
        pass
    return ""


_REVIEW_PROMPT = """You are a code reviewer for an autonomous coding agent's output.

The build command was: `{build_cmd}`
Build exit code: {build_exit}
Test command: `{test_cmd}`
Test exit code: {test_exit}
{lint_section}

## Build/test output (most recent {build_chars} chars of build, {test_chars} of test)
```
{build_output}
```
{test_block}
{lint_block}
{snippet_block}

## Your task
Produce a JSON object with this exact shape:

```json
{{
  "status": "clean" | "issues",
  "summary": "one-sentence summary of the project state",
  "issues": [
    {{
      "severity": "compile" | "test" | "lint" | "smell",
      "file": "src/path/to/File.java",
      "lines": [42, 47],
      "summary": "what's wrong (one sentence) — why it breaks",
      "suggested_fix_scope": ["src/path/to/File.java"]
    }}
  ]
}}
```

Rules:
- If both build_exit=0 and test_exit=0, emit `status:"clean"` and `issues:[]`.
- Otherwise list every distinct failure as one item. Group failures with the same root cause.
- Each issue must name a real file (use one from the build output if you can't be sure).
- `suggested_fix_scope` lists ONLY the files a fixer should edit to resolve that one issue.
- Do NOT include code blocks in your reply — only the JSON object. No prose before or after.
"""


def _try_parse_review_json(text: str) -> dict | None:
    """Try to extract a JSON review envelope from a model reply that may have
    wrapped it in markdown fences or prose."""
    if not text:
        return None
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    # Direct attempt
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fenced block
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # First {...} balanced object
    depth = 0
    start = -1
    for i, c in enumerate(text):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    start = -1
    return None


async def run_review(http, events, conv_id: str, project_dir: str,
                     project_id: str = "", parent_run_id: str = "") -> dict:
    """Execute a Reviewer run on a project. Returns the result envelope.

    Side effects:
      - Creates a `runs` row with role="reviewer".
      - Emits tool_progress events on the conversation's EventBus so the UI
        can render a live timeline alongside the existing RunCard.
      - Updates the run row with status + envelope at the end.
    """
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    try:
        await db.create_run(run_id, conv_id, role="reviewer",
                            project_id=project_id, parent_run_id=parent_run_id,
                            status="running")
    except Exception as e:
        print(f"[REVIEWER] create_run failed (non-fatal): {e}")
        run_id = ""

    async def _step(action: str, detail: str = ""):
        """Emit one structured step on both the live SSE channel and the durable log."""
        await events.emit(conv_id, "tool_progress", {
            "tool": "run_review", "icon": "search-check",
            "status": f"🔍 {action}: {detail[:120]}" if detail else f"🔍 {action}",
            "run_id": run_id,
        })
        if run_id:
            try:
                await db.append_run_event(run_id, {"type": "step", "action": action,
                                                    "detail": detail[:300]})
            except Exception:
                pass

    await events.emit(conv_id, "tool_start", {
        "tool": "run_review", "icon": "search-check",
        "status": f"🔍 Reviewing project at {project_dir}",
        "run_id": run_id,
    })

    # 1. Detect build/test commands.
    await _step("detect", project_dir)
    detect = await _detect_project(http, project_dir)
    marker = detect.get("marker", "(none)")
    build_cmd = detect.get("build_cmd", "")
    test_cmd = detect.get("test_cmd", "")
    lint_cmd = detect.get("lint_cmd", "")
    language = detect.get("language", "")
    print(f"[REVIEWER] {project_dir}: marker={marker} build={build_cmd[:60]!r}")
    await _step("detected", f"marker={marker} lang={language}")

    # Hard fail when we can't identify the project — a reviewer that "passes"
    # an empty / nonexistent directory is worse than no reviewer at all,
    # because the orchestrator then thinks the build is good and ships.
    if marker == "(none)" or detect.get("error"):
        why = detect.get("error") or f"No build markers found at {project_dir} (no pom.xml / Cargo.toml / package.json / go.mod / etc.)"
        print(f"[REVIEWER] aborting: {why}")
        envelope = {
            "status": "error",
            "summary": f"Reviewer could not identify project at {project_dir}: {why}",
            "issues": [{
                "severity": "config",
                "file": project_dir,
                "lines": [],
                "summary": why,
                "suggested_fix_scope": [],
            }],
            "build_cmd": "", "test_cmd": "", "lint_cmd": "",
            "build_exit": -1, "test_exit": -1, "lint_exit": -1,
            "language": language, "marker": marker,
            "project_dir": project_dir,
            "run_id": run_id,
        }
        await events.emit(conv_id, "tool_end", {
            "tool": "run_review", "icon": "search-check",
            "status": f"⚠ Reviewer could not identify project at {project_dir}",
            "run_id": run_id,
        })
        if run_id:
            try:
                await db.update_run(run_id, status="failed",
                                    result_envelope=envelope, ended=True)
            except Exception:
                pass
        return envelope

    # 2. Run build / test / lint.
    build_result = {"exit_code": 0, "stdout": "(skipped — no build command)"}
    test_result = {"exit_code": 0, "stdout": "(skipped — no test command)"}
    lint_result = {"exit_code": 0, "stdout": ""}

    if build_cmd:
        await _step("build", build_cmd)
        build_result = await _run_in_sandbox(http, project_dir, build_cmd, timeout=300)
        await _step("build_done", f"exit={build_result['exit_code']}")

    if test_cmd:
        await _step("test", test_cmd)
        test_result = await _run_in_sandbox(http, project_dir, test_cmd, timeout=300)
        await _step("test_done", f"exit={test_result['exit_code']}")

    if lint_cmd:
        await _step("lint", lint_cmd)
        lint_result = await _run_in_sandbox(http, project_dir, lint_cmd, timeout=120)
        await _step("lint_done", f"exit={lint_result['exit_code']}")

    # 3. Pull file snippets for any failures.
    failure_text = ""
    if build_result["exit_code"] != 0:
        failure_text += build_result.get("stdout", "")
    if test_result["exit_code"] != 0:
        failure_text += "\n" + test_result.get("stdout", "")
    refs = _extract_file_refs(failure_text)
    snippet_blocks = []
    for ref in refs[:5]:
        snip = await _read_file_snippet(http, project_dir, ref["file"], ref["line"])
        if snip:
            snippet_blocks.append(f"### {ref['file']}{':'+str(ref['line']) if ref['line'] else ''}\n```\n{snip[:1500]}\n```")
    snippet_block = ""
    if snippet_blocks:
        await _step("read_files", f"{len(snippet_blocks)} files for context")
        snippet_block = "\n\n## Failing-file snippets (for context only)\n\n" + "\n\n".join(snippet_blocks)

    # 4. Fast path: clean build + tests → skip the LLM call entirely.
    if build_result["exit_code"] == 0 and test_result["exit_code"] == 0 and not refs:
        envelope = {
            "status": "clean",
            "summary": f"Build + tests pass for {marker} project",
            "issues": [],
            "build_exit": 0, "test_exit": 0,
            "build_cmd": build_cmd, "test_cmd": test_cmd,
            "language": language,
            "project_dir": project_dir,
            "run_id": run_id,
        }
        await events.emit(conv_id, "tool_end", {
            "tool": "run_review", "icon": "search-check",
            "status": "✅ Review clean — build + tests pass",
            "run_id": run_id,
        })
        if run_id:
            try:
                await db.update_run(run_id, status="succeeded",
                                    result_envelope=envelope, ended=True)
            except Exception:
                pass
        return envelope

    # 5. Slow path: feed everything to the planning-model LLM and parse JSON.
    review_model = config.PLANNING_MODEL or config.DEFAULT_MODEL
    prompt = _REVIEW_PROMPT.format(
        build_cmd=build_cmd or "(none)",
        build_exit=build_result["exit_code"],
        test_cmd=test_cmd or "(none)",
        test_exit=test_result["exit_code"],
        lint_section=(f"Lint command: `{lint_cmd}`\nLint exit: {lint_result['exit_code']}" if lint_cmd else ""),
        build_chars=len(build_result.get("stdout", "")),
        test_chars=len(test_result.get("stdout", "")),
        build_output=build_result.get("stdout", "")[:4000],
        test_block=("\n## Test output\n```\n" + test_result.get("stdout", "")[:3000] + "\n```") if test_cmd else "",
        lint_block=("\n## Lint output\n```\n" + lint_result.get("stdout", "")[:1500] + "\n```") if lint_cmd else "",
        snippet_block=snippet_block,
    )

    await _step("analyze", f"calling {review_model}")
    review_text = ""
    try:
        r = await http.post(
            f"{config.OLLAMA_URL}/api/chat",
            json={
                "model": review_model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.2, "num_ctx": config.DEFAULT_NUM_CTX},
            },
            timeout=600,
        )
        if r.status_code == 200:
            review_text = (r.json().get("message", {}).get("content") or "").strip()
    except Exception as e:
        print(f"[REVIEWER] LLM call failed: {e}")

    parsed = _try_parse_review_json(review_text)
    if not parsed or "status" not in parsed:
        # Heuristic fallback when the model didn't emit clean JSON.
        await _step("parse_fallback", "model output was not valid JSON")
        parsed = {
            "status": "issues" if (build_result["exit_code"] != 0 or test_result["exit_code"] != 0) else "clean",
            "summary": "Reviewer could not parse model output cleanly; reporting raw build/test exits.",
            "issues": [{
                "severity": "compile" if build_result["exit_code"] != 0 else "test",
                "file": refs[0]["file"] if refs else "",
                "lines": [refs[0]["line"]] if refs and refs[0]["line"] else [],
                "summary": (failure_text[:200] or "build/test failed").strip(),
                "suggested_fix_scope": [r["file"] for r in refs[:3]],
            }] if (build_result["exit_code"] != 0 or test_result["exit_code"] != 0) else [],
        }

    envelope = {
        **parsed,
        "build_cmd": build_cmd, "test_cmd": test_cmd, "lint_cmd": lint_cmd,
        "build_exit": build_result["exit_code"], "test_exit": test_result["exit_code"],
        "lint_exit": lint_result["exit_code"],
        "language": language, "marker": marker,
        "review_model": review_model,
        "raw_review_chars": len(review_text),
        "project_dir": project_dir,
        "run_id": run_id,
    }

    final_status = "succeeded" if envelope.get("status") == "clean" else "succeeded"  # The Reviewer itself succeeded; the project state is what `status` reports.
    n_issues = len(envelope.get("issues") or [])
    await events.emit(conv_id, "tool_end", {
        "tool": "run_review", "icon": "search-check",
        "status": (f"✅ Review clean ({build_cmd} ok)" if envelope.get("status") == "clean"
                   else f"⚠ Review found {n_issues} issue{'s' if n_issues != 1 else ''}"),
        "run_id": run_id,
    })
    if run_id:
        try:
            await db.update_run(run_id, status=final_status,
                                result_envelope=envelope, ended=True)
        except Exception:
            pass

    return envelope
