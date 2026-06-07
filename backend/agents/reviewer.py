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

import asyncio
import json
import re
import shlex
import uuid

import config
import database as db
import cancel_registry


# Pin the venv python that Codebox provisions — same binary `run_tests` uses
# (tools.py:2009). Using bare `python3` here meant pip install -e . hit Arch's
# externally-managed system Python (silently failed under `|| true`) and the
# pytest run that followed had no project deps installed → ImportError → the
# `|| echo` chain swallowed it → exit 0 → reviewer reported "clean" while the
# project was actually broken.
_VENV_PY = "/root/venv/bin/python3"

# Python test command that:
#   - Uses the venv python so installed deps are visible
#   - Distinguishes pytest exit 5 (no tests collected) from 1/3/4 (real
#     failures) — only exit 5 is converted to success-with-note. All other
#     non-zero exits propagate so the reviewer can flag them.
#   - Falls back to unittest discover ONLY when pytest itself isn't installed
#     in the venv. Previously the chain treated "tests failed" the same as
#     "pytest missing" and quietly succeeded.
#   - Lets stderr through (no `2>/dev/null`) so the LLM analysis path sees
#     real diagnostics. The reviewer combines stdout+stderr at the Codebox
#     wrapper level via `2>&1` already.
_PY_TEST_CMD = (
    f"if {_VENV_PY} -c 'import pytest' 2>/dev/null; then "
    f"{_VENV_PY} -m pytest -q --no-header; ec=$?; "
    "if [ $ec -eq 5 ]; then echo '(no tests collected)'; exit 0; fi; "
    "exit $ec; "
    f"else {_VENV_PY} -m unittest discover -q; fi"
)

# Lint = a syntax sweep using the venv python (system python may not match
# project syntax level, e.g. PEP 695 generics on python 3.12+). Kept separate
# from build_cmd so its exit code surfaces as `lint_exit` in the envelope.
_PY_LINT_CMD = (
    f"{_VENV_PY} -m py_compile "
    "$(find . -name '*.py' -not -path '*/venv/*' -not -path '*/.venv/*' "
    "-not -path '*/.git/*' -not -path '*/__pycache__/*')"
)

_FIND_REVIEW_IGNORES = (
    "-not -path '*/node_modules/*' -not -path '*/venv/*' "
    "-not -path '*/.venv/*' -not -path '*/.git/*' "
    "-not -path '*/__pycache__/*' -not -path '*/out/*' "
    "-not -path '*/build/*' -not -path '*/target/*' "
    "-not -path '*/dist/*' -not -path '*/.pytest_cache/*' "
    "-not -path '*/.mypy_cache/*' -not -path '*/.ruff_cache/*' "
    "-not -path '*/.cache/*'"
)

_STATIC_HTML_BUILD_CMD = (
    "test -s index.html && "
    "grep -Eiq '<!doctype|<html' index.html && "
    "grep -Eiq '<body|<script|<canvas|<button|<main|<div' index.html && "
    "echo '(static HTML structure ok)'"
)

_STATIC_JS_LINT_CMD = (
    "if command -v node >/dev/null 2>&1; then "
    "fail=0; "
    "for f in $(find . -type f -name '*.js' "
    f"{_FIND_REVIEW_IGNORES}); do "
    "node --check \"$f\" || fail=1; "
    "done; exit $fail; "
    "else echo '(node unavailable; JS syntax check skipped)'; fi"
)

_GENERIC_STATIC_BUILD_CMD = (
    f"find . -type f {_FIND_REVIEW_IGNORES} | head -1 | grep -q . && "
    "echo '(generic project inventory present; no safe build command detected)'"
)

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
    # Python markers: install deps into the venv that codebox provisions, then
    # run pytest with proper exit-code discrimination (see _PY_TEST_CMD above).
    # Pip's exit code is NOT swallowed — if install fails (missing pkg, syntax
    # error in setup.py, etc.) the reviewer flags it as a build issue. That's
    # the right behavior; previously `|| true` meant a broken pip step looked
    # the same as a successful one.
    ("pyproject.toml",  f"{_VENV_PY} -m pip install -q -e .",
                                                            _PY_TEST_CMD,
                                                                                      _PY_LINT_CMD,
                                                                                                            "python"),
    ("requirements.txt",f"{_VENV_PY} -m pip install -q -r requirements.txt",
                                                            _PY_TEST_CMD,
                                                                                      _PY_LINT_CMD,
                                                                                                            "python"),
    ("Makefile",        "make -s 2>&1 | head -200",         "make -s test 2>&1 | head -200", "",            ""),
    ("CMakeLists.txt",  "cmake -B build -S . && cmake --build build --quiet",
                                                            "cd build && ctest --output-on-failure",
                                                                                      "",                   "cpp"),
]


