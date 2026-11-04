"""Process boundary for project code. The supervisor and its ledger stay outside.

The child has a private filesystem/process/network namespace. Dependency traffic
uses a public-address-only proxy; selected controller services use exact, scoped
forwarders. There is no route to the worker API, host loopback or LAN by default.
This module's child entrypoint uses only the standard library.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import select
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import uuid
from urllib.parse import urlsplit, urlunsplit


class IsolationUnavailable(RuntimeError):
    failure_category = 'environment'


def relay(left, right):
    """Bounded idle lifetime; closing either endpoint closes the tunnel."""
    try:
        while True:
            ready, _, _ = select.select([left, right], [], [], 3600)
            if not ready:
                return
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                (right if source is left else left).sendall(data)
    except OSError:
        return


def public_connection(host, port):
    if port not in {80, 443}:
        raise ValueError('Dependency egress permits HTTP(S) ports only')
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('Private, local and reserved network destinations are unavailable')
    # Connect to the validated address, without a second DNS lookup.
    error = None
    for family, kind, protocol, _, address in addresses:
        connection = socket.socket(family, kind, protocol)
        connection.settimeout(30)
        try:
            connection.connect(address)
            return connection
        except OSError as failure:
            error = failure
            connection.close()
    raise error or OSError('No usable dependency address')


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class TCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_server(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def clean_environment(environment):
    """An allowlist, not a growing list of names of known secret providers."""
    names = {'PATH', 'LANG', 'LC_ALL', 'TZ', 'TERM', 'NO_COLOR', 'CI', 'PORT', 'HOST',
             'HOME', 'TMPDIR', 'PYTHONPATH', 'PYTHONDONTWRITEBYTECODE', 'PYTHONNOUSERSITE',
             'VIRTUAL_ENV', 'JAVA_HOME', 'DOTNET_ROOT', 'RUSTUP_HOME', 'CARGO_HOME',
             'GOCACHE', 'GOPATH', 'GOMODCACHE', 'DOTNET_CLI_HOME', 'DOTNET_NOLOGO',
             'DOTNET_CLI_TELEMETRY_OPTOUT', 'PLAYWRIGHT_BROWSERS_PATH', 'PLAYWRIGHT_SKIP_BROWSER_GC', 'NODE_V8_COVERAGE',
             'NODE_OPTIONS', 'APP_DB_PATH', 'DATABASE_URL', 'DB_PATH', 'OLLAMA_API_BASE',
             'PIP_CACHE_DIR', 'npm_config_cache', 'npm_config_store_dir', 'YARN_CACHE_FOLDER', 'PNPM_HOME', 'COMPOSER_CACHE_DIR', 'BUNDLE_USER_CACHE', 'BUNDLE_PATH', 'MAVEN_OPTS', 'GRADLE_USER_HOME', 'NUGET_PACKAGES', 'AIDER_ANALYTICS_DISABLE', 'LITELLM_LOCAL_MODEL_COST_MAP', 'ALLOW_SHORT_CONTEXT_WINDOWS'}
    return {k: str(v) for k, v in environment.items() if k in names or k.startswith('DAEDALUS_')}


def toolchain_mounts(command):
    paths = ['/usr/bin', '/usr/lib', '/usr/lib64', '/usr/libexec', '/usr/share', '/usr/include',
             '/usr/local/bin', '/usr/local/lib', '/usr/local/share', '/usr/local/include',
             '/bin', '/sbin', '/lib', '/lib64', '/etc/alternatives', '/etc/ssl/certs',
             '/etc/ca-certificates', '/etc/ld.so.cache', '/etc/nsswitch.conf', '/etc/resolv.conf',
             '/etc/hosts', '/etc/passwd', '/etc/group', '/etc/localtime',
             '/etc/dbus-1', '/etc/fonts', '/etc/X11',
             '/etc/maven/m2.conf', '/etc/maven/logging',
             '/root/.cargo/bin', '/root/.rustup/toolchains', '/root/.rustup/settings.toml',
             '/root/.dotnet', '/root/.cache/ms-playwright']
    paths.extend(str(p) for p in Path('/etc').glob('java-*-openjdk'))
    # A private test/deployment venv may reuse another installed venv via .pth.
    # Mount only its dependency prefix, never the surrounding worker state.
    dependencies = [str(Path(p) / '_dependency_marker') for p in sys.path if Path(p).name == 'site-packages']
    for binary in [sys.executable, *command[:1], *dependencies]:
        path = Path(binary)
        for parent in path.parents:
            if (parent / 'pyvenv.cfg').is_file():
                paths.append(str(parent))
                link = parent / 'bin/python'
                if link.is_symlink() and link.readlink().is_absolute():
                    paths.append(str(link.readlink().parent.parent))
                interpreter = link.resolve()
                if interpreter.is_file() and interpreter.parent.name == 'bin':
                    paths.append(str(interpreter.parent.parent))
                break
    return [Path(p) for p in dict.fromkeys(paths) if Path(p).exists()]


class Sandbox(AbstractContextManager):
    """Create a single operation boundary, including optional service forwarding.

    readonly/writable are supervisor-selected paths, never model input. Callers
    must keep revision repositories, operation databases and evidence ledgers out
    of them. Runtime/cache directories are distinct from those trusted stores.
    """
    def __init__(self, command, *, cwd, writable=(), readonly=(), environment=None,
                 network_urls=(), listen_port=None, runtime=None, short_tmp=False,
                 aliases=(), timeout=None, memory_mib=None, managed_application=False, process_trace=None, runtime_profile=None, resource_group=None, public_network=True):
        self.command = list(command)
        self.cwd = Path(cwd).absolute()
        from coder_mount_policy import private_paths
        self.writable = [Path(p).absolute() for p in writable]
        if environment and environment.get('DAEDALUS_RUNTIME_POLICY') == '1':
            self.writable += private_paths(environment)
        self.readonly = [Path(p).absolute() for p in readonly]
        self.environment = clean_environment(environment if environment is not None else {k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'LC_ALL', 'TZ'}})
        self.urls = list(dict.fromkeys(network_urls))
        self.listen_port = listen_port
        self.runtime = Path(runtime).absolute() if runtime else None
        # Chromium creates Unix sockets below TMPDIR (108-byte path ceiling).
        self.short_tmp = short_tmp or os.environ.get('HEPHAESTUS_EXECUTOR')=='docker'
        self.aliases = list(aliases)
        self.timeout = timeout
        self.memory_mib = memory_mib
        self.managed_application = managed_application
        self.process_trace = process_trace
        self.runtime_profile = runtime_profile
        self.resource_group = resource_group
        self.public_network = public_network
        self.servers = []
        self.temporary = None
        self.cache_lease = None
        self.execution_identity = None

    def translate_url(self, value):
        for i, original in enumerate(self.urls):
            target = urlsplit(original)
            if value.startswith(original.rstrip('/')):
                host = '127.0.0.1' if self.environment.get('DAEDALUS_LOOPBACK_ENDPOINTS') == '1' else f'upstream-{i}.invalid'
                alias = urlunsplit((target.scheme, f'{host}:{target.port}', target.path, '', ''))
                return alias.rstrip('/') + value[len(original.rstrip('/')):]
        return value

    def __enter__(self):
        try:
            return self._enter()
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def _enter(self):
        docker = os.environ.get('HEPHAESTUS_EXECUTOR') == 'docker'
        if sys.platform != 'linux' or (not docker and not shutil.which('bwrap')):
            raise IsolationUnavailable('Project execution requires Linux bubblewrap with user and network namespaces')
        cache = self.environment.get('DAEDALUS_ENVIRONMENT_ROOT')
        if cache:
            from coder_engine_cache import lease
            self.cache_lease = lease(cache)
            self.cache_lease.__enter__()
        self.temporary = tempfile.TemporaryDirectory(prefix='daedalus-sandbox-')
        private = Path(self.temporary.name)
        ipc = private / 'ipc'; ipc.mkdir(mode=0o700)
        runtime = self.runtime or private / 'runtime'
        runtime.mkdir(parents=True, exist_ok=True)
        home, tmp = runtime / 'home', runtime / 'tmp'
        home.mkdir(exist_ok=True); tmp.mkdir(exist_ok=True)
        endpoints = {}
        for i, value in enumerate(self.urls):
            url = urlsplit(value)
            if url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost'} or not url.port:
                raise ValueError('Sandbox capabilities must identify an exact controller loopback HTTP service')
            endpoints[(f'upstream-{i}.invalid', url.port)] = ('127.0.0.1', url.port)

        class Gateway(socketserver.StreamRequestHandler):
            def handle(handler):
                try:
                    request = json.loads(handler.rfile.readline(16384))
                    host, port = request['host'], int(request['port'])
                    if (host, port) in endpoints:
                        remote = socket.create_connection(endpoints[(host, port)], timeout=30)
                    elif self.public_network:
                        remote = public_connection(host, port)
                    else: raise OSError('Public network access is disabled for this operation')
                    handler.wfile.write(b'OK\n'); handler.wfile.flush()
                    with remote:
                        relay(handler.connection, remote)
                except (OSError, ValueError, KeyError, TypeError):
                    try: handler.wfile.write(b'DENIED\n')
                    except OSError: pass

        self.servers.append(start_server(UnixServer(str(ipc / 'gateway.sock'), Gateway)))
        if self.listen_port:
            class Forward(socketserver.BaseRequestHandler):
                def handle(handler):
                    try:
                        with socket.socket(socket.AF_UNIX) as remote:
                            remote.connect(str(ipc / 'service.sock'))
                            relay(handler.request, remote)
                    except OSError:
                        pass
            self.servers.append(start_server(TCPServer(('127.0.0.1', self.listen_port), Forward)))
        environment = {k: self.translate_url(v) for k, v in self.environment.items()}
        environment.update(HOME=str(home), TMPDIR='/tmp/daedalus' if self.short_tmp else str(tmp), PYTHONNOUSERSITE='1',
            PYTHONDONTWRITEBYTECODE='1', CARGO_HOME=environment.get('CARGO_HOME', str(home / '.cargo')), GOPATH=environment.get('GOPATH', str(home / 'go')),
            GOMODCACHE=environment.get('GOMODCACHE', str(home / 'go/pkg/mod')), DOTNET_CLI_HOME=environment.get('DOTNET_CLI_HOME', str(home / '.dotnet')),
            DOTNET_NOLOGO='1', DOTNET_CLI_TELEMETRY_OPTOUT='1',
            RUSTUP_HOME='/root/.rustup', DOTNET_ROOT='/usr/share/dotnet' if docker else '/root/.dotnet',
            PLAYWRIGHT_BROWSERS_PATH=environment.get('PLAYWRIGHT_BROWSERS_PATH', os.environ.get('PLAYWRIGHT_BROWSERS_PATH','/root/.cache/ms-playwright')),
            DAEDALUS_CONTROLLER_BROWSERS_PATH=os.environ.get('DAEDALUS_CONTROLLER_BROWSERS_PATH', os.environ.get('PLAYWRIGHT_BROWSERS_PATH','/root/.cache/ms-playwright')),
            PIP_REQUIRE_VIRTUALENV='true')
        spec = private / 'launch.json'
        spec.write_text(json.dumps({'command': self.command, 'cwd': str(self.cwd), 'environment': environment,
                                    'ipc': str(ipc), 'listen_port': self.listen_port,
                                    'trace_socket': str(ipc/'trace.sock') if docker and self.process_trace else None,
                                    'loopback_endpoints': list(endpoints) if self.environment.get('DAEDALUS_LOOPBACK_ENDPOINTS') == '1' else []}))
        module = Path(__file__).resolve()
        if docker:
            # Preserve the capability gateway and child protocol; replace only
            # the process/filesystem boundary. Toolchains come from the pinned image.
            writable = [*self.writable, runtime, ipc]
            # Default cwd mounts may be covered by a source write grant. Explicit
            # read grants are retained so contradictory child mounts fail closed.
            reads = [module, spec, *self.readonly]
            if not any(self.cwd == w or self.cwd.is_relative_to(w) for w in writable): reads.append(self.cwd)
            mounts = [{'source': str(p), 'target': str(p), 'writable': False} for p in reads if p.exists()]
            mounts += [{'source': str(p), 'target': str(p), 'writable': True} for p in writable]
            for key in ('PLAYWRIGHT_BROWSERS_PATH', 'DAEDALUS_CONTROLLER_BROWSERS_PATH'):
                browsers=Path(environment[key])
                if browsers.is_relative_to('/srv/hephaestus/control/browsers') and browsers.is_dir():
                    mounts.append({'source':str(browsers),'target':str(browsers),'writable':False})
            # SDK interpreters live in locked controller release virtualenvs.
            # Only those dependency prefixes are shared, never host /usr or /root.
            for p in toolchain_mounts(self.command):
                if (p/'pyvenv.cfg').is_file():
                    mounts.append({'source': str(p), 'target': str(p), 'writable': False})
            if self.short_tmp:
                mounts.append({'source': str(tmp), 'target': '/tmp/daedalus', 'writable': True})
            mounts += [{'source': str(source), 'target': str(target), 'writable': True}
                       for source, target in self.aliases]
            # Explicit children override parent mounts, as with bubblewrap.
            unique = {item['target']: item for item in mounts}
            if environment.get('DAEDALUS_RUNTIME_POLICY') == '1':
                from coder_mount_policy import mounts as policy_mounts
                canonical = policy_mounts([m['target'] for m in unique.values() if not m['writable']],
                                          [m['target'] for m in unique.values() if m['writable']])
                # Aliases retain their host source; policy validation operates on container destinations.
                unique = {m['target']: {**m, 'source': unique[m['target']]['source']} for m in canonical}
            scope = self.environment.get('DAEDALUS_RUNTIME_SCOPE') or __import__('hashlib').sha256(str(self.cwd).encode()).hexdigest()
            from hephaestus_scope import current
            identity = current() or json.loads(os.environ.get('HEPHAESTUS_EXECUTION_IDENTITY','{}'))
            request = {'id': uuid.uuid4().hex, 'scope': scope,
                       'identity':identity,
                       'profile': self.runtime_profile or os.environ.get('HEPHAESTUS_RUNTIME_PROFILE', 'core'),
                       'cwd': str(self.cwd), 'mounts': sorted(unique.values(), key=lambda x: len(Path(x['target']).parts)),
                       'command': ['/usr/bin/python3', str(module), '--child', str(spec)],
                       'memory_mib': self.memory_mib or (1024 if self.listen_port else 3072),
                       'timeout': self.timeout if self.timeout is not None else float(os.environ.get('HEPHAESTUS_COMMAND_CEILING', '900')),
                       'managed_application': self.managed_application}
            if self.resource_group: request['resource_group'] = self.resource_group
            if self.process_trace:
                request.update(trace_path=str(self.process_trace),trace_socket=str(ipc/'trace.sock'))
            launch = private/'broker.json'; launch.write_text(json.dumps(request))
            self.execution_identity = (request['id'],scope)
            self.args = [sys.executable, str(module.with_name('hephaestus_client.py')), str(launch)]
            self.env = {k: v for k, v in os.environ.items() if k in {'PATH','LANG','HEPHAESTUS_BROKER_SOCKET'}}
            return self
        mounts = [*toolchain_mounts(self.command), module, spec, *self.readonly, self.cwd]
        if environment.get('DAEDALUS_RUNTIME_POLICY') == '1':
            from coder_mount_policy import mounts as policy_mounts
            mounts = [*toolchain_mounts(self.command), module, spec, *self.readonly]
            if not any(self.cwd == w or self.cwd.is_relative_to(w) for w in self.writable): mounts.append(self.cwd)
            policy_mounts(mounts, self.writable)
        args = ['bwrap', '--unshare-user', '--unshare-pid', '--unshare-net', '--unshare-ipc',
                '--unshare-uts', '--new-session', '--die-with-parent', '--cap-drop', 'ALL',
                '--tmpfs', '/', '--dev', '/dev', '--proc', '/proc', '--tmpfs', '/tmp']
        # Read-only first; explicit writable children may override source mounts.
        seen = set()
        for path in sorted(mounts, key=lambda p: (len(p.parts), str(p))):
            if path in seen or not path.exists():
                continue
            seen.add(path)
            args.extend(['--ro-bind', str(path), str(path)])
        for path in [*self.writable, runtime, ipc]:
            if not path.exists():
                raise ValueError('Sandbox writable path does not exist: ' + str(path))
            args.extend(['--bind', str(path), str(path)])
        if self.short_tmp:
            # Chromium/DBus use Unix sockets beneath TMPDIR (108-byte limit).
            # Keep their path short inside this private namespace while the
            # backing files remain in the controller's disk-backed leased cache.
            args.extend(['--bind', str(tmp), '/tmp/daedalus'])
        self.args = [*args, '--chdir', str(self.cwd), '--', '/usr/bin/python3', str(module), '--child', str(spec)]
        self.env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        return self

    def __exit__(self, *exception):
        stop_error = None
        if self.execution_identity:
            identity,scope=self.execution_identity
            try:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.settimeout(60)
                    connection.connect(os.environ.get('HEPHAESTUS_BROKER_SOCKET','/run/hephaestus/broker.sock'))
                    connection.sendall(json.dumps({'action':'cancel','id':identity,'scope':scope}).encode()+b'\n')
                    with connection.makefile('rb') as reader: result=json.loads(reader.readline(65536))
                    if not result.get('result',{}).get('stopped'): raise RuntimeError('Container stop is unconfirmed')
            except (OSError,ValueError,RuntimeError) as error:
                stop_error=IsolationUnavailable('Hephaestus could not confirm the writer stopped: '+str(error))
            self.execution_identity=None
        for server, thread in reversed(self.servers):
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        if self.temporary:
            self.temporary.cleanup()
        if self.cache_lease:
            self.cache_lease.__exit__(*exception)
            self.cache_lease = None
        if stop_error: raise stop_error


def child_main(spec):
    """Namespace-local dependency proxy and optional reverse service endpoint."""
    config = json.loads(Path(spec).read_text())
    if config.get('trace_socket'):
        # No project process exists until the independent host tracer is attached.
        with socket.socket(socket.AF_UNIX) as barrier:
            barrier.settimeout(20); barrier.connect(config['trace_socket'])
            if barrier.recv(16)!=b'TRACED': raise RuntimeError('Independent process tracing is unavailable')
    ipc = Path(config['ipc'])

    def connect(host, port):
        remote = socket.socket(socket.AF_UNIX)
        remote.settimeout(3600)
        remote.connect(str(ipc / 'gateway.sock'))
        remote.sendall(json.dumps({'host': host, 'port': port}).encode() + b'\n')
        reply = bytearray()
        while not reply.endswith(b'\n') and len(reply) < 32:
            chunk = remote.recv(1)
            if not chunk:
                break
            reply.extend(chunk)
        if reply != b'OK\n':
            remote.close()
            raise OSError('Sandbox network destination denied')
        return remote

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_CONNECT(self):
            try:
                parsed = urlsplit('//' + self.path)
                with connect(parsed.hostname, parsed.port or 443) as remote:
                    self.send_response(200); self.end_headers()
                    relay(self.connection, remote)
            except (OSError, ValueError):
                self.send_error(502, 'Sandbox network destination unavailable')
        def forward(self):
            try:
                parsed = urlsplit(self.path)
                if parsed.scheme != 'http' or not parsed.hostname:
                    raise ValueError('An absolute HTTP URL is required')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 16 * 1024 * 1024 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('Unsupported proxy request body')
                body = self.rfile.read(length)
                with connect(parsed.hostname, parsed.port or 80) as remote:
                    path = urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
                    headers = ''.join(f'{k}: {v}\r\n' for k, v in self.headers.items()
                                      if k.lower() not in {'connection', 'proxy-connection', 'proxy-authorization'})
                    remote.sendall((f'{self.command} {path} HTTP/1.1\r\n' + headers + 'Connection: close\r\n\r\n').encode() + body)
                    while True:
                        data = remote.recv(65536)
                        if not data:
                            break
                        self.connection.sendall(data)
                self.close_connection = True
            except (OSError, ValueError):
                self.send_error(502, 'Sandbox network destination unavailable')
        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = forward

    proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    start_server(proxy)
    # These listeners exist only inside the child's network namespace. Each
    # tunnels one supervisor-granted endpoint through the existing capability
    # gateway; no other host loopback service becomes reachable. Keeping the
    # browser origin on loopback preserves Fetch Metadata and secure-context
    # behavior that an http://*.invalid proxy alias loses.
    forwarded_ports = set()
    for host, port in config.get('loopback_endpoints', []):
        if port in forwarded_ports:
            continue
        def forwarder(target_host, target_port):
            class Endpoint(socketserver.BaseRequestHandler):
                def handle(self):
                    try:
                        with connect(target_host, target_port) as remote:
                            relay(self.request, remote)
                    except OSError:
                        pass
            return Endpoint
        start_server(TCPServer(('127.0.0.1', port), forwarder(host, port)))
        forwarded_ports.add(port)
    if config.get('listen_port'):
        class Reverse(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    with socket.create_connection(('127.0.0.1', config['listen_port']), timeout=5) as remote:
                        relay(self.request, remote)
                except OSError:
                    pass
        start_server(UnixServer(str(ipc / 'service.sock'), Reverse))
    environment = config['environment']
    if environment.get('DAEDALUS_DOCUMENT_LIMITS')=='1':
        import resource
        resource.setrlimit(resource.RLIMIT_FSIZE,(64*1024**2,64*1024**2))
        resource.setrlimit(resource.RLIMIT_NOFILE,(256,256))
    url = f'http://127.0.0.1:{proxy.server_port}'
    environment.update({k: url for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']})
    environment.update(NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost', DAEDALUS_HTTP_PROXY=url,
                       DAEDALUS_SANDBOXED='1')
    java_proxy = f'-Dhttp.proxyHost=127.0.0.1 -Dhttp.proxyPort={proxy.server_port} -Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort={proxy.server_port}'
    environment.update(MAVEN_OPTS=(environment.get('MAVEN_OPTS', '') + ' ' + java_proxy).strip(),
                       GRADLE_OPTS=(environment.get('GRADLE_OPTS', '') + ' ' + java_proxy).strip())
    try:
        for name in json.loads(environment.get('DAEDALUS_PROBE_WRITES', '[]')):
            with tempfile.TemporaryFile(dir=name): pass
    except OSError as error:
        print('DAEDALUS_PREPARATION_FAILURE:' + json.dumps({'category': 'mount', 'path': name, 'errno': error.errno, 'detail': str(error)}), file=sys.stderr)
        return 125
    result = subprocess.run(config['command'], cwd=config['cwd'], env=environment)
    return result.returncode


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--child':
        raise SystemExit('Sandbox entrypoint requires --child <supervisor launch spec>')
    raise SystemExit(child_main(sys.argv[2]))
