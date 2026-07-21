"""Jarvis CalDAV sync — two-way sync between `calendar_events` and a remote
CalDAV calendar (iCloud, Nextcloud, Radicale, Fastmail, ...).

Rules:
- Pull first. Remote wins on conflict (phone edits are authoritative); a
  clobbered local edit is surfaced as a notification.
- Local rows with sync_state='dirty' push after the pull; 'deleted' tombstones
  delete the remote event then hard-delete the row.
- VEVENT only. RRULE is stored as text (no expansion in v1).

The `caldav` package is synchronous — every network call runs inside
asyncio.to_thread via `_sync_account_blocking`. The background loop rides the
scheduler tick (see scheduler._tick) and syncs each enabled account at most
every SYNC_INTERVAL_MIN minutes.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import database as db
import notifications

SYNC_INTERVAL_MIN = 10
_sync_lock = asyncio.Lock()


from timeutil import safe_zone as _safe_zone


def _to_utc_naive(value, tz: ZoneInfo) -> str | None:
    """icalendar DTSTART/DTEND value (date | naive dt | aware dt) → naive-UTC ISO.

    Everything downstream (calendar panel, reminders, gatherers) treats the
    stored value as UTC, so floating times and all-day dates must be
    interpreted in the account owner's timezone here — storing them raw shifts
    every all-day event by the user's UTC offset on display."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(ZoneInfo("UTC")).replace(tzinfo=None).isoformat()
        # Floating time — wall clock in the owner's tz.
        return (value.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))
                .replace(tzinfo=None).isoformat())
    if isinstance(value, date):
        # All-day — local midnight in the owner's tz.
        local_midnight = datetime(value.year, value.month, value.day, tzinfo=tz)
        return (local_midnight.astimezone(ZoneInfo("UTC"))
                .replace(tzinfo=None).isoformat())
    return None


def _event_fields_from_vevent(vevent, tz: ZoneInfo) -> dict | None:
    uid = str(vevent.get("UID") or "").strip()
    if not uid:
        return None
    dtstart = vevent.get("DTSTART")
    start_val = dtstart.dt if dtstart is not None else None
    dtend = vevent.get("DTEND")
    end_val = dtend.dt if dtend is not None else None
    all_day = isinstance(start_val, date) and not isinstance(start_val, datetime)
    rrule = vevent.get("RRULE")
    return {
        "uid": uid,
        "title": str(vevent.get("SUMMARY") or "(untitled)")[:300],
        "description": str(vevent.get("DESCRIPTION") or "")[:5000],
        "location": str(vevent.get("LOCATION") or "")[:300],
        "start_at": _to_utc_naive(start_val, tz),
        "end_at": _to_utc_naive(end_val, tz),
        "all_day": all_day,
        "rrule": rrule.to_ical().decode("utf-8", "replace") if rrule is not None else "",
    }


def _build_ics(event: dict, tz: ZoneInfo) -> str:
    from icalendar import Calendar, Event as IcsEvent
    cal = Calendar()
    cal.add("prodid", "-//HyprChat//Jarvis//EN")
    cal.add("version", "2.0")
    ics = IcsEvent()
    ics.add("uid", event["caldav_uid"])
    ics.add("summary", event.get("title") or "(untitled)")
    if event.get("description"):
        ics.add("description", event["description"])
    if event.get("location"):
        ics.add("location", event["location"])
    start = datetime.fromisoformat(event["start_at"]).replace(tzinfo=ZoneInfo("UTC"))
    if event.get("all_day"):
        # Stored start is local-midnight-converted-to-UTC; convert back to the
        # owner's tz before taking the date, or east-of-UTC users push the
        # previous day.
        ics.add("dtstart", start.astimezone(tz).date())
        end = (datetime.fromisoformat(event["end_at"]).replace(tzinfo=ZoneInfo("UTC"))
               if event.get("end_at") else start + timedelta(days=1))
        ics.add("dtend", end.astimezone(tz).date())
    else:
        ics.add("dtstart", start)
        end_raw = event.get("end_at")
        ics.add("dtend", (datetime.fromisoformat(end_raw).replace(tzinfo=ZoneInfo("UTC"))
                          if end_raw else start + timedelta(hours=1)))
    ics.add("dtstamp", datetime.now(ZoneInfo("UTC")))
    cal.add_component(ics)
    return cal.to_ical().decode("utf-8")


