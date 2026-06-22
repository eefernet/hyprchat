import sys
from pathlib import Path


_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import storage_diagnostics  # noqa: E402


def test_readonly_storage_error_classifier_matches_chroma_sqlite_message():
    msg = "Database error: error returned from database: (code: 1032) attempt to write a readonly database"
    assert storage_diagnostics.is_readonly_storage_error(msg)
    assert storage_diagnostics.is_readonly_storage_error(PermissionError("permission denied"))
    assert not storage_diagnostics.is_readonly_storage_error("temporary database is locked")


def test_runtime_storage_status_ok_for_writable_temp_paths(tmp_path):
    chroma_dir = tmp_path / "chroma_db"
    chroma_dir.mkdir()
    db_path = tmp_path / "hyprchat.db"

    status = storage_diagnostics.runtime_storage_status(str(db_path), str(chroma_dir))

    assert status["status"] == "ok"
    assert status["database"]["status"] == "ok"
    assert status["rag_chroma"]["status"] == "ok"
