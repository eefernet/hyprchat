"""Revision-pinned, expiring GUI previews outside the worker's trusted stores."""
import asyncio
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tarfile
import time
import threading

import psutil
from fastapi import Body, HTTPException, WebSocket, WebSocketDisconnect


def read_session(root, identity, job_id):
    if not re.fullmatch(r'[a-f0-9]{32}', identity): raise LookupError('Preview not found')
    path = root/'previews'/identity/'session.json'
    try: state = json.loads(path.read_text())
    except (OSError, ValueError): raise LookupError('Preview not found') from None
    if state['job_id'] != job_id: raise LookupError('Preview not found')
    return state


def save(path, state):
    temporary = path.with_suffix('.tmp'); temporary.write_text(json.dumps(state)); temporary.replace(path)


def update(path, **changes):
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()); state.update(changes); save(path, state)
        return state


def expired(state):
    return state.get('lifecycle','temporary') != 'pinned' and state.get('expires_at',0) <= time.time()


def launch_session(root, state):
    directory = Path(root)/'previews'/state['id']
    path = directory/'session.json'
    state = update(path, status='starting', desired_state='running', token=secrets.token_urlsafe(32),
                   generation=state.get('generation',0)+1, error='')
    with (directory/'supervisor.log').open('ab') as log:
        runtime=state.get('runtime_controller',{})
        process = subprocess.Popen([runtime.get('python',sys.executable), runtime.get('runner',str(Path(__file__).resolve())), '--run', str(root), state['id']],
            env={**os.environ,**({'PLAYWRIGHT_BROWSERS_PATH':runtime['browsers']} if runtime.get('browsers') else {})},
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return update(path,pid=process.pid,process_started=psutil.Process(process.pid).create_time(),
                  process_identity=process_identity(process.pid))


def process_identity(pid):
    """Linux start ticks survive wall-clock corrections and reject PID reuse."""
    if sys.platform != 'linux': return None
    # The command name in parentheses may itself contain spaces or parentheses.
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'start_ticks': int(fields[19])}


def alive(state):
    try:
        process = psutil.Process(state['pid'])
        identity = state.get('process_identity')
        same = (process_identity(state['pid']) == identity if identity else
                abs(process.create_time()-state['process_started']) < .01)
        return (same and process.is_running()
                and process.status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD})
    except (psutil.Error, KeyError, OSError, ValueError, IndexError): return False


def reserves_slot(state):
    # Closed temporary sessions remain historical records, not live applications.
    # A pinned session waiting for recovery still owns its reserved slot.
    return state.get('desired_state') == 'running' and (
        alive(state) or (state.get('lifecycle') == 'pinned' and state.get('status') != 'failed'))


def stop(state):
    if not alive(state): return
    process = psutil.Process(state['pid'])
    if os.environ.get('HEPHAESTUS_EXECUTOR')=='docker':
        process.terminate()
        try: process.wait(timeout=65)
        except psutil.TimeoutExpired: raise RuntimeError('Application stop is unconfirmed; its data has been retained')
        return
    descendants = process.children(recursive=True)
    for child in reversed(descendants):
        try: child.terminate()
        except psutil.Error: pass
    process.terminate()
    _, remaining = psutil.wait_procs([*descendants, process], timeout=3)
    for child in remaining:
        try: child.kill()
        except psutil.Error: pass


