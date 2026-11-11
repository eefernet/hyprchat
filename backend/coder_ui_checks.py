"""Controller-owned interaction assertions outside the application namespace."""
from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import time
import uuid
from urllib.parse import unquote, urlsplit

import requests


class UIObservationFailure(RuntimeError):
    def __init__(self, details):
        super().__init__(details.get('error', 'GUI action failed'))
        self.details = details


@contextmanager
def startup_preparation(store, operation_id):
    """Share dependency preparation's durable time/attempt ledger across Resume."""
    from coder_engine_environment import preparation
    from coder_environment_publish import atomic_json
    operation = store.get(operation_id)
    path = store.root/'preparation-ledgers'/(operation['job_id'] + '.json')
    ledger = json.loads(path.read_text()) if path.exists() else {'failures': {}, 'seconds': 0}
    inflight = ledger.pop('inflight', None)
    if inflight:
        previous = store.get(inflight['operation_id'])
        last_seen = previous.get('ended') or previous['payload'].get('_usage_checkpoint', {}).get('at', inflight['started'])
        ledger['seconds'] += max(0, last_seen - inflight['started'])
    limit = operation['payload']['settings'].get('daedalus_preparation_seconds', 600)
    key = 'gui-startup'
    if ledger['seconds'] >= limit or ledger['failures'].get(key, 0) >= 2:
        raise TimeoutError('GUI preparation allowance exhausted')
    began = time.monotonic()
    ledger['failures'][key] = ledger['failures'].get(key, 0) + 1
    ledger['inflight'] = {'key': key, 'operation_id': operation_id, 'started': time.time()}
    path.parent.mkdir(parents=True, exist_ok=True); atomic_json(path, ledger)
    try:
        with preparation(store, operation_id):
            payload = dict(store.get(operation_id)['payload'])
            payload['_preparation_deadline'] = min(payload['_preparation_deadline'], time.time() + limit - ledger['seconds'])
            store.update(operation_id, payload=payload)
            yield
        ledger['failures'][key] -= 1
    finally:
        ledger['seconds'] += time.monotonic() - began
        ledger.pop('inflight', None); atomic_json(path, ledger)


def wait_for_session(store, operation_id, process, started, ready, log):
    """Charge broker admission to preparation; give the launched GUI 20 seconds."""
    from coder_worker_runtime import _boundary
    deadline = None
    try:
        with startup_preparation(store, operation_id):
            while True:
                _boundary(store, operation_id)
                if process.poll() is not None:
                    raise RuntimeError('GUI process exited before becoming ready')
                if started.exists():
                    if deadline is None: deadline = time.monotonic() + 20
                    if ready.exists(): return
                if deadline is not None and time.monotonic() >= deadline:
                    raise RuntimeError('GUI process did not become ready within 20 seconds')
                time.sleep(.1)
    except InterruptedError:
        raise
    except (TimeoutError, RuntimeError) as error:
        limited = isinstance(error, TimeoutError)
        raise UIObservationFailure({'error': str(error), 'environment_fault': True,
            'category': 'preparation_limit' if limited else 'gui_startup',
            'phase': 'startup', 'retryable': not limited,
            'log_tail': log.read_text(errors='replace')[-2000:] if log.exists() else ''}) from error


