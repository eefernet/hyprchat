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
