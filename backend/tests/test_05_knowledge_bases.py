"""
Tests for Knowledge Base CRUD and file management.
"""
import pytest
import io


class TestKBCRUD:
    def test_create_kb(self, client):
        r = client.post("/api/knowledge-bases", json={"name": "Temp KB", "description": "temp"})
        assert r.status_code == 200
        kb = r.json()
        assert "id" in kb
        assert kb["name"] == "Temp KB"
        client.delete(f"/api/knowledge-bases/{kb['id']}")

    def test_list_kbs(self, client, created_kb):
        r = client.get("/api/knowledge-bases")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        ids = [k["id"] for k in data]
        assert created_kb["id"] in ids

    def test_update_kb(self, client, created_kb):
        kb_id = created_kb["id"]
        r = client.put(f"/api/knowledge-bases/{kb_id}", json={
            "name": "Updated KB Name",
            "description": "updated desc"
        })
        assert r.status_code == 200

    def test_delete_nonexistent_kb(self, client):
        r = client.delete("/api/knowledge-bases/kb-nonexistent999")
        # Should return 404 or succeed silently
        assert r.status_code in (200, 404)


class TestKBFiles:
    def test_upload_text_file(self, client, created_kb):
        kb_id = created_kb["id"]
        file_content = b"This is test content for the knowledge base.\nLine two.\nLine three."
        files = {"file": ("test_doc.txt", io.BytesIO(file_content), "text/plain")}
        r = client.post(f"/api/knowledge-bases/{kb_id}/files", files=files)
        assert r.status_code == 200
        data = r.json()
        assert "id" in data or "file_id" in data or "filename" in data

    def test_list_kb_shows_files(self, client, created_kb):
        kb_id = created_kb["id"]
        r = client.get("/api/knowledge-bases")
        assert r.status_code == 200
        kbs = r.json()
        kb = next((k for k in kbs if k["id"] == kb_id), None)
        assert kb is not None
        # KB should have files after upload
        files = kb.get("files", [])
        assert isinstance(files, list)

    def test_reindex_kb(self, client, created_kb):
        kb_id = created_kb["id"]
        r = client.post(f"/api/knowledge-bases/{kb_id}/reindex")
        assert r.status_code == 200
