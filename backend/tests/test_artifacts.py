import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("aiosqlite") is None,
    reason="aiosqlite not installed",
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def artifact_db(tmp_path, monkeypatch):
    import database

    monkeypatch.setattr(database, "DATABASE_PATH", str(tmp_path / "hyprchat-test.db"))
    token = database.set_current_user_id(database.DEFAULT_USER_ID)
    _run(database.init_db())
    yield database
    database.reset_current_user_id(token)


def test_add_conversation_file_creates_artifact_and_workspace_scope(artifact_db):
    db = artifact_db
    _run(db.create_conversation("conv-artifacts", "Artifacts"))
    _run(db.create_workspace("ws-artifacts", "Artifact Workspace"))
    _run(db.add_conv_to_workspace("ws-artifacts", "conv-artifacts"))
    msg_id = _run(db.add_message("conv-artifacts", "assistant", "done"))

    artifact = _run(db.add_conversation_file(
        "cf-artifacts-1",
        "conv-artifacts",
        "chart.png",
        "/api/downloads/chart.png",
        message_id=msg_id,
        metadata={"source_tool": "download_file"},
    ))

    assert artifact is not None
    assert artifact["kind"] == "image"
    assert artifact["workspace_id"] == "ws-artifacts"
    assert artifact["workspace_ids"] == ["ws-artifacts"]
    assert artifact["message_id"] == msg_id
    assert artifact["metadata"]["conversation_file_id"] == "cf-artifacts-1"

    by_workspace = _run(db.list_artifacts(workspace_id="ws-artifacts"))
    assert [a["id"] for a in by_workspace] == [artifact["id"]]

    updated = _run(db.update_artifact(artifact["id"], title="Chart", description="A generated chart"))
    assert updated["title"] == "Chart"
    assert updated["description"] == "A generated chart"

    _run(db.delete_conversation("conv-artifacts"))
    detached = _run(db.get_artifact(artifact["id"]))
    assert detached is not None
    assert detached["conversation_id"] is None
    assert detached["message_id"] is None
    assert detached["workspace_id"] == "ws-artifacts"

    assert _run(db.delete_artifact(artifact["id"])) is True
    assert _run(db.list_artifacts(workspace_id="ws-artifacts")) == []


def test_artifact_backfill_is_idempotent(artifact_db):
    db = artifact_db
    _run(db.create_conversation("conv-backfill", "Backfill"))

    async def seed_legacy_file():
        conn = await db.get_db()
        try:
            await conn.execute(
                "INSERT INTO conversation_files(id,conversation_id,filename,url) VALUES(?,?,?,?)",
                ("cf-legacy-1", "conv-backfill", "package.tar.gz", "/api/downloads/package.tar.gz"),
            )
            await conn.commit()
        finally:
            await conn.close()

    _run(seed_legacy_file())
    _run(db.init_db())
    first = _run(db.list_artifacts(conversation_id="conv-backfill"))
    _run(db.init_db())
    second = _run(db.list_artifacts(conversation_id="conv-backfill"))

    assert len(first) == 1
    assert len(second) == 1
    assert first[0]["kind"] == "archive"
    assert first[0]["metadata"]["source"] == "conversation_files_backfill"


def test_artifact_v2_filters_tags_workspaces_and_lineage(artifact_db, tmp_path):
    db = artifact_db
    _run(db.create_workspace("ws-a", "Workspace A"))
    _run(db.create_workspace("ws-b", "Workspace B"))

    source = tmp_path / "report.csv"
    source.write_text("name,score\nAda,10\nGrace,9\n", encoding="utf-8")

    artifact = _run(db.add_artifact(
        filename="report.csv",
        url="/api/downloads/report.csv",
        kind="data",
        mime_type="text/csv",
        title="Scores",
        description="Generated report",
        storage_path=str(source),
        size_bytes=source.stat().st_size,
        sha256="abc123",
        exists_status="present",
        status="accepted",
        pinned=True,
        workspace_ids=["ws-a", "ws-b"],
        tags=["Data", "Quarterly Report"],
        content_text=source.read_text(encoding="utf-8"),
        metadata={"source_tool": "download_file"},
    ))

    assert artifact["status"] == "accepted"
    assert artifact["pinned"] == 1
    assert artifact["workspace_id"] == "ws-a"
    assert artifact["workspace_ids"] == ["ws-a", "ws-b"]
    assert artifact["tags"] == ["data", "quarterly-report"]

    assert [a["id"] for a in _run(db.list_artifacts(workspace_id="ws-b"))] == [artifact["id"]]
    assert [a["id"] for a in _run(db.list_artifacts(status="accepted"))] == [artifact["id"]]
    assert [a["id"] for a in _run(db.list_artifacts(pinned=True))] == [artifact["id"]]
    assert [a["id"] for a in _run(db.list_artifacts(tag="quarterly-report"))] == [artifact["id"]]
    assert [a["id"] for a in _run(db.list_artifacts(q="Grace"))] == [artifact["id"]]
    assert [a["id"] for a in _run(db.list_artifacts(source="download_file"))] == [artifact["id"]]

    updated = _run(db.update_artifact(
        artifact["id"],
        status="archived",
        pinned=False,
        tags=["final"],
        workspace_ids=["ws-b"],
    ))
    assert updated["status"] == "archived"
    assert updated["pinned"] == 0
    assert updated["tags"] == ["final"]
    assert updated["workspace_id"] == "ws-b"
    assert updated["workspace_ids"] == ["ws-b"]

    child = _run(db.add_artifact(
        filename="report-v2.csv",
        url="/api/downloads/report-v2.csv",
        kind="data",
        parent_artifact_id=artifact["id"],
        supersedes_artifact_id=artifact["id"],
        workspace_ids=["ws-b"],
    ))
    detail = _run(db.get_artifact(child["id"]))
    assert detail["parent_artifact_id"] == artifact["id"]
    assert detail["supersedes_artifact_id"] == artifact["id"]
    assert [v["id"] for v in detail["versions"]] == [artifact["id"], child["id"]]
