"""Run isolated Daedalus fixtures against a worker; never enable production flags.

Run on Codebox, next to the backend modules, with an isolated worker service.
All verification is against the downloaded accepted artifact, outside its tree.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import config
import context_policy
import database as db
import coder_jobs
from db import coder_jobs as store
from coder_checks import browser_check
from evals.coder_fixtures import FIXTURES


def validate_resume(report, expected):
    for key in ('settings','source_hashes','environment'):
        if report.get(key) != expected[key]:
            raise ValueError(f'Benchmark {key} changed. Use a fresh --state directory rather than combining incompatible results.')


def golden_check(fixture, artifact, root):
    destination=root/'golden'/fixture['id'];destination.mkdir(parents=True,exist_ok=True)
    with tarfile.open(artifact['storage_path']) as bundle:
        bundle.extractall(destination,filter='data')
    for name in ('notes.txt','styles.css'):
        if name in fixture.get('files',{}) and (destination/name).read_text()!=fixture['files'][name]:
            return {'passed':False,'error':f'Unrelated file changed: {name}'}
    if fixture.get('padding'):
        for path in (destination/'padding').glob('*.py'):
            expected=hashlib.sha256(('# Unrelated package source\n'*(fixture['padding']//100)).encode()).hexdigest()
            if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                return {'passed':False,'error':'Unrelated source changed'}
        if len(list((destination/'padding').glob('*.py')))!=100:
            return {'passed':False,'error':'Unrelated source deleted'}
    if not fixture.get('files') and not any(destination.glob('README*')):
        return {'passed':False,'error':'Requested README missing'}
    if fixture.get('browser'):
        from playwright.sync_api import sync_playwright,expect
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=subprocess.Popen([sys.executable,'-m','http.server',str(port),'--bind','127.0.0.1'],cwd=destination,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,args=['--no-sandbox']);page=browser.new_page()
                for _ in range(40):
                    try:page.goto(f'http://127.0.0.1:{port}',timeout=1000);break
                    except Exception:time.sleep(.1)
                if fixture['browser']=='greeting':
                    page.locator('#name').fill('  Ada  ');page.locator('#greet').click();expect(page.locator('#result')).to_have_text('Hello, Ada!')
                    page.locator('#name').fill('');page.locator('#greet').click();expect(page.locator('#result')).to_have_text('Hello, World!')
                else:
                    page.locator('#task').fill('<b>literal</b>');page.locator('#add').click();page.reload()
                    expect(page.locator('#todos li')).to_have_count(1);expect(page.locator('#todos')).to_contain_text('<b>literal</b>')
                    expect(page.locator('#todos b')).to_have_count(0)
                    page.locator('#todos li button').click();page.reload();expect(page.locator('#todos li')).to_have_count(0)
                browser.close()
            return {'passed':True}
        except Exception as error:return {'passed':False,'error':str(error)}
        finally:server.terminate();server.wait(timeout=10)
    if fixture.get('language')=='javascript':
        command=['node','--input-type=module','-e',fixture['golden']]
    else:
        command=[sys.executable,'-c',fixture['golden']]
    result=subprocess.run(command,cwd=destination,capture_output=True,text=True,timeout=30)
    return {'passed':result.returncode==0,'exit_code':result.returncode,'stderr':result.stderr}


async def main(args):
    import httpx
    state=Path(args.state).resolve();state.mkdir(parents=True,exist_ok=True)
    db.DATABASE_PATH=str(state/'benchmark.sqlite3')
    config.SANDBOX_OUTPUTS_DIR=str(state/'artifacts')
    config.OPENHANDS_URL=args.worker_url;config.OLLAMA_URL=args.ollama_url
    settings={**context_policy.DEFAULTS,'daedalus_v3_enabled':True,'daedalus_compaction':'on'}
    if args.settings:settings.update(json.loads(Path(args.settings).read_text()))
    if args.seconds:settings['daedalus_job_seconds']=args.seconds
    if args.calls:settings['daedalus_model_calls']=args.calls
    context_policy.apply_settings(settings)
    for field in ('ARCHITECT_MODEL','PLANNING_MODEL','ACCEPTANCE_MODEL','QA_MODEL','BUILDER_MODEL'):setattr(config,field,'')
    output=state/'results.json'
    from importlib.metadata import version,PackageNotFoundError
    packages={}
    for name in ('openhands-sdk','openhands-tools','litellm','tree-sitter','tree-sitter-javascript','tree-sitter-typescript','playwright'):
        try:packages[name]=version(name)
        except PackageNotFoundError:packages[name]=None
    source=Path(__file__).resolve().parents[1]
    source_files=[*source.glob('coder_*.py'),*[source/path for path in ('context_policy.py','openhands_worker.py','database.py','config.py','db/coder_jobs.py','db/schema.py','db/artifacts.py','evals/coder_fixtures.py','evals/run_coder_benchmark.py')]]
    expected={'settings':settings,'environment':{'python':sys.version,'packages':packages},
              'source_hashes':{p.relative_to(source).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}}
    report=json.loads(output.read_text()) if output.exists() else {**expected,'results':[]}
    validate_resume(report,expected)
    output.write_text(json.dumps(report,indent=2))
    await db.init_db()

    async with httpx.AsyncClient() as http:
        coder_jobs.configure(http,None)
        ordered = sorted(FIXTURES,key=lambda fixture:(args.priority.index(fixture["id"]) if fixture["id"] in args.priority else len(args.priority)))
        for fixture in ordered:
            for model in args.models:
                if args.fixture and fixture['id'] not in args.fixture:continue
                if any(r['model']==model and r['fixture']==fixture['id'] for r in report['results']):continue
                identity='eval-'+hashlib.sha256(f'{state}:{model}:{fixture["id"]}'.encode()).hexdigest()[:20]
                if not await db.get_conversation(identity):await db.create_conversation(identity,model=model)
                project_id=''
                if fixture.get('files'):
                    project_id=identity
                    source=Path(args.projects_root)/project_id
                    source.mkdir(parents=True,exist_ok=True)
                    for name,content in fixture['files'].items():
                        path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
                    if fixture.get('padding'):
                        padding=source/'padding';padding.mkdir(exist_ok=True)
                        for number in range(100):(padding/f'source-{number:03}.py').write_text('# Unrelated package source\n'*(fixture['padding']//100))
                    await db.upsert_coding_project(project_id,fixture['id'],conversation_id=identity,openhands_project_id=project_id)
                started=time.time()
                job=await coder_jobs.create(identity,fixture['task'],'fix_uploaded_project' if project_id else 'build_from_prompt',project_id,model,key=identity)
                print(json.dumps({'fixture':fixture['id'],'model':model,'job_id':job['id'],'state':'started'}),flush=True)
                while job['state'] in store.ACTIVE:
                    await asyncio.sleep(2);job=await store.get(job['id'])
                evidence={'passed':False,'error':job.get('blocker','No accepted artifact')}
                if job['state']=='completed' and job.get('artifact'):
                    try:
                        evidence=await asyncio.to_thread(golden_check,fixture,job['artifact'],state/hashlib.sha256(model.encode()).hexdigest()[:8])
                    except Exception as error:
                        evidence={'passed':False,'error':f'{type(error).__name__}: {error}'}
                row={'fixture':fixture['id'],'model':model,'job_id':job['id'],'state':job['state'],'passed':bool(evidence['passed']),
                     'false_acceptance':job['state']=='completed' and not evidence['passed'],'evidence':evidence,
                     'seconds':time.time()-started,'calls':job.get('calls_used',0),'blocker':job.get('blocker','')}
                report['results'].append(row);output.write_text(json.dumps(report,indent=2));print(json.dumps(row),flush=True)
    baseline=json.loads(Path(args.baseline).read_text()) if args.baseline else None
    report['promotion']={}
    for model in args.models:
        rows=[r for r in report['results'] if r['model']==model];passed=sum(r['passed'] for r in rows)
        baseline_rows=[r for r in (baseline or {}).get('results',[]) if r['model']==model]
        large_ids={fixture['id'] for fixture in FIXTURES if fixture.get('padding')}
        large_passed=all(any(row['fixture']==identity and row['passed'] for row in rows) for identity in large_ids)
        report['promotion'][model]={'large_projects_passed':large_passed,'passed':passed,'total':len(rows),'false_acceptance':sum(r['false_acceptance'] for r in rows),
            'baseline_available':len(baseline_rows)==12,'promotable':large_passed and len(rows)==12 and passed>=9 and not any(r['false_acceptance'] for r in rows)
                and len(baseline_rows)==12 and passed>=sum(r['passed'] for r in baseline_rows)}
    output.write_text(json.dumps(report,indent=2));print(json.dumps(report['promotion']),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',required=True);parser.add_argument('--worker-url',required=True);parser.add_argument('--ollama-url',required=True)
    parser.add_argument('--models',nargs='+',required=True);parser.add_argument('--projects-root',default='/root/projects')
    parser.add_argument('--settings');parser.add_argument('--seconds',type=int);parser.add_argument('--calls',type=int);parser.add_argument('--baseline')
    parser.add_argument('--fixture',action='append');parser.add_argument('--priority',nargs='*',default=[])
    asyncio.run(main(parser.parse_args()))
