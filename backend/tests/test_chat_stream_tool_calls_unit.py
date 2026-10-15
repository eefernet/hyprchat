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
