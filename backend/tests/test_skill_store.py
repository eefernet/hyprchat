"""Skill storage against an isolated SQLite database."""
import asyncio
from datetime import datetime, timedelta

import pytest

import database as db
from db import skill_store

BODY = "## When to use\nWhenever images need resizing.\n\n## Steps\n1. Run the resize script.\n"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(db, "DATABASE_PATH", str(tmp_path / "skills.db"))
    monkeypatch.setattr(config, "KB_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setattr(config, "ARTIFACT_DIR", str(tmp_path / "artifacts"))
    asyncio.run(db.init_db())
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def as_user(user_id, coro_factory):
    async def scoped():
        token = db.set_current_user_id(user_id)
        try:
            return await coro_factory()
        finally:
            db.reset_current_user_id(token)
    return run(scoped())


def test_migration_is_idempotent_and_keeps_rows(isolated):
    async def scenario():
        await skill_store.create("resize-images", "Use when images need resizing", BODY)
        await db.init_db()
        await db.init_db()
        return await skill_store.list_skills()
    assert [s["name"] for s in run(scenario())] == ["resize-images"]


def test_skills_are_scoped_to_their_owner_and_names_are_unique_per_user(isolated):
    alice = as_user("alice", lambda: skill_store.create("resize-images", "Use when images need resizing", BODY))
    assert as_user("bob", lambda: skill_store.get(alice["id"])) is None
    assert as_user("bob", lambda: skill_store.list_skills()) == []
    assert as_user("bob", lambda: skill_store.update(alice["id"], body=BODY + "changed")) is None
    assert as_user("bob", lambda: skill_store.delete(alice["id"])) is False
    # The same name is free for another user, and taken for the owner.
    assert as_user("bob", lambda: skill_store.create("resize-images", "Use when images need resizing", BODY))["user_id"] == "bob"
    with pytest.raises(ValueError):
        as_user("alice", lambda: skill_store.create("resize-images", "Use when images need resizing", BODY))


def test_listing_order_is_category_then_name(isolated):
    async def scenario():
        await skill_store.create("zeta", "Use when zeta things happen", BODY, category="data")
        await skill_store.create("alpha", "Use when alpha things happen", BODY, category="web")
        await skill_store.create("beta", "Use when beta things happen", BODY, category="data")
        return [s["name"] for s in await skill_store.list_skills()]
    assert run(scenario()) == ["beta", "zeta", "alpha"]


def test_text_changes_keep_a_revision_and_revert_restores_it(isolated):
    async def scenario():
        skill = await skill_store.create("resize-images", "Use when images need resizing", BODY)
        same = await skill_store.update(skill["id"], body=BODY, category="media")
        changed = await skill_store.update(skill["id"], body=BODY + "2. Check the output.\n", origin="reviewer", reason="added a check")
        history = await skill_store.revisions(skill["id"])
        reverted = await skill_store.revert(skill["id"], history[0]["id"])
        return same, changed, history, reverted, await skill_store.revisions(skill["id"])
    same, changed, history, reverted, after = run(scenario())
    assert (same["version"], same["category"]) == (1, "media")        # no text change: no revision, no version bump
    assert changed["version"] == 2 and len(history) == 1
    assert history[0]["body"] == BODY and history[0]["reason"] == "added a check"
    assert reverted["body"].strip() == BODY.strip() and reverted["version"] == 3
    assert len(after) == 2 and "Check the output" in after[0]["body"]   # the replaced text became a revision


def test_only_the_newest_revisions_are_kept(isolated):
    async def scenario():
        skill = await skill_store.create("resize-images", "Use when images need resizing", BODY)
        for n in range(skill_store.REVISION_LIMIT + 4):
            await skill_store.update(skill["id"], body=BODY + f"edit {n}\n")
        return await skill_store.revisions(skill["id"])
    history = run(scenario())
    assert len(history) == skill_store.REVISION_LIMIT
    assert "edit 12" in history[0]["body"]


def test_a_pending_change_does_not_touch_the_live_text_until_applied(isolated):
    async def scenario():
        skill = await skill_store.create("resize-images", "Use when images need resizing", BODY)
        parked = await skill_store.set_pending(skill["id"], {"description": "Use when photos need resizing",
                                                             "body": BODY + "new step\n", "origin": "reviewer"})
        applied = await skill_store.apply_pending(skill["id"])
        return parked, applied
    parked, applied = run(scenario())
    assert parked["body"] == BODY and parked["pending"]["body"].endswith("new step\n")
    assert applied["pending"] is None and applied["body"].endswith("new step\n")
    assert applied["description"] == "Use when photos need resizing" and applied["version"] == 2


def test_using_a_stale_skill_makes_it_active_again(isolated):
    async def scenario():
        skill = await skill_store.create("resize-images", "Use when images need resizing", BODY, status="stale")
        await skill_store.touch(skill["id"])
        return await skill_store.get(skill["id"])
    skill = run(scenario())
    assert (skill["status"], skill["use_count"]) == ("active", 1) and skill["last_used_at"]


def test_files_and_conversation_taint(isolated):
    async def scenario():
        skill = await skill_store.create("resize-images", "Use when images need resizing", BODY)
        await skill_store.put_file(skill["id"], "scripts/resize.py", "print('v1')")
        await skill_store.put_file(skill["id"], "scripts/resize.py", "print('v2')")
        listing = await skill_store.files(skill["id"])
        content = await skill_store.get_file(skill["id"], "scripts/resize.py")
        removed = await skill_store.delete_file(skill["id"], "scripts/resize.py")
        await skill_store.taint("conv-1", "email")
        await skill_store.taint("conv-1", "connector")          # the first reason sticks
        return listing, content, removed, await skill_store.taint_reason("conv-1"), await skill_store.taint_reason("conv-2")
    listing, content, removed, tainted, clean = run(scenario())
    assert [(f["path"], f["size"]) for f in listing] == [("scripts/resize.py", 11)]
    assert content["content"] == "print('v2')" and removed
    assert (tainted, clean) == ("email", "")


def test_deleting_a_skill_removes_its_files_and_revisions(isolated):
    async def scenario():
        skill = await skill_store.create("resize-images", "Use when images need resizing", BODY)
        await skill_store.put_file(skill["id"], "scripts/resize.py", "print(1)")
        await skill_store.update(skill["id"], body=BODY + "more\n")
        await skill_store.delete(skill["id"])
        conn = await db.get_db()
        try:
            return [(await conn.execute_fetchall(f"SELECT COUNT(*) AS n FROM {t}"))[0]["n"]
                    for t in ("skills", "skill_files", "skill_revisions")]
        finally:
            await conn.close()
    assert run(scenario()) == [0, 0, 0]


def test_curator_retires_unused_learned_skills_and_spares_pinned_and_owner_written(isolated):
    now = datetime(2026, 11, 12)

    async def backdate(skill_id, days):
        conn = await db.get_db()
        try:
            await conn.execute("UPDATE skills SET created_at=?, last_used_at=NULL WHERE id=?",
                               ((now - timedelta(days=days)).isoformat(), skill_id))
            await conn.commit()
        finally:
            await conn.close()

    async def scenario():
        made = {}
        for name, origin, days, pinned in (("fresh", "reviewer", 5, False), ("idle", "reviewer", 40, False),
                                           ("forgotten", "agent", 120, False), ("pinned-old", "reviewer", 400, True),
                                           ("mine-old", "user", 400, False)):
            skill = await skill_store.create(name, f"Use when {name} things are needed", BODY, origin=origin)
            await backdate(skill["id"], days)
            if pinned:
                await skill_store.set_fields(skill["id"], pinned=True)
            made[name] = skill["id"]
        preview = await skill_store.curate("default", 30, 90, dry_run=True, now=now)
        untouched = {s["name"]: s["status"] for s in await skill_store.list_skills()}
        moves = await skill_store.curate("default", 30, 90, now=now)
        after = {s["name"]: s["status"] for s in await skill_store.list_skills()}
        again = await skill_store.curate("default", 30, 90, now=now)
        return preview, untouched, moves, after, again
    preview, untouched, moves, after, again = run(scenario())
    assert {(m["name"], m["to"]) for m in preview} == {("idle", "stale"), ("forgotten", "archived")}
    assert set(untouched.values()) == {"active"}                       # a dry run changes nothing
    assert moves == preview
    assert after == {"fresh": "active", "idle": "stale", "forgotten": "archived",
                     "pinned-old": "active", "mine-old": "active"}
    assert again == []
