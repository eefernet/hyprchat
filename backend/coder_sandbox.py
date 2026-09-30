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
             'DOTNET_CLI_TELEMETRY_OPTOUT', 'PLAYWRIGHT_BROWSERS_PATH', 'NODE_V8_COVERAGE',
             'NODE_OPTIONS', 'APP_DB_PATH', 'DATABASE_URL', 'DB_PATH', 'OLLAMA_API_BASE',
             'AIDER_ANALYTICS_DISABLE', 'LITELLM_LOCAL_MODEL_COST_MAP', 'ALLOW_SHORT_CONTEXT_WINDOWS'}
    return {k: str(v) for k, v in environment.items() if k in names or k.startswith('DAEDALUS_')}


def toolchain_mounts(command):
    paths = ['/usr/bin', '/usr/lib', '/usr/lib64', '/usr/libexec', '/usr/share', '/usr/include',
             '/usr/local/bin', '/usr/local/lib', '/usr/local/share', '/usr/local/include',
             '/bin', '/sbin', '/lib', '/lib64', '/etc/alternatives', '/etc/ssl/certs',
             '/etc/ca-certificates', '/etc/ld.so.cache', '/etc/nsswitch.conf', '/etc/resolv.conf',
             '/etc/hosts', '/etc/passwd', '/etc/group', '/etc/localtime',
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
                 network_urls=(), listen_port=None, runtime=None):
        self.command = list(command)
        self.cwd = Path(cwd).absolute()
        self.writable = [Path(p).absolute() for p in writable]
        self.readonly = [Path(p).absolute() for p in readonly]
        self.environment = clean_environment(environment if environment is not None else {k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'LC_ALL', 'TZ'}})
        self.urls = list(dict.fromkeys(network_urls))
        self.listen_port = listen_port
        self.runtime = Path(runtime).absolute() if runtime else None
        self.servers = []
        self.temporary = None

    def translate_url(self, value):
        for i, original in enumerate(self.urls):
            target = urlsplit(original)
            if value.startswith(original.rstrip('/')):
                alias = urlunsplit((target.scheme, f'upstream-{i}.invalid:{target.port}', target.path, '', ''))
                return alias.rstrip('/') + value[len(original.rstrip('/')):]
        return value

    def __enter__(self):
        try:
            return self._enter()
        except BaseException:
            self.__exit__()
            raise

    def _enter(self):
        if sys.platform != 'linux' or not shutil.which('bwrap'):
            raise IsolationUnavailable('Project execution requires Linux bubblewrap with user and network namespaces')
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
                    else:
                        remote = public_connection(host, port)
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
        environment.update(HOME=str(home), TMPDIR=str(tmp), PYTHONNOUSERSITE='1',
            PYTHONDONTWRITEBYTECODE='1', CARGO_HOME=str(home / '.cargo'), GOPATH=str(home / 'go'),
            GOMODCACHE=str(home / 'go/pkg/mod'), DOTNET_CLI_HOME=str(home / '.dotnet'),
            DOTNET_NOLOGO='1', DOTNET_CLI_TELEMETRY_OPTOUT='1',
            RUSTUP_HOME='/root/.rustup', DOTNET_ROOT='/root/.dotnet',
            PLAYWRIGHT_BROWSERS_PATH='/root/.cache/ms-playwright',
            PIP_REQUIRE_VIRTUALENV='true')
        spec = private / 'launch.json'
        spec.write_text(json.dumps({'command': self.command, 'cwd': str(self.cwd), 'environment': environment,
                                    'ipc': str(ipc), 'listen_port': self.listen_port}))
        module = Path(__file__).resolve()
        mounts = [*toolchain_mounts(self.command), module, spec, *self.readonly, self.cwd]
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
        self.args = [*args, '--chdir', str(self.cwd), '--', '/usr/bin/python3', str(module), '--child', str(spec)]
        self.env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        return self

    def __exit__(self, *exception):
        for server, thread in reversed(self.servers):
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        if self.temporary:
            self.temporary.cleanup()


def child_main(spec):
    """Namespace-local dependency proxy and optional reverse service endpoint."""
    config = json.loads(Path(spec).read_text())
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
    url = f'http://127.0.0.1:{proxy.server_port}'
    environment.update({k: url for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']})
    environment.update(NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost', DAEDALUS_HTTP_PROXY=url,
                       DAEDALUS_SANDBOXED='1')
    java_proxy = f'-Dhttp.proxyHost=127.0.0.1 -Dhttp.proxyPort={proxy.server_port} -Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort={proxy.server_port}'
    environment.update(MAVEN_OPTS=java_proxy, GRADLE_OPTS=java_proxy)
    result = subprocess.run(config['command'], cwd=config['cwd'], env=environment)
    return result.returncode


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--child':
        raise SystemExit('Sandbox entrypoint requires --child <supervisor launch spec>')
    raise SystemExit(child_main(sys.argv[2]))
