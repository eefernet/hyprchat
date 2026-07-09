"""Offline unit tests for provider_tools.py — native cloud tool calling.

No live server, no network: pure conversion/parsing helpers.
Run: python -m pytest tests/test_provider_tools.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import provider_tools  # noqa: E402


OLLAMA_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {"type": "function", "function": {"name": "no_params"}},
    {"type": "function", "function": {}},  # nameless — dropped
]


# ── Tool definition conversion ───────────────────────────────────────────────

def test_openai_tool_defs():
    defs = provider_tools.openai_tool_defs(OLLAMA_TOOLS)
    assert len(defs) == 2
    assert defs[0] == {
        "type": "function",
        "name": "web_search",
        "description": "Search the web",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
    # Missing parameters degrade to an empty object schema
    assert defs[1]["parameters"] == {"type": "object", "properties": {}}


def test_anthropic_tool_defs():
    defs = provider_tools.anthropic_tool_defs(OLLAMA_TOOLS)
    assert len(defs) == 2
    assert defs[0]["name"] == "web_search"
    assert defs[0]["input_schema"]["required"] == ["query"]
    assert "type" not in defs[0]  # Anthropic defs are flat, no {"type":"function"}


def test_tool_defs_empty():
    assert provider_tools.openai_tool_defs(None) == []
    assert provider_tools.anthropic_tool_defs([]) == []


# ── Native history conversion ────────────────────────────────────────────────

HISTORY = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "What's the weather?"},
    {
        "role": "assistant",
        "content": "Let me check.",
        "tool_calls": [
            {"id": "call_1", "function": {"name": "web_search", "arguments": {"query": "weather"}}},
            {"id": "call_dangling", "function": {"name": "web_search", "arguments": {}}},
        ],
    },
    {"role": "tool", "content": "Sunny, 25C", "tool_call_id": "call_1"},
    {"role": "tool", "content": "SYSTEM: stop looping"},  # nudge — no id
]


def test_openai_input_native_pairing():
    items = provider_tools.openai_input_native(HISTORY)
    kinds = [i.get("type") or i.get("role") for i in items]
    assert kinds == ["system", "user", "assistant", "function_call", "function_call_output", "user"]
    fc = items[3]
    assert fc["call_id"] == "call_1"
    assert fc["name"] == "web_search"
    assert '"query"' in fc["arguments"]  # arguments serialized to JSON string
    out = items[4]
    assert out == {"type": "function_call_output", "call_id": "call_1", "output": "Sunny, 25C"}
    # dangling call (no result) skipped; orphan nudge degraded to user text
    assert all(i.get("call_id") != "call_dangling" for i in items if isinstance(i, dict))
    assert items[5]["content"].startswith("[Tool result]")


def test_openai_input_native_orphan_result_degrades():
    # Result id never emitted as a call — must not produce function_call_output
    items = provider_tools.openai_input_native([
        {"role": "tool", "content": "stale", "tool_call_id": "call_unknown"},
    ])
    assert items == [{"role": "user", "content": "[Tool result]\nstale"}]


def test_anthropic_messages_native_pairing():
    system, msgs = provider_tools.anthropic_messages_native(HISTORY)
    assert system == "You are helpful."
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    a_blocks = msgs[1]["content"]
    assert a_blocks[0] == {"type": "text", "text": "Let me check."}
    assert a_blocks[1]["type"] == "tool_use"
    assert a_blocks[1]["id"] == "call_1"
    assert a_blocks[1]["input"] == {"query": "weather"}
    assert len(a_blocks) == 2  # dangling call dropped
    # result + nudge merged into ONE user message, result block first
    u_blocks = msgs[2]["content"]
    assert u_blocks[0]["type"] == "tool_result"
    assert u_blocks[0]["tool_use_id"] == "call_1"
    assert u_blocks[1]["type"] == "text"
    assert "stop looping" in u_blocks[1]["text"]


def test_anthropic_messages_native_string_args_parsed():
    _, msgs = provider_tools.anthropic_messages_native([
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": '{"a": 1}'}}]},
        {"role": "tool", "content": "ok", "tool_call_id": "c1"},
    ])
    assert msgs[0]["content"][0]["input"] == {"a": 1}


def test_anthropic_messages_native_empty():
    system, msgs = provider_tools.anthropic_messages_native([])
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"][0]["text"]


# ── Stream parsing ───────────────────────────────────────────────────────────

def test_openai_tool_call_event():
    evt = provider_tools.openai_tool_call_event({
        "type": "response.output_item.done",
        "item": {"type": "function_call", "call_id": "call_9", "name": "web_search",
                 "arguments": '{"query": "hi"}'},
    })
    assert evt == {"type": "tool_call", "id": "call_9", "name": "web_search",
                   "arguments": {"query": "hi"}}


def test_openai_tool_call_event_ignores_others():
    assert provider_tools.openai_tool_call_event({"type": "response.output_text.delta"}) is None
    assert provider_tools.openai_tool_call_event({
        "type": "response.output_item.done", "item": {"type": "message"},
    }) is None
    # Malformed arguments degrade to {}
    evt = provider_tools.openai_tool_call_event({
        "type": "response.output_item.done",
        "item": {"type": "function_call", "call_id": "c", "name": "t", "arguments": "{bad"},
    })
    assert evt["arguments"] == {}


def test_anthropic_tool_accumulator():
    acc = provider_tools.AnthropicToolAccumulator()
    assert acc.feed({"type": "content_block_start", "index": 1,
                     "content_block": {"type": "tool_use", "id": "tu_1", "name": "web_search"}}) is None
    assert acc.feed({"type": "content_block_delta", "index": 1,
                     "delta": {"type": "input_json_delta", "partial_json": '{"que'}}) is None
    assert acc.feed({"type": "content_block_delta", "index": 1,
                     "delta": {"type": "input_json_delta", "partial_json": 'ry": "x"}'}}) is None
    evt = acc.feed({"type": "content_block_stop", "index": 1})
    assert evt == {"type": "tool_call", "id": "tu_1", "name": "web_search",
                   "arguments": {"query": "x"}}
    # Text blocks pass through untouched (no event, no state)
    assert acc.feed({"type": "content_block_start", "index": 0,
                     "content_block": {"type": "text"}}) is None
    assert acc.feed({"type": "content_block_stop", "index": 0}) is None


def test_anthropic_tool_accumulator_empty_input():
    acc = provider_tools.AnthropicToolAccumulator()
    acc.feed({"type": "content_block_start", "index": 0,
              "content_block": {"type": "tool_use", "id": "tu", "name": "t"}})
    evt = acc.feed({"type": "content_block_stop", "index": 0})
    assert evt["arguments"] == {}  # no-arg tools stream no input_json_delta


# ── Fallback heuristic ───────────────────────────────────────────────────────

def test_tools_unsupported_error():
    assert provider_tools.tools_unsupported_error("This model does not support tool use.")
    assert provider_tools.tools_unsupported_error("Invalid parameter: 'tools' is not supported")
    assert not provider_tools.tools_unsupported_error("rate limit exceeded")
    assert not provider_tools.tools_unsupported_error("")
    # "tool" alone isn't enough — needs an unsupported cue
    assert not provider_tools.tools_unsupported_error("tool execution failed downstream")


def test_supports_native_tools():
    assert provider_tools.supports_native_tools("openai")
    assert provider_tools.supports_native_tools("anthropic")
    assert not provider_tools.supports_native_tools("custom")
    assert not provider_tools.supports_native_tools("ollama")
    assert not provider_tools.supports_native_tools("")
