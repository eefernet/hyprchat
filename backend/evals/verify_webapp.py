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
from coder_sandbox_call import browser_options


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


STATUS_NAMES = {'todo': ('todo', 'to do'), 'in_progress': ('in progress', 'in-progress'), 'done': ('done',)}


def column_selectors(status):
    """Locator expressions for a status column, most specific first: the id/data conventions, then the
    nearest section/div/article around a heading that reads as the request's column name (the request
    names the columns Todo / In Progress / Done and nothing else)."""
    names = STATUS_NAMES.get(status, (status.replace('_', ' '),))
    dashed = status.replace('_', '-')
    ids = dict.fromkeys([status, dashed])
    css = ', '.join(f'[data-status="{status}"], #{i}, #column-{i}, #{i}-column, #{i}-tasks, #{i}-list' for i in ids)
    lower = "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    headings = ' or '.join(f'self::h{n}' for n in range(1, 7)) + ' or self::header'
    xpaths = [f"//*[{headings}][{lower}='{name}' or starts-with({lower}, '{name} ')]"
              "/ancestor::*[self::section or self::div or self::article][1]" for name in names]
    return [('css', css), *(('xpath', x) for x in xpaths)]


def port_from_log(text):
    """The port a server reports listening on, or None; an address-in-use error is not a listener."""
    import re
    if re.search(r'EADDRINUSE|address already in use', text, re.I):
        return None
    for line in reversed(text.splitlines()):
        if not re.search(r'listen|running|started|ready|server|http://', line, re.I):
            continue
        match = re.search(r'https?://[^\s/:]+:(\d{2,5})\b', line) or re.search(r'(?:\bport\s*[:=]?\s*|:)(\d{2,5})\b', line, re.I)
        if match and 1 <= int(match.group(1)) <= 65535:
            return int(match.group(1))
    return None


