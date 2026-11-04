"""Codebox-compatible execution API. All project commands go through the broker."""
from contextvars import ContextVar
import asyncio
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import time
import uuid

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from hephaestus_languages import LANGUAGES
from hephaestus_scope import key, verify, scope

ROOT = Path(os.environ.get('HEPHAESTUS_ROOT', '/srv/hephaestus'))
_scope = ContextVar('execution_scope')
_processes = {}
_environment_locks = {}
_workspace_locks = {}
app = FastAPI(title='Hephaestus execution service', version='1.0')


@app.middleware('http')
async def authenticated_scope(request: Request, call_next):
    if request.url.path == '/health': return await call_next(request)
    try: metadata = verify(request.headers, os.environ.get('HEPHAESTUS_TOKEN', ''))
    except (ValueError, TypeError): return JSONResponse({'detail':'Authenticated execution scope is required'},status_code=401)
    token = _scope.set(metadata)
    try:
        with scope(**{k:v for k,v in metadata.items() if k not in {'issued','nonce'}}):
            return await call_next(request)
    finally: _scope.reset(token)


def workspace():
    from hephaestus_storage import ensure_quota, require_space
    if _scope.get().get('workspace_available')=='false':
        raise HTTPException(409,'This workspace was discarded when Codebox retired. Start from a retained artifact or upload its source.')
    identity = key(_scope.get())
    path = ROOT/'projects'/identity
    if not path.exists():
        require_space(ROOT)
        ensure_quota(ROOT, path)
    for name in ('root','tmp'): (path/name).mkdir(exist_ok=True)
    return path


def virtual_path(path, directory):
    value = PurePosixPath(path)
    if not value.is_absolute() or '..' in value.parts: raise HTTPException(422, 'Expected a scoped /root or /tmp path')
    if Path(path).is_relative_to(directory):
        if not Path(path).resolve().is_relative_to(directory.resolve()): raise HTTPException(422,'Path escapes its workspace')
        return Path(path)
    if value.parts[1:2] not in [('root',), ('tmp',)]: raise HTTPException(422, 'Path must be under /root or /tmp')
    target = directory.joinpath(*value.parts[1:])
    if not target.resolve().is_relative_to(directory.resolve()): raise HTTPException(422, 'Path escapes its workspace')
    return target


async def execute(command, *, cwd='/root', timeout=60, stdin=None, job_id=None, request=None):
    lock=_workspace_locks.setdefault(key(_scope.get()),asyncio.Lock())
    async with lock:
        return await _execute_scoped(command,cwd=cwd,timeout=timeout,stdin=stdin,job_id=job_id,request=request)


