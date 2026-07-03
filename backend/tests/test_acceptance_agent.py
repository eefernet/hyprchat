"""
Focused unit tests for the Coder Bot v2 Acceptance agent.

These mock Codebox, Ollama, and the run database. No network or live HyprChat
service is required.
"""
import asyncio
import importlib.util
import json
import sys
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch


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
    get_coding_project=_db_stub,
    get_coding_project_by_conv=_db_stub,
)
_existing_database = sys.modules.get("database")
sys.modules["database"] = _database_stub
_spec = importlib.util.spec_from_file_location("acceptance_under_test", _AGENTS / "acceptance.py")
acceptance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acceptance)
if _existing_database is None:
    sys.modules.pop("database", None)
else:
    sys.modules["database"] = _existing_database


def _run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload




class _OllamaStreamShim:
    """Adapts a post()-style fake to complete_chat's streaming Ollama path:
    wraps the fake's JSON body into a single NDJSON chunk."""

    def __init__(self, fake, url, json_payload, timeout):
        self.fake = fake
        self.url = url
        self.json_payload = json_payload
        self.timeout = timeout

    async def __aenter__(self):
        import json as _json
        resp = await self.fake.post(self.url, json=self.json_payload, timeout=self.timeout)
        body = resp.json() if resp.status_code == 200 else {}

        class _SResp:
            status_code = resp.status_code

            async def aiter_lines(_s):
                msg = body.get("message") if isinstance(body.get("message"), dict) else {}
                yield _json.dumps({"message": msg, "thinking": body.get("thinking"), "done": True})

        return _SResp()

    async def __aexit__(self, *a):
        return False

class _FakeHTTP:
    def __init__(self, ollama_payload):
        self.ollama_payload = ollama_payload
        self.posts = []

    def stream(self, method, url, json=None, timeout=None):
        return _OllamaStreamShim(self, url, json, timeout)

    async def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        if url.endswith("/api/chat"):
            return _FakeResponse(self.ollama_payload)

        command = (json or {}).get("command", "")
        if "find . -maxdepth 6" in command:
            stdout = "\n".join([
                "./README.md",
                "./pyproject.toml",
                "./app.py",
                "./tests/test_app.py",
            ])
        elif "find ." in command:
            stdout = ""
        elif "README.md" in command:
            stdout = "# Demo\n\nRun with python app.py\n"
        elif "pyproject.toml" in command:
            stdout = "[project]\nname = \"demo\"\n"
        elif "tests/test_app.py" in command:
            stdout = "def test_demo():\n    assert True\n"
        elif "app.py" in command:
            stdout = "print('demo')\n"
        else:
            stdout = ""
        return _FakeResponse({"exit_code": 0, "stdout": stdout, "stderr": ""})


class _FakeEvents:
    def __init__(self):
        self.events = []

    async def emit(self, conv_id, event_type, data):
        self.events.append((conv_id, event_type, data))


def _accepted_json():
    return json.dumps({
        "status": "accepted",
        "summary": "Project satisfies the request.",
        "issues": [],
    })


def _run_acceptance_with_ollama_payload(payload):
    http = _FakeHTTP(payload)
    events = _FakeEvents()
    clean_review = {
        "status": "clean",
        "summary": "Build, tests, and lint pass.",
        "project_dir": "/root/projects/demo",
        "build_cmd": "python -m py_compile app.py",
        "test_cmd": "pytest",
        "lint_cmd": "",
        "language": "python",
    }

    with ExitStack() as stack:
        stack.enter_context(patch.object(acceptance.db, "create_run", new=AsyncMock(return_value=None)))
        stack.enter_context(patch.object(acceptance.db, "update_run", new=AsyncMock(return_value=None)))
        stack.enter_context(patch.object(acceptance.db, "get_run", new=AsyncMock(return_value={
            "result_envelope": clean_review,
        })))
        stack.enter_context(patch.object(acceptance.db, "get_conversation", new=AsyncMock(return_value={
            "messages": [{"role": "user", "content": "Build a tiny Python demo."}],
        })))
        stack.enter_context(patch.object(acceptance, "_project_plan", new=AsyncMock(return_value="Tiny Python demo plan.")))
        stack.enter_context(patch.object(acceptance.config, "CODEBOX_URL", "http://codebox"))
        stack.enter_context(patch.object(acceptance.config, "OLLAMA_URL", "http://ollama"))
        stack.enter_context(patch.object(acceptance.config, "ACCEPTANCE_MODEL", ""))
        stack.enter_context(patch.object(acceptance.config, "PLANNING_MODEL", "acceptance-test-model"))
        stack.enter_context(patch.object(acceptance.config, "DEFAULT_MODEL", "fallback-model"))
        stack.enter_context(patch.object(acceptance.config, "DEFAULT_NUM_CTX", 8192))

        envelope = _run(acceptance.run_acceptance_review(
            http, events, "conv-1",
            project_dir="/root/projects/demo",
            reviewer_run_id="run-review",
        ))

    return envelope, http, events


