"""
Tests for Ollama model management endpoints.
"""
import pytest


class TestModelList:
    def test_list_models(self, client):
        r = client.get("/api/models")
        assert r.status_code == 200
        data = r.json()
        assert "models" in data
        assert isinstance(data["models"], list)
        assert len(data["models"]) > 0, "No models found — is Ollama running?"

    def test_models_are_strings(self, client):
        """Models endpoint returns a list of model name strings."""
        r = client.get("/api/models")
        models = r.json()["models"]
        for m in models[:3]:
            assert isinstance(m, str), f"Expected string model name, got: {type(m)}"

    def test_model_details_present(self, client):
        r = client.get("/api/models")
        data = r.json()
        assert "model_details" in data
        assert isinstance(data["model_details"], dict)


class TestModelInfo:
    def test_model_info(self, client):
        models = client.get("/api/models").json()["models"]
        if not models:
            pytest.skip("No models available")
        model_name = models[0]  # models are strings
        r = client.get(f"/api/models/{model_name}/info")
        assert r.status_code == 200

    def test_model_template_info(self, client):
        models = client.get("/api/models").json()["models"]
        if not models:
            pytest.skip("No models available")
        model_name = models[0]
        r = client.get(f"/api/models/{model_name}/template-info")
        assert r.status_code == 200


class TestBuiltinTools:
    def test_list_builtin_tools(self, client):
        r = client.get("/api/builtin-tools")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) > 0, "No builtin tools returned"
        # Tools are returned with display names (emoji-prefixed)
        all_names = " ".join(str(t) if isinstance(t, str) else t.get("name", "") for t in data)
        assert "CodeAgent" in all_names or "Research" in all_names

    def test_get_execution_languages(self, client):
        r = client.get("/api/execute/languages")
        assert r.status_code == 200
        data = r.json()
        assert "python" in str(data).lower()
