"""
Tests for chat streaming (SSE) and event system.
"""
import pytest
import httpx
import json


class TestChatStream:
    def test_chat_stream_returns_sse(self, base_url, created_conversation):
        """Verify chat/stream returns SSE events with content."""
        cid = created_conversation["id"]
        with httpx.Client(base_url=base_url, timeout=120.0, verify=False) as c:
            with c.stream("POST", "/api/chat/stream", json={
                "conversation_id": cid,
                "model": "qwen3.5:4b",
                "messages": [{"role": "user", "content": "Say the word 'pineapple' and nothing else."}],
                "stream": True,
                "think_budget": 0,
            }) as resp:
                assert resp.status_code == 200
                content_type = resp.headers.get("content-type", "")
                assert "text/event-stream" in content_type

                got_token = False
                got_done = False
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            got_done = True
                            break
                        try:
                            evt = json.loads(payload)
                            if evt.get("type") in ("token", "content") and evt.get("content"):
                                got_token = True
                            if evt.get("type") == "done":
                                got_done = True
                                break
                        except json.JSONDecodeError:
                            pass

                assert got_token, "No token events received from chat stream"
                assert got_done, "Stream did not end with done event"

    def test_chat_stream_bad_model(self, base_url, created_conversation):
        """Chat with a nonexistent model should still return SSE (with error)."""
        cid = created_conversation["id"]
        with httpx.Client(base_url=base_url, timeout=30.0, verify=False) as c:
            with c.stream("POST", "/api/chat/stream", json={
                "conversation_id": cid,
                "model": "nonexistent-model-xyz:latest",
                "messages": [{"role": "user", "content": "test"}],
                "stream": True,
            }) as resp:
                # Should still return 200 (SSE) — error comes in the stream
                assert resp.status_code == 200


class TestEventStream:
    def test_event_stream_connects(self, base_url, created_conversation):
        """Verify SSE event stream endpoint accepts connection."""
        cid = created_conversation["id"]
        with httpx.Client(base_url=base_url, timeout=5.0, verify=False) as c:
            try:
                with c.stream("GET", f"/api/events/{cid}") as resp:
                    assert resp.status_code == 200
                    content_type = resp.headers.get("content-type", "")
                    assert "text/event-stream" in content_type
                    # Just verify connection works, don't wait for events
            except httpx.ReadTimeout:
                pass  # Expected — no events to send