def _validate_legacy(value, behavior_version=0):
    if not isinstance(value, list) or len(value) > 12: raise ValueError('UI checks must be a list of at most twelve flows')
    for index, flow in enumerate(value):
        if not isinstance(flow, dict):
            raise ValueError(f'UI flow {index} must be a JSON object with kind, command (or web file_path), and steps fields')
        if flow.get('kind') not in {'web','desktop'}:
            raise ValueError(f'UI flow {index}: kind must be "web" or "desktop"; received {flow.get("kind")!r}. '
                             'Use toolkit for "qt", "gtk", or "swing".')
        if not (flow.get('command') or flow['kind'] == 'web' and flow.get('file_path')):
            keys=', '.join(sorted(str(key) for key in flow))
            raise ValueError(f'UI flow {index}: missing required "command" field. Received fields: {keys}. '
                             'The key is "command", not "launch" or "launch_command". '
                             'Put your application launch command in that field; only web flows may use file_path instead.')
        if flow.get('command') and not isinstance(flow['command'],str):
            raise ValueError(f'UI flow {index}: "command" must be a shell command string, not an object or argument array')
        if not isinstance(flow.get('steps'), list) or not 1 <= len(flow['steps']) <= 40:
            raise ValueError('Each UI check needs one to forty interaction steps')
        if flow.get('file_path'):
            if not isinstance(flow['file_path'], str): raise ValueError('Browser file path must be text')
            path = Path(flow['file_path'])
            if path.is_absolute() or '..' in path.parts or path.suffix.lower() not in {'.html', '.htm'}:
                raise ValueError('Browser file paths must identify a project-relative HTML file')
        if str(flow.get('url', '')).startswith('file:'):
            raise ValueError('Use a project-relative file_path for file browser checks so the controller binds it to the revision')
        assertions = 0
        for step_index, step in enumerate(flow['steps']):
            if not isinstance(step, dict): raise ValueError('UI steps must be action objects')
            if behavior_version >= 1 and flow['kind'] == 'desktop' and (step.get('selector') or step.get('session_id')):
                raise ValueError(f'UI flow {index}, step {step_index}: desktop node ids are session-local. Persist observed accessible name/role locators, not selector or session_id.')
            if step.get('action') not in {'observe','click','fill','text','value','press','reload'}:
                action = repr(step.get('action'))[:100]
                raise ValueError(f"UI flow {index}, step {step_index}: unsupported action {action}. "
                                 'Each step needs an action field: observe, click, fill, text, value, press, or reload. '
                                 'Assertion shape: {"action":"text","selector":"<result selector>","equals":"<expected text>"}. '
                                 'Put equals or contains directly on the step, not in a separate assertion object.')
            if 'equals' in step or 'contains' in step: assertions += 1
        if not assertions: raise ValueError(f'UI flow {index} needs an explicit expected result: put equals or contains '
                                            'directly on a text/observe step after interacting with a control.')
    return value


class UIFlowValidationError(ValueError):
    def __init__(self, errors):
        self.errors = errors[:64]
        super().__init__('\n'.join(f"Flow {e.get('flow_index', '-')}, step {e.get('step_index', '-')}: {e['reason']}" for e in self.errors))


def validate(value, behavior_version=0, *, preflight_version=0):
    """One schema for early diagnostics and immutable-revision replay."""
    if not preflight_version:
        return _validate_legacy(value, behavior_version)
    errors = []
    def error(reason, flow=None, step=None):
        errors.append({'category': 'ui_flow_schema', 'flow_index': flow, 'step_index': step, 'reason': reason})
    if not isinstance(value, list) or len(value) > 12:
        raise UIFlowValidationError([{'reason': 'UI checks must be an array of at most twelve flows.'}])
    for i, flow in enumerate(value):
        if not isinstance(flow, dict):
            error('Use an object with kind, command (or web file_path), and steps.', i); continue
        kind = flow.get('kind')
        if not isinstance(kind, str) or kind not in {'web', 'desktop'}: error('kind must be web or desktop; put qt/gtk/swing in toolkit.', i)
        if not (flow.get('command') or kind == 'web' and flow.get('file_path')):
            error('Provide command as a shell command string, or a project-relative HTML file_path for web.', i)
        if flow.get('command') and not isinstance(flow['command'], str): error('command must be a shell command string.', i)
        path = flow.get('file_path')
        if path and (not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts or Path(path).suffix.lower() not in {'.html', '.htm'}):
            error('file_path must identify a project-relative HTML file.', i)
        if str(flow.get('url', '')).startswith('file:'): error('Use file_path instead of a file:// URL.', i)
        steps = flow.get('steps')
        if not isinstance(steps, list) or not 1 <= len(steps) <= 40:
            error('Provide one to forty steps.', i); continue
        interacted = asserted = False
        for n, step in enumerate(steps):
            if not isinstance(step, dict): error('Use an action object.', i, n); continue
            action = step.get('action'); expectation = 'equals' in step or 'contains' in step
            if not isinstance(action, str):
                error('action must be text naming a supported action.', i, n); continue
            if action not in {'observe', 'click', 'fill', 'text', 'value', 'press', 'reload'}:
                error('action must be observe, click, fill, text, value, press, or reload.', i, n)
            if kind == 'desktop' and (step.get('selector') or step.get('session_id')):
                error('Desktop references are session-local; persist accessible name/role instead.', i, n)
            if action == 'observe' and (any(step.get(k) for k in ('selector', 'name', 'role')) or 'equals' in step):
                error('observe reads the whole interface. For one display use {"action":"text","selector":"<result>","equals":"<expected text>"}; for an input use action value.', i, n)
            if expectation and action not in {'observe', 'text', 'value'}:
                error('Put equals/contains on a subsequent read-only text or value step, never on click/fill/press/reload.', i, n)
            if 'equals' in step and 'contains' in step: error('Choose one comparison: equals or contains.', i, n)
            if step.get('contains') == '': error('contains must not be empty; use equals "" to check an empty result.', i, n)
            for field in ('equals', 'contains', 'value', 'selector', 'name', 'role'):
                if field in step and not isinstance(step[field], str): error(f'{field} must be text; preserve exact values such as "0.00" or "".', i, n)
            if action in {'click', 'fill', 'text', 'value', 'press'} and not (step.get('selector') or step.get('name')):
                error('Provide a unique selector (web) or accessible name/role.', i, n)
            if action in {'fill', 'press'} and 'value' not in step: error('Provide value for fill/press.', i, n)
            if expectation and action in {'observe', 'text', 'value'} and interacted: asserted = True
            if action in {'click', 'fill', 'press'}: interacted = True
        if not asserted: error('Interact with a control, then assert its result using equals or contains on text/value (or whole-interface observe contains).', i)
    if errors: raise UIFlowValidationError(errors)
    return value


