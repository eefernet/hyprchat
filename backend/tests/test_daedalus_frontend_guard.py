"""The form that posts nothing (accepted Kanban build, September; delivered again live 2026-09-22)."""
import json
import functools
import http.server
import shutil
import threading

import pytest

from coder_frontend_guard import form_fields_row, form_row, formdata_faults

KANBAN_HTML = ('<button id="open">Add Task</button><div id="modal" hidden><form id="task-form">'
               '<input type="text" id="task-title" required><textarea id="task-description"></textarea>'
               '<select id="task-priority"><option value="">Pick</option><option value="high">High</option></select>'
               '<button type="submit">Save Task</button></form></div>')
KANBAN_JS = ('const taskForm = document.getElementById("task-form");\n'
             'document.getElementById("open").onclick = () => document.getElementById("modal").hidden = false;\n'
             'taskForm.addEventListener("submit", async e => { e.preventDefault(); const formData = new FormData(taskForm);\n'
             '  await fetch("/api/tasks", {method: "POST", headers: {"Content-Type": "application/json"},\n'
             '    body: JSON.stringify({title: formData.get("task-title"), priority: formData.get("task-priority")})}); });\n')


def project(tmp_path, html, script):
    (tmp_path / 'public').mkdir(exist_ok=True)
    (tmp_path / 'public' / 'index.html').write_text(html + '<script src="app.js"></script>')
    (tmp_path / 'public' / 'app.js').write_text(script)
    return tmp_path


def test_formdata_keys_must_be_names_the_html_assigns(tmp_path):
    root = project(tmp_path, KANBAN_HTML, KANBAN_JS)
    faults, used = formdata_faults(root)
    assert used and faults == [('task-priority', 'public/app.js'), ('task-title', 'public/app.js')]
    row = form_fields_row(root, 'rev', 'op')
    assert row and not row['passed'] and row['classification'] == 'application_defect' and 'name="task-title"' in row['reason']
    named = KANBAN_HTML.replace('id="task-title"', 'id="task-title" name="task-title"').replace('id="task-priority"', 'id="task-priority" name=task-priority')
    assert formdata_faults(project(tmp_path, named, KANBAN_JS)) == ([], True)
    assert form_fields_row(project(tmp_path, named, KANBAN_JS), 'rev', 'op')['passed']
    # Values read through the elements, or FormData spread wholesale, name no literal keys: nothing to check.
    by_id = 'fetch("/api/tasks", {body: JSON.stringify({title: document.getElementById("task-title").value})})'
    assert form_fields_row(project(tmp_path, KANBAN_HTML, by_id), 'rev', 'op') is None
    spread = 'const body = Object.fromEntries(new FormData(taskForm)); fetch("/api/tasks", {body: JSON.stringify(body)})'
    assert form_fields_row(project(tmp_path, KANBAN_HTML, spread), 'rev', 'op') is None
    # No plain HTML (JSX/Vue/templates): not this check's business.
    (tmp_path / 'public' / 'index.html').unlink()
    assert form_fields_row(tmp_path, 'rev', 'op') is None


def test_form_row_fails_only_on_a_rejected_write():
    service, settings = {'id': 'app', 'cwd': '.'}, {}
    probe = lambda writes, forms=1, fault='': (lambda url, **k: {'forms': forms, 'submitted': forms, 'writes': writes, 'errors': [], 'fault': fault})
    rejected = form_row(service, 'http://127.0.0.1:1', settings, prober=probe([{'method': 'POST', 'path': '/api/tasks', 'status': 400, 'form': 1}]))
    assert rejected['id'] == 'form:app' and not rejected['passed'] and 'POST /api/tasks -> 400' in rejected['reason'] and not rejected['environment_fault']
    assert form_row(service, 'http://127.0.0.1:1', settings, prober=probe([{'method': 'POST', 'path': '/api/tasks', 'status': 201, 'form': 1}]))['passed']
    silent = form_row(service, 'http://127.0.0.1:1', settings, prober=probe([]))
    assert silent['passed'] and 'advisory' in silent['log_tail']            # a GET/search/login form is not a defect
    assert form_row(service, 'http://127.0.0.1:1', settings, prober=probe([], forms=0)) is None
    broken = form_row(service, 'http://127.0.0.1:1', settings, prober=probe([], fault='Executable does not exist'))
    assert not broken['passed'] and broken['environment_fault']
    from coder_policy7_evidence import blocking
    from coder_project_runtime import failure_class
    assert blocking(rejected) and failure_class(rejected) == 'application_defect' and failure_class(broken) == 'environment'


@pytest.fixture
def kanban_server(tmp_path):
    """Serves the page and answers the API like the live Kanban server: 400 when title is missing."""
    servers = []
    def start(html, script):
        folder = tmp_path / 'site'; shutil.rmtree(folder, ignore_errors=True); folder.mkdir()
        (folder / 'index.html').write_text(html + '<script src="app.js"></script>'); (folder / 'app.js').write_text(script)
        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k): super().__init__(*a, directory=str(folder), **k)
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
                status = 201 if body.get('title') else 400
                data = json.dumps({'id': 1, **body} if status == 201 else {'error': 'title is required'}).encode()
                self.send_response(status); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start(); servers.append(server)
        return f'http://127.0.0.1:{server.server_port}'
    yield start
    for server in servers:
        server.shutdown()


def test_real_chromium_submits_the_modal_form_and_reports_the_rejected_write(kanban_server):
    pytest.importorskip('playwright.sync_api')
    try:
        broken = form_row({'id': 'app', 'cwd': '.'}, kanban_server(KANBAN_HTML, KANBAN_JS), {})
    except Exception as error:
        pytest.skip('Chromium is not installed here: ' + str(error)[:120])
    if broken and broken.get('environment_fault'):
        pytest.skip('Chromium is not installed here: ' + broken['reason'][:120])
    assert broken and not broken['passed'] and 'POST /api/tasks -> 400' in broken['reason'], broken
    named = KANBAN_HTML.replace('id="task-title"', 'id="task-title" name="task-title"').replace('id="task-priority"', 'id="task-priority" name="task-priority"')
    working = form_row({'id': 'app', 'cwd': '.'}, kanban_server(named, KANBAN_JS), {})
    assert working['passed'] and any(w['status'] == 201 for w in working['form_probe']['writes']), working
