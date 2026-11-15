import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import chat_delivery
import database as db
import reasoning


def request(**kwargs):
    return SimpleNamespace(conversation_id='c', model='local', persona_id='', ephemeral=False,
                           **kwargs)


async def source(*events):
    for event in events:
        if isinstance(event, BaseException):
            raise event
        yield chat_delivery.sse(event)


@pytest.fixture
def capture(monkeypatch):
    save, usage = AsyncMock(), AsyncMock()
    monkeypatch.setattr(db, 'save_chat_delivery', save)
    monkeypatch.setattr(db, 'record_token_usage', usage)
    return save, usage


def drain(stream):
    async def run():
        return [json.loads(c[5:]) async for c in chat_delivery.deliver(stream, request())]
    return asyncio.run(run())


INIT = {'type':'init','message_id':42}
DONE = {'type':'done','message_id':42}
REVIEW = {'type':'refinement_start','round':1,'total':1}


def test_server_finalizes_before_done_without_browser(capture):
    save, _ = capture
    async def run():
        async for chunk in chat_delivery.deliver(source(INIT, {'type':'token','content':'answer'}, DONE), request()):
            if json.loads(chunk[5:])['type'] == 'done':
                assert save.call_args.args[1] == 'answer'
                assert save.call_args.args[2]['in_progress'] is False
    asyncio.run(run())


def test_shorter_review_replaces_only_when_complete_and_keeps_history(capture):
    save, _ = capture
    events=drain(source(INIT, {'type':'token','content':'long original '*100}, REVIEW,
                        {'type':'token','content':'Correct.'}, DONE))
    assert [e['content'] for e in events if e['type']=='token']==['long original '*100]
    assert [e['content'] for e in events if e['type']=='replace']==['Correct.']
    assert save.call_args.args[1]=='Correct.'
    assert len(save.call_args.args[2]['review_history'])==2


@pytest.mark.parametrize('ending', [
    [{'type':'error','error':'provider failed'}],
    [{'type':'review_incomplete'}, DONE],
    [{**DONE,'done_reason':'length'}],
])
def test_failed_or_incomplete_review_retains_answer(capture,ending):
    save,_=capture
    drain(source(INIT,{'type':'token','content':'original'},REVIEW,
                 {'type':'token','content':'partial replacement'},*ending))
    assert save.call_args.args[1]=='original'
    assert not save.call_args.args[2]['in_progress']


def test_disconnect_closes_producer_and_preserves_last_complete_review(capture):
    save,_=capture
    async def run():
        stream=chat_delivery.deliver(source(INIT,{'type':'token','content':'original'},REVIEW),request())
        async for chunk in stream:
            if json.loads(chunk[5:])['type']=='refinement_start':
                await stream.aclose()
                break
    asyncio.run(run())
    assert save.call_args.args[1]=='original'
    assert save.call_args.args[2]['delivery_status']=='interrupted'


def test_usage_includes_completed_calls_before_error(capture):
    save,usage=capture
    events=drain(source(INIT,{'type':'usage_round','prompt_tokens':10,'gen_tokens':3},
                        {'type':'token','content':'answer'},REVIEW,
                        {'type':'usage_round','prompt_tokens':20,'gen_tokens':7},
                        {'type':'error','error':'later failure'}))
    assert usage.await_count==2
    assert save.call_args.args[2]['total_gen_tokens']==10
    assert all(e['type']!='usage_round' for e in events)


def test_ghost_turn_never_persists(capture):
    save,usage=capture
    req=request();req.ephemeral=True
    async def ghost():
        return [c async for c in chat_delivery.deliver(source(INIT,{'type':'usage_round','prompt_tokens':2,'gen_tokens':1},DONE),req)]
    asyncio.run(ghost())
    save.assert_not_called();usage.assert_not_called()


@pytest.mark.parametrize('values,mode', [([False,True],'review'),([False],'review'),([], 'review'),
    (['low','medium','high'],'native'),([False,'low','medium','xhigh'],'native')])
def test_capability_modes(values,mode):
    assert reasoning.normalize('local',{'values':values})['mode']==mode


