"""Chat fixes from the 2026-11-15 audit: the window a model really has, strict chat templates, replies a
restart left running, and a failed reply's context reading.

What the live server showed: ``lstep/neuraldaredevil-8b`` (own limit 8,192) was asked for 32,768 and
loaded at 8,192; two "Qwen3.8 uncensored" builds failed every chat with "System message must be at the
beginning"; two replies had been "running" for months. Run through the real chat loop against a fixture
model where the loop matters.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import chat_context
import chat_delivery
import chat_payload
import chat_recovery
import config
import context_inspector
import database as db
import model_limits
import reasoning
from agents import chat
from events import EventBus
from main import ChatRequest

REFUSAL = ('{"error":{"code":500,"message":"\\n------------\\nWhile executing CallExpression at line 106, column 32 '
           'in source:\\n...first %}\\n {{- raise_exception(\'System message must be at the beginning.\') }}\\n'
           'Error: Jinja Exception: System message must be at the beginning.","type":"server_error"}}')


def packets(*items):
    return httpx.Response(200, text=''.join(json.dumps(item) + '\n' for item in items))


def answer(text='Hello there.'):
    return packets({'message': {'role': 'assistant', 'content': text}, 'done': False},
                   {'message': {'content': ''}, 'done': True, 'done_reason': 'stop', 'prompt_eval_count': 900,
                    'eval_count': 30, 'eval_duration': 1000000})


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'chat.db'))
    monkeypatch.setattr(chat, '_suggest_workspace_memories', AsyncMock())
    reasoning._cache.clear()
    model_limits._cache.clear()
    chat_payload.forget_all()
    state = SimpleNamespace(calls=[], limit=0, show_status=200, strict=False, always_refuse=False,
                            loaded=0, bus=EventBus())

    def handle(request):
        if request.url.path == '/api/show':
            info = {'general.architecture': 'llama', 'llama.context_length': state.limit} if state.limit else {}
            return httpx.Response(state.show_status, json={'capabilities': ['completion'], 'model_info': info})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'fixture', 'digest': 'fixture'}]})
        if request.url.path == '/api/ps':
            models = [{'name': 'fixture', 'context_length': state.loaded}] if state.loaded else []
            return httpx.Response(200, json={'models': models})
        assert request.url.path == '/api/chat', request.url
        payload = json.loads(request.content)
        state.calls.append(payload)
        roles = [message['role'] for message in payload['messages']]
        if state.always_refuse or (state.strict and 'system' in roles[1:]):
            return httpx.Response(500, json={'error': REFUSAL})
        return answer()

    async def run(messages=None, **request):
        await db.init_db()
        if not await db.get_conversation('c'):
            await db.create_conversation('c', model='fixture')
        req = ChatRequest(conversation_id='c', model='fixture',
                          messages=messages or [{'role': 'user', 'content': 'Hello'}],
                          tool_ids=[], use_memories=False, think_budget=0, turn='new', **request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            events = [json.loads(s[5:]) async for s in chat.chat_stream_generate(req, http, state.bus, {}, {})]
        state.saved = (await db.get_conversation('c'))['messages'][-1]
        return events

    return run, state


def kinds(events):
    return [event['type'] for event in events]


# ── The window a model really has ──────────────────────────────────────────────

def test_a_model_with_a_smaller_limit_is_asked_for_its_own_window(harness):
    run, state = harness
    state.limit = 8192
    events = asyncio.run(run(num_ctx=32768))
    assert state.calls[0]['options']['num_ctx'] == 8192
    reading = next(event for event in events if event['type'] == 'done')['context']
    assert reading['window'] == 8192 and reading['asked'] == 32768
    assert state.saved['metadata']['context']['window'] == 8192


def test_a_model_with_a_smaller_limit_is_compacted_at_its_own_window(harness, monkeypatch):
    run, state = harness
    state.limit = 8192
    seen = []

    async def spy(**kwargs):
        seen.append(kwargs['options']['num_ctx'])
        yield {'messages': kwargs['messages'], 'history': list(kwargs['history']), 'event': None}
    monkeypatch.setattr(chat_context, 'compact', spy)
    asyncio.run(run(num_ctx=32768, messages=[{'role': 'user', 'content': 'Hello', 'id': 1}]))
    assert seen == [8192]          # 32,768 here meant a chat that never compacted before the model's window filled


def test_a_model_with_a_larger_limit_keeps_the_window_that_was_asked_for(harness):
    run, state = harness
    state.limit = 262144
    events = asyncio.run(run(num_ctx=32768))
    assert state.calls[0]['options']['num_ctx'] == 32768
    assert next(event for event in events if event['type'] == 'done')['context']['window'] == 32768


def test_a_model_lookup_that_fails_changes_nothing(harness):
    run, state = harness
    state.limit, state.show_status = 8192, 500
    events = asyncio.run(run(num_ctx=32768))
    assert state.calls[0]['options']['num_ctx'] == 32768 and 'done' in kinds(events)


def test_a_cloud_model_is_never_looked_up():
    async def run():
        http = SimpleNamespace(post=AsyncMock(side_effect=AssertionError('a cloud model has no Ollama metadata')))
        return [await model_limits.context_limit(http, name) for name in ('openai:gpt-5', 'anthropic:claude-opus-5', 'custom:x', 'auto', '')]
    assert asyncio.run(run()) == [0, 0, 0, 0, 0]


def test_the_limit_is_read_from_the_model_metadata_and_junk_is_no_limit():
    assert model_limits.from_show({'model_info': {'general.architecture': 'qwen3', 'qwen3.context_length': 40960}}) == 40960
    assert model_limits.from_show({'model_info': {'llama.context_length': 'many'}}) == 0
    assert model_limits.from_show({}) == 0 and model_limits.from_show(None) == 0
    assert model_limits.effective(32768, 8192) == 8192 and model_limits.effective(8192, 32768) == 8192
    assert model_limits.effective(32768, 0) == 32768 and model_limits.effective('x', 5) == 0


def test_the_window_ollama_loaded_is_kept_for_the_whole_reply():
    meter = chat_context.TurnMeter()
    meter.info({'round': 0, 'window': 32768, 'asked': 32768, 'prompt_estimate': 1000})
    meter.usage({'prompt_tokens': 800, 'gen_tokens': 20})
    meter.info({'round': 0, 'loaded_window': 8192})
    assert meter.window == 8192
    meter.info({'round': 1, 'window': 32768, 'prompt_estimate': 1200})      # the next call's event used to put 32,768 back
    assert meter.window == 8192
    reading = meter.reading('An answer.')
    assert reading['window'] == 8192 and reading['asked'] == 32768


# ── Strict chat templates ──────────────────────────────────────────────────────

def test_a_template_that_allows_one_system_message_gets_one(harness):
    run, state = harness
    state.strict = True
    events = asyncio.run(run())
    assert 'error' not in kinds(events) and 'done' in kinds(events)
    assert len(state.calls) == 2                                   # refused once, then sent merged
    refused, sent = state.calls
    assert [m['role'] for m in refused['messages']].count('system') > 1
    assert [m['role'] for m in sent['messages']].count('system') == 1 and sent['messages'][0]['role'] == 'system'
    for message in refused['messages']:                            # nothing the model was told is lost
        if message['role'] == 'system':
            assert message['content'].strip() in sent['messages'][0]['content']
    assert state.saved['content'].strip() == 'Hello there.'
    asyncio.run(run())                                             # the next turn goes straight to the merged form
    assert len(state.calls) == 3


def test_a_model_that_still_refuses_is_reported_not_retried_forever(harness):
    run, state = harness
    state.always_refuse = True
    events = asyncio.run(run())
    assert len(state.calls) == 2 and 'error' in kinds(events)


def test_other_models_keep_their_system_messages_apart(harness):
    run, state = harness
    asyncio.run(run())
    assert len(state.calls) == 1
    assert [m['role'] for m in state.calls[0]['messages']].count('system') > 1


def test_merging_keeps_every_instruction_and_the_order_of_the_conversation():
    first, late = {'role': 'system', 'content': 'Be brief.'}, {'role': 'system', 'content': 'Documents: cite them.'}
    question, reply = {'role': 'user', 'content': 'Hi'}, {'role': 'assistant', 'content': 'Hello'}
    merged = chat_payload.single_system([first, {'role': 'system', 'content': '  '}, question, reply, late])
    assert merged[0] == {'role': 'system', 'content': 'Be brief.\n\nDocuments: cite them.'}
    assert merged[1] is question and merged[2] is reply and len(merged) == 3
    plain = [first, question]
    assert chat_payload.single_system(plain) is plain              # already one system message, first
    assert chat_payload.single_system([question]) == [question]


def test_only_the_template_refusal_asks_for_one_system_message():
    assert chat_payload.wants_single_system(REFUSAL)
    assert chat_payload.wants_single_system('Jinja Exception: System message must be first')
    assert not chat_payload.wants_single_system('model "x" does not support tools')
    assert not chat_payload.wants_single_system('') and not chat_payload.wants_single_system(None)


# ── Replies a restart left running ─────────────────────────────────────────────

def test_a_reply_left_running_by_a_restart_can_be_continued(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'chat.db'))

    async def run():
        await db.init_db()
        await db.create_conversation('c', model='fixture')
        await db.add_message('c', 'user', 'Tell me a story')
        cut = await db.add_message('c', 'assistant', 'Once upon a', {'in_progress': True, 'delivery_status': 'running', 'server_persists': True})
        old = await db.add_message('c', 'assistant', 'An old half', {'in_progress': True})
        fine = await db.add_message('c', 'assistant', 'The end.', {'in_progress': False, 'delivery_status': 'completed'})
        swept = await chat_recovery.sweep()
        rows = {row['id']: row for row in (await db.get_conversation('c'))['messages']}
        return swept, rows[cut]['metadata'], rows[old]['metadata'], rows[fine]['metadata'], await chat_recovery.sweep()
    swept, cut, old, fine, again = asyncio.run(run())
    assert swept == 2 and again == 0
    assert cut['delivery_status'] == 'interrupted' and cut['in_progress'] is False and cut['server_persists'] is True
    assert old['delivery_status'] == 'interrupted' and old['in_progress'] is False
    assert fine == {'in_progress': False, 'delivery_status': 'completed'}


# ── A failed reply's reading, and the record of what was sent ──────────────────

def test_a_failed_reply_sends_its_context_reading(monkeypatch):
    monkeypatch.setattr(db, 'save_chat_delivery', AsyncMock())
    monkeypatch.setattr(db, 'record_token_usage', AsyncMock())

    async def source():
        for event in ({'type': 'init', 'message_id': 42},
                      {'type': 'context_info', 'round': 0, 'window': 8192, 'prompt_estimate': 1000, 'turn_only': 0, 'ratio': 0, 'fixed': 500},
                      {'type': 'token', 'content': 'Half an answer'},
                      {'type': 'usage_round', 'prompt_tokens': 800, 'gen_tokens': 20},
                      {'type': 'error', 'error': 'model stopped'}):
            yield chat_delivery.sse(event)

    async def run():
        req = SimpleNamespace(conversation_id='c', model='local', persona_id='', ephemeral=False)
        return [json.loads(chunk[5:]) async for chunk in chat_delivery.deliver(source(), req)]
    failed = next(event for event in asyncio.run(run()) if event['type'] == 'error')
    assert failed['context']['used'] >= 800 and failed['context']['window'] == 8192     # the pill used to stay mid-reply


def test_a_database_error_while_recording_what_was_sent_does_not_fail_the_reply(harness, monkeypatch):
    run, state = harness
    monkeypatch.setattr(context_inspector, 'capture', AsyncMock(side_effect=RuntimeError('database is locked')))
    monkeypatch.setattr(context_inspector, 'record_usage', AsyncMock(side_effect=RuntimeError('database is locked')))
    events = asyncio.run(run())
    assert 'error' not in kinds(events) and 'done' in kinds(events)
    assert state.saved['content'].strip() == 'Hello there.'


def test_a_reply_cut_by_a_restart_keeps_what_was_written(monkeypatch):
    # A graceful stop mid-reply left the row "running" with no text: the final save never ran.
    save = AsyncMock()
    monkeypatch.setattr(db, 'save_chat_delivery', save)
    monkeypatch.setattr(db, 'record_token_usage', AsyncMock())
    monkeypatch.setattr(chat_delivery, 'PARTIAL_SAVE_SECONDS', 0)
    seen = []

    async def source():
        yield chat_delivery.sse({'type': 'init', 'message_id': 42})
        for piece in ('Once ', 'upon ', 'a time'):
            yield chat_delivery.sse({'type': 'token', 'content': piece})
        seen.append([(call.args[1], call.args[2]['delivery_status']) for call in save.call_args_list])
        raise asyncio.CancelledError()            # the service goes away here

    async def run():
        req = SimpleNamespace(conversation_id='c', model='local', persona_id='', ephemeral=False)
        try:
            async for _ in chat_delivery.deliver(source(), req):
                pass
        except asyncio.CancelledError:
            pass
    asyncio.run(run())
    assert ('Once upon a time', 'running') in seen[0]          # on disk before anything went wrong


def test_a_partial_save_that_fails_does_not_fail_the_reply(monkeypatch):
    calls = []

    async def save(message_id, content, metadata):
        calls.append(metadata['delivery_status'])
        if metadata['delivery_status'] == 'running' and content:
            raise RuntimeError('database is locked')
    monkeypatch.setattr(db, 'save_chat_delivery', save)
    monkeypatch.setattr(db, 'record_token_usage', AsyncMock())
    monkeypatch.setattr(chat_delivery, 'PARTIAL_SAVE_SECONDS', 0)

    async def source():
        for event in ({'type': 'init', 'message_id': 42}, {'type': 'token', 'content': 'Hello'}, {'type': 'done', 'message_id': 42}):
            yield chat_delivery.sse(event)

    async def run():
        req = SimpleNamespace(conversation_id='c', model='local', persona_id='', ephemeral=False)
        return [json.loads(chunk[5:]) async for chunk in chat_delivery.deliver(source(), req)]
    events = asyncio.run(run())
    assert [event['type'] for event in events] == ['init', 'token', 'done'] and calls[-1] == 'completed'
