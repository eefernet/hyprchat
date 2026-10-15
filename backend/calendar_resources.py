"""Preserved calendar resources, bounded occurrence views and conflict history."""
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import database as db

UTC = timezone.utc
MAX_OCCURRENCES = 2000


async def migrate(conn):
    columns = {row[1] for row in await conn.execute_fetchall('PRAGMA table_info(calendar_events)')}
    for name in ('resource_ics', 'timezone'):
        if name not in columns:
            await conn.execute(f"ALTER TABLE calendar_events ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    await conn.executescript('''
        CREATE TABLE IF NOT EXISTS calendar_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            local_json TEXT NOT NULL, remote_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS one_pending_calendar_conflict ON calendar_conflicts(event_id) WHERE status='pending';
        CREATE TABLE IF NOT EXISTS calendar_reminders (
            occurrence_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            notification_id TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    ''')


def parse(text):
    from icalendar import Calendar
    if len(text.encode()) > 2 * 1024 * 1024:
        raise ValueError('Calendar resource exceeds 2 MB')
    return Calendar.from_ical(text)


def semantic_hash(text):
    """Ignore server bookkeeping, retain recurrence exceptions and unknown fields."""
    calendar = parse(text)
    for component in calendar.walk():
        for key in ('DTSTAMP', 'LAST-MODIFIED', 'CREATED', 'SEQUENCE'):
            component.pop(key, None)
    return hashlib.sha256(calendar.to_ical()).hexdigest()[:32]


def resource(event, tz):
    if event.get('resource_ics'):
        return parse(event['resource_ics'])
    from caldav_sync import _build_ics
    return parse(_build_ics({**event, 'caldav_uid':event.get('caldav_uid') or event['id']}, tz))


def _instant(value, tz):
    from caldav_sync import _to_utc_naive
    return _to_utc_naive(value, tz)


def occurrences(event, start, end, tz_name='UTC'):
    import recurring_ical_events
    from caldav_sync import _event_fields_from_vevent
    tz = ZoneInfo(event.get('timezone') or tz_name)
    begin = datetime.fromisoformat(start).replace(tzinfo=UTC)
    stop = datetime.fromisoformat(end).replace(tzinfo=UTC)
    if stop <= begin or stop - begin > timedelta(days=366):
        raise ValueError('Calendar date window must be positive and at most 366 days')
    if not event.get('rrule') and not event.get('resource_ics'):
        return [{**event, 'series_id':event['id'], 'occurrence_id':event['id'], 'recurrence_id':''}] if event['start_at'] < end and (event.get('end_at') or event['start_at']) >= start else []
    output=[]
    for component in recurring_ical_events.of(resource(event, tz)).after(begin):
        fields = _event_fields_from_vevent(component, tz)
        if not fields or not fields['start_at']:
            continue
        if fields['start_at'] >= end:
            break
        if len(output) >= MAX_OCCURRENCES:
            raise ValueError('Calendar window contains too many occurrences; request a shorter window')
        rid=component.get('RECURRENCE-ID') or component.get('DTSTART')
        identity = _instant(rid.dt, tz)
        occurrence_id = event['id'] + ':' + hashlib.sha256(identity.encode()).hexdigest()[:24]
        output.append({**event, **{k:v for k,v in fields.items() if k not in ('uid','rrule')},
            'series_start_at':event['start_at'],'series_end_at':event.get('end_at'),
            'series_id':event['id'], 'occurrence_id':occurrence_id, 'recurrence_id':identity,
            'resource_ics':''})
    return output


async def list_occurrences(start='', end='', user_id=None):
    import pim
    now=datetime.now(UTC).replace(tzinfo=None)
    start=start or (now-timedelta(days=7)).isoformat()
    end=end or (datetime.fromisoformat(start)+timedelta(days=60)).isoformat()
    if not 0 < (datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds() <= 366*86400:
        raise ValueError('Calendar date window must be positive and at most 366 days')
    tz=await pim.user_timezone(user_id)
    rows=await db.list_calendar_events(limit=10000,user_id=user_id)
    if len(rows)>=10000: raise ValueError('Too many calendar resources to expand')
    result=[]
    for event in rows:
        result.extend(occurrences(event,start,end,tz))
        if len(result)>MAX_OCCURRENCES: raise ValueError('Request a shorter calendar date window')
    return sorted(result,key=lambda e:e['start_at'])


def edit_resource(event, changes, tz, recurrence_id=''):
    from icalendar import vRecur
    import recurring_ical_events
    cal=resource(event,tz)
    masters=[c for c in cal.walk('VEVENT') if not c.get('RECURRENCE-ID')]
    if not masters: raise ValueError('Calendar resource has no editable series')
    target=masters[0]
    if recurrence_id:
        at=datetime.fromisoformat(recurrence_id).replace(tzinfo=UTC)
        match=next((c for c in recurring_ical_events.of(cal).between(at-timedelta(days=2),at+timedelta(days=2))
            if _instant((c.get('RECURRENCE-ID') or c['DTSTART']).dt,tz)==recurrence_id),None)
        if match is None: raise ValueError('Occurrence is no longer available; refresh the calendar')
        target=copy.deepcopy(match)
        for key in ('RRULE','EXDATE','RDATE','RECURRENCE-ID'):target.pop(key,None)
        master_start=masters[0]['DTSTART'].dt
        target.add('recurrence-id', at.astimezone(master_start.tzinfo or tz) if isinstance(master_start,datetime) else at.astimezone(tz).date())
        cal.subcomponents=[c for c in cal.subcomponents if not (c.name=='VEVENT' and c.get('RECURRENCE-ID') and _instant(c['RECURRENCE-ID'].dt,tz)==recurrence_id)]
        cal.add_component(target)
    for key,ical_key in (('title','SUMMARY'),('description','DESCRIPTION'),('location','LOCATION')):
        if key in changes:
            target.pop(ical_key,None);target.add(ical_key,changes[key])
    for key,ical_key in (('start_at','DTSTART'),('end_at','DTEND')):
        if changes.get(key):
            original=target.get(ical_key) or target.get('DTSTART')
            original_dt=original.dt
            at=datetime.fromisoformat(changes[key]).replace(tzinfo=UTC)
            value=at.astimezone(getattr(original_dt,'tzinfo',None) or tz)
            if changes.get('all_day',event.get('all_day')):value=value.date()
            target.pop(ical_key,None);target.add(ical_key,value)
    if 'rrule' in changes:
        if recurrence_id:raise ValueError('Recurrence rules can only be changed for a series')
        target.pop('RRULE',None)
        if changes['rrule']:
            rule=vRecur.from_ical(changes['rrule'])
            if not rule.get('FREQ'):raise ValueError('Recurrence rule requires FREQ')
            target['RRULE']=rule
    if changes.get('cancel_occurrence'):target['STATUS']='CANCELLED'
    return cal.to_ical().decode()


async def preserve_conflict(local, remote):
    conn=await db.get_db()
    try:
        await conn.execute('BEGIN IMMEDIATE')
        latest=await conn.execute_fetchall('SELECT * FROM calendar_events WHERE id=? AND user_id=?',(local['id'],local['user_id']))
        if not latest:return
        local=dict(latest[0])
        await conn.execute('INSERT OR IGNORE INTO calendar_conflicts(event_id,user_id,local_json,remote_json) VALUES(?,?,?,?)',
            (local['id'],local['user_id'],json.dumps(local),json.dumps(remote)))
        await conn.execute("UPDATE calendar_events SET sync_state='conflict' WHERE id=? AND user_id=?",(local['id'],local['user_id']))
        await conn.commit()
    finally:await conn.close()


async def conflicts():
    conn=await db.get_db()
    try:
        rows=await conn.execute_fetchall("SELECT * FROM calendar_conflicts WHERE user_id=? AND status='pending'",(db.current_user_id(),))
        return [{**dict(r),'local':json.loads(r['local_json']),'remote':json.loads(r['remote_json'])} for r in rows]
    finally:await conn.close()


async def resolve(conflict_id, choice):
    if choice not in ('local','remote'):raise ValueError('Choose local or remote')
    conn=await db.get_db()
    try:
        await conn.execute('BEGIN IMMEDIATE')
        rows=await conn.execute_fetchall("SELECT * FROM calendar_conflicts WHERE id=? AND user_id=? AND status='pending'",(conflict_id,db.current_user_id()))
        if not rows:raise LookupError('Conflict not found')
        row=dict(rows[0]);remote=json.loads(row['remote_json']);local=json.loads(row['local_json'])
        if choice=='remote' and remote.get('deleted'):
            await conn.execute('DELETE FROM calendar_events WHERE id=? AND user_id=?',(row['event_id'],db.current_user_id()))
            await conn.commit()
            return {'id':row['event_id'],'status':'deleted'}
        fields=local if choice=='local' else remote
        keys=('title','description','location','start_at','end_at','all_day','rrule','resource_ics','timezone')
        await conn.execute('UPDATE calendar_events SET '+','.join(k+'=?' for k in keys)+',caldav_etag=?,sync_state=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?',
            tuple(fields.get(k) or (False if k=='all_day' else '') for k in keys)+(remote.get('hash',''),('deleted' if local.get('sync_state')=='deleted' else 'dirty') if choice=='local' else 'synced',row['event_id'],db.current_user_id()))
        await conn.execute('UPDATE calendar_conflicts SET status=?,resolved_at=CURRENT_TIMESTAMP WHERE id=?',(choice,conflict_id))
        await conn.commit()
    finally:await conn.close()
    return await db.get_calendar_event(row['event_id'])


async def deliver_occurrence_reminders(now=None):
    """Claim an occurrence and create its notification in one SQLite transaction."""
    import pim
    now=now or datetime.now(UTC).replace(tzinfo=None)
    conn=await db.get_db()
    fired=0
    try:
        rows=await conn.execute_fetchall("SELECT * FROM calendar_events WHERE remind_minutes IS NOT NULL AND sync_state!='deleted' AND (rrule!='' OR resource_ics LIKE '%RRULE:%' OR resource_ics LIKE '%RDATE:%')")
        for raw in rows:
            event=dict(raw)
            tz=await pim.user_timezone(event['user_id'])
            minutes=max(0,min(int(event['remind_minutes']),364*1440))
            for occurrence in occurrences(event,(now-timedelta(days=1)).isoformat(),(now+timedelta(minutes=minutes,seconds=1)).isoformat(),tz):
                if datetime.fromisoformat(occurrence['start_at'])-timedelta(minutes=minutes)>now:continue
                await conn.execute('BEGIN IMMEDIATE')
                claim=await conn.execute('INSERT OR IGNORE INTO calendar_reminders(occurrence_id,event_id,user_id) VALUES(?,?,?)',
                    (occurrence['occurrence_id'],event['id'],event['user_id']))
                if claim.rowcount:
                    when=pim.utc_to_local(occurrence['start_at'],tz)
                    notice=await conn.execute("INSERT INTO notifications(user_id,title,body,kind,created_at) VALUES(?,?,?,'reminder',?)",
                        (event['user_id'],'Upcoming: '+occurrence['title'],when+(' @ '+occurrence['location'] if occurrence.get('location') else ''),now.isoformat()))
                    await conn.execute('UPDATE calendar_reminders SET notification_id=? WHERE occurrence_id=?',(notice.lastrowid,occurrence['occurrence_id']))
                    await conn.execute("INSERT INTO notification_deliveries(notification_id,user_id,channel) VALUES(?,?,'ntfy')",(notice.lastrowid,event['user_id']))
                    fired+=1
                await conn.commit()
    finally:await conn.close()
    return fired
