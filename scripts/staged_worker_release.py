#!/usr/bin/env python3
"""Stage worker dependencies without mutating the accepted virtual environment.

Document activation atomically changes a current symlink. API-worker activation
uses a journaled systemd drop-in. Both require explicit acceptance commands and
retain the previous environment/source for rollback.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
import uuid
from staged_release import atomic_json,run,tree_hashes

SYSTEMD=Path('/etc/systemd/system')


def prepare(root,source,python,kind):
    root,source=Path(root).resolve(),Path(source).resolve()
    release=root/'.releases'/(time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
    release.mkdir(parents=True)
    for path in source.iterdir():
        if path.is_file() and path.suffix in {'.py','.txt','.json'}:shutil.copy2(path,release/path.name)
    for name in ('agents','db','tooling'):
        if (source/name).is_dir():shutil.copytree(source/name,release/name,ignore=shutil.ignore_patterns('__pycache__'))
    lock=release/('document-requirements.lock.txt' if kind=='documents' else 'worker-requirements.lock.txt')
    run([python,'-m','venv',str(release/'venv')])
    interpreter=str(release/'venv/bin/python')
    run([interpreter,'-m','pip','install','--require-hashes','-r',str(lock)])
    run([interpreter,'-m','pip','check'])
    # Validate our modules; packages can ship unused Python 2 compatibility
    # sources (olefile2.py) that are not importable on the deployed interpreter.
    run([interpreter,'-m','compileall','-q','-x',r'/venv/',str(release)])
    if kind=='documents':
        health=json.loads(run([interpreter,str(release/'document_worker.py'),'health']))
        if not health.get('ready'):raise RuntimeError('Staged document worker is not ready')
    else:
        run([interpreter,'-c','import openhands.sdk,litellm,coder_sdk_runtime,coder_worker_runtime'],cwd=release)
    state={'state':'prepared','root':str(root),'release':str(release),'kind':kind,
        'files':{str(p.relative_to(release)):__import__('hashlib').sha256(p.read_bytes()).hexdigest() for p in release.rglob('*.py') if 'venv' not in p.parts and '__pycache__' not in p.parts}}
    atomic_json(release/'release.json',state)
    return release


def active_documents():
    for path in Path('/root/hyprchat-documents/jobs').glob('*/status.json'):
        state=json.loads(path.read_text())
        if state.get('status') in {'queued','running','cancelling'}:raise RuntimeError('Document jobs are active; activation refused')


def recover(root):
    journal=Path(root)/'.worker-release-journal.json'
    if not journal.exists():return
    state=json.loads(journal.read_text())
    if state['state']=='committed':journal.unlink();return
    if state['kind']=='documents':
        link=Path(root)/'current'
        if state.get('old_target'):
            temp=Path(root)/'.rollback-current';temp.unlink(missing_ok=True);temp.symlink_to(state['old_target']);os.replace(temp,link)
        else:link.unlink(missing_ok=True)
    else:
        dropin=Path(state['dropin'])
        if state.get('old_dropin') is None:dropin.unlink(missing_ok=True)
        else:dropin.write_text(state['old_dropin'])
        run(['systemctl','daemon-reload']);run(['systemctl','restart','openhands-worker'])
        if run(['systemctl','is-active','openhands-worker'])!='active':raise RuntimeError('Worker rollback needs operator attention')
    journal.unlink()


def activate(release,acceptance):
    if not acceptance:raise RuntimeError('A qualification acceptance command is required')
    release=Path(release);state=json.loads((release/'release.json').read_text());root=Path(state['root'])
    if (root/'.worker-release-journal.json').exists():raise RuntimeError('Recover the previous activation first')
    if state['state']!='prepared':raise RuntimeError('Unprepared worker release')
    for name,digest in state['files'].items():
        if __import__('hashlib').sha256((release/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('Staged source changed after qualification')
    if state['kind']=='documents':
        active_documents()
        current=root/'current'
        if current.exists() and not current.is_symlink():raise RuntimeError('Current path is not a managed symlink')
        state['old_target']=str(current.resolve()) if current.is_symlink() else None
    else:
        # Qualification is coordinator-owned: refuse unless it verifies the
        # worker has no active operations before its service is stopped.
        run(acceptance+['--preflight'])
        dropin=SYSTEMD/'openhands-worker.service.d/zz-hyprchat-release.conf';dropin.parent.mkdir(parents=True,exist_ok=True)
        state.update(dropin=str(dropin),old_dropin=dropin.read_text() if dropin.exists() else None)
    state['state']='activating';journal=root/'.worker-release-journal.json';atomic_json(journal,state)
    try:
        if state['kind']=='documents':
            temp=root/'.next-current';temp.unlink(missing_ok=True);temp.symlink_to(release);os.replace(temp,root/'current')
        else:
            Path(state['dropin']).write_text('[Service]\nWorkingDirectory='+str(release)+'\nExecStart=\nExecStart='+shlex.join([str(release/'venv/bin/python'),str(release/'openhands_worker.py')])+'\n')
            run(['systemctl','daemon-reload']);run(['systemctl','restart','openhands-worker'])
            if run(['systemctl','is-active','openhands-worker'])!='active':raise RuntimeError('Staged worker did not start')
        run(acceptance)
        state['state']='committed';atomic_json(journal,state);atomic_json(release/'release.json',state);journal.unlink()
    except BaseException:
        recover(root);raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','activate','recover']);parser.add_argument('--root',required=True);parser.add_argument('--source');parser.add_argument('--python');parser.add_argument('--kind',choices=['documents','worker']);parser.add_argument('--release');parser.add_argument('--acceptance',nargs='+')
    args=parser.parse_args()
    if args.action=='prepare':print(prepare(args.root,args.source,args.python,args.kind))
    elif args.action=='activate':activate(args.release,args.acceptance)
    else:recover(args.root)
