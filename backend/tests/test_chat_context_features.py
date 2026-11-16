"""Context features from the 2026-11-15 chat audit (stage 2): a warning when a request did not fit the
window, telling the model that rounds or context are running low, telling it when messages were written,
and summarizing or forgetting on request.

Measured first, through the chat, with an 8,192-token window: one message of twice the window was read as
4,098 tokens (exactly half), a history of twice the window as 7,922, and 12,000 characters that fit as
5,964. Nothing was reported in any of the three.
"""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

import chat_compact_now
import chat_context
import chat_overflow
import chat_payload
import chat_rounds
import chat_times
import config
import database as db
import model_limits
import reasoning
from agents import chat
from db import tool_memory
from events import EventBus
from main import ChatRequest


def packets(*items):
    return httpx.Response(200, text=''.join(json.dumps(item) + '\n' for item in items))


def finished(prompt_tokens=900, **extra):
    return {'message': {'content': ''}, 'done': True, 'done_reason': 'stop', 'prompt_eval_count': prompt_tokens,
            'eval_count': 30, 'eval_duration': 1000000, **extra}


def tool_round(number):
    call = {'function': {'name': 'execute_code', 'arguments': {
        'description': f'Step {number}', 'code': f'print({number})', 'language': 'python'}}}
    return packets({'message': {'role': 'assistant', 'content': '', 'tool_calls': [call]}, 'done': False}, finished())


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'chat.db'))
    monkeypatch.setattr(chat, '_suggest_workspace_memories', AsyncMock())
    monkeypatch.setattr(config, 'CHAT_TIME_NOTES', 'on')
    reasoning._cache.clear()
    model_limits._cache.clear()
    chat_payload.forget_all()
    state = SimpleNamespace(calls=[], prompt_tokens=900, tools=0, bus=EventBus())

    async def fake_tool(http, events, name, args, conv_id, *rest, **more):
        await events.emit(conv_id, 'tool_end', {'tool': name, 'status': 'OK'})
        return '**SUCCESS** | python | exit 0\nstep output'
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
        last = payload['messages'][-1]
        if last['role'] == 'user' and 'Do not call a tool' in last['content']:      # the answer at the round limit
            return packets({'message': {'role': 'assistant', 'content': 'Done.'}, 'done': False}, finished())
        if len(state.calls) <= state.tools:
            return tool_round(len(state.calls))
        return packets({'message': {'role': 'assistant', 'content': 'Hello there.'}, 'done': False}, finished(state.prompt_tokens))

    async def run(messages=None, **request):
        await db.init_db()
        if not await db.get_conversation('c'):
            await db.create_conversation('c', model='fixture')
        req = ChatRequest(conversation_id='c', model='fixture', messages=messages or [{'role': 'user', 'content': 'Hello'}],
                          tool_ids=[], use_memories=False, think_budget=0, turn='new', **request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            events = [json.loads(s[5:]) async for s in chat.chat_stream_generate(req, http, state.bus, {}, {})]
        state.rows = (await db.get_conversation('c'))['messages']
        return events

    return run, state


def of(events, kind):
    return [event for event in events if event['type'] == kind]


def user_texts(payload):
    return [message['content'] for message in payload['messages'] if message['role'] == 'user']


# ── A request that did not fit ─────────────────────────────────────────────────

def test_a_message_of_twice_the_window_is_flagged():
    found = chat_overflow.check(20100, 4098, 8192)              # measured: read as exactly half the window
    assert found and found['window'] == 8192 and found['read'] == 4098 and found['needed'] > 8192
    assert chat_overflow.check(60000, 4098, 8192)['needed'] > 30000          # six times the window


def test_a_history_that_lost_its_oldest_messages_is_flagged():
    found = chat_overflow.check(20200, 7922, 8192)              # measured: old messages dropped until it fit
    assert found and found['needed'] > 8192


def test_a_request_that_fits_is_never_flagged():
    assert chat_overflow.check(8170, 5964, 8192) is None        # measured: 12,000 characters that fit
    assert chat_overflow.check(5600, 4098, 8192) is None        # happens to land on half the window, at a normal rate
    assert chat_overflow.check(10700, 7800, 8192) is None       # nearly full, and all of it was read
    assert chat_overflow.check(11200, 8100, 8192) is None
    assert chat_overflow.check(1200, 900, 262144) is None
    for unknown in ((0, 900, 8192), (20000, 0, 8192), (20000, 4098, 0), ('x', 4098, 8192), (None, None, None)):
        assert chat_overflow.check(*unknown) is None


def test_the_figure_shown_uses_the_chats_own_token_rate():
    assert chat_overflow.needed(20000, 0.62) == 12400
    assert chat_overflow.needed(20000) == 14000                 # no measured rate yet
    assert chat_overflow.needed(20000, 0.05) == 14000           # a rate that cannot be right is not used
    assert chat_overflow.needed(20000, 'junk') == 14000


def test_a_reply_to_a_request_that_did_not_fit_says_so(harness):
    run, state = harness
    state.prompt_tokens = 4098                                  # what Ollama reports after cutting the prompt
    events = asyncio.run(run(num_ctx=8192, messages=[{'role': 'user', 'content': 'word ' * 12000 + 'What was the first word?'}]))
    flagged = of(events, 'context_overflow')
    assert len(flagged) == 1 and flagged[0]['window'] == 8192 and flagged[0]['needed'] > 8192
    done = of(events, 'done')[0]
    assert done['overflow'] == {'needed': flagged[0]['needed'], 'window': 8192}
    assert done['context']['overflow'] == flagged[0]['needed']
    assert done['context']['used'] >= flagged[0]['needed']      # not the 4,098 the model happened to read
    saved = state.rows[-1]['metadata']
    assert saved['overflow'] == done['overflow'] and saved['context']['overflow'] == flagged[0]['needed']


def test_an_ordinary_reply_carries_no_overflow(harness):
    run, state = harness
    events = asyncio.run(run(num_ctx=8192))
    assert not of(events, 'context_overflow') and 'overflow' not in of(events, 'done')[0]
    assert 'overflow' not in state.rows[-1]['metadata'] and 'overflow' not in state.rows[-1]['metadata']['context']


def test_a_cut_count_does_not_spoil_the_chats_token_rate():
    meter = chat_context.TurnMeter()
    meter.info({'round': 0, 'window': 8192, 'prompt_estimate': 20000, 'ratio': 0.7})
    meter.usage({'prompt_tokens': 4098, 'gen_tokens': 20})
    assert meter.ratio < 0.4                                    # 4,098 over 20,000: what the cut count implies
    meter.overflowed({'needed': 14000, 'window': 8192, 'ratio': 0.7})
    reading = meter.reading('An answer.')
    assert reading['ratio'] == 0.7 and reading['overflow'] == 14000 and reading['used'] >= 14000
    assert 'parts' not in reading                               # no split of a figure the model never read


# ── Telling the model it is running low ────────────────────────────────────────

def test_the_model_is_told_when_three_rounds_are_left():
    notes = chat_rounds.BudgetNotes(40)
    assert all(notes.note(number) == '' for number in range(37))          # nothing is said early in a reply
    assert '3 tool rounds are left' in notes.note(37)
    assert '2 tool rounds are left' in notes.note(38)
    assert 'last tool round' in notes.note(39)
    assert notes.note(37).startswith('[Note from HyprChat: ') and notes.note(37).endswith(']')


def test_a_nearly_full_window_is_mentioned_once():
    notes = chat_rounds.BudgetNotes(40)
    assert notes.note(5, used=20000, window=32768, share=0.92) == ''
    said = notes.note(6, used=25000, window=32768, share=0.92)             # 80 % of the way to where room is made
    assert 'about 76% full' in said and 'tool rounds' not in said
    assert notes.note(7, used=26000, window=32768, share=0.92) == ''
    assert chat_rounds.BudgetNotes(40).note(6, used=25000, window=0) == ''  # a cloud model: the window is not known


def test_the_note_follows_the_newest_tool_result_and_is_not_kept(harness, monkeypatch):
    run, state = harness
    monkeypatch.setattr(config, 'MAX_AGENT_ROUNDS', 4)
    state.tools = 2                                             # two tool rounds, then the answer
    asyncio.run(run())
    first, second, third = state.calls[:3]
    assert 'Note from HyprChat' not in json.dumps(first['messages'])
    newest = second['messages'][-1]
    assert newest['role'] == 'tool' and newest['content'].endswith('[Note from HyprChat: 3 tool rounds are left for this reply. Finish the task, or write your answer.]')
    assert '2 tool rounds are left' in third['messages'][-1]['content']
    assert third['messages'][:len(second['messages'])] == second['messages']      # nothing sent earlier changed: the cache holds

    async def kept():
        rows = await tool_memory.for_messages('c', [state.rows[-1]['id']])
        return [row['result'] for row in rows.get(state.rows[-1]['id'], [])]
    stored = asyncio.run(kept())
    assert stored and not any('Note from HyprChat' in text for text in stored)


# ── Telling the model when things were said ────────────────────────────────────

ZONE = ZoneInfo('America/Denver')


def test_a_message_line_says_when_it_was_written_in_the_owners_time_zone():
    assert chat_times.line(datetime(2026, 11, 15, 22, 42), None, ZONE) == '[Sunday 15 November 2026, 3:42 PM]'
    assert chat_times.line(datetime(2026, 7, 4, 6, 5), None, ZONE) == '[Saturday 4 July 2026, 12:05 AM]'
    assert chat_times.line(datetime(2026, 11, 15, 19, 0), None, ZoneInfo('UTC')) == '[Sunday 15 November 2026, 7:00 PM]'


def test_a_long_pause_is_spelled_out_and_a_short_one_is_not():
    at = datetime(2026, 11, 15, 22, 42)
    assert chat_times.line(at, at - timedelta(minutes=20), ZONE) == '[Sunday 15 November 2026, 3:42 PM]'
    assert chat_times.line(at, at - timedelta(days=3, hours=2), ZONE).endswith('· 3 days after the previous message]')
    assert [chat_times.gap_words(hours * 3600) for hours in (1, 5.9, 6, 23, 24, 30, 24 * 9, 24 * 15, 24 * 45, 24 * 70, 24 * 400)] == \
        ['', '', '6 hours', '23 hours', '1 day', '1 day', '9 days', '2 weeks', '6 weeks', '2 months', '1 year']
    assert chat_times.line(at, at + timedelta(days=1), ZONE) == '[Sunday 15 November 2026, 3:42 PM]'    # clocks that disagree: no gap


def test_saved_times_are_read_in_any_of_their_forms():
    assert chat_times.parse('2026-11-15T22:42:10.123456') == datetime(2026, 11, 15, 22, 42, 10, 123456)
    assert chat_times.parse('2026-11-15 22:42:10') == datetime(2026, 11, 15, 22, 42, 10)
    assert chat_times.parse('2026-11-15T22:42:10Z') == datetime(2026, 11, 15, 22, 42, 10)
    assert chat_times.parse('2026-11-15T15:42:10-07:00') == datetime(2026, 11, 15, 22, 42, 10)
    assert chat_times.parse('') is None and chat_times.parse('yesterday') is None and chat_times.parse(None) is None


def test_only_messages_whose_time_is_known_get_a_line():
    saved = {1: datetime(2026, 11, 12, 20, 0), 2: datetime(2026, 11, 12, 20, 1), 9: datetime(2026, 11, 15, 22, 42)}
    request = [{'role': 'user', 'content': 'a', 'id': 1}, {'role': 'assistant', 'content': 'b', 'id': 2},
               {'role': 'user', 'content': 'not saved'}, {'role': 'assistant', 'content': 'c'},
               {'role': 'user', 'content': 'the one being sent'}]
    lines = chat_times.lines_for(request, saved, ZONE, newest_id=9)
    assert set(lines) == {0, 4}                                 # replies never get one; an unknown time gets none
    assert lines[0] == '[Thursday 12 November 2026, 1:00 PM]'
    assert lines[4] == '[Sunday 15 November 2026, 3:42 PM · 3 days after the previous message]'
    assert chat_times.lines_for(request, {}, ZONE, newest_id=9) == {}
    assert chat_times.lines_for([{'role': 'user', 'content': 'x', 'id': 'junk'}], saved, ZONE) == {}


def test_a_message_carries_the_same_line_on_every_turn(harness):
    run, state = harness
    asyncio.run(run())
    first_turn = user_texts(state.calls[0])[-1]
    assert first_turn.startswith('[') and first_turn.endswith(']\nHello')
    assert state.rows[0]['content'] == 'Hello'                  # nothing changes in what is saved or shown
    assert chat_times.INSTRUCTION in state.calls[0]['messages'][0]['content']

    async def three_days_pass():
        conn = await db.get_db()
        try:
            await conn.execute("UPDATE messages SET created_at = ? WHERE conversation_id = 'c'",
                               ((datetime.utcnow() - timedelta(days=3)).isoformat(),))
            await conn.commit()
        finally:
            await conn.close()
    asyncio.run(three_days_pass())
    history = [{'role': row['role'], 'content': row['content'], 'id': row['id']} for row in state.rows]
    asyncio.run(run(messages=history + [{'role': 'user', 'content': 'And now?'}]))
    asyncio.run(run(messages=[{'role': row['role'], 'content': row['content'], 'id': row['id']} for row in state.rows]
                    + [{'role': 'user', 'content': 'Once more'}]))
    second, third = user_texts(state.calls[1]), user_texts(state.calls[2])
    assert second[0].endswith(']\nHello') and '· ' not in second[0].split('\n')[0]
    assert second[1].split('\n')[0].endswith('· 3 days after the previous message]') and second[1].endswith('\nAnd now?')
    assert third[:2] == second                                  # the same text on the next turn: the cache holds


def test_no_lines_when_switched_off_or_in_a_ghost_chat(harness, monkeypatch):
    run, state = harness
    monkeypatch.setattr(config, 'CHAT_TIME_NOTES', 'off')
    asyncio.run(run())
    assert user_texts(state.calls[0])[-1] == 'Hello'
    assert chat_times.INSTRUCTION not in json.dumps(state.calls[0]['messages'])
    monkeypatch.setattr(config, 'CHAT_TIME_NOTES', 'on')
    asyncio.run(run(ephemeral=True))
    assert user_texts(state.calls[1])[-1] == 'Hello'
    assert chat_times.INSTRUCTION not in json.dumps(state.calls[1]['messages'])


# ── Compact now, and forgetting kept tool results ──────────────────────────────

LONG = 'The survey of the northern districts recorded steady output and no faults this quarter. ' * 12


@pytest.fixture
def saved_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'compact.db'))
    monkeypatch.setattr(config, 'CONTEXT_COMPACTION', 'on')
    monkeypatch.setattr(config, 'CHAT_TOOL_MEMORY', 'on')
    monkeypatch.setattr(model_limits, 'context_limit', AsyncMock(return_value=0))
    written = SimpleNamespace(calls=[], text='SUMMARY: districts steady, no faults.')

    async def summary(http, model, window, old_summary, fold, words):
        written.calls.append({'model': model, 'window': window, 'old': old_summary, 'fold': fold})
        return written.text
    monkeypatch.setattr(chat_context, '_plain_summary', summary)

    async def build(exchanges=6, model='fixture', reading=True):
        await db.init_db()
        await db.create_conversation('c', model=model)
        ids = []
        for number in range(exchanges):
            ids.append(await db.add_message('c', 'user', f'Question {number}. {LONG}'))
            last = number == exchanges - 1
            ids.append(await db.add_message('c', 'assistant', f'Answer {number}. {LONG}', {
                'delivery_status': 'completed',
                **({'context': {'used': 9000, 'window': 16384, 'asked': 16384, 'measured': True, 'ratio': 0.7,
                                'parts': {'instructions': 2000, 'tools': 1000, 'messages': 5600, 'kept_tools': 400}}} if last and reading else {})}))
        return ids
    return build, written


