"""Custom tool and connector management routes."""
import json
import os
import uuid
from typing import Any, Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

import config
import connectors
import database as db

from .context import route_context


router = APIRouter()


class ToolCreate(BaseModel):
    name: str
    description: str = ""
    filename: str
    code: str


class ToolUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    code: Optional[str] = None


class MCPServerCreate(BaseModel):
    name: str
    transport: str = "stdio"
    command: str = ""
    url: str = ""
    args: list[str] = []
    env: dict = {}
    headers: dict = {}
    allow_private: bool = False
    enabled: bool = True


class MCPServerUpdate(BaseModel):
    name: Optional[str] = None
    transport: Optional[str] = None
    command: Optional[str] = None
    url: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict] = None
    headers: Optional[dict] = None
    allow_private: Optional[bool] = None
    enabled: Optional[bool] = None


class OpenAPIConnectorCreate(BaseModel):
    name: str
    spec_url: str = ""
    spec_json: Any = ""
    base_url: str = ""
    auth: dict = {}
    headers: dict = {}
    allow_private: bool = False
    enabled: bool = True


class OpenAPIConnectorUpdate(BaseModel):
    name: Optional[str] = None
    spec_url: Optional[str] = None
    spec_json: Optional[Any] = None
    base_url: Optional[str] = None
    auth: Optional[dict] = None
    headers: Optional[dict] = None
    allow_private: Optional[bool] = None
    enabled: Optional[bool] = None


def _spec_json_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _connector_tool_public(tl: dict) -> dict:
    out = dict(tl)
    out["name"] = tl.get("display_name") or tl.get("external_name") or tl.get("tool_name")
    out["description"] = tl.get("description", "")
    out["icon"] = "plug"
    out["connector"] = True
    return out


@router.get("/api/tools")
async def list_tools():
    return await db.get_tools()


@router.post("/api/tools")
async def create_tool(req: ToolCreate):
    tool_id = f"tool-{uuid.uuid4().hex[:12]}"
    safe_name = os.path.basename(req.filename or "tool.py")
    if not safe_name or safe_name != (req.filename or ""):
        raise HTTPException(400, "Invalid filename")
    filepath = os.path.abspath(os.path.join(config.TOOLS_DIR, safe_name))
    tools_root = os.path.abspath(config.TOOLS_DIR)
    if filepath != tools_root and not filepath.startswith(tools_root + os.sep):
        raise HTTPException(400, "Invalid filename")

    await db.create_tool(tool_id, req.name, req.description, safe_name, req.code)
    os.makedirs(config.TOOLS_DIR, exist_ok=True)
    with open(filepath, "w") as f:
        f.write(req.code)

    return {"id": tool_id, **req.model_dump(exclude={"filename"}), "filename": safe_name}


@router.post("/api/tools/upload")
async def upload_tool(file: UploadFile = File(...)):
    """Upload a .py file as a tool."""
    safe_name = os.path.basename(file.filename or "tool.py")
    if not safe_name.endswith(".py"):
        raise HTTPException(400, "Only .py files accepted")
    filepath = os.path.join(config.TOOLS_DIR, safe_name)
    if not os.path.abspath(filepath).startswith(os.path.abspath(config.TOOLS_DIR)):
        raise HTTPException(400, "Invalid filename")

    content = await file.read()
    code = content.decode("utf-8")
    name = safe_name.replace(".py", "")
    tool_id = f"tool-{uuid.uuid4().hex[:12]}"

    with open(filepath, "w") as f:
        f.write(code)

    await db.create_tool(tool_id, name, f"Uploaded: {safe_name}", safe_name, code)
    return {"id": tool_id, "name": name, "filename": safe_name, "code": code}


@router.patch("/api/tools/{tool_id}")
async def update_tool(tool_id: str, req: ToolUpdate):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    await db.update_tool(tool_id, **kwargs)
    return {"status": "updated"}


@router.delete("/api/tools/{tool_id}")
async def delete_tool(tool_id: str):
    await db.delete_tool(tool_id)
    return {"status": "deleted"}


@router.put("/api/tools/{tool_id}")
async def update_tool_put(tool_id: str, req: ToolUpdate):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    if kwargs:
        await db.update_tool(tool_id, **kwargs)
    return {"status": "updated"}


