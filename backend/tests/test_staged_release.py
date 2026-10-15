import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import pytest

spec=importlib.util.spec_from_file_location('staged_release',Path(__file__).resolve().parents[2]/'scripts/staged_release.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)


def test_controller_between_operations_prevents_activation(tmp_path):
    path = tmp_path/'live.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE coder_workflows (id TEXT, state TEXT, workflow_version INTEGER)')
        conn.executemany('INSERT INTO coder_workflows VALUES (?,?,?)',
                         [('active','candidate_packaging',3),('parked','ready_for_review',3),('legacy-stale','fixing',2)])
    assert release.active_jobs(path) == 1


def test_install_failure_does_not_activate_or_change_live_files(tmp_path,monkeypatch):
    live=tmp_path/'live'; source=tmp_path/'source'
    for root in [live,source]:
        (root/'backend').mkdir(parents=True)
        (root/'backend/main.py').write_text('pass')
        (root/'frontend/dist').mkdir(parents=True)
        (root/'frontend/dist/index.html').write_text('UI')
    (source/'backend/requirements.lock.txt').write_text('invalid==0')
    before=release.tree_hashes(live);commands=[]
    monkeypatch.setattr(release,'live_command',lambda _: ['/usr/bin/python3','-m','uvicorn','main:app'])
    def run(args,**kw):
        commands.append(args)
        if args[:2]==['systemctl','show']:return str(live/'backend')
        if 'pip' in args:raise subprocess.CalledProcessError(1,args)
        return ''
    monkeypatch.setattr(release,'run',run)
    with pytest.raises(subprocess.CalledProcessError):release.prepare(live,source,'hyprchat')
    assert release.tree_hashes(live)==before
    assert not any('restart' in args for args in commands)
    assert not (live/'.release-journal.json').exists()


def test_acceptance_failure_restores_previous_service_configuration(tmp_path,monkeypatch):
    root=tmp_path/'app'; candidate=root/'.releases/candidate'; candidate.mkdir(parents=True)
    (root/'data').mkdir();sqlite3.connect(root/'data/hyprchat.db').close()
    vectors=root/'data/chroma_db';vectors.mkdir();(vectors/'version').write_text('previous vector store')
    if os.geteuid()==0:
        for path in (root/'data/hyprchat.db',vectors,vectors/'version'):os.chown(path,65534,65534)
    database_owner=(root/'data/hyprchat.db').stat().st_uid
    vector_owner=(vectors/'version').stat().st_uid
    monkeypatch.setattr(release,'SYSTEMD_DIR',tmp_path/'systemd')
    dropin=tmp_path/'systemd/hyprchat.service.d/zz-hyprchat-release.conf'
    dropin.parent.mkdir(parents=True);dropin.write_text('previous config')
    state={'state':'prepared','release':str(candidate),'root':str(root),'active_root':str(root),
           'service':'hyprchat','command':['/old/python','-m','uvicorn','main:app'],
           'baseline':{},'candidate':{}}
    release.atomic_json(candidate/'release.json',state)
    commands=[]
    def run(args,**kw):
        commands.append(args)
        if args==['acceptance-fixture']:
            (vectors/'version').write_text('candidate vector format')
            with sqlite3.connect(root/'data/hyprchat.db') as db:
                db.execute('CREATE TABLE candidate_only(value TEXT)')
            raise RuntimeError('acceptance failure')
        return 'active' if 'is-active' in args else ''
    class Response(io.BytesIO):status=200
    monkeypatch.setattr(release,'run',run)
    monkeypatch.setattr(release.urllib.request,'urlopen',lambda *a,**k:Response(b'{"status":"ok"}'))
    with pytest.raises(RuntimeError,match='acceptance failure'):
        release.activate(candidate,'http://fixture',['acceptance-fixture'])
    assert dropin.read_text()=='previous config'
    assert commands.count(['systemctl','restart','hyprchat'])==2
    assert not (root/'.release-journal.json').exists()
    assert candidate.exists()
    with sqlite3.connect(root/'data/hyprchat.db') as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='candidate_only'").fetchall()==[]
    assert list(candidate.glob('rollback-*/hyprchat.db'))
    assert (vectors/'version').read_text()=='previous vector store'
    assert (root/'data/hyprchat.db').stat().st_uid==database_owner
    assert (vectors/'version').stat().st_uid==vector_owner


def test_document_activation_failure_restores_previous_pointer(tmp_path,monkeypatch):
    import sys
    scripts=Path(__file__).resolve().parents[2]/'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    import staged_worker_release as worker
    root=tmp_path/'documents';root.mkdir()
    old=root/'old';old.mkdir()
    candidate=root/'candidate';candidate.mkdir()
    (root/'current').symlink_to(old)
    worker.atomic_json(candidate/'release.json',{'state':'prepared','root':str(root),'release':str(candidate),'kind':'documents','files':{}})
    monkeypatch.setattr(worker,'active_documents',lambda:None)
    def reject(*a,**k):raise RuntimeError('fixture acceptance rejected')
    monkeypatch.setattr(worker,'run',reject)
    with pytest.raises(RuntimeError,match='acceptance'):worker.activate(candidate,['fixture'])
    assert (root/'current').resolve()==old
    assert candidate.exists()
    assert not (root/'.worker-release-journal.json').exists()
