import asyncio
import httpx
import pytest
import database as db
import notification_delivery as delivery
import notifications


@pytest.fixture
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DATABASE_PATH',str(tmp_path/'notify.db'))
    monkeypatch.setattr(delivery,'_lock',asyncio.Lock())
    asyncio.run(db.init_db())


async def due_again():
    conn=await db.get_db()
    try:
        await conn.execute("UPDATE notification_deliveries SET next_attempt='2000-01-01'")
        await conn.commit()
    finally: await conn.close()


def test_transient_failure_retries_but_read_timeout_is_uncertain(isolated,monkeypatch):
    async def run():
        calls=[]
        async def send(*a,**k):
            calls.append(1)
            if len(calls)==1: raise httpx.ConnectError('secret URL must not be saved')
            raise httpx.ReadTimeout('response lost after message accepted')
        monkeypatch.setattr(notifications,'_send_ntfy',send)
        identity=await notifications.notify('Fixture',ntfy=True)
        assert (await delivery.list_deliveries(identity))[0]['status']=='retry_wait'
        await due_again();await delivery.deliver_due()
        state=(await delivery.list_deliveries(identity))[0]
        assert state['status']=='uncertain' and state['error']=='ReadTimeout'
        await due_again();await delivery.deliver_due()
        assert len(calls)==2
    asyncio.run(run())


def test_quiet_hours_rechecked_before_actual_delivery(isolated,monkeypatch):
    async def run():
        import quiet_hours
        quiet=[True];calls=[]
        monkeypatch.setattr(quiet_hours,'suppress_push',lambda *a,**k:quiet[0])
        async def send(*a,**k):calls.append(1);return True
        monkeypatch.setattr(notifications,'_send_ntfy',send)
        identity=await notifications.notify('Fixture',ntfy=True)
        assert not calls
        assert (await delivery.list_deliveries(identity))[0]['attempts']==0
        quiet[0]=False;await due_again();await delivery.deliver_due()
        assert calls==[1]
        assert (await delivery.list_deliveries(identity))[0]['status']=='delivered'
    asyncio.run(run())


def test_interrupted_send_is_retained_without_resend(isolated,monkeypatch):
    async def run():
        identity=await db.add_notification('Fixture',delivery_channels=['email'])
        conn=await db.get_db()
        try:
            await conn.execute("UPDATE notification_deliveries SET status='sending',attempts=1")
            await conn.commit()
        finally: await conn.close()
        await delivery.reconcile()
        assert (await delivery.list_deliveries(identity))[0]['status']=='uncertain'
        other=await db.create_user('Other');token=db.set_current_user_id(other['id'])
        try: assert await delivery.list_deliveries(identity)==[]
        finally: db.reset_current_user_id(token)
    asyncio.run(run())


def test_async_smtp_definite_rejection_and_uncertain_disconnect():
    errors=pytest.importorskip('aiosmtplib.errors')
    assert delivery.classify(errors.SMTPResponseException(450,'temporary rejection'))=='retry_wait'
    assert delivery.classify(errors.SMTPResponseException(550,'permanent rejection'))=='failed'
    assert delivery.classify(errors.SMTPServerDisconnected('response lost'))=='uncertain'
