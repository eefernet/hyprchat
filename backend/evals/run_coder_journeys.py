"""Real upload/create/update APIs with independent delivered-artifact checks.

Run on Codebox against two isolated workers. The second must use /root/projects
as its project registration root so the real upload endpoint can be exercised.
No application lifespan/scheduler or production database is started.
"""
import argparse
import asyncio
import csv
import hashlib
import io
import json
import socket
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import config
import context_policy
import database as db
import coder_jobs
from db import coder_jobs as store
from evals.run_coder_benchmark import evaluation_profile,source_identity
from evals.inference_meter import InferenceMeter


WEB_TASKS=[
    'Build a dependency-free expense tracker in index.html. Inputs #amount (decimal money), #category, #date (YYYY-MM-DD), and button #add-expense add rows to #expenses. '
    'Display the sum with two decimal places in #total. Preserve expenses across reloads with localStorage. Render entered text literally. Add a README and executable browser regression checks.',
    'Add an Edit button to each expense row. Clicking it loads that row into the existing inputs; #save-expense saves its changed amount, category and date without adding a duplicate. '
    'Preserve adding expenses and reload persistence, and recalculate #total. Add browser regression checks.',
    'Add a button #export-csv that downloads expenses.csv with header date,category,amount, one row per expense, two-decimal amounts and correct CSV quoting. '
    'Preserve adding, editing, totals and reload persistence. Document export and add regression checks.',
]
CLI_SOURCE='''import argparse,csv
from decimal import Decimal
VERSION = "keep-me"
def total(path):
    with open(path,newline="") as source:
        rows=csv.DictReader(source)
        next(rows,None)  # bug: skips the first expense
        return sum((Decimal(row["amount"]) for row in rows),Decimal(0))
if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("path")
    args=parser.parse_args();print(f"{total(args.path):.2f}")
'''
CLI_TASKS=[
    'Fix ledger.py so python3 ledger.py expenses.csv sums every CSV data row using Decimal and prints a two-decimal total. CSV columns are category,amount. Preserve VERSION and the existing CLI. Add tests and README.',
    'Add optional --category CATEGORY filtering and --json output to ledger.py. JSON must contain total as a two-decimal string and count as an integer number of matching rows. '
    'No matching rows returns total "0.00" and count 0. Preserve the existing plain total output and VERSION. Add regression tests and documentation.',
]


def extract(artifact,destination):
    source=Path(artifact['storage_path'])
    assert hashlib.sha256(source.read_bytes()).hexdigest()==artifact['sha256']
    destination.mkdir(parents=True,exist_ok=True)
    with tarfile.open(source) as bundle:bundle.extractall(destination,filter='data')


