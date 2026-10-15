"""Jarvis calendar + CalDAV account routes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database as db
import pim

router = APIRouter()


class EventCreate(BaseModel):
    title: str
    start_local: str              # YYYY-MM-DDTHH:MM in the assistant timezone
    end_local: str = ""
    description: str = ""
    location: str = ""
    all_day: bool = False
    remind_minutes: Optional[int] = None
    rrule: Optional[str] = None


class EventUpdate(BaseModel):
    recurrence_id: Optional[str] = None
    title: Optional[str] = None
    start_local: Optional[str] = None
    end_local: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    all_day: Optional[bool] = None
    remind_minutes: Optional[int] = None
    rrule: Optional[str] = None


class CaldavAccountCreate(BaseModel):
    label: str = ""
    url: str
    username: str = ""
    password: str = ""
    calendar_url: str = ""


class CaldavAccountUpdate(BaseModel):
    label: Optional[str] = None
    url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None   # empty string keeps the stored one
    calendar_url: Optional[str] = None
    enabled: Optional[bool] = None


def _localized(event: dict, tz: str) -> dict:
    event["start_local"] = pim.utc_to_local(event.get("start_at") or "", tz)
    event["end_local"] = pim.utc_to_local(event.get("end_at") or "", tz)
    event["series_start_local"] = pim.utc_to_local(event.get("series_start_at") or "", tz)
    event["series_end_local"] = pim.utc_to_local(event.get("series_end_at") or "", tz)
    return event


@router.get("/api/calendar/events")
async def list_events(start_local: str = "", end_local: str = ""):
    tz = await pim.user_timezone()
    start = pim.local_to_utc(start_local, tz) or ""
    end = pim.local_to_utc(end_local, tz) or ""
    from calendar_resources import list_occurrences
    try:
        events = await list_occurrences(start=start, end=end)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return [_localized(e, tz) for e in events]


@router.post("/api/calendar/events")
async def create_event(req: EventCreate):
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "title is required")
    try:
        event = await pim.create_event(
            title=title, start_local=req.start_local, end_local=req.end_local,
            description=req.description, location=req.location,
            all_day=req.all_day, remind_minutes=req.remind_minutes, rrule=req.rrule or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _localized(event, await pim.user_timezone())


@router.patch("/api/calendar/events/{event_id}")
async def update_event(event_id: str, req: EventUpdate):
    if not await db.get_calendar_event(event_id):
        raise HTTPException(404, "Event not found")
    # exclude_unset so pim can tell "not sent" from an explicit null
    # (remind_minutes: null clears the reminder).
    try:
        event = await pim.update_event(event_id, req.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _localized(event, await pim.user_timezone())


@router.delete("/api/calendar/events/{event_id}")
async def delete_event(event_id: str):
    if not await pim.delete_event(event_id):
        raise HTTPException(404, "Event not found")
    return {"status": "deleted"}


# ── CalDAV accounts ──
@router.get('/api/calendar/proposals')
async def proposals(status: str = 'pending'):
    import calendar_proposals
    return await calendar_proposals.list_proposals(status)


@router.post('/api/calendar/proposals/{proposal_id}/{decision}')
async def decide_proposal(proposal_id: str, decision: str):
    import calendar_proposals
    try:
        return await calendar_proposals.decide(proposal_id,decision)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc



@router.get("/api/calendar/caldav")
async def list_caldav_accounts():
    return await db.list_caldav_accounts()


@router.post("/api/calendar/caldav")
async def create_caldav_account(req: CaldavAccountCreate):
    if not req.url.strip().lower().startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")
    return await db.create_caldav_account(
        f"caldav-{uuid.uuid4().hex[:8]}", label=req.label or req.url,
        url=req.url.strip(), username=req.username, password=req.password,
        calendar_url=req.calendar_url.strip())


@router.patch("/api/calendar/caldav/{account_id}")
async def update_caldav_account(account_id: str, req: CaldavAccountUpdate):
    if not await db.get_caldav_account(account_id):
        raise HTTPException(404, "Account not found")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if fields.get("password") == "":
        fields.pop("password")  # blank keeps the stored secret
    return await db.update_caldav_account(account_id, fields)


@router.delete("/api/calendar/caldav/{account_id}")
async def delete_caldav_account(account_id: str):
    if not await db.delete_caldav_account(account_id):
        raise HTTPException(404, "Account not found")
    return {"status": "deleted"}


@router.post("/api/calendar/caldav/{account_id}/test")
async def test_caldav_account(account_id: str):
    account = await db.get_caldav_account(account_id, with_password=True)
    if not account:
        raise HTTPException(404, "Account not found")
    try:
        import caldav_sync
        return await caldav_sync.test_account(account)
    except ImportError:
        raise HTTPException(503, "caldav package not installed on the server")
    except Exception as e:
        raise HTTPException(502, f"CalDAV connection failed: {e}")


@router.post("/api/calendar/sync")
async def sync_now():
    """On-demand sync of the current user's enabled accounts."""
    import caldav_sync
    accounts = [a for a in await db.caldav_accounts_for_sync()
                if a["user_id"] == db.current_user_id()]
    if not accounts:
        raise HTTPException(400, "No enabled CalDAV accounts")
    results = {}
    for account in accounts:
        try:
            results[account["id"]] = await caldav_sync.sync_account_locked(account)
            await db.update_caldav_account(account["id"], {
                "last_sync_at": datetime.utcnow().isoformat(), "last_error": ""})
        except Exception as e:
            results[account["id"]] = {"error": str(e)[:300]}
            await db.update_caldav_account(account["id"], {"last_error": str(e)[:500]})
    return results


@router.get('/api/calendar/conflicts')
async def calendar_conflicts():
    import calendar_resources
    return await calendar_resources.conflicts()


@router.post('/api/calendar/conflicts/{conflict_id}/{choice}')
async def resolve_calendar_conflict(conflict_id: int, choice: str):
    import calendar_resources
    try:
        return await calendar_resources.resolve(conflict_id, choice)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
