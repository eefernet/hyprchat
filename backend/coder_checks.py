"""Deterministic package verification and browser evidence for worker snapshots."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import time
from context_policy import DEFAULTS


def validate_plan(answer):
    """Catch invalid check programs before asking the Builder to repair code."""
    milestones = answer.get("milestones")
    if not isinstance(milestones,list) or not milestones:
        raise ValueError("Plan needs a nonempty milestones list")
    for milestone in milestones:
        if not isinstance(milestone,dict) or not isinstance(milestone.get("task"),str) or not milestone["task"].strip():
            raise ValueError("Each milestone needs a task")
        checks = milestone.get("checks", [])
        if not isinstance(checks,list):
            raise ValueError("Optional checks must be a list")
        flows = milestone.get("browser_flows", [])
        if not isinstance(flows,list):
            raise ValueError("Browser flows must be a list")
        checks = [*checks,*[{**flow,"kind":"browser","server_command":"true"} for flow in flows if isinstance(flow,dict)]]
        if any(not isinstance(flow,dict) for flow in flows):
            raise ValueError("Each browser flow must be an object")
        for check in checks:
            if not isinstance(check,dict):
                raise ValueError("Each check must be an object")
            command = check.get("server_command") if check.get("kind") == "browser" else check.get("command")
            if not isinstance(command,str) or not command.strip():
                raise ValueError("Each check needs an executable command or browser server command")
            parsed = subprocess.run(["bash","-n","-c",command],capture_output=True,text=True)
            if parsed.returncode:
                raise ValueError(f"Check {check.get('id','')} has invalid shell syntax: {parsed.stderr.strip()}")
            words = shlex.split(command)
            for index,word in enumerate(words[:-2]):
                if Path(word).name in {"python","python3"} and words[index+1] == "-c":
                    try:ast.parse(words[index+2])
                    except SyntaxError as error:
                        raise ValueError(f"Check {check.get('id','')} has invalid Python syntax: {error.msg}. Use a test file or multiline script.") from error
            if check.get("kind") == "browser":
                for step in check.get("steps",[]):
                    if not isinstance(step,dict) or step.get("action") not in {"click","fill","visible","text","reload"}:
                        raise ValueError("Browser steps must use click, fill, visible, text, or reload")
                    if step["action"] != "reload" and not step.get("selector"):
                        raise ValueError("Browser step needs a selector")
                    if step["action"] == "text" and "value" not in step:
                        raise ValueError("Browser text check needs an expected value")


def discover(repository):
    """Inspect every indexed package manifest; no first-N package sampling."""
    checks, web = [], []
    with repository.connect() as db:
        paths = {row[0] for row in db.execute('SELECT path FROM files')}
    packages = sorted({str(Path(p).parent) for p in paths if Path(p).name in {'package.json','pyproject.toml','requirements.txt','Cargo.toml','go.mod','pom.xml'}})
    if not packages and any(Path(p).suffix in {".py",".html",".js",".mjs"} for p in paths):
        packages = ["."]
    for package in packages:
        root = repository.root / package
        prefix = '' if package == '.' else package + '/'
        def add(label, command, **extra):
            checks.append({'id':package + ':' + label,'cwd':package,'command':command,'origin':'deterministic',**extra})
        if (root/'package.json').is_file():
            try:
                manifest = json.loads((root/'package.json').read_text())
            except ValueError:
                add('manifest', 'python3 -m json.tool package.json >/dev/null')
                continue
            scripts = manifest.get('scripts') or {}
            if (root/'pnpm-lock.yaml').exists():
                manager, install = 'pnpm', 'pnpm install --frozen-lockfile'
            elif (root/'yarn.lock').exists():
                manager, install = 'yarn', 'yarn install --frozen-lockfile'
            else:
                manager = 'npm'
                install = 'npm ci' if (root/'package-lock.json').exists() else 'npm install'
            add('install',install,setup=True)
            for name in ('build','lint','typecheck','test'):
                if name in scripts:
                    command = f'{manager} run {name}'
                    if name == 'test' and 'vitest' in scripts[name] and '--run' not in scripts[name]:
                        command += ' -- --run'
                    add(name,command)
            if any(key in {**manifest.get('dependencies',{}),**manifest.get('devDependencies',{})} for key in ('vite','next','react','vue','svelte')):
                web.append(package)
                if 'dev' in scripts:
                    flags = '--hostname 127.0.0.1 --port {port}' if 'next' in scripts['dev'] else '--host 127.0.0.1 --port {port}'
                    add('browser', '', kind='browser', server_command=f'{manager} run dev -- {flags}', path='/', steps=[])
        if (root/'pyproject.toml').exists() or (root/'requirements.txt').exists() or (package == "." and any(p.endswith(".py") for p in paths)):
            setup = 'python3 -m venv .venv'
            if (root/'pyproject.toml').exists():
                setup += ' && .venv/bin/python -m pip install -e .'
            if (root/'requirements.txt').exists():
                setup += ' && .venv/bin/python -m pip install -r requirements.txt'
            test_paths = [p for p in paths if p.startswith(prefix) and p.endswith('.py') and (p.startswith(prefix+'tests/') or Path(p).name.startswith('test') or Path(p).name.endswith('_test.py'))]
            pytest_needed = False
            for test_path in test_paths:
                try:
                    tree = ast.parse((repository.root/test_path).read_bytes())
                    pytest_needed |= any(isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith('test_') for node in tree.body)
                    pytest_needed |= any(isinstance(node,ast.ClassDef) and node.name.startswith('Test')
                        and any(isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef)) and child.name.startswith('test_') for child in node.body)
                        and not any((isinstance(base,ast.Attribute) and base.attr=='TestCase') or (isinstance(base,ast.Name) and base.id=='TestCase') for base in node.bases)
                        for node in tree.body)
                    pytest_needed |= any((isinstance(node,ast.Import) and any(alias.name=='pytest' for alias in node.names)) or (isinstance(node,ast.ImportFrom) and node.module=='pytest') for node in ast.walk(tree))
                except (SyntaxError,UnicodeError):pass
            if pytest_needed:
                setup += ' && .venv/bin/python -m pip install pytest'
            add('install',setup,setup=True)
            add('dependencies','.venv/bin/python -m pip check')
            add('syntax', '.venv/bin/python -m compileall -q -x "(^|/)(\\.venv|venv|node_modules)/" .')
            if test_paths:
                add('tests','.venv/bin/python -m pytest -q' if pytest_needed else '.venv/bin/python -m unittest discover '+('-s tests ' if (root/'tests').is_dir() else '')+"-p '*test*.py' -v")
        if (root/'Cargo.toml').exists():
            add('tests','cargo test --workspace')
        if (root/'go.mod').exists():
            add('tests','go test ./...')
        if (root/'pom.xml').exists():
            add('tests','mvn test')
        if not (root/'package.json').exists() and (root/'index.html').exists():
            web.append(package)
            add('browser','',kind='browser',server_command='python3 -m http.server {port} --bind 127.0.0.1',path='/',steps=[])
        if package == '.' and not (root/'package.json').exists():
            for source in sorted(p for p in paths if Path(p).suffix in {'.js','.mjs'}):
                add('syntax:'+source,'node --check '+shlex.quote(source))
        if not (root/'package.json').exists():
            for source in sorted(p for p in paths if p.startswith(prefix) and Path(p).suffix in {'.js','.mjs','.cjs'}):
                name = Path(source).name
                relative = str(Path(source).relative_to(package))
                if name.startswith(('test_', 'test.')) or '.test.' in name or any(part in {'test','tests'} for part in Path(relative).parts[:-1]):
                    add('tests:'+relative,'node --test '+shlex.quote(relative))
    return {'checks':checks,'packages':packages,'web_packages':web}


def browser_check(root, check, evidence_dir, timeout, *, step_timeout=None):
    from playwright.sync_api import sync_playwright
    step_timeout = DEFAULTS['daedalus_browser_step_seconds'] if step_timeout is None else step_timeout
    with socket.socket() as socket_:
        socket_.bind(('127.0.0.1',0))
        port = socket_.getsockname()[1]
    command = check['server_command'].replace('{port}',str(port))
    started = time.monotonic()
    errors, failed_requests = [], []
    server_log = evidence_dir/'browser-server.log'
    with server_log.open('wb') as log:
        server = subprocess.Popen(['bash','-c',command],cwd=root,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            with sync_playwright() as playwright:
                def remaining_ms():
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise TimeoutError('Browser verification exceeded its command allowance')
                    return remaining * 1000
                browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'], timeout=remaining_ms())
                page = browser.new_page()
                page.on('pageerror',lambda error: errors.append(str(error)))
                page.on('console',lambda message: errors.append(message.text) if message.type=='error' else None)
                page.on('requestfailed',lambda request: failed_requests.append({'url':request.url,'failure':request.failure}))
                path = check.get('path','/')
                if not path.startswith('/') or path.startswith('//'):
                    raise ValueError('Browser path must be project-relative')
                while True:
                    if server.poll() is not None:
                        raise ValueError('Preview server exited before browser verification')
                    if time.monotonic()-started >= timeout:
                        raise TimeoutError('Preview did not become ready within command allowance')
                    try:
                        response = page.goto(f'http://127.0.0.1:{port}{path}',wait_until='domcontentloaded',timeout=remaining_ms())
                        if response and response.status >= 400:
                            raise ValueError(f'Preview returned HTTP {response.status}')
                        break
                    except Exception as error:
                        if 'ERR_CONNECTION_REFUSED' not in str(error):
                            raise
                        # These failures occurred before the preview was ready;
                        # they are not errors emitted by the running app.
                        errors.clear()
                        failed_requests.clear()
                        time.sleep(0.25)
                for step in check.get('steps',[]):
                    action = step['action']
                    milliseconds = min(remaining_ms(), step_timeout * 1000)
                    if action == 'reload':
                        page.reload(timeout=milliseconds)
                        continue
                    locator = page.locator(step['selector'])
                    if action == 'click':locator.click(timeout=milliseconds)
                    elif action == 'fill':locator.fill(step.get('value',''),timeout=milliseconds)
                    elif action == 'visible':
                        locator.wait_for(state='visible',timeout=milliseconds)
                        if 'value' in step:
                            from playwright.sync_api import expect
                            expect(locator).to_contain_text(step['value'],timeout=milliseconds)
                    elif action == 'text':
                        from playwright.sync_api import expect
                        expect(locator).to_contain_text(step['value'],timeout=milliseconds)
                    else:raise ValueError(f'Unknown browser action: {action}')
                screenshot = evidence_dir/'browser.png'
                page.screenshot(path=str(screenshot),full_page=True,timeout=remaining_ms())
                title = page.title()
                browser.close()
            return {'passed':not errors and not failed_requests,'console_errors':errors,'failed_requests':failed_requests,
                    'screenshot':str(screenshot),'title':title,'steps':check.get('steps',[]),'server_log':str(server_log)}
        finally:
            import signal
            try:os.killpg(server.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid,signal.SIGKILL)
                server.wait()
