"""Sequential reliability evaluation. Never enables production Settings.

Use fresh disk-backed state and isolated workers; the legacy worker must use
/root/projects. Failed focused pilots stop before launching expensive matrices.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evals.rollout_gate import FOCUSED_FIXTURES,focused_verdict,rollout_verdict


def run(args):
    root=Path(args.state);root.mkdir(parents=True,exist_ok=False)
    source=Path(__file__).resolve().parents[1]
    environment={**os.environ,'LITELLM_LOCAL_MODEL_COST_MAP':'True'}
    def note(stage,status,**extra):
        value={'stage':stage,'status':status,'at':time.time(),**extra}
        temp=root/'status.tmp';temp.write_text(json.dumps(value,indent=2));temp.replace(root/'status.json')
        print(json.dumps(value),flush=True)
    def stage(name,command):
        note(name,'running')
        with (root/(name+'.log')).open('w') as log:
            process=subprocess.Popen(command,cwd=source,env=environment,stdout=log,stderr=subprocess.STDOUT)
            (root/(name+'.pid')).write_text(str(process.pid))
            code=process.wait()
        if code: raise RuntimeError(f'{name} exited {code}; inspect its retained log')
        note(name,'completed')
        return json.loads((root/name/'results.json').read_text())
    def benchmark(name,version=3,focused=False,model=None):
        command=[args.api_python if version==2 else sys.executable,'evals/run_coder_benchmark.py',
            '--state',str(root/name),'--worker-url',args.legacy_worker_url if version==2 else args.worker_url,
            '--ollama-url',args.ollama_url,'--codebox-url',args.codebox_url,'--projects-root',args.projects_root,
            '--models',model or args.model,'--profile','reliability','--workflow-version',str(version),'--priority','grouping']
        if args.settings: command+=['--settings',args.settings]
        if version==3: command+=['--followups']
        if focused:
            for fixture in sorted(FOCUSED_FIXTURES): command+=['--fixture',fixture]
        return stage(name,command)
    try:
        from evals.coder_preflight import run_preflight
        run_preflight(args, root)
        if args.wait_for_report:
            note('previous-evaluation','waiting')
            previous=Path(args.wait_for_report)
            while True:
                try:
                    if 'promotion' in json.loads(previous.read_text()): break
                except (FileNotFoundError,json.JSONDecodeError): pass
                if args.wait_for_pid:
                    try: os.kill(args.wait_for_pid,0)
                    except ProcessLookupError: raise RuntimeError('Previous evaluation stopped without a complete report')
                time.sleep(5)
        from evals.check_ollama_schema import run as check_schema
        note('schema-compatibility','running')
        if not check_schema(args.ollama_url.rstrip('/'),args.model,root/'schema-compatibility.json'):
            note('schema-compatibility','blocked');return 1
        pilot=benchmark('focused',focused=True)
        gate=focused_verdict(pilot);(root/'focused-gate.json').write_text(json.dumps(gate,indent=2))
        if not gate['eligible']:
            alternatives=[]
            for index,model in enumerate(args.compare_models):
                report=benchmark(f'comparison-{index+1}',focused=True,model=model)
                verdict=focused_verdict(report)
                (root/f'comparison-{index+1}-gate.json').write_text(json.dumps(verdict,indent=2))
                if verdict['eligible']:
                    rows=report['results']
                    calls=sum(r.get('calls',0)+sum(e.get('calls',0) for e in r.get('followups',{}).get('requests',[])) for r in rows)
                    seconds=sum(r.get('seconds',0) for r in rows)  # Row elapsed time already includes its follow-ups.
                    alternatives.append((calls,seconds,model))
            if not alternatives:
                note('focused','blocked',reasons=gate['reasons'],comparison='No configuration qualified');return 1
            calls,seconds,args.model=min(alternatives)
            (root/'selected-model.json').write_text(json.dumps({'model':args.model,'calls':calls,'seconds':seconds},indent=2))
        first=benchmark('matrix-1');second=benchmark('matrix-2');legacy=benchmark('legacy',version=2)
        command=[args.api_python,'evals/run_coder_journeys.py','--state',str(root/'journeys'),
            '--worker-url',args.worker_url,'--restore-worker-url',args.restore_worker_url,'--ollama-url',args.ollama_url,
            '--codebox-url',args.codebox_url,'--model',args.model,'--profile','reliability']
        if args.settings: command+=['--settings',args.settings]
        journeys=stage('journeys',command)
        gate=rollout_verdict(first,second,legacy,journeys)
        (root/'rollout-gate.json').write_text(json.dumps(gate,indent=2))
        note('finished','completed',eligible=gate['eligible'],deployment_health_required=True)
        return 0 if gate['eligible'] else 1
    except Exception as error:
        note('error','stopped',error=str(error));raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('state','worker-url','legacy-worker-url','restore-worker-url','projects-root','model','ollama-url','codebox-url','api-python'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--settings');parser.add_argument('--wait-for-report');parser.add_argument('--wait-for-pid',type=int)
    parser.add_argument('--compare-models',nargs='*',default=['qwen3.6:27b','qwen3-coder:30b'])
    raise SystemExit(run(parser.parse_args()))
