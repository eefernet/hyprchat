"""The tool-round limit of a chat reply, and the answer a reply writes when it reaches it.

Seen live 2026-11-14: three replies in a row stopped at 12 rounds, at 6 % of the context window, and
each said only "Ran out of round budget". Run through the real chat loop against a fixture model.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import chat_rounds
import config
import database as db
import reasoning
from agents import chat
from events import EventBus
from main import ChatRequest

ANSWER = 'Here is what I found: three files.'


def packets(*items):
    return httpx.Response(200, text=''.join(json.dumps(item) + '\n' for item in items))


def done(**extra):
    return {'message': {'content': ''}, 'done': True, 'done_reason': 'stop', 'prompt_eval_count': 900,
            'eval_count': 30, 'eval_duration': 1000000, **extra}


def tool_round(number):
    call = {'function': {'name': 'execute_code', 'arguments': {
        'description': f'Step {number}', 'code': f'print({number})', 'language': 'python'}}}
    return packets({'message': {'role': 'assistant', 'content': '', 'tool_calls': [call]}, 'done': False}, done())


def is_wrap_up(payload):
    last = payload['messages'][-1]
    return last['role'] == 'user' and 'tool rounds' in last['content'] and 'Do not call a tool' in last['content']


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'chat.db'))
    monkeypatch.setattr(chat, '_suggest_workspace_memories', AsyncMock())
    monkeypatch.setattr(config, 'MAX_AGENT_ROUNDS', 2)
    reasoning._cache.clear()
    state = SimpleNamespace(calls=[], ran=[], wrap=lambda: packets(
        {'message': {'role': 'assistant', 'content': ANSWER}, 'done': False}, done()), bus=EventBus())

    async def fake_tool(http, events, name, args, conv_id, *rest, **more):
        state.ran.append(args.get('code'))
        await events.emit(conv_id, 'tool_end', {'tool': name, 'status': 'OK'})
        await events.emit(conv_id, 'code_output', {'stdout': 'x', 'success': True})
        return '**SUCCESS** | python | exit 0'
    monkeypatch.setattr(chat, 'exec_tool', fake_tool)

    def handle(request):
        if request.url.path == '/api/show':
            return httpx.Response(200, json={'capabilities': ['completion', 'tools']})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'fixture', 'digest': 'fixture'}]})
        if request.url.path == '/api/ps':
            return httpx.Response(200, json={'models': []})
        assert request.url.path == '/api/chat', request.url
        payload = json.loads(request.content)
        state.calls.append(payload)
        scripted = state.reply(payload, len(state.calls)) if getattr(state, 'reply', None) else None
        if scripted is not None:
            return scripted
        return state.wrap() if is_wrap_up(payload) else tool_round(len(state.calls))

    async def run(**request):
        await db.init_db()
        await db.create_conversation('c', model='fixture')
        state.queue = await state.bus.subscribe('c')
        req = ChatRequest(conversation_id='c', model='fixture', messages=[{'role': 'user', 'content': 'Look at the code base'}],
                          tool_ids=[], use_memories=False, think_budget=0, turn='new', **request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            events = [json.loads(s[5:]) async for s in chat.chat_stream_generate(req, http, state.bus, {}, {})]
        state.saved = (await db.get_conversation('c'))['messages'][-1]
        return events

    return run, state


def text_of(events):
    text = ''
    for event in events:
        if event['type'] == 'token':
            text += event.get('content', '')
        elif event['type'] == 'clear':
            text = text[:event.get('keep', 0)]
    return text


def test_a_plain_chat_gets_forty_rounds_by_default(monkeypatch):
    assert chat_rounds.DEFAULT == 40
    monkeypatch.setattr(config, 'MAX_AGENT_ROUNDS', None)
    assert chat_rounds.limit(daedalus=False) == 40
    monkeypatch.setattr(config, 'MAX_AGENT_ROUNDS', 25)
    assert chat_rounds.limit(daedalus=False) == 25


def test_the_round_limit_setting_is_clamped_and_survives_junk():
    assert chat_rounds.setting(60) == 60 and chat_rounds.setting('20') == 20
    assert chat_rounds.setting(1) == 4 and chat_rounds.setting(100000) == 200
    assert chat_rounds.setting('many') == 40 and chat_rounds.setting([]) == 40


def test_daedalus_keeps_its_own_round_limit(monkeypatch):
    monkeypatch.setattr(config, 'MAX_AGENT_ROUNDS', 60)
    monkeypatch.setattr(config, 'MAX_AGENT_ROUNDS_CODER', 30)
    assert chat_rounds.limit(daedalus=True) == 30 and chat_rounds.limit(daedalus=False) == 60


def test_a_reply_that_hits_the_round_limit_ends_with_an_answer_not_a_warning(harness):
    run, state = harness
    events = asyncio.run(run())
    assert len(state.ran) == 2 and len(state.calls) == 3          # two tool rounds, then the answer
    assert text_of(events).strip() == ANSWER
    assert 'Ran out of round budget' not in text_of(events)
    finished = next(e for e in events if e['type'] == 'done')
    assert finished['round_limit'] == 2
    assert state.saved['content'].strip() == ANSWER
    assert state.saved['metadata']['round_limit'] == 2 and state.saved['metadata']['delivery_status'] == 'completed'
    assert len(state.saved['metadata']['usage_calls']) == 3       # the answer call is counted like any other


def test_the_wrap_up_call_keeps_the_tool_definitions_and_adds_one_instruction(harness):
    run, state = harness
    asyncio.run(run())
    before, wrap = state.calls[1], state.calls[2]
    assert [tool['function']['name'] for tool in wrap['tools']] == [tool['function']['name'] for tool in before['tools']]
    assert is_wrap_up(wrap) and 'all 2 tool rounds' in wrap['messages'][-1]['content']
    # Everything the model had is still there: its two tool calls and both results.
    assert sum(1 for m in wrap['messages'] if m['role'] == 'tool' and 'SUCCESS' in m['content']) == 2
    assert wrap['options'] == before['options']                   # same options: no model reload


def test_tool_calls_in_the_wrap_up_are_not_run(harness):
    run, state = harness
    state.wrap = lambda: tool_round(99)
    events = asyncio.run(run())
    assert len(state.ran) == 2 and '99' not in ''.join(state.ran)
    assert text_of(events).strip() == chat_rounds.NOTICE
    assert state.saved['content'].strip() == chat_rounds.NOTICE and state.saved['metadata']['round_limit'] == 2


def test_a_tool_call_written_as_text_in_the_wrap_up_is_taken_back(harness):
    run, state = harness
    markup = '<tool_call>{"name": "execute_code", "arguments": {"code": "print(1)", "language": "python"}}</tool_call>'
    state.wrap = lambda: packets({'message': {'role': 'assistant', 'content': markup}, 'done': False}, done())
    events = asyncio.run(run())
    assert len(state.ran) == 2
    assert text_of(events).strip() == chat_rounds.NOTICE and '<tool_call>' not in state.saved['content']


def test_a_failed_wrap_up_call_still_ends_the_reply(harness):
    run, state = harness
    state.wrap = lambda: httpx.Response(503, text='model unloaded')
    events = asyncio.run(run())
    assert not [e for e in events if e['type'] == 'error']
    assert text_of(events).strip() == chat_rounds.NOTICE
    assert next(e for e in events if e['type'] == 'done')['round_limit'] == 2


def test_text_written_before_the_limit_is_kept_when_the_wrap_up_fails(harness):
    run, state = harness
    spoken = iter(['First I will list the files.', 'Now the line counts.'])
    original = state.wrap
    def wrap():
        return packets({'message': {'role': 'assistant', 'content': 'Half an ans'}, 'done': False}, {'error': 'out of memory'})
    state.wrap = wrap

    def handle_round(number):
        call = {'function': {'name': 'execute_code', 'arguments': {'code': f'print({number})', 'language': 'python'}}}
        return packets({'message': {'role': 'assistant', 'content': next(spoken), 'tool_calls': [call]}, 'done': False}, done())
    globals()['tool_round'], previous = handle_round, tool_round
    try:
        events = asyncio.run(run())
    finally:
        globals()['tool_round'] = previous
    text = text_of(events)
    assert 'First I will list the files.' in text and 'Now the line counts.' in text
    assert 'Half an ans' not in text and text.strip().endswith(chat_rounds.NOTICE)


def test_the_round_limit_is_saved_and_the_browser_cannot_change_it(harness):
    run, state = harness
    asyncio.run(run())
    async def patch():
        await db.patch_message_presentation('c', state.saved['id'], metadata={'round_limit': 0, 'saved_events': []})
        return (await db.get_conversation('c'))['messages'][-1]['metadata']
    assert asyncio.run(patch())['round_limit'] == 2


def test_every_event_of_a_tool_call_in_the_loop_carries_one_id(harness):
    run, state = harness
    asyncio.run(run())
    seen = []
    while not state.queue.empty():
        seen.append(state.queue.get_nowait())
    by_call = {}
    for event in seen:
        if event['data'].get('tool') == 'execute_code' or event['type'] == 'code_output':
            by_call.setdefault(event['data'].get('tool_call_id'), []).append(event['type'])
    assert None not in by_call and len(by_call) == 2              # two calls, two ids
    for kinds in by_call.values():
        assert kinds[0] == 'tool_start' and {'tool_end', 'code_output'} <= set(kinds)
    started = [e['data']['status'] for e in seen if e['type'] == 'tool_start' and e['data'].get('tool') == 'execute_code']
    assert all('Step' in status for status in started)            # the model's description is the status line


def test_a_review_pass_that_runs_out_keeps_the_answer_without_a_wrap_up(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'chat.db'))
    monkeypatch.setattr(chat, '_suggest_workspace_memories', AsyncMock())
    monkeypatch.setattr(config, 'MAX_AGENT_ROUNDS', 2)
    reasoning._cache.clear()
    payloads = []

    def handle(request):
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'fixture', 'digest': 'fixture'}]})
        if request.url.path == '/api/show':
            return httpx.Response(200, json={'thinking': {'values': [False, True], 'default': True}, 'capabilities': ['completion']})
        if request.url.path == '/api/ps':
            return httpx.Response(200, json={'models': []})
        payloads.append(json.loads(request.content))
        text = 'The initial answer.' if len(payloads) == 1 else 'Checked.'
        return packets({'message': {'role': 'assistant', 'content': text}, 'done': True, 'done_reason': 'stop',
                        'prompt_eval_count': 12, 'eval_count': 4, 'eval_duration': 1000000})

    async def scenario():
        await db.init_db()
        await db.create_conversation('c', model='fixture')
        req = ChatRequest(conversation_id='c', model='fixture', messages=[{'role': 'user', 'content': 'Say hello.'}],
                          tool_ids=[], use_memories=False, effort_mode='auto', reasoning_model='fixture',
                          effort_rounds=3, think_budget=0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            events = [json.loads(e[5:]) async for e in chat.chat_stream_generate(req, http, SimpleNamespace(emit=AsyncMock()), {}, {})]
        assert not any(is_wrap_up(payload) for payload in payloads)
        assert not next(e for e in events if e['type'] == 'done').get('round_limit')
        saved = (await db.get_conversation('c'))['messages'][-1]
        assert saved['content'].strip() and chat_rounds.NOTICE not in saved['content']
        assert not saved['metadata'].get('round_limit')
    asyncio.run(scenario())


def test_inline_reasoning_is_not_shown_and_a_tag_still_arriving_is_held_back():
    assert chat_rounds.visible_text('<think>plan</think>The answer') == 'The answer'
    assert chat_rounds.visible_text('Start <think>unfinished') == 'Start '
    assert chat_rounds.visible_text('Value is 3 <', streaming=True) == 'Value is 3 '
    assert chat_rounds.visible_text('Value is 3 <', streaming=False) == 'Value is 3 <'
    assert chat_rounds.visible_text('a <b> c', streaming=True) == 'a <b> c'


def test_the_wrap_up_needs_room_for_a_reply():
    messages = [{'role': 'user', 'content': 'x' * 30000}]
    assert chat_rounds.fits(messages, None, 0, 0.8)                # cloud model: the window is not known here
    assert chat_rounds.fits(messages, None, 32768, 0.8)
    assert not chat_rounds.fits(messages, None, 8192, 0.8)


def test_a_model_that_goes_quiet_after_its_tools_ran_is_asked_for_the_answer(harness, monkeypatch):
    # Seen 2026-11-15 with a small model: three clean code runs, then an empty reply, and the turn ended
    # as "The model produced no text response".
    run, state = harness
    monkeypatch.setattr(config, 'MAX_AGENT_ROUNDS', 10)

    def reply(payload, number):
        last = payload['messages'][-1]
        if last['role'] == 'user' and last['content'].startswith('Your last reply was empty'):
            return packets({'message': {'role': 'assistant', 'content': ANSWER}, 'done': False}, done())
        return tool_round(number) if number <= 3 else packets(done(eval_count=0))
    state.reply = reply
    events = asyncio.run(run())
    assert len(state.ran) == 3 and len(state.calls) == 5          # three runs, the empty reply, the ask
    assert text_of(events).strip() == ANSWER
    assert not next(e for e in events if e['type'] == 'done').get('round_limit')   # the limit was not the reason
    assert state.saved['content'].strip() == ANSWER and not state.saved['metadata']['round_limit']
    assert 'tool rounds' not in state.calls[-1]['messages'][-1]['content']


def test_a_model_that_stays_quiet_gets_the_usual_note(harness, monkeypatch):
    run, state = harness
    monkeypatch.setattr(config, 'MAX_AGENT_ROUNDS', 10)
    state.reply = lambda payload, number: tool_round(number) if number <= 3 else packets(done(eval_count=0))
    events = asyncio.run(run())
    assert 'no text response' in text_of(events) and chat_rounds.NOTICE not in text_of(events)
    assert not state.saved['metadata']['round_limit']


# ── The context reading of a tool-using reply (Stage B, 2026-11-15) ──

def test_the_saved_reading_carries_the_breakdown_and_the_peak(harness, monkeypatch):
    run, state = harness
    monkeypatch.setattr(config, 'MAX_AGENT_ROUNDS', 10)
    state.reply = lambda payload, number: tool_round(number) if number <= 2 else packets(
        {'message': {'role': 'assistant', 'content': ANSWER}, 'done': False}, done(prompt_eval_count=1400))
    events = asyncio.run(run())
    updates = [e for e in events if e['type'] == 'ctx_update' and e.get('parts')]
    assert updates and all(sum(e['parts'].values()) == e['total'] for e in updates)
    # While the tools ran, their results were already in the figure.
    assert max(e['parts']['tool_calls'] for e in updates) > 0
    reading = state.saved['metadata']['context']
    assert sum(reading['parts'].values()) == reading['used']
    assert set(reading['parts']) == {'instructions', 'tools', 'memory', 'skills', 'summary', 'messages', 'kept_tools'}
    assert reading['parts']['tools'] > 0 and reading['parts']['instructions'] > 0
    assert reading['parts']['kept_tools'] > 0          # this reply's two tool results are kept for the next turn
    assert reading['peak']['total'] == reading['used'] + reading['working_peak']
    assert sum(reading['peak']['parts'].values()) == reading['peak']['total']
    assert next(e for e in events if e['type'] == 'done')['context'] == reading


def test_a_broken_breakdown_never_fails_the_reply(harness, monkeypatch):
    import context_breakdown
    run, state = harness
    monkeypatch.setattr(config, 'MAX_AGENT_ROUNDS', 10)
    monkeypatch.setattr(context_breakdown, 'scale', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setattr(context_breakdown, 'kept', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    state.reply = lambda payload, number: tool_round(number) if number <= 1 else packets(
        {'message': {'role': 'assistant', 'content': ANSWER}, 'done': False}, done())
    events = asyncio.run(run())
    assert not [e for e in events if e['type'] == 'error'] and text_of(events).strip() == ANSWER
    reading = state.saved['metadata']['context']
    assert reading['used'] > 0 and 'parts' not in reading and 'peak' not in reading
