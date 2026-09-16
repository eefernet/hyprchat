"""Jarvis notifications — in-app queue plus optional push channels.

The in-app `notifications` row ALWAYS lands (it's the durable channel the
frontend bell polls); push channels are best-effort fanout on top. ntfy config
lives in the per-user `user_prefs` key "ntfy":
    {"url": "https://ntfy.sh", "topic": "my-topic", "token": "", "allow_private": false}
The URL goes through connectors.assert_url_allowed so a LAN ntfy server needs
the explicit allow_private opt-in, matching the connector rules.

The email channel arrives with Phase 3 (user-owned SMTP accounts).
"""

from __future__ import annotations

import database as db

_http = None


def configure(http) -> None:
    global _http
    _http = http


async def notify(title: str, body: str = "", *, kind: str = "task",
                 source_task_id: str = "", conversation_id: str = "",
                 ntfy: bool = False, email: bool = False,
                 urgent: bool = False,
                 user_id: str | None = None) -> int:
    notification_id = await db.add_notification(
        title, body, kind=kind, source_task_id=source_task_id,
        conversation_id=conversation_id, user_id=user_id)
    # Quiet hours suppress only the push fanout — the in-app row above always
    # lands. `urgent=True` (e.g. urgent email triage) can pierce the window
    # when the profile's urgent_override is on. Fails open: a profile lookup
    # error must never eat a push.
    if ntfy or email:
        try:
            import quiet_hours
            profile = await db.get_assistant_profile(user_id=user_id)
            if quiet_hours.suppress_push(profile, urgent=urgent):
                ntfy = email = False
        except Exception as e:
            print(f"[NOTIFY] quiet-hours check failed (pushing anyway): {e}")
    if ntfy:
        try:
            await _send_ntfy(title, body, user_id=user_id)
        except Exception as e:
            # In-app already landed; push is best-effort.
            print(f"[NOTIFY] ntfy send failed: {e}")
    if email:
        try:
            await _send_self_email(title, body, user_id=user_id)
        except Exception as e:
            print(f"[NOTIFY] email send failed: {e}")
    return notification_id


async def _send_self_email(title: str, body: str, user_id: str | None = None) -> None:
    """Deliver a notification to the user's own address via their first
    configured SMTP account (Phase 3 channel)."""
    accounts = await db.list_email_accounts(user_id=user_id)
    if not accounts:
        return
    account = await db.get_email_account(accounts[0]["id"], with_password=True,
                                         user_id=user_id)
    if not account or not account.get("smtp_host"):
        return
    import email_client  # call-time: keep notifications import-light
    to = account.get("from_address") or account.get("username") or ""
    if "@" not in to:
        return
    await email_client.send_mail(account, to=to, subject=f"[HyprChat] {title}",
                                 body=body or title)


async def _send_ntfy(title: str, body: str, user_id: str | None = None) -> None:
    prefs = await db.get_user_pref("ntfy", user_id=user_id)
    if not isinstance(prefs, dict):
        return
    base_url = str(prefs.get("url") or "").strip().rstrip("/")
    topic = str(prefs.get("topic") or "").strip().strip("/")
    if not base_url or not topic or _http is None:
        return
    import connectors  # call-time: keep notifications import-light
    url = f"{base_url}/{topic}"
    await connectors.assert_url_allowed(url, bool(prefs.get("allow_private")))
    headers = {"Title": title.encode("ascii", "replace").decode("ascii")[:200]}
    token = str(prefs.get("token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = await _http.post(url, content=(body or title)[:4000], headers=headers, timeout=10)
    resp.raise_for_status()
