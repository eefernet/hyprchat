"""Independent checks of delivered archives; never shown to the coding model."""
import argparse
import csv
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from coder_sandbox_call import browser_options



def labelled(page, name, control='text'):
    """`get_by_label(name, exact=True)` narrowed to the fillable/selectable control when the app labels two elements the
    same way (fix6 medium p1, 2026-09-27: a project <select> and the project-name <input> were both labelled "Project name"
    and Playwright's strict mode refused to fill). The label association is still required; only the ambiguity is resolved."""
    found = page.get_by_label(name, exact=True)
    if found.count() > 1:
        narrowed = found.and_(page.locator('select' if control == 'select' else 'input, textarea'))
        if narrowed.count():
            return narrowed.first
    return found

def title_pattern(text):
    """The title as rendered anywhere in an element's text (a row may add status/priority around it)."""
    return re.compile(r'(?<!\w)'+re.escape(text)+r'(?!\w)')


def requirements_file(source):
    """The delivered pip requirements: shallowest first, `requirements.txt` before variants, never node_modules."""
    found=[p for p in source.rglob('requirements*.txt') if 'node_modules' not in p.parts]
    return sorted(found,key=lambda p:(len(p.parts),p.name!='requirements.txt',str(p)))[0] if found else None



def native_project_browser_tests(source, candidates, base, url, serverlog):
    from coder_profiles import python_test_kind
    from coder_native_checks import native_count, PYTHON_TRACE, provenance
    evidence=base/'browser-test-evidence'; evidence.mkdir()
    instrumentation=base/'browser-instrumentation'; instrumentation.mkdir()
    (instrumentation/'sitecustomize.py').write_text(PYTHON_TRACE)
    env=dict(os.environ,DAEDALUS_PROJECT_ROOT=str(source),DAEDALUS_AUDIT_DIR=str(source),DAEDALUS_PROVENANCE=str(evidence),NODE_V8_COVERAGE=str(evidence),PYTHONPATH=str(instrumentation)+os.pathsep+str(source),DAEDALUS_APP_URL=url,BASE_URL=url)
    offset=(base/'golden-server.log').stat().st_size
    commands=[]
    if (source/'package.json').exists():
        manifest=json.loads((source/'package.json').read_text())
        if any(p.suffix in {'.js','.mjs','.ts','.tsx'} for p in candidates) and manifest.get('scripts',{}).get('test'):
            install=subprocess.run(['npm','install','--no-audit','--no-fund'],cwd=source,env=env,capture_output=True,text=True,timeout=240)
            assert install.returncode==0,(install.stdout+install.stderr)[-2000:]
            commands.append(['npm','test'])
    for path in candidates:
        if path.suffix=='.py':
            kind=python_test_kind(path)
            commands.append([sys.executable,'-m','pytest','-q',str(path)] if kind=='pytest' else [sys.executable,str(path)])
    assert commands,'No supported documented native browser runner found'
    observed=False
    for index,cmd in enumerate(commands):
        result=subprocess.run(cmd,cwd=source,env=env,capture_output=True,text=True,timeout=120)
        output=result.stdout+result.stderr
        (base/f'browser-runner-{index}.log').write_text(output)
        assert result.returncode==0,output[-3000:]
        count=native_count(' '.join(cmd),output)
        observed |= bool(count and count>0)
    _,assertions=provenance(evidence,source)
    assert observed or assertions>0,'Native browser command collected no observed assertions'
    with (base/'golden-server.log').open() as log:
        log.seek(offset); traffic=log.read()
    assert re.search(r'(?:GET|POST) /',traffic),'Browser tests did not request the served application'

