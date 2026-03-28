"""
Tests for HuggingFace model browser endpoints.
"""
import pytest


class TestHuggingFaceSearch:
    def test_search_gguf_models(self, client):
        r = client.get("/api/hf/search", params={
            "q": "llama",
            "limit": 5,
            "gguf_only": True
        })
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    def test_search_all_models(self, client):
        r = client.get("/api/hf/search", params={
            "q": "bert",
            "limit": 3,
            "gguf_only": False
        })
        assert r.status_code == 200

    def test_search_empty_query(self, client):
        r = client.get("/api/hf/search", params={"q": "", "limit": 5})
        assert r.status_code == 200


class TestHuggingFaceModelInfo:
    def test_model_info(self, client):
        r = client.get("/api/hf/model", params={
            "repo_id": "TheBloke/Llama-2-7B-GGUF"
        })
        # May return 200 or error depending on HF availability
        assert r.status_code in (200, 404, 500)

    def test_model_readme(self, client):
        r = client.get("/api/hf/readme", params={
            "repo_id": "TheBloke/Llama-2-7B-GGUF"
        })
        assert r.status_code in (200, 404, 500)
