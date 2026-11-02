"""Run the SDK in an untrusted child; authorize inference in the supervisor."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import copy
import http.client
import socket
from urllib.parse import urlsplit
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from coder_sandbox import Sandbox, IsolationUnavailable


@contextmanager
def model_bridge(store, operation_id, *, repository=None, environment=None, preparation_folder=None):
    from coder_worker_runtime import _boundary
    from coder_inference import require_local_model, ensure_context
    from context_policy import resolve, operation_settings, estimate_tokens
    operation = _boundary(store, operation_id)
    payload = operation['payload']
    require_local_model(payload['ollama_url'], payload['model'])
    modern = payload.get('engine') == 'openhands159'
    upstream = payload['ollama_url'].rstrip('/')
    lock = threading.Lock()
    connections, network_lock, closed = set(), threading.Lock(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def respond(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data))); self.end_headers()
            self.wfile.write(data)
        def forward(self):
            try:
                current = _boundary(store, operation_id)
                active = current['payload']
                if modern and self.path == '/daedalus/environment':
                    if self.command != 'POST' or repository is None:
                        raise ValueError('Environment preparation capability unavailable')
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 16000: raise ValueError('Invalid environment request size')
                    request = json.loads(self.rfile.read(length))
                    if set(request) != {'manifests'}: raise ValueError('Only dependency manifests may be requested')
                    from coder_engine_environment import refresh
                    with lock:
                        result = refresh(store, operation_id, repository, request['manifests'], environment, preparation_folder)
                    self.respond(200, result)
                    return
                if self.path not in {'/api/chat', '/api/generate', '/api/show', '/api/tags', '/api/ps', '/api/version'}:
                    raise ValueError('Unsupported inference endpoint')
                body = None
                if self.command == 'POST':
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 8 * 1024 * 1024:
                        raise ValueError('Invalid inference request size')
                    body = json.loads(self.rfile.read(length))
                    if body.get('model', body.get('name')) not in {active['model'], active['model'] + ':latest'}:
                        raise ValueError('The operation cannot select another model')
                elif self.path not in {'/api/tags', '/api/ps', '/api/version'}:
                    raise ValueError('Inference requires POST')
                warm = (self.path == '/api/generate' and body is not None and not body.get('prompt')
                        and not set(body) - {'model', 'name', 'prompt', 'stream', 'keep_alive', 'options'}
                        and not set(body.get('options') or {}) - {'num_ctx'})
                infer = self.path in {'/api/chat', '/api/generate'} and not warm
                with lock:
                    if closed.is_set():
                        raise InterruptedError('Inference bridge closed')
                    if infer:
                        if body is None:
                            raise ValueError('Inference requires a request body')
                        role = self.headers.get('X-Daedalus-Role', 'builder')
                        if role not in {'builder', 'compaction'}:
                            raise ValueError('This child cannot request another inference stage')
                        policy = resolve(role, operation_settings(active))
                        estimated = estimate_tokens({'messages': body.get('messages', body.get('prompt', '')), 'tools': body.get('tools', [])})
                        if not modern and estimated > policy.input_budget:
                            raise ValueError('Builder input exceeds its configured input budget')
                        if not modern: _boundary(store, operation_id, model_call=True)
                        body['options'] = {**body.get('options', {}), 'num_ctx': policy.num_ctx,
                                           'num_predict': policy.num_predict}
                        body['stream'] = False
                        if not modern: store.event(operation_id, 'model_call', context=policy.as_dict(),
                                    prompt_tokens_estimate=estimate_tokens(body.get('messages', body.get('prompt', ''))),
                                    inference_options=body['options'], thinking=body.get('think'),
                                    authority='supervisor')
                    if warm:
                        policies = [resolve(role, operation_settings(active)) for role in ('builder', 'compaction')]
                        requested = (body.get('options') or {}).get('num_ctx')
                        if requested is not None and requested not in {policy.num_ctx for policy in policies}:
                            raise ValueError('Warmup context is outside the configured stage policy')
                        body['stream'] = False
                    remaining = max(1, active['seconds_remaining'] - (time.time() - active.get('_active_started', current['started'])))
                    if warm and str(body.get('keep_alive', '')) not in {'0', '0s'}:
                        # Live 2026-10-28 (cw3-13fb755c): the child sees /api/ps filtered to its own runner below, so its
                        # eviction loop unloaded itself six times beside the resident planning model and raised a false
                        # "does not fit". Only the supervisor sees every occupant: evict here, before the warm load.
                        policy = next((p for p in policies if requested in (None, p.num_ctx)), policies[0])
                        ensure_context(upstream, active['model'], policy, remaining)
                        store.event(operation_id, 'warmup', context=policy.num_ctx, authority='supervisor')
                    from coder_engine_transport import request_json, infer as modern_infer
                    def send(current, request_body=None):
                        latest = current['payload']
                        seconds = max(1, latest['seconds_remaining'] - (time.time() - latest.get('_active_started', current['started'])))
                        return request_json(upstream, self.command, self.path, body if request_body is None else request_body,
                                            seconds, connections, network_lock, closed)
                    result = modern_infer(store, operation_id, body, role, policy, send) if infer and modern else send(current)
                    if self.path in {'/api/tags', '/api/ps'}:
                        result['models'] = [m for m in result.get('models', [])
                                            if m.get('name', m.get('model')) in {active['model'], active['model'] + ':latest'}]
                    self.respond(200, result)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, http.client.HTTPException) as error:
                from coder_engine_transport import TransportFailure
                category = getattr(error, 'category', 'cancelled' if isinstance(error, InterruptedError) else 'budget_exhausted' if isinstance(error, TimeoutError) else type(error).__name__)
                status = getattr(error, 'status', 409 if isinstance(error, (InterruptedError, TimeoutError)) else 400) if modern else 400
                store.event(operation_id, 'sandbox_inference_error', category=category, status=status, message=str(error)[:1000])
                try: self.respond(status, {'error': str(error)[:1000], 'category': category})
                except OSError: pass
        do_GET = do_POST = forward

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler); server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        closed.set()
        with network_lock:
            for connection in connections:
                if connection.sock:
                    try: connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                connection.close()
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def run_isolated(store, operation_id, repository):
    from coder_worker_runtime import _boundary
    from coder_patch_runtime import stop_process
    operation = _boundary(store, operation_id)
    runtime = store.root / 'agent-runtime' / operation['job_id']
    runtime.mkdir(parents=True, exist_ok=True)
    output = runtime / (operation_id + '-result.json')
    output.unlink(missing_ok=True)
    request = runtime / (operation_id + '-request.json')
    command = [sys.executable, str(Path(__file__).resolve()), '--child', str(request), str(output)]
    # Mount source modules individually: the worker directory also holds private
    # runtime configuration, old deployments and other jobs.
    modules = list(Path(__file__).resolve().parent.glob('*.py'))
    environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONPATH': str(Path(__file__).resolve().parent),
                   'LITELLM_LOCAL_MODEL_COST_MAP': 'True'}
    from coder_project_runtime import contract, prepare_environment, sandbox_paths
    profiles = contract(repository, managed_dependencies=operation['payload'].get('engine') == 'openhands159')
    audit = runtime / 'fixtures'; audit.mkdir(exist_ok=True)
    environment.update(DAEDALUS_PROJECT_ROOT=str(repository.root), DAEDALUS_AUDIT_DIR=str(audit),
        DAEDALUS_ENVIRONMENT_ROOT=str(store.root / 'project-environments' / __import__('hashlib').sha256(operation['payload'].get('project_id', operation['job_id']).encode()).hexdigest()))
    preflight = []
    from contextlib import nullcontext
    from coder_engine_environment import preparation
    with preparation(store, operation_id) if operation['payload'].get('engine') == 'openhands159' else nullcontext():
        for profile in profiles['profiles']:
            preflight.extend(prepare_environment(store, operation_id, repository.root, profile,
                operation['payload'].get('project_id') or operation['job_id'], environment, runtime))
    environment['PATH'] = str(repository.root / '.venv/bin') + os.pathsep + environment.get('PATH', '')
    environment['VIRTUAL_ENV'] = str(repository.root / '.venv')
    for row in preflight:
        store.event(operation_id, 'builder_setup', check=row)
    modern = operation['payload'].get('engine') == 'openhands159'
    if modern:
        environment['DAEDALUS_ENGINE_BEHAVIOR_VERSION'] = str(operation['payload'].get('engine_behavior_version', 0))
    readonly = [*modules, *sandbox_paths(repository.root, environment)]
    if modern:
        cache = Path(environment['DAEDALUS_ENVIRONMENT_ROOT']); cache.mkdir(parents=True, exist_ok=True)
        environment.update(PLAYWRIGHT_BROWSERS_PATH=str(cache / 'browsers'), PLAYWRIGHT_SKIP_BROWSER_GC='1')
        readonly.append(cache)
        tokenizer = os.environ.get('DAEDALUS_TOKENIZER_DIR', '')
        if not tokenizer: raise ValueError('Pinned tokenizer directory is not configured')
        environment['DAEDALUS_TOKENIZER_DIR'] = tokenizer
        readonly.append(Path(tokenizer))
    cursor = 0
    with model_bridge(store, operation_id, repository=repository, environment=environment, preparation_folder=runtime / 'preparation') as bridge:
        with Sandbox(command, cwd=repository.root, writable=[repository.root, runtime], readonly=readonly,
                     runtime=(store.root / 'operation-caches' / operation_id / 'agent') if modern else runtime / 'environment',
                     short_tmp=modern, environment=environment, network_urls=[bridge]) as box:
            child_operation = copy.deepcopy(_boundary(store, operation_id))
            child_operation['payload']['ollama_url'] = box.translate_url(bridge)
            if modern:
                # The supervisor excludes preparation as it happens; this disposable
                # child ledger must not expire earlier using an obsolete clock.
                child_operation['payload']['seconds_remaining'] += operation['payload']['settings'].get('daedalus_preparation_seconds', 600)
            if any(not r.get('passed') for r in preflight):
                child_operation['payload']['builder_guidance'] = 'Fix these setup failures before implementing more behavior: ' + json.dumps(preflight) + '\n' + child_operation['payload'].get('builder_guidance', '')
            child_operation['payload']['calls_remaining'] = max(1, operation['payload']['calls_remaining'] - operation['calls'])
            request.write_text(json.dumps({'operation': child_operation, 'root': str(repository.root), 'runtime': str(runtime)}))
            log = store.root / 'logs' / (operation_id + '-agent.log')
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open('w') as stream:
                process = subprocess.Popen(box.args, env=box.env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    while process.poll() is None:
                        _boundary(store, operation_id)
                        cursor = relay_events(store, operation_id, runtime, cursor)
                        time.sleep(.1)
                finally:
                    stop_process(process)
                    relay_events(store, operation_id, runtime, cursor)
            if not output.is_file():
                raise IsolationUnavailable('SDK child did not return a checkpoint: ' + log.read_text(errors='replace')[-1500:])
            result = json.loads(output.read_text())
            if result.get('error'):
                kind = result.get('error_type')
                exception = TimeoutError if kind == 'TimeoutError' else ValueError if kind == 'ValueError' else RuntimeError
                raise exception(result['error'])
            return {k: v for k, v in result.items() if k in {
                'engine', 'sdk_version', 'agent_finished', 'execution_status', 'incomplete_reason', 'session_id', 'events', 'hook_denials', 'hook_nudges',
                'hook_denied_unresolved'}}


def relay_events(store, operation_id, runtime, cursor):
    """Diagnostic child events cannot overwrite authoritative usage or state."""
    path = runtime / 'telemetry' / (operation_id + '.jsonl')
    if not path.is_file():
        return cursor
    with path.open() as stream:
        stream.seek(cursor)
        for _ in range(100):
            before = stream.tell(); line = stream.readline(256 * 1024)
            if not line.endswith('\n'):
                stream.seek(before)
                break
            try: event = json.loads(line)
            except ValueError: continue
            kind, data = event.get('type'), event.get('data', {})
            if kind in {'agent', 'browser_action', 'browser_diagnostic', 'builder_nudge', 'builder_stalled',
                        'tool_mode_fallback', 'text_tool_fallback', 'compaction', 'usage', 'stop_hook', 'tool_mode',
                        'narration_turn'} and isinstance(data, dict):
                store.event(operation_id, kind, **{**{k: v for k, v in data.items() if k not in {'identity', 'operation_id', 'kind'}}, 'authority': 'untrusted_agent_diagnostic'})
        return stream.tell()


def child_main(request, output):
    from coder_worker_runtime import WorkerStore
    from coder_repository import Repository
    from coder_sdk_runtime import _run_coder_local
    from context_policy import apply_settings
    data = json.loads(Path(request).read_text())
    operation, runtime = data['operation'], Path(data['runtime'])
    # This private store is disposable SDK bookkeeping, never trusted job state.
    class ChildStore(WorkerStore):
        def event(self, identity, kind, **values):
            result = super().event(identity, kind, **values)
            directory = runtime / 'telemetry'; directory.mkdir(exist_ok=True)
            with (directory / (identity + '.jsonl')).open('a') as stream:
                stream.write(json.dumps({'type': kind, 'data': values}) + '\n')
            return result
    class ChildRepository(Repository):
        def snapshot(self, message='Agent checkpoint', parent=''):
            return super().snapshot(message, parent='')
    store = ChildStore(runtime / 'bookkeeping')
    store.create(operation['id'], operation['job_id'], operation['kind'], operation['payload'])
    store.update(operation['id'], status='running', calls=0, started=time.time())
    (store.root / 'jobs' / operation['job_id']).mkdir(parents=True, exist_ok=True)
    repository = ChildRepository(Path(data['root']), runtime / 'inventory', operation['payload']['settings']['daedalus_exclude_dirs'])
    apply_settings(operation['payload']['settings'])
    try:
        if operation['payload'].get('engine') == 'openhands159':
            from coder_engine_runtime import run
            result = run(store, operation['id'], repository)
        else:
            result = _run_coder_local(store, operation['id'], repository)
    except Exception as error:
        result = {'error_type': type(error).__name__, 'error': str(error)}
    Path(output).write_text(json.dumps(result))


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--child':
        raise SystemExit('SDK child requires supervisor input/output paths')
    child_main(sys.argv[2], sys.argv[3])
