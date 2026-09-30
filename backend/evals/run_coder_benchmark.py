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
from coder_verification import POLICY_VERSION
from evals.inference_meter import InferenceMeter


def source_identity():
    root=Path(__file__).resolve().parents[1]
    return {path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob('*.py')) if not {'tests','__pycache__'}.intersection(path.relative_to(root).parts)}


def evaluation_profile(name):
    roles=('architect','builder','reviewer','acceptance','fixer','aider','qa','compaction','visual')
    outputs={role:8192 for role in roles}
    thinking={role:'off' for role in roles}
    if name in {'B','reliability'}:
        for role in ('architect','reviewer','acceptance'):
            outputs[role]=16384;thinking[role]='on'
    return {'openhands_num_ctx':65536 if name=='reliability' else 32768,'daedalus_role_outputs':outputs,'daedalus_role_thinking':thinking,
        'daedalus_job_seconds':3600,'daedalus_model_calls':120}


async def evaluated_fixture(http,fixture,identity,project_id,model,state,args,settings):
    async with InferenceMeter(args.ollama_url,settings,model,state/(identity+'-inference.json'),enforce_profile=args.workflow_version==2) as meter:
        previous_create=coder_jobs.create;previous_worker=coder_jobs._worker_request
        async def create(*pos,**kw):
            meter.begin(kw.get('key') or str(pos[:2]));return await previous_create(*pos,**kw)
        async def worker(*pos,**kw):
            body=kw.get('json')
            if isinstance(body,dict) and 'kind' in body and 'ollama_url' in body:
                role={'plan':'architect','verify':'reviewer','accept':'acceptance','qa':'qa','visual':'visual'}.get(body['kind'],'builder')
                kw['json']={**body,'ollama_url':meter.url+'/roles/'+role}
            return await previous_worker(*pos,**kw)
        coder_jobs.create=create;coder_jobs._worker_request=worker
        try:
            if args.workflow_version==2:
                from evals.legacy_runner import legacy_fixture
                job=await legacy_fixture(http,fixture,identity,model,project_id,state,settings,meter)
            else:
                config.OLLAMA_URL=meter.url
                job=await coder_jobs.create(identity,fixture['task'],'fix_uploaded_project' if project_id else 'build_from_prompt',project_id,model,key=identity)
                print(json.dumps({'fixture':fixture['id'],'model':model,'job_id':job['id'],'state':'started'}),flush=True)
                while job['state'] in store.ACTIVE:
                    await asyncio.sleep(2);job=await store.get(job['id'])
            evidence={'passed':False,'error':job.get('blocker','No accepted artifact')}
            if job['state']=='completed' and job.get('artifact'):
                try:evidence=await asyncio.to_thread(golden_check,fixture,job['artifact'],state/hashlib.sha256(model.encode()).hexdigest()[:8])
                except Exception as error:evidence={'passed':False,'error':f'{type(error).__name__}: {error}'}
            followups=None
            calls=meter.calls
            if args.followups and evidence['passed'] and args.workflow_version==3:
                followups=await followup_check(job,fixture,model,state/hashlib.sha256(model.encode()).hexdigest()[:8],meter)
            return job,evidence,followups,calls
        finally:
            coder_jobs.create=previous_create;coder_jobs._worker_request=previous_worker;config.OLLAMA_URL=args.ollama_url


