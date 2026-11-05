"""Private, model-free verification of a freshly downloaded Daedalus archive."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import stat
import sys
import tarfile
import time
import uuid

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine_cases import private_probes
from coder_project_runtime import prepare_application, sandbox_paths, services
from coder_repository import Repository, safe_relative
from coder_sandbox import Sandbox
from coder_ui_checks import session, UIObservationFailure
from coder_worker_runtime import WorkerStore


def named_button(action, request):
    """An ordinary request can name a button by its visible legend or accessible name."""
    nodes = action({'action':'observe'})['nodes']
    name = request['name']
    matches = [n for n in nodes if n.get('showing') and n.get('activatable') and
               (n.get('role')=='button' or n.get('tag')=='button') and
               (n.get('name')==name or str(n.get('text','')).strip()==name)]
    if len(matches) != 1:
        raise UIObservationFailure({'category':'ambiguous_target' if matches else 'target_not_found',
                                    'error':f'Expected one visible button named {name!r}; found {len(matches)}'})
    return {'action':'click','selector':matches[0]['selector']}


def ui_checks(action, case, followup):
    """Dispatch inputs once. Only poll reads, keeping expectations outside the app."""
    launch = {k: case[k] for k in ('kind', 'command', 'file_path', 'url', 'toolkit') if case.get(k)}
    initial = action({'action': 'launch', **launch})
    checks = []
    steps = case['steps'] + (case['followup_steps'] if followup else [])
    for index, step in enumerate(steps):
        request = {k: step[k] for k in ('action','selector','name','role','value') if k in step}
        if request['action'] == 'restart':
            action({'action':'reset'})
            action({'action':'launch', **launch})
            continue
        if step.get('labelled_field'):
            nodes = action({'action':'observe'})['nodes']
            fields = [n for n in nodes if n.get('name') == request['name'] and n.get('showing') and n.get('editable')]
            if len(fields) != 1:
                raise ValueError(f"Expected one editable field named {request['name']!r}")
            request = {**request, 'selector':fields[0]['selector']}
        if case['kind'] == 'web' and request['action']=='click' and request.get('role')=='button' and request.get('name') and not request.get('selector') and not step.get('exact_accessible_name'):
            request = named_button(action, request)
        if case['kind'] == 'desktop' and request.get('role') == 'button':
            # AT-SPI roles vary between Swing, GTK and Qt. Resolve a unique
            # activatable control by its observed name in this session.
            request.pop('role')
        comparison = next((k for k in ('equals','contains','absent_name','feedback_pattern','excludes','json') if k in step), None)
        if comparison is None:
            action(request)
            continue
        expected = step[comparison]
        deadline = time.monotonic() + 3
        while True:
            observed = action(request)
            if comparison == 'absent_name':
                actual = [n.get('name') for n in observed['nodes'] if n.get('showing', True)]
                passed = expected not in actual
            elif comparison == 'feedback_pattern':
                previous = set(initial.get('accessibility', '').splitlines())
                actual = '\n'.join(line for line in observed['accessibility'].splitlines() if line not in previous)
                passed = bool(re.search(expected, actual))
            else:
                prop = request['action']
                actual = observed[prop] if prop in ('value','text') else json.dumps(observed, ensure_ascii=False)
                if comparison == 'json':
                    try:
                        passed = json.loads(actual) == expected
                    except ValueError:
                        passed = False
                elif comparison == 'excludes':
                    passed = expected not in actual
                else:
                    passed = actual == expected if comparison == 'equals' else expected in actual
            if passed or time.monotonic() >= deadline:
                break
            time.sleep(.1)
        checks.append({'id': f'step-{index}', 'request': request, 'comparison': comparison,
                       'expected': expected, 'actual': actual, 'passed': passed})
        if not passed:
            break
    return checks


def cli_checks(worker, operation, project, case, followup, runtime, repo):
    env = {'PATH': os.environ.get('PATH','/usr/bin:/bin')}
    from coder_prepared_environment import configure
    project_id = worker.get(operation)['payload']['project_id']
    configure(worker, operation, env, project_id)
    setup = prepare_application(worker, operation, repo, project_id, env, runtime)
    failed = [row for row in setup if not row.get('passed')]
    if failed:
        return [{'id': 'prepare', 'passed': False, 'details': failed}]
    if case.get('kind') in ('http','pocket'):
        import urllib.error
        import urllib.request
        class LocalOnly(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}), LocalOnly())
        service = {'id':'subject','cwd':'.','command':case['command'],'ready_path':case['ready_path']}
        checks = []
        with services(worker, operation, project, [service], env, runtime) as (endpoints, launches):
            if not launches or not all(r['passed'] for r in launches):
                return [{'id':'launch','passed':False,'details':launches}]
            if case['kind'] == 'pocket':
                # The private test browser and the generated server use separate
                # containers. Only the server endpoint crosses their boundary.
                oracle = Path(__file__).with_name('pocket_judge.py')
                scratch = runtime/'observer'
                scratch.mkdir()
                url = endpoints['DAEDALUS_SUBJECT_URL']
                with Sandbox([sys.executable,str(oracle),url,'edit' if followup else 'initial'],
                             cwd=scratch, writable=[scratch], readonly=[oracle], network_urls=[url],
                             environment={'PATH':env['PATH'], 'DAEDALUS_LOOPBACK_ENDPOINTS':'1'},
                             memory_mib=1024, timeout=100) as box:
                    run = subprocess.run(box.args,env=box.env,text=True,capture_output=True,timeout=115)
                if run.returncode:
                    raise RuntimeError('Private Pocket Tasks browser failed: '+run.stderr[-600:]+run.stdout[-600:])
                return json.loads(run.stdout)
            for index, probe in enumerate(case['probes'] + (case['followup_probes'] if followup else [])):
                try:
                    response = client.open(endpoints['DAEDALUS_SUBJECT_URL'] + probe['path'], timeout=10)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    actual = response.read(65536).decode(errors='replace')
                    passed = response.status == probe.get('code', 200)
                    if 'json' in probe:
                        try:
                            passed &= json.loads(actual) == probe['json']
                        except ValueError:
                            passed = False
                    checks.append({'id':f'http-{index}','expected':probe,'exit':response.status,'actual':actual,'passed':passed})
        return checks
    probes = private_probes(case, followup)
    checks = []
    for index, probe in enumerate(probes):
        for name, content in probe.get('files', {}).items():
            safe_relative(project,name).write_text(content)
        command = case['command'] + probe.get('args', [])
        # The broker transports container output and exit status, but does not
        # forward the client process's stdin. Redirect inside the container.
        stdin_path, stderr_path = runtime/f'{index}.stdin', runtime/f'{index}.stderr'
        stdin_path.write_text(probe.get('stdin',''))
        wrapper = ['sh','-c','input=$1; errors=$2; shift 2; exec "$@" <"$input" 2>"$errors"',
                   'judge',str(stdin_path),str(stderr_path),*command]
        with Sandbox(wrapper,cwd=project,writable=[project,runtime],readonly=sandbox_paths(project,env),
                     environment=env,timeout=120) as box:
            result = subprocess.run(box.args,env=box.env,text=True,capture_output=True,timeout=135)
        # Ignore broker diagnostics as application stderr. Refuse links and
        # special files produced by an untrusted downloaded project.
        with os.fdopen(os.open(stderr_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Invalid private probe stderr file')
            stderr = stream.read(16384).decode(errors='replace')
        passed = result.returncode != 0 if probe.get('nonzero') else result.returncode == probe.get('code',0)
        if 'stdout' in probe:
            passed &= result.stdout == probe['stdout']
        if 'json' in probe:
            try:
                passed &= json.loads(result.stdout) == probe['json']
            except ValueError:
                passed = False
        if probe.get('stderr_nonempty'):
            passed &= bool(stderr.strip())
        if 'stderr_contains' in probe:
            passed &= probe['stderr_contains'].lower() in stderr.lower()
        checks.append({'id': f'probe-{index}', 'passed': passed, 'expected': probe, 'exit': result.returncode,
                       'stdout': result.stdout[-3000:], 'stderr': stderr[-3000:]})
    return checks


def evaluate(root, job, case, followup=False):
    import config
    from hephaestus_scope import scope
    root = Path(root).resolve()
    identity = 'eval-' + uuid.uuid4().hex
    directory = root / 'assertions' / identity
    directory.mkdir(parents=True)
    artifact = job['candidate_artifact']
    source = Path(artifact['storage_path'])
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != artifact['sha256']:
        raise ValueError('Candidate archive hash mismatch')
    project = directory / 'source'
    project.mkdir()
    with tarfile.open(source) as archive:
        archive.extractall(project, filter='data')
    settings = config.CONTEXT_SETTINGS
    repo = Repository(project, directory/'repository', settings['daedalus_exclude_dirs'])
    repo.refresh()
    snapshot = repo.snapshot('Private verification of delivered archive')
    store = WorkerStore(root/'evaluator-worker')
    # The lock is released by the OS on hard interruption. Only the holder
    # owns this private evaluator store. Never reconcile production operations.
    with (root/'evaluator.lock').open('a') as lock, scope(owner='qualification-judge',project=job['project_id']):
        fcntl.flock(lock, fcntl.LOCK_EX)
        with store.connect() as db:
            db.execute("UPDATE operations SET status='interrupted',ended=? WHERE kind='check' AND status IN ('queued','running')", (time.time(),))
        removed = []
        if os.environ.get('HEPHAESTUS_STORE_ROLE') == 'test' and (root/'qualification-judge').is_file():
            # Each archive must run in a fresh private environment. Old
            # installs can fill the judge's quota even with free host space.
            # The worker collector honors leases and retains logs and source.
            from coder_engine_cache import collect
            removed = collect(store, limit_bytes=0)
        store.create(identity, job['id'], 'check', {'settings': settings, 'seconds_remaining': 400,
                     'calls_remaining': 0, 'revision_id': snapshot['revision'], 'project_id': job['project_id'],
                     'engine': 'openhands159', 'engine_behavior_version': 1})
        store.update(identity, status='running', started=time.time())
        if removed:
            store.event(identity, 'evaluator_cache_collection', removed=removed)
        status = 'interrupted'
        try:
            if case['family'] == 'core' or case.get('kind') in ('http','pocket'):
                runtime = directory/'runtime'
                runtime.mkdir()
                checks = cli_checks(store, identity, project, case, followup, runtime, repo)
            else:
                with session(store, identity, repo) as action:
                    try:
                        checks = ui_checks(action, case, followup)
                    except UIObservationFailure as error:
                        details = error.details
                        if not (details.get('category') in {'ambiguous_target','target_not_found','unsupported_action'} or
                                'Locator.' in str(error) and ('Timeout' in str(error) or 'strict mode violation' in str(error))):
                            raise
                        checks = [{'id':'required-control','passed':False,'actual':str(error),'diagnostic':details}]
            passed = bool(checks) and all(row['passed'] is True for row in checks)
            status = 'succeeded' if passed else 'failed'
            return {'passed': passed, 'archive_sha256': digest, 'checks': checks}
        except Exception:
            status = 'failed'
            raise
        finally:
            store.update(identity, status=status, ended=time.time())


def main():
    """Run on an isolated Linux judge. JSON metadata precedes the archive bytes."""
    import argparse
    import tempfile
    import config
    from context_policy import DEFAULTS
    from engine_cases import CASES, DEVELOPMENT
    from engine_cases_everyday import CASES as EVERYDAY, POCKET, REHEARSAL, breadth_cases
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--settings', type=Path, required=True)
    args = parser.parse_args()
    root = args.store.resolve()
    if os.environ.get('HEPHAESTUS_STORE_ROLE') != 'test' or not (root/'qualification-judge').is_file():
        raise ValueError('A separate marked qualification store is required')
    request = json.loads(sys.stdin.buffer.readline(16384))
    if not re.fullmatch(r'cw3-[a-f0-9]+', request['job_id']):
        raise ValueError('Invalid submitted job identity')
    case = next(c for c in DEVELOPMENT + CASES + EVERYDAY + POCKET + REHEARSAL + breadth_cases() if c['id']==request['case'])
    config.CONTEXT_SETTINGS = {**DEFAULTS, **json.loads(args.settings.read_text())}
    limit = config.CONTEXT_SETTINGS['daedalus_upload_mb']*1024**2
    with tempfile.NamedTemporaryFile(dir=root) as archive:
        size = 0
        while chunk := sys.stdin.buffer.read(1024*1024):
            size += len(chunk)
            if size > limit:
                raise ValueError('Downloaded archive exceeds qualification limit')
            archive.write(chunk)
        archive.flush()
        job = {'id':request['job_id'], 'project_id':request['project_id'],
               'candidate_artifact':{'sha256':request['sha256'], 'storage_path':archive.name}}
        print(json.dumps(evaluate(root,job,case,request['followup'])))


if __name__=='__main__':
    main()