def _content_hash(fields: dict) -> str:
    """Change-detection hash over the fields we actually store (etag stand-in —
    real etags aren't uniformly exposed across servers). Deliberately NOT a
    hash of the raw resource: servers rewrite volatile fields (DTSTAMP,
    SEQUENCE) on normalization, which made every such rewrite look like a real
    change and could clobber a dirty local edit with a false conflict.
    Normalizes both sides: all_day bool-vs-0/1, and the same caps
    _event_fields_from_vevent applies."""
    basis = "\x1f".join((
        str(fields.get("title") or "")[:300],
        str(fields.get("description") or "")[:5000],
        str(fields.get("location") or "")[:300],
        str(fields.get("start_at") or ""),
        str(fields.get("end_at") or ""),
        str(int(bool(fields.get("all_day")))),
        str(fields.get("rrule") or ""),
    ))
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()[:32]


def _remote_snapshot_blocking(account: dict, tz: ZoneInfo) -> list[dict]:
    """Fetch all remote VEVENTs. Each entry: fields dict + content hash (etag
    stand-in — real etags aren't uniformly exposed across servers)."""
    import caldav
    client = caldav.DAVClient(url=account["url"], username=account["username"],
                              password=account["password"])
    if account.get("calendar_url"):
        calendar = caldav.Calendar(client=client, url=account["calendar_url"])
    else:
        calendars = client.principal().calendars()
        if not calendars:
            raise RuntimeError("No calendars found on the CalDAV server")
        calendar = calendars[0]
    out = []
    for remote in calendar.events():
        try:
            for comp in remote.icalendar_instance.walk("VEVENT"):
                fields = _event_fields_from_vevent(comp, tz)
                if fields and fields["start_at"]:
                    fields["hash"] = _content_hash(fields)
                    out.append(fields)
                break  # first VEVENT per resource; recurrence expansion is out of scope
        except Exception as e:
            print(f"[CALDAV] skipping unparseable remote event: {e}")
    return out


def _push_blocking(account: dict, ics_text: str) -> None:
    import caldav
    client = caldav.DAVClient(url=account["url"], username=account["username"],
                              password=account["password"])
    calendar = (caldav.Calendar(client=client, url=account["calendar_url"])
                if account.get("calendar_url")
                else client.principal().calendars()[0])
    calendar.save_event(ics_text)


def _delete_remote_blocking(account: dict, uid: str) -> None:
    import caldav
    client = caldav.DAVClient(url=account["url"], username=account["username"],
                              password=account["password"])
    calendar = (caldav.Calendar(client=client, url=account["calendar_url"])
                if account.get("calendar_url")
                else client.principal().calendars()[0])
    try:
        remote = calendar.event_by_uid(uid)
        remote.delete()
    except Exception as e:
        # Already gone remotely is fine.
        if "not found" not in str(e).lower() and "404" not in str(e):
            raise


async def test_account(account: dict) -> dict:
    """Connection test for the routes: list calendars."""
    def _blocking():
        import caldav
        client = caldav.DAVClient(url=account["url"], username=account["username"],
                                  password=account["password"])
        return [{"name": str(c.name or ""), "url": str(c.url)}
                for c in client.principal().calendars()]
    calendars = await asyncio.to_thread(_blocking)
    return {"status": "ok", "calendars": calendars}


