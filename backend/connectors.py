"""
Connector discovery and execution for MCP servers and OpenAPI specs.

The chat loop still owns tool allow-listing. This module only turns persisted
connector definitions into Ollama-compatible function schemas and executes the
operation selected by that allow-list.
"""
import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import urllib.parse
from typing import Any
from datetime import datetime, timezone

import httpx

import config
import database as db


_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_CONNECTOR_SECRET_CACHE: dict[str, Any] | None = None


class ConnectorError(Exception):
    pass


def _slug(value: str, fallback: str = "tool", max_len: int = 40) -> str:
    raw = _TOOL_NAME_RE.sub("_", (value or "").strip()).strip("_").lower()
    if not raw:
        raw = fallback
    if raw and raw[0].isdigit():
        raw = f"_{raw}"
    return raw[:max_len].strip("_") or fallback


def _tool_name(prefix: str, connector_id: str, external_name: str) -> str:
    base = f"{prefix}_{_slug(connector_id, 'conn', 18)}_{_slug(external_name, 'tool', 34)}"
    digest = hashlib.sha1(f"{prefix}:{connector_id}:{external_name}".encode("utf-8")).hexdigest()[:8]
    if len(base) > 55:
        base = base[:55].rstrip("_")
    return f"{base}_{digest}"


def _connector_tool_id(prefix: str, connector_id: str, external_name: str) -> str:
    return f"{prefix}:{connector_id}:{_slug(external_name, 'tool', 80)}"


def _schema_object(schema: Any) -> dict:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "required": []}
    if schema.get("type") == "object":
        schema.setdefault("properties", {})
        schema.setdefault("required", [])
        return schema
    if "properties" in schema:
        schema.setdefault("type", "object")
        schema.setdefault("required", [])
        return schema
    return {"type": "object", "properties": {}, "required": []}


def _load_secret_file() -> dict:
    global _CONNECTOR_SECRET_CACHE
    if _CONNECTOR_SECRET_CACHE is not None:
        return _CONNECTOR_SECRET_CACHE
    path = getattr(config, "CONNECTOR_SECRETS_PATH", "")
    if not path:
        _CONNECTOR_SECRET_CACHE = {}
        return _CONNECTOR_SECRET_CACHE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _CONNECTOR_SECRET_CACHE = data if isinstance(data, dict) else {}
    except FileNotFoundError:
        _CONNECTOR_SECRET_CACHE = {}
    except Exception as e:
        print(f"[connectors] connector secret file ignored: {e}")
        _CONNECTOR_SECRET_CACHE = {}
    return _CONNECTOR_SECRET_CACHE


def reload_connector_secrets() -> None:
    global _CONNECTOR_SECRET_CACHE
    _CONNECTOR_SECRET_CACHE = None


def resolve_placeholder(name: str, required: bool = True) -> str:
    key = (name or "").strip()
    if not key:
        if required:
            raise ConnectorError("Missing credential placeholder name")
        return ""
    if key in os.environ:
        return os.environ[key]
    secrets = _load_secret_file()
    if key in secrets:
        return str(secrets[key])
    if required:
        raise ConnectorError(
            f"Missing credential placeholder {key}. Set ${key} or add it to {config.CONNECTOR_SECRETS_PATH}."
        )
    return ""


def expand_placeholders(value: Any, required: bool = True, plain_names: bool = False) -> Any:
    if isinstance(value, str):
        if plain_names and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value.strip()):
            return resolve_placeholder(value.strip(), required=required)
        m = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", value.strip())
        if m:
            return resolve_placeholder(m.group(1), required=required)
        def repl(match):
            return resolve_placeholder(match.group(1), required=required)
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, value)
    if isinstance(value, list):
        return [expand_placeholders(v, required=required, plain_names=plain_names) for v in value]
    if isinstance(value, dict):
        return {k: expand_placeholders(v, required=required, plain_names=plain_names) for k, v in value.items()}
    return value


def _url_private(host: str) -> bool:
    if not host:
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except Exception:
        # Fail closed like quick_search._url_safe and research._resolve_host_public:
        # an unresolvable host is blocked, not allowed through.
        return True
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                return True
        except ValueError:
            continue
    return False


def assert_url_allowed(url: str, allow_private: bool = False) -> None:
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConnectorError("Connector URL must be http(s)")
    if not allow_private and _url_private(parsed.hostname or ""):
        raise ConnectorError("Private, loopback, or link-local URL blocked. Enable allow_private for trusted local connectors.")