def test_native_choice_overrides_boolean_and_disables_reviews():
    req=request(effort_mode='auto',effort_rounds=3,reasoning_level='medium',reasoning_model='local')
    caps=reasoning.normalize('local',{'values':[False,'low','medium','xhigh'],'default':'medium'})
    result=reasoning.resolve(req,caps,False,'persona')
    assert result['thinking']=='medium' and result['review_rounds']==0
    req.reasoning_level=False
    assert reasoning.resolve(req,caps)['thinking'] is False
    req.reasoning_level='true'
    assert reasoning.resolve(req,caps)['thinking']=='medium'
    req.reasoning_model='different'
    assert reasoning.resolve(req,caps)['notice']


def test_boolean_models_and_old_clients_keep_review_rounds():
    caps=reasoning.normalize('local',{'values':[False,True],'default':True})
    result=reasoning.resolve(request(effort_mode='auto',effort_rounds=3),caps,False,'persona')
    assert result['review_rounds']==3 and result['thinking'] is False
    caps=reasoning.normalize('local',{'values':['low','medium','high']})
    assert reasoning.resolve(request(effort_rounds=2),caps)['mode']=='review'


def test_browser_finalization_cannot_overwrite_canonical_answer(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DATABASE_PATH',str(tmp_path/'chat.db'))
    async def run():
        await db.init_db();await db.create_conversation(id='c',title='test')
        mid=await db.add_message('c','assistant','',metadata={'run_ids':['run']})
        await db.save_chat_delivery(mid,'canonical',{'in_progress':False,'review_history':[{'content':'first'}]})
        await db.patch_message_presentation('c',mid,content='stale browser',metadata={'in_progress':True,'review_history':[],'saved_events':[]})
        row=(await db.get_conversation('c'))['messages'][0]
        assert row['content']=='canonical'
        assert row['metadata']['review_history']==[{'content':'first'}]
        assert row['metadata']['run_ids']==['run']
        with pytest.raises(LookupError):await db.patch_message_presentation('other',mid,content='wrong conversation')
    asyncio.run(run())


def test_early_failure_cannot_overwrite_continue_message(capture):
    save, _ = capture
    async def run():
        return [c async for c in chat_delivery.deliver(source({'type':'error','error':'validation failed'}),request(continue_message_id=42))]
    asyncio.run(run())
    save.assert_not_called()


def test_empty_response_is_finalized_with_visible_status(capture):
    save, _ = capture
    events = drain(source(INIT,DONE))
    assert 'no text response' in save.call_args.args[1]
    assert any(e['type']=='token' for e in events)


def test_restore_history_is_owner_scoped_and_blocked_during_generation(tmp_path,monkeypatch):
    from routes.jobs import restore_message_review, message_reviews
    from fastapi import BackgroundTasks, HTTPException
    monkeypatch.setattr(db,'DATABASE_PATH',str(tmp_path/'chat.db'))
    async def run():
        await db.init_db();await db.create_conversation(id='c',title='test')
        mid=await db.add_message('c','assistant','final')
        await db.save_chat_delivery(mid,'final',{'in_progress':True,'review_history':[{'content':'original'}]})
        with pytest.raises(HTTPException) as exc:await restore_message_review(mid,0,BackgroundTasks())
        assert exc.value.status_code==409
        await db.save_chat_delivery(mid,'final',{'in_progress':False})
        assert (await restore_message_review(mid,0,BackgroundTasks()))['content']=='original'
        assert len((await message_reviews(mid))['revisions'])==1
        with pytest.raises(HTTPException) as exc:await restore_message_review(mid,-1,BackgroundTasks())
        assert exc.value.status_code==404
        monkeypatch.setattr(db,'current_user_id',lambda:'another-owner')
        with pytest.raises(HTTPException) as exc:await message_reviews(mid)
        assert exc.value.status_code==404
    asyncio.run(run())


def test_error_reason_is_kept_on_the_persisted_row(capture):
    # conv-5f8704bb8897 (2026-11-12): a reload showed only "generation failed" after a cut-off tool call.
    save,_=capture
    drain(source(INIT,{'type':'token','content':'Here is the page'},
                 {'type':'error','error':'Context window full: about 3,369 of 3,550 tokens are used'}))
    assert save.call_args.args[1]=='Here is the page\n\n_[interrupted: Context window full: about 3,369 of 3,550 tokens are used]_'
    assert save.call_args.args[2]['delivery_status']=='failed'
    drain(source(INIT,{'type':'error','error':'llama-server  returned\ninvalid tool call arguments'}))
    assert save.call_args.args[1]=='_[No response — llama-server returned invalid tool call arguments]_'
    drain(source(INIT,{'type':'error'}))
    assert save.call_args.args[1]=='_[No response; generation failed.]_'


def test_the_context_reading_is_sent_saved_and_survives_a_tool_round(capture):
    # Live 2026-11-13: the header pill fell after every reply (it counted thinking and tool results,
    # which are not sent again) and dropped to 0 on reload (nothing was restored).
    save, _ = capture
    events = drain(source(
        INIT,
        {'type': 'context_info', 'round': 0, 'window': 32768, 'prompt_estimate': 6000, 'turn_only': 0, 'ratio': 0},
        {'type': 'ctx_update', 'gen_tokens': 0, 'prompt_tokens': 6000, 'live': True},
        {'type': 'token', 'content': 'Let me check. '},
        {'type': 'ctx_update', 'gen_tokens': 900, 'prompt_tokens': 4000},
        {'type': 'usage_round', 'round': 0, 'prompt_tokens': 4000, 'gen_tokens': 900},
        {'type': 'context_info', 'round': 1, 'window': 32768, 'prompt_estimate': 12000},
        {'type': 'token', 'content': 'Done: 6 rows.'},
        {'type': 'ctx_update', 'gen_tokens': 60, 'prompt_tokens': 8200},
        {'type': 'usage_round', 'round': 1, 'prompt_tokens': 8200, 'gen_tokens': 60},
        DONE))
    assert all(event['type'] != 'context_info' for event in events)
    updates = [event for event in events if event['type'] == 'ctx_update']
    assert updates[0]['measured'] is False and updates[0]['window'] == 32768
    assert updates[1]['measured'] is True and updates[1]['working'] == 4900 - updates[1]['used']   # thinking so far
    assert updates[2]['measured'] is True and updates[2]['working'] == 8260 - updates[2]['used']
    reading = events[-1]['context']
    assert reading == save.call_args.args[2]['context']
    assert 4000 < reading['used'] < 4020                # the first prompt plus the two short sentences
    assert reading['working_peak'] == 8260 - reading['used'] and reading['measured'] is True


def test_a_reply_without_a_model_call_has_no_reading(capture):
    save, _ = capture
    events = drain(source(INIT, {'type': 'token', 'content': 'A saved receipt.'}, DONE))
    assert 'context' not in events[-1] and 'context' not in save.call_args.args[2]


ROUND = {'type': 'context_info', 'round': 0, 'window': 8192, 'prompt_estimate': 100}


def test_clear_takes_back_only_the_current_rounds_text(capture):
    # Found 2026-11-13: a text-style tool call cleared the whole reply, so the text of earlier rounds vanished.
    save, _ = capture
    events = drain(source(INIT, ROUND, {'type': 'token', 'content': 'First I looked at the file. '},
                          {**ROUND, 'round': 1}, {'type': 'token', 'content': '<tool_call>{"name":"x"}</tool_call>'},
                          {'type': 'clear'}, {**ROUND, 'round': 2}, {'type': 'token', 'content': 'It has 6 rows.'}, DONE))
    assert save.call_args.args[1] == 'First I looked at the file. It has 6 rows.'
    cleared = next(event for event in events if event['type'] == 'clear')
    assert cleared == {'type': 'clear', 'keep': len('First I looked at the file. ')}


def test_clear_in_continue_mode_keeps_the_original_half(capture):
    # The same wipe erased the first half of a continued reply for good.
    save, _ = capture
    drain(source({**INIT, 'prior_content': 'The first half. '}, ROUND, {'type': 'token', 'content': 'thought\nleaked'},
                 {'type': 'clear'}, {'type': 'token', 'content': 'The second half.'}, DONE))
    assert save.call_args.args[1] == 'The first half. The second half.'


def test_a_failure_that_replaces_the_reply_still_clears_everything(capture):
    save, _ = capture
    events = drain(source(INIT, ROUND, {'type': 'token', 'content': 'Working on the document. '},
                          {'type': 'clear', 'all': True}, {'type': 'token', 'content': 'The document could not be saved.'}, DONE))
    assert save.call_args.args[1] == 'The document could not be saved.'
    assert next(event for event in events if event['type'] == 'clear')['keep'] == 0


def test_a_reply_cut_off_by_a_disconnect_is_saved_even_when_cancelled_twice(monkeypatch):
    # Found 2026-11-14: the web server cancels the request task again at every await once the browser has
    # gone. The second cancellation landed on the final save, so stopped replies stayed "running" with
    # no text (two such rows were on the live server).
    saved = []

    async def slow_save(message_id, content, metadata):
        await asyncio.sleep(0.05)
        saved.append((content, metadata['delivery_status']))

    monkeypatch.setattr(db, 'save_chat_delivery', slow_save)

    async def endless():
        yield chat_delivery.sse(INIT)
        yield chat_delivery.sse({'type': 'token', 'content': 'Half an answer'})
        await asyncio.sleep(30)

    async def scenario():
        async def consume():
            async for _ in chat_delivery.deliver(endless(), request()):
                pass
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()                       # what the server's cancel scope does
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert saved[-1] == ('Half an answer', 'interrupted')


# ── Chat bug-fix pass, stage 4 (2026-11-15) ──────────────────────────────────────────────────────────────

def test_an_unexpected_failure_reaches_the_browser_as_an_error(capture):
    # A bug or a malformed request inside the chat loop used to just close the connection: the browser
    # took that for a finished reply, and the saved row said "interrupted" with no reason.
    save, _ = capture
    events = drain(source(INIT, {'type': 'token', 'content': 'Half an answer'}, KeyError('role')))
    assert events[-1]['type'] == 'error' and 'KeyError' in events[-1]['error'] and events[-1]['saved_note'] is True
    assert save.call_args.args[1].startswith('Half an answer\n\n_[interrupted: KeyError')
    assert save.call_args.args[2]['delivery_status'] == 'failed'
    events = drain(source(INIT, ValueError('bad request')))
    assert [e['type'] for e in events][-2:] == ['token', 'error']
    assert save.call_args.args[1] == '_[No response — ValueError: bad request]_'


def test_an_error_event_says_the_saved_reply_already_carries_the_reason(capture):
    # The browser drew its own ⚠ block under the server's note: the same error twice.
    events = drain(source(INIT, {'type': 'error', 'error': 'boom'}))
    assert events[-2] == {'type': 'token', 'content': '_[No response — boom]_'}
    assert events[-1]['saved_note'] is True
    events = drain(source(INIT, {'type': 'token', 'content': 'partial'}, {'type': 'error', 'error': 'boom'}))
    assert events[-1]['saved_note'] is True and [e['type'] for e in events].count('token') == 1


def test_the_reply_records_who_wrote_it_and_whether_it_was_cut_off(capture):
    # The header showed the chat's CURRENT model on every reply, and "cut off" lived only in the browser's
    # copy of the metadata, where it came back after a successful Continue.
    save, _ = capture
    drain(source(INIT, {'type': 'model_routed', 'model': 'big:27b'}, {'type': 'token', 'content': 'x'},
                 {**DONE, 'done_reason': 'length'}))
    assert save.call_args.args[2]['model'] == 'big:27b' and save.call_args.args[2]['truncated'] is True
    drain(source(INIT, {'type': 'token', 'content': 'x'}, {**DONE, 'model': 'other:4b'}))
    assert save.call_args.args[2]['model'] == 'other:4b' and save.call_args.args[2]['truncated'] is False
    drain(source(INIT, {'type': 'token', 'content': 'x'}, {'type': 'error', 'error': 'boom'}))
    assert save.call_args.args[2]['model'] == 'local' and save.call_args.args[2]['truncated'] is False


def test_the_browsers_final_patch_cannot_bring_back_the_cut_off_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'p.db'))
    async def run():
        await db.init_db()
        await db.create_conversation('c', model='local')
        mid = await db.add_message('c', 'assistant', 'whole answer', metadata={'in_progress': True})
        await db.save_chat_delivery(mid, 'whole answer', {'in_progress': False, 'delivery_status': 'completed',
                                                          'model': 'local', 'truncated': False})
        await db.patch_message_presentation('c', mid, metadata={'truncated': True, 'model': 'someone-else', 'saved_events': []})
        row = next(m for m in (await db.get_conversation('c'))['messages'] if m['id'] == mid)
        assert row['metadata']['truncated'] is False and row['metadata']['model'] == 'local'
        assert row['metadata']['saved_events'] == []
    asyncio.run(run())
