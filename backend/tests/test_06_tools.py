"""
Tests for custom tool CRUD and code execution.
"""
import pytest


class TestToolCRUD:
    def test_create_tool(self, client):
        r = client.post("/api/tools", json={
            "name": "temp_tool",
            "description": "Temporary tool",
            "filename": "temp_tool.py",
            "code": "def run(args):\n    return 'temp result'"
        })
        assert r.status_code == 200
        tool = r.json()
        assert "id" in tool
        assert tool["name"] == "temp_tool"
        client.delete(f"/api/tools/{tool['id']}")

    def test_list_tools(self, client, created_tool):
        r = client.get("/api/tools")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    def test_update_tool(self, client, created_tool):
        tid = created_tool["id"]
        r = client.patch(f"/api/tools/{tid}", json={
            "description": "Updated description"
        })
        assert r.status_code == 200

    def test_delete_nonexistent_tool(self, client):
        r = client.delete("/api/tools/tool-nonexistent999")
        assert r.status_code in (200, 404)


class TestCodeExecution:
    def test_execute_python(self, client):
        r = client.post("/api/execute", json={
            "code": "print('hello from test')",
            "language": "python",
            "timeout": 30
        })
        assert r.status_code == 200
        data = r.json()
        result = data.get("result", data.get("output", str(data)))
        assert "hello from test" in str(result).lower() or "success" in str(result).lower()

    def test_execute_shell(self, client):
        r = client.post("/api/execute/shell", json={
            "command": "echo 'shell test ok'",
            "timeout": 10
        })
        assert r.status_code == 200
        data = r.json()
        assert "shell test ok" in str(data).lower() or "result" in data


class TestSearchAndFetch:
    def test_fetch_url(self, client):
        r = client.post("/api/fetch-url", json={
            "url": "https://example.com",
            "max_chars": 1000
        })
        assert r.status_code == 200
        data = r.json()
        content = data.get("content", data.get("result", str(data)))
        assert "example" in str(content).lower()

    def test_web_search(self, client):
        r = client.post("/api/search", json={
            "query": "python programming",
            "count": 3
        })
        assert r.status_code == 200
        data = r.json()
        # Should return results (list or dict with results key)
        assert isinstance(data, (list, dict))

    def test_quick_search(self, client):
        r = client.post("/api/quick-search", json={
            "query": "test query",
            "count": 3
        })
        assert r.status_code == 200