def reading_of(rows):
    return rows[-1]['metadata'].get('context')


def test_compact_now_summarizes_the_earlier_messages_and_keeps_the_newest(saved_chat):
    build, written = saved_chat

    async def run():
        ids = await build()
        result = await chat_compact_now.compact_now(None, 'c')
        return ids, result, await db.get_conversation_summary('c'), (await db.get_conversation('c'))['messages']
    ids, result, (summary, cursor), rows = asyncio.run(run())
    assert result['compacted'] is True and result['cursor'] == cursor and result['window'] == 16384
    assert summary == written.text and cursor in ids and cursor < ids[-2]        # the last exchange stays word for word
    assert rows[[row['id'] for row in rows].index(cursor) + 1]['role'] == 'user'   # the kept part opens on a question
    assert result['summarized'] == ids.index(cursor) + 1 and result['freed'] > 0
    assert len(rows) == 12                                                       # no message is deleted
    assert written.calls[0]['model'] == 'fixture' and written.calls[0]['window'] == 16384
    lowered = reading_of(rows)
    assert lowered['used'] == 9000 - result['freed'] and lowered['measured'] is False and 'parts' not in lowered


def test_a_second_compact_now_merges_the_earlier_summary(saved_chat):
    build, written = saved_chat

    async def run():
        await build(exchanges=10)
        first = await chat_compact_now.compact_now(None, 'c')
        for number in range(6):
            await db.add_message('c', 'user', f'Later question {number}. {LONG}')
            await db.add_message('c', 'assistant', f'Later answer {number}. {LONG}', {'delivery_status': 'completed'})
        written.text = 'SUMMARY 2: everything so far.'
        second = await chat_compact_now.compact_now(None, 'c')
        return first, second, await db.get_conversation_summary('c')
    first, second, (summary, cursor) = asyncio.run(run())
    assert second['compacted'] and second['cursor'] > first['cursor'] and cursor == second['cursor']
    assert summary == 'SUMMARY 2: everything so far.' and written.calls[1]['old'] == 'SUMMARY: districts steady, no faults.'
    assert all('Question 0.' not in message['content'] for message in written.calls[1]['fold'])   # already inside the first summary


