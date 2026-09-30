"""The isolation boundary must hold against actual child processes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coder_sandbox import Sandbox, clean_environment, public_connection


def test_private_network_and_non_http_ports_are_rejected():
    for host, port in [('127.0.0.1', 80), ('192.168.1.201', 443), ('169.254.169.254', 80), ('::1', 443), ('example.com', 22)]:
        with pytest.raises(ValueError):
            public_connection(host, port)


def test_environment_does_not_inherit_unknown_credentials():
    env = clean_environment({'PATH': '/usr/bin', 'AWS_SECRET_ACCESS_KEY': 'hidden',
                             'GITHUB_TOKEN': 'hidden', 'HYPRCHAT_PASS': 'hidden', 'SSH_AUTH_SOCK': '/socket'})
    assert env == {'PATH': '/usr/bin'}


@pytest.mark.skipif(not shutil.which('bwrap'), reason='Linux Codebox isolation integration')
def test_child_can_write_only_its_workspace_and_cannot_reach_host(tmp_path):
    source = tmp_path / 'project'; source.mkdir()
    sibling = tmp_path / 'other-job'; sibling.mkdir()
    secret = sibling / 'private'; secret.write_text('must stay hidden')
    code = f'''
import pathlib,socket,json,os
result={{}}
pathlib.Path({str(source / 'created')!r}).write_text('local')
result['other_job_visible']=pathlib.Path({str(secret)!r}).exists()
try:
 pathlib.Path('/usr/local/lib/daedalus-forbidden-write').write_text('forbidden')
 result['host_write']=True
except OSError: result['host_write']=False
try:
 socket.create_connection(('192.168.1.201',8586),timeout=1).close()
 result['worker_reachable']=True
except OSError: result['worker_reachable']=False
print(json.dumps(result))
'''
    with Sandbox(['/usr/bin/python3', '-c', code], cwd=source, writable=[source]) as box:
        result = subprocess.run(box.args, env=box.env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'other_job_visible': False, 'host_write': False, 'worker_reachable': False}
    assert (source / 'created').read_text() == 'local'
    assert secret.read_text() == 'must stay hidden'


@pytest.mark.skipif(not shutil.which('bwrap'), reason='Linux Codebox isolation integration')
def test_cancellation_stops_background_descendants(tmp_path):
    import time
    from coder_patch_runtime import stop_process
    heartbeat = tmp_path / 'heartbeat'
    code = "import pathlib,time\np=pathlib.Path(" + repr(str(heartbeat)) + ")\nwhile True:\n p.write_text(str(time.time_ns()))\n time.sleep(.03)\n"
    launcher = 'import subprocess,time\nsubprocess.Popen(["/usr/bin/python3","-c",' + repr(code) + '], start_new_session=True)\ntime.sleep(60)'
    with Sandbox(['/usr/bin/python3', '-c', launcher], cwd=tmp_path, writable=[tmp_path]) as box:
        process = subprocess.Popen(box.args, env=box.env, start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while not heartbeat.exists() and time.monotonic() < deadline: time.sleep(.05)
            assert heartbeat.exists()
        finally:
            stop_process(process)
    before = heartbeat.read_text(); time.sleep(.2)
    assert heartbeat.read_text() == before


def test_inference_bridge_rejects_other_models_and_bills_supervisor(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading, time, urllib.request, urllib.error
    from coder_sandbox_agent import model_bridge
    from coder_worker_runtime import WorkerStore
    from context_policy import DEFAULTS
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/api/chat': self.server.bodies.append(body)
            data = b'{"message":{"role":"assistant","content":"ok"},"done":true}'
            self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler); server.bodies = []
    thread = threading.Thread(target=server.serve_forever); thread.start()
    store = WorkerStore(tmp_path / 'supervisor')
    store.create('op', 'job', 'code', {'model': 'local:coder', 'ollama_url': f'http://127.0.0.1:{server.server_port}',
        'settings': DEFAULTS, 'calls_remaining': 1, 'seconds_remaining': 30})
    store.update('op', status='running', started=time.time())
    try:
        with model_bridge(store, 'op') as bridge:
            def post(model):
                request = urllib.request.Request(bridge + '/api/chat', data=json.dumps({'model': model,
                    'messages': [{'role':'user','content':'small'}], 'options': {'num_ctx': 999999}}).encode(), headers={'Content-Type':'application/json'})
                return urllib.request.urlopen(request, timeout=5)
            with pytest.raises(urllib.error.HTTPError): post('different:model')
            assert store.get('op')['calls'] == 0
            with post('local:coder') as response: assert response.status == 200
            assert store.get('op')['calls'] == 1
            with pytest.raises(urllib.error.HTTPError): post('local:coder')
            assert len(server.bodies) == 1
            assert server.bodies[0]['options']['num_ctx'] == DEFAULTS['openhands_num_ctx']
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_closing_inference_bridge_disconnects_an_inflight_upstream(tmp_path):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import threading,time,urllib.request
    from coder_sandbox_agent import model_bridge
    from coder_worker_runtime import WorkerStore
    from context_policy import DEFAULTS
    started,disconnected=threading.Event(),threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            if self.path=='/api/show':
                self.send_response(200);self.end_headers();self.wfile.write(b'{}');return
            started.set();self.connection.settimeout(5)
            if self.rfile.read(1)==b'':disconnected.set()
    upstream=ThreadingHTTPServer(('127.0.0.1',0),Handler);upstream.daemon_threads=True
    thread=threading.Thread(target=upstream.serve_forever);thread.start()
    store=WorkerStore(tmp_path/'ledger')
    store.create('op','job','code',{'model':'local','ollama_url':f'http://127.0.0.1:{upstream.server_port}',
        'settings':DEFAULTS,'calls_remaining':2,'seconds_remaining':30})
    store.update('op',status='running',started=time.time())
    client=None
    try:
        with model_bridge(store,'op') as bridge:
            def request():
                try:
                    with urllib.request.urlopen(urllib.request.Request(bridge+'/api/chat',data=b'{"model":"local","messages":[]}'),timeout=6) as response:response.read()
                except OSError:pass
            client=threading.Thread(target=request);client.start()
            assert started.wait(5)
        assert disconnected.wait(2), 'Upstream inference survived the child boundary'
    finally:
        upstream.shutdown();upstream.server_close();thread.join()
        if client:client.join(7)