def _parse_spec_text(text: str) -> dict:
    try:
        data = json.loads(text)
    except Exception:
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(text)
        except Exception as e:
            raise ConnectorError(f"OpenAPI spec must be JSON, or install PyYAML for YAML specs: {e}")
    if not isinstance(data, dict):
        raise ConnectorError("OpenAPI spec is not an object")
    if not data.get("openapi") and not data.get("swagger"):
        raise ConnectorError("Spec does not look like OpenAPI")
    return data


async def _fetch_spec(http: httpx.AsyncClient, url: str, allow_private: bool) -> dict:
    assert_url_allowed(url, allow_private=allow_private)
    r = await http.get(url, timeout=20)
    r.raise_for_status()
    return _parse_spec_text(r.text)


def mcp_tool_to_connector(server: dict, tool: dict) -> dict:
    external = str(tool.get("name") or "").strip()
    if not external:
        raise ConnectorError("MCP tool missing name")
    schema = _schema_object(tool.get("inputSchema") or tool.get("input_schema") or {})
    return {
        "id": _connector_tool_id("mcp", server["id"], external),
        "connector_type": "mcp",
        "connector_id": server["id"],
        "external_name": external,
        "tool_name": _tool_name("mcp", server["id"], external),
        "display_name": f"{server.get('name') or 'MCP'}: {external}",
        "description": tool.get("description") or f"MCP tool {external}",
        "input_schema": schema,
        "metadata": {
            "source": "mcp",
            "server_id": server["id"],
            "transport": server.get("transport", "stdio"),
        },
        "enabled": True,
    }


def _jsonrpc_message(message: dict) -> bytes:
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


async def _read_mcp_message(stream: asyncio.StreamReader, timeout: float = 20) -> dict:
    first = await asyncio.wait_for(stream.readline(), timeout=timeout)
    if not first:
        raise ConnectorError("MCP server closed stdout")
    if first.strip().startswith(b"{"):
        return json.loads(first.decode("utf-8"))
    headers = {}
    line = first
    while line and line.strip():
        text = line.decode("utf-8", errors="replace").strip()
        if ":" in text:
            k, v = text.split(":", 1)
            headers[k.lower()] = v.strip()
        line = await asyncio.wait_for(stream.readline(), timeout=timeout)
    length = int(headers.get("content-length") or "0")
    if length <= 0:
        raise ConnectorError("MCP message missing Content-Length")
    raw = await asyncio.wait_for(stream.readexactly(length), timeout=timeout)
    return json.loads(raw.decode("utf-8"))


async def _mcp_stdio_exchange(server: dict, method: str, params: dict | None = None) -> dict:
    command = (server.get("command") or "").strip()
    if not command:
        raise ConnectorError("MCP stdio server requires command")
    env = os.environ.copy()
    env.update(expand_placeholders(server.get("env") or {}, required=True, plain_names=True))
    args = [str(a) for a in (server.get("args") or [])]
    proc = await asyncio.create_subprocess_exec(
        command,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    next_id = 1

    # Drain stderr continuously — a server that logs more than the OS pipe
    # buffer would otherwise block and deadlock the stdout read loop.
    stderr_tail = bytearray()

    async def _drain_stderr():
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            stderr_tail.extend(chunk)
            del stderr_tail[:-2048]

    stderr_task = asyncio.create_task(_drain_stderr())

    async def request(req_method: str, req_params: dict | None = None, read_timeout: float = 20) -> dict:
        nonlocal next_id
        req_id = next_id
        next_id += 1
        payload = {"jsonrpc": "2.0", "id": req_id, "method": req_method, "params": req_params or {}}
        assert proc.stdin is not None
        proc.stdin.write(_jsonrpc_message(payload))
        await proc.stdin.drain()
        while True:
            assert proc.stdout is not None
            msg = await _read_mcp_message(proc.stdout, timeout=read_timeout)
            if msg.get("id") == req_id:
                if msg.get("error"):
                    raise ConnectorError(str(msg["error"]))
                return msg.get("result") or {}

    try:
        await request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "HyprChat", "version": "connector-mvp"},
        })
        assert proc.stdin is not None
        proc.stdin.write(_jsonrpc_message({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}))
        await proc.stdin.drain()
        return await request(method, params or {}, read_timeout=120)
    except Exception as e:
        tail = stderr_tail.decode("utf-8", errors="replace").strip()
        if tail:
            raise ConnectorError(f"{e or type(e).__name__} | server stderr: {tail[-400:]}") from e
        raise
    finally:
        stderr_task.cancel()
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