# Plain-source fallbacks when no formal build file exists. Order matters: we
# pick the language with the most source files in the tree.
_PLAIN_LANG_PROFILES = {
    "html":   {"glob": "*.html",
               "build": _STATIC_HTML_BUILD_CMD,
               "test":  "echo '(static HTML app — no automated tests detected)'",
               "lint":  _STATIC_JS_LINT_CMD,
               "verification_level": "static-inspected"},
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
               "lint":  "",
               "verification_level": "syntax-verified"},
    "python": {"glob": "*.py",
               # Plain-source python: no pyproject/requirements means no
               # `pip install` step; we just sweep with py_compile to catch
               # syntax errors. Use the venv python so 3.12+ syntax (PEP 695,
               # type alias) doesn't fail on a system 3.11. Same exit-code
               # discrimination as the marker-driven path above so a
               # greenfield "no tests" project doesn't look like a failure.
               "build": _PY_LINT_CMD,
               "test":  _PY_TEST_CMD,
               "lint":  "",
               "verification_level": "syntax-verified"},
    "go":     {"glob": "*.go",
               "build": "go build ./... 2>&1 || (find . -name '*.go' -exec gofmt -l {} \\;)",
               "test":  "go test ./... 2>&1 || echo '(no go.mod — no tests)'",
               "lint":  "go vet ./... 2>&1 || true",
               "verification_level": "syntax-verified"},
    "rust":   {"glob": "*.rs",
               "build": "rustc --edition 2021 --crate-type bin $(find . -name 'main.rs' | head -1) 2>&1 "
                        "|| find . -name '*.rs' -exec rustc --edition 2021 --emit=metadata {} \\; 2>&1",
               "test":  "echo '(no Cargo.toml — tests not run)'",
               "lint":  "",
               "verification_level": "syntax-verified"},
    "javascript": {"glob": "*.js",
                   "build": ("if command -v node >/dev/null 2>&1; then "
                             "fail=0; for f in $(find . -name '*.js' -not -path '*/node_modules/*'); do "
                             "node --check \"$f\" || fail=1; done; exit $fail; "
                             "else echo '(node unavailable; JS syntax check skipped)'; fi"),
                   "test":  "echo '(no package.json — tests not run)'",
                   "lint":  "",
                   "verification_level": "syntax-verified"},
    "typescript": {"glob": "*.ts",
                   "build": ("if command -v tsc >/dev/null 2>&1; then "
                             "tsc --noEmit $(find . -name '*.ts' -not -path '*/node_modules/*') 2>&1; "
                             "else echo '(tsc unavailable; TypeScript syntax check skipped)'; fi"),
                   "test":  "echo '(no package.json — tests not run)'",
                   "lint":  "",
                   "verification_level": "syntax-verified"},
    "c":      {"glob": "*.c",
               "build": "gcc -Wall -fsyntax-only $(find . -name '*.c') 2>&1",
               "test":  "echo '(plain C — no test runner)'",
               "lint":  "",
               "verification_level": "syntax-verified"},
    "cpp":    {"glob": "*.cpp",
               "build": "g++ -Wall -fsyntax-only $(find . -name '*.cpp' -o -name '*.cc') 2>&1",
               "test":  "echo '(plain C++ — no test runner)'",
               "lint":  "",
               "verification_level": "syntax-verified"},
    "ruby":   {"glob": "*.rb",
               "build": ("if command -v ruby >/dev/null 2>&1; then "
                         "fail=0; for f in $(find . -name '*.rb' -not -path '*/vendor/*'); do "
                         "ruby -c \"$f\" || fail=1; done; exit $fail; "
                         "else echo '(ruby unavailable; Ruby syntax check skipped)'; fi"),
               "test":  "echo '(plain Ruby — no test runner detected)'",
               "lint":  "",
               "verification_level": "syntax-verified"},
    "php":    {"glob": "*.php",
               "build": ("if command -v php >/dev/null 2>&1; then "
                         "fail=0; for f in $(find . -name '*.php' -not -path '*/vendor/*'); do "
                         "php -l \"$f\" || fail=1; done; exit $fail; "
                         "else echo '(php unavailable; PHP syntax check skipped)'; fi"),
               "test":  "echo '(plain PHP — no test runner detected)'",
               "lint":  "",
               "verification_level": "syntax-verified"},
    "shell":  {"glob": "*.sh",
               "build": ("fail=0; for f in $(find . -name '*.sh'); do "
                         "sh -n \"$f\" || fail=1; done; exit $fail"),
               "test":  "echo '(plain shell — no test runner detected)'",
               "lint":  "",
               "verification_level": "syntax-verified"},
}


async def _detect_project(http, project_dir: str) -> dict:
    """Look at the project's top-level files and pick build/test commands.

    Three layers:
      1. Match a top-level build marker (pom.xml, Cargo.toml, etc.) → use it.
      2. Walk the tree for plain source files (any depth) and pick the language
         with the most sources, e.g. a `src/main/java/...` directory of .java
         files → run javac directly. Catches projects where the model wrote
         sources but skipped the build file.
      3. If files exist but no safe build command is known, use generic static
         inspection instead of treating "no package manager" as a project error.
    """
    listing = ""
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": f"ls -1 {shlex.quote(project_dir)}", "timeout": 5},
            timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            if int(d.get("exit_code", 0) or 0) != 0:
                return {"error": (d.get("stdout") or d.get("stderr") or f"Cannot list {project_dir}")[:500],
                        "build_cmd": "", "test_cmd": "", "lint_cmd": "", "language": ""}
            listing = (d.get("stdout") or "")
    except Exception as e:
        return {"error": f"Cannot list {project_dir}: {e}",
                "build_cmd": "", "test_cmd": "", "lint_cmd": "", "language": ""}

    files = set(line.strip() for line in listing.splitlines() if line.strip())
    for marker, build, test, lint, lang in _PROJECT_MARKERS:
        if marker in files:
            return {"marker": marker, "build_cmd": build, "test_cmd": test,
                    "lint_cmd": lint, "language": lang, "files": sorted(files)[:30],
                    "verification_level": "build-verified", "confidence": "high",
                    "profile": f"marker:{marker}"}

    if "index.html" in files:
        prof = _PLAIN_LANG_PROFILES["html"]
        return {"marker": "index.html", "build_cmd": prof["build"],
                "test_cmd": prof["test"], "lint_cmd": prof["lint"],
                "language": "html", "files": sorted(files)[:30],
                "verification_level": prof["verification_level"],
                "confidence": "high", "profile": "static-html"}

    # Layer 2: tree-walk for plain source files. Run a single find that emits
    # extension counts so we don't make N round trips.
    tree_cmd = (
        f"cd {shlex.quote(project_dir)} && "
        "find . -type f \\( "
        "-name '*.java' -o -name '*.py' -o -name '*.go' -o -name '*.rs' "
        "-o -name '*.js' -o -name '*.jsx' -o -name '*.ts' -o -name '*.tsx' "
        "-o -name '*.html' -o -name '*.css' -o -name '*.c' -o -name '*.cpp' -o -name '*.cc' "
        "-o -name '*.rb' -o -name '*.php' -o -name '*.cs' -o -name '*.swift' "
        "-o -name '*.scala' -o -name '*.kt' -o -name '*.sh' "
        "\\) "
        f"{_FIND_REVIEW_IGNORES} "
        "| sed -E 's/.*\\.(java|py|go|rs|js|jsx|ts|tsx|html|css|c|cpp|cc|rb|php|cs|swift|scala|kt|sh)$/\\1/' "
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
        "js": "javascript", "jsx": "javascript", "ts": "typescript",
        "tsx": "typescript", "html": "html", "css": "html",
        "c": "c", "cpp": "cpp", "cc": "cpp", "rb": "ruby",
        "php": "php", "kt": "java", "sh": "shell",
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
                    "files": sorted(files)[:30],
                    "verification_level": prof["verification_level"],
                    "confidence": "medium", "profile": f"plain-{winner}"}

    inventory_cmd = (
        f"cd {shlex.quote(project_dir)} && "
        f"find . -type f {_FIND_REVIEW_IGNORES} | sed 's#^./##' | head -100"
    )
    inventory: list[str] = []
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": inventory_cmd, "timeout": 10},
            timeout=15,
        )
        if r.status_code == 200:
            inventory = [line.strip() for line in (r.json().get("stdout") or "").splitlines()
                         if line.strip()]
    except Exception:
        pass

    if inventory:
        return {"marker": f"{len(inventory)} generic file{'s' if len(inventory) != 1 else ''}",
                "build_cmd": _GENERIC_STATIC_BUILD_CMD,
                "test_cmd": "echo '(generic project — no automated tests detected)'",
                "lint_cmd": "", "language": "generic",
                "files": inventory[:30],
                "verification_level": "static-inspected",
                "confidence": "low", "profile": "generic-static"}

    return {"error": f"No deliverable files found at {project_dir}",
            "marker": "(empty)", "build_cmd": "", "test_cmd": "", "lint_cmd": "",
            "language": "", "files": [], "verification_level": "none",
            "confidence": "none", "profile": "empty"}