def test_acceptance_parses_message_thinking_fallback():
    envelope, http, _events = _run_acceptance_with_ollama_payload({
        "message": {"content": "", "thinking": _accepted_json()},
    })

    assert envelope["status"] == "accepted"
    ollama_call = [p for p in http.posts if p["url"].endswith("/api/chat")][0]
    assert ollama_call["json"]["think"] is False


def test_acceptance_parses_top_level_thinking_fallback():
    envelope, _http, _events = _run_acceptance_with_ollama_payload({
        "message": {"content": ""},
        "thinking": _accepted_json(),
    })

    assert envelope["status"] == "accepted"
    assert envelope["issues"] == []


def test_acceptance_invalid_json_returns_error():
    envelope, _http, _events = _run_acceptance_with_ollama_payload({
        "message": {"content": "not valid json"},
    })

    assert envelope["status"] == "error"
    assert envelope["summary"] == "Acceptance model did not return valid JSON."
    assert envelope["issues"][0]["summary"] == "Acceptance reviewer output could not be parsed."


def test_acceptance_generic_exception_persists_failed():
    """An Ollama transport error must not strand the run at 'running'."""

    class _ExplodingHTTP(_FakeHTTP):
        async def post(self, url, json=None, timeout=None):
            if url.endswith("/api/chat"):
                raise RuntimeError("simulated ollama timeout")
            return await super().post(url, json=json, timeout=timeout)

    http = _ExplodingHTTP({})
    events = _FakeEvents()
    clean_review = {
        "status": "clean",
        "summary": "Build, tests, and lint pass.",
        "project_dir": "/root/projects/demo",
        "language": "python",
    }
    update_run = AsyncMock(return_value=None)

    with ExitStack() as stack:
        stack.enter_context(patch.object(acceptance.db, "create_run", new=AsyncMock(return_value=None)))
        stack.enter_context(patch.object(acceptance.db, "update_run", new=update_run))
        stack.enter_context(patch.object(acceptance.db, "get_run", new=AsyncMock(return_value={
            "result_envelope": clean_review,
        })))
        stack.enter_context(patch.object(acceptance.db, "get_conversation", new=AsyncMock(return_value={
            "messages": [{"role": "user", "content": "Build a tiny Python demo."}],
        })))
        stack.enter_context(patch.object(acceptance, "_project_plan", new=AsyncMock(return_value="plan")))
        stack.enter_context(patch.object(acceptance.config, "CODEBOX_URL", "http://codebox"))
        stack.enter_context(patch.object(acceptance.config, "OLLAMA_URL", "http://ollama"))
        stack.enter_context(patch.object(acceptance.config, "ACCEPTANCE_MODEL", ""))
        stack.enter_context(patch.object(acceptance.config, "PLANNING_MODEL", "acceptance-test-model"))
        stack.enter_context(patch.object(acceptance.config, "DEFAULT_MODEL", "fallback-model"))
        stack.enter_context(patch.object(acceptance.config, "DEFAULT_NUM_CTX", 8192))

        envelope = _run(acceptance.run_acceptance_review(
            http, events, "conv-1",
            project_dir="/root/projects/demo",
            reviewer_run_id="run-review",
        ))

    assert envelope["status"] == "error"
    assert "simulated ollama timeout" in envelope["summary"]
    final_calls = [c for c in update_run.await_args_list if c.kwargs.get("ended")]
    assert final_calls, "run must be finalized"
    assert final_calls[-1].kwargs["status"] == "failed"


