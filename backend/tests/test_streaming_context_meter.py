"""
Static guards for chat streaming/context-meter UI contracts.

The chat stream is integration-heavy, so these tests protect the source-level
contracts that keep live generation visible and token-based.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHAT = ROOT / "backend" / "agents" / "chat.py"
FRONTEND = ROOT / "frontend" / "src" / "main.jsx"


def test_tool_enabled_generation_streams_live_and_clears_tool_drafts():
    source = CHAT.read_text(encoding="utf-8")

    assert "_streamed_content = False" in source
    assert "Stream assistant text live even when tools" in source
    assert "'type': 'token', 'content': _chunk_buf" in source
    # Drafts are cleared only after a text-tool-call round (stronger contract
    # than the old unconditional clear).
    assert "if _streamed_content and _text_tool_call_round:" in source
    assert "'type': 'clear'" in source


def test_generation_status_and_context_meter_use_tokens_not_chars():
    chat_source = CHAT.read_text(encoding="utf-8")
    html = FRONTEND.read_text(encoding="utf-8")

    assert "Generating... (~{_shown_tokens} tokens)" in chat_source
    assert "Generating... ({len(content)} chars)" not in chat_source
    # The pill reads one saved reading per conversation (contextMeter.js) and is never cleared on send,
    # chat switch or reload: those resets were the "context counter resets itself" bug of 2026-11-13.
    meter = (ROOT / "frontend" / "src" / "contextMeter.js").read_text(encoding="utf-8")
    assert "Conversation: " in meter and "not kept" in meter
    assert "setCtxTokens" not in html and "contextReadings[actId]" in html
    assert "atStreamStart(messagesToSend" in html and "fromMessages(full.messages)" in html