async def _run_in_sandbox(http, project_dir: str, command: str,
                          timeout: int = 300, run_id: str = "") -> dict:
    """Run a shell command at project_dir via Codebox. Returns truncated stdout/stderr.
    When run_id is provided, the HTTP call is wrapped in cancel_registry so the
    user's Stop button can abort it mid-flight."""
    if not command:
        return {"exit_code": -1, "stdout": "", "stderr": "(no command)"}
    try:
        coro = http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": f"cd {shlex.quote(project_dir)} && ({command}) 2>&1",
                  "timeout": timeout},
            timeout=timeout + 30,
        )
        r = await (cancel_registry.await_cancellable(coro, run_id) if run_id else coro)
        if r.status_code != 200:
            return {"exit_code": -1, "stdout": "",
                    "stderr": f"Codebox HTTP {r.status_code}: {r.text[:200]}"}
        d = r.json()
        out = (d.get("stdout") or "")
        if len(out) > 4096:
            out = "... [truncated head] ...\n" + out[-4096:]
        return {"exit_code": d.get("exit_code", 0), "stdout": out, "stderr": d.get("stderr", "")}
    except cancel_registry.RunCancelled:
        raise
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": f"Exception: {e}"}


# Files referenced in error output (Java, Python, Rust, Go, JS/TS).
_FILE_REFS_RE = re.compile(
    r"(?:^|[\s\(\[])"
    r"((?:[\w./-]*?/)?[A-Za-z][\w-]*\.(?:java|py|rs|go|js|jsx|ts|tsx|cpp|c|h|hpp|kt|rb|php|cs|swift|scala))"
    r"(?::(\d+))?",
    re.MULTILINE,
)
_SOURCE_EXTS = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php",
    ".cs", ".swift", ".scala", ".kt", ".cpp", ".cc", ".c", ".h", ".hpp",
    ".html", ".css",
)
_DOC_EXTS = (".md", ".rst", ".txt")
_CONFIG_EXTS = (
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties",
    ".xml", ".sql", ".sh", ".env",
)
_MANIFEST_NAMES = {
    "requirements.txt", "requirements-dev.txt", "dev-requirements.txt",
    "constraints.txt", "Makefile", "go.mod", "go.sum", "Gemfile", "Procfile",
}
_IGNORED_PARTS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".cache", ".venv", "venv", "node_modules", "dist", "build", "target",
}
_STORAGE_NAME_RE = re.compile(
    r"(?:^|[/_.-])(db|database|storage|store|repository|repo|persist|"
    r"persistence|schema|migration|migrations|dao|model|models)(?:$|[/_.-])",
    re.I,
)
_STATE_ERROR_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"\bno such column\b",
        r"\bno such table\b",
        r"\bmissing column\b",
        r"\bcolumn ['\"]?[\w.-]+['\"]? (?:does not exist|not found)\b",
        r"\brelation ['\"]?[\w.-]+['\"]? does not exist\b",
        r"\btable ['\"]?[\w.-]+['\"]? (?:does not exist|not found)\b",
        r"\bdatabase schema\b",
        r"\bschema mismatch",
        r"\bmigration\b.*\b(failed|missing|needed|required)\b",
        r"\bno item with that key\b",
        r"\bkeyerror\b",
        r"\balready exists\b",
        r"\bduplicate key\b",
        r"\bunique constraint\b",
        r"\bforeign key constraint\b",
        r"\bdatabase is locked\b",
        r"\bstale (?:database|db|cache|state)\b",
        r"\bshared (?:database|db|cache|state)\b",
    )
]
_STATE_PATH_RE = re.compile(
    r"(?:~|/root|/home/[A-Za-z0-9_.-]+)/(?:\.[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+)"
    r"(?:/[^\s'\"),;]+)?"
)
_STATE_PATH_EXCLUDE_RE = re.compile(
    r"(^/root/venv/|/(?:\.venv|venv)/|^/usr/|^/opt/|"
    r"/(?:bin|sbin)/(?:python\d*|pip\d*|pytest|node|npm|npx|bash|sh)$)",
    re.I,
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


def _is_probably_test_file(path: str) -> bool:
    path = (path or "").replace("\\", "/")
    base = path.rsplit("/", 1)[-1].lower()
    return (
        path.startswith(("test/", "tests/", "spec/", "specs/"))
        or "/test/" in path or "/tests/" in path or "/spec/" in path or "/specs/" in path
        or base.startswith(("test_", "spec_"))
        or base.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts", "test.java", "tests.java"))
    )


def _source_file_score(path: str) -> int:
    p = (path or "").lower()
    if p.endswith(_SOURCE_EXTS):
        return 0
    if p.endswith(_CONFIG_EXTS) or p.rsplit("/", 1)[-1] in _MANIFEST_NAMES:
        return 1
    return 2


def _valid_project_file(path: str) -> bool:
    p = (path or "").replace("\\", "/").strip()
    if not p or p.startswith(("/", "../")) or "/../" in p:
        return False
    parts = set(p.split("/"))
    if parts & _IGNORED_PARTS:
        return False
    return (
        p.endswith(_SOURCE_EXTS + _CONFIG_EXTS + _DOC_EXTS)
        or p.rsplit("/", 1)[-1] in _MANIFEST_NAMES
    )


async def _list_project_files(http, project_dir: str, limit: int = 5000) -> list[str]:
    """Return real project-relative files that Reviewer/Aider may cite."""
    cmd = (
        f"cd {shlex.quote(project_dir)} && "
        "find . -type f "
        "-not -path '*/.git/*' -not -path '*/__pycache__/*' "
        "-not -path '*/.pytest_cache/*' -not -path '*/.mypy_cache/*' "
        "-not -path '*/.ruff_cache/*' -not -path '*/.cache/*' "
        "-not -path '*/.venv/*' -not -path '*/venv/*' "
        "-not -path '*/node_modules/*' -not -path '*/dist/*' "
        "-not -path '*/build/*' -not -path '*/target/*' "
        f"| sed 's#^./##' | head -{int(limit)}"
    )
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": cmd, "timeout": 20},
            timeout=30,
        )
        if r.status_code == 200:
            files = []
            for line in (r.json().get("stdout") or "").splitlines():
                p = line.strip()
                if _valid_project_file(p) and p not in files:
                    files.append(p)
            return files
    except Exception:
        pass
    return []


