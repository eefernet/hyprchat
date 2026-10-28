"""Run the actual chat loop and persistence against deterministic Ollama streams."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
import config
import database as db
import reasoning
from main import ChatRequest
from agents import chat


@pytest.mark.parametrize('values,level,rounds,expected_calls,review_behavior',[
    ([False,'low','medium','xhigh'],'medium',3,1,'complete'),
    ([False,True],None,2,3,'complete'),
    ([False],None,1,2,'complete'),
    ([False,True],None,1,2,'truncated'),
    ([False,True],None,1,2,'failed'),
    ([False,True],None,1,3,'action'),
])
def test_chat_uses_native_effort_or_review_and_saves_final_answer(tmp_path,monkeypatch,values,level,rounds,expected_calls,review_behavior):
    monkeypatch.setattr(db,'DATABASE_PATH',str(tmp_path/'chat.db'))
    monkeypatch.setattr(chat,'_suggest_workspace_memories',AsyncMock())
    monkeypatch.setattr(config,'MAX_AGENT_ROUNDS',8)
    reasoning._cache.clear()
    execute = AsyncMock(side_effect=AssertionError('Review executed an action'))
    monkeypatch.setattr(chat,'exec_tool',execute)
    payloads=[]
    def handle(request):
        if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'fixture','digest':'fixture'}]})
        if request.url.path=='/api/show':return httpx.Response(200,json={'thinking':{'values':values,'default':values[-1]},'capabilities':['completion']})
        assert request.url.path=='/api/chat'
        payloads.append(json.loads(request.content))
        if len(payloads)==2 and review_behavior=='failed':
            return httpx.Response(503,text='fixture provider failure')
        if len(payloads)==2 and review_behavior=='action':
            return httpx.Response(200,text=json.dumps({'message':{'role':'assistant','content':'','tool_calls':[{'function':{'name':'run_shell','arguments':{'command':'touch forbidden'}}}]},'done':True,'done_reason':'stop','eval_count':4,'prompt_eval_count':12})+'\n')
        text='The initial answer.' if len(payloads)==1 else 'Correct.'
        return httpx.Response(200,text=json.dumps({'message':{'role':'assistant','content':text},'done':True,'done_reason':'length' if len(payloads)>1 and review_behavior=='truncated' else 'stop','prompt_eval_count':12,'eval_count':4,'eval_duration':1000000})+'\n')
    async def run():
        await db.init_db();await db.create_conversation(id='c',title='test')
        req=ChatRequest(conversation_id='c',model='fixture',messages=[{'role':'user','content':'Say hello.'}],tool_ids=['codeagent'] if review_behavior=='action' else [],use_memories=False,effort_mode='auto',reasoning_model='fixture',reasoning_level=level,effort_rounds=rounds,think_budget=0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            events=[json.loads(e[5:]) async for e in chat.chat_stream_generate(req,http,SimpleNamespace(emit=AsyncMock()),{}, {})]
        assert bool([e for e in events if e.get('type')=='error']) == (review_behavior=='failed')
        execute.assert_not_called()
        assert len(payloads)==expected_calls
        if expected_calls==1:assert payloads[0]['think']=='medium'
        else:assert all(p['think'] is False for p in payloads)
        conv=await db.get_conversation('c');message=conv['messages'][-1]
        assert message['content']==('The initial answer.' if expected_calls==1 or review_behavior in {'truncated','failed'} else 'Correct.')
        assert message['metadata']['in_progress'] is False
        assert len(message['metadata']['usage_calls'])==expected_calls-(review_behavior=='failed')
        assert message['metadata']['total_gen_tokens']==4*(expected_calls-(review_behavior=='failed'))
    asyncio.run(run())
