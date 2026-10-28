"""Provider contract fixtures: never call a paid API in this suite."""
import asyncio
import json
from unittest.mock import AsyncMock
import httpx
import model_providers as providers
import provider_tools
import reasoning


def test_signed_anthropic_reasoning_survives_next_tool_round(monkeypatch):
    monkeypatch.setattr(providers,'get_api_key',AsyncMock(return_value='fixture'))
    events=[
        {'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':''}},
        {'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'reasoning'}},
        {'type':'content_block_delta','index':0,'delta':{'type':'signature_delta','signature':'signed'}},
        {'type':'content_block_stop','index':0},
        {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':7}},
        {'type':'message_stop'},
    ]
    payloads=[]
    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200,text=''.join('data: '+json.dumps(e)+'\n\n' for e in events))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            return [e async for e in providers._stream_anthropic(http,'claude-sonnet-4-6',[{'role':'user','content':'hello'}],{'reasoning_level':'high','temperature':.7,'top_p':.9})]
    output=asyncio.run(run())
    assert payloads[0]['thinking']=={'type':'adaptive'}
    assert payloads[0]['output_config']=={'effort':'high'}
    assert 'temperature' not in payloads[0] and 'top_p' not in payloads[0]
    state=[e['item'] for e in output if e['type']=='provider_state']
    assert state==[{'type':'thinking','thinking':'reasoning','signature':'signed'}]
    _, messages=provider_tools.anthropic_messages_native([
        {'role':'assistant','content':'checking','provider_state':state,'tool_calls':[{'id':'call','function':{'name':'read_file','arguments':{}}}]},
        {'role':'tool','tool_call_id':'call','content':'result'}])
    assert messages[0]['content'][0]==state[0]
    assert messages[1]['content'][0]['tool_use_id']=='call'


def test_openai_encrypted_reasoning_is_returned_and_preserved(monkeypatch):
    monkeypatch.setattr(providers,'get_api_key',AsyncMock(return_value='fixture'))
    state={'type':'reasoning','id':'rs_1','summary':[],'encrypted_content':'opaque'}
    output_message={'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer','annotations':[]}]}
    payloads=[]
    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200,text=''.join('data: '+json.dumps({'type':'response.output_item.done','item':item})+'\n\n' for item in [state,output_message]))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            return [e async for e in providers._stream_openai(http,'gpt-5.1',[{'role':'user','content':'hello'}],{'reasoning_level':'high','temperature':.7})]
    events=asyncio.run(run())
    assert payloads[0]['reasoning']=={'effort':'high'}
    assert payloads[0]['store'] is False
    assert payloads[0]['include']==['reasoning.encrypted_content']
    assert 'temperature' not in payloads[0]
    items=provider_tools.openai_input_native([{'role':'assistant','content':'answer','provider_state':[e['item'] for e in events if e['type']=='provider_state']}])
    assert items==[state,output_message]


def test_unknown_custom_and_boolean_models_keep_review_ladder():
    assert reasoning.cloud_capabilities('openai:gpt-5.1-2025-11-13')['mode']=='native'
    for model in ('custom:gpt-5.1','openai:gpt-5.1-unknown','anthropic:unknown'):
        assert reasoning.cloud_capabilities(model)['mode']=='review'


def test_capability_cache_invalidates_when_model_digest_changes():
    reasoning._cache.clear()
    digest='first';shows=[]
    def handle(request):
        if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'alias','digest':digest}]})
        shows.append(digest)
        return httpx.Response(200,json={'thinking':{'values':['low','high'] if digest=='first' else [False,True],'default':'high' if digest=='first' else True}})
    async def run():
        nonlocal digest
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            assert (await reasoning.capabilities(http,'alias'))['mode']=='native'
            assert (await reasoning.capabilities(http,'alias'))['mode']=='native'
            digest='changed'
            assert (await reasoning.capabilities(http,'alias'))['mode']=='review'
    asyncio.run(run())
    assert shows==['first','changed']
