"""Bounded child calls for controller browser probes; no project code runs on the host."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit
from coder_sandbox import Sandbox, IsolationUnavailable


def browser_options():
    proxy = os.environ.get('DAEDALUS_HTTP_PROXY')
    return {'proxy': {'server': proxy, 'bypass': '127.0.0.1,localhost'}} if proxy else {}


def invoke(module, function, args, kwargs, *, writable=(), readonly=(), timeout=180, boundary=None):
    from coder_patch_runtime import stop_process
    import time
    with tempfile.TemporaryDirectory(prefix='daedalus-probe-') as directory:
        folder = Path(directory)
        request, result = folder / 'request.json', folder / 'result.json'
        def urls(value):
            if isinstance(value, str) and value.startswith('http://127.0.0.1:'):
                parts = urlsplit(value)
                return [urlunsplit((parts.scheme, parts.netloc, '', '', ''))]
            if isinstance(value, (list, tuple)):
                return [u for item in value for u in urls(item)]
            if isinstance(value, dict):
                return [u for item in value.values() for u in urls(item)]
            return []
        command = [sys.executable, str(Path(__file__).resolve()), str(request), str(result)]
        modules = list(Path(__file__).parent.glob('*.py'))
        with Sandbox(command, cwd=folder, writable=[folder, *writable], readonly=[*modules, *readonly],
                     environment={'PATH': os.environ.get('PATH', '/usr/bin:/bin')}, network_urls=urls([args, kwargs])) as box:
            def translate(value):
                if isinstance(value, str): return box.translate_url(value)
                if isinstance(value, (list, tuple)): return [translate(v) for v in value]
                if isinstance(value, dict): return {k: translate(v) for k, v in value.items()}
                return value
            request.write_text(json.dumps({'module': module, 'function': function, 'args': translate(args), 'kwargs': translate(kwargs)}))
            with (folder / 'stderr.log').open('w') as log:
                process = subprocess.Popen(box.args, env=box.env, stdout=log, stderr=log, start_new_session=True)
                started = time.monotonic()
                try:
                    while process.poll() is None:
                        if boundary: boundary()
                        if time.monotonic() - started >= timeout:
                            raise TimeoutError('Isolated browser exceeded its command allowance')
                        time.sleep(.1)
                finally:
                    stop_process(process)
            if not result.is_file():
                raise IsolationUnavailable('Browser child failed: ' + (folder / 'stderr.log').read_text(errors='replace')[-1500:])
            response = json.loads(result.read_text())
            if 'error' in response:
                raise RuntimeError(response['error'])
            return response['result']


if __name__ == '__main__':
    request = json.loads(Path(sys.argv[1]).read_text())
    allowed = {('coder_page_guard', 'load_errors'), ('coder_frontend_guard', 'submit_forms'), ('coder_browser', 'browser_check'),
               ('coder_policy7_visual', 'capture_browser'), ('coder_workflow_guard', 'run_workflows')}
    try:
        if (request['module'], request['function']) not in allowed:
            raise ValueError('Unsupported isolated probe')
        function = getattr(importlib.import_module(request['module']), request['function'])
        result = {'result': function(*request['args'], **request['kwargs'])}
    except Exception as error:
        result = {'error': type(error).__name__ + ': ' + str(error)}
    Path(sys.argv[2]).write_text(json.dumps(result))