def _resolve_project_file(path: str, project_files: list[str]) -> str:
    """Map a possibly guessed path to a real project-relative file."""
    raw = (path or "").replace("\\", "/").strip().strip("'\"")
    if not raw or not project_files:
        return ""
    raw = re.sub(r"^/root/projects/[A-Za-z0-9._-]+/", "", raw)
    raw = raw[2:] if raw.startswith("./") else raw
    if raw in project_files:
        return raw

    by_lower = {p.lower(): p for p in project_files}
    if raw.lower() in by_lower:
        return by_lower[raw.lower()]

    # A guessed path may have the right suffix but the wrong package root.
    suffix_matches = [p for p in project_files if p.lower().endswith("/" + raw.lower())]
    if len(suffix_matches) == 1:
        return suffix_matches[0]

    base = raw.rsplit("/", 1)[-1].lower()
    if not base:
        return ""
    base_matches = [p for p in project_files if p.rsplit("/", 1)[-1].lower() == base]
    if len(base_matches) == 1:
        return base_matches[0]
    if base_matches:
        ranked = sorted(
            base_matches,
            key=lambda p: (_source_file_score(p), 1 if _is_probably_test_file(p) else 0, len(p), p),
        )
        return ranked[0]
    return ""


def _state_error_signals(text: str) -> list[str]:
    signals = []
    lower = text or ""
    for pat in _STATE_ERROR_PATTERNS:
        m = pat.search(lower)
        if m:
            sig = m.group(0).strip()
            if sig and sig.lower() not in [s.lower() for s in signals]:
                signals.append(sig)
    return signals[:6]


def _extract_state_paths(text: str) -> list[str]:
    paths = []
    for m in _STATE_PATH_RE.finditer(text or ""):
        p = m.group(0).rstrip(".,:;")
        if p.startswith("/root/projects/"):
            continue
        if _STATE_PATH_EXCLUDE_RE.search(p):
            continue
        if p not in paths:
            paths.append(p)
    return paths[:5]


def _rank_storage_candidates(project_files: list[str]) -> list[str]:
    candidates = [p for p in project_files or [] if _STORAGE_NAME_RE.search(p)]
    ranked = sorted(
        candidates,
        key=lambda p: (
            _source_file_score(p),
            1 if _is_probably_test_file(p) else 0,
            0 if re.search(r"(^|/)db\.", p, re.I) else 1,
            0 if re.search(r"(^|/)database\.", p, re.I) else 1,
            len(p),
            p,
        ),
    )
    return ranked[:8]


def _test_files_from_refs(refs: list[dict], project_files: list[str]) -> list[str]:
    out = []
    for ref in refs or []:
        resolved = _resolve_project_file(ref.get("file") or "", project_files)
        if resolved and _is_probably_test_file(resolved) and resolved not in out:
            out.append(resolved)
    if out:
        return out[:5]
    for p in project_files or []:
        if _is_probably_test_file(p) and p not in out:
            out.append(p)
            if len(out) >= 5:
                break
    return out


def _state_isolation_issue_from_failure(failure_text: str, project_dir: str,
                                        project_files: list[str],
                                        refs: list[dict]) -> dict | None:
    """Classify persistent test-state/storage-schema failures without LLM guesses.

    This is language-neutral: it uses failure text ("no such column", duplicate
    key, stale DB/cache state) and the actual files present in the project.
    """
    signals = _state_error_signals(failure_text)
    if not signals:
        return None

    storage_files = _rank_storage_candidates(project_files)
    test_files = _test_files_from_refs(refs, project_files)
    scope = []
    for p in storage_files[:3] + test_files[:3]:
        if p and p not in scope:
            scope.append(p)

    primary = storage_files[0] if storage_files else (test_files[0] if test_files else "")
    state_paths = _extract_state_paths(failure_text)
    signal_display = ", ".join(signals[:3])
    scope_hint = (
        "Make the application's data store configurable for tests and give each "
        "test a fresh temp DB/cache/state path; also make schema creation or "
        "migration idempotent for existing state."
    )
    if state_paths:
        scope_hint += " Detected persistent state path(s): " + ", ".join(state_paths) + "."

    return {
        "status": "issues",
        "summary": (
            "Tests fail with persistent storage/schema state errors"
            + (f" ({signal_display})." if signal_display else ".")
        ),
        "issues": [{
            "severity": "test",
            "file": primary,
            "lines": [],
            "summary": scope_hint,
            "suggested_fix_scope": scope,
            "state_error_signals": signals,
            "state_paths": state_paths,
            "test_isolation_suspected": True,
        }],
        "deterministic_issue": "persistent_test_state",
        "state_error_signals": signals,
        "state_paths": state_paths,
    }


_DEP_INSTALL_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"\bfailed building wheel for\s+([A-Za-z0-9_.-]+)",
        r"\bfailed to build installable wheels\b",
        r"\bcould not find a version that satisfies the requirement\b",
        r"\bno matching distribution found\b",
        r"\brequires a different python\b",
        r"\bpackage .+ requires python\b",
    )
]
_DEP_PACKAGE_RE = re.compile(
    r"(?:failed building wheel for|failed to build installable wheels .*?\(|"
    r"no matching distribution found for|requirement already satisfied:)\s*"
    r"([A-Za-z0-9_.-]+)",
    re.I,
)
_PY_VERSION_RE = re.compile(r"python(?:\s+|/|)(\d+\.\d+)", re.I)


def _extract_failed_dependency_names(text: str) -> list[str]:
    names = []
    for m in _DEP_PACKAGE_RE.finditer(text or ""):
        name = (m.group(1) or "").strip(" .,:;)").lower()
        if name and name not in names:
            names.append(name)
    return names[:5]


def _dependency_manifest_for_failure(marker: str, project_files: list[str]) -> str:
    candidates = []
    if marker:
        candidates.append(marker)
    candidates.extend([
        "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
        "constraints.txt", "package.json", "Cargo.toml", "go.mod",
    ])
    for candidate in candidates:
        resolved = _resolve_project_file(candidate, project_files)
        if resolved:
            return resolved
    return marker or ""


