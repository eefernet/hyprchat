"""
Language adapters for Coder Bot workflows.

The adapters intentionally return conservative commands. They are used to
describe uploaded projects, seed workflow contracts, and decide which commands
are safe to hand to Aider's --test-cmd / --lint-cmd flags.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import os
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

CODEBOX_PYTHON = "/root/venv/bin/python3"


def _isolated_copy_cmd() -> str:
    """Copy the project into a temporary clean work tree.

    Reviewer substitutes `{tmp}` with a run-scoped /tmp path before execution.
    The copy deliberately excludes dependency/build/cache outputs so isolated
    verification proves the declared project files can recreate what it needs.
    """
    return (
        "rm -rf {tmp} && mkdir -p {tmp}/work && "
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

    test_cmd = f"{CODEBOX_PYTHON} -m pytest -q" if has_tests else ""
    smoke_cmds = []
    for pkg in packages[:3]:
        if any(p == f"{pkg}/__main__.py" or p == f"src/{pkg}/__main__.py" for p in manifest):
            smoke_cmds.append(f"{CODEBOX_PYTHON} -m {pkg} --help")
    if has_pyproject and packages and not smoke_cmds:
        smoke_cmds.append(f"{CODEBOX_PYTHON} -c \"import {packages[0]}; print({packages[0]}.__name__)\"")

    isolated_applicable = has_pyproject or has_requirements
    isolated_setup = ""
    isolated_verify = []
    isolated_runtime = []
    if isolated_applicable:
        isolated_setup = (
            _isolated_copy_cmd()
            + f" && {CODEBOX_PYTHON} -m venv {{tmp}}/venv"
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
        aider_test_cmd=test_cmd,
        aider_lint_cmd=build_cmd if not has_pyproject and not has_requirements else py_compile,
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
            "cd {tmp}/work && npm start -- --help"
        ] if has_pkg else []),
        reason="fresh Node dependency install from package manifest",
    )
    return LanguageAdapter(
        language="typescript" if is_ts else "javascript",
        build_system="package.json" if has_pkg else "plain-node",
        build_cmd=build_cmd or plain_node_check,
        test_cmd=test_cmd,
        smoke_cmds=["npm start -- --help"] if has_pkg else [],
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
    return LanguageAdapter(
        language="rust",
        build_system="Cargo.toml" if has_cargo else "plain-rust",
        build_cmd="cargo build --quiet" if has_cargo else "",
        test_cmd="cargo test --quiet" if has_cargo else "",
        smoke_cmds=["cargo run -- --help"] if has_cargo else [],
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
                "cd {tmp}/work && CARGO_TARGET_DIR={tmp}/target cargo run -- --help"
            ] if has_cargo else []),
            reason="isolated Cargo target directory",
        ),
    )


def go_adapter(manifest: list[str]) -> LanguageAdapter:
    has_mod = _top_level_has(manifest, "go.mod")
    return LanguageAdapter(
        language="go",
        build_system="go.mod" if has_mod else "plain-go",
        build_cmd="go build ./..." if has_mod else "",
        test_cmd="go test ./..." if has_mod else "",
        smoke_cmds=[],
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
    if has_maven:
        build_cmd = "mvn -q -DskipTests compile"
        test_cmd = "mvn -q test"
        build_system = "pom.xml"
        isolated_verify = [
            "cd {tmp}/work && mvn -q -Dmaven.repo.local={tmp}/m2 -DskipTests compile",
            "cd {tmp}/work && mvn -q -Dmaven.repo.local={tmp}/m2 test",
        ]
    elif has_gradle:
        build_cmd = "./gradlew build -q -x test || gradle build -q -x test"
        test_cmd = "./gradlew test -q || gradle test -q"
        build_system = "gradle"
        isolated_verify = [
            "cd {tmp}/work && export GRADLE_USER_HOME={tmp}/gradle && (./gradlew build -q -x test || gradle build -q -x test)",
            "cd {tmp}/work && export GRADLE_USER_HOME={tmp}/gradle && (./gradlew test -q || gradle test -q)",
        ]
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
        smoke_cmds=[],
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
    if has_cmake:
        build_cmd = "cmake -B build -S . && cmake --build build --quiet"
        test_cmd = "cd build && ctest --output-on-failure"
        build_system = "cmake"
    elif is_cpp:
        build_cmd = "g++ -Wall -fsyntax-only $(find . -name '*.cpp' -o -name '*.cc' -o -name '*.cxx')"
        test_cmd = ""
        build_system = "plain-cpp"
    else:
        build_cmd = "gcc -Wall -fsyntax-only $(find . -name '*.c')"
        test_cmd = ""
        build_system = "plain-c"
    return LanguageAdapter(
        language="cpp" if is_cpp else "c",
        build_system=build_system,
        build_cmd=build_cmd,
        test_cmd=test_cmd,
        smoke_cmds=[],
        package_rules=["Do not package build/, dist/, out/, coverage, or compiler outputs."],
        source_extensions=[".cpp", ".cc", ".cxx", ".hpp", ".h"] if is_cpp else [".c", ".h"],
        ignored_dirs=COMMON_IGNORES + ["out"],
        aider_test_cmd=test_cmd,
        aider_lint_cmd=build_cmd if not has_cmake else "",
        safe_lint=not has_cmake,
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
    if language == "html" or _has(manifest, "index.html"):
        return html_adapter(manifest)
    if language == "python" or _has(manifest, "pyproject.toml") or _has(manifest, "requirements.txt"):
        return python_adapter(manifest)
    if language in {"javascript", "typescript"} or _has(manifest, "package.json"):
        return node_adapter(manifest, language)
    if language == "rust" or _has(manifest, "Cargo.toml"):
        return rust_adapter(manifest)
    if language == "go" or _has(manifest, "go.mod"):
        return go_adapter(manifest)
    if language in {"java", "kotlin"} or _has(manifest, "pom.xml") or _has(manifest, "build.gradle") or _has(manifest, "build.gradle.kts"):
        return java_adapter(manifest)
    if language in {"c", "cpp"} or _has(manifest, "CMakeLists.txt"):
        return c_cpp_adapter(manifest, language)
    return generic_adapter(manifest, language)


def detect_contract(manifest: list[str], language_hint: str = "") -> dict:
    return detect_adapter(manifest, language_hint).to_contract()
