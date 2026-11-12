"""Serialized, revision-bound qualification; evaluator assertions stay outside the agent.

Run only on the isolated candidate host. Development runs use different cases
from the 8 core / 4 web / 4 desktop held-out set. Resume skips finished trials;
never reinterpret failed trials as passes after changing engine or assertions.
"""
import argparse
import asyncio
from collections import defaultdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
import config
import database as db
import coder_jobs as controller
from db import coder_jobs as jobs, coder_promotion
from context_policy import DEFAULTS
from coder_worker_runtime import WorkerStore
from engine_judge import evaluate
from engine_cases import CASES, DEVELOPMENT
from engine_campaign import CampaignPaused, freeze, interruption, exception_interruption, paused, collect_evaluator_caches


def configure(root,args):
    db.DATABASE_PATH=str(root/'jobs.db'); config.DATABASE_PATH=db.DATABASE_PATH
    config.SANDBOX_OUTPUTS_DIR=str(root/'outputs'); config.ARTIFACT_DIR=str(root/'artifacts')
    config.OLLAMA_URL=args.ollama
    live=json.loads(args.settings.read_text()) if args.settings else {}
    config.CONTEXT_COMPACTION=live.get('context_compaction','on')
    config.CONTEXT_SETTINGS={**DEFAULTS,**live,'daedalus_v3_enabled':True,'daedalus_engine':'openhands159',
        'daedalus_role_thinking':{'builder':'on','compaction':'on'},'daedalus_model_calls':40,
        'daedalus_job_seconds':900,'daedalus_command_seconds':120}
    if args.cohort == 'reliability':
        config.CONTEXT_SETTINGS.update(daedalus_model_calls=120, daedalus_job_seconds=3600, daedalus_command_seconds=600)
    for key in ('daedalus_model_calls','daedalus_job_seconds','daedalus_command_seconds'):
        if key in live:config.CONTEXT_SETTINGS[key]=live[key]
    os.environ['DAEDALUS_ENGINE_URL']=args.worker
    controller.spawn=lambda *args:None
    config._EVALUATION_COHORT=args.cohort


def save(path, value):
    temporary=path.with_suffix('.tmp'); temporary.write_text(json.dumps(value,indent=2,default=str)+'\n'); temporary.replace(path)