def browser_create_flow(base, note, restart):
    """Frozen behavioral oracle; DOM discovery only locates the delivered controls."""
    import re
    from playwright.sync_api import sync_playwright
    def rows():
        status, raw = call(base, 'GET', '/api/tasks')
        assert status == 200
        value = json.loads(raw)
        return value.get('tasks', []) if isinstance(value, dict) else value
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **browser_options())
        try:
            for width, height in ((1440,900),(390,844)):
                page = browser.new_page(viewport={'width':width,'height':height})
                page.set_default_timeout(8000)
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)[:300]))
                page.on('dialog', lambda d: d.accept())
                prefix = str(width) + ': '
                title = 'Browser verify ' + str(time.time_ns())
                try:
                    page.goto(base + '/'); page.wait_for_timeout(500)
                    field = page.locator('form input[name=title]:visible, form input[type=text]:visible, form input:not([type]):visible').first
                    if not field.count():
                        page.get_by_role('button', name=re.compile('add|new|create',re.I)).first.click()
                    field.fill(title)
                    description = page.locator('form textarea:visible, form input[name=description]:visible').first
                    description.fill('Created by independent browser check')
                    priority = page.locator('form select:visible').filter(has=page.locator('option',has_text=re.compile('High',re.I))).first
                    high = priority.locator('option').evaluate_all('(xs)=>xs.find(x=>x.textContent.trim().toLowerCase()==="high").value')
                    priority.select_option(high)
                    submit = page.locator('form button[type=submit]:visible, form input[type=submit]:visible, form button:not([type]):visible').first
                    submit.click(); page.wait_for_timeout(700)
                    created = next((row for row in rows() if row.get('title') == title), None)
                    assert created, 'UI creation did not persist'
                    identity = created['id']
                    note(prefix+'create stores all fields', created.get('description')=='Created by independent browser check' and created.get('priority')==high, created)
                    def card():
                        return page.locator('[draggable=true]').filter(has_text=title).first
                    assert card().is_visible(), 'Created card is not visible/draggable'
                    edit = card().locator('button').filter(has_text=re.compile('edit',re.I))
                    if not edit.count(): edit = card().locator('[aria-label*=edit i], [title*=edit i], .edit-btn, .edit')
                    edit.first.click()
                    # An edit handler that fetches the task and then populates the form is asynchronous; filling the
                    # fields before it resolves lets the original values overwrite the edit (kanban1 re-judge, 2026-09-26).
                    for _ in range(25):
                        if field.input_value() == title:
                            break
                        page.wait_for_timeout(200)
                    title += ' edited'; field.fill(title); description.fill('Edited independently')
                    low = priority.locator('option').evaluate_all('(xs)=>xs.find(x=>x.textContent.trim().toLowerCase()==="low").value')
                    priority.select_option(low); submit.click(); page.wait_for_timeout(700)
                    edited = next(row for row in rows() if row['id']==identity)
                    note(prefix+'edit persists on same task', edited.get('title')==title and edited.get('description')=='Edited independently' and edited.get('priority')==low, edited)
                    note(prefix+'edit creates no duplicate', sum(r.get('title')==title for r in rows())==1)
                    assert card().is_visible(), 'Edited card not visible'
                    for status in ('in_progress','done'):
                        target = next((found for kind, expr in column_selectors(status)
                                       for found in [page.locator(('xpath=' if kind == 'xpath' else '') + expr).first] if found.count()), None)
                        assert target is not None, 'Cannot locate destination column '+status
                        # A real drop lands on the innermost list under the pointer, and many apps resolve the zone with
                        # event.target.closest('.task-list'); dispatching on the outer column never reaches them.
                        zone = target.locator('[ondrop], .task-list, .tasks, .task-container, .cards, .card-list, ul, ol').first
                        if not zone.count(): zone = target
                        def landed():
                            return target.locator('[draggable=true]').filter(has_text=title).count()==1
                        try:
                            card().drag_to(zone, timeout=5000); page.wait_for_timeout(600)
                        except Exception:
                            pass
                        for drop_target in ([zone, target] if not landed() else []):
                            transfer = page.evaluate_handle('new DataTransfer()')
                            card().dispatch_event('dragstart', {'dataTransfer':transfer})
                            drop_target.dispatch_event('dragover', {'dataTransfer':transfer})
                            drop_target.dispatch_event('drop', {'dataTransfer':transfer})
                            page.wait_for_timeout(600)
                            if landed(): break
                        note(prefix+'drag immediately places card in '+status, landed())
                        moved = next(row for row in rows() if row['id']==identity)
                        note(prefix+'drag persists '+status,moved.get('status')==status,moved)
                    before = next(row for row in rows() if row['id']==identity)
                    restart(); page.reload(); page.wait_for_timeout(500)
                    after = next(row for row in rows() if row['id']==identity)
                    note(prefix+'server restart preserves task', before==after, {'before':before,'after':after})
                    note(prefix+'card visible after restart',card().is_visible())
                    delete = card().locator('button').filter(has_text=re.compile('delete|remove',re.I))
                    if not delete.count(): delete=card().locator('[aria-label*=delete i], [title*=delete i], .delete-btn, .delete')
                    delete.first.click(); page.wait_for_timeout(600)
                    note(prefix+'delete removes stored task',not any(row['id']==identity for row in rows()))
                    page.reload(); page.wait_for_timeout(400)
                    note(prefix+'delete remains absent after reload',card().count()==0)
                except Exception as error:
                    note(prefix+'complete browser journey',False,type(error).__name__+': '+str(error)[:700])
                finally:
                    note(prefix+'no browser script errors',not errors,errors)
                    page.close()
        finally: browser.close()


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
        if not ready and server.poll() is None:
            # `npm start` ignored $PORT (the request never mentions it): follow the port the server logs.
            logged = port_from_log((base_dir / '.verify-server.log').read_text(errors='replace'))
            if logged and logged != port:
                base = f'http://127.0.0.1:{logged}'
                for _ in range(10):
                    try:
                        ready = call(base, 'GET', '/')[0] < 500; break
                    except OSError:
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
            note('PUT /api/tasks/:id persists', status in (200, 204) and moved.get('status') == done, {'status': status, 'row': moved})
            status, _ = call(base, 'DELETE', f'/api/tasks/{identity}')
            _, listing = call(base, 'GET', '/api/tasks')
            rows = json.loads(listing); rows = rows.get('tasks', []) if isinstance(rows, dict) else rows
            note('DELETE /api/tasks/:id removes', status in (200, 204) and not any(r.get('id') == identity for r in rows), {'status': status})
        def restart():
            nonlocal server
            from coder_patch_runtime import stop_process
            stop_process(server)
            server = subprocess.Popen('npm start',shell=True,cwd=base_dir,stdout=log,stderr=subprocess.STDOUT,
                env={**os.environ,'PORT':str(port),'HOST':'127.0.0.1'},start_new_session=True)
            deadline=time.monotonic()+60
            while time.monotonic()<deadline:
                try:
                    if call(base,'GET','/')[0]==200:return
                except OSError: pass
                time.sleep(.2)
            raise RuntimeError('Server failed to restart')
        browser_create_flow(base, note, restart)
    except Exception as error:
        note('http/browser exercise', False, type(error).__name__ + ': ' + str(error)[:300])
    finally:
        try:
            os.killpg(server.pid, signal.SIGTERM)
        except OSError:
            pass
        log.close()
    return checks