async def _mcp_http_exchange(http: httpx.AsyncClient, server: dict, method: str, params: dict | None = None) -> dict:
    url = (server.get("url") or "").strip()
    if not url:
        raise ConnectorError("MCP HTTP server requires URL")
    assert_url_allowed(url, allow_private=bool(server.get("allow_private")))
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    headers.update(expand_placeholders(server.get("headers") or {}, required=True))
    session_id = ""

    async def post(req_id: int, req_method: str, req_params: dict | None = None, timeout: float = 30) -> tuple[dict, httpx.Headers]:
        request_headers = dict(headers)
        if session_id:
            request_headers["Mcp-Session-Id"] = session_id
        r = await http.post(
            url,
            json={"jsonrpc": "2.0", "id": req_id, "method": req_method, "params": req_params or {}},
            headers=request_headers,
            timeout=timeout,
        )
        r.raise_for_status()
        text = r.text.strip()
        if text.startswith(("event:", "data:", "id:", ":")) or "\ndata:" in text:
            # SSE data may span multiple consecutive `data:` lines; join the
            # first event's data lines instead of taking only the first one.
            data_lines: list[str] = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line.strip() and data_lines:
                    break
            if data_lines:
                text = "\n".join(data_lines)
        msg = json.loads(text)
        if msg.get("error"):
            raise ConnectorError(str(msg["error"]))
        return msg.get("result") or {}, r.headers

    _, init_headers = await post(1, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "HyprChat", "version": "connector-mvp"},
    })
    session_id = init_headers.get("mcp-session-id", "")
    try:
        request_headers = dict(headers)
        if session_id:
            request_headers["Mcp-Session-Id"] = session_id
        await http.post(
            url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers=request_headers,
            timeout=10,
        )
    except Exception:
        pass
    result, _ = await post(2, method, params or {}, timeout=120)
    return result


async def mcp_exchange(http: httpx.AsyncClient, server: dict, method: str, params: dict | None = None) -> dict:
    transport = (server.get("transport") or "stdio").strip().lower()
    if transport == "stdio":
        return await _mcp_stdio_exchange(server, method, params)
    if transport in {"http", "streamable_http", "sse"}:
        return await _mcp_http_exchange(http, server, method, params)
    raise ConnectorError(f"Unsupported MCP transport: {transport}")


async def discover_mcp_server(http: httpx.AsyncClient, server_id: str) -> dict:
    server = await db.get_mcp_server(server_id)
    if not server:
        raise ConnectorError("MCP server not found")
    try:
        result = await mcp_exchange(http, server, "tools/list", {})
        raw_tools = result.get("tools") or []
        tools = [mcp_tool_to_connector(server, t) for t in raw_tools if isinstance(t, dict)]
        await db.replace_connector_tools("mcp", server_id, tools)
        await db.update_mcp_server(
            server_id,
            health="ok",
            last_error="",
            discovered_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"status": "ok", "tool_count": len(tools), "tools": tools}
    except Exception as e:
        await db.update_mcp_server(server_id, health="error", last_error=str(e))
        raise


def _resolve_ref(spec: dict, obj: Any) -> Any:
    if not isinstance(obj, dict) or "$ref" not in obj:
        return obj
    ref = obj.get("$ref", "")
    if not ref.startswith("#/"):
        return obj
    cur: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(cur, dict) or part not in cur:
            return obj
        cur = cur[part]
    return cur


def _request_body_schema(spec: dict, request_body: dict) -> tuple[dict, bool]:
    request_body = _resolve_ref(spec, request_body) or {}
    content = request_body.get("content") or {}
    media = content.get("application/json") or content.get("application/*+json") or {}
    schema = _resolve_ref(spec, media.get("schema") or {})
    if not isinstance(schema, dict):
        schema = {"type": "object"}
    return schema, bool(request_body.get("required"))


