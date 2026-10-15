import asyncio
import pytest
import database as db
import calendar_proposals as proposals


@pytest.fixture
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DATABASE_PATH',str(tmp_path/'proposals.db'))
    asyncio.run(db.init_db())


def test_confirmation_is_atomic_deduplicated_and_rejection_is_retained(isolated):
    async def run():
        message={'id':'cached-message','rfc_message_id':'<stable@example>','subject':'Appointment'}
        event={'title':'Appointment','start':'2026-11-03T09:00'}
        identity=await proposals.propose(message,event,'America/New_York')
        assert await db.list_calendar_events()==[]
        assert await proposals.propose(message,event,'America/New_York')==identity
        assert await proposals.propose({**message,'id':'recached'},event,'America/New_York')==identity
        assert len(await proposals.list_proposals())==1
        first,second=await asyncio.gather(proposals.decide(identity,'confirmed'),proposals.decide(identity,'confirmed'))
        assert first['event_id']==second['event_id']
        events=await db.list_calendar_events()
        assert len(events)==1 and events[0]['start_at']=='2026-11-03T14:00:00'
        event['title']='Other'
        rejected=await proposals.propose(message,event,'UTC')
        await proposals.decide(rejected,'rejected')
        assert await proposals.propose(message,event,'UTC')==rejected
        assert await proposals.list_proposals()==[]
        with pytest.raises(ValueError): await proposals.decide(rejected,'confirmed')
    asyncio.run(run())


def test_other_profile_cannot_confirm_proposal(isolated):
    async def run():
        identity=await proposals.propose({'id':'private'}, {'title':'Private','start':'2026-10-10T10:00'},'UTC')
        other=await db.create_user('Other');token=db.set_current_user_id(other['id'])
        try:
            assert await proposals.list_proposals()==[]
            with pytest.raises(LookupError):await proposals.decide(identity,'confirmed')
        finally:db.reset_current_user_id(token)
    asyncio.run(run())
