"""Policy-6 execution profiles. Discovery is independent of preview support.

Explicit commands and .daedalus.json use {packages: {'.': {test: '...'}}}.
Commands are ordinary shell commands run inside Codebox, never on the API host.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import tomllib

from coder_repository import safe_relative

MANIFESTS = {'package.json', 'pyproject.toml', 'requirements.txt', 'setup.py',
             'Cargo.toml', 'go.mod', 'pom.xml', 'build.gradle', 'build.gradle.kts',
             'CMakeLists.txt', 'Makefile', 'composer.json', 'Gemfile', '.daedalus.json'}
NODE_SERVERS = {'express', 'fastify', 'koa', 'hapi', '@hapi/hapi', 'restify'}
PHASES = ('setup', 'build', 'lint', 'typecheck', 'test', 'launch')


def validate_commands(value):
    if not isinstance(value, dict) or set(value) - {'packages'}:
        raise ValueError('Execution commands need a packages object')
    packages = value.get('packages', {})
    if not isinstance(packages, dict):
        raise ValueError('Execution packages must be an object')
    for path, phases in packages.items():
        if not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError('Execution package must be project-relative')
        if not isinstance(phases, dict) or set(phases) - set(PHASES):
            raise ValueError('Unknown execution phase')
        for command in phases.values():
            validate_command(command)
    return value


def validate_command(command):
    if not isinstance(command, str) or not command.strip():
        raise ValueError('An execution command must be nonempty text')
    parsed = subprocess.run(['bash', '-n', '-c', command], capture_output=True, text=True)
    if parsed.returncode:
        raise ValueError('Invalid command syntax: ' + parsed.stderr.strip())


def python_test_kind(path):
    """Examples are not gates. Executable assertion scripts are not unittest."""
    try:
        tree = ast.parse(path.read_bytes())
    except (SyntaxError, UnicodeError):
        return 'invalid'
    if any(isinstance(n, ast.ClassDef) and any(
            isinstance(b, ast.Attribute) and b.attr == 'TestCase' or
            isinstance(b, ast.Name) and b.id == 'TestCase' for b in n.bases)
           for n in ast.walk(tree)):
        return 'unittest'
    if any(isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and
           any(isinstance(v, ast.Name) and v.id == '__name__' for v in ast.walk(n.test)) and
           any(isinstance(v, ast.Call) for child in n.body for v in ast.walk(child)) for n in tree.body):
        return 'script'
    if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith('test_')
           for n in ast.walk(tree)):
        return 'pytest'
    if any(isinstance(n, ast.Assert) for n in ast.walk(tree)):
        return 'script'
    return None


def discover(repository, explicit=None, proposals=None):
    repository.refresh()
    with repository.connect() as db:
        paths = {row[0] for row in db.execute('SELECT path FROM files')}
    packages = {str(Path(p).parent) for p in paths
                if Path(p).name in MANIFESTS or Path(p).suffix in {'.csproj', '.fsproj', '.sln'}}
    # A nested static application does not disappear when its parent has tooling.
    # ...unless a parent Node server serves it: launching public/ beside the Express app that owns it
    # is a redundant second service.
    def served_by_parent(folder):
        for parent in Path(folder).parents:
            manifest = repository.root / parent / 'package.json'
            if str(parent) in packages and manifest.is_file():
                try:
                    data = json.loads(manifest.read_text(errors='replace') or '{}')
                except ValueError:
                    continue
                if set({**data.get('dependencies', {}), **data.get('devDependencies', {})}) & NODE_SERVERS:
                    return True
        return False
    packages.update(folder for folder in {str(Path(p).parent) for p in paths if Path(p).name == 'index.html'}
                    if folder in packages or not served_by_parent(folder))
    if not packages:
        packages.add('.')
    config = {}
    config_error = None
    if '.daedalus.json' in paths and (repository.root / '.daedalus.json').read_text(errors='replace').strip():
        try:
            config = validate_commands(json.loads((repository.root / '.daedalus.json').read_text())).get('packages', {})
        except (ValueError, TypeError) as error:
            config_error = str(error)
    explicit = validate_commands(explicit or {}).get('packages', {})
    proposed = validate_commands(proposals or {}).get('packages', {})
    packages.update(config)
    packages.update(explicit)
    # A project with a native test runner keeps it: a .daedalus.json wrapper script (models copy the
    # documentation example) hides the real suite and yields no observable assertions.
    from coder_native_checks import native_runner
    for path, phases in list(config.items()):
        base = repository.root / path
        native = any((base / name).exists() for name in ('CMakeLists.txt', 'pom.xml', 'go.mod', 'Cargo.toml', 'build.gradle',
                     'build.gradle.kts')) or any(base.glob('*.sln')) or any(base.glob('*.csproj'))
        command = phases.get('test') if isinstance(phases, dict) else None
        if native and isinstance(command, str) and not native_runner(command) and 'DAEDALUS_AUDIT_PYTHON' not in command:
            phases.pop('test')
    # The controller configures CMake into ./build; a bare `ctest` from the project root finds no tests.
    for path, phases in [*config.items(), *explicit.items(), *proposed.items()]:
        base = repository.root / path
        if isinstance(phases, dict) and (base / 'CMakeLists.txt').exists():
            for phase, command in list(phases.items()):
                if isinstance(command, str) and re.fullmatch(r'\s*ctest(\s+(?!--test-dir)\S+)*\s*', command) and '--test-dir' not in command:
                    phases[phase] = command.strip().replace('ctest', 'ctest --test-dir build --output-on-failure', 1)
    packages.update(proposed)
    profiles, checks, web = [], [], []
    for package in sorted(packages):
        root = safe_relative(repository.root, package)
        if not root.is_dir():
            raise ValueError(f'Execution package does not exist: {package}')
        own = sorted(p for p in paths if Path(p).is_relative_to(Path(package)) and not any(
            other != package and Path(other).is_relative_to(Path(package)) and
            Path(p).is_relative_to(Path(other)) for other in packages))
        commands, runners, toolchains = {}, {}, []
        launch = None
        def add(phase, command, runner=''):
            commands.setdefault(phase, []).append(command)
            if runner:
                runners[command] = runner
        # A zero-byte package.json is an editor placeholder, not a Node package.
        if (root / 'package.json').is_file() and (root / 'package.json').read_text(errors='replace').strip():
            toolchains += ['node', 'npm']
            try:
                manifest = json.loads((root / 'package.json').read_text())
                scripts = manifest.get('scripts', {})
                if not isinstance(scripts, dict):
                    raise ValueError('scripts must be an object')
            except (ValueError, AttributeError):
                manifest = {}
                scripts = {}
                add('build', 'python3 -m json.tool package.json')
            manager = 'pnpm' if (root / 'pnpm-lock.yaml').exists() else 'yarn' if (root / 'yarn.lock').exists() else 'npm'
            toolchains.append(manager)
            install = f'{manager} install --frozen-lockfile' if manager != 'npm' else 'npm ci' if (root / 'package-lock.json').exists() else 'npm install --package-lock=false'
            add('setup', install)
            for phase in ('build', 'lint', 'typecheck', 'test'):
                if phase in scripts:
                    command = f'{manager} run {phase}'
                    if phase == 'test' and re.search(r'\bvitest\b', scripts[phase]) and not re.search(r'\brun\b|--run', scripts[phase]):
                        command += ' -- --run'
                    add(phase, command, scripts[phase])
            dependencies = {**manifest.get('dependencies', {}), **manifest.get('devDependencies', {})}
            web_runtime = bool(set(dependencies) & {'vite', 'next', 'react', 'vue', 'svelte', 'astro', 'express', 'fastify', 'koa'})
            # A server's dev script is usually a file watcher (nodemon); `start` is the documented way to run it.
            if 'start' in scripts and set(dependencies) & NODE_SERVERS:
                launch = f'PORT={{port}} HOST=127.0.0.1 {manager} run start'
            elif 'dev' in scripts and web_runtime:
                flags = '--hostname' if 'next' in scripts['dev'] else '--host'
                launch = f'{manager} run dev -- {flags} 127.0.0.1 --port {{port}}'
            elif 'start' in scripts:
                launch = f'PORT={{port}} HOST=127.0.0.1 {manager} run start' if web_runtime else f'{manager} run start'
        pyfiles = [p for p in own if p.endswith('.py')]
        if pyfiles or any((root / p).exists() for p in ('pyproject.toml', 'requirements.txt', 'setup.py')):
            toolchains.append('python3')
            tests = [(str(Path(p).relative_to(package)), python_test_kind(repository.root / p))
                     for p in pyfiles if Path(p).name.startswith('test_') or Path(p).name.endswith('_test.py') or Path(p).name in {'test.py','tests.py'}]
            setup = 'python3 -m venv .venv'
            project_config = {}
            if (root / 'pyproject.toml').exists():
                try:
                    project_config = tomllib.loads((root / 'pyproject.toml').read_text())
                except ValueError:
                    add('build', "python3 -c \"import tomllib; tomllib.load(open('pyproject.toml','rb'))\"")
            if 'build-system' in project_config or (root / 'setup.py').exists():
                setup += ' && .venv/bin/python -m pip install -e .'
            if (root / 'requirements.txt').exists():
                setup += ' && .venv/bin/python -m pip install -r requirements.txt'
            if any(kind == 'pytest' for _, kind in tests):
                setup += ' && .venv/bin/python -m pip install pytest'
            add('setup', setup)
            add('build', '.venv/bin/python -m compileall -q -x "(^|/)(\\.venv|venv|node_modules)/" .')
            for path, kind in tests:
                if kind in {'unittest', 'pytest'}:
                    add('test', '.venv/bin/python -m ' + ('pytest -q ' if kind == 'pytest' else 'unittest -v ') + shlex.quote(path))
                elif kind in {'script', 'invalid'}:
                    # Browser automation exists only in the controller interpreter, never a fresh project venv.
                    source = (root / path).read_text(errors='replace') if (root / path).is_file() else ''
                    runner = '"$DAEDALUS_AUDIT_PYTHON" ' if re.search(r'^\s*(?:from|import)\s+playwright\b', source, re.M) else '.venv/bin/python '
                    add('test', runner + shlex.quote(path))
        if (root / 'Cargo.toml').exists():
            toolchains.append('cargo'); add('build', 'cargo build --workspace'); add('test', 'cargo test --workspace')
        if (root / 'go.mod').exists():
            toolchains.append('go'); add('build', 'go build ./...'); add('test', 'go test -v ./...')
        if (root / 'pom.xml').exists():
            # Classes must exist in the fresh execution copy before a read-only audit launches them.
            toolchains.append('mvn'); add('build', 'mvn -q -DskipTests package'); add('test', 'mvn test')
        if (root / 'build.gradle').exists() or (root / 'build.gradle.kts').exists():
            gradle = './gradlew' if (root / 'gradlew').exists() else 'gradle'
            toolchains.append('java'); add('build', gradle + ' -q classes'); add('test', gradle + ' test')
        dotnet = sorted(p.name for p in root.iterdir() if p.suffix in {'.sln', '.csproj', '.fsproj'})
        if dotnet:
            toolchains.append('dotnet')
            target = shlex.quote(next((p for p in dotnet if p.endswith('.sln')), dotnet[0]))
            add('setup', f'dotnet restore {target}'); add('build', f'dotnet build {target} --no-restore')
            # Only a solution or a project that references a test SDK has tests; running
            # `dotnet test` on the application project reports zero tests and would fail the gate.
            texts = ''.join((root / name).read_text(errors='replace') for name in dotnet if not name.endswith('.sln'))
            if target.strip("'\"").endswith('.sln') or re.search(r'Microsoft\.NET\.Test\.Sdk|xunit|nunit|MSTest', texts, re.I):
                add('test', f'dotnet test {target} --no-build')
        if (root / 'CMakeLists.txt').exists():
            toolchains += ['cmake', 'ctest']
            add('setup', 'cmake -S . -B build'); add('build', 'cmake --build build'); add('test', 'ctest --test-dir build --output-on-failure')
        elif (root / 'Makefile').exists():
            toolchains.append('make'); add('build', 'make')
            if re.search(r'^test\s*:', (root / 'Makefile').read_text(), re.M):
                add('test', 'make test')
        if (root / 'composer.json').exists():
            toolchains += ['php', 'composer']; add('setup', 'composer install --no-interaction')
            try:
                scripts = json.loads((root / 'composer.json').read_text()).get('scripts', {})
                for phase in ('test', 'lint', 'build'):
                    if phase in scripts:
                        add(phase, f'composer run {phase}')
                if 'test' not in scripts and (root / 'phpunit.xml').exists():
                    add('test', 'vendor/bin/phpunit')
            except ValueError:
                add('build', 'composer validate')
        if (root / 'Gemfile').exists():
            toolchains += ['ruby', 'bundle']; add('setup', 'bundle install')
            if (root / 'spec').is_dir():
                add('test', 'bundle exec rspec')
            elif (root / 'Rakefile').exists():
                add('test', 'bundle exec rake test')
        # README startup recipes can identify a server without a Node manifest.
        if not launch:
            launch = documented_launch(root)
        # Preview classification is separate from the presence of any manifest.
        if not launch and (root / 'index.html').is_file():
            launch = 'python3 -m http.server {port} --bind 127.0.0.1'
        if not (root / 'package.json').exists():
            for p in own:
                if Path(p).suffix in {'.js', '.mjs', '.cjs'}:
                    relative = str(Path(p).relative_to(package))
                    add('build', 'node --check ' + shlex.quote(relative))
                    if re.search(r'(^test[_.-]|\.test\.)', Path(p).name):
                        add('test', 'node --test ' + shlex.quote(relative))
        origins = {phase: 'manifest' for phase in commands}
        for source, origin in ((proposed, 'proposed'), (config, 'configuration'), (explicit, 'user')):
            for phase, command in source.get(package, {}).items():
                if origin == 'proposed':
                    # Only a repository-grounded fallback; never override discovered commands.
                    words = shlex.split(command)
                    references = [w for w in words[1:] if not w.startswith('-') and (root / w).is_file()]
                    if not references:
                        raise ValueError('Proposed command must reference an existing repository file')
                    if phase in commands or phase == 'launch' and launch:
                        continue
                if phase == 'launch':
                    launch = command
                else:
                    commands[phase] = [command]; origins[phase] = origin
        profile = {'cwd': package, 'toolchains': sorted(set(toolchains)), 'commands': commands, 'launch': launch}
        profiles.append(profile)
        for phase in PHASES[:-1]:
            for i, command in enumerate(commands.get(phase, [])):
                checks.append({'id': f'{package}:{phase}:{i}', 'cwd': package, 'command': command,
                               'phase': phase, 'origin': 'project', 'command_origin': origins[phase],
                               'setup': phase == 'setup', 'is_test': phase == 'test', 'test_runner': runners.get(command, command)})
        if launch and '{port}' in launch:
            web.append(package)
            checks.append({'id': f'{package}:launch', 'cwd': package, 'kind': 'browser', 'phase': 'launch',
                           'origin': 'project', 'server_command': launch, 'steps': [], 'path': '/'})
        elif launch:
            checks.append({'id':f'{package}:launch', 'cwd':package, 'command':launch, 'phase':'launch', 'origin':'project'})
    if config_error:
        checks.append({'id': 'execution-config', 'command': 'exit 1', 'error': config_error, 'origin': 'project'})
    return {'profiles': profiles, 'checks': checks, 'packages': sorted(packages), 'web_packages': web}


def environment_key(root, profile):
    """Dependency cache identity includes manifests, lockfiles and tool versions."""
    manifests = {}
    for path in root.iterdir():
        if path.is_file() and (path.name in MANIFESTS or 'lock' in path.name.lower() or path.suffix in {'.csproj', '.fsproj', '.sln'}):
            manifests[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    versions = {}
    for tool in profile.get('toolchains', []):
        try:
            result = subprocess.run([tool, '--version'], capture_output=True, timeout=5)
            versions[tool] = (result.stdout + result.stderr).decode(errors='replace')
        except (OSError, subprocess.TimeoutExpired) as error:
            versions[tool] = str(error)
    key = hashlib.sha256(json.dumps({'manifests': manifests, 'versions': versions,
        'setup': profile.get('commands', {}).get('setup', [])}, sort_keys=True).encode()).hexdigest()
    return key, versions


def documented_launch(root):
    """Ground a small set of ordinary startup recipes in existing source files.

    Unrecognized recipes remain available through explicit execution commands;
    prose and guessed module names never start a controller-managed service.
    """
    root = Path(root)
    for readme in sorted(root.glob('README*')):
        if not readme.is_file(): continue
        for line in readme.read_text(errors='replace').splitlines():
            command = line.strip().strip('`').removeprefix('$ ').strip()
            try: words = shlex.split(command)
            except ValueError: continue
            if not words or any(token in words for token in ('&&',';','|','>','&')): continue
            runner = words[0]
            offset = 1
            if runner in {'python','python3'} and words[1:3] == ['-m','uvicorn']:
                runner,offset = 'uvicorn',3
            if runner == 'uvicorn' and len(words)>offset and re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*',words[offset]):
                module = words[offset].split(':')[0].replace('.','/')
                if not (root/(module+'.py')).is_file(): continue
                # Controller supplies bind address and port; app import stays exactly documented.
                return '.venv/bin/python -m uvicorn ' + shlex.quote(words[offset]) + ' --host 0.0.0.0 --port {port}'
            if runner == 'node' and len(words)==2 and not Path(words[1]).is_absolute() and '..' not in Path(words[1]).parts and (root/words[1]).is_file() and words[1].endswith(('.js','.mjs','.cjs')):
                source=(root/words[1]).read_text(errors='replace')
                if re.search(r'\.listen\s*\(',source) and 'process.env.PORT' in source:
                    return 'PORT={port} HOST=127.0.0.1 node ' + shlex.quote(words[1])
    return None
