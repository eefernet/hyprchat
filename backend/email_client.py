"""Jarvis email client — IMAP (imap-tools) + SMTP (aiosmtplib) wrapper.

imap-tools is synchronous; every IMAP call runs through asyncio.to_thread.
Bodies are fetched live and never stored (only headers/snippets are cached in
email_messages). Credentials come from the email_accounts row — never log
them, never include them in error strings shown to the model.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, parseaddr


def _imap_mailbox(account: dict):
    from imap_tools import MailBox, MailBoxUnencrypted
    cls = MailBox if account.get("imap_ssl", True) else MailBoxUnencrypted
    box = cls(account["imap_host"], port=int(account.get("imap_port") or 993))
    box.login(account["username"], account["password"], initial_folder="INBOX")
    return box


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _msg_snippet(msg) -> str:
    body = (msg.text or "").strip() or _html_to_text(msg.html or "")
    return re.sub(r"\s+", " ", body)[:500]


def _fetch_new_blocking(account: dict, last_seen_uid: int) -> dict:
    """New INBOX messages with uid > last_seen_uid. First poll (uid 0) imports
    only the latest 25 so a huge mailbox doesn't flood the cache. Returns
    {uidvalidity, max_uid, messages:[{uid,subject,from,to,date,snippet,unread}]}."""
    from imap_tools import AND
    with _imap_mailbox(account) as box:
        status = box.folder.status("INBOX")
        uidvalidity = str(status.get("UIDVALIDITY", ""))
        initial = not last_seen_uid
        criteria = AND(all=True) if initial else AND(uid=f"{last_seen_uid + 1}:*")
        out, max_uid = [], last_seen_uid
        for msg in box.fetch(criteria, mark_seen=False, bulk=True,
                             limit=25 if initial else 200,
                             reverse=initial, headers_only=False):
            uid = int(msg.uid or 0)
            if uid <= last_seen_uid:
                continue  # servers ignore open-ended UID ranges when empty
            max_uid = max(max_uid, uid)
            out.append({
                "uid": uid,
                "subject": msg.subject or "(no subject)",
                "from": msg.from_ or "",
                "to": ", ".join(msg.to or ()),
                "date": msg.date.isoformat() if isinstance(msg.date, datetime) else None,
                "snippet": _msg_snippet(msg),
                "unread": "\\Seen" not in (msg.flags or ()),
            })
        return {"uidvalidity": uidvalidity, "max_uid": max_uid, "messages": out}


def _fetch_body_blocking(account: dict, uid: int) -> dict:
    from imap_tools import AND
    with _imap_mailbox(account) as box:
        for msg in box.fetch(AND(uid=str(uid)), mark_seen=False):
            body = (msg.text or "").strip() or _html_to_text(msg.html or "")
            return {"subject": msg.subject or "", "from": msg.from_ or "",
                    "to": ", ".join(msg.to or ()), "date": str(msg.date or ""),
                    "body": body[:50000]}
    raise RuntimeError(f"message uid {uid} not found on the server")


def _flag_blocking(account: dict, uid: int, seen: bool | None = None,
                   archive: bool = False) -> None:
    from imap_tools import AND, MailMessageFlags
    with _imap_mailbox(account) as box:
        if seen is not None:
            box.flag(AND(uid=str(uid)), MailMessageFlags.SEEN, seen)
        if archive:
            # Prefer a real Archive folder; fall back to marking read.
            target = None
            for folder in box.folder.list():
                name = folder.name
                if name.lower() in ("archive", "[gmail]/all mail"):
                    target = name
                    break
            if target:
                box.move(AND(uid=str(uid)), target)
            else:
                box.flag(AND(uid=str(uid)), MailMessageFlags.SEEN, True)


def _test_blocking(account: dict) -> dict:
    with _imap_mailbox(account) as box:
        status = box.folder.status("INBOX")
        return {"status": "ok", "inbox_messages": int(status.get("MESSAGES", 0))}


async def fetch_new(account: dict, last_seen_uid: int) -> dict:
    return await asyncio.to_thread(_fetch_new_blocking, account, last_seen_uid)


async def fetch_body(account: dict, uid: int) -> dict:
    return await asyncio.to_thread(_fetch_body_blocking, account, uid)


async def set_flags(account: dict, uid: int, *, seen: bool | None = None,
                    archive: bool = False) -> None:
    await asyncio.to_thread(_flag_blocking, account, uid, seen, archive)


async def test_account(account: dict) -> dict:
    return await asyncio.to_thread(_test_blocking, account)


async def send_mail(account: dict, *, to: str, subject: str, body: str,
                    in_reply_to: str = "") -> None:
    import aiosmtplib
    if not account.get("smtp_host"):
        raise RuntimeError("SMTP host not configured for this account")
    message = EmailMessage()
    from_addr = account.get("from_address") or account["username"]
    label = account.get("label") or ""
    message["From"] = formataddr((label, from_addr)) if label else from_addr
    message["To"] = to
    message["Subject"] = subject[:300]
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.set_content(body[:100000])
    port = int(account.get("smtp_port") or 587)
    use_tls = bool(account.get("smtp_ssl", True)) and port == 465
    await aiosmtplib.send(
        message, hostname=account["smtp_host"], port=port,
        username=account["username"], password=account["password"],
        use_tls=use_tls, start_tls=(not use_tls) or None, timeout=30)


def clean_address(value: str) -> str:
    _, addr = parseaddr(value or "")
    return addr
