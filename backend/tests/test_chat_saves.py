"""Saving what the preview pane shows as a file of its chat (2026-11-16)."""
import asyncio
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import database as db
from routes import chat_saves
from routes.chat_saves import SavePreviewRequest, save_name, save_preview

PAGE = "<!doctype html><html><body><h1>Hello</h1></body></html>"


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", str(tmp_path / "saves.db"))
    for name, folder in [("SANDBOX_OUTPUTS_DIR", "outputs"), ("ARTIFACT_DIR", "artifacts"), ("UPLOAD_DIR", "uploads"), ("KB_DIR", "kb")]:
        monkeypatch.setattr(config, name, str(tmp_path / folder))
        (tmp_path / folder).mkdir()
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "saves.db"), raising=False)

    async def make():
        await db.init_db()
        await db.create_conversation("conv-save", "Saves", "local-model")
    asyncio.run(make())
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def test_a_previewed_page_is_saved_as_a_file_of_its_chat(chat):
    result = run(save_preview("conv-save", SavePreviewRequest(filename="My page.html", content=PAGE)))
    artifact = result["artifact"]
    assert result["already_saved"] is False
    assert artifact["filename"] == "My page.html"
    assert artifact["conversation_id"] == "conv-save"
    assert artifact["url"].startswith("/api/downloads/") and artifact["url"].endswith("-My-page.html")
    stored = Path(artifact["storage_path"])
    assert stored.read_text() == PAGE
    # The browsable copy sits in the chat's own folder.
    folders = [p for p in (chat / "outputs").glob("*/conv-save/My page.html")]
    assert len(folders) == 1 and folders[0].read_text() == PAGE


def test_saving_the_same_content_twice_returns_the_file_that_exists(chat):
    first = run(save_preview("conv-save", SavePreviewRequest(filename="page.html", content=PAGE)))
    again = run(save_preview("conv-save", SavePreviewRequest(filename="page.html", content=PAGE)))
    assert again["already_saved"] is True
    assert again["artifact"]["id"] == first["artifact"]["id"]
    assert len(list((chat / "outputs").glob("*/conv-save/*"))) == 1
    # Changed content is a new file, and the first one is kept.
    changed = run(save_preview("conv-save", SavePreviewRequest(filename="page.html", content=PAGE + "<!-- v2 -->")))
    assert changed["already_saved"] is False and changed["artifact"]["id"] != first["artifact"]["id"]
    assert sorted(p.name for p in (chat / "outputs").glob("*/conv-save/*")) == ["page (2).html", "page.html"]


def test_a_chat_that_does_not_exist_or_belongs_to_another_profile_is_not_found(chat):
    with pytest.raises(HTTPException) as error:
        run(save_preview("conv-missing", SavePreviewRequest(filename="a.html", content=PAGE)))
    assert error.value.status_code == 404
    token = db.set_current_user_id("someone-else")
    try:
        with pytest.raises(HTTPException) as error:
            run(save_preview("conv-save", SavePreviewRequest(filename="a.html", content=PAGE)))
        assert error.value.status_code == 404
    finally:
        db.reset_current_user_id(token)
    assert not list((chat / "outputs").glob("*/*/*"))


def test_empty_and_oversized_content_are_refused_and_leave_nothing_behind(chat):
    with pytest.raises(HTTPException) as error:
        run(save_preview("conv-save", SavePreviewRequest(filename="a.md", content="   \n")))
    assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        run(save_preview("conv-save", SavePreviewRequest(filename="a.md", content="x" * (chat_saves.MAX_BYTES + 1))))
    assert error.value.status_code == 413
    assert not list((chat / "outputs").glob("*/*/*"))


@pytest.mark.parametrize("given,kind,expected", [
    ("report.md", "", "report.md"),
    ("Report", "markdown", "Report.md"),
    ("", "html", "preview.html"),
    ("chart", "svg", "chart.svg"),
    ("data", "table", "data.csv"),
    ("../../etc/notes.txt", "", "notes.txt"),
])
def test_the_saved_name_is_safe_and_gets_an_extension_from_what_was_previewed(given, kind, expected):
    assert save_name(given, kind) == expected


@pytest.mark.parametrize("given", ["run.sh", "tool.py", "page.php", "archive.tar.gz", "noext"])
def test_only_pages_documents_and_tables_can_be_saved(given):
    with pytest.raises(HTTPException) as error:
        save_name(given, "")
    assert error.value.status_code == 400
