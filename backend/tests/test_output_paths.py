"""Delivered files: one folder per chat and month, nothing overwritten, one address per delivery.

Found 2026-11-16: every tool wrote the bare file name into one flat folder, so `index.html` from one chat
replaced `index.html` from another, and three download links on the live server served a newer chat's
archive instead of their own.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import output_paths

WHEN = datetime(2026, 11, 16, tzinfo=timezone.utc)


@pytest.fixture()
def outputs(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    monkeypatch.setattr(config, "SANDBOX_OUTPUTS_DIR", str(root))
    return root


def test_a_file_goes_into_its_chats_folder_for_the_month(outputs):
    spot = output_paths.place("conv-abc123", "index.html", when=WHEN)
    assert Path(spot.path) == outputs / "2026-11" / "conv-abc123" / "index.html"
    assert spot.filename == "index.html"
    assert Path(spot.path).parent.is_dir()


def test_the_same_name_from_two_chats_does_not_overwrite(outputs):
    first = output_paths.place("conv-a", "index.html", when=WHEN)
    Path(first.path).write_text("from a")
    second = output_paths.place("conv-b", "index.html", when=WHEN)
    Path(second.path).write_text("from b")
    assert first.path != second.path
    assert Path(first.path).read_text() == "from a"


def test_a_repeated_name_in_one_chat_gets_a_number_and_keeps_the_first(outputs):
    first = output_paths.place("conv-a", "report.md", when=WHEN)
    Path(first.path).write_text("one")
    second = output_paths.place("conv-a", "report.md", when=WHEN)
    Path(second.path).write_text("two")
    third = output_paths.place("conv-a", "report.md", when=WHEN)
    assert Path(second.path).name == "report (2).md"
    assert Path(third.path).name == "report (3).md"
    assert Path(first.path).read_text() == "one"
    # What people see and download stays the plain name.
    assert second.filename == third.filename == "report.md"


def test_each_delivery_has_its_own_download_address(outputs):
    first = output_paths.place("conv-a", "site.tar.gz", when=WHEN)
    Path(first.path).write_text("x")
    second = output_paths.place("conv-b", "site.tar.gz", when=WHEN)
    assert first.url != second.url
    assert first.url.startswith("/api/downloads/") and first.url.endswith("-site.tar.gz")
    # One path segment, so the download route's own name check accepts it.
    assert "/" not in first.url_name and os.path.basename(first.url_name) == first.url_name


def test_names_that_are_already_unique_keep_their_name_and_address(outputs):
    spot = output_paths.place("conv-a", "candidate-0123abcd-cw3-1-rev.tar.gz", keep_name=True, when=WHEN)
    assert spot.url == "/api/downloads/candidate-0123abcd-cw3-1-rev.tar.gz"
    assert Path(spot.path).name == "candidate-0123abcd-cw3-1-rev.tar.gz"
    assert Path(spot.path).parent == outputs / "2026-11" / "conv-a"


@pytest.mark.parametrize("chat", ["../../etc", "/abs/olute", "a/b", "..", ".", "", None, "conv\x00x"])
def test_a_chat_id_can_never_leave_the_outputs_folder(outputs, chat):
    spot = output_paths.place(chat, "x.txt", when=WHEN)
    path = Path(spot.path).resolve()
    assert path.is_relative_to(outputs.resolve())
    assert path.parent.parent == (outputs / "2026-11").resolve()


@pytest.mark.parametrize("name,expected", [
    ("../../secret.txt", "secret.txt"), ("/etc/passwd", "passwd"), ("a\\b\\c.txt", "c.txt"),
    ("", "file"), ("..", "file"), (".", "file"), ("nul\x00l.txt", "null.txt"), ("  spaced name.md ", "spaced name.md"),
])
def test_a_file_name_is_reduced_to_its_last_safe_segment(outputs, name, expected):
    spot = output_paths.place("conv-a", name, when=WHEN)
    assert Path(spot.path).name == expected
    assert Path(spot.path).resolve().is_relative_to(outputs.resolve())


def test_files_with_no_chat_go_to_named_buckets(outputs):
    assert Path(output_paths.place(output_paths.IMAGE_STUDIO, "comfy_1_0.png", keep_name=True, when=WHEN).path).parent.name == "image-studio"
    assert Path(output_paths.place(output_paths.LIBRARY, "bundle.zip", when=WHEN).path).parent.name == "library"
    assert Path(output_paths.place(None, "x", when=WHEN).path).parent.name == "unsorted"


def test_an_old_flat_file_and_a_new_nested_file_are_both_found_by_name(outputs):
    outputs.mkdir()
    (outputs / "old.tar.gz").write_text("flat")
    nested = output_paths.place("conv-a", "candidate-new.tar.gz", keep_name=True, when=WHEN)
    Path(nested.path).write_text("nested")
    assert output_paths.find("old.tar.gz") == str(outputs / "old.tar.gz")
    assert output_paths.find("candidate-new.tar.gz") == nested.path
    assert output_paths.find("missing.txt") is None
    # Only a bare name is looked up.
    assert output_paths.find("2026-11/conv-a/candidate-new.tar.gz") is None
    assert output_paths.find("../old.tar.gz") is None
    assert output_paths.find("") is None


def test_find_does_not_follow_links_or_look_in_the_temporary_folder(outputs, tmp_path):
    secret = tmp_path / "secret.txt"; secret.write_text("s")
    chat = outputs / "2026-11" / "conv-a"; chat.mkdir(parents=True)
    (chat / "link.txt").symlink_to(secret)
    hidden = outputs / ".hc-temporary" / "x"; hidden.mkdir(parents=True)
    (hidden / "leased.tmp").write_text("t")
    assert output_paths.find("link.txt") is None
    assert output_paths.find("leased.tmp") is None


def test_the_size_shown_in_settings_counts_files_in_chat_folders(outputs):
    outputs.mkdir()
    (outputs / "flat.bin").write_bytes(b"12345")
    spot = output_paths.place("conv-a", "nested.bin", when=WHEN)
    Path(spot.path).write_bytes(b"1234567")
    (outputs / ".hc-temporary").mkdir()
    (outputs / ".hc-temporary" / "t.tmp").write_bytes(b"123")
    assert output_paths.usage() == (2, 12)


def test_the_by_name_fallbacks_see_nested_files(outputs, tmp_path, monkeypatch):
    import artifact_files
    import durable_storage
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path / "uploads"))
    nested = output_paths.place("conv-a", "abcd1234-notes.txt", keep_name=True, when=WHEN)
    Path(nested.path).write_text("n")
    assert artifact_files.resolve_download_path("abcd1234-notes.txt") == (nested.path, "abcd1234-notes.txt")
    assert artifact_files.resolve_download_path("nope.txt") == (None, "nope.txt")
    assert str(durable_storage.source_for("", "/api/downloads/abcd1234-notes.txt", "")) == nested.path


def test_the_storage_page_counts_files_in_their_folders_not_only_loose_ones(outputs):
    """After the files were sorted into folders the page read "Output files: 0" (2026-11-16)."""
    outputs.mkdir()
    assert output_paths.summary()["files"] == 0
    for chat, name, when, size in [("conv-a", "site.tar.gz", WHEN, 10), ("conv-a", "notes.md", WHEN, 5), ("conv-b", "x.csv", WHEN, 7),
                                   ("conv-c", "old.tar.gz", datetime(2026, 9, 3, tzinfo=timezone.utc), 20),
                                   (output_paths.IMAGE_STUDIO, "comfy_1_0.png", WHEN, 30), (None, "orphan.bin", WHEN, 2)]:
        Path(output_paths.place(chat, name, keep_name=True, when=when).path).write_bytes(b"x" * size)
    (outputs / "still-loose.bin").write_bytes(b"x" * 3)
    (outputs / ".hc-temporary").mkdir()
    (outputs / ".hc-temporary" / "t.tmp").write_bytes(b"x" * 100)
    told = output_paths.summary()
    assert (told["files"], told["bytes"]) == (7, 77)
    assert told["chat_folders"] == 3
    assert told["loose"] == {"files": 1, "bytes": 3}
    assert told["kinds"] == {"chats": {"files": 4, "bytes": 42}, "image_studio": {"files": 1, "bytes": 30},
                             "library": {"files": 0, "bytes": 0}, "unsorted": {"files": 1, "bytes": 2}}
    assert told["months"] == [{"month": "2026-11", "files": 5, "bytes": 54, "chats": 2},
                              {"month": "2026-09", "files": 1, "bytes": 20, "chats": 1}]


def test_the_storage_summary_of_a_missing_folder_is_all_zeros(outputs):
    told = output_paths.summary()
    assert told["files"] == 0 and told["months"] == [] and told["chat_folders"] == 0
