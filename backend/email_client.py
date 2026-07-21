"""Jarvis email client — IMAP (imap-tools) + SMTP (aiosmtplib) wrapper.

imap-tools is synchronous; every IMAP call runs through asyncio.to_thread.
Bodies are fetched live and never stored (only headers/snippets are cached in
email_messages). Credentials come from the email_accounts row — never log
them, never include them in error strings shown to the model.
"""

from __future__ import annotations

import asyncio
import base64
import html as html_mod
import re
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, parseaddr


IMAP_TIMEOUT_SECONDS = 30

# Special-folder fallbacks when the server doesn't advertise RFC 6154
# special-use flags. Lowercase names.
_TRASH_NAMES = ("trash", "[gmail]/trash", "deleted items", "deleted messages",
                "deleted")
_ARCHIVE_NAMES = ("archive", "[gmail]/all mail")


def _imap_mailbox(account: dict):
    from imap_tools import MailBox, MailBoxUnencrypted
    cls = MailBox if account.get("imap_ssl", True) else MailBoxUnencrypted
    # The socket timeout guards the whole scheduler: the poller rides the tick
    # loop, so a blackholed IMAP host must fail fast instead of hanging a
    # to_thread worker (and the tick) forever.
    box = cls(account["imap_host"], port=int(account.get("imap_port") or 993),
              timeout=IMAP_TIMEOUT_SECONDS)
    box.login(account["username"], account["password"], initial_folder="INBOX")
    return box


def _find_special_folder(box, use_flag: str, names: tuple[str, ...]) -> str | None:
    """Resolve a special folder by RFC 6154 special-use flag first (survives
    localized folder names), then by the common-name fallback list."""
    try:
        folders = list(box.folder.list())
    except Exception:
        return None
    for folder in folders:
        if use_flag in (folder.flags or ()):
            return folder.name
    for folder in folders:
        if folder.name.lower() in names:
            return folder.name
    return None


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
            headers = {k.lower(): v for k, v in (msg.headers or {}).items()}
            rfc_message_id = str((headers.get("message-id") or ("",))[0]).strip()
            out.append({
                "uid": uid,
                "message_id": rfc_message_id,
                "subject": msg.subject or "(no subject)",
                "from": msg.from_ or "",
                "to": ", ".join(msg.to or ()),
                "date": msg.date.isoformat() if isinstance(msg.date, datetime) else None,
                "snippet": _msg_snippet(msg),
                "unread": "\\Seen" not in (msg.flags or ()),
            })
        return {"uidvalidity": uidvalidity, "max_uid": max_uid, "messages": out}


# Raw HTML cap (newsletters routinely run 100-300 KB) and total decoded bytes
# of inline cid: images embedded as data URIs in the response.
_HTML_BODY_CAP = 400_000
_INLINE_IMAGE_BUDGET = 3 * 1024 * 1024


def _inline_images(msg) -> list[dict]:
    """Image attachments referenced by Content-ID (multipart/related inline
    images — signatures, embedded screenshots) as base64, budget-capped."""
    out, spent = [], 0
    for att in (msg.attachments or ()):
        cid = (att.content_id or "").strip().strip("<>")
        ctype = (att.content_type or "").lower()
        payload = att.payload or b""
        # Strict whitelist — this value is interpolated into a data: URI
        # attribute by email_render, so a crafted Content-Type must not be
        # able to smuggle quotes/attributes past layer one.
        if not cid or not re.fullmatch(r"image/[a-z0-9.+-]+", ctype) or not payload:
            continue
        if spent + len(payload) > _INLINE_IMAGE_BUDGET:
            continue
        spent += len(payload)
        out.append({"cid": cid, "content_type": ctype,
                    "b64": base64.b64encode(payload).decode("ascii")})
    return out


def _fetch_body_blocking(account: dict, uid: int) -> dict:
    from imap_tools import AND
    with _imap_mailbox(account) as box:
        for msg in box.fetch(AND(uid=str(uid)), mark_seen=False):
            body = (msg.text or "").strip() or _html_to_text(msg.html or "")
            # `html` is RAW — sanitization happens at the delivery edge
            # (routes/email.py via email_render); the read_email chat tool
            # keeps using the plain-text `body`.
            return {"subject": msg.subject or "", "from": msg.from_ or "",
                    "to": ", ".join(msg.to or ()), "date": str(msg.date or ""),
                    "body": body[:50000],
                    "html": (msg.html or "")[:_HTML_BODY_CAP],
                    "inline_images": _inline_images(msg)}
    raise RuntimeError(f"message uid {uid} not found on the server")


def _flag_blocking(account: dict, uid: int, seen: bool | None = None,
                   archive: bool = False) -> bool:
    # flag/move/delete take UID strings, not AND() criteria — only fetch()
    # takes criteria. A criteria object here raises "uid ... is not string".
    # Returns True when an archive request did a REAL folder move (callers
    # report the mark-read fallback honestly instead of claiming a move).
    from imap_tools import MailMessageFlags
    moved = False
    with _imap_mailbox(account) as box:
        if seen is not None:
            box.flag(str(uid), MailMessageFlags.SEEN, seen)
        if archive:
            # Prefer a real Archive folder; fall back to marking read.
            target = _find_special_folder(box, "\\Archive", _ARCHIVE_NAMES)
            if target:
                box.move(str(uid), target)
                moved = True
            else:
                box.flag(str(uid), MailMessageFlags.SEEN, True)
    return moved


def _delete_blocking(account: dict, uid: int) -> None:
    with _imap_mailbox(account) as box:
        target = _find_special_folder(box, "\\Trash", _TRASH_NAMES)
        if not target:
            # Never fall through to \Deleted+EXPUNGE — that is permanent,
            # unrecoverable deletion nobody asked for.
            raise RuntimeError(
                "no Trash folder found on the IMAP server — refusing to "
                "permanently delete; archive the message instead")
        box.move(str(uid), target)


def _test_blocking(account: dict) -> dict:
    with _imap_mailbox(account) as box:
        status = box.folder.status("INBOX")
        return {"status": "ok", "inbox_messages": int(status.get("MESSAGES", 0))}


async def fetch_new(account: dict, last_seen_uid: int) -> dict:
    return await asyncio.to_thread(_fetch_new_blocking, account, last_seen_uid)


async def fetch_body(account: dict, uid: int) -> dict:
    return await asyncio.to_thread(_fetch_body_blocking, account, uid)


async def set_flags(account: dict, uid: int, *, seen: bool | None = None,
                    archive: bool = False) -> bool:
    """Returns True when an archive request really moved the message."""
    return await asyncio.to_thread(_flag_blocking, account, uid, seen, archive)


async def delete_message(account: dict, uid: int) -> None:
    await asyncio.to_thread(_delete_blocking, account, uid)


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
    # Model/user-supplied subject: strip CR/LF so it can never smuggle headers.
    message["Subject"] = re.sub(r"[\r\n]+", " ", subject or "").strip()[:300]
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
