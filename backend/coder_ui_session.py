"""Private Xvfb/DBus application session used by tools, evaluation and previews.

This process must be launched inside coder_sandbox.Sandbox. It exposes no host
filesystem or network service. Browser pages execute here; the HyprChat browser
receives pixels, not application HTML or scripts.
"""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import threading
import uuid


class UIFailure(ValueError):
    def __init__(self, details):
        super().__init__(details['error'])
        self.details = details


def free_port():
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', 0))
        return connection.getsockname()[1]


class UISession:
    def __init__(self, directory, vnc_port=None):
        self.directory = Path(directory); self.directory.mkdir(parents=True, exist_ok=True)
        self.processes, self.playwright, self.browser, self.page = [], None, None, None
        self.kind = None
        self.behavior_version = int(os.environ.get('DAEDALUS_ENGINE_BEHAVIOR_VERSION', '0'))
        self.session_id = ''
        self.application_processes = []
        self.env = {**os.environ, 'DISPLAY': ':91', 'QT_LINUX_ACCESSIBILITY_ALWAYS_ON': '1',
                    'QT_ACCESSIBILITY': '1', 'NO_AT_BRIDGE': '0', 'GTK_A11Y': 'atspi',
                    'GSETTINGS_BACKEND': 'memory'}
        self.spawn(['Xvfb', ':91', '-screen', '0', '1280x800x24', '-nolisten', 'tcp'], 'xvfb')
        deadline = time.monotonic()+10
        while not Path('/tmp/.X11-unix/X91').exists():
            if time.monotonic() > deadline: raise RuntimeError('Xvfb did not become ready')
            time.sleep(.1)
        manager = self.spawn(['openbox'], 'window-manager')
        deadline = time.monotonic()+10
        while True:
            probe = subprocess.run(['xprop','-root','_NET_SUPPORTING_WM_CHECK'], env=self.env,
                                   capture_output=True, text=True, timeout=2)
            if 'window id #' in probe.stdout: break
            if manager.poll() is not None or time.monotonic()>deadline:
                raise RuntimeError('The desktop window manager did not become ready')
            time.sleep(.1)
        if vnc_port:
            self.spawn(['x11vnc', '-display', ':91', '-localhost', '-rfbport', str(vnc_port),
                        '-nopw', '-forever', '-shared', '-noxdamage'], 'vnc')

    def spawn(self, command, name):
        process = subprocess.Popen(command, env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        path=self.directory/(name+'.log')
        def collect():
            with process.stdout:
                while chunk:=process.stdout.read1(32768):
                    if path.exists() and path.stat().st_size>=1024**2:
                        path.replace(path.with_suffix('.log.1'))
                    with path.open('ab') as log: log.write(chunk)
        threading.Thread(target=collect,daemon=True).start()
        self.processes.append(process)
        return process

    def perform(self, request):
        guarded = self.behavior_version >= 1
        launching = request.get('action') == 'launch' and not self.kind
        if guarded and launching:
            self.session_id = uuid.uuid4().hex
        if guarded and self.kind == 'desktop' and request.get('selector') and request.get('session_id') != self.session_id:
            raise UIFailure({'error': 'This desktop node reference belongs to another session. Observe the current session again.',
                             'category': 'stale_session', 'requested': request, 'session_id': self.session_id})
        before = len(self.processes)
        try:
            result = self._perform(request)
        except Exception as error:
            if guarded and launching:
                tail = self.log_tail()
                self.close_application(self.processes[before:])
                raise UIFailure({'error': str(error), 'category': 'application_startup', **getattr(error, 'details', {}),
                                 'requested': request, 'log_tail': tail, 'session_id': self.session_id}) from error
            raise
        if guarded:
            if request.get('action') == 'launch': self.application_processes = self.processes[before:]
            result['session_id'] = self.session_id
            for node in result.get('nodes', []): node['session_id'] = self.session_id
        return result

    def close_application(self, processes):
        for resource, method in ((self.browser, 'close'), (self.playwright, 'stop')):
            if resource:
                try: getattr(resource, method)()
                except Exception: pass  # Continue terminating the failed application.
        self.browser = self.playwright = self.page = None
        for process in reversed(processes):
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
            except ProcessLookupError:
                pass
            if process in self.processes: self.processes.remove(process)
        self.kind = None
        self.application_processes = []
        self.env.pop('JAVA_TOOL_OPTIONS', None)

    def _perform(self, request):
        action = request.get('action')
        if action == 'launch':
            if self.kind: raise ValueError('Close the session before launching another application')
            kind = request.get('kind')
            if kind not in {'web', 'desktop'}: raise ValueError('Choose web or desktop')
            file_page = kind == 'web' and str(request.get('url', '')).startswith('file://')
            if not request.get('command') and not file_page: raise ValueError('A launch command is required')
            self.kind = kind
            if self.behavior_version >= 1 and kind == 'desktop':
                # GUI verification uses the visible Xvfb platform. Unit-test
                # commands may separately opt into offscreen execution.
                self.env.pop('QT_QPA_PLATFORM', None)
            if kind == 'desktop' and request.get('toolkit') == 'swing':
                self.env['JAVA_TOOL_OPTIONS'] = ('-Xbootclasspath/a:/usr/share/java/java-atk-wrapper.jar '
                    '-Djavax.accessibility.assistive_technologies=org.GNOME.Accessibility.AtkWrapper '
                    '-Djava.library.path=/usr/lib/jni')
            app = self.spawn(['bash', '-c', request['command']], 'application') if request.get('command') else None
            if kind == 'web':
                from playwright.sync_api import sync_playwright
                # The controller driver and project tests may pin different
                # Playwright versions. Only the driver inherits this cache;
                # self.env retains the project's environment for its process.
                previous = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
                os.environ['PLAYWRIGHT_BROWSERS_PATH'] = os.environ.get('DAEDALUS_CONTROLLER_BROWSERS_PATH', previous or '/root/.cache/ms-playwright')
                try:
                    self.playwright = sync_playwright().start()
                finally:
                    if previous is None: os.environ.pop('PLAYWRIGHT_BROWSERS_PATH', None)
                    else: os.environ['PLAYWRIGHT_BROWSERS_PATH'] = previous
                executable = self.playwright.chromium.executable_path
                if not Path(executable).is_file() or not os.access(executable, os.X_OK):
                    raise UIFailure({'error': 'The controller browser is missing or not executable; repair the worker browser installation.',
                        'category': 'environment', 'environment_fault': True, 'phase': 'browser_preparation',
                        'managed_path': executable})
                self.browser = self.playwright.chromium.launch(headless=False, env=self.env,
                    args=['--no-sandbox', '--disable-dev-shm-usage'])
                self.page = self.browser.new_page(viewport={'width': 1200, 'height': 720})
                self.page.set_default_timeout(10000)
                url = request.get('url') or 'http://127.0.0.1:8000'
                deadline = time.monotonic()+25
                while True:
                    try:
                        self.page.goto(url, timeout=3000, wait_until='domcontentloaded'); break
                    except Exception:
                        if time.monotonic() > deadline or app is not None and app.poll() is not None: raise
                        time.sleep(.2)
            else:
                deadline = time.monotonic()+25
                while True:
                    if app.poll() is not None: raise RuntimeError('Application exited: '+self.log_tail())
                    observed = self.perform({'action':'observe'})
                    if any(n.get('showing') and n['role'] not in {'application','desktop frame'}
                           for n in observed.get('nodes',[])):
                        break
                    if time.monotonic()>deadline: raise RuntimeError('Application launched but no visible accessible window appeared: '+self.log_tail())
                    time.sleep(.2)
            return self.perform({'action': 'observe'})
        if not self.kind: raise ValueError('Launch an application first')
        if self.kind == 'desktop':
            response = subprocess.run(['/usr/bin/python3', str(Path(__file__).with_name('coder_accessibility.py'))],
                input=json.dumps({**request, '_behavior_version': self.behavior_version}), text=True, capture_output=True, env=self.env, timeout=15)
            try: result = json.loads(response.stdout)
            except ValueError: raise RuntimeError(response.stderr[-2000:] or 'Accessibility service returned no data')
            if response.returncode:
                if self.behavior_version >= 1: raise UIFailure({**result, 'session_id': self.session_id})
                raise ValueError(result.get('error', 'Accessibility action failed'))
            return result
        if action == 'observe':
            return {'url': self.page.url, 'title': self.page.title(),
                    'accessibility': self.page.locator('body').aria_snapshot()[:14000],
                    'nodes': self.page.locator('body').evaluate('''body => Array.from(body.querySelectorAll('input,textarea,select,button,output,label,[role],[id]')).slice(0,500).map(el => {
                      const tag=el.tagName.toLowerCase();
                      const editable=el.matches('input:not([type=hidden]):not([type=button]):not([type=submit]):not([type=checkbox]):not([type=radio]),textarea') && !el.disabled && !el.readOnly;
                      const labels=el.labels ? Array.from(el.labels).map(x=>x.innerText).join(' ').trim() : '';
                      const name=el.getAttribute('aria-label') || (el.getAttribute('aria-labelledby') || '').split(/\\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' ').trim() || labels || (tag==='button' ? el.innerText : '');
                      let selector;
                      if(el.id) selector='#'+CSS.escape(el.id);
                      else { let n=el,parts=[]; while(n && n!==body) { parts.unshift(n.tagName.toLowerCase()+':nth-child('+(Array.from(n.parentElement.children).indexOf(n)+1)+')');n=n.parentElement; } selector='body > '+parts.join(' > '); }
                      return {selector,tag,name,role:el.getAttribute('role')||(tag==='label'?'label':tag==='output'?'output':editable?'textbox':tag==='button'?'button':tag),editable,interactive_ancestor:!!el.parentElement?.closest('button,[role=button],input,textarea'),activatable:el.matches('button,input[type=button],input[type=submit],[role=button]')&&!el.disabled,showing:!!el.getClientRects().length,text:el.innerText||'',...(editable?{value:el.value}:{})};
                    })''')}
        if action == 'reload': self.page.reload(wait_until='domcontentloaded')
        elif action == 'screenshot':
            path = self.directory/'browser.png'; self.page.screenshot(path=str(path)); return {'screenshot': str(path)}
        else:
            target = (self.page.locator(request['selector']) if request.get('selector') else
                      self.page.get_by_role(request.get('role', 'button'), name=request.get('name', ''), exact=True))
            if action == 'click': target.click()
            elif action == 'fill': target.fill(str(request.get('value', '')))
            elif action == 'text': return {'text': target.inner_text()}
            elif action == 'value': return {'value': target.input_value()}
            elif action == 'press': target.press(str(request['value']))
            else: raise ValueError('Unsupported browser action')
        return self.perform({'action': 'observe'})

    def log_tail(self):
        path = self.directory/'application.log'
        if not path.exists(): return ''
        with path.open('rb') as stream:
            stream.seek(0, 2); stream.seek(max(0, stream.tell()-4000))
            return stream.read().decode(errors='replace')

    def close(self):
        if self.browser: self.browser.close()
        if self.playwright: self.playwright.stop()
        for process in reversed(self.processes):
            if process.poll() is None:
                try: os.killpg(process.pid, signal.SIGTERM); process.wait(timeout=3)
                except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait()
                except ProcessLookupError: pass


def serve(args):
    session = UISession(args.directory, args.vnc_port)
    if args.launch:
        session.perform(json.loads(Path(args.launch).read_text()))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            try:
                length = int(self.headers.get('content-length', '0'))
                if not 0 < length <= 16000: raise ValueError('Invalid action size')
                data = session.perform(json.loads(self.rfile.read(length)))
                status = 200
            except Exception as error:
                data, status = {**getattr(error, 'details', {'error': str(error)}),
                                'log_tail': getattr(error, 'details', {}).get('log_tail', session.log_tail() if session.kind else '')}, 422
            body = json.dumps(data).encode(); self.send_response(status)
            self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body)))
            self.end_headers(); self.wfile.write(body)
    server = HTTPServer(('127.0.0.1', args.port), Handler)
    server.timeout = .5
    (Path(args.directory)/'ready.json').write_text(json.dumps({'port': server.server_port, 'pid': os.getpid()}))
    deadline = time.monotonic()+args.seconds if args.seconds else float('inf')
    try:
        while time.monotonic() < deadline: server.handle_request()
    finally:
        server.server_close(); session.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--seconds', type=int, default=900)
    parser.add_argument('--vnc-port', type=int)
    parser.add_argument('--launch')
    serve(parser.parse_args())
