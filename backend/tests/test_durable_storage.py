"""Storage survival, verified migration and restart safety, without a live service."""
import sqlite3
from pathlib import Path
import pytest
import config
import durable_storage as storage


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, relative in [('DATA_DIR', ''), ('DATABASE_PATH', 'hyprchat.db'),
                           ('ARTIFACT_DIR', 'artifacts'), ('UPLOAD_DIR', 'uploads'),
                           ('KB_DIR', 'knowledge_bases'), ('SANDBOX_OUTPUTS_DIR', 'outputs')]:
        monkeypatch.setattr(config, name, str(tmp_path / relative))
    Path(config.SANDBOX_OUTPUTS_DIR).mkdir()
    with sqlite3.connect(config.DATABASE_PATH) as conn:
        conn.execute('CREATE TABLE artifacts(id TEXT,storage_path TEXT,url TEXT,filename TEXT,sha256 TEXT)')
    return tmp_path


def test_verified_copy_and_idempotent_reference_migration(env):
    original = Path(config.SANDBOX_OUTPUTS_DIR) / 'report.docx'
    original.write_bytes(b'original document')
    with sqlite3.connect(config.DATABASE_PATH) as conn:
        conn.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)', ('art-1', str(original), '/api/downloads/report.docx', 'report.docx', storage.digest(original)))
    assert storage.migrate_references() == {'migrated': 1, 'unresolved': 0}
    with sqlite3.connect(config.DATABASE_PATH) as conn:
        aid, path, url = conn.execute('SELECT id,storage_path,url FROM artifacts').fetchone()
    assert aid == 'art-1' and url == '/api/downloads/report.docx'
    assert Path(path).read_bytes() == original.read_bytes()
    assert Path(path).is_relative_to(config.ARTIFACT_DIR)
    assert storage.migrate_references() == {'migrated': 0, 'unresolved': 0}
    storage.cleanup_outputs(float('inf'))
    assert original.exists() and Path(path).exists()


def test_hash_mismatch_retains_legacy_reference_and_file(env):
    path = Path(config.SANDBOX_OUTPUTS_DIR) / 'bad.zip'
    path.write_bytes(b'changed')
    with sqlite3.connect(config.DATABASE_PATH) as conn:
        conn.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)', ('art-1', str(path), '', 'bad.zip', '0' * 64))
    assert storage.migrate_references() == {'migrated': 0, 'unresolved': 1}
    assert path.exists()
    with sqlite3.connect(config.DATABASE_PATH) as conn:
        assert conn.execute('SELECT storage_path FROM artifacts').fetchone()[0] == str(path)


def test_cleanup_preserves_unreferenced_active_and_legacy_outputs(env):
    for name in ['legacy.png', 'active-job.tmp']:
        (Path(config.SANDBOX_OUTPUTS_DIR) / name).write_bytes(b'content')
    assert storage.cleanup_outputs(0)['preserved'] == 2


def test_symlink_cannot_escape_allowed_source_roots(env):
    secret=env/'private.txt';secret.write_text('private')
    link=Path(config.SANDBOX_OUTPUTS_DIR)/'escape.txt';link.symlink_to(secret)
    assert storage.source_for(str(link),'','escape.txt') is None
    with pytest.raises(ValueError):storage.publish(str(link))


def test_cleanup_requires_release_and_honors_all_references(env):
    with storage.temporary_output('owner-a', 'job') as active:
        active.write_bytes(b'active')
        assert storage.cleanup_outputs()['deleted'] == 0
    with storage.temporary_output('owner-b', 'job') as referenced:
        referenced.write_bytes(b'saved')
    with sqlite3.connect(config.DATABASE_PATH) as conn:
        conn.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)', ('saved', str(referenced), '', referenced.name, ''))
    with storage.temporary_output('owner-a', 'job') as changed:
        changed.write_bytes(b'old')
    changed.write_bytes(b'new')
    legacy = Path(config.SANDBOX_OUTPUTS_DIR) / 'legacy.tmp'
    legacy.write_bytes(b'legacy')
    preview = storage.cleanup_outputs(dry_run=True)
    assert preview['reclaimable_files'] == 1
    assert preview['reclaimable_bytes'] == 6
    assert active.exists()
    assert storage.cleanup_outputs(0)['deleted'] == 0
    result = storage.cleanup_outputs()
    assert result['deleted'] == 1 and not active.exists()
    assert referenced.exists() and changed.exists() and legacy.exists()
    assert result['reasons']['referenced by saved work'] == 1


def test_moved_staging_file_releases_lease_without_deleting_deliverable(env):
    with storage.temporary_output('owner', 'job') as path:
        path.write_bytes(b'finished')
        destination = Path(config.SANDBOX_OUTPUTS_DIR) / 'final.png'
        path.rename(destination)
    assert storage.cleanup_outputs()['deleted'] == 0
    assert destination.read_bytes() == b'finished'
    with sqlite3.connect(config.DATABASE_PATH) as conn:
        assert conn.execute('SELECT COUNT(*) FROM temporary_outputs').fetchone()[0] == 0
