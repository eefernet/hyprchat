import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


def module(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).parents[1]/'hephaestus'/(name+'.py'))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


def test_retirement_requires_every_gate_and_exact_old_identity(tmp_path):
    m=module('retire_codebox')
    config='hostname: agentcodebox\nnet0: name=eth0,hwaddr=BC:24:11:8C:87:5C\nrootfs: local-lvm:vm-115-disk-0,size=51G\n'
    archive=tmp_path/'old.conf';archive.write_text(config)
    evidence=tmp_path/'evidence';evidence.write_text('verified')
    report=tmp_path/'gate';report.write_text('passed')
    record={'version':1,'old_guest':{'vmid':115,'config_archive':str(archive),'config_sha256':m.digest(archive)},
            'replacement':{'vmid':118,'tag':'hephaestus-test','release':'release'},'cutover_at':1,'reboot_verified_at':2,
            'workspaces_marked_unavailable':True,'data_disk_free_bytes':11*1024**3,
            'evidence_archive':{'path':str(evidence),'sha256':m.digest(evidence)},
            'post_cutover_release':'release','gates':{name:{'passed':True,'release':'release','path':str(report),'sha256':m.digest(report)} for name in m.REQUIRED_GATES}}
    new={'name':'hephaestus','tags':'hephaestus-test'}
    m.validate(record,config,'status: stopped',new)
    for change in ({'gates':{}},{'data_disk_free_bytes':0},{'post_cutover_release':'other'},{'cutover_at':None}):
        with pytest.raises(ValueError):m.validate({**record,**change},config,'status: stopped',new)
    with pytest.raises(ValueError):m.validate(record,config,'status: running',new)
    with pytest.raises(ValueError):m.validate(record,config+'changed','status: stopped',new)
    with pytest.raises(ValueError):m.validate(record,config,'status: stopped',{'name':'other','tags':'hephaestus-test'})


def test_workspace_retirement_preserves_accepted_revisions_artifacts_and_budgets(tmp_path):
    m=module('retire_workspaces');path=tmp_path/'database';registry=tmp_path/'registry.json'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE coder_workflows(id TEXT,state TEXT,job_json TEXT,workflow_version INTEGER); CREATE TABLE coding_projects(id TEXT,user_id TEXT,openhands_project_id TEXT,accepted_revision_id TEXT); CREATE TABLE runs(status TEXT); CREATE TABLE artifacts(id TEXT,sha256 TEXT);')
        job={'calls_used':17,'seconds_used':225,'repair_round':2,'artifact':{'id':'artifact','revision_id':'accepted'}}
        db.execute('INSERT INTO coder_workflows VALUES(?,?,?,3)',('job','ready_for_review',json.dumps(job)))
        db.execute("INSERT INTO coding_projects VALUES('project','owner','worker-project','accepted')")
        db.execute("INSERT INTO artifacts VALUES('artifact','unchanged')")
    m.mark(path,registry);backup=registry.with_suffix('.before.sqlite3').read_bytes()
    m.mark(path,registry)
    assert registry.with_suffix('.before.sqlite3').read_bytes()==backup
    with sqlite3.connect(path) as db:
        result=json.loads(db.execute('SELECT job_json FROM coder_workflows').fetchone()[0])
        assert result['worker_workspace_available'] is False
        assert all(result[k]==v for k,v in job.items())
        assert db.execute('SELECT accepted_revision_id FROM coding_projects').fetchone()[0]=='accepted'
        assert db.execute('SELECT sha256 FROM artifacts').fetchone()[0]=='unchanged'