def web_golden(artifact,destination,step):
    from playwright.sync_api import sync_playwright,expect
    extract(artifact,destination)
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=subprocess.Popen([sys.executable,'-m','http.server',str(port),'--bind','127.0.0.1'],cwd=destination,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--no-sandbox'])
            try:
                for width in (1440,390):
                    context=browser.new_context(viewport={'width':width,'height':900},accept_downloads=True)
                    page=context.new_page();context.tracing.start(screenshots=True,snapshots=True,sources=True)
                    try:
                        for _ in range(50):
                            try:page.goto(f'http://127.0.0.1:{port}',timeout=1000);break
                            except Exception:time.sleep(.1)
                        page.locator('#amount').fill('0.10');page.locator('#category').fill('food,local');page.locator('#date').fill('2026-09-16');page.locator('#add-expense').click()
                        page.locator('#amount').fill('0.20');page.locator('#category').fill('<b>travel</b>');page.locator('#date').fill('2026-09-17');page.locator('#add-expense').click()
                        expect(page.locator('#total')).to_contain_text('0.30');page.reload()
                        expect(page.locator('#expenses')).to_contain_text('<b>travel</b>')
                        assert page.locator('#expenses b').count()==0
                        if step>=1:
                            page.locator('#expenses').get_by_role('button',name='Edit',exact=True).first.click()
                            page.locator('#amount').fill('0.40');page.locator('#save-expense').click();page.reload()
                            expect(page.locator('#total')).to_contain_text('0.60')
                            expect(page.locator('#expenses').get_by_role('button',name='Edit',exact=True)).to_have_count(2)
                        if step>=2:
                            with page.expect_download() as captured:page.locator('#export-csv').click()
                            download=captured.value;assert download.suggested_filename=='expenses.csv'
                            rows=list(csv.DictReader(Path(download.path()).read_text().splitlines()))
                            assert rows==[{'date':'2026-09-16','category':'food,local','amount':'0.40'},
                                          {'date':'2026-09-17','category':'<b>travel</b>','amount':'0.20'}]
                        page.screenshot(path=str(destination/f'golden-{width}.png'),full_page=True)
                    finally:
                        context.tracing.stop(path=str(destination/f'golden-{width}.zip'));context.close()
            finally:browser.close()
    finally:
        server.terminate()
        try:server.wait(timeout=5)
        except subprocess.TimeoutExpired:server.kill();server.wait()
    return {'passed':True}


def cli_golden(artifact,destination,step):
    extract(artifact,destination)
    (destination/'expenses.csv').write_text('category,amount\nfood,0.10\ntravel,0.20\n')
    def run(*args):
        result=subprocess.run([sys.executable,'ledger.py','expenses.csv',*args],cwd=destination,capture_output=True,text=True,timeout=15)
        assert result.returncode==0,result.stderr
        return result.stdout.strip()
    assert run()=='0.30'
    result=subprocess.run([sys.executable,'-c','from ledger import VERSION; assert VERSION == "keep-me"'],cwd=destination,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    if step:
        assert run('--category','food')=='0.10'
        assert json.loads(run('--category','food','--json'))=={'total':'0.10','count':1}
        assert json.loads(run('--category','missing','--json'))=={'total':'0.00','count':0}
    return {'passed':True}


async def main(args):
    import httpx
    state=Path(args.state).resolve();state.mkdir(parents=True,exist_ok=True)
    db.DATABASE_PATH=str(state/'journeys.sqlite3');config.SANDBOX_OUTPUTS_DIR=str(state/'artifacts')
    config.OLLAMA_URL=args.ollama_url;config.OPENHANDS_URL=args.worker_url;config.CODEBOX_URL=args.codebox_url
    settings={**context_policy.DEFAULTS,**evaluation_profile(args.profile),'daedalus_v3_enabled':True,'daedalus_compaction':'on'}
    if args.settings:settings.update(json.loads(Path(args.settings).read_text()))
    context_policy.apply_settings(settings)
    for field in ('ARCHITECT_MODEL','PLANNING_MODEL','REVIEWER_MODEL','ACCEPTANCE_MODEL','QA_MODEL','BUILDER_MODEL'):setattr(config,field,'')
    await db.init_db()
    import main as application
    async with httpx.AsyncClient() as metadata:
        response=await metadata.get(args.restore_worker_url.rstrip('/')+'/health');response.raise_for_status()
        if response.json().get('projects_root')!='/root/projects':
            raise RuntimeError('Upload journey restoration worker must see Codebox /root/projects uploads with separate worker state')
        response=await metadata.get(args.ollama_url.rstrip('/')+'/api/tags');response.raise_for_status()
        installed=next(m for m in response.json()['models'] if m['name'] in {args.model,args.model+':latest'})
    report={'profile':args.profile,'settings':settings,'models':{args.model:{'digest':installed['digest'],'details':installed.get('details',{})}},
        'source_hashes':source_identity(),'results':[]}
    originals=[]
    async with httpx.AsyncClient() as http, httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app),base_url='http://evaluation') as api:
        application.http_client=http;coder_jobs.configure(http,None)
        async with InferenceMeter(args.ollama_url,settings,args.model,state/'inference.json') as meter:
            config.OLLAMA_URL=meter.url
            previous=coder_jobs._worker_request
            async def worker(*pos,**kw):
                body=kw.get('json')
                if isinstance(body,dict) and 'ollama_url' in body:
                    role={'plan':'architect','verify':'reviewer','accept':'acceptance','visual':'visual'}.get(body.get('kind'),'builder')
                    kw['json']={**body,'ollama_url':meter.url+'/roles/'+role}
                return await previous(*pos,**kw)
            coder_jobs._worker_request=worker
            try:
                for scenario,tasks,golden in [('web',WEB_TASKS,web_golden),('upload',CLI_TASKS,cli_golden)]:
                    conversation='journey-'+hashlib.sha256((str(state)+scenario).encode()).hexdigest()[:16]
                    await db.create_conversation(conversation,model=args.model)
                    project=''
                    if scenario=='upload':
                        config.OPENHANDS_URL=args.restore_worker_url
                        archive=io.BytesIO()
                        with zipfile.ZipFile(archive,'w') as bundle:bundle.writestr('ledger.py',CLI_SOURCE)
                        response=await api.post('/api/coder/upload-project',data={'conv_id':conversation},files={'file':('ledger.zip',archive.getvalue(),'application/zip')})
                        response.raise_for_status();project=response.json()['project_id'];report['upload']=response.json()
                    for index,task in enumerate(tasks):
                        if scenario=='web' and index:config.OPENHANDS_URL=args.restore_worker_url
                        meter.begin(f'{scenario}-{index}');started=time.time()
                        response=await api.post('/api/coder/workflows',json={'conversation_id':conversation,'task':task,
                            'mode':'edit_project' if project else 'build_from_prompt','project_id':project,'model':args.model,'visual_review':False})
                        response.raise_for_status();job=response.json();project=job['project_id']
                        while job['state'] in store.ACTIVE:await asyncio.sleep(2);job=await store.get(job['id'])
                        evidence={'passed':False,'error':job.get('blocker')}
                        if job.get('artifact'):
                            try:evidence=await asyncio.to_thread(golden,job['artifact'],state/f'{scenario}-{index}',index)
                            except Exception as error:evidence={'passed':False,'error':f'{type(error).__name__}: {error}'}
                            originals.append(job['artifact'])
                        row={'scenario':scenario,'step':index,'job_id':job['id'],'project_id':project,'source_job_id':job.get('source_job_id'),
                            'state':job['state'],'seconds':time.time()-started,'calls':meter.calls,'evidence':evidence,
                            'stage_usage':job.get('stage_usage',[]),'probe_replacements':job.get('probe_replacements',{}),
                            'failure':job.get('failure'),'verification_events':job.get('verification_events',[]),
                            'false_acceptance':job['state']=='completed' and not evidence['passed']}
                        report['results'].append(row);(state/'results.json').write_text(json.dumps(report,indent=2));print(json.dumps(row),flush=True)
                        if not evidence['passed']:break
            finally:coder_jobs._worker_request=previous
    report['original_artifacts_preserved']=all(hashlib.sha256(Path(a['storage_path']).read_bytes()).hexdigest()==a['sha256'] for a in originals)
    report['passed']=len(report['results'])==5 and all(r['evidence']['passed'] for r in report['results']) and report['original_artifacts_preserved']
    (state/'results.json').write_text(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('state','worker-url','restore-worker-url','ollama-url','codebox-url','model'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--profile',choices=['A','B','reliability'],required=True)
    parser.add_argument('--settings',help='Explicit evaluation overrides, matching the benchmark settings file')
    asyncio.run(main(parser.parse_args()))
