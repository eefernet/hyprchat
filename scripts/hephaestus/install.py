"""Stage Hephaestus code, locked controller environments and immutable runtime images.

This installs the modern runtime with recoverable activation. It never changes
HyprChat's connection, model settings, project revisions or guest identity.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sqlite3
import urllib.request
import fcntl
import time
import uuid


def atomic_write(path, content, mode=0o600):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.pending')
    try:
        with temporary.open('wb') as stream:
            stream.write(content);stream.flush();os.fsync(stream.fileno())
        temporary.chmod(mode);temporary.replace(path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:temporary.unlink(missing_ok=True)


def recover_activation(journal):
    """Restore the last runtime configuration after a failed/interrupted upgrade."""
    journal=Path(journal)
    if not journal.exists():return False
    state=json.loads(journal.read_text())
    if state['state']=='committed':journal.unlink();return False
    for name in state['services']:run('systemctl','stop',name)
    for item in state['files']:
        target=Path(item['target'])
        if item['backup']:
            atomic_write(target,Path(item['backup']).read_bytes(),item['mode'])
        else:target.unlink(missing_ok=True)
    run('systemctl','daemon-reload')
    for name in state['active_services']:run('systemctl','start',name)
    state['state']='rolled_back'
    atomic_write(Path(state['backup'])/'receipt.json',json.dumps(state,indent=2).encode())
    journal.unlink()
    return True


def activate_files(pending, journal, services, validate):
    """Publish only prepared configuration; retain source and rollback on failure."""
    journal=Path(journal)
    if recover_activation(journal):
        raise RuntimeError('Recovered interrupted runtime activation; retry with a fresh drain check')
    backup=journal.parent/('rollback-'+uuid.uuid4().hex)
    backup.mkdir(parents=True,mode=0o700)
    state={'state':'activating','backup':str(backup),'files':[],'services':list(services),
           'active_services':[name for name in services if subprocess.run(
               ['systemctl','is-active','--quiet',name]).returncode==0]}
    for index,(target,_,_) in enumerate(pending):
        target=Path(target)
        if target.is_symlink():raise RuntimeError('Refusing to replace a symlinked runtime configuration: '+str(target))
        saved=backup/str(index) if target.exists() else None
        if saved:atomic_write(saved,target.read_bytes())
        state['files'].append({'target':str(target),'backup':str(saved) if saved else None,
                               'mode':target.stat().st_mode & 0o777 if saved else None})
    atomic_write(journal,json.dumps(state).encode())
    try:
        for target,text,mode in pending:atomic_write(target,text.encode(),mode)
        run('systemctl','daemon-reload')
        for name in services:run('systemctl','restart',name)
        validate()
        state['state']='committed'
        atomic_write(journal,json.dumps(state).encode())
        atomic_write(backup/'receipt.json',json.dumps(state,indent=2).encode())
        journal.unlink()
    except BaseException:
        recover_activation(journal)
        raise


def validate_services(release=None):
    deadline=time.monotonic()+60
    while True:
        try:
            if release:
                for name in ('hephaestus-broker','hephaestus-api','hephaestus-modern'):
                    active=output('systemctl','show',name,'-p','WorkingDirectory','--value')
                    if active!=str(Path(release)/'backend'):
                        raise RuntimeError('A service still points at a previous Hephaestus release: '+name)
            for port in (8585,8587):
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=3) as response:
                    if response.status!=200:raise RuntimeError('Hephaestus health check failed')
                    health=json.load(response)
                    if health.get('status')!='ok':raise RuntimeError('Hephaestus runtime is not ready')
                    if port==8587 and (health.get('engine')!='openhands159' or health.get('sdk_version')!='1.59.0'):
                        raise RuntimeError('Hephaestus coding runtime version does not match this release')
            return
        except Exception:
            if time.monotonic()>=deadline:raise
            time.sleep(1)


def prepare_tokenizer(destination, backend, ollama):
    import sys
    sys.path.insert(0,str(backend))
    from coder_engine_tokens import ASSET_HASHES, MODEL_DIGEST, TOKENIZER_REPOSITORY, TOKENIZER_REVISION
    if destination.exists():
        for name,digest in ASSET_HASHES.items():
            if hashlib.sha256((destination/name).read_bytes()).hexdigest()!=digest:
                raise SystemExit('Existing tokenizer assets differ; refusing to overwrite them')
        return
    def fetch(url, body=None):
        request=urllib.request.Request(url,data=json.dumps(body).encode() if body else None,headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=120) as response: return response.read()
    tags=json.loads(fetch(ollama.rstrip('/')+'/api/tags'))['models']
    model=next((m['name'] for m in tags if m['digest']==MODEL_DIGEST),None)
    if not model: raise SystemExit('Pinned Qwen model is unavailable; set --ollama-url to its existing server')
    show=json.loads(fetch(ollama.rstrip('/')+'/api/show',{'model':model}))
    assets={'tokenizer.json':fetch(f'https://huggingface.co/{TOKENIZER_REPOSITORY}/resolve/{TOKENIZER_REVISION}/tokenizer.json'),
            'chat_template.jinja':show['template'].encode()}
    if {name:hashlib.sha256(value).hexdigest() for name,value in assets.items()}!=ASSET_HASHES:
        raise SystemExit('Tokenizer/template checksum mismatch; model configuration was not changed')
    staging=destination.with_suffix('.preparing'); staging.mkdir(exist_ok=True)
    for name,value in assets.items(): (staging/name).write_bytes(value)
    write(staging/'manifest.json',json.dumps({'model':model,'model_digest':MODEL_DIGEST,'repository':TOKENIZER_REPOSITORY,
        'revision':TOKENIZER_REVISION,'files':ASSET_HASHES},indent=2)+'\n')
    staging.rename(destination)


def require_drained(root):
    """Activation is explicit and fails closed while executions or pinned apps are live."""
    database=root/'control/broker/executions.sqlite'
    if database.exists():
        with sqlite3.connect(database) as db:
            if db.execute("SELECT 1 FROM executions WHERE status IN ('queued','running','stop_unconfirmed') LIMIT 1").fetchone():
                raise SystemExit('Release staged. Drain execution before activating a different runtime release.')
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'backend'))
    from hephaestus_inventory import discover, active_stores
    if active_stores(root,discover(root)):
        raise SystemExit('Release staged. A worker or document operation is active, including inference or preparation.')
    for path in (root/'projects').glob('*/root/hyprchat-documents/jobs/*/status.json'):
        if json.loads(path.read_text()).get('status') in {'queued','running'}:
            raise SystemExit('Release staged. Drain document jobs before activation.')
    if subprocess.run(['systemctl','is-active','--quiet','hephaestus-legacy']).returncode==0:
        with urllib.request.urlopen('http://127.0.0.1:8586/health',timeout=10) as response:
            legacy=json.load(response)
        if any(legacy.get(field)!=0 for field in ('active_runs','active_aider_runs')):
            raise SystemExit('Release staged. Legacy worker must report zero active jobs, or be explicitly drained and stopped.')
    for path in (root/'state').glob('*/previews/*/session.json'):
        state=json.loads(path.read_text())
        if state.get('desired_state')=='running' and state.get('lifecycle')=='pinned':
            raise SystemExit('Release staged. Stop pinned applications before activation; their data will be retained.')


def run(*args, **kwargs):
    subprocess.run([str(x) for x in args],check=True,**kwargs)


def output(*args):
    return subprocess.check_output([str(x) for x in args],text=True).strip()


def write(path, text, mode=0o644):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp'); temporary.write_text(text); temporary.chmod(mode); temporary.replace(path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument('--ollama-url',default=os.environ.get('OLLAMA_URL','http://127.0.0.1:11434'))
    args=parser.parse_args()
    if os.geteuid()!=0 or output('hostname','-s')!='hephaestus': raise SystemExit('Run inside Hephaestus as root')
    install_lock=Path('/etc/hephaestus/install.lock').open('a')
    try: fcntl.flock(install_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('Another Hephaestus installation is already running')
    root=Path('/srv/hephaestus')
    if not os.path.ismount(root): raise SystemExit('The data disk is not mounted; run bootstrap first')
    journal=root/'control/releases/activation.json'
    if recover_activation(journal):raise SystemExit('Recovered interrupted activation. Review service health, then rerun installation.')
    files=[]
    for parent in ('backend','scripts/hephaestus'):
        files.extend(p for p in (args.source/parent).rglob('*') if p.is_file() and
                     '__pycache__' not in p.parts and p.suffix in {'.py','.txt','.json','.sh','.core'})
    manifest={str(p.relative_to(args.source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
    identity=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()[:20]
    release=root/'releases'/identity; release.mkdir(parents=True,exist_ok=True)
    current=Path('/etc/hephaestus/current-release')
    if current.exists() and current.read_text().strip()==str(release) and (release/'release.json').exists():
        print(json.dumps({'release':str(release),'status':'already installed; services and data preserved'})); return
    for path in files:
        target=release/path.relative_to(args.source); target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()!=manifest[str(path.relative_to(args.source))]:
            raise SystemExit('Immutable release source was modified')
        if not target.exists(): shutil.copy2(path,target)
    write(release/'source-manifest.json',json.dumps(manifest,indent=2)+'\n')
    image_lock=Path('/etc/hephaestus/images.json')
    images=json.loads(image_lock.read_text()) if image_lock.exists() else {}
    if 'base' not in images:
        run('docker','pull','debian:13-slim')
        images['base']=json.loads(output('docker','image','inspect','debian:13-slim'))[0]['RepoDigests'][0]
        write(image_lock,json.dumps(images,indent=2)+'\n')
    image_tag='hephaestus-core:'+manifest['scripts/hephaestus/Dockerfile.core'][:20]
    available=subprocess.run(['docker','image','inspect',image_tag],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    if not available:
        run('docker','build','--build-arg','BASE_IMAGE='+images['base'],'-f',release/'scripts/hephaestus/Dockerfile.core',
            '-t',image_tag,release/'scripts/hephaestus')
    image=json.loads(output('docker','image','inspect',image_tag))[0]['Id']
    language_key=hashlib.sha256((image+manifest['scripts/hephaestus/Dockerfile.languages.core']).encode()).hexdigest()[:20]
    language_tag='hephaestus-languages:'+language_key
    if subprocess.run(['docker','image','inspect',language_tag],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        run('docker','build','--build-arg','BASE_IMAGE='+image_tag,'-f',release/'scripts/hephaestus/Dockerfile.languages.core',
            '-t',language_tag,release/'scripts/hephaestus')
    language_image=json.loads(output('docker','image','inspect',language_tag))[0]['Id']
    if 'dotnet8' not in images:
        run('docker','pull','mcr.microsoft.com/dotnet/sdk:8.0.408-bookworm-slim')
        images['dotnet8']=json.loads(output('docker','image','inspect','mcr.microsoft.com/dotnet/sdk:8.0.408-bookworm-slim'))[0]['RepoDigests'][0]
        write(image_lock,json.dumps(images,indent=2)+'\n')
    tools_key=hashlib.sha256((language_image+images['dotnet8']+manifest['scripts/hephaestus/Dockerfile.runtime-tools.core']).encode()).hexdigest()[:20]
    base_language_tag=language_tag
    language_tag='hephaestus-languages:'+tools_key
    if subprocess.run(['docker','image','inspect',language_tag],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        run('docker','build','--build-arg','BASE_IMAGE='+base_language_tag,'--build-arg','DOTNET_IMAGE='+images['dotnet8'],
            '-f',release/'scripts/hephaestus/Dockerfile.runtime-tools.core','-t',language_tag,release/'scripts/hephaestus')
    language_image=json.loads(output('docker','image','inspect',language_tag))[0]['Id']
    environments={}
    browsers={}
    for name,lock in [('modern','engine-runtime.lock.txt'),('documents','document-requirements.lock.txt')]:
        fingerprint=hashlib.sha256((release/'backend'/lock).read_bytes()).hexdigest()
        destination=root/'control/environments'/fingerprint
        ready=destination/'ready.json'
        if not ready.exists():
            destination.mkdir(parents=True,exist_ok=True)
            if not (destination/'venv/bin/python').exists(): run('python3','-m','venv',destination/'venv')
            run(destination/'venv/bin/pip','install','--require-hashes','-r',release/'backend'/lock)
            run(destination/'venv/bin/pip','check')
            write(ready,json.dumps({'lock_sha256':fingerprint,'installed':output(destination/'venv/bin/pip','freeze')})+'\n')
        environments[name]=str(destination/'venv')
        probe=subprocess.run([str(destination/'venv/bin/python'),'-c','import importlib.metadata; print(importlib.metadata.version("playwright"))'],capture_output=True,text=True)
        if probe.returncode==0:
            version=probe.stdout.strip()
            cache=root/'control/browsers'/version
            marker=cache/'installed.json'
            if not marker.exists():
                cache.mkdir(parents=True,exist_ok=True)
                run(destination/'venv/bin/python','-m','playwright','install','chromium',
                    env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(cache)})
                write(marker,json.dumps({'playwright':version,'lock_sha256':fingerprint})+'\n')
            browsers[name]=str(cache)
    prepare_tokenizer(root/'control/tokenizer',release/'backend',args.ollama_url)
    require_drained(root)
    pending=[]
    def stage(path,text,mode=0o644):pending.append((Path(path),text,mode))
    config_path=Path('/etc/hephaestus/runtime.json')
    config=json.loads(config_path.read_text()) if config_path.exists() else {}
    config.update({'version':1,'profiles':{**config.get('profiles',{}),'core':{'image':image}},
                   'roots':{'writable':[str(root/'projects'),str(root/'runs'),str(root/'state'),str(root/'applications')],
                            'readonly':[str(root/'releases'),str(root/'control/environments'),str(root/'projects'),
                                        str(root/'runs'),str(root/'state'),str(root/'applications')]}})
    config['profiles']['core-'+image.split(':')[1][:20]]={'image':image}
    config['profiles']['languages']={'image':language_image}
    config['profiles']['languages-'+language_image.split(':')[1][:20]]={'image':language_image}
    config['roots']['readonly'].append(str(root/'control/tokenizer'))
    config['roots']['readonly'].append(str(root/'control/browsers'))
    config['roots']['readonly'].append(str(root/'caches/shared'))
    for path in (root/'control/storage',root/'caches/shared'):
        path.mkdir(parents=True,exist_ok=True,mode=0o755)
    stage(config_path,json.dumps(config,indent=2)+'\n',0o600)
    token=Path('/etc/hephaestus/token')
    if not token.exists():
        import secrets
        write(token,secrets.token_hex(32)+'\n',0o600)
    stage('/etc/hephaestus/service.env','HEPHAESTUS_TOKEN='+token.read_text().strip()+'\nHEPHAESTUS_SHARED_CACHE=1\n',0o600)
    for name in ('modern','documents','generic'):
        (root/'state'/name).mkdir(parents=True,exist_ok=True)
        (root/'runs'/name).mkdir(parents=True,exist_ok=True)
    units={
        'hephaestus-broker':f'/usr/bin/python3 {release}/backend/hephaestus_runtime.py',
        'hephaestus-api':f'{environments["modern"]}/bin/python -m uvicorn hephaestus_api:app --host 0.0.0.0 --port 8585',
        'hephaestus-modern':f'{environments["modern"]}/bin/python -m uvicorn coder_engine_worker:app --host 0.0.0.0 --port 8587',
    }
    for name,command in units.items():
        role=name.removeprefix('hephaestus-')
        state_role=role if role == 'modern' else 'generic'
        text=f'''[Unit]
Description=Hephaestus {role}
After=docker.service network-online.target{' hephaestus-broker.service' if role != 'broker' else ''}
RequiresMountsFor=/srv/hephaestus

[Service]
Type=simple
WorkingDirectory={release}/backend
EnvironmentFile=/etc/hephaestus/service.env
Environment=HEPHAESTUS_EXECUTOR=docker
Environment=HEPHAESTUS_RUNTIME_PROFILE=languages
Environment=DAEDALUS_PROJECTS_DIR={root}/projects/{state_role}
Environment=PROJECTS_DIR={root}/projects/{state_role}
Environment=DAEDALUS_STATE_DIR={root}/state/{state_role}
Environment=TMPDIR={root}/runs/{state_role}
Environment=DAEDALUS_TOKENIZER_DIR={root}/control/tokenizer
Environment=PLAYWRIGHT_BROWSERS_PATH={browsers.get(role,browsers.get('modern',''))}
Environment=PYTHONUNBUFFERED=1
ExecStart={command}
Restart=on-failure
RestartSec=3
KillMode=control-group
TimeoutStopSec=90
UMask=0077

[Install]
WantedBy=multi-user.target
'''
        stage('/etc/systemd/system/'+name+'.service',text)
        # Prior upgrades may have release drop-ins. Own the final override while
        # preserving unrelated administrator limits and service configuration.
        override='[Service]\n'+f'WorkingDirectory={release}/backend\nExecStart=\nExecStart={command}\n'
        override+='\n'.join(line for line in text.splitlines() if line.startswith('Environment='))+'\n'
        stage('/etc/systemd/system/'+name+'.service.d/zzzzzz-hephaestus-release.conf',override)
    write(release/'release.json',json.dumps({'source':manifest,'environments':environments,'browsers':browsers,'image':image,'languages_image':language_image,'id':identity},indent=2)+'\n')
    # New installs protect their runtime; upgrades retain one verified rollback.
    import sys
    sys.path.insert(0,str(release/'backend'))
    from hephaestus_inventory import control,lock
    previous=Path('/etc/hephaestus/current-release')
    retained=[str(release)]
    if previous.exists() and Path(previous.read_text().strip(),'release.json').exists():
        retained.append(previous.read_text().strip())
    stage(control(root)/'retention.json',json.dumps({'version':1,'verified':True,'releases':list(dict.fromkeys(retained)),'images':[image,language_image]}),0o600)
    stage('/etc/systemd/system/hephaestus-cleanup.service',f'''[Unit]
Description=Lease-aware Hephaestus scratch and cache cleanup
RequiresMountsFor=/srv/hephaestus
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {release}/backend/hephaestus_storage.py
UMask=0077
''')
    stage('/etc/systemd/system/hephaestus-cleanup.timer','''[Unit]
Description=Periodic Hephaestus storage maintenance
[Timer]
OnBootSec=10min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
''')
    stage('/etc/hephaestus/current-release',str(release)+'\n')
    services=('hephaestus-broker','hephaestus-api','hephaestus-modern')
    with lock(root,'admission',blocking=False):
        require_drained(root)
        activate_files(pending,journal,services,lambda:validate_services(release))
    for name in services:run('systemctl','enable',name)
    run('systemctl','enable','--now','hephaestus-cleanup.timer')
    print(json.dumps({'release':str(release),'image':image,'status':'modern runtime installed; application activation and compatibility checks required'}))


if __name__=='__main__': main()
