"""Offline unit tests for canvas_edit.py — Artifact Canvas AI selection edits.

No live server, no Ollama: complete_chat is monkeypatched.
Run: python -m pytest tests/test_canvas_edit.py -v
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import canvas_edit  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_strip_fences():
    assert canvas_edit._strip_fences("```python\nx = 1\n```") == "x = 1"
    assert canvas_edit._strip_fences("```\nplain\n```") == "plain"
    # Untouched when no full wrap
    assert canvas_edit._strip_fences("x = 1") == "x = 1"
    assert canvas_edit._strip_fences("prefix\n```\ncode\n```") == "prefix\n```\ncode\n```"


def test_build_prompt_sections():
    content = "AAAA selected BBBB"
    p = canvas_edit.build_prompt(content, 5, 13, "make it loud", filename="notes.md")
    assert "<<<SELECTED\nselected\nSELECTED>>>" in p
    assert "AAAA " in p and " BBBB" in p
    assert "make it loud" in p
    assert "notes.md" in p


def test_ai_edit_selection_basic(monkeypatch):
    async def fake_complete_chat(http, model, prompt, **kw):
        assert "INSTRUCTION: capitalize" in prompt
        return "SELECTED"

    monkeypatch.setattr(canvas_edit.model_providers, "complete_chat", fake_complete_chat)
    out = _run(canvas_edit.ai_edit_selection(
        None, "hello selected world", 6, 14, "capitalize", model="m"))
    assert out == "SELECTED"


def test_ai_edit_empty_selection_means_whole_doc(monkeypatch):
    seen = {}

    async def fake_complete_chat(http, model, prompt, **kw):
        seen["prompt"] = prompt
        return "rewritten"

    monkeypatch.setattr(canvas_edit.model_providers, "complete_chat", fake_complete_chat)
    out = _run(canvas_edit.ai_edit_selection(None, "whole document text", 7, 7, "rewrite", model="m"))
    assert out == "rewritten"
    assert "whole document text" in seen["prompt"].split("<<<SELECTED")[1]


def test_ai_edit_strips_model_fence(monkeypatch):
    async def fake_complete_chat(http, model, prompt, **kw):
        return "```js\nconst x = 1;\n```"

    monkeypatch.setattr(canvas_edit.model_providers, "complete_chat", fake_complete_chat)
    out = _run(canvas_edit.ai_edit_selection(None, "var x = 1;", 0, 10, "modernize", model="m"))
    assert out == "const x = 1;"


def test_ai_edit_requires_instruction():
    with pytest.raises(canvas_edit.CanvasEditError):
        _run(canvas_edit.ai_edit_selection(None, "text", 0, 4, "   ", model="m"))


def test_ai_edit_selection_too_large():
    big = "x" * (canvas_edit._MAX_SELECTION_CHARS + 10)
    with pytest.raises(canvas_edit.CanvasEditError):
        _run(canvas_edit.ai_edit_selection(None, big, 0, len(big), "edit", model="m"))


def test_ai_edit_empty_model_response(monkeypatch):
    async def fake_complete_chat(http, model, prompt, **kw):
        return ""

    monkeypatch.setattr(canvas_edit.model_providers, "complete_chat", fake_complete_chat)
    with pytest.raises(canvas_edit.CanvasEditError):
        _run(canvas_edit.ai_edit_selection(None, "text", 0, 4, "edit", model="m"))


def test_ai_edit_clamps_out_of_range_selection(monkeypatch):
    async def fake_complete_chat(http, model, prompt, **kw):
        return "ok"

    monkeypatch.setattr(canvas_edit.model_providers, "complete_chat", fake_complete_chat)
    out = _run(canvas_edit.ai_edit_selection(None, "short", 2, 99999, "edit", model="m"))
    assert out == "ok"
