"""The checks a chat request passes before a model is called, run through the real chat loop."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import database as db
from agents import chat
from main import ChatRequest


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'chat.db'))
    monkeypatch.setattr(chat, '_suggest_workspace_memories', AsyncMock())
    calls = []

    def handle(request):
        if request.url.path == '/api/show':
            return httpx.Response(200, json={'capabilities': ['completion']})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'fixture', 'digest': 'fixture'}]})
        if request.url.path == '/api/ps':
            return httpx.Response(200, json={'models': []})
        assert request.url.path == '/api/chat', request.url
        calls.append(json.loads(request.content))
        packets = [{'message': {'role': 'assistant', 'content': ' and the rest.'}, 'done': False},
                   {'message': {'content': ''}, 'done': True, 'done_reason': 'stop', 'prompt_eval_count': 12,
                    'eval_count': 4, 'eval_duration': 1000000}]
        return httpx.Response(200, text=''.join(json.dumps(p) + '\n' for p in packets))

    async def run(messages, conversation_id='c', **request):
        req = ChatRequest(conversation_id=conversation_id, model='fixture', messages=messages, tool_ids=[],
                          use_memories=False, think_budget=0, **request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            return [json.loads(s[5:]) async for s in chat.chat_stream_generate(
                req, http, SimpleNamespace(emit=AsyncMock()), {}, {})]

    return run, calls


def errors(events):
    return [e['error'] for e in events if e['type'] == 'error']


def test_the_first_event_names_both_saved_rows(harness):
    # The browser never learned the id of the message it had just sent: deleting it only hid it until the
    # next reload, and other actions looked rows up by position.
    run, _ = harness
    async def scenario():
        await db.init_db()
        await db.create_conversation('c', model='fixture')
        events = await run([{'role': 'user', 'content': 'Hello there'}], turn='new')
        init = next(e for e in events if e['type'] == 'init')
        rows = {m['id']: m['role'] for m in (await db.get_conversation('c'))['messages']}
        assert rows[init['user_message_id']] == 'user' and rows[init['message_id']] == 'assistant'
    asyncio.run(scenario())


def test_a_chat_that_is_not_the_callers_is_refused_before_any_model_call(harness):
    run, calls = harness
    async def scenario():
        await db.init_db()
        await db.create_conversation('c', model='fixture')
        assert 'no longer exists' in errors(await run([{'role': 'user', 'content': 'hi'}], conversation_id='conv-gone'))[0]
        token = db.set_current_user_id('someone-else')
        try:
            assert errors(await run([{'role': 'user', 'content': 'hi'}]))
        finally:
            db.reset_current_user_id(token)
        assert not calls and len((await db.get_conversation('c'))['messages']) == 0
        # Ghost and offline chats have no saved row by design.
        assert not errors(await run([{'role': 'user', 'content': 'hi'}], conversation_id='ghost-1', ephemeral=True))
    asyncio.run(scenario())


def test_continue_is_refused_for_a_reply_of_another_chat(harness):
    run, calls = harness
    async def scenario():
        await db.init_db()
        await db.create_conversation('c', model='fixture')
        await db.create_conversation('other', model='fixture')
        await db.add_message('other', 'user', 'their question')
        foreign = await db.add_message('other', 'assistant', 'their private answer')
        await db.add_message('c', 'user', 'my question')
        mine = await db.add_message('c', 'assistant', 'My answer starts')
        history = [{'role': 'user', 'content': 'my question'}, {'role': 'assistant', 'content': 'My answer starts'}]
        for bad in (foreign, 999999):       # a non-numeric id is rejected by the request model (HTTP 422)
            assert errors(await run(history, continue_message_id=bad)), bad
        assert not calls
        other = (await db.get_conversation('other'))['messages'][-1]
        assert other['content'] == 'their private answer'
        events = await run(history, continue_message_id=mine)
        assert not errors(events) and len(calls) == 1
        saved = next(m for m in (await db.get_conversation('c'))['messages'] if m['id'] == mine)
        assert saved['content'] == 'My answer starts and the rest.'
        assert saved['metadata']['model'] == 'fixture' and saved['metadata']['truncated'] is False
    asyncio.run(scenario())
