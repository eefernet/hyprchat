"""
Language adapters for Coder Bot workflows.

The adapters intentionally return conservative commands. They are used to
describe uploaded projects, seed workflow contracts, and decide which commands
are safe to hand to Aider's --test-cmd / --lint-cmd flags.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import os
import shlex
from collections import Counter


@dataclass
class LanguageAdapter:
    language: str
    build_system: str = "generic"
    build_cmd: str = ""
    test_cmd: str = ""
    smoke_cmds: list[str] | None = None
    package_rules: list[str] | None = None
    source_extensions: list[str] | None = None
    ignored_dirs: list[str] | None = None
    aider_test_cmd: str = ""
    aider_lint_cmd: str = ""
    safe_lint: bool = False
    isolated_verification: dict | None = None

    def to_contract(self) -> dict:
        data = asdict(self)
        for key in ("smoke_cmds", "package_rules", "source_extensions", "ignored_dirs"):
            data[key] = data.get(key) or []
        data["isolated_verification"] = data.get("isolated_verification") or {
            "applicable": False,
            "required_for_delivery": False,
            "setup_cmd": "",
            "verify_cmds": [],
            "runtime_smoke_cmds": [],
            "cleanup_paths": [],
        }
        return data


COMMON_IGNORES = [
    ".git", "__pycache__", ".cache", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "dist", "build", "target", ".next",
    "venv", ".venv", ".tox",
]

# Host-level package-manager config redirectors. Any of these set in the
# Codebox service environment (systemd Environment=, pip.conf companions,
# shell profiles) silently changes what a project build installs — or breaks
# it outright, as PIP_CONSTRAINT pointing at a missing file did. Project
# verification must see the project, not the host. Mirrored in
# openhands_worker.py (_HERMETIC_UNSET_VARS) — keep both lists in sync; the
# worker deploys standalone to Codebox and cannot import this module.
HERMETIC_UNSET_VARS = [
    "PIP_CONSTRAINT",
    "PIP_CONFIG_FILE",
    "PIP_REQUIRE_VIRTUALENV",
    "NPM_CONFIG_USERCONFIG",
    "NPM_CONFIG_GLOBALCONFIG",
    "GOFLAGS",
]


def hermetic_prefix() -> str:
    """Shell prefix that strips host-level package-manager config vars."""
    return "unset " + " ".join(HERMETIC_UNSET_VARS) + "; "


# Per-project virtualenv, created by the build command below and used for all
# build/test/smoke phases. Relative because every execution path cds into the
# project root first. The old shared /root/venv remains only for the generic
# chat run_python/run_shell scratch tools (tooling/codebox_tools.py).
CODEBOX_PYTHON = "./.venv/bin/python3"

# Idempotent guard: create the project venv if missing. Bare python3 is used
# only for creation — never for installs (externally-managed system Python).
PY_VENV_GUARD = "test -x .venv/bin/python3 || python3 -m venv .venv; "

# Full bootstrap for build phases: guard + verification tooling (pytest,
# flake8) inside the project venv so test/lint never depend on host packages.
PY_VENV_BOOTSTRAP = (
    PY_VENV_GUARD
    + f"{CODEBOX_PYTHON} -m pip install -q -U pip pytest flake8"
)


NODE_RUNTIME_SMOKE_CMD = (
    "if [ ! -f package.json ]; then "
    "echo '(node runtime smoke skipped: no package.json)'; exit 0; fi; "
    "kind=$(node -e \"const fs=require('fs'); "
    "const p=JSON.parse(fs.readFileSync('package.json','utf8')); "
    "const bin=p.bin; let bp=''; "
    "if (typeof bin === 'string') bp=bin; "
    "else if (bin && typeof bin === 'object') bp=Object.values(bin)[0] || ''; "
    "if (bp) console.log('binpath:' + bp); "
    "else if (p.scripts && p.scripts.start) console.log('start'); "
    "else console.log('skip');\") || exit $?; "
    "case \"$kind\" in "
    "binpath:*) binpath=${kind#binpath:}; "
    "if [ ! -f \"$binpath\" ]; then echo \"declared bin target missing: $binpath\"; exit 1; fi; "
    "timeout 15 node \"$binpath\" --help; rc=$?; "
    "if [ \"$rc\" = 124 ]; then echo '(node bin smoke stayed up; treating timeout as started-ok)'; exit 0; fi; "
    "exit $rc ;; "
    "start) timeout 15 npm start -- --help; rc=$?; "
    "if [ \"$rc\" = 124 ]; then echo '(node start stayed up; treating timeout as started-ok)'; exit 0; fi; "
    "exit $rc ;; "
    "*) echo '(node runtime smoke skipped: no start script or bin)'; exit 0 ;; "
    "esac"
)


def _bounded_rust_runtime_smoke(run_cmd: str) -> str:
    return (
        f"timeout 15 {run_cmd}; rc=$?; "
        "if [ \"$rc\" = 124 ]; then "
        "echo '(cargo run stayed up; treating timeout as started-ok)'; exit 0; fi; "
        "exit $rc"
    )


def _bounded_entrypoint_smoke(run_cmd: str, timeout_s: int, label: str) -> str:
    """Bounded 'actually run the program' smoke shared by the adapters.

    Exit contract: 0 = ran and exited cleanly; timeout (124) = still running
    when the window closed, which for games/servers/loops means it started
    fine. Programs that genuinely need a user (stdin CLIs, X11 GUIs) are
    exempted by output signature so a headless sandbox doesn't flag them.
    Anything else is a startup crash the compile-level phases can't see —
    e.g. a cross-file constructor mismatch that shipped as ACCEPTED once.
    SDL dummy drivers let pygame/SDL programs run without a display.
    """
    return (
        "out=$(mktemp); "
        "export SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy; "
        f"timeout {timeout_s} {run_cmd} </dev/null >\"$out\" 2>&1; rc=$?; "
        "tail -n 25 \"$out\"; "
        f"if [ \"$rc\" = 124 ]; then echo '({label} stayed up {timeout_s}s; started-ok)'; "
        "rm -f \"$out\"; exit 0; fi; "
        "if [ \"$rc\" = 0 ]; then rm -f \"$out\"; exit 0; fi; "
        "if grep -qE 'EOFError|cannot open display|not connect to display|no display name' \"$out\"; then "
        f"echo '({label} needs an interactive/display environment; treating as started-ok)'; "
        "rm -f \"$out\"; exit 0; fi; "
        "rm -f \"$out\"; exit $rc"
    )


# Top-level script names that count as a runnable entrypoint for flat
# (non-packaged) projects, in priority order.
PY_ENTRYPOINT_FILES = ("main.py", "app.py", "game.py", "run.py")
NODE_ENTRYPOINT_FILES = ("index.js", "main.js", "app.js", "server.js")


def _first_top_level(manifest: list[str], candidates: tuple[str, ...]) -> str:
    return next((c for c in candidates if _top_level_has(manifest, c)), "")


def _rust_runtime_smoke_cmd(manifest: list[str]) -> str:
    if _has(manifest, "src/main.rs"):
        return _bounded_rust_runtime_smoke("cargo run -- --help")
    bin_paths = sorted(
        p for p in manifest
        if p.startswith("src/bin/") and p.endswith(".rs")
    )
    if bin_paths:
        bin_name = os.path.splitext(os.path.basename(bin_paths[0]))[0]
        return _bounded_rust_runtime_smoke(
            f"cargo run --bin {shlex.quote(bin_name)} -- --help"
        )
    return ""


def _isolated_copy_cmd() -> str:
    """Copy the project into a temporary clean work tree.

    Reviewer substitutes `{tmp}` with a run-scoped /tmp path before execution.
    The copy deliberately excludes dependency/build/cache outputs so isolated
    verification proves the declared project files can recreate what it needs.

    `rm -rf -- {tmp}` (not `rm -rf {tmp}`): codebox-api's /command deny-list
    substring-matches "rm -rf /" and 400s ANY absolute-path rm without the
    POSIX `--` end-of-options marker. A bare `rm -rf /tmp/...` here blocked
    every isolated verification run with "Blocked dangerous command pattern".
    """
    return (
        "rm -rf -- {tmp} && mkdir -p {tmp}/work && "
        "tar cf - "
        "--exclude=.git --exclude='*/.git/*' "
        "--exclude=__pycache__ --exclude='*/__pycache__/*' "
        "--exclude=.pytest_cache --exclude='*/.pytest_cache/*' "
        "--exclude=.mypy_cache --exclude='*/.mypy_cache/*' "
        "--exclude=.ruff_cache --exclude='*/.ruff_cache/*' "
        "--exclude=.cache --exclude='*/.cache/*' "
        "--exclude=.venv --exclude='*/.venv/*' "
        "--exclude=venv --exclude='*/venv/*' "
        "--exclude=node_modules --exclude='*/node_modules/*' "
        "--exclude=dist --exclude='*/dist/*' "
        "--exclude=build --exclude='*/build/*' "
        "--exclude=target --exclude='*/target/*' "
        "--exclude='.aider*' --exclude='*/.aider*' "
        "--exclude='*.egg-info' --exclude='*.egg-info/*' "
        "--exclude='*.pyc' "
        ". | (cd {tmp}/work && tar xf -)"
    )


def _isolated_contract(*, applicable: bool, setup_cmd: str = "",
                       verify_cmds: list[str] | None = None,
                       runtime_smoke_cmds: list[str] | None = None,
                       cleanup_paths: list[str] | None = None,
                       required_for_delivery: bool | None = None,
                       reason: str = "") -> dict:
    required = applicable if required_for_delivery is None else required_for_delivery
    return {
        "applicable": bool(applicable),
        "required_for_delivery": bool(required),
        "setup_cmd": setup_cmd if applicable else "",
        "verify_cmds": verify_cmds or [],
        "runtime_smoke_cmds": runtime_smoke_cmds or [],
        "cleanup_paths": cleanup_paths or (["{tmp}"] if applicable else []),
        "reason": reason,
    }


EXT_TO_LANGUAGE = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".html": "html",
    ".css": "html",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "java",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".csproj": "csharp",
    ".sln": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".sh": "shell",
}


def _has(manifest: list[str], name: str) -> bool:
    return any(p == name or p.endswith("/" + name) for p in manifest)


def _top_level_has(manifest: list[str], name: str) -> bool:
    return name in {p.split("/", 1)[0] for p in manifest}


def _python_packages(manifest: list[str]) -> list[str]:
    packages = []
    for path in manifest:
        if not path.endswith("__init__.py"):
            continue
        parts = path.split("/")
        if len(parts) == 2:
            packages.append(parts[0])
        elif len(parts) == 3 and parts[0] == "src":
            packages.append(parts[1])
    seen = set()
    out = []
    for pkg in packages:
        if pkg and pkg not in seen and pkg not in {"tests", "test"}:
            seen.add(pkg)
            out.append(pkg)
    return out


def _detect_language(manifest: list[str], language_hint: str = "") -> str:
    hint = (language_hint or "").strip().lower()
    if hint and hint != "unknown":
        if hint in {"js", "node"}:
            return "javascript"
        if hint in {"ts", "tsx"}:
            return "typescript"
        return hint
    counts = Counter()
    for path in manifest:
        ext = os.path.splitext(path)[1].lower()
        lang = EXT_TO_LANGUAGE.get(ext)
        if lang:
            counts[lang] += 1
    return counts.most_common(1)[0][0] if counts else "generic"


def python_adapter(manifest: list[str]) -> LanguageAdapter:
    has_pyproject = _top_level_has(manifest, "pyproject.toml")
    has_requirements = _top_level_has(manifest, "requirements.txt")
    has_tests = any(
        p.startswith(("tests/", "test/"))
        or os.path.basename(p).startswith("test_")
        or os.path.basename(p).endswith("_test.py")
        for p in manifest
    )
    packages = _python_packages(manifest)
    py_compile = (
        f"{CODEBOX_PYTHON} -m py_compile "
        "$(find . -name '*.py' -not -path '*/.venv/*' -not -path '*/venv/*' "
        "-not -path '*/.git/*' -not -path '*/__pycache__/*')"
    )
    build_cmd = py_compile
    if has_pyproject:
        build_system = "pyproject.toml"
        build_cmd = f"{CODEBOX_PYTHON} -m pip install -e . && " + build_cmd
        isolated_install = "{tmp}/venv/bin/python -m pip install -q -e ."
    elif has_requirements:
        build_system = "requirements.txt"
        build_cmd = f"{CODEBOX_PYTHON} -m pip install -r requirements.txt && " + build_cmd
        isolated_install = "{tmp}/venv/bin/python -m pip install -q -r requirements.txt"
    else:
        build_system = "plain-python"
        isolated_install = ""
    build_cmd = PY_VENV_BOOTSTRAP + " && " + build_cmd

    test_cmd = f"{PY_VENV_GUARD}{CODEBOX_PYTHON} -m pytest -q" if has_tests else ""
    # Aider runs its test/lint commands standalone — possibly before any build
    # phase has created ./.venv — so they must self-bootstrap. The pytest
    # install is conditional to avoid a pip round-trip on every Aider round.
    aider_pytest_cmd = (
        PY_VENV_GUARD
        + f"{CODEBOX_PYTHON} -m pytest --version >/dev/null 2>&1"
        + f" || {CODEBOX_PYTHON} -m pip install -q pytest; "
        + f"{CODEBOX_PYTHON} -m pytest -q"
    ) if has_tests else ""
    smoke_cmds = []
    for pkg in packages[:3]:
        if any(p == f"{pkg}/__main__.py" or p == f"src/{pkg}/__main__.py" for p in manifest):
            smoke_cmds.append(f"{CODEBOX_PYTHON} -m {pkg} --help")
    if has_pyproject and packages and not smoke_cmds:
        smoke_cmds.append(f"{CODEBOX_PYTHON} -c \"import {packages[0]}; print({packages[0]}.__name__)\"")
    entry_script = _first_top_level(manifest, PY_ENTRYPOINT_FILES)
    if entry_script and not smoke_cmds:
        # Flat script projects (main.py + requirements.txt) previously got NO
        # smoke at all — py_compile happily shipped a game whose constructors
        # didn't match their call sites. Actually run the entrypoint, bounded.
        smoke_cmds.append(_bounded_entrypoint_smoke(
            f"{CODEBOX_PYTHON} {entry_script}", 10, f"python {entry_script}"))

    isolated_applicable = has_pyproject or has_requirements
    isolated_setup = ""
    isolated_verify = []
    isolated_runtime = []
    if isolated_applicable:
        isolated_setup = (
            _isolated_copy_cmd()
            + " && python3 -m venv {tmp}/venv"
            + " && ({tmp}/venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1 || true)"
            + " && cd {tmp}/work && "
            + isolated_install
        )
        isolated_verify = [
            "cd {tmp}/work && {tmp}/venv/bin/python -m pip check",
            (
                "cd {tmp}/work && {tmp}/venv/bin/python -m py_compile "
                "$(find . -name '*.py' -not -path '*/.venv/*' -not -path '*/venv/*' "
                "-not -path '*/.git/*' -not -path '*/__pycache__/*')"
            ),
        ]
        if packages:
            isolated_verify.append(
                f"cd {{tmp}}/work && {{tmp}}/venv/bin/python -c \"import {packages[0]}; print({packages[0]}.__name__)\""
            )
        isolated_runtime = [(
            "if grep -R -E --include='*.py' --include='pyproject.toml' "
            "--include='requirements*.txt' "
            "'import pygame|from pygame|pygame-ce|pygame[<>=~! ]' {tmp}/work "
            ">/dev/null 2>&1; then "
            "cd {tmp}/work && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy "
            "{tmp}/venv/bin/python -c \"import pygame, pygame.font; "
            "pygame.init(); pygame.font.init(); pygame.display.set_mode((1, 1)); "
            "pygame.font.Font(None, 24); pygame.quit(); print('pygame smoke ok')\"; "
            "else echo '(pygame smoke skipped)'; fi"
        )]
        if entry_script:
            # The check above only proves the pygame LIBRARY works — it never
            # executes the project. Run the real entrypoint in the clean env.
            isolated_runtime.append(
                "cd {tmp}/work && " + _bounded_entrypoint_smoke(
                    "{tmp}/venv/bin/python " + entry_script, 10,
                    f"python {entry_script}"))

    return LanguageAdapter(
        language="python",
        build_system=build_system,
        build_cmd=build_cmd,
        test_cmd=test_cmd,
        smoke_cmds=smoke_cmds,
        package_rules=[
            "CLI packages should expose either console_scripts in pyproject.toml or a package __main__.py.",
            "Tests must isolate DB/filesystem state and not depend on prior local runs.",
            "When documentation promises python -m <package>, include a smoke command for it.",
        ],
        source_extensions=[".py"],
        ignored_dirs=COMMON_IGNORES,
        aider_test_cmd=aider_pytest_cmd,
        aider_lint_cmd=build_cmd if not has_pyproject and not has_requirements else PY_VENV_GUARD + py_compile,
        safe_lint=True,
        isolated_verification=_isolated_contract(
            applicable=isolated_applicable,
            setup_cmd=isolated_setup,
            verify_cmds=isolated_verify,
            runtime_smoke_cmds=isolated_runtime,
            reason="fresh Python environment from declared manifest",
        ),
    )


def node_adapter(manifest: list[str], language: str) -> LanguageAdapter:
    has_pkg = _top_level_has(manifest, "package.json")
    is_ts = language == "typescript" or any(p.endswith((".ts", ".tsx")) for p in manifest)
    build_cmd = "npm install --silent && npm run build --if-present" if has_pkg else ""
    test_cmd = "npm test --if-present" if has_pkg else ""
    lint_cmd = "npm run lint --if-present" if has_pkg else ""
    # `find -exec node --check {} \;` always exits 0 even on syntax errors —
    # accumulate failures explicitly like html_adapter's js_lint does.
    plain_node_check = (
        "fail=0; for f in $(find . -name '*.js' -not -path '*/node_modules/*'); do "
        "node --check \"$f\" || fail=1; done; exit $fail"
    )
    isolated = _isolated_contract(
        applicable=has_pkg,
        setup_cmd=(
            _isolated_copy_cmd()
            + " && cd {tmp}/work && "
            "if [ -f package-lock.json ] || [ -f npm-shrinkwrap.json ]; then "
            "npm ci --cache {tmp}/npm-cache --prefer-offline --no-audit --silent; "
            "else npm install --cache {tmp}/npm-cache --prefer-offline --no-audit --silent; fi"
        ) if has_pkg else "",
        verify_cmds=([
            "cd {tmp}/work && npm run build --if-present",
            "cd {tmp}/work && npm test --if-present",
        ] if has_pkg else []),
        runtime_smoke_cmds=([
            f"cd {{tmp}}/work && {NODE_RUNTIME_SMOKE_CMD}"
        ] if has_pkg else []),
        reason="fresh Node dependency install from package manifest",
    )
    plain_node_entry = "" if has_pkg else _first_top_level(manifest, NODE_ENTRYPOINT_FILES)
    return LanguageAdapter(
        language="typescript" if is_ts else "javascript",
        build_system="package.json" if has_pkg else "plain-node",
        build_cmd=build_cmd or plain_node_check,
        test_cmd=test_cmd,
        smoke_cmds=([NODE_RUNTIME_SMOKE_CMD] if has_pkg
                    else [_bounded_entrypoint_smoke(
                        f"node {plain_node_entry}", 10, f"node {plain_node_entry}")]
                    if plain_node_entry else []),
        package_rules=["Do not package node_modules, dist, build, .next, or cache directories."],
        source_extensions=[".ts", ".tsx", ".js", ".jsx"] if is_ts else [".js", ".jsx"],
        ignored_dirs=COMMON_IGNORES,
        aider_test_cmd=test_cmd,
        aider_lint_cmd=lint_cmd,
        safe_lint=bool(lint_cmd),
        isolated_verification=isolated,
    )


def html_adapter(manifest: list[str]) -> LanguageAdapter:
    has_index = _top_level_has(manifest, "index.html")
    js_lint = (
        "if command -v node >/dev/null 2>&1; then "
        "fail=0; for f in $(find . -name '*.js' -not -path '*/node_modules/*'); do "
        "node --check \"$f\" || fail=1; done; exit $fail; "
        "else echo '(node unavailable; JS syntax check skipped)'; fi"
    )
    return LanguageAdapter(
        language="html",
        build_system="static-html" if has_index else "plain-html",
        build_cmd=("test -s index.html && grep -Eiq '<!doctype|<html' index.html"
                   if has_index else
                   "find . -name '*.html' -print | head -1 | grep -q ."),
        test_cmd="",
        smoke_cmds=[],
        package_rules=[
            "Static browser apps may run by opening index.html directly.",
            "README run instructions must not require npm, a server, or a CLI unless the project includes that tooling.",
            "Do not package browser cache, build, dist, or dependency directories.",
        ],
        source_extensions=[".html", ".css", ".js", ".jsx", ".svg"],
        ignored_dirs=COMMON_IGNORES,
        aider_test_cmd="",
        aider_lint_cmd=js_lint,
        safe_lint=True,
        isolated_verification=_isolated_contract(
            applicable=False,
            required_for_delivery=False,
            reason="static HTML has no dependency environment",
        ),
    )


def rust_adapter(manifest: list[str]) -> LanguageAdapter:
    has_cargo = _top_level_has(manifest, "Cargo.toml")
    runtime_smoke_cmd = _rust_runtime_smoke_cmd(manifest)
    return LanguageAdapter(
        language="rust",
        build_system="Cargo.toml" if has_cargo else "plain-rust",
        build_cmd="cargo build --quiet" if has_cargo else "",
        test_cmd="cargo test --quiet" if has_cargo else "",
        smoke_cmds=[runtime_smoke_cmd] if has_cargo and runtime_smoke_cmd else [],
        package_rules=["Do not package target/."],
        source_extensions=[".rs"],
        ignored_dirs=COMMON_IGNORES,
        aider_test_cmd="cargo test --quiet" if has_cargo else "",
        aider_lint_cmd="cargo clippy --quiet" if has_cargo else "",
        safe_lint=bool(has_cargo),
        isolated_verification=_isolated_contract(
            applicable=has_cargo,
            setup_cmd=_isolated_copy_cmd() if has_cargo else "",
            verify_cmds=([
                "cd {tmp}/work && CARGO_TARGET_DIR={tmp}/target cargo build --quiet",
                "cd {tmp}/work && CARGO_TARGET_DIR={tmp}/target cargo test --quiet",
            ] if has_cargo else []),
            runtime_smoke_cmds=([
                f"cd {{tmp}}/work && CARGO_TARGET_DIR={{tmp}}/target {runtime_smoke_cmd}"
            ] if has_cargo and runtime_smoke_cmd else []),
            reason="isolated Cargo target directory",
        ),
    )


def go_adapter(manifest: list[str]) -> LanguageAdapter:
    has_mod = _top_level_has(manifest, "go.mod")
    go_run_target = ""
    if has_mod:
        if _top_level_has(manifest, "main.go"):
            go_run_target = "."
        else:
            cmd_mains = sorted(
                p for p in manifest
                if p.startswith("cmd/") and p.endswith("/main.go"))
            if cmd_mains:
                go_run_target = "./" + os.path.dirname(cmd_mains[0])
    return LanguageAdapter(
        language="go",
        build_system="go.mod" if has_mod else "plain-go",
        build_cmd="go build ./..." if has_mod else "",
        test_cmd="go test ./..." if has_mod else "",
        smoke_cmds=([_bounded_entrypoint_smoke(
            f"go run {go_run_target}", 15, "go run")] if go_run_target else []),
        package_rules=["Do not package bin/, dist/, or coverage outputs."],
        source_extensions=[".go"],
        ignored_dirs=COMMON_IGNORES,
        aider_test_cmd="go test ./..." if has_mod else "",
        aider_lint_cmd="go vet ./..." if has_mod else "",
        safe_lint=has_mod,
        isolated_verification=_isolated_contract(
            applicable=has_mod,
            setup_cmd=_isolated_copy_cmd() if has_mod else "",
            verify_cmds=([
                "cd {tmp}/work && GOMODCACHE={tmp}/gomodcache GOCACHE={tmp}/gocache go test ./...",
                "cd {tmp}/work && GOMODCACHE={tmp}/gomodcache GOCACHE={tmp}/gocache go build ./...",
            ] if has_mod else []),
            reason="isolated Go module/build caches",
        ),
    )


def java_adapter(manifest: list[str]) -> LanguageAdapter:
    has_maven = _top_level_has(manifest, "pom.xml")
    has_gradle = _top_level_has(manifest, "build.gradle") or _top_level_has(manifest, "build.gradle.kts")
    java_smoke = ""
    if has_maven:
        build_cmd = "mvn -q -DskipTests compile"
        test_cmd = "mvn -q test"
        build_system = "pom.xml"
        isolated_verify = [
            "cd {tmp}/work && mvn -q -Dmaven.repo.local={tmp}/m2 -DskipTests compile",
            "cd {tmp}/work && mvn -q -Dmaven.repo.local={tmp}/m2 test",
        ]
        # Self-gated: only runnable when the project wired the exec plugin.
        java_smoke = (
            "if grep -q exec-maven-plugin pom.xml 2>/dev/null; then "
            + _bounded_entrypoint_smoke("mvn -q exec:java", 30, "mvn exec:java")
            + "; else echo '(java run smoke skipped: no exec-maven-plugin)'; fi"
        )
    elif has_gradle:
        build_cmd = "./gradlew build -q -x test || gradle build -q -x test"
        test_cmd = "./gradlew test -q || gradle test -q"
        build_system = "gradle"
        isolated_verify = [
            "cd {tmp}/work && export GRADLE_USER_HOME={tmp}/gradle && (./gradlew build -q -x test || gradle build -q -x test)",
            "cd {tmp}/work && export GRADLE_USER_HOME={tmp}/gradle && (./gradlew test -q || gradle test -q)",
        ]
        # Self-gated: `run` exists only with the application plugin.
        java_smoke = (
            "if grep -q application build.gradle build.gradle.kts 2>/dev/null; then "
            + _bounded_entrypoint_smoke(
                "sh -c './gradlew run -q || gradle run -q'", 30, "gradle run")
            + "; else echo '(java run smoke skipped: no application plugin)'; fi"
        )
    else:
        build_cmd = "rm -rf out && mkdir -p out && find . -name '*.java' -not -path '*/out/*' -not -path '*/build/*' -not -path '*/target/*' | xargs -r javac -d out"
        test_cmd = ""
        build_system = "plain-java"
        isolated_verify = []
    return LanguageAdapter(
        language="java",
        build_system=build_system,
        build_cmd=build_cmd,
        test_cmd=test_cmd,
        smoke_cmds=[java_smoke] if java_smoke else [],
        package_rules=["Do not package target/, build/, out/, or .gradle/."],
        source_extensions=[".java", ".kt"],
        ignored_dirs=COMMON_IGNORES + [".gradle", "out"],
        aider_test_cmd=test_cmd,
        aider_lint_cmd="",
        safe_lint=False,
        isolated_verification=_isolated_contract(
            applicable=has_maven or has_gradle,
            setup_cmd=_isolated_copy_cmd() if (has_maven or has_gradle) else "",
            verify_cmds=isolated_verify,
            reason="isolated Java build cache/repository",
        ),
    )


def c_cpp_adapter(manifest: list[str], language: str) -> LanguageAdapter:
    is_cpp = language == "cpp" or any(p.endswith((".cpp", ".cc", ".cxx", ".hpp")) for p in manifest)
    has_cmake = _top_level_has(manifest, "CMakeLists.txt")
    has_make = _top_level_has(manifest, "Makefile")
    if has_cmake:
        build_cmd = "cmake -B build -S . && cmake --build build --quiet"
        test_cmd = "cd build && ctest --output-on-failure"
        build_system = "cmake"
    elif has_make:
        build_cmd = "make -s 2>&1 | head -200"
        test_cmd = ("if grep -qE '^test[[:space:]:]' Makefile; then make -s test; "
                    "else echo '(no make test target)'; fi")
        build_system = "make"
    elif is_cpp:
        build_cmd = "g++ -Wall -fsyntax-only $(find . -name '*.cpp' -o -name '*.cc' -o -name '*.cxx')"
        test_cmd = ""
        build_system = "plain-cpp"
    else:
        build_cmd = "gcc -Wall -fsyntax-only $(find . -name '*.c')"
        test_cmd = ""
        build_system = "plain-c"
    c_smoke = ""
    if has_cmake:
        # Run the first executable the cmake build produced (self-gated).
        c_smoke = (
            "bin=$(find build -maxdepth 3 -type f -perm -u+x "
            "! -name '*.so*' ! -name '*.a' ! -name '*.cmake' 2>/dev/null | head -1); "
            "if [ -n \"$bin\" ]; then "
            + _bounded_entrypoint_smoke("\"$bin\"", 10, "built binary")
            + "; else echo '(run smoke skipped: no built binary found)'; fi"
        )
    elif has_make:
        # Only artifacts newer than the Makefile count as build outputs.
        c_smoke = (
            "bin=$(find . -maxdepth 2 -type f -perm -u+x -newer Makefile "
            "! -name '*.sh' ! -name '*.py' ! -name '*.so*' ! -name 'configure' "
            "! -path './.git/*' 2>/dev/null | head -1); "
            "if [ -n \"$bin\" ]; then "
            + _bounded_entrypoint_smoke("\"$bin\"", 10, "built binary")
            + "; else echo '(run smoke skipped: no built binary found)'; fi"
        )
    return LanguageAdapter(
        language="cpp" if is_cpp else "c",
        build_system=build_system,
        build_cmd=build_cmd,
        test_cmd=test_cmd,
        smoke_cmds=[c_smoke] if c_smoke else [],
        package_rules=["Do not package build/, dist/, out/, coverage, or compiler outputs."],
        source_extensions=[".cpp", ".cc", ".cxx", ".hpp", ".h"] if is_cpp else [".c", ".h"],
        ignored_dirs=COMMON_IGNORES + ["out"],
        aider_test_cmd=test_cmd,
        # Only the pure -fsyntax-only sweeps are safe as a lint; cmake and make
        # both mutate the tree (build dirs / arbitrary recipes).
        aider_lint_cmd=build_cmd if not (has_cmake or has_make) else "",
        safe_lint=not (has_cmake or has_make),
        isolated_verification=_isolated_contract(
            applicable=has_cmake,
            setup_cmd=_isolated_copy_cmd() if has_cmake else "",
            verify_cmds=([
                "cd {tmp}/work && cmake -B {tmp}/cmake-build -S .",
                "cd {tmp}/work && cmake --build {tmp}/cmake-build --quiet",
                "cd {tmp}/cmake-build && ctest --output-on-failure",
            ] if has_cmake else []),
            reason="out-of-tree CMake build",
        ),
    )


# .NET SDK lives at /root/.dotnet on Codebox; service shells may not have it
# on PATH, so every dotnet command exports it first (mirrors reviewer.py).
_DOTNET_ENV = ('export PATH="$PATH:/root/.dotnet:$HOME/.dotnet" && '
               'export DOTNET_CLI_TELEMETRY_OPTOUT=1')


def csharp_adapter(manifest: list[str]) -> LanguageAdapter:
    has_tests = any(
        p.endswith(".csproj") and "test" in p.lower() for p in manifest
    )
    build_cmd = f"{_DOTNET_ENV} && dotnet build -nologo -v q"
    test_cmd = f"{_DOTNET_ENV} && dotnet test -nologo -v q" if has_tests else ""
    app_csproj = next(
        (p for p in manifest if p.endswith(".csproj") and "test" not in p.lower()),
        "")
    cs_smoke = ""
    if app_csproj:
        cs_smoke = _bounded_entrypoint_smoke(
            f"sh -c '{_DOTNET_ENV} && dotnet run --project \"{app_csproj}\"'",
            30, "dotnet run")
    return LanguageAdapter(
        language="csharp",
        build_system="dotnet",
        build_cmd=build_cmd,
        test_cmd=test_cmd,
        smoke_cmds=[cs_smoke] if cs_smoke else [],
        package_rules=["Do not package bin/, obj/, or NuGet cache directories."],
        source_extensions=[".cs", ".csproj", ".sln"],
        ignored_dirs=COMMON_IGNORES + ["bin", "obj"],
        aider_test_cmd=test_cmd,
        aider_lint_cmd=build_cmd,
        safe_lint=True,
        isolated_verification=_isolated_contract(
            applicable=True,
            setup_cmd=_isolated_copy_cmd(),
            verify_cmds=[
                f"cd {{tmp}}/work && {_DOTNET_ENV} && dotnet build -nologo -v q"
            ] + ([f"cd {{tmp}}/work && {_DOTNET_ENV} && dotnet test -nologo -v q"]
                 if has_tests else []),
            reason="isolated dotnet build (bin/obj kept out of the delivered tree)",
        ),
    )


def generic_adapter(manifest: list[str], language: str = "generic") -> LanguageAdapter:
    return LanguageAdapter(
        language=language or "generic",
        build_system="generic",
        build_cmd="",
        test_cmd="",
        smoke_cmds=[],
        package_rules=["Exclude generated dependency, build, cache, and VCS directories from downloads."],
        source_extensions=[],
        ignored_dirs=COMMON_IGNORES,
        aider_test_cmd="",
        aider_lint_cmd="",
        safe_lint=False,
        isolated_verification=_isolated_contract(
            applicable=False,
            required_for_delivery=False,
            reason="no known dependency environment",
        ),
    )


def detect_adapter(manifest: list[str], language_hint: str = "") -> LanguageAdapter:
    manifest = manifest or []
    language = _detect_language(manifest, language_hint)
    hint = (language_hint or "").strip().lower()
    # Marker fallbacks match TOP-LEVEL files only: a Flask app's
    # templates/index.html or a JS monorepo's nested backend/requirements.txt
    # must not hijack routing away from the hint/count-detected language.
    if language == "html" or (not hint and _top_level_has(manifest, "index.html")):
        return html_adapter(manifest)
    if language == "python" or _top_level_has(manifest, "pyproject.toml") or _top_level_has(manifest, "requirements.txt"):
        return python_adapter(manifest)
    if language in {"javascript", "typescript"} or _top_level_has(manifest, "package.json"):
        return node_adapter(manifest, language)
    if language == "rust" or _top_level_has(manifest, "Cargo.toml"):
        return rust_adapter(manifest)
    if language == "go" or _top_level_has(manifest, "go.mod"):
        return go_adapter(manifest)
    if language in {"java", "kotlin"} or _top_level_has(manifest, "pom.xml") or _top_level_has(manifest, "build.gradle") or _top_level_has(manifest, "build.gradle.kts"):
        return java_adapter(manifest)
    if language in {"csharp", "c#", "cs", "dotnet"} or any(
            p.endswith((".csproj", ".sln")) for p in manifest):
        return csharp_adapter(manifest)
    if (language in {"c", "cpp"} or _top_level_has(manifest, "CMakeLists.txt")
            or (_top_level_has(manifest, "Makefile")
                and any(p.endswith((".c", ".cpp", ".cc", ".cxx")) for p in manifest))):
        return c_cpp_adapter(manifest, language)
    return generic_adapter(manifest, language)


def detect_contract(manifest: list[str], language_hint: str = "") -> dict:
    return detect_adapter(manifest, language_hint).to_contract()
