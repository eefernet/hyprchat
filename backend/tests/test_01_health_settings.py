"""
Tests for health checks, settings, changelog, and system endpoints.
"""
import pytest


class TestHealth:
    def test_health_endpoint(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "services" in data
        services = data["services"]
        assert "ollama" in services
        assert "codebox" in services

    def test_health_history(self, client):
        r = client.get("/api/health/history", params={"days": 7})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        assert "services" in data


class TestSettings:
    def test_get_settings(self, client):
        r = client.get("/api/settings")
        assert r.status_code == 200
        data = r.json()
        assert "current_ollama_url" in data
        assert "rag" in data

    def test_patch_settings_and_revert(self, client):
        # Read current
        orig = client.get("/api/settings").json()
        orig_cleanup = orig.get("file_cleanup_days")

        # Patch
        r = client.patch("/api/settings", json={"file_cleanup_days": 99})
        assert r.status_code == 200
        assert r.json().get("file_cleanup_days") == 99

        # Verify
        r2 = client.get("/api/settings").json()
        assert r2.get("file_cleanup_days") == 99

        # Revert
        client.patch("/api/settings", json={"file_cleanup_days": orig_cleanup or 30})


class TestChangelog:
    def test_get_changelog(self, client):
        r = client.get("/api/changelog")
        assert r.status_code == 200
        data = r.json()
        assert "content" in data


class TestRagStats:
    def test_get_rag_stats(self, client):
        r = client.get("/api/rag/stats")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)


class TestAnalytics:
    def test_token_analytics_by_day(self, client):
        r = client.get("/api/analytics/tokens", params={"days": 30, "group_by": "day"})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    def test_token_analytics_by_model(self, client):
        r = client.get("/api/analytics/tokens", params={"days": 30, "group_by": "model"})
        assert r.status_code == 200

    def test_token_analytics_by_persona(self, client):
        r = client.get("/api/analytics/tokens", params={"days": 30, "group_by": "persona"})
        assert r.status_code == 200

    def test_token_summary(self, client):
        r = client.get("/api/analytics/tokens/summary")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