def workspace_bound_commands(flows, root):
    """Flow commands naming this session's workspace cannot be replayed on the controller's fresh copy."""
    roots = {str(Path(root)), str(Path(root).resolve())}
    return [{'category': 'ui_flow_schema', 'flow_index': index, 'step_index': None,
             'reason': 'command contains this session\'s workspace path, which does not exist where the controller '
                       'replays the flow. Use project-relative paths, for example .venv/bin/python app.py.'}
            for index, flow in enumerate(flows if isinstance(flows, list) else [])
            if isinstance(flow, dict) and isinstance(flow.get('command'), str) and any(r in flow['command'] for r in roots)]


def replay(flow, action, *, strict=False, deadline=None):
    """Replay one flow; never retry mutating actions or proceed after a failure."""
    observations = []; assertions = interactions = behavioral = 0; step_index = None
    request = {'action': 'launch', **{k: v for k, v in flow.items() if k != 'steps'}}
    observed = None; value = None; step = {}
    try:
        observed = action(request)
        observations.append({'action': {'action': 'launch'}, 'observed': observed})
        for step_index, step in enumerate(flow['steps']):
            observed = value = None
            if flow['kind'] == 'web' and step['action'] == 'observe' and ('equals' in step or step.get('selector')):
                raise ValueError('Web observe returns the whole page accessibility snapshot. To compare one element, use action text with its CSS selector and equals or contains.')
            request = {k: v for k, v in step.items() if k not in {'equals', 'contains'}}
            until = min(time.monotonic() + 10, deadline if deadline is not None else float('inf'))
            while True:
                if deadline is not None and time.monotonic() >= deadline: raise TimeoutError('UI flow deadline reached')
                try:
                    observed = action(request)
                    prop = 'value' if request['action'] == 'value' else 'text'
                    value = observed.get(prop, json.dumps(observed, ensure_ascii=False))
                    if strict and request['action'] == 'text' and ('equals' in step or 'contains' in step) and (observed.get('input_control') or observed.get('editable')):
                        raise UIObservationFailure({'category': 'ui_flow_property', 'error': 'This target is an input control. Assert its actual value with action value; displayed text cannot prove an empty input.'})
                    if 'equals' in step and value != step['equals']: raise AssertionError(f"Expected {step['equals']!r}; observed {value!r}")
                    if 'contains' in step and step['contains'] not in value: raise AssertionError(f"Expected {step['contains']!r} in {value!r}")
                    break
                except AssertionError:
                    if request['action'] not in {'observe', 'text', 'value'} or time.monotonic() >= until: raise
                    time.sleep(min(.2, max(0, until - time.monotonic())))
            interactions += int(request['action'] in {'click', 'fill', 'press'})
            assertions += int('equals' in step or 'contains' in step)
            behavioral += int(interactions > 0 and ('equals' in step or 'contains' in step))
            observations.append({'action': request, 'observed': observed})
        if not behavioral: raise ValueError('UI tests must exercise a control and assert its result; page loading alone is not behavioral evidence')
        return {'observations': observations, 'assertions': assertions, 'passed': True, 'reason': '', 'diagnostic': {}}
    except (InterruptedError, TimeoutError):
        raise
    except Exception as failure:
        detail = {**getattr(failure, 'details', {}), 'step_index': step_index,
                  'action': request.get('action'), 'locator': {k: request[k] for k in ('selector', 'name', 'role') if k in request},
                  'property': 'value' if request.get('action') == 'value' else 'text' if request.get('action') == 'text' else 'interface',
                  'category': getattr(failure, 'details', {}).get('category', 'assertion_mismatch' if isinstance(failure, AssertionError) else 'interaction_failed')}
        if 'equals' in step or 'contains' in step:
            detail.update(comparison='equals' if 'equals' in step else 'contains', expected=step.get('equals', step.get('contains')), observed=value[:4000] if isinstance(value, str) else value)
        if not detail.get('log_tail'):
            try: detail['log_tail'] = str(action({'action': 'diagnostics'}).get('log_tail', ''))[-4000:]
            except (InterruptedError, TimeoutError): raise
            except Exception: pass  # Preserve the original failure when diagnostics are unavailable.
        if observed is not None: observations.append({'action': request, 'observed': observed, 'failed': True})
        return {'observations': observations, 'assertions': assertions, 'passed': False, 'reason': str(failure)[:4000], 'diagnostic': detail}


