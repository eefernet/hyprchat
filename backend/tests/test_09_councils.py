"""
Tests for Council CRUD, members, presets, and suggestions.
"""
import asyncio
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


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

    def test_member_persona_link_round_trip(self, client, created_council):
        cid = created_council["id"]
        mc = client.post("/api/model-configs", json={
            "name": "Council Linked Persona",
            "base_model": "qwen3.5:4b",
            "system_prompt": "You are the linked council persona.",
            "parameters": {"profile_type": "persona", "persona": {"description": "For council tests."}}
        }).json()
        r = client.post(f"/api/councils/{cid}/members", json={
            "model": mc["base_model"],
            "model_config_id": mc["id"],
            "system_prompt": mc["system_prompt"],
            "persona_name": mc["name"]
        })
        assert r.status_code == 200
        member = r.json()
        assert member["model_config_id"] == mc["id"]

        council = client.get(f"/api/councils/{cid}").json()
        stored = next(m for m in council.get("members", []) if m["id"] == member["id"])
        assert stored["model_config_id"] == mc["id"]

        r = client.patch(f"/api/councils/members/{member['id']}", json={"model_config_id": ""})
        assert r.status_code == 200
        council = client.get(f"/api/councils/{cid}").json()
        stored = next(m for m in council.get("members", []) if m["id"] == member["id"])
        assert stored["model_config_id"] == ""

        client.delete(f"/api/councils/members/{member['id']}")
        client.delete(f"/api/model-configs/{mc['id']}")

    def test_persona_resolution_uses_linked_profile(self, monkeypatch):
        pytest.importorskip("aiosqlite")
        import council as council_mod

        async def fake_get_model_config(mc_id):
            assert mc_id == "mc-linked"
            return {
                "id": "mc-linked",
                "name": "Latest Persona Name",
                "base_model": "qwen3.5:14b",
                "system_prompt": "Latest persona prompt.",
                "parameters": {"profile_type": "persona", "persona": {"description": "Linked."}},
            }

        monkeypatch.setattr(council_mod.db, "get_model_config", fake_get_model_config)
        members = [{
            "id": "cm-linked",
            "model": "qwen3.5:4b",
            "model_config_id": "mc-linked",
            "system_prompt": "Old prompt.",
            "persona_name": "Old name",
            "points": 3,
        }]
        resolved = asyncio.run(council_mod._resolve_member_personas(members))
        assert resolved[0]["model"] == "qwen3.5:14b"
        assert resolved[0]["system_prompt"] == "Latest persona prompt."
        assert resolved[0]["persona_name"] == "Latest Persona Name"
        assert members[0]["model"] == "qwen3.5:4b"

    def test_council_round_metadata_helpers(self):
        pytest.importorskip("aiosqlite")
        import council as council_mod

        members = [
            {"id": "m1", "model": "qwen3.5:4b", "persona_name": "Socrates"},
            {"id": "m2", "model": "llama3.1:8b", "persona_name": "Aristotle"},
            {"id": "m3", "model": "mistral:7b", "persona_name": "Nietzsche"},
        ]
        responses = {
            "m1": "Virtue requires examined action and practical wisdom in public life.",
            "m2": "Ethics begins with habits, community, and the purpose of human flourishing.",
        }

        assert council_mod._round_label(0, 2) == "Opening Statements"
        assert council_mod._round_label(1, 1) == "Rebuttal Round"
        assert council_mod._round_label(2, 3) == "Rebuttal Round 2"

        responding_to = council_mod._responding_to_members(members[2], members, responses)
        assert responding_to == [
            {"id": "m1", "name": "Socrates"},
            {"id": "m2", "name": "Aristotle"},
        ]

        winners = council_mod._vote_winners({"m2": 2, "m1": 2, "missing": 4}, members)
        assert winners == [
            {"id": "m2", "name": "Aristotle", "votes": 2},
            {"id": "m1", "name": "Socrates", "votes": 2},
        ]


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

    def test_seed_philosophers_preset_links_persona_configs(self, client):
        r = client.post("/api/seed/council-preset/philosophers")
        assert r.status_code == 200
        data = r.json()
        members = data.get("members") or []
        assert len(members) == 5

        configs = {mc["id"]: mc for mc in client.get("/api/model-configs").json()}
        for member in members:
            mc_id = member.get("model_config_id")
            assert mc_id
            mc = configs.get(mc_id)
            assert mc, f"missing linked persona config {mc_id}"
            assert (mc.get("parameters") or {}).get("profile_type") == "persona"
            assert member["model"] == mc["base_model"]
            assert member["system_prompt"] == mc["system_prompt"]
            assert member["persona_name"] == mc["name"]

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