async def followup_check(job,fixture,model,state,meter=None):
    """Exercise accepted-project continuity using separate user requests."""
    if fixture['id']!='grouping': return None
    project_id=job['project_id']; original=job['artifact']
    tasks=[('Add an exported countBy(items,key) function in groups.mjs returning a Map of each item[key] to its count. Keep groupBy behavior unchanged. Add regression tests and document countBy.',
            "import {countBy,groupBy} from './groups.mjs'; import assert from 'node:assert/strict'; const items=[{k:'__proto__'},{k:'x'},{k:'__proto__'}]; assert.deepEqual([...countBy(items,'k')],[['__proto__',2],['x',1]]); assert.equal(groupBy(items,'k').get('__proto__').length,2);"),
           ('Update countBy to support an optional minimumCount argument, default 1, returning only entries meeting that count. Keep groupBy and existing countBy calls compatible. Add tests.',
            "import {countBy,groupBy} from './groups.mjs'; import assert from 'node:assert/strict'; const items=[{k:'a'},{k:'b'},{k:'a'}]; assert.deepEqual([...countBy(items,'k',2)],[['a',2]]); assert.deepEqual([...countBy(items,'k')],[['a',2],['b',1]]); assert.equal(groupBy(items,'k').get('b').length,1);")]
    rows=[]
    for index,(task,golden) in enumerate(tasks):
        started=time.monotonic()
        edited=await coder_jobs.create(job['conversation_id'],task,'edit_project',project_id,model,key=job['id']+f'-followup-{index}')
        while edited['state'] in store.ACTIVE:
            await asyncio.sleep(2);edited=await store.get(edited['id'])
        result={'passed':False,'error':edited.get('blocker','No accepted artifact')}
        if edited.get('artifact'):
            try:result=await asyncio.to_thread(golden_check,{**fixture,'golden':golden},edited['artifact'],state/f'followup-{index}')
            except Exception as error:result={'passed':False,'error':f'{type(error).__name__}: {error}'}
        rows.append({'job_id':edited['id'],'project_id':edited['project_id'],'source_job_id':edited.get('source_job_id'),
                     'state':edited['state'],'evidence':result,'seconds':time.monotonic()-started,
                     'calls':meter.calls if meter else edited.get('calls_used'),
                     'stage_usage':edited.get('stage_usage',[]),'probe_replacements':edited.get('probe_replacements',{}),
                     'failure':edited.get('failure'),'verification_events':edited.get('verification_events',[]),
                     'false_acceptance':edited['state']=='completed' and not result['passed']})
        if not result['passed']:break
        job=edited
    preserved=hashlib.sha256(Path(original['storage_path']).read_bytes()).hexdigest()==original['sha256']
    return {'passed':len(rows)==2 and all(row['evidence']['passed'] for row in rows) and preserved,
            'original_artifact_preserved':preserved,'requests':rows}


def validate_resume(report, expected):
    for key in ('settings','source_hashes','environment','models','workflow_version','policy_version'):
        if report.get(key) != expected.get(key):
            raise ValueError(f'Benchmark {key} changed. Use a fresh --state directory rather than combining incompatible results.')


def matching_baseline(baseline,current):
    if not baseline or baseline.get('workflow_version')!=2:return False
    fixture='evals/coder_fixtures.py'
    if not current.get('source_hashes',{}).get(fixture) or baseline.get('source_hashes',{}).get(fixture)!=current['source_hashes'][fixture]:return False
    if not current.get('models') or baseline.get('models')!=current['models']:return False
    keys=('default_num_ctx','openhands_num_ctx','generation_num_predict','daedalus_job_seconds','daedalus_model_calls',
          'daedalus_attempt_turns','daedalus_role_contexts','daedalus_compaction','context_headroom_percent',
          'context_compaction_threshold','daedalus_command_seconds','daedalus_browser_step_seconds')
    keys=(*keys,'daedalus_role_outputs','daedalus_role_thinking','daedalus_browser_startup_seconds')
    return all(key in baseline.get('settings',{}) and baseline['settings'][key]==current['settings'][key] for key in keys)


