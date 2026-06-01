"""
Integration tests — end-to-end flows that combine multiple features.
"""
class TestConversationLifecycle:
    """Create conv → add messages → search → generate title → fork → delete."""

    def test_full_lifecycle(self, client):
        # 1. Create
        r = client.post("/api/conversations", json={"title": "Lifecycle Test"})
        assert r.status_code == 200
        conv = r.json()
        cid = conv["id"]

        # 2. Add messages
        r = client.post(f"/api/conversations/{cid}/messages", json={
            "role": "user", "content": "What is machine learning?"
        })
        assert r.status_code == 200
        r = client.post(f"/api/conversations/{cid}/messages", json={
            "role": "assistant", "content": "Machine learning is a subset of AI."
        })
        assert r.status_code == 200

        # 3. Verify messages persisted
        conv_data = client.get(f"/api/conversations/{cid}").json()
        assert len(conv_data.get("messages", [])) >= 2

        # 4. Search
        r = client.post("/api/conversations/search", json={"query": "machine learning"})
        assert r.status_code == 200

        # 5. Update
        r = client.patch(f"/api/conversations/{cid}", json={"title": "ML Discussion"})
        assert r.status_code == 200
        assert client.get(f"/api/conversations/{cid}").json()["title"] == "ML Discussion"

        # 6. Fork
        conv_data = client.get(f"/api/conversations/{cid}").json()
        msgs = conv_data.get("messages", [])
        if msgs:
            r = client.post(f"/api/conversations/{cid}/fork", json={"message_id": msgs[0].get("id", 1)})
            assert r.status_code == 200
            fork_id = r.json()["id"]
            client.delete(f"/api/conversations/{fork_id}")

        # 7. Delete
        r = client.delete(f"/api/conversations/{cid}")
        assert r.status_code == 200

        # 8. Verify deleted
        r = client.get(f"/api/conversations/{cid}")
        assert r.status_code == 404


class TestPersonaWithKB:
    """Create KB → Create persona with KB → verify linkage → cleanup."""

    def test_persona_kb_linkage(self, client):
        # Create KB
        kb = client.post("/api/knowledge-bases", json={
            "name": "Persona Test KB", "description": "test"
        }).json()

        # Create persona linked to KB
        mc = client.post("/api/model-configs", json={
            "name": "KB-Linked Persona",
            "base_model": "qwen3.5:4b",
            "system_prompt": "You have KB knowledge.",
            "kb_ids": [kb["id"]]
        }).json()

        assert "id" in mc

        # Verify persona exists
        personas = client.get("/api/model-configs").json()
        found = next((p for p in personas if p["id"] == mc["id"]), None)
        assert found is not None

        # Cleanup
        client.delete(f"/api/model-configs/{mc['id']}")
        client.delete(f"/api/knowledge-bases/{kb['id']}")


class TestCouncilWithMembers:
    """Create council → add members → get suggestions → cleanup."""

    def test_council_with_members_flow(self, long_client):
        # 1. Create council
        council = long_client.post("/api/councils", json={
            "name": "E2E Test Council",
            "host_model": "qwen3.5:4b",
            "host_system_prompt": "Moderate a debate."
        }).json()
        cid = council["id"]

        # 2. Add members
        m1 = long_client.post(f"/api/councils/{cid}/members", json={
            "model": "qwen3.5:4b",
            "persona_name": "Optimist",
            "system_prompt": "Always see the bright side."
        }).json()
        assert "id" in m1

        m2 = long_client.post(f"/api/councils/{cid}/members", json={
            "model": "qwen3.5:4b",
            "persona_name": "Skeptic",
            "system_prompt": "Question everything."
        }).json()
        assert "id" in m2

        # 3. Verify council has members
        council_data = long_client.get(f"/api/councils/{cid}").json()
        assert len(council_data.get("members", [])) >= 2

        # 4. Get suggestions
        r = long_client.get(f"/api/councils/{cid}/suggestions")
        assert r.status_code == 200
        suggestions = r.json().get("suggestions", [])
        assert isinstance(suggestions, list)

        # 5. Get analytics
        r = long_client.get(f"/api/councils/{cid}/analyze")
        assert r.status_code == 200

        # 6. Cleanup
        long_client.delete(f"/api/councils/{cid}")


class TestWorkspaceWithConversations:
    """Create workspace → create convs → add to workspace → cleanup."""

    def test_workspace_conv_management(self, client):
        # 1. Create workspace
        ws = client.post("/api/workspaces", json={
            "name": "E2E Workspace", "description": "test"
        }).json()
        ws_id = ws["id"]

        # 2. Create conversations
        c1 = client.post("/api/conversations", json={"title": "WS Conv 1"}).json()
        c2 = client.post("/api/conversations", json={"title": "WS Conv 2"}).json()

        # 3. Add to workspace
        r = client.post(f"/api/workspaces/{ws_id}/conversations", json={
            "conversation_id": c1["id"]
        })
        assert r.status_code == 200
        r = client.post(f"/api/workspaces/{ws_id}/conversations", json={
            "conversation_id": c2["id"]
        })
        assert r.status_code == 200

        # 4. Verify workspace has conversations
        ws_data = client.get(f"/api/workspaces/{ws_id}").json()
        conv_ids = [c.get("id", c.get("conversation_id", "")) for c in ws_data.get("conversations", [])]
        assert c1["id"] in conv_ids or len(ws_data.get("conversations", [])) >= 2

        # 5. Remove one
        r = client.delete(f"/api/workspaces/{ws_id}/conversations/{c1['id']}")
        assert r.status_code == 200

        # 6. Cleanup
        client.delete(f"/api/workspaces/{ws_id}")
        client.delete(f"/api/conversations/{c1['id']}")
        client.delete(f"/api/conversations/{c2['id']}")
