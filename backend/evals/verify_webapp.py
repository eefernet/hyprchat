"""Independent verdict for a delivered Node web application: install it, start it the
documented way (`npm start`), exercise its REST API over HTTP, and USE THE PAGE in a real browser.

The browser step exists because the first version of this verifier did not have one: an accepted
Kanban build whose script threw on load (`getElementById('task-form')` vs `id="taskForm"`, so no
handler ever bound) passed every HTTP check the API could answer. An API that works is not a
frontend that works. API payload values are taken from the application's own form options rather
than assumed: the request wrote "Low/Medium/High" but never specified wire casing."""
from __future__ import annotations
import json, os, signal, socket, subprocess, time, urllib.error, urllib.request
from pathlib import Path


def call(base, method, path, body=None):
    request = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode(errors='replace')
            return response.status, raw
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors='replace')


def form_options(page_html):
    """Option values the delivered form itself offers, per select, in document order."""
    import re
    return [re.findall(r'<option[^>]*value=["\']([^"\']*)["\']', block, re.I)
            for block in re.findall(r'<select\b.*?</select>', page_html, re.I | re.S)]


def browser_create_flow(base, note):
    """Create a task through the delivered page exactly as a user would."""
    title = 'Browser verify ' + str(int(time.time()))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:  # a verifier that cannot look at the page must not pass the page
        note('browser available', False, str(error)); return
    errors, writes = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True); page = browser.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)[:300]))
        page.on('dialog', lambda d: d.accept())
        page.on('response', lambda r: writes.append({'method': r.request.method, 'status': r.status})
                if '/api/' in r.url and r.request.method != 'GET' else None)
        page.goto(base + '/'); page.wait_for_timeout(800)
        note('page loads without script errors', not errors, errors[:3])
        opener = page.locator('button:visible', has_text=__import__('re').compile(r'add|new|create', __import__('re').I))
        field = page.locator('form input[type=text]:visible, form input:not([type]):visible').first
        if not field.count() and opener.count():
            opener.first.click(); page.wait_for_timeout(400)
        if not note('task form has a visible text field', field.count() > 0, 'No visible text input inside a form'):
            browser.close(); return
        field.fill(title)
        for index in range(page.locator('form select:visible').count()):
            page.locator('form select:visible').nth(index).select_option(index=0)
        submit = page.locator('form button[type=submit]:visible, form input[type=submit]:visible, form button:not([type]):visible').first
        if note('task form has a submit control', submit.count() > 0, 'No submit button'):
            submit.click(); page.wait_for_timeout(1500)
            note('form submit sends a successful API write', any(w['status'] < 300 for w in writes), {'writes': writes, 'url': page.url.replace(base, '')})
            _, listing = call(base, 'GET', '/api/tasks')
            rows = json.loads(listing) if listing.strip().startswith(('[', '{')) else []
            rows = rows.get('tasks', []) if isinstance(rows, dict) else rows
            note('task created in the browser is stored', any(r.get('title') == title for r in rows), listing[:200])
            note('task created in the browser is shown on the board', page.get_by_text(title).count() > 0, 'Title not visible on the page')
            note('no script errors while using the page', not errors, errors[:3])
        browser.close()


def kanban(root):
    checks = []
    def note(name, passed, detail=None):
        checks.append({'name': name, 'passed': bool(passed), 'detail': None if passed else detail})
        return bool(passed)
    manifest = next((p for p in sorted(root.rglob('package.json')) if 'node_modules' not in p.parts), None)
    if not note('package.json', manifest, 'No package.json delivered'):
        return checks
    base_dir = manifest.parent
    scripts = json.loads(manifest.read_text() or '{}').get('scripts', {})
    note('npm start script', 'start' in scripts, scripts)
    install = subprocess.run('npm install --no-audit --no-fund', shell=True, cwd=base_dir, capture_output=True, text=True, timeout=600)
    if not note('npm install', install.returncode == 0, install.stderr[-600:]):
        return checks
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
    log = open(base_dir / '.verify-server.log', 'w')
    server = subprocess.Popen('npm start', shell=True, cwd=base_dir, stdout=log, stderr=subprocess.STDOUT,
                              env={**os.environ, 'PORT': str(port), 'HOST': '127.0.0.1'}, start_new_session=True)
    base = f'http://127.0.0.1:{port}'
    try:
        ready = False
        for _ in range(60):
            try:
                ready = call(base, 'GET', '/')[0] < 500; break
            except OSError:
                if server.poll() is not None:
                    break
                time.sleep(0.5)
        if not note('server starts on $PORT', ready, (base_dir / '.verify-server.log').read_text(errors='replace')[-600:]):
            return checks
        status, page = call(base, 'GET', '/')
        note('frontend served', status == 200 and '<' in page and all(word in page.lower() for word in ('todo', 'progress', 'done')), page[:300])
        # Use a priority/status the application's own form offers; fall back to the request's lowercase status style.
        offered = form_options(page)
        priority = next((v for group in offered for v in group if v.lower() == 'high'), 'high')
        todo = next((v for group in offered for v in group if v.lower().replace(' ', '_') in {'todo', 'to_do'}), 'todo')
        done = next((v for group in offered for v in group if v.lower() == 'done'), 'done')
        status, raw = call(base, 'POST', '/api/tasks', {'title': 'Verify', 'description': 'independent', 'priority': priority, 'status': todo})
        created = json.loads(raw) if status in (200, 201) and raw.strip().startswith(('{', '[')) else {}
        created = created.get('task', created) if isinstance(created, dict) else {}
        identity = created.get('id')
        note('POST /api/tasks', identity is not None, {'status': status, 'body': raw[:300]})
        status, raw = call(base, 'GET', '/api/tasks')
        rows = json.loads(raw) if status == 200 and raw.strip().startswith(('[', '{')) else []
        rows = rows.get('tasks', []) if isinstance(rows, dict) else rows
        note('GET /api/tasks lists the task', any(r.get('id') == identity and r.get('title') == 'Verify' for r in rows), raw[:300])
        if identity is not None:
            status, raw = call(base, 'PUT', f'/api/tasks/{identity}', {'title': 'Verify', 'description': 'moved', 'priority': priority, 'status': done})
            _, listing = call(base, 'GET', '/api/tasks')
            rows = json.loads(listing); rows = rows.get('tasks', []) if isinstance(rows, dict) else rows
            moved = next((r for r in rows if r.get('id') == identity), {})
            note('PUT /api/tasks/:id persists', status == 200 and moved.get('status') == done, {'status': status, 'row': moved})
            status, _ = call(base, 'DELETE', f'/api/tasks/{identity}')
            _, listing = call(base, 'GET', '/api/tasks')
            rows = json.loads(listing); rows = rows.get('tasks', []) if isinstance(rows, dict) else rows
            note('DELETE /api/tasks/:id removes', status in (200, 204) and not any(r.get('id') == identity for r in rows), {'status': status})
        browser_create_flow(base, note)
    except Exception as error:
        note('http/browser exercise', False, type(error).__name__ + ': ' + str(error)[:300])
    finally:
        try:
            os.killpg(server.pid, signal.SIGTERM)
        except OSError:
            pass
        log.close()
    return checks
