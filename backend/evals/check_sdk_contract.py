"""Check the pinned SDK adapter without making any inference requests.

Run with the worker environment: python evals/check_sdk_contract.py
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litellm import ModelResponse
from openhands.sdk import LLM, Message, TextContent

from coder_sdk_runtime import compile_context, normalize_text_tool_response, pin_task_text, run_coder
from context_policy import DEFAULTS, resolve


def check_execution():
    """Drive the actual SDK through native narration into text-tool editing."""
    from coder_repository import Repository
    from coder_worker_runtime import WorkerStore
    import coder_inference
    import openhands_worker as worker

    worker._ensure_sdk()
    with tempfile.TemporaryDirectory(prefix="daedalus-sdk-execution-") as temporary:
        root = Path(temporary)
        project = root / "project"
        project.mkdir()
        repository = Repository(project, root / "repository")
        store = WorkerStore(root / "worker")
        (store.root / "jobs" / "contract").mkdir(parents=True)
        settings = {**DEFAULTS, "openhands_num_ctx":65536}
        store.create("contract-op", "contract", "code", {
            "settings":settings, "task":"Create proof.py containing verified = True.",
            "model":"qwen3-coder:30b", "ollama_url":"http://127.0.0.1:1",
            "seconds_remaining":60, "calls_remaining":10, "request_key":"contract"})
        store.update("contract-op", status="running", started=time.time())
        requests = []

        def transport(self, *, messages, **kwargs):
            requests.append((messages, kwargs))
            if len(requests) < 3:
                content = "I need to perform the next action."
            elif len(requests) == 3:
                assert not kwargs.get("tools"), "The SDK did not switch to text tools"
                assert "</function" in kwargs.get("stop", []), "Update this contract if the SDK stop protocol changes"
                first = next(message["content"] for message in messages if message["role"] == "user")
                assert first.index("Work only in this project:") > 0, "Task pinning removed the SDK example"
                content = '<function name="file_editor">' + json.dumps({
                    "command":"create", "path":str(project / "proof.py"),
                    "file_text":"verified = True\n", "security_risk":"LOW"})
            else:
                content = '<function=finish>\n<parameter=message>Verified.</parameter>\n</function>'
            return ModelResponse(choices=[{"index":0, "finish_reason":"stop", "message":{"role":"assistant", "content":content}}])

        with patch.object(worker, "_check_tool_support", return_value=True), \
             patch.object(worker, "_model_capabilities", return_value=[]), \
             patch.object(coder_inference, "ensure_context"), \
             patch.object(worker._LLM, "_transport_call", transport):
            try:
                result = run_coder(store, "contract-op", repository)
            except Exception:
                for event in store.events("contract-op"):
                    if event["type"] == "agent":
                        value = event["data"]["event"]
                        print(json.dumps({key:value[key] for key in ("kind", "action", "observation", "llm_message") if key in value})[:1200])
                raise
        assert result["agent_finished"] and len(requests) == 4
        assert (project / "proof.py").read_text() == "verified = True\n"


def main():
    llm = LLM(model="ollama_chat/qwen3-coder:30b", api_key="ollama",
              max_input_tokens=65536, max_output_tokens=4096,
              native_tool_calling=False, num_retries=0)
    tools = [{"type":"function", "function":{"name":"terminal", "description":"Execute a command",
              "parameters":{"type":"object", "properties":{"command":{"type":"string"}}, "required":["command"]}}}]
    messages = [Message(role="system", content=[TextContent(text="Use tools to edit the project.")]),
                Message(role="user", content=[TextContent(text="The previous milestone.")])]
    current = "Repair the current failing test and verify the result."
    formatted = pin_task_text(llm.format_messages_for_llm(messages), current)
    mocked, _ = llm.pre_request_prompt_mock(formatted, tools, {})
    first = next(message["content"] for message in mocked if message["role"] == "user")
    assert current in first and first != current, "SDK tool examples were lost"
    compiled, _ = compile_context(mocked, None, resolve(settings=DEFAULTS), lambda _: "Earlier work")
    assert next(message["content"] for message in compiled if message["role"] == "user") == first
    assert messages[1].content[0].text == "The previous milestone."

    for attribute in ("name", "call"):
        response = ModelResponse(choices=[{"index":0, "finish_reason":"stop", "message":{"role":"assistant",
            "content":f'<function {attribute}="terminal">' + json.dumps({"command":"printf verified"}) + '</function>'}}])
        assert normalize_text_tool_response(response, tools) == 1
        parsed = Message.from_llm_chat_message(response.choices[0].message)
        assert parsed.tool_calls[0].name == "terminal"
        assert json.loads(parsed.tool_calls[0].arguments) == {"command":"printf verified"}
        assert normalize_text_tool_response(response, tools) == 0, "Native calls must remain unchanged"
    check_execution()
    print("Pinned SDK serialization, fallback editing, and completion contracts passed; no inference requests made.")


if __name__ == "__main__":
    main()