def test_section_caps_fit_configured_ctx():
    """Σ(section caps) must fit inside the prompt budget at the default ctx."""
    for ctx in (16384, 65536):
        budgets = acceptance._section_budgets(ctx)
        assert sum(budgets.values()) <= ctx * 3
    # Source budget grows with ctx but never exceeds the absolute ceiling.
    assert (acceptance._section_budgets(262144)["source"]
            == acceptance._SOURCE_SECTION_CAP_MAX)
    # Auto/0 and garbage fall back to the 16384 default.
    assert acceptance._section_budgets(0) == acceptance._section_budgets(16384)
    assert acceptance._section_budgets("x") == acceptance._section_budgets(16384)


def _acceptance_config_patches(stack, *, acceptance_model="", planning_model="",
                               default_model="fallback-model"):
    clean_review = {
        "status": "clean",
        "summary": "Build, tests, and lint pass.",
        "project_dir": "/root/projects/demo",
        "language": "python",
    }
    stack.enter_context(patch.object(acceptance.db, "create_run", new=AsyncMock(return_value=None)))
    stack.enter_context(patch.object(acceptance.db, "update_run", new=AsyncMock(return_value=None)))
    stack.enter_context(patch.object(acceptance.db, "get_run", new=AsyncMock(return_value={
        "result_envelope": clean_review,
    })))
    stack.enter_context(patch.object(acceptance.db, "get_conversation", new=AsyncMock(return_value={
        "messages": [{"role": "user", "content": "Build a tiny Python demo."}],
    })))
    stack.enter_context(patch.object(acceptance, "_project_plan", new=AsyncMock(return_value="plan")))
    stack.enter_context(patch.object(acceptance.config, "CODEBOX_URL", "http://codebox"))
    stack.enter_context(patch.object(acceptance.config, "OLLAMA_URL", "http://ollama"))
    stack.enter_context(patch.object(acceptance.config, "ACCEPTANCE_MODEL", acceptance_model))
    stack.enter_context(patch.object(acceptance.config, "PLANNING_MODEL", planning_model))
    stack.enter_context(patch.object(acceptance.config, "DEFAULT_MODEL", default_model))
    stack.enter_context(patch.object(acceptance.config, "DEFAULT_NUM_CTX", 8192))


def test_cloud_chat_model_is_not_inherited_by_acceptance():
    """A cloud conversation model must NOT silently power the agent — the
    chain falls through to the non-cloud default."""
    http = _FakeHTTP({"message": {"content": _accepted_json()}})
    with ExitStack() as stack:
        _acceptance_config_patches(stack)
        envelope = _run(acceptance.run_acceptance_review(
            http, _FakeEvents(), "conv-1",
            project_dir="/root/projects/demo",
            reviewer_run_id="run-review",
            conv_model="anthropic:claude-test-model",
        ))

    assert envelope["status"] == "accepted"
    ollama_call = [p for p in http.posts if p["url"].endswith("/api/chat")][0]
    assert ollama_call["json"]["model"] == "fallback-model"


def test_explicit_cloud_acceptance_model_routes_to_provider():
    """An explicitly-configured cloud model uses the provider stream, not Ollama."""
    async def fake_stream(_http, model_id, _messages, options=None):
        assert model_id == "anthropic:claude-test-model"
        for part in ('{"status": "accepted", ', '"summary": "ok", "issues": []}'):
            yield {"type": "token", "content": part}

    http = _FakeHTTP({"message": {"content": "should not be used"}})
    with ExitStack() as stack:
        _acceptance_config_patches(stack, acceptance_model="anthropic:claude-test-model")
        stack.enter_context(patch.object(
            acceptance.model_providers, "stream_provider_chat", new=fake_stream))
        envelope = _run(acceptance.run_acceptance_review(
            http, _FakeEvents(), "conv-1",
            project_dir="/root/projects/demo",
            reviewer_run_id="run-review",
        ))

    assert envelope["status"] == "accepted"
    assert not [p for p in http.posts if p["url"].endswith("/api/chat")]


def test_acceptance_clean_synonym_normalizes_to_accepted():
    """Local models emit 'clean' (the Reviewer schema's own passing word) —
    it must map to accepted, not hard-downgrade to error and block delivery."""
    envelope, _http, _events = _run_acceptance_with_ollama_payload({
        "message": {"content": json.dumps({
            "status": "clean", "summary": "Looks good.", "issues": [],
        })},
    })

    assert envelope["status"] == "accepted"
    assert envelope["issues"] == []
