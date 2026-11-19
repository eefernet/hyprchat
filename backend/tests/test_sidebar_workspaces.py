"""Chat rows carry their workspace membership (2026-11-18 sidebar sections). Offline, temp SQLite."""
import asyncio
import sqlite3

import pytest

import database as db


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(db, "DATABASE_PATH", str(tmp_path / "sidebar.db"))
    monkeypatch.setattr(config, "KB_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setattr(config, "ARTIFACT_DIR", str(tmp_path / "artifacts"))
    asyncio.run(db.init_db())
    return str(tmp_path / "sidebar.db")


async def _seed():
    await db.create_workspace("ws-a", "Home lab")
    await db.create_workspace("ws-b", "Hyprchat")
    for cid in ("c1", "c2", "c3", "loose"):
        await db.create_conversation(cid, f"Chat {cid}", "m")
    await db.add_conv_to_workspace("ws-a", "c1")
    await db.add_conv_to_workspace("ws-a", "c2")
    await db.add_conv_to_workspace("ws-b", "c2")      # one chat, two workspaces
    await db.add_conv_to_workspace("ws-b", "c3")


def test_list_rows_name_every_workspace_a_chat_is_in(isolated):
    async def scenario():
        await _seed()
        rows = {c["id"]: c for c in await db.get_conversations(50, 0)}
        assert rows["c1"]["workspace_ids"] == ["ws-a"]
        assert sorted(rows["c2"]["workspace_ids"]) == ["ws-a", "ws-b"]
        assert rows["loose"]["workspace_ids"] == []
        # Membership is not the folder icon's rule: a chat in two workspaces has no effective workspace.
        assert rows["c2"]["effective_workspace"] is None
        assert (await db.get_conversation("c2"))["workspace_ids"] == rows["c2"]["workspace_ids"]
    asyncio.run(scenario())


def test_a_workspace_lists_only_its_own_chats(isolated):
    async def scenario():
        await _seed()
        assert sorted(c["id"] for c in await db.get_conversations(50, 0, workspace_id="ws-a")) == ["c1", "c2"]
        assert [c["id"] for c in await db.get_conversations(1, 0, workspace_id="ws-b")] != []
        assert await db.get_conversations(50, 0, workspace_id="nope") == []
        token = db.set_current_user_id("someone-else")
        try:
            assert await db.get_conversations(50, 0, workspace_id="ws-a") == []
        finally:
            db.reset_current_user_id(token)
    asyncio.run(scenario())


def test_pin_and_rename_do_not_count_as_activity(isolated):
    async def scenario():
        await _seed()
        conn = sqlite3.connect(isolated)
        conn.execute("UPDATE conversations SET updated_at='2026-01-01 00:00:00' WHERE id='c1'")
        conn.commit()
        await db.update_conversation("c1", pinned="1")
        await db.update_conversation("c1", title="Renamed")
        await db.update_conversation("c1", workspace_context={"mode": "off"})
        assert conn.execute("SELECT updated_at FROM conversations WHERE id='c1'").fetchone()[0] == "2026-01-01 00:00:00"
        await db.update_conversation("c1", model="other")          # a real change still counts
        assert conn.execute("SELECT updated_at FROM conversations WHERE id='c1'").fetchone()[0] != "2026-01-01 00:00:00"
    asyncio.run(scenario())


def test_a_fork_stays_in_its_workspace(isolated):
    async def scenario():
        await _seed()
        await db.update_conversation("c1", workspace_context={"mode": "selected", "workspace_id": "ws-a"})
        message_id = await db.add_message("c1", "user", "hello")
        fork = await db.fork_conversation("c1", message_id, "fork-1")
        assert fork["workspace_ids"] == ["ws-a"]
        assert fork["workspace_context"] == {"mode": "selected", "workspace_id": "ws-a"}
        assert fork["effective_workspace"]["id"] == "ws-a"
    asyncio.run(scenario())
