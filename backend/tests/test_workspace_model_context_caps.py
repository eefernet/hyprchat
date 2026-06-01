"""
Static guard tests for Workspace Model helper calls.

Workspace helpers should never inherit chat DEFAULT_NUM_CTX or a model-native
256K context. A small helper model loaded at 256K can reserve tens of GB of KV
cache and evict the actual chat/coder models.
"""
from pathlib import Path


MAIN = Path(__file__).resolve().parents[1] / "main.py"


def _main() -> str:
    return MAIN.read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    start = src.index(f"async def {name}")
    next_route = src.find("\n\n@app.", start + 1)
    return src[start:] if next_route == -1 else src[start:next_route]


def test_workspace_helper_context_constants_exist():
    src = _main()

    assert "_WORKSPACE_HELPER_NUM_CTX = 4096" in src
    assert "_WORKSPACE_TITLE_NUM_CTX = 2048" in src


def test_workspace_topic_analysis_caps_ollama_context():
    block = _function_block(_main(), "analyze_workspace_topics")

    assert '"think": False' in block
    assert '"num_ctx": _WORKSPACE_HELPER_NUM_CTX' in block
    assert '"num_predict": 400' in block
    assert "DEFAULT_NUM_CTX" not in block


def test_council_suggestions_cap_workspace_model_context():
    block = _function_block(_main(), "get_council_suggestions")

    assert '"think": False' in block
    assert '"num_ctx": _WORKSPACE_HELPER_NUM_CTX' in block
    assert '"num_predict": 200' in block
    assert "DEFAULT_NUM_CTX" not in block


def test_auto_title_uses_tiny_workspace_context():
    block = _function_block(_main(), "generate_title")

    assert '"think": False' in block
    assert '"num_ctx": _WORKSPACE_TITLE_NUM_CTX' in block
    assert "DEFAULT_NUM_CTX" not in block
