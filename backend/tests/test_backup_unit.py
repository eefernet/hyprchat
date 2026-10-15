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
    from db.schema import DB_SCHEMA
    conn.executescript(DB_SCHEMA)
    conn.execute("INSERT INTO model_provider_credentials(user_id,provider,api_key) VALUES ('default','openai','sk-SECRET')")
    conn.execute("""INSERT INTO mcp_servers(id,name,headers_json,env_json) VALUES ('m1','mcp','{"Authorization":"Bearer xyz"}','{"TOKEN":"v"}')""")
    conn.execute("""INSERT INTO openapi_connectors(id,name,headers_json,auth_json) VALUES ('o1','api','{"X-Key":"k"}','{"token":"t"}')""")
    conn.execute("INSERT INTO conversations(id,title) VALUES ('c1','from-backup')")
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
    for attribute, relative in [('UPLOAD_DIR','uploads'),('KB_DIR','knowledge_bases'),('TOOLS_DIR','tools'),
                                ('ARTIFACT_DIR','artifacts'),('SANDBOX_OUTPUTS_DIR','sandbox/outputs'),('SETTINGS_PATH','settings.json')]:
        monkeypatch.setattr(config, attribute, str(data / relative))
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
        assert manifest["version"] == 2
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
        info = tarfile.TarInfo(name="uploads/random.txt")
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


def test_manifest_covers_durable_files_previews_and_settings_secrets(env, tmp_path):
    for name in ['artifacts/hash/original.docx', 'artifacts/hash/preview.pdf', 'sandbox/outputs/legacy.zip']:
        path = env.data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'preserved content')
    (env.data / 'settings.json').write_text(json.dumps({'ollama_scan_ssh_password': 'PRIVATE-PASSWORD',
        'nested': {'token': 'PRIVATE-TOKEN', 'url': 'https://user:pass@example.com/path?api_key=secret'}}))
    archive = backup.create_backup_archive()
    with tarfile.open(archive) as tar:
        settings = json.load(tar.extractfile('settings.json'))
        assert settings['ollama_scan_ssh_password'] == ''
        assert settings['nested']['token'] == ''
        assert settings['nested']['url'] == 'https://example.com/path?api_key='
        manifest = json.load(tar.extractfile('manifest.json'))
        assert manifest['files']['artifacts/hash/preview.pdf']['size'] == 17
    for path in (env.data / 'artifacts').rglob('*'):
        if path.is_file():
            path.write_bytes(b'changed')
    backup.stage_restore(archive)
    backup.apply_pending_restore()
    assert (env.data / 'artifacts/hash/preview.pdf').read_bytes() == b'preserved content'


def test_corrupt_content_does_not_replace_existing_data(env, tmp_path):
    archive = backup.create_backup_archive()
    corrupt = tmp_path / 'corrupt.tar.gz'
    import io
    with tarfile.open(archive) as source, tarfile.open(corrupt, 'w:gz') as target:
        for member in source:
            data = source.extractfile(member).read()
            if member.name == 'uploads/doc.txt':
                data = b'changed'
                member.size = len(data)
            target.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match='hash'):
        backup.stage_restore(str(corrupt))
    assert not Path(backup.RESTORE_MARKER).exists()
    assert (env.data / 'uploads/doc.txt').read_text() == 'hello'


@pytest.mark.parametrize('crash', [False, True])
def test_activation_failure_and_restart_restore_db_and_files(env, monkeypatch, crash):
    archive = backup.create_backup_archive()
    (env.data / 'uploads/doc.txt').write_text('new live file')
    with sqlite3.connect(env.db_path) as conn:
        conn.execute("UPDATE conversations SET title='new live title'")
    backup.stage_restore(archive)
    original = backup.shutil.copy2
    def fail(source, target, *args, **kwargs):
        if Path(source).name == 'activation.db':
            raise KeyboardInterrupt('simulated process death') if crash else OSError('simulated full disk')
        return original(source, target, *args, **kwargs)
    monkeypatch.setattr(backup.shutil, 'copy2', fail)
    with pytest.raises(KeyboardInterrupt if crash else OSError):
        backup.apply_pending_restore()
    monkeypatch.setattr(backup.shutil, 'copy2', original)
    if crash:
        assert backup._journal_path().exists()
        assert backup.apply_pending_restore() is False
    assert (env.data / 'uploads/doc.txt').read_text() == 'new live file'
    with sqlite3.connect(env.db_path) as conn:
        assert conn.execute('SELECT title FROM conversations').fetchone()[0] == 'new live title'
    assert not backup._journal_path().exists()