def _dependency_install_issue_from_failure(build_cmd: str, failure_text: str,
                                           marker: str,
                                           project_files: list[str]) -> dict | None:
    """Classify external dependency install failures before the LLM guesses files.

    Pip/compiler traces often point at generated package sources such as
    src_c/_sdl2/sdl2.c. Those are not project files; the actionable fix is the
    manifest pin or package choice.
    """
    if "pip install" not in (build_cmd or ""):
        return None
    if not any(p.search(failure_text or "") for p in _DEP_INSTALL_PATTERNS):
        return None

    manifest = _dependency_manifest_for_failure(marker, project_files)
    if not manifest:
        return None

    packages = _extract_failed_dependency_names(failure_text)
    versions = []
    for m in _PY_VERSION_RE.finditer(failure_text or ""):
        version = m.group(1)
        if version and version not in versions:
            versions.append(version)
    package_text = f" for {', '.join(packages[:3])}" if packages else ""
    version_text = f" on Python {versions[0]}" if versions else ""
    summary = (
        f"Dependency install failed{package_text}{version_text}. Update the project "
        "dependency manifest so the pinned package versions have wheels/support "
        "for the current runtime, or choose a compatible replacement. Do not edit "
        "generated external package sources referenced in pip's compiler output."
    )
    return {
        "status": "issues",
        "summary": summary,
        "issues": [{
            "severity": "dependency",
            "file": manifest,
            "lines": [],
            "summary": summary,
            "suggested_fix_scope": [manifest],
            "dependency_install_failure": True,
            "failed_packages": packages,
            "python_versions": versions[:3],
        }],
        "deterministic_issue": "dependency_install_failure",
    }


def _sanitize_review_envelope(parsed: dict, failure_text: str,
                              project_files: list[str],
                              refs: list[dict]) -> dict:
    """Normalize Reviewer output so Aider only receives real project files."""
    if not isinstance(parsed, dict):
        return parsed
    if parsed.get("status") == "clean":
        parsed["issues"] = []
        return parsed

    storage_files = _rank_storage_candidates(project_files)
    test_files = _test_files_from_refs(refs, project_files)
    existing_ref_files = []
    for ref in refs or []:
        resolved = _resolve_project_file(ref.get("file") or "", project_files)
        if resolved and resolved not in existing_ref_files:
            existing_ref_files.append(resolved)

    sanitized = {**parsed}
    issues = []
    for issue in parsed.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        new_issue = {**issue}
        issue_text = "\n".join([
            str(issue.get("file") or ""),
            str(issue.get("summary") or ""),
            " ".join(str(p) for p in issue.get("suggested_fix_scope") or []),
            failure_text[-2500:],
        ])
        state_signals = _state_error_signals(issue_text)

        original_file = issue.get("file") or ""
        resolved_file = _resolve_project_file(original_file, project_files)
        if state_signals and storage_files:
            resolved_file = storage_files[0]
        if not resolved_file:
            resolved_file = (
                existing_ref_files[0] if existing_ref_files else
                (storage_files[0] if storage_files else
                 (project_files[0] if project_files else original_file))
            )
        new_issue["file"] = resolved_file
        if original_file and resolved_file and original_file != resolved_file:
            new_issue["normalized_from_file"] = original_file

        scope = []
        for raw in issue.get("suggested_fix_scope") or []:
            resolved = _resolve_project_file(str(raw), project_files)
            if resolved and resolved not in scope:
                scope.append(resolved)
        if state_signals:
            for p in storage_files[:3] + test_files[:3]:
                if p and p not in scope:
                    scope.append(p)
            new_issue["state_error_signals"] = state_signals
            new_issue["test_isolation_suspected"] = True
        if resolved_file and resolved_file not in scope:
            scope.insert(0, resolved_file)
        new_issue["suggested_fix_scope"] = scope[:8]
        issues.append(new_issue)

    sanitized["issues"] = issues
    if issues and sanitized.get("status") not in {"issues", "error", "cancelled"}:
        sanitized["status"] = "issues"
    return sanitized


_PROJECT_ROOT_RE = re.compile(r"/root/projects/[A-Za-z0-9._-]+")


def _extract_stale_project_paths(text: str, project_dir: str) -> list[str]:
    """Return /root/projects/<id> literals that do not match project_dir."""
    active = (project_dir or "").rstrip("/")
    out: list[str] = []
    for path in _PROJECT_ROOT_RE.findall(text or ""):
        root = path.rstrip("/")
        if root == active:
            continue
        if root not in out:
            out.append(root)
    return out


def _parse_project_path_grep(stdout: str, project_dir: str) -> list[dict]:
    """Parse grep -RIn output for stale /root/projects literals."""
    hits: list[dict] = []
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        raw_file, raw_line, text = parts
        rel_file = raw_file[2:] if raw_file.startswith("./") else raw_file
        try:
            line_no = int(raw_line)
        except ValueError:
            line_no = 0
        stale = _extract_stale_project_paths(text, project_dir)
        if stale:
            hits.append({
                "file": rel_file,
                "line": line_no,
                "text": text[:500],
                "stale_paths": stale,
            })
    return hits


async def _grep_project_root_literals(http, project_dir: str) -> list[dict]:
    """Find project files that contain hardcoded /root/projects paths."""
    cmd = (
        f"cd {shlex.quote(project_dir)} && "
        "grep -RIn --exclude-dir=.git --exclude-dir=.pytest_cache "
        "--exclude-dir=__pycache__ --exclude-dir=.venv --exclude-dir=venv "
        "--exclude-dir=node_modules --exclude-dir=dist --exclude-dir=build "
        "--exclude-dir=target -- '/root/projects/' . 2>/dev/null | head -80 || true"
    )
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": cmd, "timeout": 15},
            timeout=20,
        )
        if r.status_code == 200:
            return _parse_project_path_grep(r.json().get("stdout") or "", project_dir)
    except Exception:
        pass
    return []


