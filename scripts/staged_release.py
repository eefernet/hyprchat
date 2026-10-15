#!/usr/bin/env python3
"""Run on the HyprChat server: prepare, activate, or recover a staged release.

prepare never stops services. activate requires a matching live source baseline
and zero active domain jobs. Activation keeps the existing unit/environment and
changes only its interpreter/application directory with a recoverable drop-in.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import time
import urllib.request
import uuid


SYSTEMD_DIR = Path("/etc/systemd/system")

def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout.strip()


def atomic_json(path, payload):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        json.dump(payload, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def copy_owned(source,target):
    shutil.copy2(source,target)
    stat=Path(source).stat()
    os.chown(target,stat.st_uid,stat.st_gid)
    return str(target)


def copy_tree_owned(source,target):
    shutil.copytree(source,target,copy_function=copy_owned)
    for path in [Path(source),*(p for p in Path(source).rglob('*') if p.is_dir())]:
        stat=path.stat()
        os.chown(Path(target)/path.relative_to(source),stat.st_uid,stat.st_gid)


def tree_hashes(root):
    root = Path(root)
    result = {}
    for name in ('CHANGELOG.md','README.md'):
        if (root/name).is_file():
            result[name]=hashlib.sha256((root/name).read_bytes()).hexdigest()
    for sub in ('backend', 'frontend/dist', 'docs'):
        for path in sorted((root / sub).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix in {'.py','.json','.txt','.js','.css','.html','.svg','.png','.ico','.webmanifest','.md'}:
                result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def live_command(service):
    value = run(['systemctl','show',service,'-p','ExecStart','--value'])
    if 'argv[]=' not in value:
        raise RuntimeError('Unsupported service ExecStart; no changes made')
    args = shlex.split(value.split('argv[]=',1)[1].split(' ;',1)[0])
    if args[1:4] != ['-m','uvicorn','main:app']:
        raise RuntimeError('Expected existing single-worker uvicorn main:app service')
    if '--workers' in args and args[args.index('--workers') + 1] != '1':
        raise RuntimeError('This deployment supports one backend worker')
    return args


def prepare(root, source, service):
    root, source = Path(root).resolve(), Path(source).resolve()
    command = live_command(service)
    active_root = Path(run(['systemctl','show',service,'-p','WorkingDirectory','--value'])).parent
    baseline = tree_hashes(active_root)
    releases = root / '.releases'
    releases.mkdir(exist_ok=True)
    release = releases / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    release.mkdir()
    for name in ('backend','frontend/dist'):
        shutil.copytree(source / name, release / name, ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','*.db','*.log'))
    for name in ('CHANGELOG.md','README.md'):
        if (source/name).is_file():
            shutil.copy2(source/name,release/name)
    if (source/'docs').is_dir():
        shutil.copytree(source/'docs',release/'docs')
    lock = release / 'backend/requirements.lock.txt'
    if not lock.is_file() or not (release/'frontend/dist/index.html').is_file():
        raise RuntimeError('A compiled dependency lock and built frontend are required')
    # Use the deployed interpreter, including its Python minor version.
    run([command[0],'-m','venv',str(release/'venv')])
    python = str(release/'venv/bin/python')
    run([python,'-m','pip','install','--require-hashes','-r',str(lock)])
    run([python,'-m','pip','check'])
    run([python,'-m','compileall','-q',str(release/'backend')])
    isolated = release / 'migration-check'
    isolated.mkdir()
    live_db = root/'data/hyprchat.db'
    with sqlite3.connect(f'file:{live_db}?mode=ro',uri=True) as incoming, sqlite3.connect(isolated/'hyprchat.db') as outgoing:
        incoming.backup(outgoing)
    # init_db performs additive migrations only; no lifespan, workers, scheduler,
    # notifications, external service calls or automation dispatch are started.
    env = {**os.environ,'HYPRCHAT_DATA_DIR':str(isolated),'DATABASE_PATH':str(isolated/'hyprchat.db'),
           'PYTHONPATH':str(release/'backend')}
    run([python,'-c','import asyncio,database; asyncio.run(database.init_db())'],cwd=release/'backend',env=env)
    with sqlite3.connect(isolated/'hyprchat.db') as conn:
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Migration integrity check failed')
    state = {'state':'prepared','release':str(release),'root':str(root),'active_root':str(active_root),
             'service':service,'command':command,'baseline':baseline,'candidate':tree_hashes(release)}
    atomic_json(release/'release.json',state)
    return release


def active_jobs(database):
    count = 0
    with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as conn:
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('runs','research_reports','task_runs'):
            if table in tables:
                count += conn.execute(f"SELECT count(*) FROM {table} WHERE status IN ('queued','running','cancelling')").fetchone()[0]
        if 'coder_workflows' in tables:
            # Controllers can be active between subprocess runs. Checking only
            # runs would otherwise permit deployment in that gap.
            states = ('queued','inspecting','baselining','planning','verifying',
                      'coding','checking','reviewing','auditing','visual_review',
                      'accepting','packaging','candidate_packaging','cancelling',
                      'building','fixing','running')
            count += conn.execute('SELECT count(*) FROM coder_workflows WHERE workflow_version=3 AND state IN ('
                                  + ','.join('?' for _ in states) + ')', states).fetchone()[0]
        if 'image_jobs' in tables:
            for raw, in conn.execute('SELECT job_json FROM image_jobs'):
                if json.loads(raw).get('status') in {'submitting','queued','running','cancelling'}:
                    count += 1
    return count


def wait_ready(service,url,path='/api/health/live'):
    deadline=time.monotonic()+30
    while True:
        try:
            with urllib.request.urlopen(url.rstrip('/')+path,timeout=3) as response:
                if response.status!=200 or (path=='/api/health/live' and json.load(response).get('status')!='ok'):
                    raise RuntimeError('Readiness response failed')
            if run(['systemctl','is-active',service])!='active':
                raise RuntimeError('Service is not active')
            return
        except Exception:
            if time.monotonic()>=deadline:raise
            time.sleep(1)


def recover(root):
    journal = Path(root)/'.release-journal.json'
    if not journal.exists():
        return False
    state=json.loads(journal.read_text())
    if state['state']=='committed':
        journal.unlink()
        return False
    run(['systemctl','stop',state['service']])
    snapshot = state.get('database_snapshot')
    if snapshot:
        database = Path(root)/'data/hyprchat.db'
        temp = database.with_suffix('.rollback')
        copy_owned(snapshot,temp)
        with temp.open('rb') as stream:
            os.fsync(stream.fileno())
        for suffix in ('-wal','-shm'):
            Path(str(database)+suffix).unlink(missing_ok=True)
        os.replace(temp,database)
    for item in state.get('file_snapshots',[]):
        target=Path(item['target'])
        if target.exists():shutil.rmtree(target)
        if item['existed']:copy_tree_owned(item['snapshot'],target)
    dropin=Path(state['dropin'])
    if state['old_dropin'] is None:
        dropin.unlink(missing_ok=True)
    else:
        dropin.write_text(state['old_dropin'])
    run(['systemctl','daemon-reload'])
    run(['systemctl','restart',state['service']])
    wait_ready(state['service'],state.get('url','http://100.122.119.50:8000'),path='/api/users')
    journal.unlink()
    return True


def activate(release, url, acceptance):
    if not acceptance:
        raise RuntimeError("An explicit acceptance command is required before promotion")
    release=Path(release).resolve()
    state=json.loads((release/'release.json').read_text())
    root=Path(state['root'])
    if recover(root):
        raise RuntimeError('Recovered interrupted activation; prepare a fresh baseline before proceeding')
    if state['state']!='prepared' or tree_hashes(state['active_root'])!=state['baseline'] or tree_hashes(release)!=state['candidate']:
        raise RuntimeError('Source drift or unprepared candidate; activation refused')
    if active_jobs(root/'data/hyprchat.db'):
        raise RuntimeError('Active jobs exist; activation refused')
    service=state['service']
    dropin=SYSTEMD_DIR/(service+'.service.d')/'zz-hyprchat-release.conf'
    dropin.parent.mkdir(parents=True,exist_ok=True)
    state.update(state='activating',url=url,dropin=str(dropin),old_dropin=dropin.read_text() if dropin.exists() else None)
    journal=root/'.release-journal.json'
    atomic_json(journal,state)
    try:
        run(['systemctl','stop',service])
        # Snapshot only after every writer has stopped. The old release may
        # not understand new durable paths or additive migration state.
        rollback=release/('rollback-'+str(time.time_ns()))
        rollback.mkdir(mode=0o700)
        snapshot=rollback/'hyprchat.db'
        with sqlite3.connect(root/'data/hyprchat.db') as incoming, sqlite3.connect(snapshot) as outgoing:
            incoming.backup(outgoing)
        db_stat=(root/'data/hyprchat.db').stat()
        shutil.copystat(root/'data/hyprchat.db',snapshot)
        os.chown(snapshot,db_stat.st_uid,db_stat.st_gid)
        with snapshot.open('rb') as stream:
            os.fsync(stream.fileno())
        state['database_snapshot']=str(snapshot)
        # Chroma is rebuildable, but a library upgrade can migrate its SQLite
        # format. Keep the old vector store usable during environment rollback.
        vectors=root/'data/chroma_db'
        vector_copy=rollback/'chroma_db'
        existed=vectors.exists()
        if existed:copy_tree_owned(vectors,vector_copy)
        state['file_snapshots']=[{'target':str(vectors),'snapshot':str(vector_copy),'existed':existed}]
        atomic_json(journal,state)
        command=[str(release/'venv/bin/python'),*state['command'][1:]]
        # Paths and inherited service arguments are quoted for systemd, not a shell.
        service_config='[Service]\nWorkingDirectory='+str(release/'backend')+'\nEnvironment=HYPRCHAT_DATA_DIR='+str(root/'data')+'\nExecStart=\nExecStart='+shlex.join(command)+'\n'
        dropin.write_text(service_config+'Environment=HYPRCHAT_DEPLOY_QUALIFICATION=1\n')
        run(['systemctl','daemon-reload'])
        run(['systemctl','restart',service])
        wait_ready(service,url)
        acceptance_log=release/('acceptance-'+str(time.time_ns())+'.log')
        try:
            output=run(acceptance, env={**os.environ,"HYPRCHAT_URL":url}, timeout=300)
        except subprocess.CalledProcessError as exc:
            acceptance_log.write_text((exc.stdout or '')+(exc.stderr or ''))
            acceptance_log.chmod(0o600)
            raise
        acceptance_log.write_text(output)
        acceptance_log.chmod(0o600)
        # Only qualified releases start outbound automations. Retain the
        # rollback journal until the final normal startup succeeds.
        dropin.write_text(service_config)
        run(['systemctl','daemon-reload'])
        run(['systemctl','restart',service])
        wait_ready(service,url)
        state['state']='committed'
        atomic_json(journal,state)
        atomic_json(release/'release.json',state)
        journal.unlink()
    except BaseException:
        recover(root)
        raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','activate','recover'])
    p.add_argument('--root',default='/opt/hyprchat')
    p.add_argument('--source')
    p.add_argument('--release')
    p.add_argument('--service',default='hyprchat')
    p.add_argument('--acceptance',nargs='+',help='Executable and arguments; failure triggers rollback')
    p.add_argument('--url',default='http://100.122.119.50:8000')
    a=p.parse_args()
    if a.action=='prepare':
        if not a.source:p.error('--source is required')
        print(prepare(a.root,a.source,a.service))
    elif a.action=='activate':
        if not a.release:p.error('--release is required')
        activate(a.release,a.url,a.acceptance)
    else:recover(a.root)

if __name__=='__main__':main()