@router.get("/api/connector-tools")
async def list_connector_tools(enabled_only: bool = Query(True)):
    tools = await db.get_connector_tools(enabled_only=enabled_only)
    return [_connector_tool_public(t) for t in tools]


@router.get("/api/mcp-servers")
async def list_mcp_servers():
    return await db.get_mcp_servers()


@router.post("/api/mcp-servers")
async def create_mcp_server(req: MCPServerCreate):
    server_id = f"mcp-{uuid.uuid4().hex[:12]}"
    transport = (req.transport or "stdio").strip().lower()
    if transport not in {"stdio", "http", "streamable_http", "sse"}:
        raise HTTPException(400, "transport must be stdio, http, streamable_http, or sse")
    await db.create_mcp_server(
        server_id,
        req.name,
        transport=transport,
        command=req.command,
        url=req.url,
        args=req.args,
        env=req.env,
        headers=req.headers,
        allow_private=req.allow_private,
        enabled=req.enabled,
    )
    return await db.get_mcp_server(server_id)


@router.patch("/api/mcp-servers/{server_id}")
async def update_mcp_server(server_id: str, req: MCPServerUpdate):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    if "transport" in kwargs:
        kwargs["transport"] = (kwargs["transport"] or "stdio").strip().lower()
        if kwargs["transport"] not in {"stdio", "http", "streamable_http", "sse"}:
            raise HTTPException(400, "transport must be stdio, http, streamable_http, or sse")
    await db.update_mcp_server(server_id, **kwargs)
    return await db.get_mcp_server(server_id)


@router.delete("/api/mcp-servers/{server_id}")
async def delete_mcp_server(server_id: str):
    await db.delete_mcp_server(server_id)
    return {"status": "deleted"}


@router.post("/api/mcp-servers/{server_id}/discover")
async def discover_mcp_server(server_id: str):
    try:
        return await connectors.discover_mcp_server(route_context().http, server_id)
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/api/mcp-servers/{server_id}/health")
async def health_mcp_server(server_id: str):
    try:
        result = await connectors.discover_mcp_server(route_context().http, server_id)
        return {"status": "ok", "tool_count": result.get("tool_count", 0)}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/api/mcp-servers/{server_id}/tools")
async def list_mcp_server_tools(server_id: str):
    return [_connector_tool_public(t) for t in await db.get_connector_tools("mcp", server_id, enabled_only=False)]


@router.get("/api/openapi-connectors")
async def list_openapi_connectors():
    return await db.get_openapi_connectors()


@router.post("/api/openapi-connectors")
async def create_openapi_connector(req: OpenAPIConnectorCreate):
    connector_id = f"openapi-{uuid.uuid4().hex[:12]}"
    await db.create_openapi_connector(
        connector_id,
        req.name,
        spec_url=req.spec_url,
        spec_json=_spec_json_to_text(req.spec_json),
        base_url=req.base_url,
        auth=req.auth,
        headers=req.headers,
        allow_private=req.allow_private,
        enabled=req.enabled,
    )
    return await db.get_openapi_connector(connector_id)


@router.patch("/api/openapi-connectors/{connector_id}")
async def update_openapi_connector(connector_id: str, req: OpenAPIConnectorUpdate):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    if "spec_json" in kwargs:
        kwargs["spec_json"] = _spec_json_to_text(kwargs["spec_json"])
    await db.update_openapi_connector(connector_id, **kwargs)
    return await db.get_openapi_connector(connector_id)


@router.delete("/api/openapi-connectors/{connector_id}")
async def delete_openapi_connector(connector_id: str):
    await db.delete_openapi_connector(connector_id)
    return {"status": "deleted"}


@router.post("/api/openapi-connectors/{connector_id}/discover")
async def discover_openapi_connector(connector_id: str):
    try:
        return await connectors.discover_openapi_connector(route_context().http, connector_id)
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/api/openapi-connectors/{connector_id}/health")
async def health_openapi_connector(connector_id: str):
    try:
        result = await connectors.discover_openapi_connector(route_context().http, connector_id)
        return {"status": "ok", "tool_count": result.get("tool_count", 0)}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/api/openapi-connectors/{connector_id}/tools")
async def list_openapi_connector_tools(connector_id: str):
    return [_connector_tool_public(t) for t in await db.get_connector_tools("openapi", connector_id, enabled_only=False)]
