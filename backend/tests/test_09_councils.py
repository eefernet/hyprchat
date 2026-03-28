"""
Tests for Council CRUD, members, presets, and suggestions.
"""
import pytest


class TestCouncilCRUD:
    def test_create_council(self, client):
        r = client.post("/api/councils", json={
            "name": "Temp Council",
            "host_model": "qwen3.5:4b",
            "host_system_prompt": "Test host"
        })
        assert r.status_code == 200
        council = r.json()
        assert "id" in council
        assert council["name"] == "Temp Council"
        client.delete(f"/api/councils/{council['id']}")

    def test_list_councils(self, client, created_council):
        r = client.get("/api/councils")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        ids = [c["id"] for c in data]
        assert created_council["id"] in ids

    def test_get_council(self, client, created_council):
        cid = created_council["id"]
        r = client.get(f"/api/councils/{cid}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == cid

    def test_update_council(self, client, created_council):
        cid = created_council["id"]
        r = client.patch(f"/api/councils/{cid}", json={
            "name": "Updated Council",
            "debate_rounds": 2
        })
        assert r.status_code == 200

    def test_delete_nonexistent_council(self, client):
        r = client.delete("/api/councils/council-nonexistent999")
        assert r.status_code in (200, 404)


class TestCouncilMembers:
    def test_add_member(self, client, created_council):
        cid = created_council["id"]
        r = client.post(f"/api/councils/{cid}/members", json={
            "model": "qwen3.5:4b",
            "system_prompt": "You are test member 1.",
            "persona_name": "Test Member"
        })
        assert r.status_code == 200
        member = r.json()
        assert "id" in member

    def test_update_member(self, client, created_council):
        cid = created_council["id"]
        # Get council to find members
        council = client.get(f"/api/councils/{cid}").json()
        members = council.get("members", [])
        if not members:
            pytest.skip("No members to update")
        mid = members[0]["id"]
        r = client.patch(f"/api/councils/members/{mid}", json={
            "persona_name": "Updated Member"
        })
        assert r.status_code == 200

    def test_delete_member(self, client, created_council):
        cid = created_council["id"]
        # Add a member to delete
        r = client.post(f"/api/councils/{cid}/members", json={
            "model": "qwen3.5:4b",
            "persona_name": "Disposable Member"
        })
        if r.status_code != 200:
            pytest.skip("Could not create member")
        mid = r.json()["id"]
        r2 = client.delete(f"/api/councils/members/{mid}")
        assert r2.status_code == 200


class TestCouncilPresets:
    def test_list_presets(self, client):
        r = client.get("/api/council-presets")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) > 0
        preset_names = [p.get("name", p.get("id", "")) for p in data]
        # Should have some of the standard presets
        assert len(preset_names) >= 1

    def test_seed_preset(self, client):
        # Get available presets first
        presets = client.get("/api/council-presets").json()
        if not presets:
            pytest.skip("No presets available")
        preset_id = presets[0].get("id", presets[0].get("name", "")).lower().replace(" ", "-")
        r = client.post(f"/api/seed/council-preset/{preset_id}")
        assert r.status_code == 200
        data = r.json()
        assert "id" in data or "council_id" in data or "name" in data
        # Cleanup
        council_id = data.get("id") or data.get("council_id")
        if council_id:
            client.delete(f"/api/councils/{council_id}")


class TestCouncilSuggestions:
    def test_suggestions_with_members(self, long_client, created_council):
        """Suggestions require members — add one first."""
        cid = created_council["id"]
        # Ensure at least one member
        long_client.post(f"/api/councils/{cid}/members", json={
            "model": "qwen3.5:4b",
            "persona_name": "Suggestions Test Member"
        })
        r = long_client.get(f"/api/councils/{cid}/suggestions")
        assert r.status_code == 200
        data = r.json()
        assert "suggestions" in data
        assert isinstance(data["suggestions"], list)
        # Should return up to 3 suggestions
        assert len(data["suggestions"]) <= 3

    def test_suggestions_nonexistent_council(self, client):
        r = client.get("/api/councils/council-nonexistent999/suggestions")
        assert r.status_code == 404


class TestCouncilAnalytics:
    def test_analyze_council(self, client, created_council):
        cid = created_council["id"]
        r = client.get(f"/api/councils/{cid}/analyze")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
