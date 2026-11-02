"""Tests against the pinned SDK, not mocks of its persistence model."""
import importlib.metadata
import uuid
import pytest

try: installed = importlib.metadata.version('openhands-sdk')
except importlib.metadata.PackageNotFoundError: installed = ''
pytestmark = pytest.mark.skipif(installed != '1.59.0', reason='Run in the pinned candidate environment')


def test_custom_tools_survive_conversation_reload(tmp_path):
    from openhands.sdk import Agent, Conversation, LLM
    from openhands.sdk.tool import Tool
    from coder_ui_tool import UITool
    from coder_engine_tools import EngineToolSet
    agent=Agent(llm=LLM(model='ollama_chat/local',base_url='http://127.0.0.1:1',max_input_tokens=65536,
                       max_output_tokens=8192,reasoning_effort=None),
                tools=[Tool(name=UITool.name,params={'directory':str(tmp_path/'ui')}),
                       Tool(name=EngineToolSet.name,params={'output_directory':str(tmp_path/'output')})])
    identity=uuid.uuid4(); root=tmp_path/'project'; root.mkdir()
    for _ in range(2):
        conversation=Conversation(agent=agent,workspace=root,conversation_id=identity,
                                  persistence_dir=tmp_path/'sessions',visualizer=None)
        try:
            conversation.send_message('Inspect the project, but do not run inference in this test.')
        finally: conversation.close()


def test_terminal_hard_deadline_survives_yield_and_kills_descendants(tmp_path):
    import time
    from coder_engine_terminal import TerminalLease
    from openhands.tools.terminal.definition import TerminalAction
    marker=tmp_path/'escaped'
    terminal=TerminalLease(tmp_path,tmp_path/'output',6)
    try:
        first=terminal(TerminalAction(command=f'(sleep 12; touch {marker}) & sleep 40',timeout=1))
        if first.exit_code != 124:
            assert first.exit_code == -1
            time.sleep(7)
            result=terminal(TerminalAction(command='',is_input=True,timeout=1))
            assert result.exit_code==124
        time.sleep(6)
        assert not marker.exists()
        result=terminal(TerminalAction(command='echo recovered',timeout=2))
        assert result.exit_code==0 and 'recovered' in result.text
    finally:terminal.close()


def test_counted_condenser_survives_reload(tmp_path,monkeypatch):
    import os
    if not os.environ.get('DAEDALUS_TOKENIZER_DIR'):pytest.skip('Requires pinned assets')
    from coder_engine_condenser import EngineCondenser,CountedLLM
    from openhands.sdk import Agent,Conversation,Message,TextContent
    from openhands.sdk.context.condenser.base import CondenserBase
    llm=CountedLLM(model='ollama_chat/local',base_url='http://127.0.0.1:1',max_input_tokens=65536,max_output_tokens=4096,reasoning_effort=None)
    condenser=EngineCondenser(llm=llm,max_tokens=40000)
    restored=CondenserBase.model_validate(condenser.model_dump())
    assert isinstance(restored,EngineCondenser)
    count=llm.get_token_count([Message(role='user',content=[TextContent(text='Hello')])])
    assert count>0
    root=tmp_path/'source';root.mkdir();identity=uuid.uuid4()
    for _ in range(2):
        conv=Conversation(agent=Agent(llm=llm,condenser=condenser),workspace=root,conversation_id=identity,persistence_dir=tmp_path/'sessions',visualizer=None)
        conv.close()


def test_request_receipts_survive_restart_without_repeating_instructions(tmp_path):
    from openhands.sdk import Agent, Conversation, LLM
    from coder_engine_runtime import send_request
    identity=uuid.uuid4();root=tmp_path/'project';root.mkdir()
    agent=Agent(llm=LLM(model='ollama_chat/local',base_url='http://127.0.0.1:1',reasoning_effort=None))
    for cycle in range(2):
        conversation=Conversation(agent=agent,workspace=root,conversation_id=identity,
                                  persistence_dir=tmp_path/'sessions',visualizer=None)
        try:
            assert send_request(conversation,'first','FULL IMPLEMENTATION','unused') == (cycle==0)
            assert send_request(conversation,'repair','DO NOT DUPLICATE','CONCRETE FAILURE') == (cycle==0)
            events=[e for e in conversation.state.events if getattr(e,'sender','') in {'daedalus-operation:first','daedalus-operation:repair'}]
            assert len(events)==2
            text=' '.join(str(e.model_dump()) for e in events)
            assert text.count('FULL IMPLEMENTATION')==1 and 'CONCRETE FAILURE' in text and 'DO NOT DUPLICATE' not in text
        finally:conversation.close()


def test_long_conversation_output_limit_recovery_recounts_real_template(tmp_path,monkeypatch):
    import os
    if not os.environ.get('DAEDALUS_TOKENIZER_DIR'):pytest.skip('Requires pinned assets')
    from .test_engine_recovery import body, response
    from .test_engine_reliability import operation
    from coder_engine_transport import infer
    from coder_engine_tokens import count_request
    from context_policy import DEFAULTS, resolve
    store=operation(tmp_path)
    store.update('op',payload={**store.get('op')['payload'],'engine_behavior_version':1})
    request=body();request['messages']=[{'role':'user' if i%2==0 else 'assistant',
        'content':('Observed source and behavioral checks. '*130)+str(i)} for i in range(30)]
    requests=[]
    def send(_,current):
        requests.append(current.copy())
        return response('write','length') if len(requests)==1 else response('write')
    policy=resolve('builder',{**DEFAULTS,'openhands_num_ctx':65536,'daedalus_role_outputs':{'builder':16384}})
    assert 20000<count_request(request)<policy.input_budget
    result=infer(store,'op',request,'builder',policy,send)
    assert result['done_reason']=='stop' and store.get('op')['calls']==2
    events=[e['data'] for e in store.events('op') if e['type']=='model_call']
    assert events[1]['prompt_tokens_estimate']>events[0]['prompt_tokens_estimate']
    assert events[1]['prompt_tokens_estimate']<=policy.input_budget
