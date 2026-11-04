import importlib.util
import sqlite3
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location('hephaestus_install',Path(__file__).parents[1]/'hephaestus/install.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def test_inference_between_container_actions_prevents_activation(tmp_path,monkeypatch):
    database=tmp_path/'state/modern/operations.sqlite3';database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE operations(status TEXT)');db.execute("INSERT INTO operations VALUES('running')")
    with pytest.raises(SystemExit,match='inference'):
        m.require_drained(tmp_path)

def test_queued_document_prevents_activation(tmp_path):
    path=tmp_path/'projects/project/root/hyprchat-documents/jobs/doc-test/status.json'
    path.parent.mkdir(parents=True);path.write_text('{"status":"queued"}')
    with pytest.raises(SystemExit,match='document'):m.require_drained(tmp_path)
