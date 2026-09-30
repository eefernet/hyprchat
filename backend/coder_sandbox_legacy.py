"""Legacy worker adapters share the same process boundary without changing its two-turn flow."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid


def run(kind, request, emit=None):
    import openhands_worker as worker
    from coder_worker_runtime import WorkerStore, _boundary
    from coder_sandbox import Sandbox, toolchain_mounts
    from coder_sandbox_agent import model_bridge
    from coder_patch_runtime import stop_process
    from context_policy import runtime_settings
    project, name, reused = worker._workspace_for_request(request) if kind == 'sdk' else (Path(request.project_dir).resolve(), '', True)
    if not project.is_dir():
        raise ValueError('Project directory does not exist')
    identity = 'legacy-' + uuid.uuid4().hex
    root = Path(os.environ.get('DAEDALUS_STATE_DIR', str(worker.PROJECTS_DIR.parent / '.daedalus'))) / 'legacy-isolated' / identity
    runtime = root / 'runtime'; runtime.mkdir(parents=True)
    settings = {**runtime_settings(), 'openhands_num_ctx': request.num_ctx, 'aider_num_ctx': request.num_ctx,
                'generation_num_predict': request.num_predict}
    store = WorkerStore(root / 'supervisor')
    payload = {'settings': settings, 'model': request.model, 'ollama_url': request.ollama_url,
               'calls_remaining': settings['daedalus_model_calls'], 'seconds_remaining': settings['daedalus_job_seconds']}
    store.create(identity, identity, 'code', payload); store.update(identity, status='running', started=time.time())
    cancel = threading.Event()
    register = worker._register_run if kind == 'sdk' else worker._register_aider_run
    deregister = worker._deregister_run if kind == 'sdk' else worker._deregister_aider_run
    register(request.run_id, None, cancel)
    command = [sys.executable, str(Path(__file__).resolve()), str(runtime / 'request.json')]
    modules = list(Path(__file__).parent.glob('*.py'))
    binary = worker._aider_bin() if kind == 'aider' else ''
    if binary:
        modules.extend(toolchain_mounts([binary]))
    cursor = 0
    def drain():
        nonlocal cursor
        path = runtime / 'events.jsonl'
        if not emit or not path.is_file(): return
        with path.open() as source:
            source.seek(cursor)
            for _ in range(100):
                before = source.tell(); line = source.readline(256 * 1024)
                if not line.endswith('\n'):
                    source.seek(before); break
                try: event = json.loads(line)
                except ValueError: continue
                if isinstance(event, dict) and event.get('type') == 'step': emit(event)
            cursor = source.tell()
    try:
        with model_bridge(store, identity) as bridge:
            with Sandbox(command, cwd=project, writable=[project, runtime], readonly=modules,
                         environment={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LITELLM_LOCAL_MODEL_COST_MAP': 'True'},
                         network_urls=[bridge], runtime=runtime / 'environment') as box:
                data = request.model_dump(); data['ollama_url'] = box.translate_url(bridge)
                (runtime / 'request.json').write_text(json.dumps({'kind': kind, 'request': data, 'project': str(project),
                                                                'name': name, 'reused': reused, 'runtime': str(runtime)}))
                with (root / 'child.log').open('w') as log:
                    process = subprocess.Popen(box.args, env=box.env, stdout=log, stderr=log, start_new_session=True)
                    if kind == 'aider': register(request.run_id, process, cancel)
                    try:
                        while process.poll() is None:
                            if cancel.is_set(): raise InterruptedError('Run cancelled by client')
                            _boundary(store, identity); drain(); time.sleep(.1)
                    finally:
                        stop_process(process); drain()
                if cancel.is_set(): raise InterruptedError('Run cancelled by client')
                output = runtime / 'result.json'
                if not output.is_file():
                    raise RuntimeError('Isolated legacy worker failed: ' + (root / 'child.log').read_text(errors='replace')[-1500:])
                result = json.loads(output.read_text())
                store.update(identity, status='succeeded', result=result, ended=time.time())
                return result
    except Exception as error:
        cancelled = isinstance(error, InterruptedError)
        result = {'status': 'cancelled' if cancelled else 'error', 'error': str(error), 'summary': str(error), 'agent_finished': False}
        store.update(identity, status='cancelled' if cancelled else 'failed', result=result, ended=time.time())
        return result
    finally:
        deregister(request.run_id)


async def stream(request):
    from fastapi.responses import StreamingResponse
    queue = asyncio.Queue(); loop = asyncio.get_running_loop()
    def emit(event): loop.call_soon_threadsafe(queue.put_nowait, event)
    def execute():
        try: emit({'type': 'done', **run('sdk', request, emit)})
        finally: emit(None)
    async def events():
        future = loop.run_in_executor(None, execute)
        complete = False
        try:
            while True:
                try: item = await asyncio.wait_for(queue.get(), 1)
                except asyncio.TimeoutError:
                    yield ': keepalive\n\n'; continue
                if item is None:
                    complete = True; break
                yield 'data: ' + json.dumps(item) + '\n\n'
            await future
        finally:
            if not complete:
                import openhands_worker as worker
                worker.cancel_run(request.run_id)
    return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


if __name__ == '__main__':
    import openhands_worker as worker
    data = json.loads(Path(sys.argv[1]).read_text()); runtime = Path(data['runtime'])
    worker.CACHE_PATH = runtime / 'tool-cache.json'
    worker._workspace_for_request = lambda request: (Path(data['project']), data['name'], data['reused'])
    worker._auto_cleanup_stale = lambda: None
    def emit(event):
        with (runtime / 'events.jsonl').open('a') as output: output.write(json.dumps(event) + '\n')
    if data['kind'] == 'sdk':
        original = worker._parse_event
        def parse(event):
            row = original(event)
            if row: emit({'type': 'step', **row})
            return row
        worker._parse_event = parse
        result = worker.run_task(worker.RunRequest(**data['request'])).model_dump()
    else:
        result = worker._run_aider_blocking(worker.AiderRunRequest(**data['request']), stream_cb=emit)
    (runtime / 'result.json').write_text(json.dumps(result))
