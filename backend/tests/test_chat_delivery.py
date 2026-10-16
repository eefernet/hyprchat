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