def promotion_result(rows, baseline_rows):
    """Count exact fixture identities and require the project-update scenario."""
    identities={fixture['id'] for fixture in FIXTURES}
    complete=len(rows)==len(identities) and {r['fixture'] for r in rows}==identities
    baseline_complete=len(baseline_rows)==len(identities) and {r['fixture'] for r in baseline_rows}==identities
    large_ids={fixture['id'] for fixture in FIXTURES if fixture.get('padding')}
    large_passed=all(any(r['fixture']==identity and r['passed'] for r in rows) for identity in large_ids)
    edits=next((r.get('followups') or {} for r in rows if r['fixture']=='grouping'),{})
    followups=bool(edits.get('passed') and edits.get('original_artifact_preserved')
        and len(edits.get('requests',[]))==2 and all(e.get('evidence',{}).get('passed')
            and not e.get('false_acceptance') for e in edits['requests']))
    passed=sum(r['passed'] for r in rows);false_accepts=sum(r['false_acceptance'] for r in rows)
    false_accepts+=sum(request.get('false_acceptance',False) for r in rows for request in (r.get('followups') or {}).get('requests',[]))
    return {'large_projects_passed':large_passed,'followups_passed':followups,'passed':passed,'total':len(rows),
        'false_acceptance':false_accepts,'baseline_available':baseline_complete,
        'promotable':complete and baseline_complete and large_passed and followups and passed==len(identities) and not false_accepts
            and passed>=sum(r['passed'] for r in baseline_rows)}


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
    settings={**context_policy.DEFAULTS,'daedalus_v3_enabled':args.workflow_version==3,'daedalus_compaction':'on'}
    if args.profile:settings.update(evaluation_profile(args.profile))
    if args.settings:settings.update(json.loads(Path(args.settings).read_text()))
    if args.seconds:settings['daedalus_job_seconds']=args.seconds
    if args.calls:settings['daedalus_model_calls']=args.calls
    context_policy.apply_settings(settings)
    for field in ('ARCHITECT_MODEL','PLANNING_MODEL','REVIEWER_MODEL','ACCEPTANCE_MODEL','QA_MODEL','BUILDER_MODEL'):setattr(config,field,'')
    if args.codebox_url:config.CODEBOX_URL=args.codebox_url
    output=state/'results.json'
    from importlib.metadata import version,PackageNotFoundError
    packages={}
    for name in ('openhands-sdk','openhands-tools','litellm','tree-sitter','tree-sitter-javascript','tree-sitter-typescript','playwright'):
        try:packages[name]=version(name)
        except PackageNotFoundError:packages[name]=None
    async with httpx.AsyncClient() as metadata_client:
        response=await metadata_client.get(args.ollama_url.rstrip('/')+'/api/tags',timeout=30);response.raise_for_status()
        installed={m['name']:m for m in response.json()['models']}
    model_details={}
    for model in args.models:
        details=installed.get(model) or installed.get(model+':latest')
        if not details:raise ValueError(f'Evaluation model is not installed locally: {model}')
        model_details[model]={'digest':details['digest'],'details':details.get('details',{})}
    expected={'workflow_version':args.workflow_version,'policy_version':POLICY_VERSION if args.workflow_version==3 else 0,'models':model_details,'settings':settings,'environment':{'python':sys.version,'packages':packages},
              'source_hashes':source_identity()}
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
                if args.workflow_version==2:
                    (Path('/root/projects')/identity).mkdir(parents=True,exist_ok=True)
                if fixture.get('files'):
                    project_id=identity
                    source=Path('/root/projects' if args.workflow_version==2 else args.projects_root)/project_id
                    source.mkdir(parents=True,exist_ok=True)
                    for name,content in fixture['files'].items():
                        path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
                    if fixture.get('padding'):
                        padding=source/'padding';padding.mkdir(exist_ok=True)
                        for number in range(100):(padding/f'source-{number:03}.py').write_text('# Unrelated package source\n'*(fixture['padding']//100))
                    await db.upsert_coding_project(project_id,fixture['id'],conversation_id=identity,openhands_project_id=project_id)
                started=time.time()
                job,evidence,followups,calls=await evaluated_fixture(http,fixture,identity,project_id,model,state,args,settings)
                row={'fixture':fixture['id'],'model':model,'job_id':job['id'],'state':job['state'],'passed':bool(evidence['passed']),
                     'false_acceptance':job['state']=='completed' and not evidence['passed'],'evidence':evidence,
                     'seconds':time.time()-started,'calls':calls,'blocker':job.get('blocker',''),
                     'stage_usage':job.get('stage_usage',[]),'probe_replacements':job.get('probe_replacements',{}),
                     'failure':job.get('failure'),'verification_events':job.get('verification_events',[])}
                if followups is not None:row['followups']=followups
                report['results'].append(row);output.write_text(json.dumps(report,indent=2));print(json.dumps(row),flush=True)
    baseline=json.loads(Path(args.baseline).read_text()) if args.baseline else None
    baseline_matched=matching_baseline(baseline,expected)
    report['promotion']={}
    for model in args.models:
        rows=[r for r in report['results'] if r['model']==model]
        baseline_rows=[r for r in (baseline or {}).get('results',[]) if r['model']==model] if baseline_matched else []
        report['promotion'][model]={**promotion_result(rows,baseline_rows),'baseline_matched':baseline_matched}
    output.write_text(json.dumps(report,indent=2));print(json.dumps(report['promotion']),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',required=True);parser.add_argument('--worker-url',required=True);parser.add_argument('--ollama-url',required=True)
    parser.add_argument('--models',nargs='+',required=True);parser.add_argument('--projects-root',default='/root/projects')
    parser.add_argument('--settings');parser.add_argument('--seconds',type=int);parser.add_argument('--calls',type=int);parser.add_argument('--baseline')
    parser.add_argument('--profile',choices=['A','B','reliability']);parser.add_argument('--workflow-version',type=int,choices=[2,3],default=3)
    parser.add_argument('--codebox-url',default='http://127.0.0.1:8585')
    parser.add_argument('--fixture',action='append');parser.add_argument('--priority',nargs='*',default=[])
    parser.add_argument('--followups',action='store_true',help='Run two subsequent update requests after the grouping fixture')
    asyncio.run(main(parser.parse_args()))
