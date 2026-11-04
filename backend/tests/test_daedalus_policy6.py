"""Policy-6 regressions with saved model responses; no inference or downloads."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

import coder_jobs as controller
import database as db
from db import coder_jobs as jobs
from coder_profiles import discover, python_test_kind
from coder_repository import Repository
from context_policy import DEFAULTS
import config


def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("DAEDALUS_ENGINE_URL","http://worker:8587")
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'jobs.db'))
    monkeypatch.setattr(config, 'CONTEXT_SETTINGS', {**DEFAULTS, 'daedalus_v3_enabled': True})
    monkeypatch.setattr(controller, 'spawn', lambda *args: None)
    async def initialize():
        await db.init_db()
        await db.create_conversation('conversation', model='coder:local')
    asyncio.run(initialize())

RESPONSES = json.loads((Path(__file__).parent / 'fixtures/daedalus_policy6.json').read_text())


def repository(tmp_path, files):
    root = tmp_path / 'project'; root.mkdir(parents=True)
    for name, contents in files.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(contents)
    repo = Repository(root, tmp_path / 'repo', DEFAULTS['daedalus_exclude_dirs'])
    repo.refresh()
    return repo


def test_static_preview_survives_test_tooling_manifest(tmp_path):
    repo = repository(tmp_path, {'index.html':'<html>Hello</html>', 'package.json':json.dumps({'scripts':{'test':'node --test'}})})
    result = discover(repo)
    assert result['web_packages'] == ['.']
    assert any(c.get('server_command','').startswith('python3 -m http.server') for c in result['checks'])
    assert any(c.get('command')=='npm run test' for c in result['checks'])


def test_python_scripts_unittest_pytest_and_demonstrations_are_distinct(tmp_path):
    repo = repository(tmp_path, {'test_script.py':'assert 1 == 1\n', 'test_unit.py':'import unittest\nclass T(unittest.TestCase):\n def test_x(self): pass\n',
        'test_pytest.py':'def test_answer():\n assert True\n', 'demo.py':'assert False\n', 'test_demo.py':'print("passed")\n'})
    commands = [c['command'] for c in discover(repo)['checks'] if c.get('is_test')]
    assert '.venv/bin/python test_script.py' in commands
    assert '.venv/bin/python -m unittest -v test_unit.py' in commands
    assert '.venv/bin/python -m pytest -q test_pytest.py' in commands
    assert not any('demo' in c for c in commands)


@pytest.mark.parametrize('files,command', [
    ({'app.csproj':'<Project/>'}, 'dotnet build'), ({'CMakeLists.txt':'project(App)'}, 'cmake --build'),
    ({'Makefile':'test:\n\ttrue\n'}, 'make test'), ({'composer.json':'{"scripts":{"test":"phpunit"}}'}, 'composer run test'),
    ({'Gemfile':'source "https://rubygems.org"', 'spec/example_spec.rb':''}, 'bundle exec rspec'),
    ({'Cargo.toml':''}, 'cargo test'), ({'go.mod':'module example'}, 'go test'), ({'pom.xml':'<project/>'}, 'mvn test'),
    ({'build.gradle':''}, 'gradle test')])
def test_cross_language_profiles(tmp_path, files, command):
    result = discover(repository(tmp_path, files))
    assert any(command in c.get('command','') for c in result['checks'])
    assert result['profiles'][0]['toolchains']


def test_command_precedence_and_validated_generic_fallback(tmp_path):
    repo = repository(tmp_path, {'package.json':'{"scripts":{"test":"node --test"}}', 'suite.sh':'exit 0',
        '.daedalus.json':'{"packages":{".":{"test":"bash suite.sh"}}}'})
    assert next(c for c in discover(repo)['checks'] if c.get('is_test'))['command']=='bash suite.sh'
    explicit = {'packages':{'.':{'test':'bash suite.sh explicit'}}}
    assert next(c for c in discover(repo, explicit)['checks'] if c.get('is_test'))['command']=='bash suite.sh explicit'
    proposals = {'packages':{'.':{'lint':'bash suite.sh'}}}
    assert any(c['command']=='bash suite.sh' and c.get('phase')=='lint' for c in discover(repo, proposals=proposals)['checks'])
    with pytest.raises(ValueError, match='existing repository file'):
        discover(repo, proposals={'packages':{'.':{'lint':'madeup --check'}}})


def test_candidate_publication_rejects_stale_head(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    async def scenario():
        job=await controller.create('conversation','task',model='local')
        await jobs.save(job['id'],state='candidate_packaging',revision_id='c'*40,
                        verification_summary={'revision_id':'c'*40,'accepted':False})
        connection=await db.get_db()
        try:
            # Another job delivered to this project's working version meanwhile: this candidate is stale.
            # (An Accept moving only the accepted version would not make a new build's candidate stale.)
            await connection.execute('UPDATE coding_projects SET working_job_id=?,working_revision_id=? WHERE id=?',('cw3-other','d'*40,job['project_id']))
            await connection.commit()
        finally: await connection.close()
        with pytest.raises(ValueError,match='head changed'):
            await jobs.finish_candidate(job['id'],'c'*40,artifact_id='bad',filename='bad.tar.gz',url='/bad',kind='archive')
        assert (await jobs.get(job['id']))['candidate_artifact'] is None
    asyncio.run(scenario())


def test_candidate_archive_has_review_report_and_preserves_source(tmp_path):
    import tarfile
    worker=WorkerStore(tmp_path/'worker')
    project=worker.root/'jobs'/'job'/'workspace';project.mkdir(parents=True)
    (project/'app.py').write_text('print("hello")\n')
    repo=Repository(project,worker.root/'jobs'/'job'/'repository',DEFAULTS['daedalus_exclude_dirs'])
    revision=repo.snapshot()['revision']
    payload={'engine':'openhands159','kind':'package','policy_version':6,'settings':{**DEFAULTS,'daedalus_min_free_mb':1},
             'revision_id':revision,'seconds_remaining':30,'calls_remaining':0,'candidate':True,
             'verification_summary':{'revision_id':revision,'accepted':False,'unverified':['o1']},
             'run_instructions':[{'cwd':'.','commands':{},'launch':'python3 app.py'}]}
    worker.create('package','job','package',payload);worker.update('package',started=time.time());execute(worker,'package')
    result=worker.get('package');assert result['status']=='succeeded',result
    with tarfile.open(result['result']['path']) as archive:
        assert archive.extractfile('app.py').read()==b'print("hello")\n'
        report=json.load(archive.extractfile(f'DAEDALUS_REVIEW_{revision}.json'))
    assert report['verification_summary']['unverified']==['o1']
    assert repo.snapshot(parent=revision)['revision']==revision


@pytest.mark.skipif(os.environ.get('DAEDALUS_BROWSER_TEST')!='1',reason='Opt-in Chromium integration')
@pytest.mark.parametrize('width',[390,1440])
def test_candidate_card_pins_continue_and_shows_evidence(tmp_path,width):
    import socket
    import subprocess
    import urllib.request
    from playwright.sync_api import sync_playwright,expect
    frontend=Path(__file__).resolve().parents[2]/'frontend'
    with socket.socket() as sock: sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    with (tmp_path/'vite.log').open('w') as log:
        server=subprocess.Popen(['node','node_modules/vite/bin/vite.js','--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=frontend,stdout=log,stderr=subprocess.STDOUT)
    job={'id':'fixture-job','workflow_version':3,'policy_version':6,'state':'ready_for_review','event_sequence':1,
         'revision_id':'a'*40,'candidate_artifact':{'id':'candidate','filename':'candidate.tar.gz','metadata':{'revision_id':'a'*40,'runnable':False,
         'run_instructions':[{'cwd':'.','commands':{'test':['python3 test_app.py']}}]}},
         'brief':{'outcomes':[{'id':'o1','text':'Requested behavior','evidence_types':['behavior']}]},
         'verification_summary':{'outcomes':[{'id':'o1','text':'Requested behavior','status':'unverified','reason':'Runtime not available','missing_evidence':['behavior']}]}}
    submitted=[]; errors=[]
    def route_api(route):
        nonlocal job
        if route.request.url.endswith('/api/settings'):
            return route.fulfill(json={**DEFAULTS,'resolved_contexts':{}})
        if '/api/coder/projects?' in route.request.url:
            return route.fulfill(json={'projects':[],'active_project_id':''})
        if route.request.url.split('?')[0].endswith('/resume'):
            submitted.append(route.request.post_data_json);job={**job,'state':'checking','event_sequence':2}
        if '/events?' in route.request.url:
            return route.fulfill(content_type='text/event-stream',body=': heartbeat\n\n')
        return route.fulfill(json=job)
    try:
        url=f'http://127.0.0.1:{port}/tests/daedalus-harness.html'
        for _ in range(100):
            try: urllib.request.urlopen(url,timeout=1).close();break
            except Exception: time.sleep(.05)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':width,'height':900});page.route('**/api/**',route_api)
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('console',lambda m:errors.append(m.text) if m.type=='error' else None)
            page.goto(url)
            card=page.get_by_label('Daedalus coding job')
            expect(card.locator('.dj-state')).to_have_text('Build incomplete')
            card.get_by_role('button',name='View details',exact=True).click()
            panel=page.get_by_role('dialog')
            expect(panel.get_by_text('python3 test_app.py',exact=True)).to_be_visible()
            expect(panel.get_by_text('No behavior evidence was recorded for this outcome.',exact=True)).to_be_visible()
            assert not card.get_by_text('Download accepted project',exact=True).count()
            folder=Path(os.environ.get('DAEDALUS_QA_DIR',tmp_path));folder.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(folder/f'candidate-{width}.png'),full_page=True)
            panel.get_by_role('button',name='Close job details').click()
            card.get_by_role('button',name='Continue',exact=True).click()
            expect(card.locator('.dj-state')).to_have_text('Checking')
            assert submitted==[{'candidate_revision':'a'*40}]
            job={**job,'state':'completed','artifact_status':'delivered','artifact':{'id':'accepted','filename':'accepted.tar.gz'},
                 'verification_summary':{'outcomes':[{'id':'o1','text':'Requested behavior','status':'passed','reason':'Checked'}]},
                 'acceptance':{'coverage':[{'id':'o1','text':'Requested behavior','status':'passed','reason':'Checked'}]}}
            page.reload()
            expect(card.locator('.dj-state')).to_have_text('Complete')
            card.get_by_role('button',name='View details',exact=True).click()
            expect(panel.get_by_role('link',name='Download accepted project',exact=True)).to_be_visible()
            assert not panel.get_by_role('link',name='Download candidate',exact=True).count()
            expect(panel.locator('.dp-outcomes').get_by_text('Verified',exact=True)).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors,errors
            browser.close()
    finally:
        server.terminate();server.wait(timeout=5)


def test_explicit_unittest_script_and_native_junit_report_counts(tmp_path):
    from coder_native_checks import native_count, report_count
    assert native_count('python3 test.py','Ran 2 tests in 0.01s\nOK')==2
    assert native_count('bundle exec rake test','3 runs, 8 assertions, 0 failures')==3
    report=tmp_path/'build/test-results/test/TEST-App.xml';report.parent.mkdir(parents=True)
    report.write_text('<testsuite><testcase name="executed"/><testcase name="skipped"><skipped/></testcase></testsuite>')
    assert report_count(tmp_path)==1


def test_cli_launch_is_execution_not_browser_preview(tmp_path):
    repo=repository(tmp_path,{'package.json':'{"scripts":{"start":"node cli.js"}}','cli.js':'console.log("ready")'})
    result=discover(repo)
    assert not result['web_packages']
    assert next(c for c in result['checks'] if c.get('phase')=='launch')['command']=='npm run start'
    explicit={'packages':{'.':{'launch':'node cli.js'}}}
    result=discover(repo,explicit)
    assert next(c for c in result['checks'] if c.get('phase')=='launch')['command']=='node cli.js'


def test_continued_unchanged_candidate_preserves_prior_archive(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    monkeypatch.setattr(config,'SANDBOX_OUTPUTS_DIR',str(tmp_path/'artifacts'))
    body=[b'original candidate report']
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=body[0]))) as http:
            controller.configure(http,None)
            job=await controller.create('conversation','task',model='coder:local',key='immutable-candidates')
            revision='a'*40
            job=await jobs.save(job['id'],state='candidate_packaging',revision_id=revision,worker_operation='package-1',
                verification_summary={'revision_id':revision,'accepted':False},resume_state='checking')
            first=await controller._deliver(job,{'revision_id':revision,'sha256':hashlib.sha256(body[0]).hexdigest()},candidate=True)
            # Model a completed continuation at the same revision with a new report.
            job=await jobs.save(job['id'],state='queued',event='resume',candidate_revision=revision,
                candidate_history=[first],candidate_artifact=None)
            job=await jobs.save(job['id'],state='candidate_packaging',worker_operation='package-2')
            body[0]=b'new execution evidence, unchanged source'
            result={'revision_id':revision,'sha256':hashlib.sha256(body[0]).hexdigest()}
            second=await controller._deliver(job,result,candidate=True)
            assert first['id']!=second['id'] and first['storage_path']!=second['storage_path']
            assert Path(first['storage_path']).read_bytes()==b'original candidate report'
            assert Path(second['storage_path']).read_bytes()==body[0]
            assert (await controller._deliver(job,result,candidate=True))['id']==second['id']
            assert (await db.get_coding_project(job['project_id']))['accepted_revision_id']==''
    asyncio.run(scenario())

from coder_worker_runtime import WorkerStore, execute
