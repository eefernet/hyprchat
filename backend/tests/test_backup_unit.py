"""Offline unit tests for backend/backup.py.

Covers archive include/exclude rules, secret scrubbing in the staged DB copy,
manifest shape, stage_restore validation (traversal, links, missing manifest),
apply_pending_restore's DB swap + connector-secret preservation, and the
pre-restore copy prune. Everything runs against tmp_path — no live service.
"""
import json
import sqlite3
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import backup  # noqa: E402
import config  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    db_path = data / "hyprchat.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE model_provider_credentials (user_id TEXT, provider TEXT, api_key TEXT)")
    conn.execute("INSERT INTO model_provider_credentials VALUES ('default','openai','sk-SECRET')")
    conn.execute("CREATE TABLE mcp_servers (id TEXT, headers_json TEXT, env_json TEXT)")
    conn.execute("""INSERT INTO mcp_servers VALUES ('m1','{"Authorization":"Bearer xyz"}','{"TOKEN":"v"}')""")
    conn.execute("CREATE TABLE openapi_connectors (id TEXT, headers_json TEXT, auth_json TEXT)")
    conn.execute("""INSERT INTO openapi_connectors VALUES ('o1','{"X-Key":"k"}','{"token":"t"}')""")
    conn.execute("CREATE TABLE conversations (id TEXT, title TEXT)")
    conn.execute("INSERT INTO conversations VALUES ('c1','from-backup')")
    conn.commit()
    conn.close()
    (data / "uploads").mkdir()
    (data / "uploads" / "doc.txt").write_text("hello")
    (data / "uploads" / "private.key").write_text("PRIVATE")
    (data / "knowledge_bases" / "kb1").mkdir(parents=True)
    (data / "knowledge_bases" / "kb1" / "note.md").write_text("kb")
    (data / "settings.json").write_text('{"a":1}')
    (data / "connector_secrets.json").write_text('{"s":"ecret"}')
    monkeypatch.setattr(config, "DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_path), raising=False)
    monkeypatch.setattr(backup, "BACKUP_DIR", str(data / "backups"))
    monkeypatch.setattr(backup, "RESTORE_PENDING_DIR", str(data / "restore-pending"))
    monkeypatch.setattr(backup, "RESTORE_MARKER", str(data / ".restore-pending"))
    return SimpleNamespace(data=data, db_path=db_path)


def _tar_names(path):
    with tarfile.open(path, "r:gz") as tar:
        return set(tar.getnames())


def _extract_db(archive, dest_dir):
    with tarfile.open(archive, "r:gz") as tar:
        tar.extract("hyprchat.db", dest_dir)
    return Path(dest_dir) / "hyprchat.db"


class TestCreateBackupArchive:
    def test_includes_excludes_scrub_manifest(self, env, tmp_path):
        path = backup.create_backup_archive()
        names = _tar_names(path)
        assert {"manifest.json", "hyprchat.db", "settings.json"} <= names
        assert "uploads/doc.txt" in names
        assert "knowledge_bases/kb1/note.md" in names
        # secrets never travel: excluded basename + key/pem suffixes
        assert "connector_secrets.json" not in names
        assert not any(n.endswith("private.key") for n in names)

        # staged DB copy is scrubbed; live DB is untouched
        staged = _extract_db(path, tmp_path / "x")
        conn = sqlite3.connect(staged)
        assert conn.execute("SELECT api_key FROM model_provider_credentials").fetchone()[0] == ""
        assert conn.execute("SELECT headers_json FROM mcp_servers").fetchone()[0] == "{}"
        assert conn.execute("SELECT auth_json FROM openapi_connectors").fetchone()[0] == "{}"
        # non-secret data intact in the copy
        assert conn.execute("SELECT title FROM conversations").fetchone()[0] == "from-backup"
        conn.close()
        live = sqlite3.connect(env.db_path)
        assert live.execute("SELECT api_key FROM model_provider_credentials").fetchone()[0] == "sk-SECRET"
        live.close()

        with tarfile.open(path, "r:gz") as tar:
            manifest = json.load(tar.extractfile("manifest.json"))
        assert manifest["version"] == 1
        assert manifest["hyprchat_db"] == "hyprchat.db"
        assert manifest["secrets_scrubbed"]  # non-empty list
        assert "uploads" in manifest["included_dirs"]


def _handcrafted_archive(path, members):
    """members: list of (tarinfo, bytes|None)."""
    with tarfile.open(path, "w:gz") as tar:
        for info, payload in members:
            if payload is None:
                tar.addfile(info)
            else:
                import io
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))


class TestStageRestoreValidation:
    def test_rejects_traversal_member(self, env, tmp_path):
        evil = tmp_path / "evil.tar.gz"
        info = tarfile.TarInfo(name="../evil.txt")
        _handcrafted_archive(evil, [(info, b"boom")])
        with pytest.raises(ValueError, match="[Uu]nsafe"):
            backup.stage_restore(str(evil))

    def test_rejects_symlink_member(self, env, tmp_path):
        evil = tmp_path / "link.tar.gz"
        info = tarfile.TarInfo(name="innocent")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        _handcrafted_archive(evil, [(info, None)])
        with pytest.raises(ValueError, match="[Ll]inks"):
            backup.stage_restore(str(evil))

    def test_rejects_missing_manifest(self, env, tmp_path):
        bare = tmp_path / "bare.tar.gz"
        info = tarfile.TarInfo(name="random.txt")
        _handcrafted_archive(bare, [(info, b"data")])
        with pytest.raises(ValueError, match="manifest"):
            backup.stage_restore(str(bare))
        # failed staging cleans up after itself
        assert not Path(backup.RESTORE_PENDING_DIR).exists()


class TestApplyPendingRestore:
    def test_swap_keeps_secrets_prunes_copies(self, env):
        archive = backup.create_backup_archive()

        # mutate the live system after the backup was taken
        conn = sqlite3.connect(env.db_path)
        conn.execute("UPDATE conversations SET title='live-version'")
        conn.commit()
        conn.close()
        (env.data / "connector_secrets.json").write_text('{"s":"live-secret"}')

        manifest = backup.stage_restore(archive)
        assert manifest["hyprchat_db"] == "hyprchat.db"
        assert Path(backup.RESTORE_MARKER).is_file()

        assert backup.apply_pending_restore() is True
        conn = sqlite3.connect(env.db_path)
        assert conn.execute("SELECT title FROM conversations").fetchone()[0] == "from-backup"
        conn.close()
        # connector secrets never restored over the live file
        assert (env.data / "connector_secrets.json").read_text() == '{"s":"live-secret"}'
        # marker + pending dir consumed; a pre-restore safety copy exists
        assert not Path(backup.RESTORE_MARKER).exists()
        assert not Path(backup.RESTORE_PENDING_DIR).exists()
        copies = list(env.data.glob("hyprchat.db.pre-restore-*"))
        assert len(copies) == 1

    def test_no_marker_is_noop(self, env):
        assert backup.apply_pending_restore() is False


class TestPrunePreRestoreCopies:
    def test_keeps_newest_two(self, env):
        stamps = ["20250101-000000", "20250201-000000", "20250301-000000", "20250401-000000"]
        for ts in stamps:
            (env.data / f"hyprchat.db.pre-restore-{ts}").write_text("old db")
        backup._prune_pre_restore_copies(keep=2)
        left = sorted(p.name for p in env.data.glob("hyprchat.db.pre-restore-*"))
        assert left == [
            "hyprchat.db.pre-restore-20250301-000000",
            "hyprchat.db.pre-restore-20250401-000000",
        ]
