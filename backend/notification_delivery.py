"""Durable push attempts; never automatically repeat an uncertain send."""
import asyncio
from datetime import datetime, timedelta
import smtplib
import httpx
import database as db

MAX_ATTEMPTS = 4
_lock = asyncio.Lock()


async def enqueue(notification_id, channels, *, urgent=False, user_id=None):
    owner = user_id or db.current_user_id()
    conn = await db.get_db()
    try:
        for channel in channels:
            if channel not in {'ntfy','email'}: raise ValueError('Unknown notification channel')
            await conn.execute('INSERT OR IGNORE INTO notification_deliveries(notification_id,user_id,channel,urgent) SELECT id,user_id,?,? FROM notifications WHERE id=? AND user_id=?',
                (channel, int(urgent), notification_id, owner))
        await conn.commit()
    finally: await conn.close()


async def reconcile():
    conn = await db.get_db()
    try:
        await conn.execute("UPDATE notification_deliveries SET status='uncertain',error='Backend stopped during delivery; check the destination before retrying' WHERE status='sending'")
        await conn.execute("UPDATE notification_delivery_attempts SET status='uncertain',error='Backend stopped during delivery' WHERE status='sending'")
        await conn.commit()
    finally: await conn.close()


def classify(exc):
    # aiosmtplib has its own exception hierarchy, independent of smtplib.
    try:
        from aiosmtplib import errors as smtp_errors
    except ImportError:
        smtp_errors = None
    if smtp_errors is not None:
        if isinstance(exc,(smtp_errors.SMTPConnectError,smtp_errors.SMTPConnectTimeoutError)):
            return 'retry_wait'
        if isinstance(exc,smtp_errors.SMTPResponseException):
            return 'retry_wait' if 400 <= exc.code < 500 else 'failed'
        if isinstance(exc,smtp_errors.SMTPRecipientsRefused):
            return 'retry_wait' if exc.recipients and all(400 <= r.code < 500 for r in exc.recipients) else 'failed'
    # Connection failures happen before a request reaches its destination.
    if isinstance(exc,(httpx.ConnectError,httpx.ConnectTimeout,httpx.PoolTimeout,ConnectionRefusedError)):
        return 'retry_wait'
    if isinstance(exc,httpx.HTTPStatusError):
        code=exc.response.status_code
        if code == 429: return 'retry_wait'
        if 400 <= code < 500: return 'failed'
        return 'uncertain'  # Some servers fail after accepting a message.
    if isinstance(exc,smtplib.SMTPResponseException):
        return 'retry_wait' if 400 <= exc.smtp_code < 500 else 'failed'
    if isinstance(exc,(ValueError,smtplib.SMTPRecipientsRefused)):
        return 'failed'
    return 'uncertain'


async def deliver_due(notification_id=None):
    import notifications
    import quiet_hours
    # One send per process; the claim also guards direct notify() calls racing
    # the delivery service. No private message text is logged here.
    async with _lock:
        conn = await db.get_db()
        try:
            rows=await conn.execute_fetchall('''SELECT d.*,n.title,n.body FROM notification_deliveries d
                JOIN notifications n ON n.id=d.notification_id
                WHERE d.status IN ('queued','retry_wait','quiet_hours') AND d.next_attempt<=?'''
                + (' AND d.notification_id=?' if notification_id is not None else '')+' ORDER BY d.id LIMIT 20',
                (datetime.utcnow().isoformat(),notification_id) if notification_id is not None else (datetime.utcnow().isoformat(),))
            for row in rows:
                item=dict(row)
                token=db.set_current_user_id(item['user_id'])
                try:
                    profile=await db.get_assistant_profile(user_id=item['user_id'])
                    if quiet_hours.suppress_push(profile,urgent=bool(item['urgent'])):
                        await conn.execute("UPDATE notification_deliveries SET status='quiet_hours',next_attempt=? WHERE id=?",
                            ((datetime.utcnow()+timedelta(minutes=1)).isoformat(),item['id']))
                        await conn.commit()
                        continue
                    attempt=item['attempts']+1
                    claimed=await conn.execute("UPDATE notification_deliveries SET status='sending',attempts=?,updated_at=? WHERE id=? AND status IN ('queued','retry_wait','quiet_hours')",
                        (attempt,datetime.utcnow().isoformat(),item['id']))
                    if claimed.rowcount != 1: continue
                    await conn.execute("INSERT INTO notification_delivery_attempts(delivery_id,attempt,status) VALUES(?,?,'sending')",(item['id'],attempt))
                    await conn.commit()
                    state,error='delivered',''
                    try:
                        sender=notifications._send_ntfy if item['channel']=='ntfy' else notifications._send_self_email
                        delivered=await asyncio.wait_for(sender(item['title'],item['body'],user_id=item['user_id']),40)
                        if delivered is False: state,error='skipped','Channel is not configured'
                    except asyncio.CancelledError:
                        # Sending remains durable and becomes uncertain on restart.
                        raise
                    except Exception as exc:
                        state=classify(exc)
                        error=type(exc).__name__  # Exception strings can include URLs/tokens.
                        if state=='retry_wait' and attempt>=MAX_ATTEMPTS:
                            state='failed'
                    next_try=(datetime.utcnow()+timedelta(seconds=min(3600,30*2**(attempt-1)))).isoformat()
                    await conn.execute('UPDATE notification_deliveries SET status=?,error=?,next_attempt=?,updated_at=? WHERE id=?',
                        (state,error,next_try,datetime.utcnow().isoformat(),item['id']))
                    await conn.execute('UPDATE notification_delivery_attempts SET status=?,error=? WHERE delivery_id=? AND attempt=?',
                        (state,error,item['id'],attempt))
                    await conn.commit()
                finally: db.reset_current_user_id(token)
        finally: await conn.close()


async def list_deliveries(notification_id):
    conn=await db.get_db()
    try:
        rows=await conn.execute_fetchall('SELECT channel,status,attempts,next_attempt,error,updated_at FROM notification_deliveries WHERE notification_id=? AND user_id=? ORDER BY id',
            (notification_id,db.current_user_id()))
        return [dict(row) for row in rows]
    finally: await conn.close()
