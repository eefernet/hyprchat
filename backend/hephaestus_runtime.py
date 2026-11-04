"""Trusted Docker broker. Its Unix socket is available only to controllers.

Project containers receive neither this socket nor the Docker daemon socket.
Each request has a durable identity; duplicate dispatch is rejected, never rerun.
The client connection owns the execution lease, including queued operations.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import socketserver
import sqlite3
import subprocess
import threading
import time

GIB = 1024**3
ROOT = Path(os.environ.get('HEPHAESTUS_ROOT', '/srv/hephaestus'))
SOCKET = os.environ.get('HEPHAESTUS_BROKER_SOCKET', '/run/hephaestus/broker.sock')
IDENTITY = re.compile(r'[a-zA-Z0-9_-]{1,160}')


def execution_group(spec):
    group = spec.get('resource_group') or spec['id']
    if not IDENTITY.fullmatch(group): raise ValueError('Invalid operation resource group')
    return hashlib.sha256(group.encode()).hexdigest()[:24]


def group_unit(spec):
    return 'hephaestus-workloads-op'+execution_group(spec)+'.slice'


def validate_mount(source, target, writable, roots):
    path = Path(source)
    if not path.is_absolute() or not Path(target).is_absolute() or any(c in str(path)+target for c in ',\n\r\x00'):
        raise ValueError('Invalid mount path')
    resolved = path.resolve(strict=True)
    allowed = roots['writable' if writable else 'readonly']
    if not any(resolved == Path(root) or resolved.is_relative_to(Path(root)) for root in allowed):
        raise ValueError('Mount is outside controller-granted runtime roots: '+str(path))
    if any(Path(target) == p or p.is_relative_to(Path(target)) for p in
           (Path('/var/run/docker.sock'), Path('/run/hephaestus'), Path('/proc'), Path('/sys'), Path('/dev'))):
        raise ValueError('Mount overlaps a privileged path')
    return str(resolved)


def docker_args(spec, config):
    if not IDENTITY.fullmatch(spec.get('id', '')) or not IDENTITY.fullmatch(spec.get('scope', '')):
        raise ValueError('Execution identity and scope are required')
    if not isinstance(spec.get('identity'),dict) or not spec['identity'].get('owner'):
        raise ValueError('Authenticated owner identity is required')
    profile = config['profiles'].get(spec.get('profile'))
    if not profile or not re.fullmatch(r'(?:[^\s]+@)?sha256:[a-f0-9]{64}', profile['image']):
        raise ValueError('Runtime profile must identify an installed immutable image')
    memory = spec.get('memory_mib', 3072)
    if not isinstance(memory, int) or not 128 <= memory <= 3072: raise ValueError('Invalid memory allowance')
    command = spec.get('command')
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or '\x00' in x for x in command):
        raise ValueError('Expected an argument vector')
    mounts = spec.get('mounts', [])
    if not isinstance(mounts, list) or len(mounts) > 500: raise ValueError('Invalid mount list')
    args = ['docker', 'run', '--name', 'hephaestus-'+spec['id'], '--init', '--network', 'none',
            '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
            '--pids-limit', '256', '--memory', str(memory*1024**2), '--memory-swap', str(memory*1024**2),
            '--cpus', '3', '--cgroup-parent', group_unit(spec),
            '--label', 'hyprchat.managed=execution', '--label', 'hyprchat.scope='+spec['scope'],
            '--shm-size', '128m',
            '--workdir', spec['cwd']]
    if not any(mount.get('target')=='/tmp' for mount in mounts):
        args += ['--tmpfs','/tmp:rw,nosuid,nodev,size=67108864']
    for mount in mounts:
        writable = mount.get('writable') is True
        source = validate_mount(mount['source'], mount['target'], writable, config['roots'])
        args.extend(['--mount', f'type=bind,src={source},dst={mount["target"]}'+('' if writable else ',readonly')])
    # The namespace child receives a clean environment from its controller spec.
    args += [profile['image'], *command]
    return args


class Broker:
    def __init__(self, root, config):
        self.root, self.config = Path(root), config
        self.root.mkdir(parents=True, exist_ok=True)
        self.condition = threading.Condition()
        self.reserved = 0
        self.groups = {}
        self.cancelled = set()
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS executions(id TEXT PRIMARY KEY, scope TEXT, fingerprint TEXT, status TEXT, created REAL, result TEXT)')

    def connect(self):
        db = sqlite3.connect(self.root/'executions.sqlite', timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def reserve_identity(self, spec):
        fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            try:
                db.execute('INSERT INTO executions VALUES(?,?,?,?,?,NULL)',
                           (spec['id'], spec['scope'], fingerprint, 'queued', time.time()))
            except sqlite3.IntegrityError:
                previous = dict(db.execute('SELECT * FROM executions WHERE id=?', (spec['id'],)).fetchone())
                if previous['fingerprint'] != fingerprint: raise ValueError('Execution identity reused with different input')
                raise ValueError('Execution already dispatched; inspect its existing result instead of replaying it')

    def update(self, identity, status, result=None):
        with self.connect() as db:
            db.execute('UPDATE executions SET status=?,result=? WHERE id=?', (status, json.dumps(result), identity))

    def cancel(self, identity, scope):
        if not IDENTITY.fullmatch(identity): raise ValueError('Invalid execution identity')
        with self.connect() as db:
            row=db.execute('SELECT * FROM executions WHERE id=?',(identity,)).fetchone()
        if row is None: return {'stopped':True,'status':'not_dispatched'}
        if row['scope']!=scope: raise ValueError('Execution belongs to another scope')
        if row['status'] not in {'queued','running','stop_unconfirmed'}: return {'stopped':True,'status':row['status']}
        self.cancelled.add(identity)
        until=time.monotonic()+55
        while time.monotonic()<until:
            with self.connect() as db:
                status=db.execute('SELECT status FROM executions WHERE id=?',(identity,)).fetchone()[0]
            if status not in {'queued','running','stop_unconfirmed'}:
                self.cancelled.discard(identity)
                return {'stopped':True,'status':status}
            time.sleep(.1)
        raise TimeoutError('Container stop is unconfirmed; checkpoint publication must wait')

    @staticmethod
    def connected(connection):
        ready = selectors.DefaultSelector()
        try:
            ready.register(connection, selectors.EVENT_READ)
            if ready.select(0):
                # The launch is the only incoming frame. Any subsequent data or EOF cancels.
                return False
            return True
        finally: ready.close()

    def execute(self, spec, connection, emit):
        args = docker_args(spec, self.config)
        seconds = spec.get('timeout', 900)
        application = spec.get('managed_application') is True
        if not isinstance(seconds, (float, int)) or not (0 < seconds <= 86400 or (seconds == 0 and application)):
            raise ValueError('Invalid execution deadline')
        if application and spec.get('memory_mib',3072)>1024: raise ValueError('Application sessions are limited to 1 GiB')
        deadline = time.monotonic()+seconds if seconds else float('inf')
        memory = spec.get('memory_mib', 3072)*1024**2
        group = execution_group(spec)
        self.reserve_identity(spec)
        reserved = False; process = None; tracer = None; barrier = None
        outcome = {'exit_code': 125, 'failure_category': 'environment'}
        name = 'hephaestus-'+spec['id']
        try:
            with self.condition:
                while group not in self.groups and self.reserved+memory > 5*GIB:
                    if spec['id'] in self.cancelled or not self.connected(connection): raise InterruptedError('Execution cancelled while queued')
                    if time.monotonic() >= deadline: raise TimeoutError('Execution deadline expired while queued')
                    self.condition.wait(.2)
                if spec['id'] in self.cancelled or not self.connected(connection): raise InterruptedError('Execution cancelled before dispatch')
                if group in self.groups:
                    if self.groups[group]['memory'] != memory:
                        raise ValueError('An operation resource group cannot change its memory allowance')
                    self.groups[group]['users'] += 1
                else:
                    unit = Path('/run/systemd/system')/group_unit(spec)
                    unit.write_text('[Slice]\nMemoryMax='+str(memory)+'\nMemorySwapMax=0\nTasksMax=256\nCPUQuota=300%\n')
                    subprocess.run(['systemctl','daemon-reload'],check=True,timeout=20)
                    subprocess.run(['systemctl','start',unit.name],check=True,timeout=20)
                    cgroup=subprocess.check_output(['systemctl','show',unit.name,'-p','ControlGroup','--value'],text=True).strip()
                    group_path=Path('/sys/fs/cgroup')/cgroup.lstrip('/')
                    (group_path/'memory.oom.group').write_text('1')
                    self.groups[group] = {'memory':memory,'users':1,'cgroup':group_path}
                    self.reserved += memory
                reserved = True
            self.update(spec['id'], 'running')
            from hephaestus_storage import require_space, ensure_quota, quota_root
            require_space(ROOT)
            quota_paths={}
            for mount in spec.get('mounts',[]):
                if mount.get('writable'):
                    path,limit=quota_root(ROOT,mount['source']);quota_paths[path]=limit
            for path,limit in quota_paths.items(): ensure_quota(ROOT,path,limit)
            if spec.get('trace_path'):
                trace = Path(spec['trace_path'])
                validate_mount(str(trace.parent),str(trace.parent),True,self.config['roots'])
                for mount in spec.get('mounts',[]):
                    source=Path(mount['source']).resolve()
                    if trace.resolve()==source or trace.resolve().is_relative_to(source):
                        raise ValueError('Independent trace must remain outside all project mounts')
                trace_socket=Path(spec['trace_socket'])
                validate_mount(str(trace_socket.parent),str(trace_socket.parent),True,self.config['roots'])
                barrier=socket.socket(socket.AF_UNIX);barrier.bind(str(trace_socket));barrier.listen(1);barrier.settimeout(15)
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if barrier:
                with barrier.accept()[0] as child:
                    pid=int(subprocess.check_output(['docker','inspect','--format','{{.State.Pid}}',name],text=True).strip())
                    children=Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
                    pids=[str(pid),*children]
                    trace_args=['strace','-f','-q','--decode-pids=pidns','-yy','-s','65536','-o',spec['trace_path'],
                                '-e','trace=execve,execveat,clone,clone3,fork,vfork,chdir,fchdir,open,openat,openat2']
                    for target in pids: trace_args+=['-p',target]
                    tracer=subprocess.Popen(trace_args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    until=time.monotonic()+5
                    while True:
                        attached=all(re.search(r'^TracerPid:\s*'+str(tracer.pid)+r'\s*$',Path(f'/proc/{target}/status').read_text(),re.M) for target in pids)
                        if attached: break
                        if tracer.poll() is not None or time.monotonic()>until: raise RuntimeError('Independent host tracer failed to attach')
                        time.sleep(.02)
                    child.sendall(b'TRACED')
            emitted=0; output_limit=16*1024**2; reported_truncation=False
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            try:
                while True:
                    if spec['id'] in self.cancelled or not self.connected(connection): raise InterruptedError('Execution client disconnected')
                    if time.monotonic() >= deadline: raise TimeoutError('Execution command deadline exceeded')
                    if spec.get('trace_path') and Path(spec['trace_path']).stat().st_size>32*1024**2:
                        raise RuntimeError('Independent process trace exceeded 32 MiB')
                    for key, _ in selector.select(.1):
                        data = os.read(key.fileobj.fileno(), 32768)
                        if data:
                            bounded=data[:max(0,output_limit-emitted)];emitted+=len(bounded)
                            if bounded: emit({'output':base64.b64encode(bounded).decode()})
                            if len(bounded)<len(data) and not reported_truncation:
                                emit({'output':base64.b64encode(b'\n[Execution output truncated at 16 MiB]\n').decode()})
                                reported_truncation=True
                        else: selector.unregister(key.fileobj)
                    if process.poll() is not None and not selector.get_map(): break
                outcome = {'exit_code': process.wait(), 'id': spec['id']}
                events=dict(line.split() for line in (self.groups[group]['cgroup']/'memory.events').read_text().splitlines())
                if outcome['exit_code'] == 137 or int(events.get('oom_kill',0)):
                    outcome.update(failure_category='resource_limit',resource_events=events)
            finally: selector.close()
        except (TimeoutError, InterruptedError) as error:
            outcome = {'exit_code': 124 if isinstance(error, TimeoutError) else 130,
                       'failure_category': 'timeout' if isinstance(error, TimeoutError) else 'cancelled', 'error': str(error)}
        except Exception as error:
            outcome = {'exit_code': 125, 'failure_category': 'environment', 'error': str(error)}
        finally:
            # Killing docker's attach client alone does not stop the container.
            if process is not None:
                try:
                    removal=subprocess.run(['docker', 'rm', '-f', name],capture_output=True,text=True,timeout=30)
                    if removal.returncode and 'No such container' not in removal.stderr:
                        raise RuntimeError(removal.stderr[-2000:])
                except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
                    self.update(spec['id'],'stop_unconfirmed',{'error':str(error)[-2000:]})
                    # Keep the reservation until restart recovery proves it stopped.
                    raise RuntimeError('Container termination is unconfirmed: '+str(error)[-2000:]) from error
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            if tracer is not None:
                try: tracer.wait(timeout=5)
                except subprocess.TimeoutExpired: tracer.terminate();tracer.wait(timeout=5)
            if barrier is not None:
                barrier.close();Path(spec['trace_socket']).unlink(missing_ok=True)
            self.update(spec['id'], 'succeeded' if outcome['exit_code'] == 0 else 'failed', outcome)
            if reserved:
                with self.condition:
                    self.groups[group]['users'] -= 1
                    if not self.groups[group]['users']:
                        self.reserved -= memory; del self.groups[group]
                        subprocess.run(['systemctl','stop',group_unit(spec)],capture_output=True,timeout=20)
                        (Path('/run/systemd/system')/group_unit(spec)).unlink(missing_ok=True)
                    self.condition.notify_all()
        emit({'result': outcome})

    def recover(self):
        """No client survives a broker restart. Stop only this broker's recorded operations."""
        with self.connect() as db:
            rows = db.execute("SELECT id FROM executions WHERE status IN ('queued','running','stop_unconfirmed')").fetchall()
        for row in rows:
            removal=subprocess.run(['docker', 'rm', '-f', 'hephaestus-'+row['id']],capture_output=True,text=True,timeout=30)
            if removal.returncode and 'No such container' not in removal.stderr:
                raise RuntimeError('Cannot recover a previous container: '+removal.stderr[-2000:])
            self.update(row['id'], 'failed', {'exit_code': 130, 'failure_category': 'restart',
                                            'error': 'Runtime restarted; execution was stopped and not replayed'})


def serve():
    config = json.loads(Path('/etc/hephaestus/runtime.json').read_text())
    broker = Broker(ROOT/'control/broker', config)
    broker.recover()
    class Handler(socketserver.StreamRequestHandler):
        rbufsize = 0  # Do not prefetch cancellation bytes beyond the launch frame.
        def handle(self):
            def emit(frame):
                self.wfile.write(json.dumps(frame).encode()+b'\n'); self.wfile.flush()
            try:
                self.connection.settimeout(30)
                line = self.rfile.readline(1024*1024)
                if not line.endswith(b'\n'): raise ValueError('Oversized execution request')
                request = json.loads(line)
                self.connection.settimeout(None)
                if request.get('action')=='cancel':
                    emit({'result':broker.cancel(request['id'],request['scope'])});return
                broker.execute(request, self.connection, emit)
            except (BrokenPipeError, ConnectionResetError): pass
            except Exception as error:
                try: emit({'result': {'exit_code': 125, 'failure_category': 'environment', 'error': str(error)}})
                except OSError: pass
    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
    path = Path(SOCKET); path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with Server(SOCKET, Handler) as server:
        path.chmod(0o600)
        server.serve_forever()


if __name__ == '__main__': serve()
