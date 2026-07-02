"""
Focused unit tests for Reviewer project detection.

These cover markerless projects so Daedalus can verify static, GUI, script,
and plain-source deliverables without requiring a package-manager manifest.
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path


_BACKEND = Path(__file__).resolve().parent.parent
_AGENTS = _BACKEND / "agents"
for _p in (_BACKEND, _AGENTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


async def _db_stub(*_args, **_kwargs):
    return None


_database_stub = types.SimpleNamespace(
    create_run=_db_stub,
    update_run=_db_stub,
    get_run=_db_stub,
    get_conversation=_db_stub,
    get_runs_by_conversation=_db_stub,
)
_existing_database = sys.modules.get("database")
sys.modules["database"] = _database_stub
_spec = importlib.util.spec_from_file_location("reviewer_under_test", _AGENTS / "reviewer.py")
reviewer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reviewer)
if _existing_database is None:
    sys.modules.pop("database", None)
else:
    sys.modules["database"] = _existing_database

import language_adapters


def _run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, files, ls_exit=0):
        self.files = files
        self.ls_exit = ls_exit
        self.posts = []

    async def post(self, url, json=None, timeout=None):
        command = (json or {}).get("command", "")
        self.posts.append(command)
        if command.startswith("ls -1 "):
            top_level = sorted({path.split("/", 1)[0] for path in self.files})
            return _FakeResponse({
                "exit_code": self.ls_exit,
                "stdout": "\n".join(top_level) if self.ls_exit == 0 else "",
                "stderr": "" if self.ls_exit == 0 else "ls failed",
            })
        if "-mindepth 2 -maxdepth 2" in command:
            import fnmatch
            hits = []
            for path in self.files:
                parts = path.split("/")
                if len(parts) == 2 and any(
                        fnmatch.fnmatch(parts[1], pat)
                        for pat in reviewer._NESTED_MARKER_NAMES):
                    hits.append(f"./{path}")
            return _FakeResponse({"exit_code": 0, "stdout": "\n".join(sorted(hits)),
                                  "stderr": ""})
        if "sort | uniq -c" in command:
            counts = {}
            for path in self.files:
                if "." not in path.rsplit("/", 1)[-1]:
                    continue
                ext = path.rsplit(".", 1)[-1]
                counts[ext] = counts.get(ext, 0) + 1
            stdout = "\n".join(f"{count} {ext}" for ext, count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0])
            ))
            return _FakeResponse({"exit_code": 0, "stdout": stdout, "stderr": ""})
        if "find . -type f" in command and "head -100" in command:
            return _FakeResponse({"exit_code": 0, "stdout": "\n".join(self.files), "stderr": ""})
        return _FakeResponse({"exit_code": 0, "stdout": "", "stderr": ""})


def test_detects_static_html_without_package_manifest():
    result = _run(reviewer._detect_project(
        _FakeHTTP(["index.html", "README.md"]),
        "/root/projects/rhythm",
    ))

    assert result["marker"] == "index.html"
    assert result["language"] == "html"
    assert result["profile"] == "static-html"
    assert result["verification_level"] == "static-inspected"
    assert "index.html" in result["build_cmd"]


def test_detects_plain_python_from_nested_sources():
    result = _run(reviewer._detect_project(
        _FakeHTTP(["src/main.py", "README.md"]),
        "/root/projects/tool",
    ))

    assert result["language"] == "python"
    assert result["profile"] == "plain-python"
    assert result["verification_level"] == "syntax-verified"
    assert "py_compile" in result["build_cmd"]


def test_nonempty_unknown_project_uses_generic_static_profile():
    result = _run(reviewer._detect_project(
        _FakeHTTP(["README.md", "assets/logo.svg"]),
        "/root/projects/artifact",
    ))

    assert result["language"] == "generic"
    assert result["profile"] == "generic-static"
    assert result["verification_level"] == "static-inspected"
    assert result["build_cmd"]


def test_empty_project_is_error():
    result = _run(reviewer._detect_project(
        _FakeHTTP([]),
        "/root/projects/empty",
    ))

    assert result["profile"] == "empty"
    assert "error" in result


def test_language_adapter_detects_static_html_contract():
    contract = language_adapters.detect_contract(["index.html", "README.md"])

    assert contract["language"] == "html"
    assert contract["build_system"] == "static-html"
    assert "index.html" in contract["build_cmd"]


def test_language_adapter_detects_plain_cpp_contract():
    contract = language_adapters.detect_contract(["main.cpp", "README.md"])

    assert contract["language"] == "cpp"
    assert contract["build_system"] == "plain-cpp"
    assert "g++" in contract["build_cmd"]


# ---------------------------------------------------------------------------
# Infra-failure honesty: transport/phase errors must never yield "clean"
# ---------------------------------------------------------------------------

class _NullEvents:
    async def emit(self, *a, **k):
        pass


class _TransportFailHTTP(_FakeHTTP):
    """Detection works; any actual build/test command (cd …) explodes."""

    async def post(self, url, json=None, timeout=None):
        command = (json or {}).get("command", "")
        if "&& (" in command:
            raise ConnectionError("codebox unreachable")
        return await super().post(url, json=json, timeout=timeout)


def test_reviewer_transport_failure_yields_error_envelope():
    http = _TransportFailHTTP(["src/main.py", "README.md"])

    envelope = _run(reviewer.run_review(
        http, _NullEvents(), "conv-x", "/root/projects/tool",
    ))

    assert envelope["status"] == "error"
    assert "transport" in envelope["summary"].lower() or "Sandbox" in envelope["summary"]
    assert envelope["issues"][0]["severity"] == "infra"


def test_reviewer_phase_exception_never_reports_clean(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(reviewer, "_run_in_sandbox", _boom)
    http = _FakeHTTP(["src/main.py", "README.md"])

    envelope = _run(reviewer.run_review(
        http, _NullEvents(), "conv-x", "/root/projects/tool",
    ))

    assert envelope["status"] == "error"
    assert "kaboom" in envelope["summary"]


def test_transport_failure_detail_sentinels():
    assert reviewer._transport_failure_detail(
        {"exit_code": -1, "stderr": "Exception: boom"}) == "Exception: boom"
    assert reviewer._transport_failure_detail(
        {"exit_code": -1, "stderr": "Codebox HTTP 502: bad gateway"}).startswith("Codebox HTTP")
    # Real build failures (non -1 exit) are not transport failures.
    assert reviewer._transport_failure_detail(
        {"exit_code": 1, "stderr": "SyntaxError"}) == ""
    assert reviewer._transport_failure_detail(
        {"exit_code": -1, "stderr": "(no command)"}) == ""


# ---------------------------------------------------------------------------
# Smoke phase: run the program after clean build/tests, diff runtime state
# ---------------------------------------------------------------------------

_SMOKE_FILES = ["pyproject.toml", "snip/__init__.py", "snip/__main__.py",
                "tests/test_snip.py", "README.md"]


class _SmokeHTTP(_FakeHTTP):
    """Clean build/test/lint; configurable smoke exit + runtime-created files."""

    def __init__(self, files, smoke_exit=0, smoke_stdout="usage: snip", new_files=None):
        super().__init__(files)
        self.smoke_exit = smoke_exit
        self.smoke_stdout = smoke_stdout
        self.new_files = new_files or []
        self.listing_calls = 0

    async def post(self, url, json=None, timeout=None):
        command = (json or {}).get("command", "")
        if "sed 's#^./##'" in command:
            # _list_project_files: runtime files appear after the smoke ran.
            self.listing_calls += 1
            files = list(self.files) + (self.new_files if self.listing_calls > 1 else [])
            return _FakeResponse({"exit_code": 0, "stdout": "\n".join(files), "stderr": ""})
        if "-m snip --help" in command:
            return _FakeResponse({"exit_code": self.smoke_exit,
                                  "stdout": self.smoke_stdout, "stderr": ""})
        return await super().post(url, json=json, timeout=timeout)


def test_smoke_failure_yields_runtime_issue():
    http = _SmokeHTTP(_SMOKE_FILES, smoke_exit=2,
                      smoke_stdout='Traceback...\n  File "snip/__main__.py", line 4\nModuleNotFoundError: No module named snip.cli')

    envelope = _run(reviewer.run_review(
        http, _NullEvents(), "conv-smoke", "/root/projects/snip",
    ))

    assert envelope["status"] == "issues"
    assert envelope["smoke_exit"] == 2
    assert envelope["issues"][0]["severity"] == "runtime"
    assert "-m snip --help" in envelope["issues"][0]["summary"]


def test_smoke_success_records_runtime_state_files():
    http = _SmokeHTTP(_SMOKE_FILES, smoke_exit=0, new_files=["snip.json"])

    envelope = _run(reviewer.run_review(
        http, _NullEvents(), "conv-smoke", "/root/projects/snip",
    ))

    assert envelope["status"] == "clean"
    assert envelope["smoke_exit"] == 0
    assert envelope["smoke_new_files"] == ["snip.json"]
    assert any("-m snip --help" in c for c in envelope["smoke_cmds"])
    assert envelope["isolated_status"] == "passed"
    assert envelope["isolated_required"] is True


class _IsolatedFailHTTP(_SmokeHTTP):
    async def post(self, url, json=None, timeout=None):
        command = (json or {}).get("command", "")
        if "pip check" in command:
            return _FakeResponse({
                "exit_code": 1,
                "stdout": "pygame 2.6.1 has broken font imports",
                "stderr": "",
            })
        return await super().post(url, json=json, timeout=timeout)


def test_isolated_verification_failure_blocks_clean_review():
    http = _IsolatedFailHTTP(_SMOKE_FILES, smoke_exit=0)

    envelope = _run(reviewer.run_review(
        http, _NullEvents(), "conv-smoke", "/root/projects/snip",
    ))

    assert envelope["status"] == "issues"
    assert envelope["isolated_status"] == "failed"
    assert envelope["isolated_exit"] == 1
    assert envelope["issues"][0]["severity"] == "packaging"
    assert "Isolated verification verify phase failed" in envelope["issues"][0]["summary"]


class _AdvisoryIsolatedFailHTTP(_SmokeHTTP):
    async def post(self, url, json=None, timeout=None):
        command = (json or {}).get("command", "")
        if "advisory-isolated-fail" in command:
            return _FakeResponse({
                "exit_code": 1,
                "stdout": "advisory check failed",
                "stderr": "",
            })
        return await super().post(url, json=json, timeout=timeout)


def test_advisory_isolated_verification_failure_does_not_block_clean_review(monkeypatch):
    from agents import language_adapters as agent_language_adapters

    def advisory_contract(_project_files, _language=""):
        return {
            "language": "python",
            "smoke_cmds": [],
            "isolated_verification": {
                "applicable": True,
                "required_for_delivery": False,
                "setup_cmd": "",
                "verify_cmds": ["advisory-isolated-fail"],
                "runtime_smoke_cmds": [],
                "cleanup_paths": [],
                "reason": "advisory test check",
            },
        }

    monkeypatch.setattr(agent_language_adapters, "detect_contract", advisory_contract)
    http = _AdvisoryIsolatedFailHTTP(_SMOKE_FILES, smoke_exit=0)

    envelope = _run(reviewer.run_review(
        http, _NullEvents(), "conv-smoke", "/root/projects/snip",
    ))

    assert envelope["status"] == "clean"
    assert envelope["issues"] == []
    assert envelope["isolated_required"] is False
    assert envelope["isolated_status"] == "failed"
    assert envelope["isolated_exit"] == 1
    assert "advisory check failed" in envelope["isolated_output_tail"]


def test_extract_console_scripts():
    toml = (
        "[project]\nname = \"snip\"\n\n"
        "[project.scripts]\nsnip = \"snip.cli:main\"\nsnip-admin = \"snip.admin:main\"\n\n"
        "[tool.pytest.ini_options]\naddopts = \"-q\"\n"
    )
    assert reviewer._extract_console_scripts(toml) == ["snip", "snip-admin"]
    assert reviewer._extract_console_scripts("[project]\nname='x'\n") == []
    assert reviewer._extract_console_scripts("") == []


class _ConsoleScriptSmokeHTTP(_SmokeHTTP):
    """Adds a pyproject with [project.scripts] and answers the script smoke."""

    async def post(self, url, json=None, timeout=None):
        command = (json or {}).get("command", "")
        if "cat pyproject.toml" in command:
            return _FakeResponse({"exit_code": 0, "stdout": (
                "[project]\nname = \"snip\"\n\n"
                "[project.scripts]\nsnip = \"snip.cli:main\"\n"
            ), "stderr": ""})
        if "/root/venv/bin/snip --help" in command:
            return _FakeResponse({"exit_code": self.smoke_exit,
                                  "stdout": self.smoke_stdout, "stderr": ""})
        return await super().post(url, json=json, timeout=timeout)


def test_console_script_smoke_runs_first_and_catches_runtime_state():
    http = _ConsoleScriptSmokeHTTP(_SMOKE_FILES, smoke_exit=0,
                                   new_files=["snip.json"])

    envelope = _run(reviewer.run_review(
        http, _NullEvents(), "conv-smoke", "/root/projects/snip",
    ))

    assert envelope["status"] == "clean"
    assert envelope["smoke_cmds"][0] == "/root/venv/bin/snip --help"
    assert envelope["smoke_new_files"] == ["snip.json"]


def test_smoke_cleans_up_its_own_runtime_files():
    http = _ConsoleScriptSmokeHTTP(_SMOKE_FILES, smoke_exit=0,
                                   new_files=["snip.json"])

    envelope = _run(reviewer.run_review(
        http, _NullEvents(), "conv-smoke", "/root/projects/snip",
    ))

    assert envelope["smoke_new_files"] == ["snip.json"]
    rm_cmds = [c for c in http.posts if "rm -f --" in c and "snip.json" in c]
    assert rm_cmds, "smoke must remove the runtime files it created"


def test_detects_fullstack_monorepo_compound_contract():
    """backend/pyproject.toml + frontend/package.json has NO top-level marker;
    it must compose a compound contract instead of reviewing as plain python
    (which left the frontend entirely unverified)."""
    result = _run(reviewer._detect_project(
        _FakeHTTP([
            "backend/pyproject.toml", "backend/main.py",
            "frontend/package.json", "frontend/src/App.tsx",
        ]),
        "/root/projects/recipe-manager",
    ))

    assert result["marker"].startswith("nested:")
    assert "backend" in result["build_cmd"] and "pip install" in result["build_cmd"]
    assert "frontend" in result["build_cmd"] and "npm install" in result["build_cmd"]
    # Python side is primary: it owns tests and the reported language.
    assert "backend" in result["test_cmd"]
    assert result["language"] == "python"
    assert len(result["subprojects"]) == 2
    assert result["verification_level"] == "build-verified"


def test_detects_csharp_solution_by_suffix():
    """.sln/.csproj carry the project's own name — exact-name markers can't
    match them, so the suffix pass must produce a dotnet contract."""
    result = _run(reviewer._detect_project(
        _FakeHTTP(["MyApp.sln", "MyApp/MyApp.csproj", "MyApp/Program.cs"]),
        "/root/projects/myapp",
    ))

    assert result["marker"] == "MyApp.sln"
    assert result["language"] == "csharp"
    assert "dotnet build" in result["build_cmd"]
    assert "/root/.dotnet" in result["build_cmd"]
    assert result["verification_level"] == "build-verified"


def test_single_top_level_marker_unchanged_by_nested_scan():
    """A normal single-language project must keep the existing marker path."""
    result = _run(reviewer._detect_project(
        _FakeHTTP(["pyproject.toml", "app/main.py", "tests/test_app.py"]),
        "/root/projects/tool",
    ))

    assert result["marker"] == "pyproject.toml"
    assert result["language"] == "python"
    assert result["profile"] == "marker:pyproject.toml"