def test_compact_now_changes_nothing_when_it_cannot_help(saved_chat, monkeypatch):
    build, written = saved_chat

    async def run():
        await build(exchanges=1)
        short = await chat_compact_now.compact_now(None, 'c')
        monkeypatch.setattr(config, 'CONTEXT_COMPACTION', 'off')
        off = await chat_compact_now.compact_now(None, 'c')
        monkeypatch.setattr(config, 'CONTEXT_COMPACTION', 'on')
        cloud = await chat_compact_now.compact_now(None, 'c', model='anthropic:claude-opus-5')
        return short, off, cloud, await db.get_conversation_summary('c'), (await db.get_conversation('c'))['messages']
    short, off, cloud, stored, rows = asyncio.run(run())
    for refused in (short, off, cloud):
        assert refused['compacted'] is False and refused['reason']
    assert 'not enough' in short['reason'] and 'switched off' in off['reason'] and 'local models' in cloud['reason']
    assert stored == ('', 0) and not written.calls and reading_of(rows)['used'] == 9000


def test_a_summary_that_could_not_be_written_leaves_the_chat_as_it_was(saved_chat):
    build, written = saved_chat
    written.text = ''

    async def run():
        await build()
        return await chat_compact_now.compact_now(None, 'c'), await db.get_conversation_summary('c'), (await db.get_conversation('c'))['messages']
    result, stored, rows = asyncio.run(run())
    assert result['compacted'] is False and 'Nothing was changed' in result['reason']
    assert stored == ('', 0) and reading_of(rows)['used'] == 9000 and 'parts' in reading_of(rows)


