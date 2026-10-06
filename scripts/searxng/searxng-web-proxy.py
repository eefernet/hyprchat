#!/usr/bin/env python3
import asyncio
import ipaddress
import os
import socket
import urllib.parse

LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8899"))
ALLOWED_CLIENTS = {x.strip() for x in os.getenv("ALLOWED_CLIENTS", "127.0.0.1,192.168.1.120").split(",") if x.strip()}
ALLOWED_PORTS = {int(x) for x in os.getenv("ALLOWED_PORTS", "80,443").split(",") if x.strip().isdigit()}
MAX_HEADER = 65536
CONNECT_TIMEOUT = 12


def _public_ipv4(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_global and not ip.is_multicast


async def resolve_public_ipv4(host: str) -> str:
    host = (host or "").strip().strip("[]").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("blocked hostname")
    try:
        ip = ipaddress.ip_address(host)
        if _public_ipv4(str(ip)):
            return str(ip)
        raise ValueError("blocked destination")
    except ValueError as e:
        if str(e) == "blocked destination":
            raise
    loop = asyncio.get_running_loop()
    # Reject mixed public/private answers before choosing an IPv4 address.
    # Connect to that exact address below, never resolve the hostname twice.
    infos = await loop.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or any(not ip.is_global or ip.is_multicast for ip in addresses):
        raise ValueError("blocked DNS answer")
    for info in infos:
        ip = info[4][0]
        if _public_ipv4(ip):
            return ip
    raise ValueError("no public ipv4 address")


def _client_allowed(writer: asyncio.StreamWriter) -> bool:
    peer = writer.get_extra_info("peername")
    if not peer:
        return False
    host = peer[0]
    return host in ALLOWED_CLIENTS


async def _read_header(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_HEADER:
            raise ValueError("header too large")
    head, sep, rest = data.partition(b"\r\n\r\n")
    if not sep:
        raise ValueError("bad request")
    return head + sep, rest


def _send(writer: asyncio.StreamWriter, status: str) -> None:
    writer.write(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode("ascii"))


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def _split_host_port(target: str, default_port: int | None = None) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit("//" + target)
    if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("invalid CONNECT target")
    if not parsed.hostname or not (parsed.port or default_port):
        raise ValueError("missing host or port")
    return parsed.hostname, parsed.port or default_port


async def connect_public(host: str, port: int):
    async def connect():
        ip = await resolve_public_ipv4(host)
        return await asyncio.open_connection(ip, port)
    return await asyncio.wait_for(connect(), CONNECT_TIMEOUT)


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    remote_writer = None
    try:
        if not _client_allowed(writer):
            _send(writer, "403 Forbidden")
            await writer.drain()
            return
        header, body = await asyncio.wait_for(_read_header(reader), CONNECT_TIMEOUT)
        text = header.decode("iso-8859-1", errors="replace")
        lines = text.split("\r\n")
        method, target, version = lines[0].split(" ", 2)
        method_u = method.upper()

        if method_u == "CONNECT":
            host, port = _split_host_port(target)
            if port not in ALLOWED_PORTS:
                raise ValueError("blocked port")
            remote_reader, remote_writer = await connect_public(host, port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            if body:
                remote_writer.write(body)
                await remote_writer.drain()
            await asyncio.gather(_pipe(reader, remote_writer), _pipe(remote_reader, writer), return_exceptions=True)
            return

        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("absolute proxy URL required")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in ALLOWED_PORTS:
            raise ValueError("blocked port")
        host = parsed.hostname
        remote_reader, remote_writer = await connect_public(host, port)
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        out_lines = [f"{method} {path} {version}"]
        saw_host = False
        for line in lines[1:]:
            if not line:
                continue
            name = line.split(":", 1)[0].lower()
            if name == "proxy-connection":
                continue
            if name == "connection":
                out_lines.append("Connection: close")
                continue
            if name == "host":
                saw_host = True
            out_lines.append(line)
        if not saw_host:
            out_lines.append(f"Host: {host}")
        remote_writer.write(("\r\n".join(out_lines) + "\r\n\r\n").encode("iso-8859-1") + body)
        await remote_writer.drain()
        await asyncio.gather(_pipe(reader, remote_writer), _pipe(remote_reader, writer), return_exceptions=True)
    except Exception:
        try:
            _send(writer, "502 Bad Gateway")
            await writer.drain()
        except Exception:
            pass
    finally:
        if remote_writer is not None:
            try:
                remote_writer.close()
                await remote_writer.wait_closed()
            except Exception:
                pass
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def main() -> None:
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
