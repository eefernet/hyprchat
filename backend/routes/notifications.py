"""Jarvis notification routes — the queue the frontend bell polls, plus the
per-user push-channel prefs (ntfy config lives in the user_prefs KV)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database as db

router = APIRouter()


class MarkSeenRequest(BaseModel):
    ids: Optional[list[int]] = None  # None = mark everything seen


class NtfyPrefs(BaseModel):
    url: str = ""
    topic: str = ""
    token: str = ""
    allow_private: bool = False


@router.get("/api/notifications")
async def list_notifications(since_id: int = 0, limit: int = 50, unseen_only: bool = False):
    rows = await db.list_notifications(since_id=since_id, limit=min(limit, 200),
                                       unseen_only=unseen_only)
    unseen = await db.list_notifications(limit=200, unseen_only=True)
    return {"notifications": rows, "unseen_count": len(unseen)}


@router.post("/api/notifications/seen")
async def mark_seen(req: MarkSeenRequest):
    count = await db.mark_notifications_seen(req.ids)
    return {"status": "ok", "marked": count}


@router.delete("/api/notifications/{notification_id}")
async def delete_notification(notification_id: int):
    if not await db.delete_notification(notification_id):
        raise HTTPException(404, "Notification not found")
    return {"status": "deleted"}


@router.get("/api/notifications/channels")
async def get_channels():
    prefs = await db.get_user_pref("ntfy")
    ntfy = prefs if isinstance(prefs, dict) else {}
    # Never echo the bearer token back in full.
    token = str(ntfy.get("token") or "")
    return {"ntfy": {
        "url": ntfy.get("url") or "",
        "topic": ntfy.get("topic") or "",
        "token_set": bool(token),
        "allow_private": bool(ntfy.get("allow_private")),
    }}


@router.put("/api/notifications/channels/ntfy")
async def set_ntfy(req: NtfyPrefs):
    existing = await db.get_user_pref("ntfy")
    existing = existing if isinstance(existing, dict) else {}
    value = {
        "url": req.url.strip(),
        "topic": req.topic.strip(),
        # Empty token in the request keeps the stored one (masked round-trip).
        "token": req.token.strip() or str(existing.get("token") or ""),
        "allow_private": bool(req.allow_private),
    }
    await db.set_user_pref("ntfy", value)
    return {"status": "ok"}
