"""
Tests for conversation CRUD, messages, search, and forking.
"""
import pytest


class TestConversationCRUD:
    def test_create_conversation(self, client):
        r = client.post("/api/conversations", json={"title": "CRUD Test Conv"})
        assert r.status_code == 200
        data = r.json()
        assert "id" in data
        assert data["title"] == "CRUD Test Conv"
        # Cleanup
        client.delete(f"/api/conversations/{data['id']}")

    def test_list_conversations(self, client, created_conversation):
        r = client.get("/api/conversations", params={"limit": 50, "offset": 0})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        ids = [c["id"] for c in data]
        assert created_conversation["id"] in ids

    def test_get_conversation(self, client, created_conversation):
        cid = created_conversation["id"]
        r = client.get(f"/api/conversations/{cid}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == cid

    def test_update_conversation(self, client, created_conversation):
        cid = created_conversation["id"]
        r = client.patch(f"/api/conversations/{cid}", json={"title": "Updated Title"})
        assert r.status_code == 200
        # Verify
        data = client.get(f"/api/conversations/{cid}").json()
        assert data["title"] == "Updated Title"

    def test_get_nonexistent_conversation(self, client):
        r = client.get("/api/conversations/conv-nonexistent999")
        assert r.status_code == 404


class TestMessages:
    def test_add_message(self, client, created_conversation):
        cid = created_conversation["id"]
        r = client.post(f"/api/conversations/{cid}/messages", json={
            "role": "user",
            "content": "Hello, this is a test message."
        })
        assert r.status_code == 200

    def test_messages_persisted(self, client, created_conversation):
        cid = created_conversation["id"]
        # Add a message first
        client.post(f"/api/conversations/{cid}/messages", json={
            "role": "user",
            "content": "Persisted message test"
        })
        r = client.get(f"/api/conversations/{cid}")
        assert r.status_code == 200
        messages = r.json().get("messages", [])
        assert any("Persisted message" in m.get("content", "") for m in messages)


class TestConversationSearch:
    def test_search_conversations(self, client, created_conversation):
        r = client.post("/api/conversations/search", json={"query": "test", "limit": 10})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)


class TestConversationFork:
    def test_fork_conversation(self, client, created_conversation):
        cid = created_conversation["id"]
        # Add messages so there's something to fork
        client.post(f"/api/conversations/{cid}/messages", json={
            "role": "user", "content": "Message before fork"
        })
        client.post(f"/api/conversations/{cid}/messages", json={
            "role": "assistant", "content": "Response before fork"
        })
        # Get messages to find a message_id
        conv = client.get(f"/api/conversations/{cid}").json()
        messages = conv.get("messages", [])
        if not messages:
            pytest.skip("No messages to fork from")
        msg_id = messages[0].get("id", 1)

        r = client.post(f"/api/conversations/{cid}/fork", json={"message_id": msg_id})
        assert r.status_code == 200
        forked = r.json()
        assert "id" in forked
        assert forked["id"] != cid
        # Cleanup
        client.delete(f"/api/conversations/{forked['id']}")

    def test_list_forks(self, client, created_conversation):
        cid = created_conversation["id"]
        r = client.get(f"/api/conversations/{cid}/forks")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