def _stale_path_issue_from_failure(failure_text: str, project_dir: str,
                                   grep_hits: list[dict]) -> dict | None:
    """Classify test failures caused by hardcoded stale upload roots.

    This intentionally runs before LLM review. A pytest subprocess failure that
    executes with cwd="/root/projects/old-upload" should route Aider to the test
    file containing that stale cwd, not to whatever source module failed inside
    the old copied project.
    """
    stale_from_failure = _extract_stale_project_paths(failure_text, project_dir)
    relevant_hits = []
    for hit in grep_hits or []:
        f = (hit.get("file") or "").lower()
        if f.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
                       ".toml", ".ini", ".cfg", ".yaml", ".yml", ".json", ".sh")):
            relevant_hits.append(hit)

    stale_paths: list[str] = []
    for path in stale_from_failure:
        if path not in stale_paths:
            stale_paths.append(path)
    for hit in relevant_hits:
        for path in hit.get("stale_paths") or []:
            if path not in stale_paths:
                stale_paths.append(path)
    if not stale_paths:
        return None

    stale_hit_files = []
    for hit in relevant_hits:
        if any(p in stale_paths for p in hit.get("stale_paths") or []):
            stale_hit_files.append(hit)

    def _hit_rank(hit: dict) -> tuple[int, int, str]:
        path = hit.get("file") or ""
        base = path.rsplit("/", 1)[-1]
        is_test = path.startswith("tests/") or "/tests/" in path or base.startswith("test_") or "_test." in base
        return (0 if is_test else 1, len(path), path)

    selected_hit = sorted(stale_hit_files, key=_hit_rank)[0] if stale_hit_files else None
    refs = _extract_file_refs(failure_text)
    pytest_ref = next(
        (r for r in refs if (r.get("file") or "").startswith("tests/")
         or "/tests/" in (r.get("file") or "")
         or (r.get("file") or "").rsplit("/", 1)[-1].startswith("test_")),
        None,
    )
    issue_file = (
        (selected_hit or {}).get("file")
        or (pytest_ref or {}).get("file")
        or (refs[0].get("file") if refs else project_dir)
    )
    issue_line = (selected_hit or {}).get("line") or (pytest_ref or {}).get("line") or 0
    active = (project_dir or "").rstrip("/")
    stale_display = ", ".join(stale_paths[:3])
    scope = []
    if issue_file and not issue_file.startswith("/root/projects/"):
        scope.append(issue_file)
    elif issue_file:
        scope.append(issue_file)
    for hit in sorted(stale_hit_files, key=_hit_rank):
        f = hit.get("file")
        if f and f not in scope:
            scope.append(f)
    return {
        "status": "issues",
        "summary": (
            f"Tests reference stale project root {stale_display} instead of "
            f"the active upload root {active}."
        ),
        "issues": [{
            "severity": "test",
            "file": issue_file,
            "lines": [issue_line] if issue_line else [],
            "summary": (
                f"Hardcoded stale project path {stale_display} makes the tests "
                f"run against the wrong upload; use {active} or a project-root-relative path."
            ),
            "suggested_fix_scope": scope[:5],
            "stale_project_paths": stale_paths,
            "expected_project_dir": active,
        }],
        "stale_project_paths": stale_paths,
        "expected_project_dir": active,
        "deterministic_issue": "stale_project_root",
    }


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

