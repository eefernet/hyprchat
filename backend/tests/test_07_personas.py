"""
Tests for Persona / Model Config CRUD.
"""
import pytest


class TestPersonaCRUD:
    def test_create_persona(self, client):
        r = client.post("/api/model-configs", json={
            "name": "Temp Persona",
            "base_model": "qwen3.5:4b",
            "system_prompt": "You are temporary.",
            "parameters": {"temperature": 0.5}
        })
        assert r.status_code == 200
        mc = r.json()
        assert "id" in mc
        assert mc["name"] == "Temp Persona"
        client.delete(f"/api/model-configs/{mc['id']}")

    def test_list_personas(self, client, created_persona):
        r = client.get("/api/model-configs")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        ids = [m["id"] for m in data]
        assert created_persona["id"] in ids

    def test_get_persona_fields(self, client, created_persona):
        r = client.get("/api/model-configs")
        data = r.json()
        mc = next(m for m in data if m["id"] == created_persona["id"])
        assert mc["name"] == "Test Persona"
        assert mc["base_model"] == "qwen3.5:4b"

    def test_update_persona(self, client, created_persona):
        mc_id = created_persona["id"]
        r = client.patch(f"/api/model-configs/{mc_id}", json={
            "name": "Updated Persona Name"
        })
        assert r.status_code == 200

    def test_update_persona_put(self, client, created_persona):
        mc_id = created_persona["id"]
        r = client.put(f"/api/model-configs/{mc_id}", json={
            "system_prompt": "Updated via PUT"
        })
        assert r.status_code == 200

    def test_delete_nonexistent_persona(self, client):
        r = client.delete("/api/model-configs/mc-nonexistent999")
        assert r.status_code in (200, 404)


class TestSeedPersonas:
    def test_seed_coder_bot(self, client):
        r = client.post("/api/seed/coder-bot")
        assert r.status_code == 200
        data = r.json()
        assert "id" in data or "name" in data or "message" in data

    def test_seed_conspiracy_bot(self, client):
        r = client.post("/api/seed/conspiracy-bot")
        assert r.status_code == 200

    def test_seed_based_bot(self, client):
        r = client.post("/api/seed/based-bot")
        assert r.status_code == 200
