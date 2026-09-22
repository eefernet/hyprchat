"""Exercise the actual v2 dispatcher, including its normal gates and fallback.

Uses uniquely named fixture directories under the legacy /root/projects root.
Does not reset gates, synthesize successful reviews, or ship blocked artifacts.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import tarfile
import time

import config
import database as db


class Events:
    def __init__(self,path):self.path=path
    async def emit(self,conversation,event,data):
        with self.path.open('a') as log:log.write(json.dumps({'conversation':conversation,'event':event,'data':data})+'\n')


async def dispatch_with_recovery(dispatch,name,args,task,project_id):
    """Follow explicit legacy recovery directions without releasing its gates.

    All dispatches retain the fixture's original inference/time allowance.
    A repeated recovery direction remains blocked instead of looping forever.
    """
    attempted=set()
    while True:
        text,runs=await dispatch(name,args)
        if not text.startswith(('BLOCKED','ERROR')):return runs
        recovery=None
        if text.startswith('BLOCKED') and 'Your VERY NEXT tool call MUST be:' in text:
            instruction=text.split('Your VERY NEXT tool call MUST be:',1)[1].lstrip()
            if instruction.startswith('deep_research(') and name!='deep_research':
                recovery=('deep_research',{'topic':task+'\nRepair blocker: '+text[-1200:],'depth':2})
            elif instruction.startswith('generate_code(') and name in {'run_review','run_acceptance_review'}:
                recovery=('generate_code',{'project_id':project_id,'task':task+'\nComplete the missing planned deliverables described by the workflow:\n'+text})
        if not recovery or recovery[0] in attempted:raise RuntimeError(text)
        attempted.add(recovery[0])
        recovered,_=await dispatch(*recovery)
        if recovered.startswith(('BLOCKED','ERROR')):raise RuntimeError(recovered)


async def legacy_fixture(http,fixture,identity,model,project_id,state,settings,meter):
    import tools,model_providers
    root=Path('/root/projects')/identity
    if not root.is_dir():raise ValueError('Legacy fixture must be initialized under its unique /root/projects/eval-* directory')
    if not identity.startswith('eval-'):raise ValueError('Legacy benchmark needs an isolated eval-* identity')
    events=Events(state/(identity+'-events.jsonl'));started=time.monotonic()
    persona='eval-legacy-persona'
    if not await db.get_model_config(persona):await db.create_model_config(persona,'Daedalus v2 evaluation',model,tool_ids=['codeagent'])
    await db.update_conversation(identity,model_config_id=persona)
    await db.add_message(identity,'user',fixture['task'])
    meter.begin(identity)
    previous_complete=model_providers.complete_chat
    def complete(*args,**kwargs):
        import inspect
        caller=Path(inspect.currentframe().f_back.f_code.co_filename).stem
        role={'architect':'architect','reviewer':'reviewer','acceptance':'acceptance','fixer':'fixer'}.get(caller,'builder')
        kwargs['ollama_url']=meter.url+'/roles/'+role
        return previous_complete(*args,**kwargs)
    model_providers.complete_chat=complete
    config.OLLAMA_URL=meter.url+'/roles/builder'
    config.AIDER_WORKER_URL=config.OPENHANDS_URL
    for field in ('AIDER_MODEL','FIXER_MODEL','CODER_MODEL','DEFAULT_MODEL'):setattr(config,field,model)
    last='';accepted=False;env={}
    async def dispatch(name,args):
        nonlocal last,env
        left=settings['daedalus_job_seconds']-(time.monotonic()-started)
        if left<=0:raise TimeoutError('Evaluation execution allowance exhausted')
        before={r['id'] for r in await db.get_runs_by_conversation(identity,limit=1000)}
        last=await asyncio.wait_for(tools.exec_tool(http,events,name,args,identity,conv_model=model),left)
        runs=await db.get_runs_by_conversation(identity,limit=1000)
        fresh=[r for r in runs if r['id'] not in before]
        env=(fresh[0].get('result_envelope') or {}) if fresh else {}
        return last,fresh
    async def call(name,args):
        return await dispatch_with_recovery(dispatch,name,args,fixture['task'],identity)
    try:
        if project_id:
            await db.create_coder_workflow('legacy-'+identity,identity,project_id=identity,mode='fix_uploaded_project',
                state='uploaded',user_task=fixture['task'],contract={'project_dir':str(root)})
            await call('run_aider_fix',{'project_dir':str(root),'task':fixture['task']})
        else:
            await call('plan_project',{'task':fixture['task'],'language':fixture.get('language','python')})
            # The normal Architect/Builder handoff is a separate user turn.
            await db.add_message(identity,'assistant',last)
            await db.add_message(identity,'user','Build the agreed plan. Original request: '+fixture['task'])
            await call('generate_code',{'task':fixture['task'],'project_id':identity,'language':fixture.get('language','python')})
        while time.monotonic()-started<settings['daedalus_job_seconds']:
            await call('run_review',{'project_dir':str(root),'project_id':identity})
            runs=await db.get_runs_by_conversation(identity,limit=1000)
            review=next((r for r in runs if r['role']=='reviewer'),{})
            if (review.get('result_envelope') or {}).get('status')=='clean':
                await call('run_acceptance_review',{'project_dir':str(root),'project_id':identity,'reviewer_run_id':review['id']})
                runs=await db.get_runs_by_conversation(identity,limit=1000)
                acceptance=next((r for r in runs if r['role']=='acceptance'),{})
                if (acceptance.get('result_envelope') or {}).get('status')=='accepted':accepted=True;break
                issue=acceptance
            else:issue=review
            if not issue:raise RuntimeError('Legacy stage produced no review evidence')
            await call('run_aider_fix',{'project_dir':str(root),'task':fixture['task'],'reviewer_run_id':issue['id']})
    except Exception as error:
        last=f'{type(error).__name__}: {error}'
    finally:
        model_providers.complete_chat=previous_complete
        # A timed-out stream may have already marked its run cancelled locally.
        # Cancel by this fixture's exact run IDs, including terminal rows, so
        # no remote editor survives into the next measured fixture.
        for run in await db.get_runs_by_conversation(identity,limit=1000):
            role=run.get('role','')
            endpoint='aider/cancel' if role=='aider.fix' else 'cancel' if role.startswith('builder') else ''
            if endpoint:
                response=await http.post(f"{config.OPENHANDS_URL}/{endpoint}/{run['id']}",timeout=10)
                response.raise_for_status()
    artifact=None
    if accepted:
        output=state/(identity+'.tar.gz')
        with tarfile.open(output,'w:gz') as bundle:
            def exclude(info):
                return None if any(part in settings['daedalus_exclude_dirs'] for part in Path(info.name).parts) else info
            for path in root.iterdir():bundle.add(path,arcname=path.name,filter=exclude)
        artifact={'storage_path':str(output),'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}
    return {'id':'legacy-'+identity,'state':'completed' if accepted else 'blocked','artifact':artifact,
        'blocker':'' if accepted else last,'calls_used':meter.calls}