def install_routes(app, store):
    @app.get('/jobs/{job_id}/previews')
    def listing(job_id: str):
        result=[]
        for path in (store.root/'previews').glob('*/session.json'):
            try:
                state=json.loads(path.read_text())
                if state['job_id']==job_id and state.get('status')!='deleted': result.append(state)
            except (OSError,ValueError): continue
        return sorted(result,key=lambda s:s.get('created_at',0),reverse=True)

    @app.post('/jobs/{job_id}/previews', status_code=202)
    def create(job_id: str, body: dict = Body(...)):
        # Count and reserve under one process-shared lock. Concurrent requests
        # must not bypass the session limit or launch two previews for a job.
        with (store.root/'preview-create.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return create_locked(job_id, body)

    def create_locked(job_id, body):
        revision = body.get('revision', '')
        if not re.fullmatch(r'[a-f0-9]{40,64}', revision): raise HTTPException(422, 'Invalid revision')
        if body.get('kind') not in {'web', 'desktop'} or not isinstance(body.get('command'), str) or not body['command'].strip():
            raise HTTPException(422, 'Choose an application kind and launch command')
        with store.connect() as db:
            row = db.execute("SELECT id FROM operations WHERE job_id=? AND kind IN ('inspect','code','check','package') ORDER BY started DESC LIMIT 1", (job_id,)).fetchone()
        if not row: raise HTTPException(404, 'Project not found')
        operation = store.get(row['id'])
        if operation['payload'].get('engine') != 'openhands159': raise HTTPException(409, 'This engine has no GUI session support')
        # Bound resource use, including sessions surviving a worker restart.
        previews = store.root/'previews'; previews.mkdir(exist_ok=True)
        for path in previews.glob('*/session.json'):
            try: previous = json.loads(path.read_text())
            except (OSError, ValueError): continue
            if expired(previous): stop(previous)
            elif previous['job_id'] == job_id and alive(previous):
                if previous.get('lifecycle')=='pinned': raise HTTPException(409,'Stop or delete the pinned application before replacing it')
                stop(previous)
        active = 0
        for path in previews.glob('*/session.json'):
            try: active += alive(json.loads(path.read_text()))
            except (OSError, ValueError): continue
        if active >= 3: raise HTTPException(409, 'Three previews are already running; close one first')
        identity = secrets.token_hex(16); directory = previews/identity; directory.mkdir(mode=0o700)
        from coder_worker_runtime import operation_repo
        try: operation_repo(store, operation).archive(revision, directory/'source.tar.gz')
        except Exception:
            raise HTTPException(409, 'The requested revision is unavailable') from None
        request = {'action': 'launch', 'kind': body['kind'], 'command': body['command'][:12000],
                   'url': body.get('url', 'http://127.0.0.1:8000'), 'toolkit': body.get('toolkit', '')}
        (directory/'launch.json').write_text(json.dumps(request))
        state = {'id': identity, 'job_id': job_id, 'revision': revision, 'token': secrets.token_urlsafe(32),
                 'status': 'starting', 'expires_at': time.time()+900, 'operation_id': operation['id'],
                 'lifecycle':'temporary','desired_state':'running','created_at':time.time(),'generation':0}
        if os.environ.get('HEPHAESTUS_EXECUTOR')=='docker':
            state['runtime_controller']={'python':sys.executable,'runner':str(Path(__file__).resolve()),
                                         'browsers':os.environ.get('PLAYWRIGHT_BROWSERS_PATH','')}
            state['data_root']=str(Path(os.environ.get('HEPHAESTUS_ROOT','/srv/hephaestus'))/'applications'/identity)
            runtime_config=json.loads(Path('/etc/hephaestus/runtime.json').read_text())
            profile=os.environ.get('HEPHAESTUS_RUNTIME_PROFILE','core')
            state['runtime_image']=runtime_config['profiles'][profile]['image']
            state['runtime_profile']=profile+'-'+state['runtime_image'].split(':')[1][:20]
        save(directory/'session.json', state)
        return launch_session(store.root,state)

    @app.get('/jobs/{job_id}/previews/{identity}')
    def status(job_id: str, identity: str):
        try: state = read_session(store.root, identity, job_id)
        except LookupError: raise HTTPException(404, 'Preview not found') from None
        if not alive(state) and state['status'] in {'starting', 'running'}: state['status'] = 'closed'
        return state

    @app.delete('/jobs/{job_id}/previews/{identity}')
    def close(job_id: str, identity: str):
        state = status(job_id, identity)
        path=store.root/'previews'/identity/'session.json'
        update(path,desired_state='stopped'); stop(state)
        update(path,status='closed')
        return {'status': 'closed'}

    @app.post('/jobs/{job_id}/previews/{identity}/control')
    def control(job_id: str, identity: str, body: dict=Body(...)):
        if os.environ.get('HEPHAESTUS_EXECUTOR')!='docker': raise HTTPException(409,'Persistent applications require Hephaestus')
        with (store.root/'preview-create.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            state=status(job_id,identity); directory=store.root/'previews'/identity; path=directory/'session.json'
            action=body.get('action')
            if action=='pin':
                if state['status']!='running': raise HTTPException(409,'Launch the application successfully before keeping it running')
                pinned=[]
                for candidate in (store.root/'previews').glob('*/session.json'):
                    item=json.loads(candidate.read_text())
                    if item.get('lifecycle')=='pinned' and item.get('desired_state')=='running' and item['id']!=identity: pinned.append(item)
                if len(pinned)>=2: raise HTTPException(409,'Two applications are already pinned; stop one to reserve room for coding jobs')
                return update(path,lifecycle='pinned',expires_at=None,desired_state='running')
            if action in {'stop','restart','delete'}:
                update(path,desired_state='stopped'); stop(state)
                state=update(path,status='closed')
                if action=='restart':
                    other=[json.loads(p.read_text()) for p in (store.root/'previews').glob('*/session.json') if p!=path]
                    if sum(reserves_slot(item) for item in other)>=3:
                        raise HTTPException(409,'Three application sessions are already active; stop one before restarting')
                    if state.get('lifecycle')=='pinned' and sum(item.get('lifecycle')=='pinned' and item.get('desired_state')=='running' for item in other)>=2:
                        raise HTTPException(409,'Two applications are already pinned; stop one before restarting')
                    if state.get('lifecycle')!='pinned': state=update(path,expires_at=time.time()+900)
                    return launch_session(store.root,state)
                if action=='delete':
                    import shutil
                    # Keep the revision/evidence outside this session untouched.
                    if state.get('data_root'):
                        data=Path(state['data_root']).resolve()
                        expected=Path(os.environ.get('HEPHAESTUS_ROOT','/srv/hephaestus'))/'applications'/identity
                        if data!=expected.resolve(): raise HTTPException(409,'Application data identity changed; deletion refused')
                        shutil.rmtree(data,ignore_errors=True)
                    for name in ('workspace','runtime','repository'):
                        shutil.rmtree(directory/name,ignore_errors=True)
                    return update(path,status='deleted',lifecycle='temporary',expires_at=0)
                return state
            raise HTTPException(422,'Unknown application action')

    @app.get('/jobs/{job_id}/previews/{identity}/logs')
    def logs(job_id: str, identity: str):
        state=status(job_id,identity)
        rows={}
        for name in ('supervisor.log','application-session.log','runtime/application.log','runtime/application.log.1'):
            path=store.root/'previews'/identity/name
            if path.exists():
                with path.open('rb') as source:
                    source.seek(max(0,path.stat().st_size-32768)); rows[name]=source.read(32768).decode(errors='replace')
        return {'revision':state['revision'],'logs':rows}

    if os.environ.get('HEPHAESTUS_EXECUTOR')=='docker':
        def recover():
            while True:
                time.sleep(5)
                with (store.root/'preview-create.lock').open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX)
                    for path in (store.root/'previews').glob('*/session.json'):
                        try:
                            state=json.loads(path.read_text())
                            if state.get('lifecycle')=='pinned' and state.get('desired_state')=='running' and not alive(state):
                                # Startup failure is actionable; do not create a rapid restart loop.
                                if state.get('status')!='failed': launch_session(store.root,state)
                        except (OSError,ValueError,KeyError): continue
        threading.Thread(target=recover,daemon=True,name='pinned-application-recovery').start()

    @app.websocket('/jobs/{job_id}/previews/{identity}/stream')
    async def stream(websocket: WebSocket, job_id: str, identity: str):
        try: state = read_session(store.root, identity, job_id)
        except LookupError:
            await websocket.close(code=4404); return
        if (not hmac.compare_digest(websocket.query_params.get('token',''),state['token']) or
            expired(state) or state['status'] != 'running' or not alive(state)):
            await websocket.close(code=4403); return
        await websocket.accept()
        reader, writer = await asyncio.open_connection('127.0.0.1', state['vnc_port'])
        async def send():
            while chunk := await reader.read(65536): await websocket.send_bytes(chunk)
        async def receive():
            while True:
                chunk = await websocket.receive_bytes()
                if len(chunk) > 1024*1024: raise ValueError('Preview frame too large')
                writer.write(chunk); await writer.drain()
        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            while True:
                done,_=await asyncio.wait(tasks,timeout=1,return_when=asyncio.FIRST_COMPLETED)
                latest=read_session(store.root,identity,job_id)
                if done or expired(latest) or latest.get('desired_state')!='running': break
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close(); await writer.wait_closed()
            try: await websocket.close()
            except (RuntimeError, WebSocketDisconnect): pass


def run(root, identity):
    from coder_sandbox import Sandbox
    from coder_execution_utils import stop_process
    from coder_ui_session import free_port
    from coder_worker_runtime import WorkerStore
    from coder_repository import Repository
    from coder_project_runtime import prepare_application, sandbox_paths
    store = WorkerStore(root); directory = Path(root)/'previews'/identity
    path = directory/'session.json'; state = json.loads(path.read_text())
    if state.get('runtime_profile'):
        os.environ['HEPHAESTUS_RUNTIME_PROFILE'] = state['runtime_profile']
    deadline = time.monotonic()+5
    while 'pid' not in state and time.monotonic()<deadline:
        time.sleep(.05); state = json.loads(path.read_text())
    workspace = Path(state.get('data_root',str(directory)))/'workspace'; workspace.mkdir(parents=True,exist_ok=True)
    runtime = directory/'runtime'; runtime.mkdir(exist_ok=True)
    (runtime/'ready.json').unlink(missing_ok=True)
    process = None
    operation_id = None
    from contextlib import ExitStack
    cache_leases = ExitStack()
    def expired(*args):
        # systemd may signal both the supervisor and its children during shutdown.
        # A second signal must not interrupt the broker's stop acknowledgement.
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        raise TimeoutError('Preview stopped')
    signal.signal(signal.SIGTERM, expired)
    managed=os.environ.get('HEPHAESTUS_EXECUTOR')=='docker'
    if not managed:
        signal.signal(signal.SIGALRM, expired); signal.alarm(900)
    try:
        with tarfile.open(directory/'source.tar.gz') as archive: archive.extractall(workspace, filter='data')
        source = store.get(state['operation_id'])
        if managed:
            os.environ['HEPHAESTUS_EXECUTION_IDENTITY']=json.dumps({**source['payload'].get('_execution_scope',{}),
                'job':state['job_id'],'revision':state['revision'],'operation':'preview-'+identity})
        repository = Repository(workspace, directory/'repository', source['payload']['settings']['daedalus_exclude_dirs']); repository.refresh()
        payload = {**source['payload'], 'seconds_remaining': 900, 'calls_remaining': 0, '_active_started': time.time()}
        operation_id = 'preview-'+identity+'-'+str(state.get('generation',0))
        store.create(operation_id, state['job_id'], 'check', payload)
        store.update(operation_id, status='running', started=time.time())
        from coder_engine_resources import enter_operation
        enter_operation(operation_id)
        from coder_engine_cache import lease, scope
        project_id = payload.get('project_id') or state['job_id']
        cache_leases.enter_context(lease(store.root / 'project-environments' / scope(project_id)))
        env = {'PATH': os.environ.get('PATH','/usr/bin:/bin'),
               'DAEDALUS_ENGINE_BEHAVIOR_VERSION':str(payload.get('engine_behavior_version',0))}
        checks = prepare_application(store, operation_id, repository, project_id, env, runtime)
        if env.get('DAEDALUS_ENVIRONMENT_ROOT'):
            update(path,cache_roots=[env['DAEDALUS_ENVIRONMENT_ROOT']])
        if any(not row.get('passed') for row in checks): raise RuntimeError('Preview preparation or build failed: '+json.dumps(checks)[-6000:])
        port = free_port()
        modules = Path(__file__).parent
        command = ['dbus-run-session','--',sys.executable,str(modules/'coder_ui_session.py'),
                   '--directory',str(runtime), '--vnc-port',str(port),'--launch',str(directory/'launch.json'),
                   '--seconds','0' if managed else str(max(1,int(state['expires_at']-time.time())))]
        with Sandbox(command,cwd=workspace,writable=[workspace,runtime],
                     readonly=[modules/'coder_ui_session.py',modules/'coder_accessibility.py',directory/'launch.json',
                               *sandbox_paths(workspace,env)], environment=env, listen_port=port,
                     timeout=0 if managed else None,managed_application=managed,runtime_profile=state.get('runtime_profile')) as box:
            with (directory/'application-session.log').open('ab') as log:
                process = subprocess.Popen(box.args,env=box.env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            ready = runtime/'ready.json'; until = time.monotonic()+60
            while not ready.exists():
                if process.poll() is not None or time.monotonic()>until: raise RuntimeError('Application preview failed to launch; inspect its session log')
                time.sleep(.1)
            state=update(path,status='running',vnc_port=port)
            while process.poll() is None:
                state=json.loads(path.read_text())
                if state.get('desired_state')=='stopped' or (state.get('lifecycle')!='pinned' and time.time()>=state['expires_at']): break
                time.sleep(.3)
    except TimeoutError:
        state.update(status='closed')
    except Exception as error:
        state.update(status='failed',error=str(error))
    finally:
        if process: stop_process(process)
        cache_leases.close()
        if state['status'] != 'failed': state['status']='closed'
        update(path,status=state['status'],error=state.get('error',''))
        if operation_id:
            store.update(operation_id, status='failed' if state['status']=='failed' else 'succeeded',
                         ended=time.time(), result={'preview_id':identity,'revision_id':state['revision'],
                                                   'status':state['status'],'error':state.get('error','')})


if __name__ == '__main__': run(sys.argv[2],sys.argv[3])
