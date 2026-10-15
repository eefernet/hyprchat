"""Email appointments remain proposals until the owner confirms them."""
import hashlib
import json
from datetime import datetime, timedelta
import database as db
from timeutil import local_to_utc


async def propose(message, event, timezone):
    owner=db.current_user_id()
    start=local_to_utc(str(event.get('start') or ''),timezone)
    if not start: raise ValueError('Appointment has no valid start time')
    title=str(event.get('title') or '').strip()[:200]
    if not title: raise ValueError('Appointment has no title')
    message_key=message.get('rfc_message_id') or message.get('message_id') or message['id']
    identity='proposal-'+hashlib.sha256(json.dumps([owner,message_key,start,title.casefold()],ensure_ascii=False).encode()).hexdigest()[:32]
    conn=await db.get_db()
    try:
        await conn.execute('INSERT OR IGNORE INTO calendar_proposals(id,user_id,message_id,message_key,title,start_at,end_at,description,timezone) VALUES(?,?,?,?,?,?,?,?,?)',
            (identity,owner,message['id'],message_key,title,start,(datetime.fromisoformat(start)+timedelta(hours=1)).isoformat(),
             'From email: '+str(message.get('subject') or '')[:300],timezone))
        await conn.commit()
    finally: await conn.close()
    return identity


async def list_proposals(status='pending'):
    conn=await db.get_db()
    try:
        rows=await conn.execute_fetchall('SELECT * FROM calendar_proposals WHERE user_id=?'+(' AND status=?' if status else '')+' ORDER BY created_at DESC LIMIT 200',
            (db.current_user_id(),status) if status else (db.current_user_id(),))
        return [dict(r) for r in rows]
    finally: await conn.close()


async def decide(identity,decision):
    if decision not in {'confirmed','rejected'}: raise ValueError('Invalid proposal decision')
    conn=await db.get_db()
    try:
        await conn.execute('BEGIN IMMEDIATE')
        rows=await conn.execute_fetchall('SELECT * FROM calendar_proposals WHERE id=? AND user_id=?',(identity,db.current_user_id()))
        if not rows: raise LookupError('Proposal not found')
        proposal=dict(rows[0])
        if proposal['status']==decision: return proposal
        if proposal['status']!='pending': raise ValueError('This proposal already has a decision')
        event_id=''
        if decision=='confirmed':
            event_id='event-'+identity.removeprefix('proposal-')
            await conn.execute("INSERT INTO calendar_events(id,user_id,title,start_at,end_at,description,sync_state) VALUES(?,?,?,?,?,?,'dirty')",
                (event_id,db.current_user_id(),proposal['title'],proposal['start_at'],proposal['end_at'],proposal['description']))
            await conn.execute('UPDATE email_messages SET extracted_event_id=? WHERE id=? AND user_id=?',
                (event_id,proposal['message_id'],db.current_user_id()))
        await conn.execute('UPDATE calendar_proposals SET status=?,event_id=?,decided_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?',
            (decision,event_id,identity,db.current_user_id()))
        await conn.commit()
        return {**proposal,'status':decision,'event_id':event_id}
    finally: await conn.close()
