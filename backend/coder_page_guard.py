"""Controller-owned check: a launched web page must load without throwing.

Policy 7 proved a service was up with an HTTP readiness probe only. An isolated rerun of a live
Kanban build was ACCEPTED although its script threw on every load (`getElementById('task-form')`
for a form whose id was `taskForm`), so no handler bound and the board could not create a task.
The model-written audit exercised the REST API, which worked; nothing ever opened the page.

This guard loads the ready page once in headless Chromium and fails on uncaught exceptions. It is
deliberately narrow: console.error output and failed sub-requests (a missing favicon) are NOT
failures, because rejecting working apps would only burn repair rounds. It reads the service's
direct URL, never the controller's proxy, so it cannot manufacture browser-interface evidence.
A browser that cannot start is an environment fault: never a pass, never the application's fault.
"""
from __future__ import annotations

import urllib.error
import urllib.request


def content_kind(url, timeout=5):
    """'html', 'other' (answered, but no page there), or '' when the probe itself failed."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 'html' if 'html' in (response.headers.get('Content-Type') or '').lower() else 'other'
    except urllib.error.HTTPError:
        return 'other'
    except OSError:
        return ''


def serves_html(url, timeout=5):
    return content_kind(url, timeout) == 'html'


def load_errors(url, *, startup_seconds=60, step_seconds=15):
    """Return (uncaught page errors, environment fault text)."""
    import os
    from coder_sandbox_call import invoke, browser_options
    if os.environ.get('DAEDALUS_SANDBOXED') != '1':
        return invoke('coder_page_guard', 'load_errors', [url], {'startup_seconds': startup_seconds, 'step_seconds': step_seconds}, timeout=startup_seconds + step_seconds * 20)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        return [], 'Playwright is not installed in the worker: ' + str(error)
    errors = []
    try:
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True, timeout=startup_seconds * 1000, **browser_options())
            try:
                page = browser.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)[:400]))
                page.goto(url, wait_until='load', timeout=step_seconds * 1000)
                page.wait_for_timeout(500)  # DOMContentLoaded handlers and first fetch callbacks
            finally:
                browser.close()
    except Exception as error:  # navigation timeouts included: the readiness probe already answered
        if errors:
            return list(dict.fromkeys(errors)), ''
        return [], type(error).__name__ + ': ' + str(error)[:300]
    return list(dict.fromkeys(errors)), ''


def page_row(service, url, settings, loader=load_errors):
    """A launch-phase check row, or None when the service does not serve a page."""
    path = service.get('ready_path', '/')
    kind = content_kind(url + path)
    if kind != 'html' and path != '/':
        # A JSON readiness endpoint (/api/health) says nothing about the page the user opens, and a
        # probe error there must not send the browser to the health path instead of the page.
        root = content_kind(url + '/')
        if kind == 'other' and root == 'other':
            return None
        path, kind = '/', root
    if kind == 'other':
        return None
    # A probe that errored is not evidence of "no page": the service just answered its readiness
    # check, so the browser load decides (a dead service then reports an environment fault).
    target = url + path
    errors, fault = loader(target, startup_seconds=settings.get('daedalus_browser_startup_seconds', 60),
                           step_seconds=settings.get('daedalus_browser_step_seconds', 15))
    passed = not errors and not fault
    reason = '' if passed else ('The page could not be opened in the verification browser: ' + fault) if fault else (
        'The page throws an uncaught script error as soon as it loads, so its event handlers never attach and the '
        'interface does not work: ' + ' | '.join(errors[:3]) + '. Open the page script and the HTML together and fix the '
        'mismatch (element ids/selectors the script looks up must exist in the HTML).')
    return {'id': 'page:' + service['id'], 'phase': 'launch', 'origin': 'project', 'passed': passed,
            'cwd': service.get('cwd', '.'), 'page_path': path, 'command': 'load ' + path + ' in headless Chromium',
            'reason': reason, 'log_tail': '\n'.join(errors) or fault, 'page_errors': errors,
            'environment_fault': bool(fault), 'execution_succeeded': passed}
