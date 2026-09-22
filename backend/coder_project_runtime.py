"""Policy-7 experimental execution: package ordering and managed services.

Ordinary commands and native tests remain the interface. Every verification run
uses a fresh immutable source copy, never the editor's mutable working tree.
"""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import threading
import urllib.error
import urllib.request

from coder_native_checks import PYTHON_TRACE, compiled_count, native_count, native_runner, provenance, report_count
from coder_patch_runtime import tree_hashes, stop_process, readonly_command
from coder_profiles import discover, environment_key, validate_command
from coder_repository import safe_relative, file_hash


def contract(repository, explicit=None):
    result = discover(repository, explicit)
    path = repository.root / '.daedalus-run.json'
    # A zero-byte manifest is an editor placeholder, not a (broken) configuration.
    extension = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
    if set(extension) - {'dependencies', 'services'}:
        raise ValueError('Unknown execution contract field')
    packages = {p['cwd']: p for p in result['profiles']}
    dependencies = extension.get('dependencies', {})
    if not isinstance(dependencies, dict) or set(dependencies) - packages.keys():
        raise ValueError('Dependencies must identify discovered packages')
    ordered, visiting = [], set()
    def visit(name):
        if name in ordered:
            return
        if name in visiting or name not in packages:
            raise ValueError('Cyclic or unknown package dependency')
        visiting.add(name)
        deps = dependencies.get(name, [])
        if not isinstance(deps, list):
            raise ValueError('Package dependencies must be a list')
        for parent in deps:
            visit(parent)
        visiting.remove(name)
        ordered.append(name)
    for name in packages:
        visit(name)
    result['profiles'] = [packages[name] for name in ordered]
    result['checks'] = sorted((c for c in result['checks'] if c.get('phase') != 'launch' or c.get('command')),
        key=lambda c: (['setup', 'build', 'lint', 'typecheck', 'test', 'launch'].index(c.get('phase', 'build')), ordered.index(c.get('cwd', '.'))))
    services = extension.get('services')
    # An explicit empty list must not switch off the launch and page checks of an app that serves HTTP.
    if not services:
        services = [{'id': 'app' if i == 0 else f'app{i}', 'cwd': p['cwd'], 'command': p['launch'], 'ready_path': '/'}
                    for i, p in enumerate(result['profiles']) if p.get('launch') and '{port}' in p['launch']]
    if not isinstance(services, list) or any(not isinstance(service, dict) for service in services):
        raise ValueError('services must be a list of service objects')
    known = set()
    for service in services:
        name = service.get('id', '')
        if not re.fullmatch(r'[a-z][a-z0-9_]*', name) or name in known:
            raise ValueError('Service needs a unique lowercase ID')
        known.add(name)
        if service.get('cwd', '.') not in packages:
            raise ValueError('Service must belong to a discovered package')
        validate_command(service.get('command', ''))
        if '{port}' not in service['command']:
            raise ValueError('A long-running service must use the controller {port}')
        if not str(service.get('ready_path', '/')).startswith('/'):
            raise ValueError('Service readiness must be a relative URL path')
    result['services'] = services
    return result


def command_env(root, audit, evidence, instrumentation):
    temporary = audit / 'tmp'; temporary.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('OPENAI_', 'ANTHROPIC_', 'AIDER_', 'PIP_', 'PYTHON'))
           and k not in {'VIRTUAL_ENV', 'NODE_OPTIONS', 'NPM_CONFIG_PREFIX', 'npm_config_prefix'}}
    env.update(DAEDALUS_PROJECT_ROOT=str(root), DAEDALUS_AUDIT_DIR=str(audit),
        DAEDALUS_AUDIT_PYTHON=sys.executable,
        DAEDALUS_PROVENANCE=str(evidence), NODE_V8_COVERAGE=str(evidence),
        PYTHONPATH=os.pathsep.join([str(instrumentation), str(root)]), PYTHONDONTWRITEBYTECODE='1',
        APP_DB_PATH=str(audit / 'application.sqlite'), TMPDIR=str(temporary),
        # Audits run with a read-only filesystem: tool caches must live in their writable tmp.
        GOCACHE=str(temporary / 'gocache'), DOTNET_CLI_HOME=str(temporary / 'dotnet'), DOTNET_NOLOGO='1',
        DOTNET_CLI_TELEMETRY_OPTOUT='1',
        PATH=os.pathsep.join([str(root / '.venv/bin'), env.get('PATH', '')]))
    return env


