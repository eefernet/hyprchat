"""Offline: native tool calls streamed by Ollama one-per-chunk must all survive the round (no Ollama needed)."""
import sys
from pathlib import Path

from .optional_deps import HAS_AIOSQLITE, HAS_CHROMADB, install_aiosqlite_stub, install_rag_stub

_BACKEND = Path(__file__).resolve().parent.parent
_AGENTS = _BACKEND / "agents"
for _p in (_BACKEND, _AGENTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

if not HAS_AIOSQLITE:
    install_aiosqlite_stub()
if not HAS_CHROMADB:
    install_rag_stub()

from agents.chat import _merge_stream_tool_calls  # noqa: E402


def _call(name, **args):
    return {"function": {"name": name, "arguments": args}}


def test_calls_streamed_in_separate_chunks_are_all_kept_in_order():
    # Ollama 0.35.0 (qwen3-coder:30b, qwen3.8:27b): "read a.py and b.py" arrives as two chunks, one call each.
    collected = []
    collected = _merge_stream_tool_calls(collected, [_call("read_file", path="src/a.py")])
    collected = _merge_stream_tool_calls(collected, [_call("read_file", path="src/b.py")])
    assert [c["function"]["arguments"]["path"] for c in collected] == ["src/a.py", "src/b.py"]


def test_a_restated_call_is_not_a_second_invocation():
    first = _call("execute_code", code="print(1)")
    collected = _merge_stream_tool_calls([], [first])
    collected = _merge_stream_tool_calls(collected, [first, _call("download_file", filename="out.txt")])
    assert [c["function"]["name"] for c in collected] == ["execute_code", "download_file"]


def test_empty_and_malformed_chunks_are_ignored():
    collected = _merge_stream_tool_calls([], None)
    collected = _merge_stream_tool_calls(collected, [])
    collected = _merge_stream_tool_calls(collected, ["not-a-call", {"function": {"name": "x", "arguments": {"n": 1}}}])
    assert collected == [{"function": {"name": "x", "arguments": {"n": 1}}}]


# ── Context exhaustion mid tool call (conv-5f8704bb8897, 2026-11-12) ────────────
# qwen3.6-35b pushed a 40 KB page through execute_code three times; round 4 hit
# n_tokens = 32767 / truncated = 1 and llama-server reported a cut-off JSON argument.
import inspect  # noqa: E402

from agents.chat import (context_exhausted, context_full_message, truncated_tool_call_error,  # noqa: E402
                         _chat_stream_generate)


def test_guard_blocks_only_when_observed_usage_leaves_no_room():
    # Round 3 of the live failure: 14,677 prompt + 10,516 generated + the tool result.
    assert context_exhausted(14677, 10516, 5200, 32768)
    assert context_exhausted(25183, 7585, 0, 32768)
    assert not context_exhausted(3120, 273, 40, 32768)
    # No observed prompt count (first round, or a provider that reports none) never blocks.
    assert not context_exhausted(0, 0, 40000, 32768)
    assert not context_exhausted(None, None, None, None)
    assert not context_exhausted(10, 10, 10, "bad")


def test_context_full_message_names_the_numbers_and_the_way_out():
    text = context_full_message(30441, 32768)
    assert "30,441 of 32,768" in text and "new chat" in text and "Settings" in text


def test_truncated_tool_call_error_is_translated_with_context_usage():
    raw = 'llama-server returned invalid tool call arguments for "execute_code": unexpected end of JSON input'
    text = truncated_tool_call_error(raw, 25183 + 7585, 32768)
    assert text.startswith("The model's tool call was cut off")
    assert "context window full: 32,768 of 32,768 tokens" in text
    assert "Original: llama-server returned invalid tool call" in text
    # Same parser error far from the window edge is still explained, without claiming the window filled.
    assert "context window full" not in truncated_tool_call_error(raw, 2000, 32768)
    assert "2,000 of 32,768" in truncated_tool_call_error(raw, 2000, 32768)
    # Other Ollama errors (OOM, corrupt model) keep their own branches.
    assert truncated_tool_call_error("CUDA error: out of memory", 100, 32768) is None


def test_prompt_tells_ordinary_chat_to_put_html_in_a_fence():
    # Adjacent string literals: join them the way Python does before asserting on the prompt text.
    source = inspect.getsource(_chat_stream_generate).replace('"\n', '"').replace('" "', '')
    source = "".join(line.strip() for line in source.splitlines())
    assert "Web pages / HTML demos" in source and "in ONE ```html code fence" in source
    assert "not available in ordinary chat" in source
    assert "emit a ```html fence in the reply instead of saving a file" in source
