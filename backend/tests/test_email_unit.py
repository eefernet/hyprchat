"""Offline unit tests for the Jarvis email subsystem.

Covers special-folder resolution (RFC 6154 flag first, name fallback), the
delete-refusal invariant (no Trash folder → error, NEVER a silent expunge),
archive move-vs-flag honesty, Message-ID capture, and the poller's
UIDVALIDITY purge + checkpoint-before-events ordering. IMAP/DB/LLM are all
faked — no server, no mailbox.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from .optional_deps import HAS_AIOSQLITE, install_aiosqlite_stub, module_stub

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

if not HAS_AIOSQLITE:
    install_aiosqlite_stub()
if importlib.util.find_spec("imap_tools") is None:
    module_stub(
        "imap_tools",
        MailBox=object, MailBoxUnencrypted=object,
        MailMessageFlags=SimpleNamespace(SEEN="\\Seen", DELETED="\\Deleted"),
        AND=lambda **kw: kw,
    )

import email_client  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────────

class FakeFolder:
    def __init__(self, name, flags=()):
        self.name = name
        self.flags = flags


class FakeFolderMgr:
    def __init__(self, folders):
        self._folders = folders

    def list(self):
        return self._folders

    def status(self, _name):
        return {"UIDVALIDITY": "42", "MESSAGES": 1}


class FakeBox:
    def __init__(self, folders=()):
        self.folder = FakeFolderMgr(list(folders))
        self.moved, self.deleted, self.flagged = [], [], []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def move(self, uid, target):
        self.moved.append((uid, target))

    def delete(self, uid):
        self.deleted.append(uid)

    def flag(self, uid, flag, value):
        self.flagged.append((uid, flag, value))


ACCOUNT = {"id": "email-1", "user_id": "u1", "imap_host": "imap.example.com",
           "username": "u", "password": "p", "imap_ssl": True}


# ── special-folder resolution ────────────────────────────────────────────

def test_special_use_flag_beats_name_list():
    box = FakeBox([FakeFolder("Papierkorb", ("\\HasNoChildren", "\\Trash")),
                   FakeFolder("Trash")])  # localized flagged folder listed first
    assert email_client._find_special_folder(box, "\\Trash", email_client._TRASH_NAMES) == "Papierkorb"


def test_name_fallback_when_no_flags():
    box = FakeBox([FakeFolder("INBOX"), FakeFolder("Deleted Items")])
    assert email_client._find_special_folder(box, "\\Trash", email_client._TRASH_NAMES) == "Deleted Items"


def test_no_match_returns_none():
    box = FakeBox([FakeFolder("INBOX"), FakeFolder("Papelera")])
    assert email_client._find_special_folder(box, "\\Trash", email_client._TRASH_NAMES) is None


# ── delete: refuse rather than expunge ───────────────────────────────────

def test_delete_moves_to_trash_when_found(monkeypatch):
    box = FakeBox([FakeFolder("[Gmail]/Trash", ("\\Trash",))])
    monkeypatch.setattr(email_client, "_imap_mailbox", lambda _a: box)
    email_client._delete_blocking(ACCOUNT, 7)
    assert box.moved == [("7", "[Gmail]/Trash")]
    assert box.deleted == []


def test_delete_refuses_without_trash_folder(monkeypatch):
    box = FakeBox([FakeFolder("INBOX")])
    monkeypatch.setattr(email_client, "_imap_mailbox", lambda _a: box)
    with pytest.raises(RuntimeError, match="refusing"):
        email_client._delete_blocking(ACCOUNT, 7)
    # The old behavior expunged (\Deleted + EXPUNGE) — must never come back.
    assert box.deleted == []
    assert box.moved == []


# ── archive: honest move-vs-flag status ──────────────────────────────────

def test_archive_returns_true_on_real_move(monkeypatch):
    box = FakeBox([FakeFolder("Archive", ("\\Archive",))])
    monkeypatch.setattr(email_client, "_imap_mailbox", lambda _a: box)
    assert email_client._flag_blocking(ACCOUNT, 7, archive=True) is True
    assert box.moved == [("7", "Archive")]


def test_archive_returns_false_on_mark_read_fallback(monkeypatch):
    box = FakeBox([FakeFolder("INBOX")])
    monkeypatch.setattr(email_client, "_imap_mailbox", lambda _a: box)
    assert email_client._flag_blocking(ACCOUNT, 7, archive=True) is False
    assert box.moved == []
    assert box.flagged  # fell back to \Seen


# ── fetch: Message-ID capture + uid skip guard ───────────────────────────

def test_fetch_new_captures_message_id_and_skips_seen_uids(monkeypatch):
    class FakeMsg:
        def __init__(self, uid, mid):
            self.uid = str(uid)
            self.subject = "s"
            self.from_ = "a@b.c"
            self.to = ("d@e.f",)
            self.date = None
            self.text = "body"
            self.html = ""
            self.flags = ()
            self.headers = {"Message-ID": (mid,)}

    class FetchBox(FakeBox):
        def fetch(self, _criteria, **_kw):
            return [FakeMsg(5, "<old@x>"), FakeMsg(9, "<new@x>")]

    monkeypatch.setattr(email_client, "_imap_mailbox", lambda _a: FetchBox())
    out = email_client._fetch_new_blocking(ACCOUNT, last_seen_uid=5)
    assert [m["uid"] for m in out["messages"]] == [9]  # uid 5 skipped
    assert out["messages"][0]["message_id"] == "<new@x>"
    assert out["max_uid"] == 9


# ── poller: UIDVALIDITY purge + checkpoint-before-events + event cap ─────

def _poller_fixtures(monkeypatch, *, messages, uidvalidity="new"):
    import email_triage
    import scheduler

    calls = []

    async def fake_accounts_for_poll():
        return [dict(ACCOUNT, last_seen_uid=100, last_uidvalidity="old",
                     last_checked_at=None)]

    async def fake_fetch_new(_account, last_seen):
        calls.append(("fetch", last_seen))
        return {"uidvalidity": uidvalidity, "max_uid": 100 + len(messages),
                "messages": messages}

    async def fake_purge(account_id, user_id=None):
        calls.append(("purge", account_id))
        return 5

    async def fake_upsert(_mid, **kw):
        calls.append(("upsert", kw.get("uid")))

    async def fake_update_account(_account_id, fields, user_id=None):
        if "last_seen_uid" in fields:
            calls.append(("checkpoint", fields["last_seen_uid"]))

    async def fake_fire_event(name, user_id=None):
        calls.append(("event", name))
        return 0

    async def fake_triage(_http, _account):
        calls.append(("triage",))
        return 0

    monkeypatch.setattr(email_triage.db, "email_accounts_for_poll", fake_accounts_for_poll)
    monkeypatch.setattr(email_triage.db, "delete_email_messages_for_account", fake_purge)
    monkeypatch.setattr(email_triage.db, "upsert_email_message", fake_upsert)
    monkeypatch.setattr(email_triage.db, "update_email_account", fake_update_account)
    monkeypatch.setattr(email_client, "fetch_new", fake_fetch_new)
    monkeypatch.setattr(scheduler, "fire_event", fake_fire_event)
    monkeypatch.setattr(email_triage, "triage_new_messages", fake_triage)
    return email_triage, calls


def _msg(uid):
    return {"uid": uid, "subject": "s", "from": "a@b.c", "to": "d@e.f",
            "date": None, "snippet": "x", "unread": True, "message_id": f"<{uid}@x>"}


def test_uidvalidity_change_purges_before_refetch(monkeypatch):
    email_triage, calls = _poller_fixtures(monkeypatch, messages=[_msg(101)])
    _run(email_triage.poll_due_accounts(None))
    fetches = [c for c in calls if c[0] == "fetch"]
    assert fetches == [("fetch", 100), ("fetch", 0)]  # full refetch from 0
    # Purge happens after the mismatch is detected and BEFORE the refetch.
    assert calls.index(("purge", "email-1")) < calls.index(("fetch", 0))


def test_checkpoint_advances_before_events_fire(monkeypatch):
    email_triage, calls = _poller_fixtures(
        monkeypatch, messages=[_msg(101)], uidvalidity="old")
    _run(email_triage.poll_due_accounts(None))
    checkpoint_at = calls.index(("checkpoint", 101))
    event_at = calls.index(("event", "email_received"))
    assert checkpoint_at < event_at  # crash between them can't re-fire events
    assert calls[-1] == ("triage",)


def test_event_fires_are_capped_per_poll(monkeypatch):
    many = [_msg(101 + i) for i in range(30)]
    email_triage, calls = _poller_fixtures(monkeypatch, messages=many, uidvalidity="old")
    _run(email_triage.poll_due_accounts(None))
    events = [c for c in calls if c[0] == "event"]
    assert len(events) == email_triage.EVENT_FIRE_CAP_PER_POLL


# ── fetch_body: raw HTML part + inline cid images (HTML email reader) ────

class _Att:
    def __init__(self, cid, ctype, payload):
        self.content_id = cid
        self.content_type = ctype
        self.payload = payload


def _body_msg(**over):
    class FakeMsg:
        uid = "7"
        subject = "s"
        from_ = "a@b.c"
        to = ("d@e.f",)
        date = None
        text = "plain part"
        html = "<html><body><p>hi</p></body></html>"
        attachments = ()
    m = FakeMsg()
    for k, v in over.items():
        setattr(m, k, v)
    return m


def _body_box(msg):
    class BodyBox(FakeBox):
        def fetch(self, _criteria, **_kw):
            return [msg]
    return BodyBox()


def test_fetch_body_returns_raw_html_and_text(monkeypatch):
    monkeypatch.setattr(email_client, "_imap_mailbox",
                        lambda _a: _body_box(_body_msg()))
    out = email_client._fetch_body_blocking(ACCOUNT, 7)
    assert out["body"] == "plain part"
    assert out["html"] == "<html><body><p>hi</p></body></html>"  # RAW — edge sanitizes
    assert out["inline_images"] == []


def test_fetch_body_extracts_inline_cid_images(monkeypatch):
    atts = (_Att("<logo@x>", "image/png", b"PNGDATA"),
            _Att("", "image/png", b"nocid"),          # no cid → skipped
            _Att("<doc@x>", "application/pdf", b"%PDF"))  # not an image → skipped
    monkeypatch.setattr(email_client, "_imap_mailbox",
                        lambda _a: _body_box(_body_msg(attachments=atts)))
    out = email_client._fetch_body_blocking(ACCOUNT, 7)
    assert len(out["inline_images"]) == 1
    img = out["inline_images"][0]
    assert img["cid"] == "logo@x"          # <> stripped
    assert img["content_type"] == "image/png"
    import base64 as _b64
    assert _b64.b64decode(img["b64"]) == b"PNGDATA"


def test_fetch_body_inline_image_budget(monkeypatch):
    big = _Att("<big@x>", "image/jpeg", b"x" * (email_client._INLINE_IMAGE_BUDGET + 1))
    small = _Att("<small@x>", "image/png", b"ok")
    monkeypatch.setattr(email_client, "_imap_mailbox",
                        lambda _a: _body_box(_body_msg(attachments=(big, small))))
    out = email_client._fetch_body_blocking(ACCOUNT, 7)
    # Oversized image skipped, the small one still fits.
    assert [i["cid"] for i in out["inline_images"]] == ["small@x"]


def test_fetch_body_html_cap(monkeypatch):
    huge = "<p>" + "a" * (email_client._HTML_BODY_CAP + 100)
    monkeypatch.setattr(email_client, "_imap_mailbox",
                        lambda _a: _body_box(_body_msg(html=huge)))
    out = email_client._fetch_body_blocking(ACCOUNT, 7)
    assert len(out["html"]) == email_client._HTML_BODY_CAP
