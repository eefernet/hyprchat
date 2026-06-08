"""
Static guards for chat streaming/context-meter UI contracts.

The chat stream is integration-heavy, so these tests protect the source-level
contracts that keep live generation visible and token-based.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHAT = ROOT / "backend" / "agents" / "chat.py"
FRONTEND = ROOT / "frontend" / "dist" / "index.html"


def test_tool_enabled_generation_streams_live_and_clears_tool_drafts():
    source = CHAT.read_text(encoding="utf-8")

    assert "_streamed_content = False" in source
    assert "Stream assistant text live even when tools" in source
    assert "'type': 'token', 'content': _chunk_buf" in source
    assert "if _streamed_content:" in source
    assert "'type': 'clear'" in source


def test_generation_status_and_context_meter_use_tokens_not_chars():
    chat_source = CHAT.read_text(encoding="utf-8")
    html = FRONTEND.read_text(encoding="utf-8")

    assert "Generating... (~{_shown_tokens} tokens)" in chat_source
    assert "Generating... ({len(content)} chars)" not in chat_source
    assert "Context used: prompt + generated tokens" in html
