"""
HyprChat Test Suite — shared fixtures and configuration.

Usage:
    pytest tests/ -v                    # run all tests
    pytest tests/ -v -k "health"        # run only health tests
    pytest tests/ -v --tb=short         # shorter tracebacks

Set HYPRCHAT_URL env var to test against a different server:
    HYPRCHAT_URL=http://127.0.0.1:8000 pytest tests/ -v
"""
import os
import pytest
import httpx

from .optional_deps import HAS_AIOSQLITE, HAS_CHROMADB, HAS_FASTAPI  # noqa: F401

BASE_URL = os.getenv("HYPRCHAT_URL", "http://127.0.0.1:8000")


def pytest_configure(config):
    """Live browser dependencies belong on candidate storage, not LXC tmpfs."""
    root = os.getenv('DAEDALUS_TEST_ROOT')
    if os.getenv('DAEDALUS_LIVE_ENV') == '1' and root and not config.option.basetemp:
        from pathlib import Path
        import uuid
        root = Path(root).resolve()
        if not (root / 'codex-candidate').is_file():
            raise pytest.UsageError('Live dependency tests require a marked isolated candidate release')
        directory = root / 'test-runs' / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=True)
        import json
        from evals.engine_campaign import collect_evaluator_caches
        receipts = collect_evaluator_caches(root)
        (root / 'test-runs' / (directory.name + '-preparation.json')).write_text(json.dumps(receipts, indent=2) + '\n')
        config.option.basetemp = str(directory)
        config._daedalus_fixture_root = directory


def pytest_sessionfinish(session, exitstatus):
    directory = getattr(session.config, '_daedalus_fixture_root', None)
    if directory is None or not directory.exists():
        return
    import json
    from coder_worker_runtime import WorkerStore
    from coder_engine_cache import collect
    reclaimed = []
    for database in directory.rglob('operations.sqlite3'):
        store = WorkerStore(database.parent)
        removed = collect(store, limit_bytes=0)
        if removed:
            reclaimed.append({'store': str(database.parent.relative_to(directory)), 'removed': removed})
    (directory / 'cache-reclamation.json').write_text(json.dumps(reclaimed, indent=2) + '\n')


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


def _ensure_server_available(client):
    """Skip live endpoint tests cleanly when no HyprChat server is running."""
    try:
        client.get("/", timeout=2.0)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        pytest.skip(f"HyprChat test server is not reachable at {BASE_URL}: {e}")


@pytest.fixture(scope="session")
def client():
    """Shared httpx client for the entire test session."""
    with httpx.Client(base_url=BASE_URL, timeout=30.0, verify=False) as c:
        _ensure_server_available(c)
        yield c


@pytest.fixture(scope="session")
def long_client():
    """Client with longer timeout for AI operations."""
    with httpx.Client(base_url=BASE_URL, timeout=300.0, verify=False) as c:
        _ensure_server_available(c)
        yield c


# ── Reusable resource factories ──────────────────────────────────────────

@pytest.fixture(scope="session")
def created_conversation(client):
    """Create a conversation once for the session, clean up after."""
    r = client.post("/api/conversations", json={"title": "Test Conv"})
    assert r.status_code == 200
    conv = r.json()
    yield conv
    client.delete(f"/api/conversations/{conv['id']}")


@pytest.fixture(scope="session")
def created_kb(client):
    """Create a knowledge base once for the session, clean up after."""
    r = client.post("/api/knowledge-bases", json={"name": "Test KB", "description": "For testing"})
    assert r.status_code == 200
    kb = r.json()
    yield kb
    client.delete(f"/api/knowledge-bases/{kb['id']}")


@pytest.fixture(scope="session")
def created_tool(client):
    """Create a custom tool once for the session, clean up after."""
    r = client.post("/api/tools", json={
        "name": "test_tool",
        "description": "A test tool",
        "filename": "test_tool.py",
        "code": "def run(args):\n    return 'hello from test tool'"
    })
    assert r.status_code == 200
    tool = r.json()
    yield tool
    client.delete(f"/api/tools/{tool['id']}")


@pytest.fixture(scope="session")
def created_persona(client):
    """Create a persona/model-config once for the session, clean up after."""
    r = client.post("/api/model-configs", json={
        "name": "Test Persona",
        "base_model": "qwen3.5:4b",
        "system_prompt": "You are a test persona.",
        "parameters": {"temperature": 0.7}
    })
    assert r.status_code == 200
    mc = r.json()
    yield mc
    client.delete(f"/api/model-configs/{mc['id']}")


@pytest.fixture(scope="session")
def created_workspace(client, created_conversation):
    """Create a workspace once for the session, clean up after."""
    r = client.post("/api/workspaces", json={
        "name": "Test Workspace",
        "description": "For testing"
    })
    assert r.status_code == 200
    ws = r.json()
    yield ws
    client.delete(f"/api/workspaces/{ws['id']}")


@pytest.fixture(scope="session")
def created_council(client):
    """Create a council once for the session, clean up after."""
    r = client.post("/api/councils", json={
        "name": "Test Council",
        "host_model": "qwen3.5:4b",
        "host_system_prompt": "You are a test council host."
    })
    assert r.status_code == 200
    council = r.json()
    yield council
    client.delete(f"/api/councils/{council['id']}")