class CommandTimeout(TimeoutError):
    """One project command outlived daedalus_command_seconds. The job's own allowance is a plain
    TimeoutError and must keep propagating; a hanging test is a failed check a repair can read."""


def execute(store, op_id, command, cwd, env, log, writable=None):
    from coder_worker_runtime import _boundary
    operation = _boundary(store, op_id)
    deadline = time.monotonic() + operation['payload']['settings']['daedalus_command_seconds']
    with log.open('w') as output:
        args = ['bash', '-c', command]
        if writable is not None:
            args = readonly_command(args, writable)
        process = subprocess.Popen(args, cwd=cwd, env=env,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while process.poll() is None:
                _boundary(store, op_id)
                if time.monotonic() >= deadline:
                    raise CommandTimeout('Project command timed out')
                time.sleep(.1)
        finally:
            stop_process(process)
    return process.returncode


def uninstrumented(environment):
    """Setup/build/server work is execution, not an assertion trace."""
    return {k: v for k, v in environment.items() if k not in {
        'PYTHONPATH', 'NODE_V8_COVERAGE', 'DAEDALUS_PROVENANCE'}}


def prepare_environment(store, op_id, root, profile, project_id, env, folder):
    """Same cache for editing/checks/follow-ups; editable installs always refresh."""
    cwd = safe_relative(root, profile['cwd'])
    key, versions = environment_key(cwd, profile)
    scope = hashlib.sha256(project_id.encode()).hexdigest()
    cache = store.root / 'project-environments' / scope / key
    cache.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, original in enumerate(profile['commands'].get('setup', [])):
        command = original
        marker = cache / (hashlib.sha256(command.encode()).hexdigest() + '.json')
        if command.startswith('python3 -m venv .venv'):
            if not (cache / '.venv').exists():
                try:
                    subprocess.run(['python3', '-m', 'venv', str(cache / '.venv')], check=True, capture_output=True,
                                   timeout=store.get(op_id)['payload']['settings']['daedalus_command_seconds'])
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError) as error:
                    # The sandbox could not create an environment: a failed setup row, not a failed operation.
                    shutil.rmtree(cache / '.venv', ignore_errors=True)
                    rows.append({'id': profile['cwd'] + ':setup:' + str(i), 'phase': 'setup', 'command': original, 'passed': False,
                        'exit_code': 124 if isinstance(error, subprocess.TimeoutExpired) else 1, 'environment_reused': False,
                        'toolchains': versions, 'log_tail': 'Could not create the Python environment: ' + str(error)[:500],
                        'environment_fault': True})
                    break
            command = command.replace('python3 -m venv .venv', 'true', 1)
        for name in ('.venv', 'node_modules', 'vendor'):
            target = cwd / name
            if (cache / name).exists() and not target.exists():
                target.symlink_to(cache / name, target_is_directory=True)
        reusable = marker.exists() and not re.search(r'(?:-e\s|--editable|cmake|dotnet)', original)
        log = folder / f'setup-{hashlib.sha256(profile["cwd"].encode()).hexdigest()[:10]}-{i}.log'
        try:
            code = 0 if reusable else execute(store, op_id, command, cwd, uninstrumented(env), log)
            stalled = False
        except CommandTimeout:
            # A dependency install that never returns is the sandbox's problem, not the application's.
            code, stalled = 124, True
        tail = log.read_text(errors='replace')[-4000:] if log.exists() else 'Dependency environment reused'
        if stalled:
            tail = (tail + '\nThe setup command did not finish within the command time limit.').strip()
        row = {'id': profile['cwd'] + ':setup:' + str(i), 'phase': 'setup', 'command': original,
            'passed': code == 0, 'exit_code': code, 'environment_reused': reusable, 'toolchains': versions,
            'log': str(log), 'log_tail': tail, 'environment_fault': stalled or code == 127 or bool(re.search(
                'ENOTFOUND|EAI_AGAIN|Temporary failure|Network is unreachable|Connection refused', tail, re.I))}
        rows.append(row)
        if code:
            break
        for name in ('.venv', 'node_modules', 'vendor'):
            target = cwd / name
            if target.is_dir() and not target.is_symlink() and not (cache / name).exists():
                shutil.copytree(target, cache / name, symlinks=True)
        marker.write_text(json.dumps({'command': original, 'toolchains': versions}))
    return rows


