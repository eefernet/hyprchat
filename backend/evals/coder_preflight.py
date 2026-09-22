"""Validate evaluation infrastructure before spending local inference calls."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import uuid

import httpx


def run_preflight(args, root):
    from context_policy import DEFAULTS
    reserve = DEFAULTS['daedalus_min_free_mb'] * 1024 * 1024
    if shutil.disk_usage(root).free < reserve:
        raise RuntimeError('Evaluation state has insufficient free disk space')
    mounts = Path('/proc/mounts')
    if mounts.exists():
        candidates = [line.split() for line in mounts.read_text().splitlines()
            if Path(root).resolve().is_relative_to(Path(line.split()[1]))]
        mount = max(candidates, key=lambda row: len(row[1]))
        if mount[2] in {'tmpfs','ramfs'}: raise RuntimeError('Evaluation state must be disk-backed')
    dependencies = {}
    for python in {sys.executable, args.api_python}:
        check = subprocess.run([python, '-c', 'import fastapi,aiosqlite,httpx,pytest,playwright,requests; print("ok")'], capture_output=True, text=True)
        if check.returncode: raise RuntimeError(f'Evaluation dependencies unavailable in {python}: {check.stderr}')
        dependencies[python] = 'ok'
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox']);browser.close()
    for name in ('git','node','bash'):
        if not shutil.which(name): raise RuntimeError(f'Missing evaluation executable: {name}')
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as archive:
        payload=b'Evaluation archive transfer preflight\n'
        info=tarfile.TarInfo('README.md');info.size=len(payload);archive.addfile(info,io.BytesIO(payload))
    content=data.getvalue();sha=hashlib.sha256(content).hexdigest()
    job='cw3-'+uuid.uuid4().hex
    workers={}
    for role,url,expected in [('candidate',args.worker_url,args.projects_root),
        ('legacy',args.legacy_worker_url,'/root/projects'),('restore',args.restore_worker_url,'/root/projects')]:
        with httpx.Client(base_url=url, timeout=30) as client:
            response=client.get('/health');response.raise_for_status();health=response.json()
            if health.get('projects_root') != str(Path(expected).resolve()):
                raise RuntimeError(f'{role} worker project root must be {expected}; got {health.get("projects_root")}')
            state=Path(health['state_root'])
            if not state.is_relative_to(root.parent.resolve()):
                raise RuntimeError(f'{role} must use isolated worker state under the evaluation root')
            state.mkdir(parents=True,exist_ok=True)
            if shutil.disk_usage(state).free < reserve: raise RuntimeError(f'{role} worker disk is full')
            response=client.put(f'/jobs/{job}/source/{sha}',content=content);response.raise_for_status()
            response=client.get(f'/jobs/{job}/source/{sha}');response.raise_for_status()
            if response.json().get('sha256')!=sha: raise RuntimeError('Archive transfer checksum mismatch')
            workers[role]={**health,'archive_sha256':sha,'free_bytes':shutil.disk_usage(state).free}
    if len({w['state_root'] for w in workers.values()})!=3:
        raise RuntimeError('Candidate, legacy and restoration worker state must be separate')
    report={'dependencies':dependencies,'browser':'passed','workers':workers,'disk_backed':True}
    (root/'preflight.json').write_text(json.dumps(report,indent=2))
    return report