def test_compact_now_waits_for_a_reply_that_is_being_written(saved_chat):
    build, written = saved_chat

    async def run():
        await build()
        await db.add_message('c', 'user', 'One more')
        await db.add_message('c', 'assistant', '', {'delivery_status': 'running', 'in_progress': True})
        return await chat_compact_now.compact_now(None, 'c')
    result = asyncio.run(run())
    assert result['compacted'] is False and 'being written' in result['reason'] and not written.calls


def test_someone_elses_chat_cannot_be_compacted_or_cleared(saved_chat):
    build, written = saved_chat

    async def run():
        await build()
        token = db.set_current_user_id('someone-else')
        try:
            outcomes = []
            for action in (chat_compact_now.compact_now(None, 'c'), chat_compact_now.forget_tool_results('c')):
                try:
                    await action
                    outcomes.append('allowed')
                except LookupError:
                    outcomes.append('refused')
            return outcomes
        finally:
            db.reset_current_user_id(token)
    assert asyncio.run(run()) == ['refused', 'refused'] and not written.calls


def test_what_is_summarized_is_what_the_model_was_given():
    rows = [{'id': 1, 'role': 'user', 'content': 'Summarize this', 'metadata': {'model_content': 'Summarize this\n\nPDF: revenue rose 4%.'}},
            {'id': 2, 'role': 'assistant', 'content': 'It rose 4%.', 'metadata': {}},
            {'id': 3, 'role': 'user', 'content': 'Thanks', 'metadata': None}]
    sent = chat_compact_now.as_sent(rows, {2: 'Results of the tools you ran: exit 0'})
    assert sent[0]['content'] == 'Summarize this\n\nPDF: revenue rose 4%.\n\nResults of the tools you ran: exit 0'
    assert sent[1] == {'role': 'assistant', 'content': 'It rose 4%.'} and sent[2] == {'role': 'user', 'content': 'Thanks'}


def test_forgetting_kept_tool_results_removes_them_for_this_chat_only(saved_chat):
    build, written = saved_chat

    async def run():
        ids = await build()
        await db.create_conversation('other', model='fixture')
        elsewhere = await db.add_message('other', 'assistant', 'x')
        for message_id in (ids[1], ids[-1], elsewhere):
            await tool_memory.add(message_id, round_number=0, call_id='a', tool='execute_code', status='ok', kept=1,
                                  arguments='{}', result='exit 0')
        result = await chat_compact_now.forget_tool_results('c')
        left_here = await tool_memory.for_messages('c', ids)
        left_there = await tool_memory.for_messages('other', [elsewhere])
        again = await chat_compact_now.forget_tool_results('c')
        return result, left_here, left_there, again, (await db.get_conversation('c'))['messages']
    result, left_here, left_there, again, rows = asyncio.run(run())
    assert result == {'removed': 2, 'freed': 400} and left_here == {} and len(left_there) == 1
    assert again == {'removed': 0, 'freed': 0}
    lowered = reading_of(rows)
    assert lowered['used'] == 8600 and 'parts' not in lowered
