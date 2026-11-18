"""Every event of one tool call carries the same id (tool_calls.py).

Seen live 2026-11-14: the Activity badge said "26 tools" for twelve code runs and one search, because one
call's start, progress and end events had nothing in common and each "start" was counted.
"""
import asyncio

import tool_calls
from events import EventBus


async def collect(bus, conv='c'):
    queue = await bus.subscribe(conv)
    async def drain():
        seen = []
        while not queue.empty():
            seen.append(queue.get_nowait())
        return seen
    return drain


def test_every_event_of_one_tool_call_carries_the_same_id():
    async def scenario():
        bus = EventBus()
        drain = await collect(bus)

        async def tool():
            scope = tool_calls.begin('c3-aa')
            try:
                await bus.emit('c', 'tool_start', {'tool': 'execute_code'})
                await bus.emit('c', 'tool_end', {'tool': 'execute_code'})
                await bus.emit('c', 'code_output', {'stdout': '2'})
            finally:
                tool_calls.end(scope)
        await asyncio.create_task(tool())
        assert [e['data'].get('tool_call_id') for e in await drain()] == ['c3-aa'] * 3
    asyncio.run(scenario())


def test_parallel_tool_calls_do_not_share_an_id():
    async def scenario():
        bus = EventBus()
        drain = await collect(bus)

        async def tool(call_id, name):
            scope = tool_calls.begin(call_id)
            await bus.emit('c', 'tool_start', {'tool': name})
            await asyncio.sleep(0.01)                     # the other task runs in between
            await bus.emit('c', 'tool_end', {'tool': name})
            tool_calls.end(scope)
        await asyncio.gather(asyncio.create_task(tool('a', 'read_file')), asyncio.create_task(tool('b', 'list_files')))
        seen = {(e['data']['tool'], e['data']['tool_call_id']) for e in await drain()}
        assert seen == {('read_file', 'a'), ('list_files', 'b')}
    asyncio.run(scenario())


def test_events_after_the_tool_returned_are_not_stamped():
    # A job the tool started inherits the task's context. It must not keep stamping a call that is over.
    async def scenario():
        bus = EventBus()
        drain = await collect(bus)
        later = asyncio.Event()

        async def background():
            await later.wait()
            await bus.emit('c', 'file_ready', {'filename': 'late.zip'})

        async def tool():
            scope = tool_calls.begin('a')
            job = asyncio.create_task(background())
            await bus.emit('c', 'tool_end', {'tool': 'start_coder_workflow'})
            tool_calls.end(scope)
            return job
        job = await asyncio.create_task(tool())
        later.set()
        await job
        first, late = await drain()
        assert first['data']['tool_call_id'] == 'a' and 'tool_call_id' not in late['data']
    asyncio.run(scenario())


def test_events_outside_a_tool_call_and_other_event_types_have_no_id():
    async def scenario():
        bus = EventBus()
        drain = await collect(bus)
        await bus.emit('c', 'tool_start', {'tool': 'processing'})
        scope = tool_calls.begin('a')
        payload = {'status': 'thinking'}
        await bus.emit('c', 'thinking', payload)
        await bus.emit('c', 'tool_start', {'tool': 'x', 'tool_call_id': 'given'})
        await bus.emit('c', 'tool_start', payload)
        tool_calls.end(scope)
        outside, thinking, given, stamped = await drain()
        assert 'tool_call_id' not in outside['data'] and 'tool_call_id' not in thinking['data']
        assert given['data']['tool_call_id'] == 'given' and stamped['data']['tool_call_id'] == 'a'
        assert payload == {'status': 'thinking'}          # the caller's dict is never changed
    asyncio.run(scenario())


def test_the_description_is_one_clean_line_and_is_not_part_of_the_duplicate_check():
    assert tool_calls.description({'description': '  Count the\n lines   of Python '}) == 'Count the lines of Python'
    assert tool_calls.description({'code': 'x'}) == '' and tool_calls.description('junk') == ''
    assert len(tool_calls.description({'description': 'x' * 500})) == 120
    code = {'code': 'print(1)', 'language': 'python'}
    # The same code with a new description is still the same call.
    assert tool_calls.comparable('execute_code', {**code, 'description': 'First try'}) == code
    assert tool_calls.comparable('execute_code', '{"code": "print(1)", "language": "python", "description": "x"}') == code
    assert tool_calls.comparable('run_shell', {'command': 'ls', 'description': 'kept'}) == {'command': 'ls', 'description': 'kept'}
    assert tool_calls.comparable('execute_code', 'not json') == 'not json'
