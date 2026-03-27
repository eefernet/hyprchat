"""
Tests for Workspace CRUD and conversation association.
"""
import pytest


class TestWorkspaceCRUD:
    def test_create_workspace(self, client):
        r = client.post("/api/workspaces", json={
            "name": "Temp Workspace",
            "description": "temp"
        })
        assert r.status_code == 200
        ws = r.json()
        assert "id" in ws
        assert ws["name"] == "Temp Workspace"
        client.delete(f"/api/workspaces/{ws['id']}")

    def test_list_workspaces(self, client, created_workspace):
        r = client.get("/api/workspaces")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        ids = [w["id"] for w in data]
        assert created_workspace["id"] in ids

    def test_get_workspace(self, client, created_workspace):
        ws_id = created_workspace["id"]
        r = client.get(f"/api/workspaces/{ws_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == ws_id

    def test_update_workspace(self, client, created_workspace):
        ws_id = created_workspace["id"]
        r = client.patch(f"/api/workspaces/{ws_id}", json={
            "name": "Updated Workspace"
        })
        assert r.status_code == 200

    def test_delete_nonexistent_workspace(self, client):
        r = client.delete("/api/workspaces/ws-nonexistent999")
        assert r.status_code in (200, 404)


class TestWorkspaceConversations:
    def test_add_conversation_to_workspace(self, client, created_workspace, created_conversation):
        ws_id = created_workspace["id"]
        conv_id = created_conversation["id"]
        r = client.post(f"/api/workspaces/{ws_id}/conversations", json={
            "conversation_id": conv_id
        })
        assert r.status_code == 200

    def test_remove_conversation_from_workspace(self, client, created_workspace, created_conversation):
        ws_id = created_workspace["id"]
        conv_id = created_conversation["id"]
        # Add first (idempotent)
        client.post(f"/api/workspaces/{ws_id}/conversations", json={
            "conversation_id": conv_id
        })
        r = client.delete(f"/api/workspaces/{ws_id}/conversations/{conv_id}")
        assert r.status_code == 200