def run(args):
    from playwright.sync_api import sync_playwright, expect
    base=Path(args.folder);archive=base/args.archive;source=base/'golden-source'
    source.mkdir()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                if Path(name).is_absolute() or '..' in Path(name).parts:raise ValueError('Unsafe archive path')
            bundle.extractall(source)
    else:
        with tarfile.open(archive) as bundle:bundle.extractall(source,filter='data')
    while len(list(source.iterdir()))==1 and next(source.iterdir()).is_dir():
        source=next(source.iterdir())
    checks=[];report={'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'source':str(source),'checks':checks}
    def check(name,fn):
        try:
            detail=fn();checks.append({'name':name,'passed':True,'detail':detail})
        except Exception as error:
            checks.append({'name':name,'passed':False,'error':f'{type(error).__name__}: {error}','trace':traceback.format_exc()[-3000:]})
        (base/'golden.json').write_text(json.dumps(report,indent=2))
    def command(cmd,cwd=source,timeout=60,env=None):
        result=subprocess.run(cmd,cwd=cwd,capture_output=True,text=True,timeout=timeout,env=env)
        assert result.returncode==0,(result.stdout+result.stderr)[-4000:]
        return result.stdout.strip()
    def docs():
        paths=list(source.glob('README*'));assert paths,'README missing'
        assert any(p.stat().st_size>40 for p in paths),'README has no meaningful instructions'
    check('README',docs)
    if args.scenario=='upload-python':
        data=base/'expenses.csv';data.write_text('category,amount\nfood,0.10\ntravel,0.20\n')
        def cli(*extra):return command([sys.executable,'ledger.py',str(data),*extra])
        def totals():assert cli()=='0.30'
        def version():command([sys.executable,'-c','from ledger import VERSION; assert VERSION == "keep-me"'])
        def executable():command([sys.executable,'tests/check_ledger.py'])
        check('all rows and Decimal total',totals);check('VERSION compatibility',version);check('explicit executable test script',executable)
        if args.step:
            def filters():
                assert cli('--category','food')=='0.10'
                assert json.loads(cli('--category','food','--json'))=={'total':'0.10','count':1}
                assert json.loads(cli('--category','missing','--json'))=={'total':'0.00','count':0}
            check('filter and JSON API',filters)
    else:
        if args.scenario=='upload-web':
            def css():
                assert (source/'styles.css').read_text()=='body { font-family: sans-serif; max-width: 48rem; margin: 2rem auto; padding: 1rem; }\n'
            check('explicit stylesheet preservation',css)
            check('configured Node tests',lambda:command(['npm','test']))
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url=f'http://127.0.0.1:{port}';env=dict(os.environ);process=None;serverlog=(base/'golden-server.log').open('w')
        startcmd=[sys.executable,'-m','http.server',str(port),'--bind','127.0.0.1']
        if args.scenario=='medium':
            def install():
                venv=base/'golden-env'
                command([sys.executable,'-m','venv',str(venv)],timeout=90)
                requirements=requirements_file(source)
                if requirements:command([str(venv/'bin/python'),'-m','pip','install','-r',str(requirements)],timeout=240)
                elif (source/'pyproject.toml').exists():command([str(venv/'bin/python'),'-m','pip','install',str(source)],timeout=240)
                frontends=[]
                for manifest_path in source.rglob('package.json'):
                    if 'node_modules' in manifest_path.parts:continue
                    manifest=json.loads(manifest_path.read_text())
                    if 'react' in {**manifest.get('dependencies',{}),**manifest.get('devDependencies',{})}:frontends.append(manifest_path.parent)
                assert frontends,'No React frontend package discovered'
                frontend=sorted(frontends,key=lambda p:(len(p.parts),str(p)))[0]
                command(['npm','ci' if (frontend/'package-lock.json').exists() else 'install','--no-audit','--no-fund'],cwd=frontend,timeout=240)
                command(['npm','run','build'],cwd=frontend,timeout=120)
            check('documented application build',install)
            env['APP_DB_PATH']=str(base/'golden.sqlite3')
            startcmd=[str(base/'golden-env/bin/python'),'-m','uvicorn','app:app','--host','127.0.0.1','--port',str(port)]
        if args.scenario=='upload-medium':
            check('Node dependency setup',lambda:command(['npm','install','--no-audit','--no-fund'],timeout=240))
            env['APP_DB_PATH']=str(base/'golden.sqlite3');env['PORT']=str(port)
            startcmd=['node','server.js']
            def protected():
                from evals.policy7_fixtures import NODE_FILES
                for path in ('VERSION','public/styles.css'):
                    assert (source/path).read_text()==NODE_FILES[path],path
            check('protected files',protected)
        def start():
            nonlocal process
            process=subprocess.Popen(startcmd,cwd=source,env=env,stdout=serverlog,stderr=subprocess.STDOUT)
            for _ in range(100):
                if process.poll() is not None:raise AssertionError((base/'golden-server.log').read_text()[-3000:])
                try:
                    with urllib.request.urlopen(url,timeout=1) as response:
                        if response.status==200:return
                except Exception:time.sleep(.2)
            raise AssertionError('Application did not launch')
        def stop():
            if process and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=8)
                except subprocess.TimeoutExpired:process.kill();process.wait()
        def api(method,path,data=None,expected=200):
            request=urllib.request.Request(url+path,data=json.dumps(data).encode() if data is not None else None,
                headers={'Content-Type':'application/json'},method=method)
            try:
                with urllib.request.urlopen(request,timeout=10) as response:status=response.status;raw=response.read()
            except urllib.error.HTTPError as error:status=error.code;raw=error.read()
            allowed=expected if isinstance(expected,tuple) else (expected,)
            assert status in allowed,(method,path,status,raw[:400])
            return json.loads(raw) if raw else None
        check('application launch',start)
        try:
            if args.scenario in {'web','upload-web','medium'}:
                def project_browser_tests():
                    html_tests=[p for p in source.rglob('*.html') if 'test' in p.name.lower() and 'node_modules' not in p.parts]
                    if not html_tests:
                        candidates=[p for p in source.rglob('*') if p.is_file() and 'node_modules' not in p.parts and
                            p.suffix in {'.py','.js','.mjs','.ts','.tsx'} and ('test' in p.name.lower() or 'spec' in p.name.lower()) and
                            any(s in p.read_text(errors='replace').lower() for s in ['playwright','selenium','puppeteer'])]
                        assert candidates,'Requested executable browser regression tests are missing'
                        native_project_browser_tests(source, candidates, base, url, serverlog)
                        return
                    with sync_playwright() as pw:
                        browser=pw.chromium.launch(headless=True,args=['--no-sandbox'], **browser_options())
                        try:
                            for test in html_tests:
                                page=browser.new_page();errors=[];requests=[]
                                page.on('pageerror',lambda e:errors.append(str(e)))
                                page.on('request',lambda r:requests.append(r.url))
                                page.goto(url+'/'+test.relative_to(source).as_posix(),wait_until='networkidle')
                                failures=page.locator('.fail,.failed,[data-status="failed"]').all_text_contents()
                                body=page.locator('body').inner_text()
                                if re.search(r'(?:Test \d+ failed|Failed to load application|FAIL:)',body,re.I):failures.append(body[-1500:])
                                assert not errors and not failures,{'page_errors':errors,'failed_assertions':failures}
                                assert any(u.endswith('/index.html') or u.endswith('/app.js') for u in requests),'Test page did not load the actual app'
                                assert page.locator('.pass,.passed,[data-status="passed"]').count()>0 or 'All tests passed!' in body,'No completed browser assertions observed'
                        finally:browser.close()
                check('delivered executable browser regression tests',project_browser_tests)
            if args.scenario in {'medium','upload-medium'} and process and process.poll() is None:
                ids={}
                def crud():
                    # The delivered tests ran first against this same database: judge deltas, scoped to our project.
                    before=api('GET','/api/summary');ids['before']=before
                    project=api('POST','/api/projects',{'name':'Golden project'},(200,201));ids['project']=project['id']
                    assert any(p['id']==project['id'] for p in api('GET','/api/projects'))
                    scope='project_id='+str(project['id'])
                    tasks=[]
                    for title,status,priority in [('Budget alpha','todo','high'),('Ship beta','doing','low')]:
                        tasks.append(api('POST','/api/tasks',{'project_id':project['id'],'title':title,'status':status,'priority':priority},(200,201)))
                    ids['tasks']=[t['id'] for t in tasks]
                    assert len(api('GET','/api/tasks?'+scope))==2
                    assert [t['title'] for t in api('GET','/api/tasks?q=alpha&'+scope)]==['Budget alpha']
                    assert [t['title'] for t in api('GET','/api/tasks?status=doing&'+scope)]==['Ship beta']
                    summary=api('GET','/api/summary')
                    assert all(summary[k]-before[k]==v for k,v in {'total':2,'todo':1,'doing':1,'done':0}.items()),(before,summary)
                    api('PATCH','/api/tasks/'+str(tasks[0]['id']),{'title':'Budget updated','status':'done','priority':'medium'},(200,204))
                    assert api('GET','/api/summary')['done']==before['done']+1
                    api('POST','/api/projects',{'name':''},(400,422))
                    api('POST','/api/tasks',{'project_id':project['id'],'title':'','priority':'high','status':'todo'},(400,422))
                    api('POST','/api/tasks',{'project_id':project['id'],'title':'Invalid','priority':'urgent','status':'todo'},(400,422))
                    api('PATCH','/api/tasks/999999',{'status':'done'},404)
                check('API CRUD filtering summaries and validation',crud)
                def restart():
                    stop();start()
                    tasks=api('GET','/api/tasks?project_id='+str(ids['project']))
                    assert {t['id'] for t in tasks}==set(ids['tasks'])
                    assert any(t['title']=='Budget updated' for t in tasks)
                    for ident in ids['tasks']:api('DELETE','/api/tasks/'+str(ident),expected=(200,204))
                    assert api('GET','/api/summary')['total']==ids['before']['total']
                check('restart persistence and deletion',restart)
            if args.scenario=='upload-medium' and process and process.poll() is None:
                check('delivered executable API tests',lambda:command([sys.executable,'tests/test_api.py'],env={**env,'DAEDALUS_APP_URL':url}))
                if args.step:
                    def priority_filter():
                        project=api('POST','/api/projects',{'name':'Priority regression'},(200,201))
                        a=api('POST','/api/tasks',{'project_id':project['id'],'title':'Priority alpha','status':'doing','priority':'high'},(200,201))
                        b=api('POST','/api/tasks',{'project_id':project['id'],'title':'Priority beta','status':'doing','priority':'low'},(200,201))
                        assert [t['id'] for t in api('GET','/api/tasks?priority=high&status=doing&q=alpha&project_id='+str(project['id']))]==[a['id']]
                        assert api('GET','/api/tasks?priority=low&q=alpha&project_id='+str(project['id']))==[]
                        for task in (a,b):api('DELETE','/api/tasks/'+str(task['id']),expected=(200,204))
                    check('priority filter API',priority_filter)
            for width in (1440,390):
                def browser_case(width=width):
                    with sync_playwright() as pw:
                        browser=pw.chromium.launch(headless=True,args=['--no-sandbox'], **browser_options())
                        context=browser.new_context(viewport={'width':width,'height':900},accept_downloads=True)
                        context.tracing.start(screenshots=True,snapshots=True,sources=True)
                        page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.on('dialog',lambda d:d.accept())   # a confirm() before delete is the app's choice, not a failure
                        def shown(text):
                            return page.locator('#rows li').filter(has_text=text) if args.scenario=='upload-medium' else page.get_by_text(title_pattern(text))
                        def visible(text):expect(shown(text).first).to_be_visible()
                        def absent(text):
                            expect(shown(text).first).not_to_be_visible()
                            assert not any(e.is_visible() for e in shown(text).all()),text+' is still shown'
                        try:
                            page.goto(url,wait_until='networkidle',timeout=20000)
                            if args.scenario=='web':
                                categories=['food,local','<b>travel</b>']
                                dropdown=page.locator('#category').evaluate('(e)=>e.tagName')=='SELECT'
                                if dropdown:
                                    categories=page.locator('#category option').evaluate_all('(nodes)=>nodes.filter(n=>n.value).slice(0,2).map(n=>n.value)')
                                    report.setdefault('limitations',[]).append('Category dropdown: arbitrary text entry could not be exercised at '+str(width))
                                for amount,category,date in [('0.10',categories[0],'2026-09-16'),('0.20',categories[1],'2026-09-17')]:
                                    page.locator('#amount').fill(amount)
                                    if dropdown:page.locator('#category').select_option(category)
                                    else:page.locator('#category').fill(category)
                                    page.locator('#date').fill(date);page.locator('#add-expense').click()
                                expect(page.locator('#total')).to_contain_text('0.30');page.reload()
                                expect(page.locator('#expenses')).to_contain_text(categories[1])
                                assert page.locator('#expenses b').count()==0
                                if args.step>=1:
                                    page.locator('#expenses').get_by_role('button',name='Edit',exact=True).first.click()
                                    page.locator('#amount').fill('0.40');page.locator('#save-expense').click();page.reload()
                                    expect(page.locator('#total')).to_contain_text('0.60')
                                    expect(page.locator('#expenses').get_by_role('button',name='Edit',exact=True)).to_have_count(2)
                                if args.step>=2:
                                    with page.expect_download() as event:page.locator('#export-csv').click()
                                    download=event.value;assert download.suggested_filename=='expenses.csv'
                                    with open(download.path(),newline='') as file:rows=list(csv.DictReader(file))
                                    assert rows==[{'date':'2026-09-16','category':categories[0],'amount':'0.40'},{'date':'2026-09-17','category':categories[1],'amount':'0.20'}],rows
                            elif args.scenario=='upload-web':
                                page.locator('#task').fill('<b>literal</b>');page.locator('#add').click()
                                page.locator('#task').fill('Keep me');page.locator('#add').click();page.reload()
                                expect(page.locator('#todos')).to_contain_text('<b>literal</b>')
                                assert page.locator('#todos b').count()==0
                                page.locator('#todos li').filter(has_text='<b>literal</b>').get_by_role('button',name='Delete',exact=True).click()
                                page.reload();expect(page.locator('#todos')).not_to_contain_text('<b>literal</b>')
                                expect(page.locator('#todos')).to_contain_text('Keep me')
                            else:
                                name='Browser '+str(width)
                                labelled(page,'Project name','text').fill(name);page.get_by_role('button',name='Create project',exact=True).click()
                                labelled(page,'Task title','text').fill('Browser task '+str(width))
                                labelled(page,'Priority','select').select_option('high')
                                labelled(page,'Status','select').select_option('todo')
                                page.get_by_role('button',name='Add task',exact=True).click()
                                visible('Browser task '+str(width))
                                page.reload();visible('Browser task '+str(width))
                                labelled(page,'Search','text').fill('NO_MATCH_9876')
                                absent('Browser task '+str(width))
                                labelled(page,'Search','text').fill('')
                                page.get_by_role('button',name='Edit',exact=True).last.click()
                                labelled(page,'Task title','text').fill('Edited browser task '+str(width))
                                page.get_by_role('button',name='Save task',exact=True).click()
                                visible('Edited browser task '+str(width))
                                page.get_by_role('button',name='Delete',exact=True).last.click()
                                page.reload();absent('Edited browser task '+str(width))
                            if args.scenario=='upload-medium' and args.step:
                                expect(labelled(page,'Priority filter','select')).to_be_visible()
                                labelled(page,'Priority filter','select').select_option('high')
                            assert not errors,errors
                            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'),'Horizontal viewport overflow'
                        finally:
                            page.screenshot(path=str(base/f'golden-{width}.png'),full_page=True)
                            context.tracing.stop(path=str(base/f'golden-{width}.zip'))
                            browser.close()
                check('browser '+str(width),browser_case)
        finally:stop();serverlog.close()
    report['passed']=all(c['passed'] for c in checks)
    report['runnable']=any(c['passed'] and c['name'] in {'application launch','all rows and Decimal total'} for c in checks)
    report['archive_unchanged']=hashlib.sha256(archive.read_bytes()).hexdigest()==report['archive_sha256']
    (base/'golden.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({'passed':report['passed'],'checks':[{k:v for k,v in c.items() if k not in {'trace','detail'}} for c in checks]}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--folder',required=True);parser.add_argument('--archive',required=True)
    parser.add_argument('--scenario',required=True);parser.add_argument('--step',type=int,default=0)
    run(parser.parse_args())