def from_browser(headers):
    """A real browser request. Every Chromium request (navigation, fetch, subresource) to a
    loopback origin carries Fetch Metadata; a Python client that only sets a Mozilla User-Agent
    does not, and that alone used to manufacture the requested `browser` test interface."""
    return 'Mozilla/' in (headers.get('User-Agent') or '') and bool(headers.get('Sec-Fetch-Mode'))


@contextmanager
def services(store, op_id, root, definitions, environment, folder):
    from coder_worker_runtime import _boundary
    processes, rows, streams, proxies = [], [], [], []
    env = dict(environment)
    try:
        for service in definitions:
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            url = f'http://127.0.0.1:{port}'
            log = folder / ('service-' + service['id'] + '.log')
            stream = log.open('w'); streams.append(stream)
            command = service['command'].replace('{port}', str(port))
            process = subprocess.Popen(['bash', '-c', command], cwd=safe_relative(root, service.get('cwd', '.')),
                env=uninstrumented(env), stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            processes.append(process)
            ready, started = False, time.monotonic()
            limit = store.get(op_id)['payload']['settings']['daedalus_command_seconds']
            while process.poll() is None and time.monotonic() - started < min(30, limit):
                _boundary(store, op_id)
                try:
                    with urllib.request.urlopen(url + service.get('ready_path', '/'), timeout=1) as response:
                        ready = response.status < 400
                except OSError:
                    pass
                if ready:
                    break
                time.sleep(.1)
            traffic = []
            # Clients use a controller-owned forwarding URL. Traffic provenance
            # must not depend on whether Express or another framework logs HTTP.
            def handler(target, observed):
                class Proxy(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass
                    def forward(self):
                        data = self.rfile.read(int(self.headers.get('Content-Length', 0))) or None
                        request = urllib.request.Request(target + self.path, data=data, method=self.command,
                            headers={k: v for k, v in self.headers.items() if k.lower() not in {'host', 'connection', 'content-length'}})
                        try:
                            response = urllib.request.urlopen(request, timeout=15)
                        except urllib.error.HTTPError as error:
                            response = error
                        except OSError:
                            self.send_error(502)
                            return
                        with response:
                            body = response.read()
                            observed.append({'method': self.command, 'path': self.path, 'status': response.status,
                                             'browser': from_browser(self.headers)})
                            self.send_response(response.status)
                            for key, value in response.headers.items():
                                if key.lower() not in {'transfer-encoding', 'connection', 'content-length'}:
                                    self.send_header(key, value)
                            self.send_header('Content-Length', str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = forward
                return Proxy
            proxy = ThreadingHTTPServer(('127.0.0.1', 0), handler(url, traffic))
            proxy.daemon_threads = True
            thread = threading.Thread(target=proxy.serve_forever, daemon=True); thread.start()
            proxies.append((proxy, thread))
            public_url = f'http://127.0.0.1:{proxy.server_port}'
            env['DAEDALUS_' + service['id'].upper() + '_URL'] = public_url
            env['PORT'] = str(port)
            launch_tail = log.read_text(errors='replace')[-4000:]
            rows.append({'id': 'launch:' + service['id'], 'phase': 'launch', 'passed': ready,
                         'cwd': service.get('cwd', '.'), 'url': public_url, 'command': command,
                         'log': str(log), 'log_tail': launch_tail,
                         'environment_fault': host_fault(process.poll(), launch_tail),
                         'execution_succeeded': ready, 'traffic': traffic})
            if not ready:
                break
            # Up is not working: open the page once and fail on uncaught script errors (direct URL, not the proxy).
            from coder_page_guard import page_row
            settings = store.get(op_id)['payload']['settings']
            loaded = page_row(service, url, settings)
            if loaded:
                rows.append(loaded)
            if loaded and loaded['passed']:
                # Loading is not working: submit the page's own forms and fail on a rejected write (direct URL).
                from coder_frontend_guard import form_row
                submitted = form_row(service, url, settings)
                if submitted:
                    rows.append(submitted)
        yield env, rows
    finally:
        for proxy, thread in proxies:
            proxy.shutdown(); proxy.server_close(); thread.join(timeout=2)
        for process in reversed(processes):
            stop_process(process)
        for stream in streams:
            stream.close()


# Native runners whose programs a Python/Node tracer cannot follow. The runner's own report of
# executed tests is the observation; the package sources it compiled are the provenance.
_JVM, _C = ('.java', '.kt', '.kts', '.scala'), ('.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hh')
COMPILED_RUNNERS = {'mvn': _JVM, 'gradle': _JVM, 'gradlew': _JVM, 'go': ('.go',), 'cargo': ('.rs',),
                    'dotnet': ('.cs', '.fs', '.vb'), 'ctest': _C}
BUILD_DIRS = {'target', 'build', 'bin', 'obj', 'out', 'dist', '.gradle', 'vendor', 'node_modules', '.venv', '.git',
              'CMakeFiles', 'Testing'}


def launches_project(audit):
    """An audit script really calls subprocess and really reads DAEDALUS_PROJECT_ROOT.

    Words in a comment or docstring are not a launch; this once bound every compiled source file
    to an audit that never touched the program."""
    import ast
    for path in sorted(Path(audit).rglob('*.py')):
        try:
            tree = ast.parse(path.read_text(errors='replace'))
        except (SyntaxError, ValueError):
            continue
        imported = {alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                    and node.module == 'subprocess' for alias in node.names}
        calls = any(isinstance(node, ast.Call) and (
            isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == 'subprocess'
            or isinstance(node.func, ast.Name) and node.func.id in imported) for node in ast.walk(tree))
        reads = any(isinstance(node, ast.Constant) and node.value == 'DAEDALUS_PROJECT_ROOT' for node in ast.walk(tree))
        if calls and reads:
            return True
    return False


def compiled_sources(cwd, root, suffixes):
    found = []
    for path in sorted(cwd.rglob('*')):
        parts = path.relative_to(cwd).parts
        if path.is_file() and path.suffix in suffixes and not BUILD_DIRS & set(parts) and not any(
                part.startswith('cmake-build') for part in parts):
            found.append({'path': path.relative_to(root).as_posix(), 'sha256': file_hash(path)})
    return found[:400]


def compiled_bindings(command, cwd, root):
    """Sources a native compiled test runner built: tests and application, hashed."""
    words = shlex.split(command)
    suffixes = COMPILED_RUNNERS.get(Path(words[0]).name) if words else None
    if not suffixes:
        return []
    found = compiled_sources(cwd, root, suffixes)
    if Path(words[0]).name == 'dotnet':
        # A test project compiles the application through its ProjectReference siblings.
        for project in cwd.glob('*.*proj'):
            for reference in re.findall(r'<ProjectReference\s+Include="([^"]+)"', project.read_text(errors='replace')):
                target = (cwd / reference.replace('\\', '/')).resolve().parent
                if target.is_dir() and target.is_relative_to(root):
                    found.extend(compiled_sources(target, root, suffixes))
    return list({b['path']: b for b in found}.values())[:400]


HOST_TOOLS = {'node', 'npm', 'npx', 'pnpm', 'yarn', 'python', 'python3', 'pip', 'pip3', 'java', 'javac', 'mvn', 'gradle', 'go', 'cargo',
              'rustc', 'dotnet', 'cmake', 'ctest', 'make', 'gcc', 'g++', 'cc', 'c++', 'sh', 'bash', 'bwrap'}


def host_fault(code, tail):
    """Exit 127 is the sandbox's fault only when a toolchain is missing. A package script that names an
    undeclared binary (`nodemon: not found`) is an application defect a repair can fix."""
    if code != 127:
        return False
    missing = re.findall(r'(?:sh|bash): (?:\d+: |line \d+: )?([\w.@/+-]+): (?:command )?not found', tail or '')
    return not missing or any(Path(name).name in HOST_TOOLS for name in missing)


def failure_class(row, baseline=()):
    if row.get('passed'):
        return 'passed'
    if row.get('failure_kind') in {'source_mutation', 'missing_coverage', 'missing_provenance'}:
        return row['failure_kind']
    if row.get('environment_fault'):
        return 'environment'
    if row.get('origin') == 'independent':
        tail = row.get('log_tail', '')
        # A harness failure is not grounds for editing the application.
        if re.search(r"can't open file|No such file or directory|NameError:|SyntaxError:|ImportError:|ModuleNotFoundError:|ERROR collecting", tail):
            return 'audit_defect'
        return 'unresolved'  # Assertion failures need a grounded per-check diagnosis.
    old = next((b for b in baseline if b.get('id') == row.get('id')), None)
    if old and not old.get('passed'):
        from coder_policy7_evidence import failure_signature
        if failure_signature([{**old, 'classification': None}]) == failure_signature([{**row, 'classification': None}]):
            return 'existing_failure'
    return 'application_defect'


def run_revision(store, op_id, repository, profile, *, project_id, audit_source=None, checks=None):
    operation = store.get(op_id)
    payload = operation['payload']
    folder = store.root / 'execution' / op_id
    folder.mkdir(parents=True, exist_ok=True)
    root, audit = folder / 'project', folder / 'audit'
    if root.exists():
        raise ValueError('An execution operation cannot overwrite prior evidence')
    root.mkdir(); audit.mkdir()
    archive = folder / 'source.tar.gz'
    repository.archive(payload['revision_id'], archive)
    with tarfile.open(archive) as bundle:
        bundle.extractall(root, filter='data')
    if audit_source:
        shutil.copytree(audit_source, audit, dirs_exist_ok=True)
    instrumentation = folder / 'instrumentation'; instrumentation.mkdir()
    # Do not resolve paths for every standard-library/pip/pytest execution line.
    trace = PYTHON_TRACE.replace("if event == 'line':", "if event == 'line' and frame.f_code.co_filename.startswith((str(root), str(audit))):")
    (instrumentation / 'sitecustomize.py').write_text(trace)
    evidence = folder / 'bindings'; evidence.mkdir()
    env = command_env(root, audit, evidence, instrumentation)
    excludes = tuple(payload['settings']['daedalus_exclude_dirs'])
    # Native builds generate sources (obj/*.AssemblyInfo.cs, CMakeFiles/*CompilerId.c): those are
    # build output, not a check mutating the application.
    # `vendor` is linked to the dependency cache by prepare_environment: an out-of-tree symlink the link guard would reject.
    generated = {'obj', '.gradle', 'CMakeFiles', 'Testing', 'vendor'}
    if any(repository.root.rglob('*.csproj')) or any(repository.root.rglob('*.sln')):
        generated.add('bin')
    excludes = tuple(dict.fromkeys([*excludes, *sorted(generated)]))
    before = tree_hashes(root, excludes)
    rows = []
    ready_packages = set()
    for package in profile['profiles']:
        setup = prepare_environment(store, op_id, root, package, project_id, env, folder)
        rows.extend(setup)
        if all(r['passed'] for r in setup):
            ready_packages.add(package['cwd'])
    if not profile['profiles']:
        ready_packages.add('.')
    commands = [c for c in profile['checks'] if c.get('phase') != 'setup'] if checks is None else [
        *[c for c in profile['checks'] if c.get('phase') == 'build'], *checks]

    def check(c):
        index = len(rows)
        log = folder / f'command-{index}.log'
        local_evidence = evidence / str(index); local_evidence.mkdir()
        check_env = {**env, 'DAEDALUS_PROVENANCE': str(local_evidence), 'NODE_V8_COVERAGE': str(local_evidence)}
        if not c.get('is_test') and c.get('origin') != 'independent':
            check_env = uninstrumented(check_env)
        cwd = safe_relative(root, c.get('cwd', '.'))
        if c.get('kind') == 'file':
            path = safe_relative(root, c['path'])
            content = path.read_text(errors='replace') if path.is_file() else ''
            passed = path.is_file() and bool(content.strip()) and all(a['kind'] == 'exists' or a['kind'] == 'nonempty' and bool(content.strip()) or
                a['kind'] == 'contains' and a.get('value', '') in content for a in c['assertions'])
            rows.append({**c, 'passed': passed, 'file_bindings': [{'path': c['path'], 'sha256': file_hash(path)}] if path.is_file() else [],
                         'log_tail': '' if passed else 'Requested documentation is absent or incomplete',
                         'classification': 'application_defect' if not passed else 'passed'})
            return
        check_env['PATH'] = str(cwd / '.venv/bin') + os.pathsep + env['PATH']
        command = c['command']
        stale = [name for name, digest in (c.get('audit_hashes') or {}).items()
                 if not (audit / name).is_file() or file_hash(audit / name) != digest]
        if stale:
            # The bytes that were validated are the bytes that run.
            rows.append({**c, 'passed': False, 'exit_code': None, 'classification': 'audit_defect', 'coverage_observed': False,
                         'execution_succeeded': False, 'source_bindings': [], 'service_bindings': [], 'test_files': [], 'test_interfaces': [],
                         'log_tail': 'Audit file changed after it was validated: ' + ', '.join(stale)})
            return
        if c.get('origin') == 'independent':
            # Paths remain controller-owned; ordinary native files live outside source.
            command = command.replace('{audit}', shlex.quote(str(audit))).replace('{project}', shlex.quote(str(root)))
        starts = time.time()
        offsets = [(r, len(r['traffic'])) for r in rows if r.get('phase') == 'launch' and 'traffic' in r]  # page:* guard rows carry none
        limit = store.get(op_id)['payload']['settings']['daedalus_command_seconds']
        try:
            code = execute(store, op_id, command, cwd, check_env, log,
                           writable=[audit, local_evidence] if c.get('origin') == 'independent' else None)
        except CommandTimeout:
            # Blocking the whole operation made Continue hang again for the full limit.
            tail = log.read_text(errors='replace')[-6000:]
            reason = (f'The command did not finish within {limit} seconds: it hangs or waits for input. Tests must end on their own '
                      '(no input(), no foreground server, no endless loop); start services in the background and stop them.')
            rows.append({**c, 'exit_code': 124, 'passed': False, 'timed_out': True, 'log': str(log), 'log_tail': (tail + '\n' + reason).strip(),
                'reason': reason, 'test_count': None, 'assertions_executed': 0, 'coverage_observed': False, 'source_bindings': [],
                'service_bindings': [], 'environment_fault': False, 'test_files': [], 'test_interfaces': [], 'execution_succeeded': False,
                # A project command that hangs is the application's to fix; a hanging audit needs a diagnosis first.
                **({'classification': 'application_defect'} if c.get('origin') != 'independent' else {})})
            return
        tail = log.read_text(errors='replace')[-6000:]
        count = native_count(command, tail)
        if count is None:
            count = report_count(cwd, starts)
        bindings, assertions = provenance(local_evidence, root)
        if not assertions and code == 0:
            # Compiled-language runners cannot be line-traced. Their own report of executed tests
            # is the observation, and the package sources they compiled are the provenance. Only the
            # runner's own summary format counts here (Gradle: its JUnit XML), never a printed number.
            executed = compiled_count(command, tail)
            if executed is None and Path((shlex.split(command) or [''])[0]).name in {'gradle', 'gradlew'}:
                executed = report_count(cwd, starts)
            compiled = compiled_bindings(command, cwd, root) if executed else []
            if compiled:
                bindings, assertions = compiled, executed
        network = []
        for launch, offset in offsets:
            traffic = launch['traffic'][offset:]
            if traffic:
                network.append({'path': launch['cwd'], 'service_id': launch['id'],
                    'revision_id': payload['revision_id'], 'url': launch['url'], 'requests': traffic})
        # A printed runner summary is not proof of executed assertions.
        observed = assertions > 0
        row = {**c, 'exit_code': code, 'passed': code == 0, 'log': str(log), 'log_tail': tail,
            'test_count': count, 'assertions_executed': assertions, 'coverage_observed': observed,
            'source_bindings': bindings, 'service_bindings': network, 'environment_fault': host_fault(code, tail)}
        if c.get('is_test') and (not observed or count == 0):
            row.update(passed=False, failure_kind='missing_coverage')
        targets = set()
        try:
            runner_words = shlex.split(c.get('test_runner') or command)
        except ValueError:
            runner_words = []   # an unbalanced quote in a model-written npm script
        for word in runner_words:
            if c.get('is_test') and word not in {'.', ''} and not word.startswith('-') and not Path(word).is_absolute():
                for path in cwd.glob(word):
                    if path.is_file() and path.suffix in {'.py', '.js', '.ts', '.mjs', '.cjs'}:
                        targets.add(path.relative_to(root).as_posix())
        row['test_files'] = [b for b in bindings if b['path'] in targets or any('test' in p.lower() or 'spec' in p.lower() for p in Path(b['path']).parts)]
        traffic = [request for service in network for request in service['requests']]
        row['test_interfaces'] = [name for name, observed_interface in (
            ('browser', any(r['browser'] for r in traffic)), ('api', any(r['path'].startswith('/api/') for r in traffic))) if observed_interface]
        test_paths = {b['path'] for b in row['test_files']}
        actual = [b for b in bindings if b['path'] not in test_paths]
        if c.get('origin') == 'independent' and observed and code == 0 and not actual and not network:
            # An audit of a compiled program runs it as a subprocess, which a Python tracer cannot
            # follow. It counts only when the audit really launches the project's toolchain.
            if audit and launches_project(audit):
                every = tuple(dict.fromkeys(suffix for group in COMPILED_RUNNERS.values() for suffix in group))
                actual = [b for b in compiled_sources(cwd, root, every)
                          if not any('test' in part.lower() or 'spec' in part.lower() for part in Path(b['path']).parts)]
                if actual:
                    row['source_bindings'] = bindings = actual
                    row['provenance_kind'] = 'compiled_subprocess'
        row['execution_succeeded'] = code == 0 and (c.get('phase') == 'launch' or observed and bool(actual or network))
        if observed and c.get('is_test') and c.get('evidence_types', ['behavior']) != ['documentation'] and not actual and not network:
            row.update(passed=False, failure_kind='missing_provenance')
        rows.append(row)

    for c in commands:
        if c.get('kind') == 'file':
            check(c)
        elif c.get('phase') != 'test' and not c.get('is_test') and c.get('cwd', '.') in ready_packages:
            check(c)
            if not rows[-1]['passed']:
                ready_packages.discard(c.get('cwd', '.'))
    if ready_packages:
        definitions = [service for service in profile['services'] if service.get('cwd', '.') in ready_packages]
        with services(store, op_id, root, definitions, env, folder) as (service_env, launches):
            env = service_env
            rows.extend(launches)
            if all(r['passed'] for r in launches):
                for c in commands:
                    if c.get('kind') != 'file' and c.get('cwd', '.') in ready_packages and (c.get('phase') == 'test' or c.get('is_test')):
                        check(c)
    after = tree_hashes(root, excludes)
    def binary(path):
        try:
            with open(root / path, 'rb') as handle:
                return handle.read(4) in (b'\x7fELF', b'MZ\x90\x00', b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe')
        except OSError:
            return False
    def runtime_data(path):
        # A database the application itself writes when it runs (the builder started the app once, so
        # tasks.db is in the checkpoint). Launching it again is not a check editing source code.
        if re.search(r'\.(?:db|sqlite3?|db3)(?:-(?:wal|shm|journal))?$', path, re.I):
            return True
        try:
            with open(root / path, 'rb') as handle:
                return handle.read(16) == b'SQLite format 3\x00'
        except OSError:
            return False
    # A committed executable that the controller's own build rewrites is build output, not tampering.
    changed = [p for p, value in before.items() if after.get(p) != value and not binary(p) and not runtime_data(p)]
    additions = [p for p in after.keys() - before.keys() if Path(p).suffix in {'.py', '.js', '.ts', '.jsx', '.tsx', '.cs', '.c', '.cpp', '.rb', '.php'}]
    if changed or additions:
        rows.append({'id': 'immutable-source', 'passed': False, 'failure_kind': 'source_mutation', 'paths': changed + additions})
    for i, row in enumerate(rows):
        baseline = payload.get('baseline_checks', payload.get('baseline', []))
        if not isinstance(baseline, list):
            baseline = []
        row.update(revision_id=payload['revision_id'], execution_id=f'{op_id}:{i}',
                   classification=row.get('classification') or failure_class(row, baseline))
        store.event(op_id, 'check', check=row)
    result = {'revision_id': payload['revision_id'], 'checks': rows,
              'passed': bool(rows) and all(r['passed'] for r in rows), 'workspace': str(root), 'audit': str(audit)}
    (folder / 'result.json').write_text(json.dumps(result, indent=2))
    return result