async def sync_account(account: dict) -> dict:
    """Two-way sync for one account. Caller must hold the account owner's user
    contextvar. `account` must include the password."""
    user_id = account["user_id"]
    pulled = pushed = deleted = conflicts = 0
    import pim  # call-time import (module load order)
    tz = _safe_zone(await pim.user_timezone(user_id))

    # 1) Tombstones → delete remote, then hard-delete local.
    all_local = await db.list_calendar_events(include_deleted=True, user_id=user_id)
    mine = [e for e in all_local if (e.get("caldav_account_id") or "") in ("", account["id"])]
    for event in [e for e in mine if e.get("sync_state") == "deleted"]:
        if event.get("caldav_uid"):
            await asyncio.to_thread(_delete_remote_blocking, account, event["caldav_uid"])
        await db.delete_calendar_event(event["id"], user_id=user_id)
        deleted += 1

    # 2) Pull remote.
    remote_events = await asyncio.to_thread(_remote_snapshot_blocking, account, tz)
    remote_by_uid = {r["uid"]: r for r in remote_events}
    local_by_uid = {e["caldav_uid"]: e for e in mine
                    if e.get("caldav_uid") and e.get("sync_state") != "deleted"}

    for uid, remote in remote_by_uid.items():
        local = local_by_uid.get(uid)
        fields = {"title": remote["title"], "description": remote["description"],
                  "location": remote["location"], "start_at": remote["start_at"],
                  "end_at": remote["end_at"], "all_day": remote["all_day"],
                  "rrule": remote["rrule"], "caldav_etag": remote["hash"],
                  "caldav_account_id": account["id"], "sync_state": "synced"}
        if local is None:
            await db.create_calendar_event(
                f"event-{uuid.uuid4().hex[:10]}", title=remote["title"],
                start_at=remote["start_at"], end_at=remote["end_at"],
                description=remote["description"], location=remote["location"],
                all_day=remote["all_day"], rrule=remote["rrule"],
                caldav_account_id=account["id"], caldav_uid=uid,
                caldav_etag=remote["hash"], sync_state="synced", user_id=user_id)
            pulled += 1
        elif local.get("caldav_etag") != remote["hash"]:
            if local.get("sync_state") == "dirty":
                conflicts += 1
                await notifications.notify(
                    f"Calendar conflict: {remote['title']}",
                    "Both HyprChat and the CalDAV server changed this event — the server version won.",
                    kind="system", user_id=user_id)
            await db.update_calendar_event(local["id"], fields, user_id=user_id)
            pulled += 1

    # 3) Remote deletions → drop local synced rows whose uid vanished.
    for uid, local in local_by_uid.items():
        if uid not in remote_by_uid and local.get("sync_state") == "synced":
            await db.delete_calendar_event(local["id"], user_id=user_id)
            deleted += 1

    # 4) Push local dirty rows (new or edited-while-remote-unchanged).
    mine = [e for e in await db.list_calendar_events(user_id=user_id)
            if (e.get("caldav_account_id") or "") in ("", account["id"])]
    for event in [e for e in mine if e.get("sync_state") in ("dirty", "local") or not e.get("caldav_uid")]:
        if event.get("sync_state") == "synced":
            continue
        if not event.get("start_at"):
            continue
        uid = event.get("caldav_uid") or f"hyprchat-{event['id']}@jarvis"
        payload = {**event, "caldav_uid": uid}
        await asyncio.to_thread(_push_blocking, account, _build_ics(payload, tz))
        # Record the pushed content's hash as the etag so the next pull sees
        # the row as up-to-date instead of re-pulling what we just pushed.
        await db.update_calendar_event(event["id"], {
            "caldav_uid": uid, "caldav_account_id": account["id"],
            "caldav_etag": _content_hash(payload),
            "sync_state": "synced"}, user_id=user_id)
        pushed += 1

    return {"pulled": pulled, "pushed": pushed, "deleted": deleted, "conflicts": conflicts}


async def sync_account_locked(account: dict) -> dict:
    """Route-facing on-demand sync. Serializes with the background tick's
    sync_due_accounts — without the lock a manual "Sync now" racing the tick
    could double-create local rows for the same remote UID."""
    async with _sync_lock:
        return await sync_account(account)


async def sync_due_accounts() -> None:
    """Background entry point (rides the scheduler tick). Syncs each enabled
    account at most every SYNC_INTERVAL_MIN minutes; failures land on the
    account row, never raise."""
    if _sync_lock.locked():
        return
    async with _sync_lock:
        try:
            accounts = await db.caldav_accounts_for_sync()
        except Exception:
            return
        now = datetime.utcnow()
        for account in accounts:
            last = account.get("last_sync_at")
            if last:
                try:
                    if now - datetime.fromisoformat(str(last)) < timedelta(minutes=SYNC_INTERVAL_MIN):
                        continue
                except (ValueError, TypeError):
                    # TypeError: aware timestamp vs naive utcnow — treat as due
                    # rather than aborting the whole cross-user sync loop.
                    pass
            token = db.set_current_user_id(account["user_id"])
            try:
                result = await sync_account(account)
                await db.update_caldav_account(account["id"], {
                    "last_sync_at": now.isoformat(), "last_error": ""})
                if any(result.values()):
                    print(f"[CALDAV] synced {account['id']}: {result}")
            except Exception as e:
                await db.update_caldav_account(account["id"], {
                    "last_sync_at": now.isoformat(), "last_error": str(e)[:500]})
                print(f"[CALDAV] sync failed for {account['id']}: {e}")
            finally:
                db.reset_current_user_id(token)
