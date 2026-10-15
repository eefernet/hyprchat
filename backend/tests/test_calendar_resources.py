import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
import database as db
import calendar_resources as resources
import caldav_sync
import pim

ICS='''BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//fixture//EN
BEGIN:VEVENT
UID:weekly
DTSTART;TZID=America/New_York:20260301T090000
DTEND;TZID=America/New_York:20260301T100000
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE;TZID=America/New_York:20260315T090000
SUMMARY:Weekly
X-PRESERVED:custom value
END:VEVENT
BEGIN:VEVENT
UID:weekly
RECURRENCE-ID;TZID=America/New_York:20260308T090000
DTSTART;TZID=America/New_York:20260308T110000
DTEND;TZID=America/New_York:20260308T120000
SUMMARY:Exception
END:VEVENT
END:VCALENDAR'''


def event():
    return dict(id='event',user_id='main',title='Weekly',start_at='2026-03-01T14:00:00',end_at='2026-03-01T15:00:00',rrule='FREQ=WEEKLY;COUNT=4',resource_ics=ICS,timezone='America/New_York',caldav_uid='weekly')


def test_dst_exclusion_exception_and_stable_occurrence_identity():
    rows=resources.occurrences(event(),'2026-03-01T00:00:00','2026-04-01T00:00:00')
    assert [r['start_at'] for r in rows]==['2026-03-01T14:00:00','2026-03-08T15:00:00','2026-03-22T13:00:00']
    edited=resources.edit_resource(event(),{'title':'Changed','start_at':'2026-03-08T16:00:00'},ZoneInfo('America/New_York'),rows[1]['recurrence_id'])
    assert 'X-PRESERVED:custom value' in edited and 'EXDATE' in edited
    updated=resources.occurrences({**event(),'resource_ics':edited},'2026-03-01T00:00:00','2026-04-01T00:00:00')
    assert updated[1]['occurrence_id']==rows[1]['occurrence_id']
    assert updated[1]['title']=='Changed' and updated[0]['title']=='Weekly'
    assert updated[1]['start_at']=='2026-03-08T16:00:00'
    with pytest.raises(ValueError,match='366'):resources.occurrences(event(),'2026-01-01','2028-01-01')


def test_conflict_versions_and_reminder_dedup_survive_restart(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DATABASE_PATH',str(tmp_path/'calendar.db'))
    async def run():
        await db.init_db()
        row=await db.create_calendar_event('event',title='Weekly',start_at='2026-03-01T14:00:00',end_at='2026-03-01T15:00:00',rrule='FREQ=WEEKLY;COUNT=4',resource_ics=ICS,timezone='America/New_York',remind_minutes=30)
        assert await resources.deliver_occurrence_reminders(datetime(2026,3,8,14,45))==1
        await db.init_db()
        assert await resources.deliver_occurrence_reminders(datetime(2026,3,8,14,45))==0
        remote={**row,'title':'Remote title','hash':'remote-hash'}
        await resources.preserve_conflict(row,remote)
        current=await db.get_calendar_event('event');assert current['title']=='Weekly' and current['sync_state']=='conflict'
        conflicts=await resources.conflicts();assert len(conflicts)==1 and conflicts[0]['remote']['title']=='Remote title'
        with pytest.raises(ValueError,match='conflict'):await pim.update_event('event',{'title':'Overwrite'})
        resolved=await resources.resolve(conflicts[0]['id'],'local')
        assert resolved['title']=='Weekly' and resolved['sync_state']=='dirty' and resolved['caldav_etag']=='remote-hash'
        assert await resources.conflicts()==[]
    asyncio.run(run())


def test_sync_cannot_overwrite_or_delete_concurrent_local_edit(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DATABASE_PATH',str(tmp_path/'calendar.db'))
    async def run():
        await db.init_db()
        snapshot=await db.create_calendar_event('race',title='Original',start_at='2026-11-01T12:00:00')
        await db.update_calendar_event('race',{'title':'Local edit','sync_state':'dirty'})
        assert await db.update_calendar_event('race',{'title':'Remote edit','sync_state':'synced'},expected_updated_at=snapshot['updated_at']) is None
        assert not await db.delete_calendar_event('race',expected_updated_at=snapshot['updated_at'])
        await resources.preserve_conflict(snapshot,{'title':'Remote edit','hash':'remote'})
        conflict=(await resources.conflicts())[0]
        assert conflict['local']['title']=='Local edit'
        assert (await resources.resolve(conflict['id'],'local'))['title']=='Local edit'
    asyncio.run(run())
