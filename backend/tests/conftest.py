"""
HyprChat Test Suite — shared fixtures and configuration.

Usage:
    pytest tests/ -v                    # run all tests
    pytest tests/ -v -k "health"        # run only health tests
    pytest tests/ -v --tb=short         # shorter tracebacks

Set HYPRCHAT_URL env var to test against a different server:
    HYPRCHAT_URL=http://192.168.1.120:8000 pytest tests/ -v
"""
import os
import pytest
import httpx

BASE_URL = os.getenv("HYPRCHAT_URL", "http://192.168.1.120:8000")


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def client():
    """Shared httpx client for the entire test session."""
    with httpx.Client(base_url=BASE_URL, timeout=30.0, verify=False) as c:
        yield c


@pytest.fixture(scope="session")
def long_client():
    """Client with longer timeout for AI/workflow operations."""
    with httpx.Client(base_url=BASE_URL, timeout=300.0, verify=False) as c:
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


@pytest.fixture(scope="session")
def created_workflow(client):
    """Create a simple workflow once for the session, clean up after."""
    r = client.post("/api/workflows", json={
        "name": "Test Workflow",
        "description": "Simple test workflow",
        "steps": [
            {
                "name": "Echo Input",
                "type": "tool",
                "tool": "execute_code",
                "args": {"code": "print('Hello from workflow: {{input}}')", "language": "python"},
                "output_var": "result"
            }
        ]
    })
    assert r.status_code == 200
    wf = r.json()
    yield wf
    client.delete(f"/api/workflows/{wf['id']}")
