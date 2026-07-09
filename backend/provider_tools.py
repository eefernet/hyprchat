"""Native tool-calling support for cloud model providers.

OpenAI (Responses API function calling) and Anthropic (tool_use blocks) get
real JSON tool definitions instead of HyprChat's text tool-call fallback.
This module owns:

  - tool-definition conversion (Ollama-style defs -> provider formats)
  - native message-history conversion (assistant tool_calls + role="tool"
    results round-trip as provider-native items instead of flattened text)
  - stream parsing helpers that turn provider SSE objects into normalized
    ``{"type": "tool_call", "id", "name", "arguments"}`` events

The custom (OpenAI-compatible) provider intentionally stays on the text
fallback — /chat/completions tool support varies wildly across local gateways.

`model_providers.py` imports this module lazily inside its stream functions;
this module imports `model_providers` only at call time, so there is no
import-order coupling between the two.
"""

import json

# Providers whose adapters understand native tool definitions. "custom" is
# deliberately absent (text fallback remains its contract).
NATIVE_TOOL_PROVIDERS = {"openai", "anthropic"}


def supports_native_tools(provider: str) -> bool:
    return (provider or "").lower() in NATIVE_TOOL_PROVIDERS


# ── Tool-definition conversion ───────────────────────────────────────────────
# HyprChat tool defs are Ollama-shaped:
#   {"type": "function", "function": {"name", "description", "parameters"}}


