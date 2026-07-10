"""Jarvis email routes — account CRUD (passwords masked), test-connection,
cached inbox, live body reads, send/draft-send, archive/mark-read, poll-now."""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database as db
from .context import route_context

router = APIRouter()


class EmailAccountCreate(BaseModel):
    label: str = ""
    imap_host: str
    imap_port: int = 993
    imap_ssl: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_ssl: bool = True
    username: str
    password: str
    from_address: str = ""


class EmailAccountUpdate(BaseModel):
    label: Optional[str] = None
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    imap_ssl: Optional[bool] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_ssl: Optional[bool] = None
    username: Optional[str] = None
    password: Optional[str] = None   # empty string keeps the stored one
    from_address: Optional[str] = None
    enabled: Optional[bool] = None
    allow_autonomous_send: Optional[bool] = None


class SendRequest(BaseModel):
    account_id: str = ""             # default: first account
    to: str
    subject: str
    body: str


class ReplyRequest(BaseModel):
    body: str = ""                   # empty = use the stored draft_reply


async def _account_or_404(account_id: str, *, with_password: bool = False) -> dict:
    account = await db.get_email_account(account_id, with_password=with_password)
    if not account:
        raise HTTPException(404, "Email account not found")
    return account


@router.get("/api/email/accounts")
async def list_accounts():
    return await db.list_email_accounts()


@router.post("/api/email/accounts")
async def create_account(req: EmailAccountCreate):
    if not req.imap_host.strip() or not req.username.strip():
        raise HTTPException(400, "imap_host and username are required")
    return await db.create_email_account(
        f"email-{uuid.uuid4().hex[:8]}", label=req.label or req.username,
        imap_host=req.imap_host.strip(), imap_port=req.imap_port,
        imap_ssl=req.imap_ssl, smtp_host=req.smtp_host.strip(),
        smtp_port=req.smtp_port, smtp_ssl=req.smtp_ssl,
        username=req.username.strip(), password=req.password,
        from_address=req.from_address.strip())


@router.patch("/api/email/accounts/{account_id}")
async def update_account(account_id: str, req: EmailAccountUpdate):
    await _account_or_404(account_id)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if fields.get("password") == "":
        fields.pop("password")
    return await db.update_email_account(account_id, fields)


@router.delete("/api/email/accounts/{account_id}")
async def delete_account(account_id: str):
    if not await db.delete_email_account(account_id):
        raise HTTPException(404, "Email account not found")
    return {"status": "deleted"}


@router.post("/api/email/accounts/{account_id}/test")
async def test_account(account_id: str):
    account = await _account_or_404(account_id, with_password=True)
    try:
        import email_client
        return await email_client.test_account(account)
    except ImportError:
        raise HTTPException(503, "imap-tools not installed on the server")
    except Exception as e:
        raise HTTPException(502, f"IMAP connection failed: {e}")


@router.post("/api/email/poll")
async def poll_now():
    """Force an immediate poll+triage cycle for the current user's accounts."""
    accounts = await db.list_email_accounts()
    if not accounts:
        raise HTTPException(400, "No email accounts configured")
    # Clear the interval gate so poll_due_accounts picks them up right now.
    for account in accounts:
        await db.update_email_account(account["id"], {"last_checked_at": None})
    import email_triage
    await email_triage.poll_due_accounts(route_context().http)
    return {"status": "ok", "accounts": [a["id"] for a in await db.list_email_accounts()]}


@router.get("/api/email/messages")
async def list_messages(account_id: str = "", limit: int = 50,
                        unread_only: bool = False, include_archived: bool = False):
    return await db.list_email_messages(account_id=account_id, limit=min(limit, 200),
                                        unread_only=unread_only,
                                        include_archived=include_archived)


@router.get("/api/email/messages/{message_id}/body")
async def read_body(message_id: str):
    msg = await db.get_email_message(message_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    account = await _account_or_404(msg["account_id"], with_password=True)
    try:
        import email_client
        body = await email_client.fetch_body(account, msg["uid"])
    except Exception as e:
        raise HTTPException(502, f"IMAP fetch failed: {e}")
    await db.update_email_message(message_id, {"unread": False})
    return body


@router.post("/api/email/messages/{message_id}/archive")
async def archive_message(message_id: str):
    msg = await db.get_email_message(message_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    account = await _account_or_404(msg["account_id"], with_password=True)
    try:
        import email_client
        await email_client.set_flags(account, msg["uid"], archive=True)
    except Exception as e:
        raise HTTPException(502, f"IMAP archive failed: {e}")
    return await db.update_email_message(message_id, {"archived": True, "unread": False})


@router.post("/api/email/messages/{message_id}/read")
async def mark_read(message_id: str):
    msg = await db.get_email_message(message_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    account = await _account_or_404(msg["account_id"], with_password=True)
    try:
        import email_client
        await email_client.set_flags(account, msg["uid"], seen=True)
    except Exception:
        pass  # cached state still flips; server flag is best-effort
    return await db.update_email_message(message_id, {"unread": False})


@router.post("/api/email/send")
async def send_email(req: SendRequest):
    """Explicit user send from the panel — not gated by allow_autonomous_send."""
    accounts = await db.list_email_accounts()
    if not accounts:
        raise HTTPException(400, "No email accounts configured")
    account_id = req.account_id or accounts[0]["id"]
    account = await _account_or_404(account_id, with_password=True)
    import email_client
    to = email_client.clean_address(req.to)
    if "@" not in to:
        raise HTTPException(400, "Invalid recipient address")
    if not req.subject.strip() or not req.body.strip():
        raise HTTPException(400, "subject and body are required")
    try:
        await email_client.send_mail(account, to=to, subject=req.subject, body=req.body)
    except Exception as e:
        raise HTTPException(502, f"SMTP send failed: {e}")
    return {"status": "sent", "to": to}


@router.post("/api/email/messages/{message_id}/reply")
async def send_reply(message_id: str, req: ReplyRequest):
    """Explicit user reply (or send-the-stored-draft) from the panel."""
    msg = await db.get_email_message(message_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    body = (req.body or msg.get("draft_reply") or "").strip()
    if not body:
        raise HTTPException(400, "No reply body (and no stored draft)")
    account = await _account_or_404(msg["account_id"], with_password=True)
    import email_client
    to = email_client.clean_address(msg["from_addr"])
    subject = msg["subject"] or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    try:
        await email_client.send_mail(account, to=to, subject=subject, body=body)
    except Exception as e:
        raise HTTPException(502, f"SMTP send failed: {e}")
    await db.update_email_message(message_id, {"draft_reply": "", "unread": False})
    return {"status": "sent", "to": to}