def test_legacy_manifest_reports_missing_content(env, tmp_path):
    archive = backup.create_backup_archive()
    legacy = tmp_path / 'legacy.tar.gz'
    import io
    with tarfile.open(archive) as source, tarfile.open(legacy, 'w:gz') as target:
        for member in source:
            data = source.extractfile(member).read()
            if member.name == 'manifest.json':
                data = json.dumps({'version': 1, 'hyprchat_db': 'hyprchat.db'}).encode()
                member.size = len(data)
            target.addfile(member, io.BytesIO(data))
    assert backup.stage_restore(str(legacy))['warnings']


def test_secrets_are_removed_from_database_free_pages(env):
    with sqlite3.connect(env.db_path) as conn:
        conn.executescript('''INSERT INTO user_sessions(token_hash,user_id) VALUES('SESSION_SECRET_123','default');
            INSERT INTO user_prefs(user_id,key,value_json) VALUES('default','ntfy','{"token":"NTFY_SECRET_456"}');''')
    archive = backup.create_backup_archive()
    with tarfile.open(archive) as tar:
        blob = tar.extractfile('hyprchat.db').read()
        assert b'SESSION_SECRET_123' not in blob
        assert b'NTFY_SECRET_456' not in blob
        assert b'sk-SECRET' not in blob


def test_external_referenced_file_is_portable_and_live_path_unchanged(env, tmp_path, monkeypatch):
    external=tmp_path/'external';external.mkdir()
    source=external/'document.docx';source.write_bytes(b'office original')
    monkeypatch.setattr(config,'UPLOAD_DIR',str(external))
    with sqlite3.connect(env.db_path) as conn:
        conn.execute('INSERT INTO artifacts(id,storage_path,url,filename,sha256) VALUES(?,?,?,?,?)',('art-1',str(source),'/api/downloads/document.docx','document.docx',backup._hash(source)))
    archive=backup.create_backup_archive()
    with sqlite3.connect(env.db_path) as conn:
        assert conn.execute('SELECT storage_path FROM artifacts').fetchone()[0]==str(source)
    staged=_extract_db(archive,tmp_path/'portable')
    with sqlite3.connect(staged) as conn:
        path=Path(conn.execute('SELECT storage_path FROM artifacts').fetchone()[0])
    relative=path.relative_to(env.data)
    with tarfile.open(archive) as tar:
        assert tar.extractfile(str(relative)).read()==b'office original'


def test_user_file_named_manifest_is_not_confused_with_archive_manifest(env):
    (env.data / 'uploads/manifest.json').write_text('{"user":"document"}')
    archive=backup.create_backup_archive()
    manifest=backup.stage_restore(archive)
    assert 'uploads/manifest.json' in manifest['files']
    assert backup.apply_pending_restore()
    assert (env.data / 'uploads/manifest.json').read_text()=='{"user":"document"}'


def test_restore_rebases_to_configured_external_artifact_root(env,tmp_path,monkeypatch):
    original_root=tmp_path/'original-artifacts';original_root.mkdir()
    source=original_root/'retained.docx';source.write_bytes(b'original document')
    monkeypatch.setattr(config,'ARTIFACT_DIR',str(original_root))
    with sqlite3.connect(env.db_path) as conn:
        conn.execute('INSERT INTO artifacts(id,storage_path,url,filename,sha256) VALUES(?,?,?,?,?)',
            ('retained',str(source),'/api/artifacts/retained/download','retained.docx',backup._hash(source)))
    archive=backup.create_backup_archive()
    new_root=tmp_path/'restored-artifacts'
    monkeypatch.setattr(config,'ARTIFACT_DIR',str(new_root))
    backup.stage_restore(archive)
    assert backup.apply_pending_restore()
    with sqlite3.connect(env.db_path) as conn:
        restored=Path(conn.execute("SELECT storage_path FROM artifacts WHERE id='retained'").fetchone()[0])
    assert restored.is_relative_to(new_root)
    assert restored.read_bytes()==b'original document'
    assert source.read_bytes()==b'original document'


def test_wal_snapshot_contains_committed_rows_and_no_transient_sidecars(env):
    conn=sqlite3.connect(env.db_path)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute("UPDATE conversations SET title='committed in WAL'")
        conn.commit()
        archive=backup.create_backup_archive()
        names=_tar_names(archive)
        assert not any(n.endswith(('-wal','-shm','-journal')) for n in names)
        backup.stage_restore(archive)
    finally:
        conn.close()
    assert backup.apply_pending_restore()
    with sqlite3.connect(env.db_path) as conn:
        assert conn.execute('SELECT title FROM conversations').fetchone()[0]=='committed in WAL'
