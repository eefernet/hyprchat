"""Frozen bounded proof through the production controller and isolated worker.

Ten base jobs, up to four accepted-web edits and two uploaded-medium features.
No production settings, databases, projects, services or accepted heads change.
"""
from __future__ import annotations
import argparse,asyncio,hashlib,json,os,socket,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import config,context_policy,coder_jobs,database as db
from db import coder_jobs as jobs
from evals.policy7_fixtures import SCENARIOS,NODE_REPAIR_TASK,NODE_FEATURE_TASK,node_fixture
import httpx


def identities():
 root=Path(__file__).resolve().parents[1]
 return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*.py')) if '__pycache__' not in p.parts}


async def main(args):
 root=Path(args.root).resolve();root.mkdir(parents=True,exist_ok=False)
 source=identities()
 from evals.frozen_environment import environment_identity, preflight
 environment=environment_identity()
 settings=json.loads(Path(args.settings).read_text())
 model=settings.pop('coder_model','qwen3-coder:30b')
 for key in ('planning_model','architect_model','builder_model','reviewer_model','acceptance_model'):settings.pop(key,None)
 config.SANDBOX_OUTPUTS_DIR=str(root/'artifacts');db.DATABASE_PATH=str(root/'proof.sqlite3')
 context_policy.apply_settings(settings)
 settings=context_policy.runtime_settings()
 namespace=hashlib.sha256(str(root).encode()).hexdigest()[:12]
 coder_jobs.POLICY_VERSION=7  # Isolated evaluator only; never changes production selection.
 for name in ('ARCHITECT_MODEL','PLANNING_MODEL','BUILDER_MODEL','REVIEWER_MODEL','ACCEPTANCE_MODEL','QA_MODEL'):setattr(config,name,'')
 config.OLLAMA_URL=args.ollama_url
 with socket.socket() as reservation:reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
 config.OPENHANDS_URL=f'http://127.0.0.1:{port}'
 uploads=root/'uploads';uploads.mkdir()
 log=(root/'worker.log').open('w')
 env={**os.environ,'DAEDALUS_STATE_DIR':str(root/'worker'),'DAEDALUS_PROJECTS_DIR':str(uploads)}
 worker=subprocess.Popen([sys.executable,'-m','uvicorn','openhands_worker:app','--host','127.0.0.1','--port',str(port)],
  cwd=Path(__file__).resolve().parents[1],env=env,stdout=log,stderr=subprocess.STDOUT)
 results=[];report={'environment':environment,'policy_version':7,'source':source,'settings':settings,'model':model,'results':results,'max_jobs':args.max_jobs or 16,'namespace':namespace, 'input_hashes':{str(p):hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (args.settings,args.fixture_validation,args.cases) if p}}
 def save(): (root/'results.json').write_text(json.dumps(report,indent=2))
 async def run_case(name,scenario,task,conversation,project='',step=0,explicit=None,protected=None,language=''):
  assert identities()==source,'Frozen source identity changed; start a new run'
  assert context_policy.runtime_settings()==settings,'Effective Settings changed; start a new run'
  assert environment_identity()==environment,'Host/toolchain identity changed; start a new run'
  installed=(await http.get(args.ollama_url+'/api/tags',timeout=20)).json()['models']
  assert any(m.get('digest')==report['model_identity']['digest'] and m['name']==report['model_identity']['name'] for m in installed),'Model identity changed'
  assert len(results)<(args.max_jobs or 16),'Frozen job cap reached'
  folder=root/name;folder.mkdir()
  job=await coder_jobs.create(conversation,task,'edit_project' if project else 'build_from_prompt',project,model,key=namespace+':'+name,
      execution_commands=explicit,protected_files=protected)
  print(json.dumps({'case':name,'job_id':job['id'],'status':'started'}),flush=True)
  started=time.monotonic()
  while job['state'] in jobs.ACTIVE:
   await asyncio.sleep(2);job=await jobs.get(job['id'])
   (folder/'job.json').write_text(json.dumps(job,indent=2))
  (folder/'job.json').write_text(json.dumps(job,indent=2))
  artifact=job.get('artifact') or job.get('candidate_artifact')
  golden={'passed':False,'error':'No artifact or candidate was published'}
  if artifact:
   def independent():
    # Custom from-scratch builds are judged by the language verifier instead of a scenario golden.
    judge=([str(Path(__file__).with_name('verify_builds.py')),'--folder',str(folder),'--archive',artifact['storage_path'],'--language',language]
     if language else [str(Path(__file__).with_name('policy7_golden.py')),'--folder',str(folder),'--archive',artifact['storage_path'],'--scenario',scenario,'--step',str(step)])
    from evals.isolated_judge import run as isolated_judge
    result=isolated_judge(judge[0],judge[1:],folder,Path(artifact['storage_path']))
    (folder/'golden-output.log').write_text(result.stdout+result.stderr)
   try:await asyncio.to_thread(independent)
   except subprocess.TimeoutExpired:pass
   if (folder/'golden.json').exists():golden=json.loads((folder/'golden.json').read_text())
  row={'name':name,'scenario':scenario,'step':step,'job_id':job['id'],'state':job['state'],'policy_version':job['policy_version'],
   'model':model,'model_digest':report['model_identity'].get('digest'),
   'reason':job.get('blocker'),'calls':job.get('calls_used'), 'seconds':round(time.monotonic()-started,2),
   'internal_accepted':job['state']=='completed' and bool(job.get('artifact')),
   'independent_passed':golden.get('passed') is True,'artifact':artifact,
   'repair_round':job.get('repair_round',0),'audit_corrections':job.get('audit_corrections',0)}
  from evals.frozen_environment import verify_worker_policy
  row['worker_policy']=verify_worker_policy(root/'worker',job['id'],settings)
  row['category']=('accepted-correct' if row['independent_passed'] else 'accepted-incorrect') if row['internal_accepted'] else ('working-withheld' if row['independent_passed'] else 'broken-candidate' if artifact else 'environment-or-incomplete')
  row['source_unchanged']=identities()==source
  row['false_acceptance']=row['internal_accepted'] and not row['independent_passed']
  row['passed']=row['internal_accepted'] and row['independent_passed']
  results.append(row);save();print(json.dumps({'case':name,**{k:v for k,v in row.items() if k!='artifact'}}),flush=True)
  return job,row
 try:
  async with httpx.AsyncClient() as http:
   for _ in range(100):
    try:
     r=await http.get(config.OPENHANDS_URL+'/health',timeout=2)
     if r.status_code==200:break
    except httpx.HTTPError:pass
    await asyncio.sleep(.2)
   else:raise RuntimeError('Isolated worker did not start')
   installed=(await http.get(args.ollama_url+'/api/tags',timeout=20)).json()['models']
   matches=[m for m in installed if m['name']==model or m['name']==model+':latest']
   if not matches:raise RuntimeError(f'Model {model} is not installed at {args.ollama_url}; installed: '+', '.join(sorted(m['name'] for m in installed)))
   report['model_identity']=matches[0]
   report['fixture_validation']=json.loads(Path(args.fixture_validation).read_text())
   assert report['fixture_validation']['passed'],'Healthy uploaded fixture did not pass independently'
   report['preflight']=await asyncio.to_thread(preflight,root/'preflight',settings)
   save();assert report['preflight']['passed'],'Sandbox toolchain/browser preflight failed'
   await db.init_db();coder_jobs.configure(http,None)
   cases=[(name,tasks if name=='web' else tasks[:1],files) for name,tasks,files in SCENARIOS]
   cases.append(('upload-medium',[NODE_REPAIR_TASK,NODE_FEATURE_TASK],node_fixture(broken=True)))
   # A filtered run is a development smoke and can never qualify as a proof.
   selected=set(args.only.split(',')) if args.only else None
   report['smoke']=bool(selected) or args.repeats!=2 or bool(args.cases)
   if args.cases:
    for repeat in range(1,args.repeats+1):
     for case in json.loads(Path(args.cases).read_text()):
      if selected and case['name'] not in selected and case['language'] not in selected:continue
      name=f"pass{repeat}-{case['name']}";conversation='qa-'+namespace+'-'+name
      await db.create_conversation(conversation,model=model)
      await run_case(name,'build-'+case['language'],case['task'],conversation,language=case['language'])
    cases=[]
   for repeat in range(1,args.repeats+1):
    for scenario,tasks,files in cases:
     if selected and scenario not in selected:continue
     name=f'pass{repeat}-{scenario}';conversation='qa-'+namespace+'-'+name
     await db.create_conversation(conversation,model=model)
     project=''
     if files:
      project='upload-'+name;folder=uploads/project;folder.mkdir()
      for path,text in files.items():
       target=folder/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(text)
      await db.upsert_coding_project(project,scenario,conversation_id=conversation,openhands_project_id=project)
     explicit={'packages':{'.':{'test':'python3 tests/check_ledger.py'}}} if scenario=='upload-python' else None
     protected=['styles.css'] if scenario=='upload-web' else ['VERSION','public/styles.css'] if scenario=='upload-medium' else None
     job,row=await run_case(name,scenario,tasks[0],conversation,project,explicit=explicit,protected=protected)
     original=job.get('artifact')
     if row['passed']:
      for step,task in enumerate(tasks[1:],1):
       job,row=await run_case(name+'-edit'+str(step),scenario,task,conversation,job['project_id'],step,protected=protected)
       if not row['passed']:break
      assert hashlib.sha256(Path(original['storage_path']).read_bytes()).hexdigest()==original['sha256'],'Accepted archive changed'
   report['qualified']=not report['smoke'] and len(results)==16 and all(row['passed'] and row['worker_policy']['passed'] for row in results) and not any(row['false_acceptance'] for row in results)
   report['completed']=True
 finally:
  report['source_unchanged']=identities()==source
  report['environment_unchanged']=environment_identity()==environment
  report['settings_unchanged']=context_policy.runtime_settings()==settings
  report['qualified']=bool(report.get('qualified') and report['source_unchanged'] and report['environment_unchanged'] and report['settings_unchanged'])
  save()
  await coder_jobs.shutdown()
  # A terminated evaluator must not leave its own model or service processes running.
  from coder_worker_runtime import WorkerStore, cancel
  ledger=WorkerStore(root/'worker')
  with ledger.connect() as connection:
   active=[row['id'] for row in connection.execute("SELECT id FROM operations WHERE status IN ('queued','starting','running','cancelling')")]
  for identity in active:
   await asyncio.to_thread(cancel,ledger,identity)
  worker.terminate()
  try:worker.wait(timeout=20)
  except subprocess.TimeoutExpired:worker.kill();worker.wait()
  log.close()
 print(json.dumps({'completed':report.get('completed',False),'qualified':report.get('qualified',False),'jobs':len(results)}),flush=True)

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--settings',required=True)
 parser.add_argument('--fixture-validation',required=True);parser.add_argument('--only',default='');parser.add_argument('--repeats',type=int,default=2)
 parser.add_argument('--cases',default='');parser.add_argument('--max-jobs',type=int,default=16)
 parser.add_argument('--ollama-url',default='http://192.168.1.110:11434')
 asyncio.run(main(parser.parse_args()))