def openai_tool_defs(ollama_tools: list | None) -> list[dict]:
    """Responses API tool list: flat {"type":"function","name",...} entries."""
    out = []
    for t in ollama_tools or []:
        fn = (t or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        out.append({
            "type": "function",
            "name": name,
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def anthropic_tool_defs(ollama_tools: list | None) -> list[dict]:
    """Messages API tool list: {"name","description","input_schema"} entries."""
    out = []
    for t in ollama_tools or []:
        fn = (t or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        out.append({
            "name": name,
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


# ── Native message-history conversion ────────────────────────────────────────
# The chat loop keeps history Ollama-shaped: assistant messages may carry
# ``tool_calls`` (each with an ``id`` when it came from a cloud provider) and
# results are ``{"role": "tool", "content", "tool_call_id"?}``. Both providers
# validate call/result pairing inside a request, so dangling calls (no matching
# result — e.g. dropped hallucinated calls) are skipped and orphaned results
# (SYSTEM/HINT nudges with no id) degrade to plain user text like the classic
# converters always did.


def _result_call_ids(messages: list[dict]) -> set[str]:
    return {
        str(m.get("tool_call_id"))
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }


def _args_json(arguments) -> str:
    if isinstance(arguments, str):
        return arguments or "{}"
    try:
        return json.dumps(arguments or {}, ensure_ascii=False)
    except Exception:
        return "{}"


def _args_dict(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def openai_input_native(messages: list[dict]) -> list[dict]:
    """Responses API input with function_call / function_call_output items."""
    import model_providers as mp  # call-time import; see module docstring

    result_ids = _result_call_ids(messages)
    emitted: set[str] = set()
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role") or "user"
        if role == "tool":
            cid = str(msg.get("tool_call_id") or "")
            text = mp._coerce_text(msg.get("content"))
            if cid and cid in emitted:
                out.append({"type": "function_call_output", "call_id": cid, "output": text})
            else:
                out.append({"role": "user", "content": f"[Tool result]\n{text}"})
            continue
        tool_calls = msg.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            text = mp._coerce_text(msg.get("content"))
            if text:
                out.append({"role": "assistant", "content": text})
            for tc in tool_calls:
                fn = (tc or {}).get("function") or {}
                cid = str((tc or {}).get("id") or "")
                name = fn.get("name") or ""
                if not cid or not name or cid not in result_ids:
                    continue  # a call with no paired output 400s the request
                out.append({
                    "type": "function_call",
                    "call_id": cid,
                    "name": name,
                    "arguments": _args_json(fn.get("arguments")),
                })
                emitted.add(cid)
            continue
        out.extend(mp._openai_input([msg]))
    return out


def anthropic_messages_native(messages: list[dict]) -> tuple[str, list[dict]]:
    """Messages API history with tool_use / tool_result content blocks.

    Returns (system, messages) like `model_providers._anthropic_messages`.
    All message contents are block lists; adjacent same-role messages merge
    (Anthropic requires alternating roles). Results always precede any merged
    trailing text in a user message because they are appended chronologically
    right after the assistant tool_use turn.
    """
    import model_providers as mp  # call-time import; see module docstring

    result_ids = _result_call_ids(messages)
    emitted: set[str] = set()
    system_parts: list[str] = []
    out: list[dict] = []

    def _append(role: str, blocks: list[dict]):
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for msg in messages:
        role = msg.get("role") or "user"
        text = mp._coerce_text(msg.get("content"))
        if role in {"system", "developer"}:
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            cid = str(msg.get("tool_call_id") or "")
            if cid and cid in emitted:
                _append("user", [{
                    "type": "tool_result",
                    "tool_use_id": cid,
                    "content": text or "(no output)",
                }])
            else:
                _append("user", [{"type": "text", "text": f"[Tool result]\n{text}"}])
            continue
        tool_calls = msg.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            blocks: list[dict] = []
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in tool_calls:
                fn = (tc or {}).get("function") or {}
                cid = str((tc or {}).get("id") or "")
                name = fn.get("name") or ""
                if not cid or not name or cid not in result_ids:
                    continue  # tool_use with no paired tool_result 400s
                blocks.append({
                    "type": "tool_use",
                    "id": cid,
                    "name": name,
                    "input": _args_dict(fn.get("arguments")),
                })
                emitted.add(cid)
            _append("assistant", blocks)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        blocks = []
        images = msg.get("images") or []
        if text:
            blocks.append({"type": "text", "text": text})
        if role == "user" and images:
            for image in images:
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mp._image_media_type(str(image)),
                        "data": str(image).split(",", 1)[-1],
                    },
                })
        _append(role, blocks)

    if not out:
        out.append({"role": "user", "content": [{"type": "text", "text": "..."}]})
    return "\n\n".join(system_parts).strip(), out


# ── Stream parsing ───────────────────────────────────────────────────────────


def openai_tool_call_event(obj: dict) -> dict | None:
    """Map a Responses-API SSE object to a normalized tool_call event.

    `response.output_item.done` carries the complete function_call item
    (name + full arguments JSON), so no delta accumulation is needed.
    """
    if obj.get("type") != "response.output_item.done":
        return None
    item = obj.get("item") or {}
    if item.get("type") != "function_call":
        return None
    name = str(item.get("name") or "")
    cid = str(item.get("call_id") or item.get("id") or "")
    if not name or not cid:
        return None
    return {
        "type": "tool_call",
        "id": cid,
        "name": name,
        "arguments": _args_dict(item.get("arguments")),
    }


class AnthropicToolAccumulator:
    """Assembles streamed tool_use blocks (input_json_delta) by block index.

    feed() returns a normalized tool_call event when a tool_use block
    completes, otherwise None. Non-tool events pass through untouched —
    text/thinking deltas are never consumed here.
    """

    def __init__(self):
        self._blocks: dict = {}

    def feed(self, obj: dict) -> dict | None:
        typ = obj.get("type", "")
        if typ == "content_block_start":
            block = obj.get("content_block") or {}
            if block.get("type") == "tool_use":
                self._blocks[obj.get("index")] = {
                    "id": str(block.get("id") or ""),
                    "name": str(block.get("name") or ""),
                    "json": "",
                }
        elif typ == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("type") == "input_json_delta" and obj.get("index") in self._blocks:
                self._blocks[obj["index"]]["json"] += delta.get("partial_json") or ""
        elif typ == "content_block_stop":
            block = self._blocks.pop(obj.get("index"), None)
            if block and block["id"] and block["name"]:
                return {
                    "type": "tool_call",
                    "id": block["id"],
                    "name": block["name"],
                    "arguments": _args_dict(block["json"]),
                }
        return None


def tools_unsupported_error(err_text: str) -> bool:
    """Heuristic: does a provider error mean this model rejects tool use?

    Used by the chat loop to flip a native-tools round to the text fallback
    instead of failing the stream (mirrors Ollama's "does not support tools"
    handling).
    """
    low = (err_text or "").lower()
    if "tool" not in low:
        return False
    return any(s in low for s in (
        "not supported", "unsupported", "does not support", "doesn't support",
        "no tools", "tools are not", "tool use is not", "invalid parameter: 'tools'",
        "unknown parameter", "unexpected keyword",
    ))