def openapi_operation_to_connector(connector: dict, spec: dict, path: str, method: str, op: dict, path_params: list[dict]) -> dict:
    operation_id = str(op.get("operationId") or f"{method}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}").strip()
    params = []
    for p in path_params + (op.get("parameters") or []):
        p = _resolve_ref(spec, p)
        if isinstance(p, dict):
            params.append(p)
    properties: dict[str, Any] = {}
    required: list[str] = []
    arg_map = []
    seen_names: set[str] = set()
    for p in params:
        loc = p.get("in") or "query"
        name = p.get("name") or ""
        if not name:
            continue
        arg_name = name if name not in seen_names else f"{loc}_{name}"
        seen_names.add(arg_name)
        schema = _resolve_ref(spec, p.get("schema") or {"type": "string"})
        if not isinstance(schema, dict):
            schema = {"type": "string"}
        desc = p.get("description") or f"{loc} parameter {name}"
        prop = dict(schema)
        prop.setdefault("description", desc)
        properties[arg_name] = prop
        if p.get("required") or loc == "path":
            required.append(arg_name)
        arg_map.append({"arg": arg_name, "name": name, "in": loc})
    if op.get("requestBody"):
        body_schema, body_required = _request_body_schema(spec, op.get("requestBody") or {})
        properties["body"] = body_schema if isinstance(body_schema, dict) else {"type": "object"}
        properties["body"].setdefault("description", "JSON request body")
        if body_required:
            required.append("body")
    if "query" not in properties:
        properties["query"] = {
            "type": "object",
            "description": "Additional query string parameters not explicitly modeled by the OpenAPI spec.",
            "additionalProperties": {},
        }
    if "headers" not in properties:
        properties["headers"] = {
            "type": "object",
            "description": "Additional request headers not explicitly modeled by the OpenAPI spec.",
            "additionalProperties": {"type": "string"},
        }
    if method.upper() not in {"GET", "HEAD"} and "body" not in properties:
        properties["body"] = {
            "type": "object",
            "description": "Optional JSON request body for APIs whose spec omits a requestBody schema.",
        }
    input_schema = {"type": "object", "properties": properties, "required": required}
    summary = op.get("summary") or op.get("description") or f"{method.upper()} {path}"
    return {
        "id": _connector_tool_id("openapi", connector["id"], operation_id),
        "connector_type": "openapi",
        "connector_id": connector["id"],
        "external_name": operation_id,
        "tool_name": _tool_name("openapi", connector["id"], operation_id),
        "display_name": f"{connector.get('name') or 'OpenAPI'}: {operation_id}",
        "description": str(summary)[:900],
        "input_schema": input_schema,
        "metadata": {
            "source": "openapi",
            "connector_id": connector["id"],
            "method": method.upper(),
            "path": path,
            "arg_map": arg_map,
        },
        "enabled": True,
    }


async def load_openapi_spec(http: httpx.AsyncClient, connector: dict) -> dict:
    if (connector.get("spec_json") or "").strip():
        return _parse_spec_text(connector.get("spec_json") or "")
    if (connector.get("spec_url") or "").strip():
        return await _fetch_spec(http, connector.get("spec_url") or "", bool(connector.get("allow_private")))
    raise ConnectorError("OpenAPI connector requires spec_url or spec_json")


async def discover_openapi_connector(http: httpx.AsyncClient, connector_id: str) -> dict:
    connector = await db.get_openapi_connector(connector_id)
    if not connector:
        raise ConnectorError("OpenAPI connector not found")
    try:
        spec = await load_openapi_spec(http, connector)
        paths = spec.get("paths") or {}
        tools = []
        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            path_params = path_item.get("parameters") or []
            for method, op in path_item.items():
                if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                    continue
                tools.append(openapi_operation_to_connector(connector, spec, path, method.lower(), op, path_params))
        if not connector.get("base_url"):
            servers = spec.get("servers") or []
            if servers and isinstance(servers[0], dict) and servers[0].get("url"):
                await db.update_openapi_connector(connector_id, base_url=str(servers[0]["url"]))
        await db.replace_connector_tools("openapi", connector_id, tools)
        await db.update_openapi_connector(
            connector_id,
            health="ok",
            last_error="",
            discovered_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"status": "ok", "tool_count": len(tools), "tools": tools}
    except Exception as e:
        await db.update_openapi_connector(connector_id, health="error", last_error=str(e))
        raise


def tool_def_from_connector_tool(tool: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool["tool_name"],
            "description": tool.get("description") or tool.get("display_name") or tool["external_name"],
            "parameters": _schema_object(tool.get("input_schema") or {}),
        },
    }