def interfaces(repository, profile):
    required = {'web'} if profile.get('web_packages') else set()
    repository.refresh()
    with repository.connect() as connection:
        entries = [dict(row) for row in connection.execute('SELECT path FROM files')]
    for entry in entries:
        path = entry.get('path', '') if isinstance(entry,dict) else entry
        if Path(path).suffix not in {'.py','.java','.cpp','.cc','.cs'}: continue
        source = repository.root/path
        if source.stat().st_size > 512000: continue
        text = source.read_text(errors='replace')
        if any(token in text for token in ('PyQt','PySide','javax.swing','Gtk.Application','QApplication','gtk_init(')):
            required.add('desktop')
    return sorted(required)


@contextmanager
def session(store, operation_id, repository):
    from coder_sandbox import Sandbox
    from coder_repository import Repository
    from coder_project_runtime import prepare_application, sandbox_paths
    from coder_execution_utils import stop_process
    from coder_worker_runtime import _boundary
    operation = store.get(operation_id); payload=operation['payload']
    from coder_engine_cache import lease, scope
    project_id = payload.get('project_id') or operation['job_id']
    with lease(store.root / 'project-environments' / scope(project_id)):
        directory=store.root/'ui-checks'/operation_id/uuid.uuid4().hex; directory.mkdir(parents=True)
        archive=directory/'source.tar.gz'; repository.archive(payload['revision_id'], archive)
        root=directory/'workspace'; root.mkdir(); runtime=directory/'runtime'; runtime.mkdir()
        with tarfile.open(archive) as bundle: bundle.extractall(root,filter='data')
        isolated=Repository(root,directory/'repository',repository.excludes); isolated.refresh()
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),
             'DAEDALUS_ENGINE_BEHAVIOR_VERSION': str(payload.get('engine_behavior_version', 0))}
        rows=prepare_application(store,operation_id,isolated,project_id,env,runtime)
        if any(not r.get('passed') for r in rows):
            failed = [r for r in rows if not r.get('passed')]
            if any(r.get('environment_fault') for r in failed):
                raise UIObservationFailure({'error': 'UI environment preparation failed', 'category': 'environment',
                    'environment_fault': True, 'phase': 'preparation', 'checks': failed})
            raise RuntimeError('UI preparation or build failed: '+json.dumps(failed)[-6000:])
        # Each sandbox has its own network namespace. Keep the internal
        # control listener off the ephemeral range and let the host reserve
        # its forwarding port atomically.
        control_port = 27771
        modules=Path(__file__).parent
        command=['dbus-run-session','--',sys.executable,str(modules/'coder_ui_session.py'),
                 '--directory',str(runtime),'--port',str(control_port),'--seconds','300']
        with Sandbox(command,cwd=root,writable=[root,runtime],readonly=[modules/'coder_ui_session.py',
                modules/'coder_accessibility.py',*sandbox_paths(root,env)],environment=env,listen_port=0,guest_port=control_port) as box:
            with (directory/'session.log').open('ab') as log:
                process=subprocess.Popen(box.args,env=box.env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            client=requests.Session(); client.trust_env=False
            def action(request):
                _boundary(store,operation_id)
                if request.get('action') == 'launch' and request.get('file_path'):
                    from coder_repository import safe_relative
                    target = safe_relative(root, request['file_path'])
                    if not target.is_file(): raise ValueError('Browser entrypoint does not exist')
                    request = {**request, 'url': target.as_uri(), 'command': ''}
                response=client.post(f'http://127.0.0.1:{box.listen_port}',json=request,timeout=45)
                data=response.json()
                if response.status_code != 200: raise UIObservationFailure(data)
                return data
            try:
                wait_for_session(store, operation_id, process, box.service_start_path,
                                 runtime/'ready.json', directory/'session.log')
                yield action
            finally:
                client.close(); stop_process(process)

def bind_project_entrypoint(repository, flow, task):
    """A model's substitute page or executable cannot verify an explicitly named app."""
    from coder_engine_contract import required_files
    named = required_files(task)
    if flow['kind'] == 'web':
        entrypoints = {name for name in named if Path(name).suffix.lower() in {'.html', '.htm'}}
        if not entrypoints and (repository.root / 'index.html').is_file(): entrypoints = {'index.html'}
        target = flow.get('file_path') or unquote(urlsplit(flow.get('url', '')).path).lstrip('/')
        if target: target = Path(target).as_posix()
        if entrypoints and target and Path(target).suffix.lower() in {'.html', '.htm'} and target not in entrypoints:
            raise ValueError('Browser checks must exercise the application entrypoint ' + ', '.join(sorted(entrypoints)) +
                             '; a separate fixture page does not verify that application.')
    else:
        entrypoints = [name for name in named if Path(name).suffix.lower() in {'.py', '.java', '.jar'}]
        words = shlex.split(flow.get('command', ''))
        def references(name):
            if name in words or './' + name in words: return True
            if name.endswith('.java'): return any(word.rsplit('.', 1)[-1] == Path(name).stem for word in words)
            if name.endswith('.py') and '-m' in words:
                return name[:-3].replace('/', '.') in words
            return False
        if entrypoints and not any(references(name) for name in entrypoints):
            raise ValueError('Desktop checks must launch the requested application entrypoint ' + ', '.join(entrypoints) +
                             '; a substitute window does not verify that application.')


def run(store, operation_id, repository, flows, *, origin='confirmed_interaction'):
    payload = store.get(operation_id)['payload']
    validate(flows, payload.get('engine_behavior_version', 0), preflight_version=payload.get('ui_preflight_version', 0))
    revision=payload['revision_id']
    rows=[]
    for index,flow in enumerate(flows):
        observations=[]; assertions=0; interactions=0; behavioral_assertions=0; error=''; diagnostic={}; step_index=None
        try:
            if origin == 'project_interaction' and repository is not None:
                bind_project_entrypoint(repository, flow, store.get(operation_id)['payload'].get('task', ''))
            with session(store,operation_id,repository) as action:
                result = replay(flow, action, strict=payload.get('ui_preflight_version', 0) >= 1)
                observations, assertions = result['observations'], result['assertions']
                error, diagnostic = result['reason'], result['diagnostic']
                behavioral_assertions = int(result['passed'])
                if diagnostic: diagnostic['flow_index'] = index
        except (InterruptedError, TimeoutError):
            raise
        except Exception as failure:
            error=str(failure)
            if payload.get('engine_behavior_version', 0) >= 1:
                diagnostic={**getattr(failure,'details',{}), 'flow_index':index, 'step_index':step_index}
        if not behavioral_assertions:
            error = error or 'UI tests must exercise a control and assert its result; page loading alone is not behavioral evidence'
        row={'id':f'ui:{index}' if origin == 'confirmed_interaction' else f'project-ui:{index}', 'origin':origin,'revision_id':revision,
             'passed':not error and assertions>0,'evidence_version':3,'coverage_observed':assertions>0,
             'execution_succeeded':not error and assertions>0,'assertions_executed':assertions,
             'test_interfaces':[flow['kind']],'source_bindings':[],
             'service_bindings':[{'revision_id':revision,'interface':flow['kind'],'file_path':flow.get('file_path'), 'observations':observations}],
             'reason':error,'classification':'interaction_failed' if error else 'passed', 'flow': flow}
        if diagnostic:
            row.update(diagnostic=diagnostic, log_tail=diagnostic.get('log_tail', ''))
            if diagnostic.get('environment_fault'):
                row.update(environment_fault=True, classification='environment')
        if origin == 'project_interaction':
            from coder_required_tests import ui_flow_signature
            manifest = repository.root / '.daedalus-ui.json' if repository is not None else None
            outcome = 'passed' if row['passed'] else 'failed'
            row.update(is_test=True, phase='test', cwd='.',
                       ui_flow_identity_version=1, ui_flow_sha256=ui_flow_signature(flow),
                       test_ids={'ids': {'ui-scenario:' + ui_flow_signature(flow, expectations=False): outcome},
                                 # Preserve old saved identities on resumed modern jobs.
                                 # New baseline sets use semantic ids only.
                                 'aggregates': {f'ui-flow:{index}': outcome}},
                       test_files=[{'path': '.daedalus-ui.json', 'sha256': hashlib.sha256(manifest.read_bytes()).hexdigest()}] if manifest and manifest.is_file() else [])
        rows.append(row); store.event(operation_id,'ui_verification',check=row)
    return rows
