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


def alive(state):
    try:
        process = psutil.Process(state['pid'])
        return abs(process.create_time()-state['process_started']) < .01 and process.is_running()
    except (psutil.Error, KeyError): return False


def stop(state):
    if not alive(state): return
    process = psutil.Process(state['pid'])
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
            if previous['expires_at'] <= time.time(): stop(previous)
            elif previous['job_id'] == job_id and alive(previous): stop(previous)
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
                 'status': 'starting', 'expires_at': time.time()+900, 'operation_id': operation['id']}
        save(directory/'session.json', state)
        with (directory/'supervisor.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--run', str(store.root), identity],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        state.update(pid=process.pid, process_started=psutil.Process(process.pid).create_time())
        save(directory/'session.json', state)
        return state

    @app.get('/jobs/{job_id}/previews/{identity}')
    def status(job_id: str, identity: str):
        try: state = read_session(store.root, identity, job_id)
        except LookupError: raise HTTPException(404, 'Preview not found') from None
        if not alive(state) and state['status'] in {'starting', 'running'}: state['status'] = 'closed'
        return state

    @app.delete('/jobs/{job_id}/previews/{identity}')
    def close(job_id: str, identity: str):
        state = status(job_id, identity); stop(state)
        state['status'] = 'closed'; save(store.root/'previews'/identity/'session.json', state)
        return {'status': 'closed'}

    @app.websocket('/jobs/{job_id}/previews/{identity}/stream')
    async def stream(websocket: WebSocket, job_id: str, identity: str):
        try: state = read_session(store.root, identity, job_id)
        except LookupError:
            await websocket.close(code=4404); return
        if (not hmac.compare_digest(websocket.query_params.get('token',''),state['token']) or
            state['expires_at'] <= time.time() or state['status'] != 'running' or not alive(state)):
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
        try: await asyncio.wait(tasks, timeout=max(0,state['expires_at']-time.time()), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close(); await writer.wait_closed()
            try: await websocket.close()
            except (RuntimeError, WebSocketDisconnect): pass


def run(root, identity):
    from coder_sandbox import Sandbox
    from coder_patch_runtime import stop_process
    from coder_ui_session import free_port
    from coder_worker_runtime import WorkerStore
    from coder_repository import Repository
    from coder_project_runtime import prepare_application, sandbox_paths
    store = WorkerStore(root); directory = Path(root)/'previews'/identity
    path = directory/'session.json'; state = json.loads(path.read_text())
    deadline = time.monotonic()+5
    while 'pid' not in state and time.monotonic()<deadline:
        time.sleep(.05); state = json.loads(path.read_text())
    workspace = directory/'workspace'; workspace.mkdir()
    runtime = directory/'runtime'; runtime.mkdir()
    process = None
    operation_id = None
    from contextlib import ExitStack
    cache_leases = ExitStack()
    def expired(*args): raise TimeoutError('Preview expired')
    signal.signal(signal.SIGTERM, expired); signal.signal(signal.SIGALRM, expired); signal.alarm(900)
    try:
        with tarfile.open(directory/'source.tar.gz') as archive: archive.extractall(workspace, filter='data')
        source = store.get(state['operation_id'])
        repository = Repository(workspace, directory/'repository', source['payload']['settings']['daedalus_exclude_dirs']); repository.refresh()
        payload = {**source['payload'], 'seconds_remaining': 900, 'calls_remaining': 0, '_active_started': time.time()}
        operation_id = 'preview-'+identity
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
        if any(not row.get('passed') for row in checks): raise RuntimeError('Preview preparation or build failed: '+json.dumps(checks)[-6000:])
        port = free_port()
        modules = Path(__file__).parent
        command = ['dbus-run-session','--',sys.executable,str(modules/'coder_ui_session.py'),
                   '--directory',str(runtime), '--vnc-port',str(port),'--launch',str(directory/'launch.json'),
                   '--seconds',str(max(1,int(state['expires_at']-time.time())))]
        with Sandbox(command,cwd=workspace,writable=[workspace,runtime],
                     readonly=[modules/'coder_ui_session.py',modules/'coder_accessibility.py',directory/'launch.json',
                               *sandbox_paths(workspace,env)], environment=env, listen_port=port) as box:
            with (directory/'application-session.log').open('ab') as log:
                process = subprocess.Popen(box.args,env=box.env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            ready = runtime/'ready.json'; until = time.monotonic()+60
            while not ready.exists():
                if process.poll() is not None or time.monotonic()>until: raise RuntimeError('Application preview failed to launch; inspect its session log')
                time.sleep(.1)
            state.update(status='running',vnc_port=port); save(path,state)
            while process.poll() is None and time.time()<state['expires_at']: time.sleep(.3)
    except TimeoutError:
        state.update(status='closed')
    except Exception as error:
        state.update(status='failed',error=str(error))
    finally:
        if process: stop_process(process)
        cache_leases.close()
        if state['status'] != 'failed': state['status']='closed'
        save(path,state)
        if operation_id:
            store.update(operation_id, status='failed' if state['status']=='failed' else 'succeeded',
                         ended=time.time(), result={'preview_id':identity,'revision_id':state['revision'],
                                                   'status':state['status'],'error':state.get('error','')})


if __name__ == '__main__': run(sys.argv[2],sys.argv[3])