async def _execute_scoped(command, *, cwd='/root', timeout=60, stdin=None, job_id=None, request=None):
    from coder_sandbox import Sandbox
    directory = workspace()
    actual_cwd = virtual_path(cwd, directory)
    if not actual_cwd.is_dir(): raise HTTPException(422, 'Working directory does not exist')
    timeout = max(1, min(float(timeout or 60), 900))
    identity = job_id or uuid.uuid4().hex
    runtime = ROOT/'runs'/identity
    from hephaestus_storage import ensure_quota, require_space
    require_space(ROOT); ensure_quota(ROOT, runtime, limit=2*1024**3)
    if stdin is not None:
        (runtime/'stdin').write_text(str(stdin))
        command = f'exec bash -c {shlex.quote(command)} < {shlex.quote(str(runtime/"stdin"))}'
    env = {'PATH':'/root/venv/bin:/usr/local/bin:/usr/bin:/bin', 'LANG':'C.UTF-8',
           'JAVA_HOME':'/usr/lib/jvm/default-java',
           'DAEDALUS_RUNTIME_SCOPE':key(_scope.get())}
    started = time.monotonic()
    from coder_engine_cache import lease
    with lease(directory), lease(runtime), Sandbox(['bash','-c',command], cwd=actual_cwd, writable=[directory,runtime],
                 aliases=[(directory/'root','/root'),(directory/'tmp','/tmp')],
                 environment=env, runtime=runtime, timeout=timeout) as box:
        process = await asyncio.create_subprocess_exec(*box.args, env=box.env, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        _processes[(key(_scope.get()),identity)] = process
        chunks = bytearray(); truncated = False
        async def collect():
            nonlocal truncated
            while data := await process.stdout.read(32768):
                remaining = 256*1024-len(chunks)
                chunks.extend(data[:max(0,remaining)])
                truncated |= len(data)>remaining
        reader = asyncio.create_task(collect())
        try:
            while process.returncode is None:
                if request is not None and await request.is_disconnected(): raise asyncio.CancelledError()
                if time.monotonic()-started > timeout+5: raise TimeoutError('Execution deadline exceeded')
                await asyncio.sleep(.1)
            await reader
        finally:
            if process.returncode is None:
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill(); await process.wait()
            reader.cancel(); await asyncio.gather(reader,return_exceptions=True)
            _processes.pop((key(_scope.get()),identity),None)
    import shutil
    shutil.rmtree(runtime)  # The broker has acknowledged that every writer stopped.
    output = chunks.decode(errors='replace')
    if truncated: output += '\n[Output truncated at 256 KiB]'
    return {'stdout':output,'stderr':'','exit_code':process.returncode,'success':process.returncode==0,
            'job_id':identity,'execution_time':time.monotonic()-started}


@app.get('/health')
def health():
    return {'status':'ok' if Path('/run/hephaestus/broker.sock').exists() else 'unavailable',
            'runtime':'hephaestus','version':'1.0','scoped_execution':True}


@app.get('/languages')
def languages():
    manifest = json.loads(Path('/etc/hephaestus/runtime.json').read_text())
    qualified = {name:row for name,row in manifest.get('languages',{}).items()
                 if row.get('image')==manifest['profiles']['languages']['image']}
    return {name:{'extension':row['ext'],'compiled':bool(row.get('compile')),
                  'available':bool(qualified.get(name,{}).get('passed')),
                  'profile':qualified.get(name,{}).get('profile'),
                  'reason': '' if qualified.get(name,{}).get('passed') else 'Runtime profile has not passed its language check'}
            for name,row in LANGUAGES.items() if len(name)>2 or name in {'c','r','v'}}


@app.get('/status')
def status():
    import shutil
    usage = shutil.disk_usage(ROOT)
    return {'runtime':'hephaestus','disk':{'total':usage.total,'used':usage.used,'free':usage.free},
            'running_jobs':sum(owner==key(_scope.get()) for owner,_ in _processes), 'languages':languages()}


@app.post('/command')
async def command(request: Request, body: dict):
    if not isinstance(body.get('command'),str): raise HTTPException(422,'Command is required')
    return await execute(body['command'],cwd=body.get('cwd') or '/root',timeout=body.get('timeout',30),request=request)


@app.post('/environment')
async def environment(body: dict):
    kind=body.get('kind')
    if kind not in {'python','data'}: raise HTTPException(422,'Unknown environment kind')
    identity=key(_scope.get())
    lock=_environment_locks.setdefault(identity,asyncio.Lock())
    async with lock:
        check='import pandas, openpyxl, sympy' if kind=='data' else 'import sys'
        result=await execute('(test -x /root/venv/bin/python3 || python3 -m venv --system-site-packages /root/venv) && '+
                             '/root/venv/bin/python3 -c '+shlex.quote(check),timeout=60)
    return {'ready':result['success'],'scope':identity,'python':'/root/venv/bin/python3','error':result['stdout'] if not result['success'] else ''}


@app.post('/documents/{action}')
async def document(action: str, body: dict):
    if action not in {'start','status','cancel','chunk','health'}: raise HTTPException(422,'Invalid document operation')
    args=body.get('args',[])
    if not isinstance(args,list) or len(args)>4: raise HTTPException(422,'Invalid document arguments')
    if action!='health' and (not args or not re.fullmatch(r'doc-[a-f0-9]{32}',str(args[0]))):
        raise HTTPException(422,'Invalid document identity')
    expected={'health':0,'start':1,'status':1,'cancel':1,'chunk':3}[action]
    if len(args)!=expected: raise HTTPException(422,'Invalid document argument count')
    release=Path('/etc/hephaestus/current-release').read_text().strip()
    manifest=json.loads((Path(release)/'release.json').read_text())
    jobs=workspace()/'root/hyprchat-documents/jobs'; jobs.mkdir(parents=True,exist_ok=True)
    env={**os.environ,'HEPHAESTUS_DOCUMENT_ROOT':str(jobs),
         'HEPHAESTUS_EXECUTION_IDENTITY':json.dumps({**_scope.get(),'operation':args[0] if args else 'health'})}
    process=await asyncio.create_subprocess_exec(manifest['environments']['documents']+'/bin/python',
        str(Path(release)/'backend/document_worker.py'),action,*map(str,args),env=env,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try: stdout,stderr=await asyncio.wait_for(process.communicate(),30)
    except asyncio.TimeoutError:
        process.kill();await process.wait();raise HTTPException(504,'Document controller timed out')
    if process.returncode: raise HTTPException(502,'Document controller failed: '+stderr.decode(errors='replace')[-2000:])
    return json.loads(stdout)


@app.post('/execute')
async def code(request: Request, body: dict):
    language = str(body.get('language','python')).lower()
    cfg = LANGUAGES.get(language)
    if not cfg: raise HTTPException(422,'Unknown language')
    manifest = json.loads(Path('/etc/hephaestus/runtime.json').read_text())
    qualification=manifest.get('languages',{}).get(language,{})
    if not qualification.get('passed') or qualification.get('image')!=manifest['profiles']['languages']['image']:
        raise HTTPException(503,f'{language} runtime is not qualified yet')
    identity = uuid.uuid4().hex
    directory = workspace(); virtual = '/tmp/codebox_workspace/'+identity
    folder = virtual_path(virtual,directory); folder.mkdir(parents=True)
    name = body.get('filename') or ('Main.java' if language=='java' else 'main'+cfg['ext'])
    if PurePosixPath(name).name != name or not re.fullmatch(r'[\w.-]{1,120}',name): raise HTTPException(422,'Invalid source filename')
    (folder/name).write_text(str(body.get('code','')))
    args = {'file':shlex.quote(virtual+'/'+name),'filename':shlex.quote(name),'dir':shlex.quote(virtual),
            'output':shlex.quote(virtual+'/program'),'stem':shlex.quote(Path(name).stem),'classname':shlex.quote(Path(name).stem)}
    line = ((cfg['compile'].format(**args)+' && ') if cfg.get('compile') else '')+cfg['run'].format(**args)
    result = await execute(line,cwd=virtual,timeout=body.get('timeout',60),stdin=body.get('stdin'),job_id=identity,request=request)
    return {**result,'language':language,'compile_output':None}


@app.post('/execute/multi')
async def multi(request: Request, body: dict):
    directory = workspace(); identity = uuid.uuid4().hex; virtual = '/tmp/codebox_workspace/'+identity
    folder = virtual_path(virtual,directory); folder.mkdir(parents=True)
    for name,text in body.get('files',{}).items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts: raise HTTPException(422,'Invalid relative source path')
        target = folder/name; target.parent.mkdir(parents=True,exist_ok=True); target.write_text(str(text))
    main = str(body.get('main_file',''))
    if not main or PurePosixPath(main).is_absolute() or '..' in PurePosixPath(main).parts: raise HTTPException(422,'Invalid main file')
    language = body.get('language','python'); cfg=LANGUAGES.get(language)
    if not cfg: raise HTTPException(422,'Unknown language')
    args = {'file':shlex.quote(main),'filename':shlex.quote(main),'dir':'.','output':'./program',
            'stem':shlex.quote(Path(main).stem),'classname':shlex.quote(Path(main).stem)}
    build=body.get('build_command') or (cfg['compile'].format(**args) if cfg.get('compile') else '')
    run=body.get('run_command') or cfg['run'].format(**args)
    result=await execute((build+' && ' if build else '')+run,cwd=virtual,timeout=body.get('timeout',60),stdin=body.get('stdin'),request=request)
    return {**result,'language':language,'compile_output':None}


@app.post('/project/build')
async def build(request: Request, body: dict):
    build=body.get('build_command',''); run=body.get('run_command','true')
    result=await execute((build+' && ' if build else '')+run,cwd=body.get('directory','/root/project'),
                         timeout=body.get('timeout',60),stdin=body.get('stdin'),request=request)
    return {**result,'build_output':result['stdout'] if build else None}


@app.post('/upload-chunk')
async def upload(file: UploadFile=File(...), path: str=Form(...), truncate: bool=Form(False)):
    lock=_workspace_locks.setdefault(key(_scope.get()),asyncio.Lock())
    async with lock:
        directory=workspace();target=virtual_path(path,directory)
        parts=target.relative_to(directory).parts
        descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                try: os.mkdir(part,0o700,dir_fd=descriptor)
                except FileExistsError: pass
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
                os.close(descriptor);descriptor=child
            output_fd=os.open(parts[-1],os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW|(os.O_TRUNC if truncate else os.O_APPEND),0o600,dir_fd=descriptor)
        finally: os.close(descriptor)
        written=0
        with os.fdopen(output_fd,'wb') as output:
            while chunk:=await file.read(1024*1024):
                written+=len(chunk)
                if written>16*1024*1024: raise HTTPException(413,'Upload chunks are limited to 16 MiB')
                output.write(chunk)
            output.flush();size=os.fstat(output.fileno()).st_size
        return {'ok':True,'bytes_written':written,'size_now':size}


@app.post('/kill/{identity}')
async def kill(identity: str):
    process=_processes.get((key(_scope.get()),identity))
    if process and process.returncode is None:
        os.killpg(process.pid,signal.SIGTERM)
        await process.wait()
        return {'killed':True,'job_id':identity}
    return {'killed':False,'reason':'No active job in this scope'}


@app.post('/cleanup')
async def cleanup():
    identities=[identity for owner,identity in _processes if owner==key(_scope.get())]
    for identity in identities: await kill(identity)
    return {'killed_jobs':len(identities),'cleaned_dirs':0}


@app.post('/file/export')
async def export_file(body: dict):
    """Snapshot a file into controller scratch so output truncation cannot corrupt downloads."""
    import hashlib
    import shutil
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask
    from hephaestus_storage import require_space, ensure_quota
    maximum=min(250*1024**2,max(1,int(body.get('max_bytes',250*1024**2))))
    directory=workspace();target=virtual_path(str(body.get('path','')),directory)
    staging=ROOT/'runs'/('export-'+uuid.uuid4().hex)
    require_space(ROOT,maximum);ensure_quota(ROOT,staging,2*1024**3)
    descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    parts=target.relative_to(directory).parts
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            os.close(descriptor);descriptor=child
        source_fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=descriptor)
    except (OSError,IndexError):
        shutil.rmtree(staging);raise HTTPException(404,'File is unavailable in this workspace')
    finally: os.close(descriptor)
    destination=staging/'download';hasher=hashlib.sha256();size=0
    try:
        import stat
        with os.fdopen(source_fd,'rb') as source, destination.open('wb') as output:
            before=os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode): raise HTTPException(422,'Expected a regular file')
            if before.st_size>maximum: raise HTTPException(413,'File exceeds the download size limit')
            while chunk:=source.read(1024**2):
                size+=len(chunk)
                if size>maximum: raise HTTPException(413,'File grew beyond the download size limit')
                hasher.update(chunk);output.write(chunk)
            after=os.fstat(source.fileno())
            if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise HTTPException(409,'File changed while exporting; retry after execution stops')
        return FileResponse(destination,media_type='application/octet-stream',headers={'X-Content-SHA256':hasher.hexdigest()},
                            background=BackgroundTask(shutil.rmtree,staging))
    except BaseException:
        shutil.rmtree(staging);raise
