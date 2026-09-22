"""Controller-owned checks for the form that posts nothing.

The September Kanban build was accepted with a page that loaded cleanly while its form could not create a
task: the inputs had `id="task-title"` but no `name`, so `FormData.get('task-title')` was null and the POST
was rejected with 400. The page guard (uncaught errors on load) cannot see that, and a model-written audit
that exercises the REST API passes. The live rerun of 2026-09-22 delivered the same defect again.

Two deterministic rows, both safe-side:

* `frontend:form-fields` (static) - every literal key read from a FormData object in delivered scripts must be
  the `name` of some control in delivered HTML. Skipped for projects without plain HTML (JSX, Vue, templates).
* `form:<service>` (dynamic) - the ready page's forms are filled type-aware and submitted in headless Chromium;
  the row FAILS only when a write the submit produced came back 4xx/5xx (a demonstrated broken write). A form
  that sends nothing is reported, never failed: search, login and GET forms are legitimate. Direct URL only,
  never the traffic proxy, so this can never manufacture browser evidence. A browser that cannot start is an
  environment fault, never a pass.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

FORMDATA_VAR = re.compile(r'\b(?:const|let|var)\s+(\w+)\s*=\s*new\s+FormData\s*\(')
NAME_ATTR = re.compile(r'''\bname\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s"'>]+))''', re.I)
SCRIPT_SUFFIXES = {'.js', '.mjs', '.cjs'}
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', 'vendor', '.venv', 'venv', '__pycache__', 'coverage'}
PROBE_TEXT = 'daedalus probe'
PROBE_VALUES = {'number': '1', 'range': '1', 'email': 'probe@example.com', 'url': 'https://example.com', 'date': '2026-01-01',
                'time': '12:00', 'datetime-local': '2026-01-01T12:00', 'month': '2026-01', 'week': '2026-W01', 'tel': '5551234',
                'password': 'daedalus-probe-1', 'text': PROBE_TEXT, 'search': PROBE_TEXT, '': PROBE_TEXT}


def _files(root, suffixes):
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        for name in files:
            if Path(name).suffix.lower() in suffixes and not any(part in ('test', 'tests', 'spec') for part in Path(directory).parts):
                yield Path(directory) / name


def formdata_keys(text):
    """Literal keys read from FormData objects: `fd.get('k')`, `new FormData(f).get('k')`, `fd.has('k')`."""
    names = set(FORMDATA_VAR.findall(text))
    keys = set()
    for variable in names:
        keys.update(re.findall(r'\b' + re.escape(variable) + r'\.(?:get|getAll|has)\(\s*["\']([^"\']+)["\']', text))
    keys.update(re.findall(r'new\s+FormData\s*\([^)]*\)\s*\.(?:get|getAll|has)\(\s*["\']([^"\']+)["\']', text))
    return keys


def formdata_faults(root):
    """([(key, script)], used): keys no delivered HTML control names, and whether any script reads a FormData at all.
    Nothing applies to a project without plain HTML (JSX, Vue, server templates)."""
    root = Path(root)
    html = list(_files(root, {'.html', '.htm'}))
    if not html:
        return [], False
    names = set()
    for page in html:
        text = page.read_text(errors='replace')
        for a, b, c in NAME_ATTR.findall(text):
            names.add(a or b or c)
    faults, used = [], False
    for script in [*_files(root, SCRIPT_SUFFIXES), *html]:
        text = script.read_text(errors='replace')
        if 'FormData' not in text:
            continue
        keys = formdata_keys(text)
        used = used or bool(keys)
        for key in sorted(keys):
            if key not in names:
                faults.append((key, script.relative_to(root).as_posix()))
    return faults, used


def form_fields_row(root, revision_id, execution_id):
    """A controller check row, or None when nothing applies."""
    faults, used = formdata_faults(root)
    if not used:
        return None
    detail = '; '.join(f"{script} reads FormData key '{key}' but no delivered HTML control has name=\"{key}\"" for key, script in faults[:8])
    return {'id': 'frontend:form-fields', 'origin': 'controller', 'passed': not faults, 'revision_id': revision_id,
            'execution_id': execution_id + ':frontend:form-fields', 'evidence_types': [], 'outcomes': [],
            'classification': 'passed' if not faults else 'application_defect',
            'log_tail': '' if not faults else detail,
            'reason': '' if not faults else ('The page script reads form values by names the HTML never assigns, so the values are null and '
                'the request the form sends is rejected: ' + detail + '. Give each control a name attribute matching what the script reads '
                '(an id alone is not a FormData key), or read the values through the elements by id.')}


def _fill(page, form):
    """Fill one form the way a user would, type-aware; returns the number of controls touched."""
    touched = 0
    for element in form.locator('input:visible, textarea:visible').all():
        kind = (element.get_attribute('type') or '').lower()
        if kind in {'hidden', 'submit', 'button', 'reset', 'checkbox', 'radio', 'file', 'color', 'image'}:
            if kind == 'checkbox' and not element.is_checked():
                element.check(); touched += 1
            continue
        if element.get_attribute('readonly') is not None or element.get_attribute('disabled') is not None:
            continue
        if element.evaluate('e => e.tagName.toLowerCase()') == 'textarea':
            element.fill(PROBE_TEXT); touched += 1; continue
        value = PROBE_VALUES.get(kind)
        if value is None:
            continue
        try:
            element.fill(value); touched += 1
        except Exception:
            continue
    for select in form.locator('select:visible').all():
        options = select.locator('option').all()
        choice = next((index for index, option in enumerate(options) if (option.get_attribute('value') or '').strip()), 0 if options else None)
        if choice is not None:
            select.select_option(index=choice); touched += 1
    return touched


def submit_forms(url, *, startup_seconds=60, step_seconds=15, limit=3):
    """Submit up to `limit` forms on the page; return {'forms', 'submitted', 'writes', 'errors', 'fault'}."""
    result = {'forms': 0, 'submitted': 0, 'writes': [], 'errors': [], 'fault': ''}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        result['fault'] = 'Playwright is not installed in the worker: ' + str(error)
        return result
    try:
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True, timeout=startup_seconds * 1000)
            try:
                page = browser.new_page()
                page.set_default_timeout(step_seconds * 1000)
                page.on('pageerror', lambda error: result['errors'].append(str(error)[:300]))
                page.on('dialog', lambda dialog: dialog.accept())
                page.on('response', lambda response: result['writes'].append({
                    'method': response.request.method, 'path': urlsplit(response.url).path or '/',
                    'status': response.status, 'form': result['submitted']}) if response.request.method in ('POST', 'PUT', 'PATCH', 'DELETE') else None)
                page.goto(url, wait_until='load', timeout=step_seconds * 1000)
                page.wait_for_timeout(500)
                forms = page.locator('form')
                if not page.locator('form:visible').count():
                    # Forms often live in a modal behind an Add/New/Create button.
                    opener = page.locator('button:visible, a:visible', has_text=re.compile(r'\b(add|new|create)\b', re.I))
                    if opener.count():
                        opener.first.click(); page.wait_for_timeout(400)
                result['forms'] = forms.count()
                for index in range(min(forms.count(), limit)):
                    form = forms.nth(index)
                    if not form.is_visible():
                        continue
                    if not _fill(page, form):
                        continue
                    result['submitted'] += 1
                    submit = form.locator('button[type=submit]:visible, input[type=submit]:visible, button:not([type]):visible').first
                    if submit.count():
                        submit.click()
                    else:
                        form.evaluate('f => f.requestSubmit ? f.requestSubmit() : f.submit()')
                    page.wait_for_timeout(1500)
                    if page.url.rstrip('/') != url.rstrip('/'):
                        page.goto(url, wait_until='load', timeout=step_seconds * 1000); page.wait_for_timeout(300)
            finally:
                browser.close()
    except Exception as error:  # navigation timeouts included: the readiness probe already answered
        result['fault'] = type(error).__name__ + ': ' + str(error)[:300]
    return result


def form_row(service, url, settings, prober=submit_forms):
    """A launch-phase check row after a page loaded, or None when the page has no form."""
    probe = prober(url + '/', startup_seconds=settings.get('daedalus_browser_startup_seconds', 60),
                   step_seconds=settings.get('daedalus_browser_step_seconds', 15))
    if not probe['fault'] and not probe['forms']:
        return None
    rejected = [w for w in probe['writes'] if w['status'] >= 400]
    passed = not probe['fault'] and not rejected
    if probe['fault']:
        reason = 'The page could not be exercised in the verification browser: ' + probe['fault']
    elif rejected:
        listed = ', '.join(f"{w['method']} {w['path']} -> {w['status']}" for w in rejected[:4])
        reason = ('Submitting the page\'s own form as a user would sent a request the server rejected (' + listed + '). The form '
                  'does not deliver its values: check that each control has a name attribute matching what the script reads (an id '
                  'alone is not a FormData key), that the request body matches what the API validates, and read the server\'s error.')
    else:
        reason = ''
    note = '' if probe['writes'] or probe['fault'] else 'No request was sent when the form was submitted (advisory: a search, login or GET form is fine).'
    return {'id': 'form:' + service['id'], 'phase': 'launch', 'origin': 'project', 'passed': passed, 'cwd': service.get('cwd', '.'),
            'command': 'fill and submit the page forms in headless Chromium', 'reason': reason, 'form_probe': probe,
            'log_tail': reason or note, 'environment_fault': bool(probe['fault']), 'execution_succeeded': passed}
