"""A chat keeps what its tools returned (chat_tool_memory.py).

Seen live 2026-11-14: a reply read a whole repository with 25 code runs, ran out of rounds, and each
"continue" started again from 8k tokens and cloned the repository again, because only reply text was
sent on the next turn.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import chat_context
import chat_tool_memory as memory
import config
import context_breakdown
import database as db
import reasoning
from agents import chat
from db import tool_memory as store
from events import EventBus
from main import ChatRequest

WINDOW = 16384


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'chat.db'))
    monkeypatch.setattr(config, 'CHAT_TOOL_MEMORY', 'on')
    monkeypatch.setattr(config, 'CONTEXT_COMPACTION', 'on')

    async def setup():
        await db.init_db()
        await db.create_conversation('c', model='fixture')
        await db.add_message('c', 'user', 'Look at the code base')
        return await db.add_message('c', 'assistant', 'Here is what I found.')
    return asyncio.run(setup())


def run(coroutine):
    return asyncio.run(coroutine)


async def recorder(reply, **extra):
    return await memory.begin_turn(SimpleNamespace(user_metadata=None), 'c', reply, window=WINDOW, **extra)


def rows(reply):
    return run(store.for_messages('c', [reply])).get(reply, [])


def test_a_tool_result_is_stored_with_the_reply_that_ran_it(database):
    async def scenario():
        turn = await recorder(database)
        assert await turn.record('execute_code', {'description': 'Count the lines', 'code': 'print(98412)', 'language': 'python'},
                                 '**SUCCESS** | python | exit 0 | 1.0s\n\nstdout:\n```\n98412\n```', 'c0-1', 0)
        assert await turn.record('execute_code', {'code': 'print(1/0)', 'language': 'python'}, '**FAILED** | python | exit 1', 'c1-1', 1)
        return turn
    turn = run(scenario())
    saved = rows(database)
    assert [(r['seq'], r['tool'], r['status'], r['kept'], r['call_id'], r['round']) for r in saved] == [
        (1, 'execute_code', 'ok', 1, 'c0-1', 0), (2, 'execute_code', 'failed', 1, 'c1-1', 1)]
    assert json.loads(saved[0]['arguments'])['code'] == 'print(98412)' and '98412' in saved[0]['result']
    assert turn.estimate() > 0


def test_the_record_says_what_ran_and_what_came_back(database):
    async def scenario():
        turn = await recorder(database)
        await turn.record('execute_code', {'description': 'Count the lines', 'code': 'print(98412)', 'language': 'python'}, 'stdout: 98412')
        await turn.record('read_file', {'path': 'backend/main.py'}, 'from fastapi import FastAPI')
    run(scenario())
    text = memory.digest(rows(database), 5000)
    assert text.startswith(memory.OPEN) and text.endswith(memory.CLOSE)
    assert '#1 execute_code: Count the lines (ok)' in text and 'code:\nprint(98412)' in text and 'result:\nstdout: 98412' in text
    assert '#2 read_file (ok)' in text and '"path": "backend/main.py"' in text
    assert memory.digest(rows(database), 5000) == text                 # the same rows give the same text: the prompt cache depends on it
    assert memory.blocks(f'A question\n\n{text}') == [text] and memory.blocks('no record here') == []


def test_email_and_connector_results_are_never_stored(database):
    # Email bodies are never stored anywhere. Connector output belongs to another system.
    async def scenario():
        turn = await recorder(database, private={'crm_lookup': {}})
        await turn.record('read_email', {'id': 7}, 'From: someone\nSubject: Salary\n\nThe confidential body.')
        await turn.record('crm_lookup', {'customer': 'Acme'}, 'Acme owes 40,000')
        await turn.record('search_history', {'query': 'salary'}, 'Another chat said: ...')
    run(scenario())
    saved = rows(database)
    assert [(r['kept'], r['arguments'], r['result']) for r in saved] == [(0, '{}', '')] * 3
    text = memory.digest(saved, 5000)
    assert 'confidential' not in text and 'Acme' not in text and 'Salary' not in text
    assert '#1 read_email: result not kept' in text and 'Call the tool again' in text


def test_after_an_email_call_the_rest_of_the_turn_keeps_headers_only(database):
    # Code written after reading an email can contain the email.
    async def scenario():
        turn = await recorder(database)
        await turn.record('execute_code', {'code': 'print(1)'}, 'stdout: 1')
        await turn.record('read_email', {'id': 7}, 'The confidential body.')
        await turn.record('execute_code', {'description': 'Summarise the confidential body', 'code': 'text = "The confidential body."'}, 'stdout: confidential')
    run(scenario())
    saved = rows(database)
    assert [r['kept'] for r in saved] == [1, 0, 2]
    assert saved[2]['arguments'] == '{}' and saved[2]['result'] == ''
    text = memory.digest(saved, 5000)
    assert 'confidential' not in text and '#3 execute_code (ok): details not kept.' in text and 'stdout: 1' in text


def test_ghost_scheduled_and_daedalus_turns_keep_nothing(database):
    async def scenario():
        plain = SimpleNamespace(user_metadata=None)
        scheduled = SimpleNamespace(user_metadata={'source': 'scheduled_task'})
        turns = [await memory.begin_turn(plain, 'c', database, window=WINDOW, ephemeral=True),
                 await memory.begin_turn(plain, 'c', database, window=WINDOW, coder=True),
                 await memory.begin_turn(scheduled, 'c', database, window=WINDOW),
                 await memory.begin_turn(plain, 'c', None, window=WINDOW),
                 await memory.begin_turn(plain, 'c', database, window=4096)]
        for turn in turns:
            assert not turn.active and not await turn.record('execute_code', {'code': 'print(1)'}, 'stdout: 1')
            assert turn.estimate() == 0
    run(scenario())
    assert rows(database) == []


def test_a_result_is_capped_head_and_tail_and_secrets_are_removed(database):
    long = 'START ' + 'x' * 9000 + ' END'
    async def scenario():
        turn = await recorder(database)
        await turn.record('fetch_url', {'url': 'https://example.org', 'note': 'n' * 900}, long)
        await turn.record('run_shell', {'command': 'curl -H "Authorization: Bearer abc.def.ghi" https://x'}, 'api_key = sk-ant-abcdefghijklmnop1234')
    run(scenario())
    first, second = rows(database)
    assert first['result'].startswith('START ') and first['result'].endswith(' END') and 'characters left out' in first['result']
    assert len(first['result']) < memory.RESULT_CHARS + 80
    assert len(json.loads(first['arguments'])['note']) < 400
    assert 'abc.def.ghi' not in second['arguments'] and 'sk-ant-abcdefghijklmnop1234' not in second['result']


def test_a_reply_that_stored_a_lot_keeps_only_one_line_for_later_calls(database, monkeypatch):
    monkeypatch.setattr(memory, 'STORED_CHARS', 6000)
    async def scenario():
        turn = await recorder(database)
        for number in range(4):
            await turn.record('execute_code', {'code': f'print({number})'}, 'y' * 3500)
    run(scenario())
    assert [r['kept'] for r in rows(database)] == [1, 1, 2, 2]


def test_a_turn_over_its_budget_gives_the_newest_calls_in_full(database):
    async def scenario():
        turn = await recorder(database)
        for number in range(5):
            await turn.record('execute_code', {'description': f'Step {number + 1}', 'code': f'print({number})'}, f'output {number + 1} ' + 'z' * 1500)
    run(scenario())
    text = memory.digest(rows(database), 3600)
    assert 'output 5' in text and 'output 4' in text and 'output 1' not in text
    assert '#1 execute_code: Step 1 (ok): details left out to save room.' in text
    assert len(text) < 3600 + 700


def test_older_replies_give_way_when_the_whole_budget_is_spent(monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACTION', 'off')            # nothing folds old turns: the smaller overall budget
    row = {'tool': 'execute_code', 'status': 'ok', 'kept': 1, 'arguments': '{"code": "print(1)"}', 'result': 'r' * 1000}
    by_message = {reply: [dict(row)] * 3 for reply in (10, 20, 30)}
    texts = memory.records(by_message, [10, 20, 30], WINDOW)            # about 7,400 characters for everything
    assert texts[30].count('r' * 1000) == 3 and texts[20].count('r' * 1000) == 3
    assert 'r' * 1000 not in texts[10] and 'details left out to save room' in texts[10]
    assert sum(len(text) for text in texts.values()) < memory.total_budget(WINDOW) + 600
    monkeypatch.setattr(config, 'CONTEXT_COMPACTION', 'on')
    assert all(text.count('r' * 1000) == 3 for text in memory.records(by_message, [10, 20, 30], WINDOW).values())


def test_a_small_window_gets_the_newest_call_shortened_rather_than_nothing():
    row = {'tool': 'execute_code', 'status': 'ok', 'kept': 1, 'arguments': '{"code": "print(1)"}', 'result': 'HEAD ' + 'r' * 5000 + ' TAIL'}
    text = memory.digest([dict(row, result='older ' + 'o' * 3000), row], memory.turn_budget(8192))
    assert 'HEAD' in text and 'TAIL' in text and 'characters left out' in text
    assert '#1 execute_code (ok): details left out to save room.' in text
    assert len(text) < memory.turn_budget(8192) + 500


def test_regenerate_edit_and_delete_remove_the_kept_results(database):
    async def scenario():
        turn = await recorder(database)
        await turn.record('execute_code', {'code': 'print(1)'}, 'stdout: 1')
        user = (await db.get_conversation('c'))['messages'][0]['id']
        later_user = await db.add_message('c', 'user', 'And then?')
        later = await db.add_message('c', 'assistant', 'Then this.')
        second = await recorder(later)
        await second.record('execute_code', {'code': 'print(2)'}, 'stdout: 2')
        assert len(await store.for_messages('c', [database, later])) == 2
        await db.delete_message(later)                                   # delete one reply
        assert list(await store.for_messages('c', [database, later])) == [database]
        await db.delete_messages_from('c', user)                         # regenerate or edit from the first message
        assert await store.for_messages('c', [database, later]) == {}
        return later_user
    run(scenario())


def test_deleting_the_chat_removes_them_too(database):
    async def scenario():
        turn = await recorder(database)
        await turn.record('execute_code', {'code': 'print(1)'}, 'stdout: 1')
        await db.delete_conversation('c')
        conn = await db.get_db()
        try:
            return (await conn.execute_fetchall('SELECT count(*) AS n FROM message_tool_results'))[0]['n']
        finally:
            await conn.close()
    assert run(scenario()) == 0


def test_a_fork_takes_the_kept_results_with_it(database):
    async def scenario():
        turn = await recorder(database)
        await turn.record('execute_code', {'description': 'Count', 'code': 'print(1)'}, 'stdout: 1')
        fork = await db.fork_conversation('c', database, 'fork')
        reply = fork['messages'][-1]['id']
        return (await store.for_messages('fork', [reply])).get(reply, []), reply
    copied, reply = run(scenario())
    assert reply != database and [(r['tool'], r['result']) for r in copied] == [('execute_code', 'stdout: 1')]
    assert rows(database)                                                # the original keeps its own


def test_continue_adds_to_the_same_reply_and_counts_only_what_is_new(database):
    async def scenario():
        first = await recorder(database)
        await first.record('execute_code', {'code': 'print(1)'}, 'stdout: 1')
        again = await recorder(database)                                 # the reply is continued
        assert len(again.rows) == 1 and again.estimate() == 0           # what it had is already in the prompt
        await again.record('execute_code', {'code': 'print(2)'}, 'stdout: 2')
        return again.estimate()
    assert run(scenario()) > 0
    assert [r['seq'] for r in rows(database)] == [1, 2]


def test_an_id_from_another_chat_or_another_user_brings_nothing(database):
    async def scenario():
        turn = await recorder(database)
        await turn.record('execute_code', {'code': 'print(1)'}, 'stdout: private')
        await db.create_conversation('other', model='fixture')
        assert await store.for_messages('other', [database]) == {}
        assert await memory.load_for('other', [{'role': 'assistant', 'id': database}], window=WINDOW) == {}
        token = db.set_current_user_id('someone-else')
        try:
            assert await store.for_messages('c', [database]) == {}
            await store.add(database, round_number=0, call_id='', tool='execute_code', status='ok', kept=1, arguments='{}', result='theirs')
        finally:
            db.reset_current_user_id(token)
        return await store.for_messages('c', [database])
    assert [r['result'] for r in run(scenario())[database]] == ['stdout: private']


def test_replies_inside_the_summary_do_not_load_their_tool_results(database):
    async def scenario():
        turn = await recorder(database)
        await turn.record('execute_code', {'code': 'print(1)'}, 'stdout: 1')
        request = [{'role': 'user', 'content': 'q', 'id': database - 1}, {'role': 'assistant', 'content': 'a', 'id': database}]
        assert list(await memory.load_for('c', request, window=WINDOW)) == [database]
        assert await memory.load_for('c', request, window=WINDOW, after=database) == {}
        assert await memory.load_for('c', request, window=4096) == {}
    run(scenario())


def test_switching_it_off_stops_storing_and_sending(database, monkeypatch):
    async def scenario():
        turn = await recorder(database)
        await turn.record('execute_code', {'code': 'print(1)'}, 'stdout: 1')
        monkeypatch.setattr(config, 'CHAT_TOOL_MEMORY', 'off')
        off = await recorder(database)
        assert not off.active and not await off.record('execute_code', {'code': 'print(2)'}, 'stdout: 2')
        return await memory.load_for('c', [{'role': 'assistant', 'id': database}], window=WINDOW)
    assert run(scenario()) == {}
    assert len(rows(database)) == 1                                       # what was stored stays until its message goes


def test_a_database_error_never_fails_the_reply(database, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError('database is locked')
    async def scenario():
        turn = await recorder(database)
        monkeypatch.setattr(store, 'add', broken)
        monkeypatch.setattr(store, 'for_messages', broken)
        assert not await turn.record('execute_code', {'code': 'print(1)'}, 'stdout: 1')
        assert await memory.load_for('c', [{'role': 'assistant', 'id': database}], window=WINDOW) == {}
        assert not (await memory.begin_turn(SimpleNamespace(user_metadata=None), 'c', database, window=WINDOW)).active
    run(scenario())


def test_migration_is_idempotent(database):
    async def scenario():
        await db.init_db()
        await db.init_db()
        conn = await db.get_db()
        try:
            await store.migrate(conn)
            return [row['name'] for row in await conn.execute_fetchall("SELECT name FROM sqlite_master WHERE name = 'message_tool_results'")]
        finally:
            await conn.close()
    assert run(scenario()) == ['message_tool_results']


def test_the_reading_counts_kept_results_and_old_readings_are_unchanged():
    meter = chat_context.TurnMeter()
    meter.info({'round': 0, 'window': 32768, 'prompt_estimate': 5000,
                'parts': {'instructions': 3000, 'tools': 1500, 'messages': 400, 'kept_tools': 100}})
    meter.usage({'prompt_tokens': 4000, 'gen_tokens': 100})
    before = meter.reading('The answer.')
    meter.keep({'type': 'context_kept', 'estimate': 1500})
    after = meter.reading('The answer.')
    assert after['used'] == before['used'] + 1200                         # 1,500 estimator units at this call's ratio
    assert after['parts']['kept_tools'] == before['parts']['kept_tools'] + 1200
    assert sum(after['parts'].values()) == after['used']
    assert before['parts']['kept_tools'] > 0                              # records of earlier replies were in the request


def test_this_replys_record_shows_even_when_the_fixed_rows_leave_messages_nothing():
    # Found 2026-11-15 at an 8K window: instructions and tools took the whole split, and the reply's own
    # record was folded into them instead of showing in its row.
    meter = chat_context.TurnMeter()
    meter.info({'round': 0, 'window': 8192, 'prompt_estimate': 4400,
                'parts': {'instructions': 2300, 'tools': 2000, 'messages': 100}})
    meter.usage({'prompt_tokens': 3400, 'gen_tokens': 60})
    meter.keep({'type': 'context_kept', 'estimate': 130})
    reading = meter.reading('Ok.')
    assert reading['parts']['kept_tools'] == round(130 * meter.ratio) > 0
    assert sum(reading['parts'].values()) == reading['used']


def test_records_in_the_history_are_their_own_row_not_messages():
    ledger = context_breakdown.Ledger()
    record = memory.digest([{'tool': 'execute_code', 'status': 'ok', 'kept': 1, 'arguments': '{"code": "print(1)"}', 'result': 'out ' * 300}], 5000)
    asked = {'role': 'user', 'content': 'Look at the code base\n\n' + record}
    history = [asked, {'role': 'assistant', 'content': 'Here is what I found.'}, {'role': 'user', 'content': 'And the tests?'}]
    messages = [{'role': 'system', 'content': 'You are helpful. ' * 20}, *history]
    ledger.start(messages)
    parts = ledger.sizes(messages, [(m, i + 1) for i, m in enumerate(history)], None)
    assert parts['kept_tools'] == chat_context.estimate(record)
    assert parts['messages'] == sum(chat_context.estimate(m) for m in history) - parts['kept_tools']
    assert parts['tool_calls'] == 0


# ── Through the real chat loop ──

def packets(*items):
    return httpx.Response(200, text=''.join(json.dumps(item) + '\n' for item in items))


def done(**extra):
    return {'message': {'content': ''}, 'done': True, 'done_reason': 'stop', 'prompt_eval_count': 900,
            'eval_count': 30, 'eval_duration': 1000000, **extra}


@pytest.fixture
def loop(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'chat.db'))
    monkeypatch.setattr(chat, '_suggest_workspace_memories', AsyncMock())
    monkeypatch.setattr(config, 'CHAT_TOOL_MEMORY', 'on')
    monkeypatch.setattr(config, 'CONTEXT_COMPACTION', 'on')
    reasoning._cache.clear()
    state = SimpleNamespace(calls=[], ran=[], bus=EventBus())

    async def fake_tool(http, events, name, args, conv_id, *rest, **more):
        state.ran.append(args.get('code'))
        return '**SUCCESS** | python | exit 0 | 1.0s\n\nstdout:\n```\nthe secret number is 4711\n```'
    monkeypatch.setattr(chat, 'exec_tool', fake_tool)

    def handle(request):
        if request.url.path == '/api/show':
            return httpx.Response(200, json={'capabilities': ['completion', 'tools']})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'fixture', 'digest': 'fixture'}]})
        if request.url.path == '/api/ps':
            return httpx.Response(200, json={'models': []})
        payload = json.loads(request.content)
        state.calls.append(payload)
        return state.reply(payload, len(state.calls))

    async def turn(messages, **request):
        req = ChatRequest(conversation_id='c', model='fixture', messages=messages, tool_ids=[], use_memories=False,
                          think_budget=0, **request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            events = [json.loads(s[5:]) async for s in chat.chat_stream_generate(req, http, state.bus, {}, {})]
        return events, (await db.get_conversation('c'))['messages']

    state.turn = turn
    return state


def call(code):
    tool_call = {'function': {'name': 'execute_code', 'arguments': {'description': 'Find the number', 'code': code, 'language': 'python'}}}
    return packets({'message': {'role': 'assistant', 'content': '', 'tool_calls': [tool_call]}, 'done': False}, done())


def answer(text):
    return packets({'message': {'role': 'assistant', 'content': text}, 'done': False}, done())


def as_history(rows):
    return [{'role': row['role'], 'content': row['content'], 'id': row['id']} for row in rows]


def test_tool_results_of_a_reply_are_sent_again_on_the_next_turn(loop):
    async def scenario():
        await db.init_db()
        await db.create_conversation('c', model='fixture')
        loop.reply = lambda payload, number: call('print(4711)') if number == 1 else answer('I found it.')
        events, saved = await loop.turn([{'role': 'user', 'content': 'Find the number'}], turn='new')
        assert not [e for e in events if e['type'] in ('error', 'context_kept')]      # the meter event is not for the browser
        reading = saved[-1]['metadata']['context']
        assert reading['parts']['kept_tools'] > 0 and sum(reading['parts'].values()) == reading['used']

        loop.reply = lambda payload, number: answer('It was 4711.')
        before = len(loop.calls)
        await loop.turn(as_history(saved) + [{'role': 'user', 'content': 'What was the number?'}], turn='new')
        request = loop.calls[before]
        asked = next(m for m in request['messages'] if m['role'] == 'user' and m['content'].startswith('Find the number'))
        assert memory.OPEN in asked['content'] and 'the secret number is 4711' in asked['content']
        assert '#1 execute_code: Find the number (ok)' in asked['content'] and 'print(4711)' in asked['content']
        # No extra messages: the record rides on the message that asked, so compaction still finds the history.
        assert [m['role'] for m in request['messages'] if m['role'] != 'system'] == ['user', 'assistant', 'user']
        assert len(loop.ran) == 1                                          # nothing was run again
        # What the user sees and what is saved is unchanged.
        assert memory.OPEN not in ''.join(m['content'] for m in (await db.get_conversation('c'))['messages'])
    asyncio.run(scenario())


def test_with_the_setting_off_only_reply_text_is_sent(loop, monkeypatch):
    async def scenario():
        await db.init_db()
        await db.create_conversation('c', model='fixture')
        loop.reply = lambda payload, number: call('print(4711)') if number == 1 else answer('I found it.')
        _, saved = await loop.turn([{'role': 'user', 'content': 'Find the number'}], turn='new')
        monkeypatch.setattr(config, 'CHAT_TOOL_MEMORY', 'off')
        loop.reply = lambda payload, number: answer('I do not know.')
        before = len(loop.calls)
        await loop.turn(as_history(saved) + [{'role': 'user', 'content': 'What was the number?'}], turn='new')
        assert not any(memory.OPEN in (m.get('content') or '') for m in loop.calls[before]['messages'])
    asyncio.run(scenario())


def test_compaction_still_finds_the_history_with_records_in_it(loop):
    async def scenario():
        await db.init_db()
        await db.create_conversation('c', model='fixture')
        loop.reply = lambda payload, number: call('print(4711)') if number == 1 else answer('I found it.')
        _, saved = await loop.turn([{'role': 'user', 'content': 'Find the number'}], turn='new')
        loop.reply = lambda payload, number: answer('It was 4711.')
        seen = {}
        original = chat_context.region
        def spy(messages, history):
            place = original(messages, history)
            seen.setdefault('places', []).append(place)
            return place
        chat_context.region, restore = spy, original
        try:
            await loop.turn(as_history(saved) + [{'role': 'user', 'content': 'What was the number?'}], turn='new')
        finally:
            chat_context.region = restore
        assert seen['places'] and all(place is not None for place in seen['places'])
    asyncio.run(scenario())
