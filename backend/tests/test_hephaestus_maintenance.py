"""Storage lifecycle tests use real files/SQLite and simulated Docker references."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

import pytest
import hephaestus_inventory as inv
import hephaestus_maintenance as maintenance
import hephaestus_shared_cache as shared


@pytest.fixture
def root(tmp_path,monkeypatch):
    monkeypatch.setenv('HEPHAESTUS_ROOT',str(tmp_path))
    (tmp_path/'state').mkdir()
    inv.atomic(inv.control(tmp_path)/'retention.json',{'verified':True,'releases':[],'images':[],'paths':[]})
    monkeypatch.setattr(maintenance,'docker_inventory',lambda refs:{'images':[],'summary':[],'containers':[]})
    monkeypatch.setattr(maintenance,'run',lambda *a,**k:'0 x')
    monkeypatch.setattr(inv,'refresh_scan',lambda root:None)
    return tmp_path


def store(root,name='modern',status='succeeded'):
    path=root/'state'/name;path.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path/'operations.sqlite3') as db:
        db.execute('CREATE TABLE operations(id TEXT,job_id TEXT,status TEXT,ended REAL,payload TEXT)')
        db.execute('INSERT INTO operations VALUES(?,?,?,?,?)',('op','job',status,time.time()-90000,json.dumps({'project_id':'project'})))
    inv.register(root,path,'store',role='production' if name=='modern' else 'test')
    return path


def file(path,body='evidence'):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body);return path


def execute(root,plan):
    identity=uuid.uuid4().hex
    record=inv.control(root)/'operations'/(identity+'.json')
    inv.atomic(record,{'id':identity,'preview_id':plan['id'],'mode':plan['mode'],'status':'queued','free_before':0,'results':[]})
    maintenance.execute(root,identity)
    return inv.read(record)


def test_cross_category_shared_extents_count_once():
    totals=inv.union_counts([(0,100,'docker'),(25,75,'dependencies'),(0,100,'docker'),(200,220,'projects')])
    assert totals['docker']==50 and totals['system']==50 and totals['projects']==20
    assert sum(totals.values())==120


def test_safe_cleanup_preserves_revisions_unknown_and_ledger(root):
    path=store(root)
    source=file(path/'jobs/job/workspace/app.py');cache=file(path/'project-environments/cache/package')
    unknown=file(root/'unknown/data')
    plan=maintenance.preview(root)
    result=execute(root,plan)
    assert result['status']=='completed'
    assert source.exists() and unknown.exists() and (path/'operations.sqlite3').exists()
    assert not cache.exists()
    assert any(r['kind']=='unknown' and not r['eligible'] for r in plan['resources'])


def test_reset_deletes_project_data_but_preserves_job_ledger_and_history(root):
    path=store(root);source=file(path/'jobs/job/workspace/app.py');logs=file(path/'operation-logs/op.json')
    result=execute(root,maintenance.preview(root,'reset'))
    assert result['status']=='completed' and not source.exists()
    assert logs.exists() and (path/'operations.sqlite3').exists()
    assert inv.read(inv.control(root)/'retired-jobs.json')['jobs']==['job']


def test_live_operation_blocks_cleanup(root):
    path=store(root,status='running');cache=file(path/'project-environments/cache/package')
    result=execute(root,maintenance.preview(root))
    assert result['status']=='blocked' and cache.exists()


def test_document_job_blocks_reset(root):
    path=store(root)
    file(root/'projects/owner/root/hyprchat-documents/jobs/doc/status.json','{"status":"running"}')
    source=file(path/'jobs/job/app.py')
    result=execute(root,maintenance.preview(root,'reset'))
    assert result['status']=='blocked' and source.exists()


def test_pinned_stopped_dependencies_survive_safe_cleanup(root):
    path=store(root);cache=file(path/'project-environments/cache/package')
    file(path/'previews/app/session.json',json.dumps({'status':'stopped','lifecycle':'pinned','cache_roots':[str(cache.parent)]}))
    execute(root,maintenance.preview(root))
    assert cache.exists()


def test_shared_cache_reader_lease_protects_entry(root):
    entry=file(root/'caches/shared/packages/key/item')
    plan=maintenance.preview(root)
    with inv.lock(root,'shared-cache-readers',exclusive=False):result=execute(root,plan)
    assert entry.exists() and any(r['status']=='skipped' for r in result['results'])


def test_cache_lease_protects_entry(root):
    from coder_engine_cache import lease
    path=store(root);cache=file(path/'project-environments/cache/package')
    with lease(cache.parent):result=execute(root,maintenance.preview(root))
    assert cache.exists() and result['results'][0]['status']=='skipped'


def test_stale_preview_inode_is_not_deleted(root):
    entry=file(root/'caches/shared/packages/key/item');plan=maintenance.preview(root)
    entry.parent.rename(entry.parent.with_name('different'))
    file(entry,'replacement')
    result=execute(root,plan)
    assert entry.exists() and any('changed after preview' in r.get('reason','') for r in result['results'])


def test_reset_requires_confirmation_and_operation_idempotence(root,monkeypatch):
    path=store(root);file(path/'jobs/job/app.py');plan=maintenance.preview(root,'reset');identity=uuid.uuid4().hex
    with pytest.raises(ValueError,match='Type DELETE'):maintenance.start(root,plan['id'],identity)
    class Thread:
        def __init__(self,**kwargs):pass
        def start(self):pass
    monkeypatch.setattr(maintenance.threading,'Thread',Thread)
    first=maintenance.start(root,plan['id'],identity,maintenance.CONFIRMATION)
    assert maintenance.start(root,plan['id'],identity,maintenance.CONFIRMATION)==first
    other=maintenance.preview(root,'reset')
    with pytest.raises(ValueError,match='already belongs'):maintenance.start(root,other['id'],identity,maintenance.CONFIRMATION)


def test_archive_ack_required_and_evidence_change_invalidates(root):
    path=store(root,'nested-test/worker');source=file(path/'jobs/job/app.py')
    assert not any(r['eligible'] for r in maintenance.preview(root)['resources'] if r['kind']=='test_environment')
    records=maintenance.prepare_archives(root);assert len(records)==1
    row=records[0]
    with pytest.raises(ValueError,match='checksum'):maintenance.acknowledge_archive(root,row['id'],'wrong','/opt/hyprchat/data/evaluation-archives/hephaestus/x.tar.gz')
    maintenance.acknowledge_archive(root,row['id'],row['sha256'],'/opt/hyprchat/data/evaluation-archives/hephaestus/x.tar.gz')
    assert any(r['eligible'] for r in maintenance.preview(root)['resources'] if r['kind']=='test_environment')
    source.write_text('changed')
    assert not any(r['eligible'] for r in maintenance.preview(root)['resources'] if r['kind']=='test_environment')
    assert source.exists()


def test_retired_tests_archive_before_removal(root):
    path=store(root,'old-test');file(path/'jobs/job/app.py');file(path/'project-environments/cache/large')
    row=maintenance.prepare_archives(root)[0]
    maintenance.acknowledge_archive(root,row['id'],row['sha256'],'/opt/hyprchat/data/evaluation-archives/hephaestus/x.tar.gz')
    result=execute(root,maintenance.preview(root))
    assert result['status']=='completed' and not path.exists()
    assert not (inv.control(root)/'evidence-outbox'/(row['id']+'.tar.gz')).exists()


def test_automatic_retention_keeps_recent_cache(root):
    path=store(root);cache=file(path/'project-environments/cache/item')
    plan=maintenance.preview(root,automatic=True)
    assert not any(r['eligible'] for r in plan['resources'] if r['kind']=='cache')
    os.utime(cache.parent,(time.time()-8*86400,)*2)
    assert any(r['eligible'] for r in maintenance.preview(root,automatic=True)['resources'] if r['kind']=='cache')


@pytest.mark.parametrize('url',['http://registry.npmjs.org/a','https://evil.invalid/a','https://user:pass@pypi.org/a','https://pypi.org:8080/a'])
def test_private_or_nonregistry_downloads_are_rejected(url):
    with pytest.raises(ValueError,match='public registry'):shared.fetch(url)


def test_content_verified_shared_download_reused(root,monkeypatch):
    calls=[];body=b'public wheel';sha=hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(shared,'fetch',lambda url:(calls.append(url) or body))
    import coder_environment_publish
    monkeypatch.setattr(coder_environment_publish,'require_capacity',lambda *a,**k:None)
    a,reused=shared.download(root,'https://files.pythonhosted.org/a.whl',sha)
    b,reused2=shared.download(root,'https://files.pythonhosted.org/a.whl',sha)
    assert a==b and not reused and reused2 and len(calls)==1
    with pytest.raises(ValueError,match='integrity'):shared.download(root,'https://files.pythonhosted.org/b.whl','0'*64)
    assert a.read_bytes()==body


def test_managed_resource_escape_rejected(root,tmp_path):
    with pytest.raises(ValueError):inv.resource_path(root,'../elsewhere')
    (root/'escape').symlink_to('/tmp')
    with pytest.raises(ValueError):inv.resource_path(root,'escape/child')


def test_internal_sdk_ledgers_are_not_independent_test_environments(root):
    parent=store(root)
    child=store(root,'modern/agent-runtime/request/bookkeeping',status='running')
    inv.register(root,child,'store',role='test')
    inv.register(root,child,'test_environment',role='test')
    registry=inv.discover(root)
    assert not any('bookkeeping' in r['path'] for r in registry['resources'].values())
    assert not inv.active_stores(root,registry)
    assert (child/'operations.sqlite3').exists()


def test_expired_temporary_preview_does_not_block_cleanup(root):
    path=store(root)
    file(path/'previews/old/session.json',json.dumps({'pid':99999999,'process_started':0,'status':'closed','desired_state':'running','lifecycle':'temporary'}))
    assert not maintenance.preview(root)['blockers']


def test_pinned_waiting_for_restart_remains_protected(root):
    path=store(root)
    file(path/'previews/pinned/session.json',json.dumps({'status':'closed','desired_state':'running','lifecycle':'pinned'}))
    assert maintenance.preview(root)['blockers']


def test_stopped_pinned_browser_binding_is_retained(root):
    path=store(root);browser=file(root/'caches/shared/browsers/browser/chrome')
    cache=path/'project-environments/cache'
    file(cache/'bindings-v2/entry.json',json.dumps({'browser_path':str(browser.parent)}))
    file(path/'previews/app/session.json',json.dumps({'status':'stopped','lifecycle':'pinned','cache_roots':[str(cache)]}))
    execute(root,maintenance.preview(root))
    assert browser.exists()


def test_unregistered_custom_store_is_protected(root):
    path=store(root,'custom-production')
    registry=inv.read(inv.control(root)/'registry.json')
    for row in registry['resources'].values():row.pop('role',None)
    inv.atomic(inv.control(root)/'registry.json',registry)
    file(path/'jobs/project/app.py')
    file(path/'project-environments/custom/package')
    plan=maintenance.preview(root)
    assert not any(r['kind']=='test_environment' for r in plan['resources'])
    assert not any(r['eligible'] for r in plan['resources'])


def test_recovery_does_not_overwrite_running_cleanup(root):
    identity=uuid.uuid4().hex
    record=inv.control(root)/'operations'/(identity+'.json')
    original={'id':identity,'status':'running','results':[{'id':'finished','status':'removed'}]}
    inv.atomic(record,original)
    with inv.lock(root,'cleanup'):maintenance.execute(root,identity)
    assert inv.read(record)==original


def test_new_unreferenced_docker_images_are_not_added_after_preview(root,monkeypatch):
    plan=maintenance.preview(root)
    monkeypatch.setattr(maintenance,'docker_inventory',lambda refs:{'images':[{'id':'new-image','protected':False,'tags':['new:latest']}],'containers':[]})
    commands=[]
    monkeypatch.setattr(maintenance,'run',lambda args,**kwargs:commands.append(args) or '')
    execute(root,plan)
    assert not any(args[:3]==['docker','image','rm'] for args in commands)


def test_authenticated_status_remains_readable_during_cleanup(root,monkeypatch):
    from fastapi.testclient import TestClient
    import hephaestus_api as api
    from hephaestus_scope import headers
    monkeypatch.setattr(api,'ROOT',root)
    monkeypatch.setattr(api,'languages',lambda:[])
    monkeypatch.setenv('HEPHAESTUS_TOKEN','maintenance-test')
    monkeypatch.setattr(maintenance,'status',lambda root:{'operations':[{'status':'running'}]})
    signed=headers('owner',secret='maintenance-test',project='storage',maintenance='storage-admin')
    with TestClient(api.app) as client,inv.lock(root,'admission'):
        assert client.get('/status',headers=signed).status_code==200
        assert client.get('/storage',headers=signed).json()['operations'][0]['status']=='running'
        assert client.post('/execute',json={},headers=signed).status_code==503
        assert client.get('/storage',headers=headers('owner',secret='maintenance-test',project='project')).status_code==403
