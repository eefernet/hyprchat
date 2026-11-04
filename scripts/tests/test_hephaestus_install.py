import importlib.util
import sqlite3
import json
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


def test_interrupted_activation_restores_files_and_preserves_candidate(tmp_path,monkeypatch):
    previous=tmp_path/'previous';previous.write_text('old configuration')
    target=tmp_path/'runtime.json';target.write_text('interrupted new configuration')
    added=tmp_path/'new.conf';added.write_text('candidate only')
    candidate=tmp_path/'candidate';candidate.mkdir()
    journal=tmp_path/'activation.json'
    state={'state':'activating','backup':str(tmp_path/'backup'),
        'services':['hephaestus-api'],'active_services':['hephaestus-api'],
        'files':[{'target':str(target),'backup':str(previous),'mode':0o600},
                 {'target':str(added),'backup':None,'mode':None}]}
    journal.write_text(json.dumps(state));commands=[]
    monkeypatch.setattr(m,'run',lambda *args:commands.append(args))
    assert m.recover_activation(journal)
    assert target.read_text()=='old configuration' and not added.exists()
    assert candidate.exists() and not journal.exists()
    assert commands==[('systemctl','stop','hephaestus-api'),('systemctl','daemon-reload'),
                      ('systemctl','start','hephaestus-api')]
    assert not m.recover_activation(journal)


def test_first_install_publishes_only_after_validation_and_can_recover(tmp_path,monkeypatch):
    target=tmp_path/'runtime.json';journal=tmp_path/'activation.json';commands=[]
    monkeypatch.setattr(m,'run',lambda *args:commands.append(args))
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:type('Result',(),{'returncode':3})())
    def reject():raise RuntimeError('health failed')
    with pytest.raises(RuntimeError,match='health failed'):
        m.activate_files([(target,'new runtime',0o600)],journal,['hephaestus-api'],reject)
    assert not target.exists() and not journal.exists()
    assert not any(c[:2]==('systemctl','start') for c in commands)
    def validate():assert target.read_text()=='new runtime' and journal.exists()
    m.activate_files([(target,'new runtime',0o600)],journal,['hephaestus-api'],validate)
    assert target.read_text()=='new runtime' and not journal.exists()
    assert target.stat().st_mode & 0o777==0o600
