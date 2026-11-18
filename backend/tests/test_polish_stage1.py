"""Polishing pass, stage 1 (2026-11-18): data loss, wrong action and ownership gaps.

One test per finding, named after what went wrong. Offline: temp SQLite, no model, no network.
"""
import asyncio

import pytest

import database as db


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", str(tmp_path / "polish.db"))
    asyncio.run(db.init_db())
    return tmp_path


def test_delete_all_chats_does_the_same_cleanup_as_deleting_one(fresh):
    """"Delete all chats" left run events, the Assistant's pinned-chat pointer and task targets behind, and
    returned no ids, so history recall kept the deleted text searchable."""
    async def run():
        await db.create_conversation(id="a", title="A")
        await db.create_conversation(id="b", title="B")
        await db.add_message("a", "user", "hello")
        other = await db.create_user("Other")
        token = db.set_current_user_id(other["id"])
        try:
            await db.create_conversation(id="theirs", title="Theirs")
        finally:
            db.reset_current_user_id(token)
        conn = await db.get_db()
        try:
            await conn.execute("INSERT INTO scheduled_tasks(id,user_id,title,prompt,schedule_kind,schedule_json,conversation_id,created_at,updated_at) "
                               "VALUES('t',?,'T','p','daily','{}','a','x','x')", (db.DEFAULT_USER_ID,))
            await conn.commit()
        finally:
            await conn.close()
        ids = await db.delete_all_conversations()
        assert sorted(ids) == ["a", "b"]
        assert await db.get_conversations() == []
        conn = await db.get_db()
        try:
            rows = await conn.execute_fetchall("SELECT conversation_id FROM scheduled_tasks WHERE id='t'")
            assert rows[0]["conversation_id"] == ""
            assert len(await conn.execute_fetchall("SELECT id FROM conversations")) == 1      # the other user's
            assert not await conn.execute_fetchall("SELECT id FROM messages")
        finally:
            await conn.close()
        assert await db.delete_all_conversations() == []
    asyncio.run(run())


def test_second_upload_under_the_same_name_replaces_the_row(fresh):
    """Two rows used to point at one file on disk; deleting either removed the file and the passages of both."""
    async def run():
        await db.create_kb("kb", "Docs")
        first = await db.add_kb_file("kb", "notes.md", "/x/notes.md", 10, "text/markdown")
        second = await db.add_kb_file("kb", "notes.md", "/x/notes.md", 25, "text/markdown", source_url="https://e.test/n")
        other = await db.add_kb_file("kb", "other.md", "/x/other.md", 5, "text/markdown")
        assert first == second and other != first
        files = (await db.get_kb("kb"))["files"]
        assert sorted((f["filename"], f["file_size"]) for f in files) == [("notes.md", 25), ("other.md", 5)]
        assert next(f for f in files if f["filename"] == "notes.md")["source_url"] == "https://e.test/n"
    asyncio.run(run())


def test_research_and_councils_only_use_knowledge_bases_the_caller_owns(fresh):
    """Retrieval is not user-scoped, so ids taken from a request must be filtered first."""
    main = pytest.importorskip("main")
    async def run():
        await db.create_kb("mine", "Mine")
        other = await db.create_user("Other")
        token = db.set_current_user_id(other["id"])
        try:
            await db.create_kb("theirs", "Theirs")
            assert await main._owned_kb_ids(["mine", "theirs"]) == ["theirs"]
        finally:
            db.reset_current_user_id(token)
        assert await main._owned_kb_ids(["theirs", "mine", "mine", "", None, "missing"]) == ["mine"]
        assert await main._owned_kb_ids(None) == []
    asyncio.run(run())


def test_webhook_address_stops_working_when_the_task_is_no_longer_a_webhook_task(fresh):
    async def run():
        task = await db.create_scheduled_task("t1", title="Hook", prompt="p", schedule_kind="webhook",
                                              schedule_json={}, webhook_token="tok-123")
        assert (await db.get_task_by_webhook_token("tok-123"))["id"] == "t1"
        await db.update_scheduled_task("t1", {"schedule_kind": "daily", "schedule_json": {"time": "08:00"}})
        assert await db.get_task_by_webhook_token("tok-123") is None      # also when the token is still stored
    asyncio.run(run())


def test_assistant_may_delete_mail_only_with_both_switches_on():
    """The delete tool checked the account switch alone, so turning the master "act on email" switch off
    still let mail be moved to Trash."""
    import email_gates
    on, off = {"allow_autonomous_email": 1}, {"allow_autonomous_email": 0}
    account = {"allow_autonomous_delete": 1, "allow_autonomous_send": 0}
    assert email_gates.allowed("delete", account, on)
    assert email_gates.allowed("delete", account, None)          # no profile row: the master switch is on
    assert not email_gates.allowed("delete", account, off)
    assert not email_gates.allowed("delete", {"allow_autonomous_delete": 0}, on)
    assert not email_gates.allowed("send", account, on)
    assert not email_gates.allowed("delete", None, on)
    import inspect, tools
    assert 'email_gates.allowed("delete"' in inspect.getsource(tools)
