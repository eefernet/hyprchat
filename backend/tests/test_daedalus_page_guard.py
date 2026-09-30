"""A launched page must load without throwing (accepted-but-broken Kanban frontend, isolated rerun 2026-09-20)."""
import functools
import http.server
import shutil
import threading

import pytest

from coder_page_guard import page_row

BROKEN = ('<form id="taskForm"><input id="title"></form><script>'
          "document.getElementById('task-form').addEventListener('submit', () => {});</script>")
WORKING = '<form id="taskForm"><input id="title"></form><script>document.getElementById("taskForm").addEventListener("submit", () => {}); console.error("noisy but fine");</script><img src="/missing.png">'


@pytest.fixture
def serve(tmp_path):
    servers = []
    def start(name, body, content_type='text/html'):
        folder = tmp_path / name; folder.mkdir()
        (folder / 'index.html').write_text(body)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(folder))
        if content_type != 'text/html':
            class Typed(http.server.SimpleHTTPRequestHandler):
                def __init__(self, *a, **k): super().__init__(*a, directory=str(folder), **k)
                def guess_type(self, path): return content_type
            handler = Typed
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start(); servers.append(server)
        return f'http://127.0.0.1:{server.server_port}'
    yield start
    for server in servers:
        server.shutdown()


def test_a_missing_browser_is_an_environment_fault_never_a_pass(serve):
    row = page_row({'id': 'app', 'cwd': '.'}, serve('site', WORKING), {}, loader=lambda *a, **k: ([], 'Executable does not exist'))
    assert not row['passed'] and row['environment_fault'] and 'could not be opened' in row['reason']


def test_a_json_service_has_no_page_to_guard(serve):
    assert page_row({'id': 'api', 'cwd': '.'}, serve('api', '{}', 'application/json'), {}, loader=lambda *a, **k: pytest.fail('not a page')) is None


def test_the_row_is_a_blocking_application_defect_the_repair_round_can_read(serve):
    from coder_policy7_evidence import blocking
    from coder_project_runtime import failure_class
    row = page_row({'id': 'app', 'cwd': '.'}, serve('site', BROKEN), {}, loader=lambda *a, **k: (["Cannot read properties of null (reading 'addEventListener')"], ''))
    assert row['id'] == 'page:app' and blocking(row) and failure_class(row) == 'application_defect'
    assert 'addEventListener' in row['reason'] and 'traffic' not in row


@pytest.mark.skipif(not shutil.which('bwrap'), reason='headless Chromium is installed on Codebox')
def test_a_script_error_on_load_fails_and_console_noise_or_a_missing_image_does_not(serve):
    broken = page_row({'id': 'app', 'cwd': '.'}, serve('broken', BROKEN), {})
    assert not broken['passed'] and not broken['environment_fault'] and 'null' in broken['log_tail'], broken
    working = page_row({'id': 'app', 'cwd': '.'}, serve('working', WORKING), {})
    assert working['passed'] and not working['page_errors'], working


# Code audit 2026-09-21: three ways the guard vanished without a trace.
def test_a_json_readiness_path_still_guards_the_page_at_the_root(serve, tmp_path):
    # .daedalus-run.json with "ready_path": "/api/health" made serves_html() false, so no page row existed.
    base = serve('site', BROKEN)
    (tmp_path / 'site' / 'health.json').write_text('{"ok": true}')
    seen = []
    row = page_row({'id': 'app', 'cwd': '.', 'ready_path': '/health.json'}, base, {},
                   loader=lambda url, **k: (seen.append(url) or ["TypeError: null"], ''))
    assert seen == [base + '/'] and row['id'] == 'page:app' and not row['passed'] and 'load / in' in row['command']


def test_a_failed_content_probe_does_not_skip_the_guard():
    # The probe used to return False on any OSError (3 s timeout on a cold server): guard silently gone.
    row = page_row({'id': 'app', 'cwd': '.'}, 'http://127.0.0.1:9', {}, loader=lambda url, **k: ([], 'TimeoutError: page.goto'))
    assert row is not None and not row['passed'] and row['environment_fault']


def test_a_service_with_no_page_at_all_is_still_not_guarded(serve):
    base = serve('api', '{}', 'application/json')
    assert page_row({'id': 'api', 'cwd': '.', 'ready_path': '/missing'}, base, {}, loader=lambda *a, **k: pytest.fail('not a page')) is None
