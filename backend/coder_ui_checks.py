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


def validate(value, behavior_version=0):
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
    from coder_patch_runtime import stop_process
    from coder_ui_session import free_port
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
        port=free_port(); modules=Path(__file__).parent
        command=['dbus-run-session','--',sys.executable,str(modules/'coder_ui_session.py'),
                 '--directory',str(runtime),'--port',str(port),'--seconds','300']
        with Sandbox(command,cwd=root,writable=[root,runtime],readonly=[modules/'coder_ui_session.py',
                modules/'coder_accessibility.py',*sandbox_paths(root,env)],environment=env,listen_port=port) as box:
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
                response=client.post(f'http://127.0.0.1:{port}',json=request,timeout=45)
                data=response.json()
                if response.status_code != 200: raise UIObservationFailure(data)
                return data
            try:
                deadline=time.monotonic()+20
                while not (runtime/'ready.json').exists():
                    _boundary(store,operation_id)
                    if process.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError('GUI session unavailable: '+(directory/'session.log').read_text()[-2000:])
                    time.sleep(.1)
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
    validate(flows, payload.get('engine_behavior_version', 0))
    revision=payload['revision_id']
    rows=[]
    for index,flow in enumerate(flows):
        observations=[]; assertions=0; interactions=0; behavioral_assertions=0; error=''; diagnostic={}; step_index=None
        try:
            if origin == 'project_interaction' and repository is not None:
                bind_project_entrypoint(repository, flow, store.get(operation_id)['payload'].get('task', ''))
            with session(store,operation_id,repository) as action:
                launched = action({'action':'launch',**{k:v for k,v in flow.items() if k!='steps'}})
                observations.append({'action': {'action': 'launch'}, 'observed': launched})
                for step_index,step in enumerate(flow['steps']):
                    if flow['kind'] == 'web' and step['action'] == 'observe' and ('equals' in step or step.get('selector')):
                        raise ValueError('Web observe returns the whole page accessibility snapshot. To compare one element, use action text with its CSS selector and equals or contains.')
                    request={k:v for k,v in step.items() if k not in {'equals','contains'}}
                    deadline=time.monotonic()+10
                    while True:
                        try:
                            observed=action(request)
                            value=observed.get('value' if request['action']=='value' else 'text',json.dumps(observed,ensure_ascii=False))
                            if 'equals' in step and value != step['equals']: raise AssertionError(f"Expected {step['equals']!r}; observed {value!r}")
                            if 'contains' in step and step['contains'] not in value: raise AssertionError(f"Expected {step['contains']!r} in {value!r}")
                            break
                        except (AssertionError,RuntimeError):
                            # Retry read-only observations; never duplicate a click or input.
                            if request['action'] not in {'observe','text','value'} or time.monotonic()>deadline: raise
                            time.sleep(.2)
                    interactions += int(request['action'] in {'click','fill','press'})
                    assertions += int('equals' in step or 'contains' in step)
                    behavioral_assertions += int(interactions > 0 and ('equals' in step or 'contains' in step))
                    observations.append({'action':request,'observed':observed})
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
            manifest = repository.root / '.daedalus-ui.json' if repository is not None else None
            row.update(is_test=True, phase='test', cwd='.',
                       test_ids={'ids': {f'ui-flow:{index}': 'passed' if row['passed'] else 'failed'}},
                       test_files=[{'path': '.daedalus-ui.json', 'sha256': hashlib.sha256(manifest.read_bytes()).hexdigest()}] if manifest and manifest.is_file() else [])
        rows.append(row); store.event(operation_id,'ui_verification',check=row)
    return rows
