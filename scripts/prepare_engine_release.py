#!/usr/bin/env python3
"""Prepare an isolated Linux worker release; never activate or replace a service."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--python', default='python3.13')
    parser.add_argument('--port', type=int, default=8587)
    parser.add_argument('--bind', default='127.0.0.1')
    args = parser.parse_args()
    if sys.platform != 'linux': parser.error('Prepare this release on the Linux worker host')
    if not 1024 <= args.port <= 65535: parser.error('Choose an unprivileged port')
    root = args.destination.resolve()
    if root.exists(): parser.error('Destination already exists; use a new release directory')
    if any(c.isspace() for c in str(root)) or any(c in str(root) for c in '%\n\r'):
        parser.error('Use a release path without whitespace or systemd expansion characters')
    backend = args.source_root.resolve()/'backend'
    lock = backend/'engine-requirements.lock.txt'
    if not lock.is_file(): parser.error('Missing tested dependency lock')
    root.mkdir(parents=True)
    def excluded(directory, names):
        return [name for name in names if name in {'__pycache__','.venv','data','logs','chroma_db'} or
                ((Path(directory)/name).is_file() and Path(name).suffix not in {'.py','.txt','.json'})]
    shutil.copytree(backend, root/'backend', ignore=excluded)
    grader = args.source_root.resolve()/'scripts/qualify_engine_report.py'
    if grader.is_file():
        (root/'scripts').mkdir(exist_ok=True)
        shutil.copy2(grader, root/'scripts/qualify_engine_report.py')
    subprocess.run([args.python, '-m', 'venv', str(root/'venv')], check=True)
    python = str(root/'venv/bin/python')
    subprocess.run([python, '-m', 'pip', 'install', '-r', str(root/'backend/engine-requirements.lock.txt')], check=True)
    subprocess.run([python, '-m', 'pip', 'check'], check=True)
    subprocess.run([python, '-m', 'playwright', 'install', 'chromium'], check=True,
                   env={**os.environ, 'PLAYWRIGHT_SKIP_BROWSER_GC':'1'})
    env = {**os.environ, 'DAEDALUS_STATE_DIR':str(root/'state'),
           'DAEDALUS_PROJECTS_DIR':str(root/'projects')}
    subprocess.run([python, '-c', 'import coder_engine_worker; import openhands.sdk; import openhands.tools'],
                   cwd=root/'backend', env=env, check=True)
    freeze = subprocess.check_output([python, '-m', 'pip', 'freeze'], text=True)
    (root/'installed-requirements.txt').write_text(freeze)
    files = {str(p.relative_to(root/'backend')):hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted((root/'backend').rglob('*')) if p.is_file() and p.suffix in {'.py','.txt','.json'}}
    (root/'release.json').write_text(json.dumps({'engine':'openhands159', 'sdk':'1.59.0',
        'python':subprocess.check_output([python,'--version'],text=True).strip(),
        'dependency_lock_sha256':hashlib.sha256(lock.read_bytes()).hexdigest(), 'files':files}, indent=2)+'\n')
    (root/'worker.service').write_text(f'''[Unit]
Description=Daedalus OpenHands 1.59 worker
After=network-online.target

[Service]
Type=simple
WorkingDirectory={root}/backend
Environment=DAEDALUS_STATE_DIR={root}/state
Environment=DAEDALUS_PROJECTS_DIR={root}/projects
Environment=DAEDALUS_TOKENIZER_DIR={root}/tokenizer
Environment=DAEDALUS_CGROUP_LIMITS=1
Slice=daedalus-candidate.slice
Delegate=memory pids
DelegateSubgroup=control
ExecStart={python} -m uvicorn coder_engine_worker:app --host {args.bind} --port {args.port}
Restart=on-failure
KillMode=process
TimeoutStopSec=30
MemoryHigh=3G
MemoryMax=4G
TasksMax=512
OOMPolicy=continue

[Install]
WantedBy=multi-user.target
''')
    (root/'daedalus-candidate.slice').write_text('[Unit]\nDescription=Daedalus candidate aggregate limits\n[Slice]\nMemoryHigh=4608M\nMemoryMax=5G\nTasksMax=768\n')
    print(f'Prepared {root}. Prepare the pinned tokenizer, install the supplied slice, then qualify before activation.')


if __name__ == '__main__': main()