async def run(args):
    root=args.output.resolve(); root.mkdir(parents=True,exist_ok=True)
    if not str(root).startswith('/opt/daedalus-openhands159-'):raise ValueError('Use the isolated candidate release')
    configure(root,args)
    lock=(root.parent/'inference-evaluation.lock').open('a'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    path=root/'report.json'
    report=json.loads(path.read_text()) if path.exists() else {'started':time.time(),'settings':config.CONTEXT_SETTINGS,
        'sdk':'1.59.0','model':args.model,'trials':[],'development':args.development}
    if args.cohort == 'reliability':
        receipts = collect_evaluator_caches(root.parent)
        if receipts:
            report.setdefault('cache_collections', []).append({'at': time.time(), 'receipts': receipts})
    identity=freeze(root.parent, config.CONTEXT_SETTINGS, args.cohort)
    if report.get('freeze') and report['freeze'] != identity:
        raise ValueError('Frozen runtime or settings changed; use a new campaign directory')
    report['freeze']=identity
    report.pop('paused',None)
    report['complete'] = False
    report['behavioral_gate_passed'] = False
    sources=Path(__file__).resolve().parents[1]
    hashes={str(p.relative_to(sources)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(sources.rglob('*.py')) if '__pycache__' not in p.parts}
    if report.get('source_hashes') and report['source_hashes']!=hashes:
        if not args.development:
            raise ValueError('Engine or evaluator changed; use a new qualification cohort')
        report.setdefault('development_source_history',[]).append(report['source_hashes'])
    report['source_hashes']=hashes
    await db.init_db()
    async def wait_for_other_inference(job_id):
        # An evaluator restart can leave a valid detached worker operation.
        # Drain it before starting any other model trial, including rechecks.
        from coder_worker_runtime import reconcile
        ledger=WorkerStore(root.parent/'state')
        deadline=time.monotonic()+config.CONTEXT_SETTINGS['daedalus_job_seconds']+900
        while True:
            with ledger.connect() as connection:
                rows=connection.execute("SELECT id FROM operations WHERE kind='code' AND job_id!=? AND status IN ('queued','starting','running','cancelling')",(job_id,)).fetchall()
            active=[reconcile(ledger,row['id']) for row in rows]
            if not any(op['status'] in {'queued','starting','running','cancelling'} and op['payload'].get('ollama_url')==args.ollama for op in active):return
            if time.monotonic()>deadline:raise TimeoutError('Another inference operation did not finish')
            await asyncio.sleep(3)
    async with httpx.AsyncClient(timeout=30) as http:
        controller.configure(http,None)
        response=await http.get(args.ollama+'/api/tags'); response.raise_for_status()
        model=next(m for m in response.json()['models'] if m['name']==args.model)
        if report.get('model_digest') and report['model_digest']!=model['digest']:
            raise ValueError('Model digest changed; use a new qualification cohort')
        report['model_digest']=model['digest']; save(path,report)
        if args.cohort=='reliability':
            from engine_cases_reliability import CASES as fresh_cases
        else: fresh_cases=CASES
        candidates=DEVELOPMENT if args.development else CASES if args.regressions else fresh_cases
        if args.case:candidates=[c for c in candidates if c['id'] in args.case.split(',')]
        for repeat in range(1,args.repeats+1):
            for case in candidates:
                key=f"{case['id']}-{repeat}"
                trial=next((t for t in report['trials'] if t['key']==key),None)
                if trial and 'finished' in trial:
                    if not (args.development and args.recheck_development and not trial['passed']):continue
                    trial.setdefault('attempts',[]).append({k:trial[k] for k in ('error','finished') if k in trial})
                    trial.pop('finished',None);trial.pop('error',None)
                if trial is None:
                    trial={'key':key,'case':case['id'],'family':case['family'],'repeat':repeat,'started':time.time(),'jobs':[],'passed':False}
                    report['trials'].append(trial)
                save(path,report)
                conversation=root.name+'-'+key
                if not await db.get_conversation(conversation):await db.create_conversation(conversation,model=args.model)
                project=''
                accepted_revision=''
                try:
                    for turn,task in enumerate([case['task'],case['followup']]):
                        execution=None
                        if case.get('toolkit') in {'qt','gtk'}:
                            execution={'packages':{'.':{'test':'QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m unittest discover -v'}}}
                        if len(trial['jobs'])>turn:
                            job=await jobs.get(trial['jobs'][turn]['id'])
                            if trial['jobs'][turn].get('exact_acceptance_verified'):
                                project=job['project_id']; accepted_revision=job['revision_id']
                                continue
                            if job['state'] in {'blocked','cancelled','waiting_for_input'}:
                                prior=trial.get('interruptions')
                                if prior:
                                    job=await controller.resume(job['id'],budget_mode='preserve',
                                        candidate_revision=job.get('revision_id') if job.get('candidate_artifact') else None)
                        else:
                            job=await controller.create(conversation,task,'edit_project' if project else 'build_from_prompt',
                                project,args.model,key+'-'+str(turn),engine='openhands159',execution_commands=execution)
                            trial['jobs'].append({'id':job['id']})
                        save(path,report)
                        await wait_for_other_inference(job['id'])
                        runner=asyncio.create_task(controller.run(job['id']))
                        deadline=time.monotonic()+config.CONTEXT_SETTINGS['daedalus_job_seconds']+900
                        while not runner.done():
                            if time.monotonic()>deadline:
                                current=await jobs.get(job['id'])
                                if interruption(current):
                                    runner.cancel()
                                    await asyncio.gather(runner, return_exceptions=True)
                                    raise CampaignPaused(interruption(current))
                                await controller.cancel(job['id']); raise TimeoutError('Controller run deadline')
                            current=await jobs.get(job['id']); trial['jobs'][turn]=current; save(path,report)
                            await asyncio.sleep(3)
                        await runner
                        job=await jobs.get(job['id']); trial['jobs'][turn]=job; save(path,report)
                        if interruption(job):raise CampaignPaused(interruption(job))
                        if not job.get('candidate_artifact'):raise AssertionError('No candidate: '+str(job.get('blocker')))
                        project=job['project_id']
                        if turn:
                            before=await db.get_coding_project(project)
                            assert before['accepted_revision_id']==accepted_revision
                            assert job['source_revision_id']==accepted_revision
                            selected,pointer,selected_revision=await controller.resolve_source(before)
                            assert pointer=='working' and selected_revision==job['revision_id']
                        result=await asyncio.to_thread(evaluate,root,job,case,turn==1)
                        trial['jobs'][turn]['held_out']=result; save(path,report)
                        if not result['passed']:raise AssertionError('Held-out behavior failed; assertions were not sent to the coding agent')
                        try:await coder_promotion.accept(project,'0'*40)
                        except (LookupError,ValueError):pass
                        else:raise AssertionError('Accepted an unknown revision')
                        await coder_promotion.accept(project,job['revision_id'])
                        promoted=await db.get_coding_project(project)
                        assert promoted['accepted_revision_id']==job['revision_id']
                        accepted_revision=job['revision_id']; trial['jobs'][turn]['exact_acceptance_verified']=True; save(path,report)
                    trial['passed']=True
                except (CampaignPaused, httpx.TransportError) as error:
                    paused(report,trial,error);save(path,report)
                    print('PAUSED',trial['key'],str(error),flush=True)
                    return
                except Exception as error:
                    reason=exception_interruption(error)
                    if reason:
                        paused(report,trial,reason);save(path,report)
                        print('PAUSED',trial['key'],reason,flush=True)
                        return
                    trial['error']=f'{type(error).__name__}: {error}'
                trial['finished']=time.time(); save(path,report)
                print(key,'PASS' if trial['passed'] else 'FAIL',trial.get('error',''),flush=True)
    families=defaultdict(list)
    for trial in report['trials']:families[trial['family']].append(trial['passed'])
    report['families']={family:{'successes':sum(outcomes),'runs':len(outcomes),'rate':sum(outcomes)/len(outcomes)} for family,outcomes in families.items()}
    expected=48 if not args.development and not args.regressions and not args.case and args.repeats==3 else len(candidates)*args.repeats
    report['complete']=len(report['trials'])==expected and all('finished' in t for t in report['trials'])
    report['behavioral_gate_passed']=report['complete'] and all(v['rate']>=.8 for v in report['families'].values())
    report['finished']=time.time(); save(path,report)
    print(json.dumps(report['families']),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--worker',default='http://127.0.0.1:8587')
    parser.add_argument('--ollama',default=os.environ.get('OLLAMA_URL','http://127.0.0.1:11434'))
    parser.add_argument('--model',default='qwen3.6-35b-a3b-gguf:latest')
    parser.add_argument('--development',action='store_true'); parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--case',default='')
    parser.add_argument('--recheck-development',action='store_true')
    parser.add_argument('--settings',type=Path)
    parser.add_argument('--cohort',choices=('classic','reliability'),default='classic')
    parser.add_argument('--regressions',action='store_true',help='Run the earlier cohort as development regressions; never revise its original scores')
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    finally:
        if args.cohort == 'reliability' and (args.output.parent / 'codex-candidate').is_file():
            receipts = collect_evaluator_caches(args.output.parent)
            path = args.output / ('cache-reclamation-' + uuid.uuid4().hex + '.json')
            if args.output.is_dir():
                path.write_text(json.dumps(receipts, indent=2) + '\n')


if __name__=='__main__':main()
