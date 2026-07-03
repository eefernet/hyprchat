"""Model config and persona avatar routes."""
import os
import uuid
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config
import database as db

router = APIRouter()


class ModelConfigCreate(BaseModel):
    name: str
    base_model: str
    system_prompt: str = ""
    tool_ids: list[str] = []
    kb_ids: list[str] = []
    parameters: dict = {}


class ModelConfigUpdate(BaseModel):
    name: Optional[str] = None
    base_model: Optional[str] = None
    system_prompt: Optional[str] = None
    tool_ids: Optional[list[str]] = None
    kb_ids: Optional[list[str]] = None
    parameters: Optional[dict] = None


@router.get("/api/model-configs")
async def list_model_configs():
    return await db.get_model_configs()


@router.post("/api/model-configs")
async def create_model_config(req: ModelConfigCreate):
    config_id = f"mc-{uuid.uuid4().hex[:12]}"
    await db.create_model_config(
        config_id,
        req.name,
        req.base_model,
        req.system_prompt,
        req.tool_ids,
        req.kb_ids,
        req.parameters,
    )
    return {"id": config_id, **req.model_dump()}


@router.patch("/api/model-configs/{mc_id}")
async def update_model_config(mc_id: str, req: ModelConfigUpdate):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    await db.update_model_config(mc_id, **kwargs)
    return {"status": "updated"}


@router.put("/api/model-configs/{mc_id}")
async def update_model_config_put(mc_id: str, req: ModelConfigUpdate):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    await db.update_model_config(mc_id, **kwargs)
    return {"status": "updated"}


@router.delete("/api/model-configs/{mc_id}")
async def delete_model_config(mc_id: str):
    await db.delete_model_config(mc_id)
    return {"status": "deleted"}


@router.post("/api/model-configs/{mc_id}/avatar")
async def upload_persona_avatar(mc_id: str, file: UploadFile = File(...)):
    """Upload an avatar image for a persona/model config."""
    existing = await db.get_model_config(mc_id)
    if not existing:
        raise HTTPException(404, "Model config not found")
    avatar_dir = os.path.join(config.UPLOAD_DIR, "avatars")
    os.makedirs(avatar_dir, exist_ok=True)

    fname = file.filename or ""
    raw_ext = fname.rsplit(".", 1)[-1].lower()[:10] if "." in fname else "png"
    if raw_ext not in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
        raise HTTPException(400, "Invalid image type — allowed: png, jpg, jpeg, gif, webp, svg")
    ext = raw_ext
    avatar_path = os.path.join(avatar_dir, f"{mc_id}.{ext}")

    content = await file.read()
    with open(avatar_path, "wb") as f:
        f.write(content)

    params = existing.get("parameters", {}) if existing else {}
    await db.update_model_config(mc_id, parameters={**params, "avatar": f"/api/avatars/{mc_id}.{ext}"})
    return {"avatar_url": f"/api/avatars/{mc_id}.{ext}"}


@router.get("/api/avatars/{filename}")
async def get_avatar(filename: str):
    """Serve avatar images."""
    avatar_dir = os.path.join(config.UPLOAD_DIR, "avatars")
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name != filename:
        raise HTTPException(400, "Invalid filename")
    filepath = os.path.join(avatar_dir, safe_name)
    if not os.path.abspath(filepath).startswith(os.path.abspath(avatar_dir)):
        raise HTTPException(400, "Invalid filename")
    if not os.path.exists(filepath):
        raise HTTPException(404, "Avatar not found")
    return FileResponse(filepath)
