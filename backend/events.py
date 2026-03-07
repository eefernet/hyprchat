"""
SSE EventBus — broadcast status events to connected clients.
Also contains tool prompt helpers used by the chat agent.
"""
import asyncio
import re
import time


class EventBus:
    """Simple pub/sub for SSE status events per conversation."""
    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, conv_id: str) -> asyncio.Queue:
        q = asyncio.Queue()
        async with self._lock:
            self._subscribers.setdefault(conv_id, []).append(q)
        return q

    async def unsubscribe(self, conv_id: str, q: asyncio.Queue):
        async with self._lock:
            if conv_id in self._subscribers:
                self._subscribers[conv_id] = [x for x in self._subscribers[conv_id] if x is not q]

    async def emit(self, conv_id: str, event_type: str, data: dict):
        """Emit a status event to all subscribers of a conversation."""
        event = {"type": event_type, "data": data, "timestamp": time.time()}
        async with self._lock:
            targets = list(self._subscribers.get(conv_id, []))
        for q in targets:
            await q.put(event)


def inject_text_tool_prompt(messages: list, available_tool_names: set):
    """Inject instructions for text-based tool calling when native protocol isn't supported."""
    tool_names = ", ".join(sorted(available_tool_names))
    text_tool_prompt = (
        "\n\n## TOOL CALLING FORMAT\n"
        "To use tools, output ONLY a <tool_call> tag with JSON. One tool call per response.\n\n"
        "Available tools: " + tool_names + "\n\n"
        "## CORRECT WORKFLOW\n"
        "1. Install packages:\n"
        "<tool_call>\n"
        '{"name": "run_shell", "arguments": {"command": "pip3 install requests"}}\n'
        "</tool_call>\n\n"
        "2. Test code with hardcoded values (NO input(), NO sys.argv):\n"
        "<tool_call>\n"
        '{"name": "execute_code", "arguments": {"code": "from art import text2art\\nprint(text2art(\'Hello\', font=\'block\'))", "language": "python"}}\n'
        "</tool_call>\n\n"
        "3. Save the final script:\n"
        "<tool_call>\n"
        '{"name": "write_file", "arguments": {"path": "/root/app.py", "content": "#!/usr/bin/env python3\\nimport sys\\n..."}}\n'
        "</tool_call>\n\n"
        "4. Test the script with arguments:\n"
        "<tool_call>\n"
        '{"name": "run_shell", "arguments": {"command": "python3 /root/app.py Hello block"}}\n'
        "</tool_call>\n\n"
        "5. Deliver the file to the user (call download_file ONCE only):\n"
        "<tool_call>\n"
        '{"name": "download_file", "arguments": {"path": "/root/app.py"}}\n'
        "</tool_call>\n\n"
        "## RULES\n"
        "- ALWAYS use <tool_call> tags. Never put raw code in your response.\n"
        "- execute_code has NO stdin (input() crashes) and NO arguments (sys.argv is empty).\n"
        "- For scripts needing args: write_file first, then run_shell with arguments.\n"
        "- Call download_file ONCE per file. Do NOT repeat the same download.\n"
        "- If code fails, read the error, fix the root cause, and retry with a DIFFERENT approach.\n"
        "- After delivering files, write a brief summary for the user. Do NOT call more tools.\n"
    )
    if messages and messages[0]["role"] == "system":
        messages[0]["content"] += text_tool_prompt
    else:
        messages.insert(0, {"role": "system", "content": text_tool_prompt.strip()})


def parse_tool_params(code: str, func_name: str) -> dict:
    """Parse a Python function's parameter list to build an Ollama-compatible JSON schema.
    Falls back to a single 'input: str' parameter if parsing fails."""
    try:
        sig_match = re.search(
            rf'def\s+{re.escape(func_name)}\s*\(([^)]*)\)', code
        )
        if not sig_match:
            raise ValueError("no match")
        raw_params = sig_match.group(1).strip()
        if not raw_params:
            return {"type": "object", "properties": {}, "required": []}
        properties = {}
        required = []
        for param in raw_params.split(","):
            param = param.strip()
            if not param or param in ("self", "*args", "**kwargs"):
                continue
            name = re.split(r'[:\s=]', param)[0].strip()
            if not name:
                continue
            type_str = "string"
            if "int" in param:
                type_str = "integer"
            elif "float" in param:
                type_str = "number"
            elif "bool" in param:
                type_str = "boolean"
            properties[name] = {"type": type_str, "description": name}
            if "=" not in param:
                required.append(name)
        return {"type": "object", "properties": properties, "required": required}
    except Exception:
        return {"type": "object", "properties": {"input": {"type": "string", "description": "Input for the tool"}}, "required": ["input"]}
