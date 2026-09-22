"""Check the pinned SDK adapter without making any inference requests.

Run with the worker environment: python evals/check_sdk_contract.py
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import shlex
from unittest.mock import patch

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litellm import ModelResponse
from openhands.sdk import LLM, Message, TextContent

from coder_sdk_runtime import compile_context, normalize_text_tool_response, pin_task_text, run_coder
from context_policy import DEFAULTS, resolve


def check_execution(protocol_failure=False, policy_version=None):
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
        (project/'index.html').write_text('<!doctype html><link rel="icon" href="data:,"><title>SDK browser fixture</title><h1>Ready</h1>')
        repository = Repository(project, root / "repository")
        store = WorkerStore(root / "worker")
        (store.root / "jobs" / "contract").mkdir(parents=True)
        settings = {**DEFAULTS, "openhands_num_ctx":65536}
        store.create("contract-op", "contract", "code", {
            "settings":settings, "policy_version":policy_version or (3 if protocol_failure else 2),"ui_required":True, "task":"Create proof.py containing verified = True.",
            "model":"qwen3-coder:30b", "ollama_url":"http://127.0.0.1:1",
            "seconds_remaining":60, "calls_remaining":10, "request_key":"contract"})
        store.update("contract-op", status="running", started=time.time())
        requests = []

        def transport(self, *, messages, **kwargs):
            requests.append((messages, kwargs))
            if len(requests)==1:
                assert any('browser' in tool['function']['name'] for tool in kwargs.get('tools',[])), 'Browser tool missing from the SDK'
                if policy_version and policy_version>=4:
                    tool=next(tool['function'] for tool in kwargs['tools'] if 'browser' in tool['function']['name'])
                    assert 'oneOf' in tool['parameters']['properties']['steps']['items'], 'Strict browser schema missing'
                if protocol_failure: raise RuntimeError('Ollama_chatException XML syntax error on line 7')
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
            elif len(requests)==4:
                name=next(tool['function']['name'] for tool in requests[0][1]['tools'] if 'browser' in tool['function']['name'])
                content='<function name="'+name+'">'+json.dumps({"server_command":shlex.quote(sys.executable)+' -m http.server {port} --bind 127.0.0.1',
                    "steps":[{"action":"text","selector":"h1","value":"Ready"}]})
            else:
                content = '<function=finish>\n<parameter=message>Verified.</parameter>\n</function>'
            return ModelResponse(choices=[{"index":0, "finish_reason":"stop", "message":{"role":"assistant", "content":content}}])

        with patch.object(worker, "_check_tool_support", return_value=True), \
             patch.object(worker, "_model_capabilities", return_value=[]), \
             patch.object(coder_inference, "ensure_context"), \
             patch.object(coder_inference, "require_local_model",return_value={'capabilities':[]}), \
             patch.object(worker._LLM, "_transport_call", transport):
            try:
                result = run_coder(store, "contract-op", repository)
            except Exception:
                for event in store.events("contract-op"):
                    if event["type"] == "agent":
                        value = event["data"]["event"]
                        print(json.dumps({key:value[key] for key in ("kind", "action", "observation", "llm_message") if key in value})[:1200])
                raise
        assert result["agent_finished"] and len(requests) == 5
        assert (project / "proof.py").read_text() == "verified = True\n"
        diagnostic=next(event['data']['result'] for event in store.events('contract-op') if event['type']=='browser_diagnostic')
        assert diagnostic['passed'] and len(diagnostic['traces'])==2 and len(diagnostic['screenshots'])==2
        assert 'data:image' not in json.dumps(requests), 'Images leaked into text-only Builder context'
        if protocol_failure:
            assert store.get('contract-op')['calls']==5
            assert json.loads((store.root/'jobs'/'contract'/'tool-mode.json').read_text())['mode']=='text'
            assert len([e for e in store.events('contract-op') if e['type']=='tool_mode_fallback'])==1


def check_batched_history():
    """Execute a native batch, then switch protocols without replaying it."""
    from coder_repository import Repository
    from coder_worker_runtime import WorkerStore
    import coder_inference
    import openhands_worker as worker
    worker._ensure_sdk()
    with tempfile.TemporaryDirectory(prefix='daedalus-sdk-batch-') as temporary:
        root=Path(temporary);project=root/'project';project.mkdir()
        repository=Repository(project,root/'repository');store=WorkerStore(root/'worker')
        store.create('batch-op','batch','code',{'settings':{**DEFAULTS,'openhands_num_ctx':65536},'policy_version':3,
            'task':'Append first, second and third to actions.log, once each.','model':'qwen3-coder:30b',
            'ollama_url':'http://127.0.0.1:1','seconds_remaining':60,'calls_remaining':10,'request_key':'batch'})
        store.update('batch-op',status='running',started=time.time());requests=[]
        def transport(self,*,messages,**kwargs):
            requests.append((messages,kwargs))
            if len(requests)==1:
                calls=[{'id':'batch-'+word,'type':'function','function':{'name':'terminal','arguments':json.dumps({
                    'command':"printf '%s\\n' "+word+' >> '+shlex.quote(str(project/'actions.log')),'security_risk':'LOW'})}}
                    for word in ('first','second','third')]
                return ModelResponse(choices=[{'index':0,'finish_reason':'tool_calls','message':{'role':'assistant','content':'Append three lines.','tool_calls':calls}}])
            assert sorted((project/'actions.log').read_text().splitlines())==['first','second','third']
            if len(requests)==2:raise RuntimeError('Ollama_chatException XML syntax error on line 7')
            assert len(requests)==3 and not kwargs.get('tools')
            # Conversion must retain all executed calls and their observations.
            serialized=json.dumps(messages)
            for word in ('first','second','third'):assert word in serialized
            return ModelResponse(choices=[{'index':0,'finish_reason':'stop','message':{'role':'assistant',
                'content':'<function=finish>\n<parameter=message>Verified.</parameter>\n</function>'}}])
        with patch.object(worker,'_check_tool_support',return_value=True), \
             patch.object(worker,'_model_capabilities',return_value=[]), \
             patch.object(coder_inference,'ensure_context'), \
             patch.object(coder_inference,'require_local_model',return_value={'capabilities':[]}), \
             patch.object(worker._LLM,'_transport_call',transport):
            result=run_coder(store,'batch-op',repository)
        assert result['agent_finished'] and len(requests)==3 and store.get('batch-op')['calls']==3
        assert sorted((project/'actions.log').read_text().splitlines())==['first','second','third']
        assert len([e for e in store.events('batch-op') if e['type']=='tool_mode_fallback'])==1


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
    check_execution(protocol_failure=True)
    check_execution(policy_version=4)
    check_execution(policy_version=5)
    check_batched_history()
    print("Pinned SDK serialization, fallback editing, and completion contracts passed; no inference requests made.")


if __name__ == "__main__":
    main()
