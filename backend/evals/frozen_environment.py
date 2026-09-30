"""Read-only release identity and actual sandbox preflight; never imports project code."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from coder_sandbox import Sandbox


def package_manifest(interpreter):
    code = "import importlib.metadata as m,json; print(json.dumps(sorted((d.metadata['Name'], d.version) for d in m.distributions())))"
    result = subprocess.run([interpreter, '-c', code], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def environment_identity():
    result = {'python': sys.version, 'packages': package_manifest(sys.executable)}
    for name, interpreter in [('system_packages', '/usr/bin/python3'), ('worker_packages', '/root/venv/bin/python3')]:
        if Path(interpreter).exists(): result[name] = package_manifest(interpreter)
    result['os_packages'] = subprocess.run(['dpkg-query', '-W', '-f=${Package}=${Version}\n'], capture_output=True,
                                           text=True, timeout=30, check=True).stdout.splitlines()
    result['browser_files'] = {str(p): {'size': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in Path('/root/.cache/ms-playwright').glob('*/chrome-linux*/chrome')}
    return result


def preflight(folder, settings):
    from context_policy import resolve
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    code = '''import json,subprocess
from playwright.sync_api import sync_playwright
from coder_sandbox_call import browser_options
rows={}
for name,args in {'python':['python3','--version'],'node':['node','--version'],'npm':['npm','--version'],
 'java':['java','-version'],'maven':['mvn','--version'],'c':['cc','--version'],'cpp':['c++','--version'],
 'cmake':['cmake','--version'],'dotnet':['dotnet','--version'],'go':['go','version'],'rust':['rustc','--version'],
 'cargo':['cargo','--version'],'bubblewrap':['bwrap','--version'],'strace':['strace','--version']}.items():
 try:
  p=subprocess.run(args,capture_output=True,text=True,timeout=30)
  rows[name]={'passed':p.returncode==0,'version':(p.stdout+p.stderr)[:1000]}
 except Exception as e: rows[name]={'passed':False,'error':str(e)}
try:
 with sync_playwright() as p:
  b=p.chromium.launch(headless=True,**browser_options()); page=b.new_page()
  page.set_content('<title>Daedalus preflight</title>'); assert page.title()=='Daedalus preflight'
  rows['browser']={'passed':True,'version':b.version,'path':p.chromium.executable_path}; b.close()
except Exception as e: rows['browser']={'passed':False,'error':str(e)}
print(json.dumps(rows))
'''
    backend = Path(__file__).resolve().parents[1]
    with Sandbox([sys.executable, '-c', code], cwd=folder, writable=[folder], readonly=list(backend.glob('*.py')),
                 environment={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONPATH': str(backend)}) as box:
        run = subprocess.run(box.args, env=box.env, capture_output=True, text=True, timeout=180)
    (folder / 'output.log').write_text(run.stdout + run.stderr)
    rows = json.loads(run.stdout) if run.returncode == 0 else {'boundary': {'passed': False, 'error': run.stderr[-3000:]}}
    return {'passed': all(r['passed'] for r in rows.values()), 'checks': rows,
            'model_policy': {role: resolve(role, settings).as_dict() for role in ('architect', 'builder', 'reviewer', 'acceptance', 'compaction')}}


def verify_worker_policy(worker_root, job_id, settings):
    from coder_worker_runtime import WorkerStore
    from context_policy import resolve
    ledger = WorkerStore(worker_root)
    expected = [resolve(role, settings).as_dict() for role in ('architect','builder','reviewer','acceptance','compaction','qa')]
    failures, calls, operations = [], 0, []
    with ledger.connect() as connection:
        ids = [row['id'] for row in connection.execute('SELECT id FROM operations WHERE job_id=?', (job_id,))]
    for identity in ids:
        operation = ledger.get(identity); operations.append(identity)
        if operation['payload'].get('settings') != settings:
            failures.append(identity + ': worker payload differs from frozen effective Settings')
        calls += operation['calls']
        cursor = 0
        while True:
            events = ledger.events(identity, cursor)
            if not events: break
            for event in events:
                context = event.get('data', {}).get('context')
                if context and context not in expected:
                    failures.append(identity + ': actual model context differs from frozen policy')
            cursor = events[-1]['seq']
    return {'passed': not failures, 'failures': failures, 'calls': calls, 'operations': operations}
