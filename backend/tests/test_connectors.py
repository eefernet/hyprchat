import asyncio
import os
import sys
import types

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
try:
    import aiosqlite  # noqa: F401
except ModuleNotFoundError:
    sys.modules.setdefault("database", types.SimpleNamespace())

import connectors


def test_mcp_tool_to_connector_generates_safe_function_name():
    server = {"id": "mcp-test", "name": "Test MCP", "transport": "stdio"}
    tool = {
        "name": "repo.list-issues",
        "description": "List issues",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
    }

    converted = connectors.mcp_tool_to_connector(server, tool)

    assert converted["id"].startswith("mcp:mcp-test:")
    assert converted["tool_name"].startswith("mcp_mcp_test_repo_list_issues")
    assert converted["input_schema"]["required"] == ["repo"]


def test_openapi_operation_to_connector_maps_parameters_and_body():
    connector = {"id": "openapi-test", "name": "Demo"}
    spec = {"openapi": "3.1.0"}
    op = {
        "operationId": "updateItem",
        "parameters": [
            {"name": "item_id", "in": "path", "schema": {"type": "string"}, "required": True},
            {"name": "verbose", "in": "query", "schema": {"type": "boolean"}},
        ],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"type": "object", "properties": {"name": {"type": "string"}}}}},
        },
    }

    converted = connectors.openapi_operation_to_connector(connector, spec, "/items/{item_id}", "patch", op, [])

    props = converted["input_schema"]["properties"]
    assert converted["id"].startswith("openapi:openapi-test:")
    assert props["item_id"]["type"] == "string"
    assert props["verbose"]["type"] == "boolean"
    assert "body" in props
    assert set(converted["input_schema"]["required"]) == {"item_id", "body"}


def test_private_openapi_url_is_blocked():
    with pytest.raises(connectors.ConnectorError):
        connectors.assert_url_allowed("http://127.0.0.1:8000/openapi.json", allow_private=False)


def test_plain_env_placeholder_resolution(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret-value")
    expanded = connectors.expand_placeholders({"GITHUB_TOKEN": "GITHUB_TOKEN"}, plain_names=True)
    assert expanded == {"GITHUB_TOKEN": "secret-value"}


def test_execute_openapi_tool_builds_request(monkeypatch):
    connector = {
        "id": "openapi-test",
        "name": "Demo",
        "base_url": "https://api.example.test",
        "auth": {},
        "headers": {"X-Test": "yes"},
        "enabled": True,
        "allow_private": False,
    }
    spec = {"openapi": "3.1.0"}
    tool = connectors.openapi_operation_to_connector(
        connector,
        spec,
        "/items/{item_id}",
        "post",
        {
            "operationId": "createItem",
            "parameters": [
                {"name": "item_id", "in": "path", "schema": {"type": "string"}, "required": True},
                {"name": "verbose", "in": "query", "schema": {"type": "boolean"}},
            ],
            "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
        },
        [],
    )

    async def fake_get_openapi_connector(connector_id):
        assert connector_id == "openapi-test"
        return connector

    monkeypatch.setattr(connectors.db, "get_openapi_connector", fake_get_openapi_connector, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://api.example.test/items/abc?verbose=true"
        assert request.headers["X-Test"] == "yes"
        assert request.content == b'{"name":"demo"}'
        return httpx.Response(200, json={"ok": True})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await connectors.execute_openapi_tool(
                client,
                tool,
                {"item_id": "abc", "verbose": True, "body": {"name": "demo"}},
            )

    result = asyncio.run(run())
    assert '"status_code": 200' in result
    assert '"ok": true' in result