async def execute_mcp_tool(http: httpx.AsyncClient, tool: dict, args: dict) -> str:
    server = await db.get_mcp_server(tool.get("connector_id", ""))
    if not server or not server.get("enabled"):
        raise ConnectorError("MCP server is disabled or missing")
    result = await mcp_exchange(http, server, "tools/call", {
        "name": tool["external_name"],
        "arguments": args or {},
    })
    content = result.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        text = "\n".join(p for p in parts if p)
        return text or json.dumps(result, ensure_ascii=False)
    return json.dumps(result, indent=2, ensure_ascii=False)


def _merge_url(base_url: str, path: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        raise ConnectorError("OpenAPI connector has no base_url")
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return base + "/" + path.lstrip("/")


def _apply_auth(headers: dict, query: dict, auth: dict) -> None:
    auth = auth or {}
    auth_type = (auth.get("type") or "").lower()
    if not auth_type:
        return
    if auth_type == "bearer":
        token = resolve_placeholder(auth.get("token_placeholder") or auth.get("placeholder") or "TOKEN")
        headers["Authorization"] = f"Bearer {token}"
    elif auth_type == "api_key":
        value = resolve_placeholder(auth.get("value_placeholder") or auth.get("placeholder") or "API_KEY")
        name = auth.get("name") or "X-API-Key"
        if (auth.get("in") or "header").lower() == "query":
            query[name] = value
        else:
            headers[name] = value
    elif auth_type == "basic":
        import base64
        user = resolve_placeholder(auth.get("username_placeholder") or "USERNAME")
        password = resolve_placeholder(auth.get("password_placeholder") or "PASSWORD")
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    else:
        raise ConnectorError(f"Unsupported auth type: {auth_type}")


async def execute_openapi_tool(http: httpx.AsyncClient, tool: dict, args: dict) -> str:
    connector = await db.get_openapi_connector(tool.get("connector_id", ""))
    if not connector or not connector.get("enabled"):
        raise ConnectorError("OpenAPI connector is disabled or missing")
    metadata = tool.get("metadata") or {}
    url = _merge_url(connector.get("base_url") or "", metadata.get("path") or "")
    method = (metadata.get("method") or "GET").upper()
    query: dict[str, Any] = {}
    headers: dict[str, str] = {}
    path_values: dict[str, Any] = {}
    body = (args or {}).get("body", None)

    extra_query = (args or {}).get("query")
    if isinstance(extra_query, dict):
        for key, value in extra_query.items():
            if key and value is not None:
                query[str(key)] = value
    extra_headers = (args or {}).get("headers")
    if isinstance(extra_headers, dict):
        for key, value in extra_headers.items():
            if key and value is not None:
                headers[str(key)] = str(value)

    for item in metadata.get("arg_map") or []:
        arg_name = item.get("arg")
        if arg_name not in (args or {}):
            continue
        loc = item.get("in")
        raw_name = item.get("name")
        value = args[arg_name]
        if loc == "path":
            path_values[raw_name] = value
        elif loc == "header":
            headers[raw_name] = str(value)
        elif loc == "cookie":
            headers["Cookie"] = f"{raw_name}={value}"
        else:
            query[raw_name] = value

    for name, value in path_values.items():
        url = url.replace("{" + name + "}", urllib.parse.quote(str(value), safe=""))
    headers.update(expand_placeholders(connector.get("headers") or {}, required=True))
    _apply_auth(headers, query, connector.get("auth") or {})
    assert_url_allowed(url, allow_private=bool(connector.get("allow_private")))

    r = await http.request(method, url, params=query, headers=headers, json=body, timeout=45)
    content_type = r.headers.get("content-type", "")
    summary = {
        "status_code": r.status_code,
        "reason": r.reason_phrase,
        "content_type": content_type,
    }
    try:
        parsed = r.json()
        body_text = json.dumps(parsed, indent=2, ensure_ascii=False)
    except Exception:
        body_text = r.text
    if len(body_text) > 18000:
        body_text = body_text[:12000] + f"\n\n[... {len(body_text) - 18000} chars omitted ...]\n\n" + body_text[-6000:]
    return json.dumps(summary, ensure_ascii=False) + "\n\n" + body_text


async def execute_connector_tool(http: httpx.AsyncClient, tool: dict, args: dict) -> str:
    if tool.get("connector_type") == "mcp":
        return await execute_mcp_tool(http, tool, args)
    if tool.get("connector_type") == "openapi":
        return await execute_openapi_tool(http, tool, args)
    raise ConnectorError(f"Unknown connector type: {tool.get('connector_type')}")
