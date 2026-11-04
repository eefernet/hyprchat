"""Authenticated, owner-scoped execution metadata shared by internal callers."""
from contextlib import contextmanager
from contextvars import ContextVar
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from urllib.parse import urlsplit

_context = ContextVar('hephaestus_scope', default={})


def current():
    return dict(_context.get())


@contextmanager
def scope(**values):
    token = _context.set({**_context.get(), **{k: str(v) for k, v in values.items() if v}})
    try: yield
    finally: _context.reset(token)


def headers(owner, *, secret=None, **values):
    secret = secret if secret is not None else os.environ.get('HEPHAESTUS_TOKEN', '')
    if not secret: return {}
    if not owner: raise ValueError('Execution requires an authenticated owner')
    metadata = {**_context.get(), **{k:str(v) for k,v in values.items() if v}, 'owner':str(owner),
                'issued': int(time.time()), 'nonce': uuid.uuid4().hex}
    encoded = base64.urlsafe_b64encode(json.dumps(metadata, sort_keys=True).encode()).decode()
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return {'X-Hephaestus-Scope': encoded, 'X-Hephaestus-Signature': signature}


def verify(values, secret, now=None):
    encoded = values.get('x-hephaestus-scope', '')
    signature = values.get('x-hephaestus-signature', '')
    if not secret or len(encoded) > 4096 or not hmac.compare_digest(
            hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest(), signature):
        raise ValueError('Authenticated execution scope is required')
    metadata = json.loads(base64.urlsafe_b64decode(encoded))
    if not isinstance(metadata, dict) or not metadata.get('owner') or abs((now or time.time())-metadata.get('issued',0)) > 120:
        raise ValueError('Invalid or expired execution scope')
    return metadata


def key(metadata):
    # Generic owner workspace is deliberate; project/conversation work gets its own namespace.
    identity = [metadata['owner'], metadata.get('project') or metadata.get('conversation') or 'personal']
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


async def conversation_metadata(conversation):
    """Resolve the selected project through the authenticated database owner."""
    result={'conversation':conversation}
    if os.environ.get('HEPHAESTUS_TOKEN') and conversation:
        import database as db
        project=await db.get_coding_project_by_conv(conversation)
        if project:
            result.update(project=project.get('openhands_project_id') or project['id'])
            if retired_project(project): result['workspace_available']='false'
    return result


def retired_project(project):
    from pathlib import Path
    import config
    path=Path(config.DATA_DIR)/'retired-worker-workspaces.json'
    if not os.environ.get('HEPHAESTUS_TOKEN') or not path.exists(): return False
    return any(row['id']==project['id'] and row['user_id']==project['user_id'] for row in json.loads(path.read_text())['projects'])


def workspace_root():
    from pathlib import Path
    metadata=current()
    if not metadata.get('owner'): raise ValueError('Authenticated execution scope is missing')
    if metadata.get('workspace_available')=='false': raise ValueError('This workspace was discarded when Codebox retired. Start from a retained artifact or upload its source.')
    root=Path(os.environ.get('HEPHAESTUS_ROOT','/srv/hephaestus'))
    directory=root/'projects'/key(metadata)
    from hephaestus_storage import require_space, ensure_quota
    require_space(root); ensure_quota(root,directory)
    (directory/'root').mkdir(exist_ok=True)
    (directory/'tmp').mkdir(exist_ok=True)
    return directory


def resolve_workspace_path(value):
    from pathlib import Path
    directory=workspace_root()
    path=Path(value)
    if path.parts[1:2] in [('root',),('tmp',)]: path=directory.joinpath(*path.parts[1:])
    if not path.is_absolute() or not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError('Project path is outside the authenticated workspace')
    return path


def profile_probe(profile, command):
    """A controller health probe, with no project mounts or model access."""
    from pathlib import Path
    import subprocess
    import tempfile
    from coder_sandbox import Sandbox
    root=Path(os.environ.get('HEPHAESTUS_ROOT','/srv/hephaestus'))/'runs'
    with tempfile.TemporaryDirectory(prefix='health-',dir=root) as directory, scope(owner='controller-health'):
        with Sandbox(command,cwd=directory,writable=[directory],runtime_profile=profile,timeout=25) as box:
            return subprocess.run(box.args,env=box.env,capture_output=True,text=True,timeout=35)


async def authorize_request(request):
    if not os.environ.get('HEPHAESTUS_TOKEN'): return
    import config
    import database as db
    destinations = {urlsplit(value).netloc for value in (config.CODEBOX_URL, config.OPENHANDS_URL,
                    getattr(config, 'AIDER_WORKER_URL', ''), os.environ.get('HEPHAESTUS_MODERN_URL',''),
                    os.environ.get('DAEDALUS_ENGINE_URL','')) if value}
    if request.url.netloc.decode() not in destinations: return
    if 'x-hephaestus-signature' not in request.headers:
        request.headers.update(headers(db.current_user_id()))


def claim_job(job_id, owner, *, create=False):
    """Durable owner binding for every worker job, including uploads and previews."""
    import re
    from pathlib import Path
    if not re.fullmatch(r'cw3-[a-f0-9]+',job_id): raise ValueError('Invalid job identity')
    root=Path(os.environ['DAEDALUS_STATE_DIR'])
    directory=root/'job-owners';directory.mkdir(exist_ok=True)
    path=directory/job_id
    if not path.exists():
        # Existing operations retain the owner stored at dispatch.
        import sqlite3
        database=root/'operations.sqlite3'
        recorded=None
        if database.exists():
            with sqlite3.connect(database) as db:
                row=db.execute('SELECT payload FROM operations WHERE job_id=? LIMIT 1',(job_id,)).fetchone()
            if row: recorded=json.loads(row[0]).get('_execution_scope',{}).get('owner')
        if recorded is None and not create: raise ValueError('Job not found')
        selected=recorded or owner
        try:
            with path.open('x') as output: output.write(selected)
        except FileExistsError: pass
    if path.read_text()!=owner: raise ValueError('Job belongs to another owner')


def install_worker_auth(app):
    if os.environ.get('HEPHAESTUS_EXECUTOR')!='docker': return
    from fastapi import Request
    from fastapi.responses import JSONResponse
    @app.middleware('http')
    async def authenticate(request: Request, call_next):
        if request.url.path in {'/health','/aider/health'}: return await call_next(request)
        try: metadata=verify(request.headers,os.environ.get('HEPHAESTUS_TOKEN',''))
        except (ValueError,TypeError): return JSONResponse({'detail':'Authenticated execution scope is required'},status_code=401)
        parts=request.url.path.strip('/').split('/')
        if parts[0]=='jobs' and len(parts)>1:
            try: claim_job(parts[1],metadata['owner'],create=request.method in {'POST','PUT'})
            except (ValueError,OSError): return JSONResponse({'detail':'Job is unavailable in this owner scope'},status_code=404)
        with scope(**{k:v for k,v in metadata.items() if k in {'owner','conversation','project','workspace_available'}}):
            return await call_next(request)


async def download_bytes(http, path, maximum):
    import config
    result=bytearray()
    async with http.stream('POST',config.CODEBOX_URL+'/file/export',json={'path':path,'max_bytes':maximum},timeout=120) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            result.extend(chunk)
            if len(result)>maximum: raise ValueError('File exceeds the download size limit')
        if hashlib.sha256(result).hexdigest()!=response.headers.get('X-Content-SHA256'):
            raise ValueError('File export checksum mismatch')
    return bytes(result)