Verification profile: {verification_profile}
Verification level: {verification_level}
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
- If build_exit=0 AND test_exit=0 AND lint_exit=0 (all three), emit `status:"clean"` and `issues:[]`.
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
                     project_id: str = "", parent_run_id: str = "",
                     conv_model: str = "") -> dict:
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

    # Register cancel event so POST /api/runs/{id}/cancel can abort the
    # in-flight Codebox + Ollama work. Cleaned up at the end of this run.
    if run_id:
        cancel_registry.register(run_id)

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
    verification_level = detect.get("verification_level", "build-verified")
    profile = detect.get("profile", marker)
    confidence = detect.get("confidence", "medium")
    print(f"[REVIEWER] {project_dir}: marker={marker} build={build_cmd[:60]!r}")
    await _step("detected", f"profile={profile} marker={marker} lang={language} verification={verification_level}")

    # Hard fail when we can't identify the project — a reviewer that "passes"
    # an empty / nonexistent directory is worse than no reviewer at all. A
    # markerless but nonempty project is reviewed with a plain/static profile.
    if detect.get("error"):
        why = detect.get("error") or f"Reviewer could not inspect {project_dir}"
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
            "verification_level": verification_level,
            "verification_profile": profile,
            "verification_confidence": confidence,
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
            cancel_registry.cleanup(run_id)
        return envelope

    # 2. Run build / test / lint → snippets → fast path → LLM analysis.
    # All wrapped in a single RunCancelled handler so Stop works during any phase.
    build_result = {"exit_code": 0, "stdout": "(skipped — no build command)"}
    test_result = {"exit_code": 0, "stdout": "(skipped — no test command)"}
    lint_result = {"exit_code": 0, "stdout": ""}
    review_model = ""
    review_text = ""
    failure_text = ""
    refs: list[dict] = []
    project_files: list[str] = []
    _cancel_phase = "build"
    _ticker = None

    try:
        if build_cmd:
            _cancel_phase = "build"
            await _step("build", build_cmd)
            build_result = await _run_in_sandbox(http, project_dir, build_cmd, timeout=300, run_id=run_id)
            await _step("build_done", f"exit={build_result['exit_code']}")

        if test_cmd:
            _cancel_phase = "test"
            await _step("test", test_cmd)
            test_result = await _run_in_sandbox(http, project_dir, test_cmd, timeout=300, run_id=run_id)
            await _step("test_done", f"exit={test_result['exit_code']}")

        if lint_cmd:
            _cancel_phase = "lint"
            await _step("lint", lint_cmd)
            lint_result = await _run_in_sandbox(http, project_dir, lint_cmd, timeout=120, run_id=run_id)
            await _step("lint_done", f"exit={lint_result['exit_code']}")

        # 3. Pull file snippets for any failures.
        failure_text = ""
        if build_result["exit_code"] != 0:
            failure_text += build_result.get("stdout", "")
        if test_result["exit_code"] != 0:
            failure_text += "\n" + test_result.get("stdout", "")
        if lint_result["exit_code"] != 0:
            failure_text += "\n" + lint_result.get("stdout", "")
        refs = _extract_file_refs(failure_text)
        any_failure = (
            build_result["exit_code"] != 0
            or test_result["exit_code"] != 0
            or lint_result["exit_code"] != 0
        )
        project_files = []
        if any_failure:
            await _step("scan_files", "indexing real project files")
            project_files = await _list_project_files(http, project_dir)

        # Deterministic dependency-install classification. If pip fails while
        # building or resolving an external package, compiler traces can cite
        # generated package C files. Route the fix to the real manifest instead
        # of letting the LLM/sanitizer map that external file to app source.
        if build_result["exit_code"] != 0:
            dep_parsed = _dependency_install_issue_from_failure(
                build_cmd, failure_text, marker, project_files
            )
            if dep_parsed:
                envelope = {
                    **dep_parsed,
                    "build_cmd": build_cmd, "test_cmd": test_cmd, "lint_cmd": lint_cmd,
                    "build_exit": build_result["exit_code"], "test_exit": test_result["exit_code"],
                    "lint_exit": lint_result["exit_code"],
                    "build_stdout_tail": (build_result.get("stdout", "") or "")[-3000:],
                    "test_stdout_tail": (test_result.get("stdout", "") or "")[-5000:],
                    "lint_stdout_tail": (lint_result.get("stdout", "") or "")[-2000:],
                    "language": language, "marker": marker,
                    "verification_level": verification_level,
                    "verification_profile": profile,
                    "verification_confidence": confidence,
                    "review_model": "(deterministic dependency classifier)",
                    "raw_review_chars": 0,
                    "project_dir": project_dir,
                    "run_id": run_id,
                }
                n_issues = len(envelope.get("issues") or [])
                await events.emit(conv_id, "tool_end", {
                    "tool": "run_review", "icon": "search-check",
                    "status": f"⚠ Review found {n_issues} dependency issue",
                    "run_id": run_id,
                })
                if run_id:
                    try:
                        await db.update_run(run_id, status="succeeded",
                                            result_envelope=envelope, ended=True)
                    except Exception:
                        pass
                    cancel_registry.cleanup(run_id)
                return envelope

        # Deterministic stale-upload-root classification. This catches pytest
        # failures where tests still run subprocesses with cwd="/root/projects/<old>"
        # after the upload was remounted as /root/projects/proj-*. That failure
        # mode misleads the LLM toward source imports from the old project; the
        # correct fix is usually in the test file containing the hardcoded cwd.
        grep_hits: list[dict] = []
        if test_result["exit_code"] != 0:
            await _step("scan_paths", "checking for stale /root/projects literals")
            grep_hits = await _grep_project_root_literals(http, project_dir)
            stale_parsed = _stale_path_issue_from_failure(failure_text, project_dir, grep_hits)
            if stale_parsed:
                envelope = {
                    **stale_parsed,
                    "build_cmd": build_cmd, "test_cmd": test_cmd, "lint_cmd": lint_cmd,
                    "build_exit": build_result["exit_code"], "test_exit": test_result["exit_code"],
                    "lint_exit": lint_result["exit_code"],
                    "build_stdout_tail": (build_result.get("stdout", "") or "")[-3000:],
                    "test_stdout_tail": (test_result.get("stdout", "") or "")[-5000:],
                    "lint_stdout_tail": (lint_result.get("stdout", "") or "")[-2000:],
                    "language": language, "marker": marker,
                    "verification_level": verification_level,
                    "verification_profile": profile,
                    "verification_confidence": confidence,
                    "review_model": "(deterministic stale project-root classifier)",
                    "raw_review_chars": 0,
                    "project_dir": project_dir,
                    "run_id": run_id,
                }
                n_issues = len(envelope.get("issues") or [])
                await events.emit(conv_id, "tool_end", {
                    "tool": "run_review", "icon": "search-check",
                    "status": f"⚠ Review found {n_issues} stale project path issue",
                    "run_id": run_id,
                })
                if run_id:
                    try:
                        await db.update_run(run_id, status="succeeded",
                                            result_envelope=envelope, ended=True)
                    except Exception:
                        pass
                    cancel_registry.cleanup(run_id)
                return envelope

        # Deterministic persistent-state/schema classification. This catches a
        # common uploaded-project failure after source imports are fixed: CLI or
        # integration tests keep using an old/global DB/cache/state file, so the
        # failures look like missing columns, missing tables, duplicate keys, or
        # row key errors. The actionable fix is in the real storage/config/test
        # files, not in hallucinated modules such as storage.py when no such file
        # exists.
        if test_result["exit_code"] != 0:
            state_parsed = _state_isolation_issue_from_failure(
                failure_text, project_dir, project_files, refs
            )
            if state_parsed:
                envelope = {
                    **state_parsed,
                    "build_cmd": build_cmd, "test_cmd": test_cmd, "lint_cmd": lint_cmd,
                    "build_exit": build_result["exit_code"], "test_exit": test_result["exit_code"],
                    "lint_exit": lint_result["exit_code"],
                    "build_stdout_tail": (build_result.get("stdout", "") or "")[-3000:],
                    "test_stdout_tail": (test_result.get("stdout", "") or "")[-5000:],
                    "lint_stdout_tail": (lint_result.get("stdout", "") or "")[-2000:],
                    "language": language, "marker": marker,
                    "verification_level": verification_level,
                    "verification_profile": profile,
                    "verification_confidence": confidence,
                    "review_model": "(deterministic persistent-state classifier)",
                    "raw_review_chars": 0,
                    "project_dir": project_dir,
                    "run_id": run_id,
                }
                n_issues = len(envelope.get("issues") or [])
                await events.emit(conv_id, "tool_end", {
                    "tool": "run_review", "icon": "search-check",
                    "status": f"⚠ Review found {n_issues} persistent state issue",
                    "run_id": run_id,
                })
                if run_id:
                    try:
                        await db.update_run(run_id, status="succeeded",
                                            result_envelope=envelope, ended=True)
                    except Exception:
                        pass
                    cancel_registry.cleanup(run_id)
                return envelope

        snippet_blocks = []
        if refs:
            _read_tasks = [
                _read_file_snippet(http, project_dir, ref["file"], ref["line"])
                for ref in refs[:5]
            ]
            _snippets = await asyncio.gather(*_read_tasks, return_exceptions=True)
            for ref, snip in zip(refs[:5], _snippets):
                if isinstance(snip, str) and snip:
                    snippet_blocks.append(f"### {ref['file']}{':'+str(ref['line']) if ref['line'] else ''}\n```\n{snip[:1500]}\n```")
        snippet_block = ""
        if snippet_blocks:
            await _step("read_files", f"{len(snippet_blocks)} files for context")
            snippet_block = "\n\n## Failing-file snippets (for context only)\n\n" + "\n\n".join(snippet_blocks)

        # 4. Fast path: clean build + tests + lint → skip the LLM call entirely.
        if build_result["exit_code"] == 0 and test_result["exit_code"] == 0 and lint_result["exit_code"] == 0 and not refs:
            envelope = {
                "status": "clean",
                "summary": f"{verification_level} verification passed for {marker} project",
                "issues": [],
                "build_exit": 0, "test_exit": 0, "lint_exit": 0,
                "build_cmd": build_cmd, "test_cmd": test_cmd, "lint_cmd": lint_cmd,
                "build_stdout_tail": (build_result.get("stdout", "") or "")[-3000:],
                "test_stdout_tail": (test_result.get("stdout", "") or "")[-5000:],
                "lint_stdout_tail": (lint_result.get("stdout", "") or "")[-2000:],
                "language": language, "marker": marker,
                "verification_level": verification_level,
                "verification_profile": profile,
                "verification_confidence": confidence,
                "project_dir": project_dir,
                "run_id": run_id,
            }
            await events.emit(conv_id, "tool_end", {
                "tool": "run_review", "icon": "search-check",
                "status": f"✅ Review clean — {verification_level} checks pass",
                "run_id": run_id,
            })
            if run_id:
                try:
                    await db.update_run(run_id, status="succeeded",
                                        result_envelope=envelope, ended=True)
                except Exception:
                    pass
                cancel_registry.cleanup(run_id)
            return envelope

        # 5. Slow path: feed everything to the planning-model LLM and parse JSON.
        review_model = config.REVIEWER_MODEL or config.PLANNING_MODEL or conv_model or config.DEFAULT_MODEL
        prompt = _REVIEW_PROMPT.format(
            verification_profile=profile,
            verification_level=verification_level,
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

        _cancel_phase = "analysis"
        await _step("analyze", f"calling {review_model}")

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
        coro = http.post(
            f"{config.OLLAMA_URL}/api/chat",
            json={
                "model": review_model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "think": False,
                "options": {"temperature": 0.2, "num_ctx": config.DEFAULT_NUM_CTX},
            },
            timeout=600,
        )
        r = await cancel_registry.await_cancellable(coro, run_id)
        if r.status_code == 200:
            review_text = (r.json().get("message", {}).get("content") or "").strip()

    except cancel_registry.RunCancelled:
        cancelled_env = {
            "status": "cancelled",
            "summary": f"Reviewer cancelled by user during {_cancel_phase}.",
            "issues": [],
            "build_cmd": build_cmd, "test_cmd": test_cmd, "lint_cmd": lint_cmd,
            "build_exit": build_result["exit_code"],
            "test_exit": test_result["exit_code"],
            "lint_exit": lint_result["exit_code"],
            "language": language, "marker": marker,
            "verification_level": verification_level,
            "verification_profile": profile,
            "verification_confidence": confidence,
            "review_model": review_model,
            "project_dir": project_dir,
            "run_id": run_id,
        }
        try:
            await events.emit(conv_id, "tool_end", {
                "tool": "run_review", "icon": "search-check",
                "status": f"⛔ Review cancelled by user during {_cancel_phase}",
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
        print(f"[REVIEWER] review phase failed ({_cancel_phase}): {e}")
    finally:
        if _ticker:
            _ticker.cancel()
            try:
                await _ticker
            except asyncio.CancelledError:
                pass

    parsed = _try_parse_review_json(review_text)
    if not parsed or "status" not in parsed:
        # Heuristic fallback when the model didn't emit clean JSON.
        await _step("parse_fallback", "model output was not valid JSON")
        _any_fail = (build_result["exit_code"] != 0
                     or test_result["exit_code"] != 0
                     or lint_result["exit_code"] != 0)
        _fb_issues = []

        # Extract FAILED/ERROR lines from test output for actionable summaries
        # instead of using raw failure_text[:200] which grabs random traceback chunks.
        _FAIL_LINE_RE = re.compile(
            r"^(?:FAILED|ERROR)\s+([\w./\-]+\.py)(?:::([\w.\[\]<>:_\-]+))?(?:\s*-\s*(.{0,200}))?",
            re.MULTILINE,
        )

        if build_result["exit_code"] != 0:
            _fb_issues.append({
                "severity": "compile",
                "file": refs[0]["file"] if refs else "",
                "lines": [refs[0]["line"]] if refs and refs[0]["line"] else [],
                "summary": (build_result.get("stdout", "")[-500:].strip()[:200] or "build failed"),
                "suggested_fix_scope": [r["file"] for r in refs[:3]],
            })
        if test_result["exit_code"] != 0:
            _test_out = test_result.get("stdout", "")
            _fail_matches = _FAIL_LINE_RE.findall(_test_out)
            if _fail_matches:
                _by_file: dict[str, list[str]] = {}
                for _fm_file, _fm_test, _fm_reason in _fail_matches:
                    _by_file.setdefault(_fm_file, []).append(
                        f"{_fm_test}: {_fm_reason}" if _fm_reason else _fm_test
                    )
                for _fm_file, _fm_descs in list(_by_file.items())[:3]:
                    _fb_issues.append({
                        "severity": "test",
                        "file": _fm_file,
                        "lines": [],
                        "summary": f"{len(_fm_descs)} failing test(s): " + "; ".join(_fm_descs[:3])[:200],
                        "suggested_fix_scope": [_fm_file],
                    })
            else:
                _fb_issues.append({
                    "severity": "test",
                    "file": refs[0]["file"] if refs else "",
                    "lines": [refs[0]["line"]] if refs and refs[0]["line"] else [],
                    "summary": (_test_out[-500:].strip()[:200] or "tests failed"),
                    "suggested_fix_scope": [r["file"] for r in refs[:3]],
                })
        if lint_result["exit_code"] != 0:
            _lint_refs = _extract_file_refs(lint_result.get("stdout", ""))
            _fb_issues.append({
                "severity": "lint",
                "file": _lint_refs[0]["file"] if _lint_refs else "",
                "lines": [_lint_refs[0]["line"]] if _lint_refs and _lint_refs[0]["line"] else [],
                "summary": (lint_result.get("stdout", "")[-500:].strip()[:200] or "lint failed"),
                "suggested_fix_scope": [r["file"] for r in _lint_refs[:3]],
            })
        parsed = {
            "status": "issues" if _any_fail else "clean",
            "summary": "Reviewer could not parse model output cleanly; reporting raw build/test/lint exits.",
            "issues": _fb_issues,
        }

    parsed = _sanitize_review_envelope(parsed, failure_text, project_files, refs)

    envelope = {
        **parsed,
        "build_cmd": build_cmd, "test_cmd": test_cmd, "lint_cmd": lint_cmd,
        "build_exit": build_result["exit_code"], "test_exit": test_result["exit_code"],
        "lint_exit": lint_result["exit_code"],
        "build_stdout_tail": (build_result.get("stdout", "") or "")[-3000:],
        "test_stdout_tail": (test_result.get("stdout", "") or "")[-5000:],
        "lint_stdout_tail": (lint_result.get("stdout", "") or "")[-2000:],
        "language": language, "marker": marker,
        "verification_level": verification_level,
        "verification_profile": profile,
        "verification_confidence": confidence,
        "review_model": review_model,
        "raw_review_chars": len(review_text),
        "project_dir": project_dir,
        "run_id": run_id,
    }

    final_status = "succeeded"  # The Reviewer itself succeeded; the project state is what envelope `status` reports.
    n_issues = len(envelope.get("issues") or [])
    await events.emit(conv_id, "tool_end", {
        "tool": "run_review", "icon": "search-check",
        "status": (f"✅ Review clean — {verification_level} checks pass" if envelope.get("status") == "clean"
                   else f"⚠ Review found {n_issues} issue{'s' if n_issues != 1 else ''}"),
        "run_id": run_id,
    })
    if run_id:
        try:
            await db.update_run(run_id, status=final_status,
                                result_envelope=envelope, ended=True)
        except Exception:
            pass
        cancel_registry.cleanup(run_id)

    return envelope
