"""HuggingFace model browser routes."""
from fastapi import APIRouter, Request

import hf as hf_module
import hf_compat

from .context import route_context


router = APIRouter()


@router.get("/api/hf/search")
async def hf_search_ep(q: str = "", limit: int = 20, gguf_only: bool = True, sort: str = "downloads", direction: int = -1):
    return await hf_module.hf_search(route_context().http, q, limit, gguf_only, sort, direction)


@router.get("/api/hf/model")
async def hf_model_info_ep(repo_id: str):
    return await hf_module.hf_model_info(route_context().http, repo_id)


@router.get("/api/hf/readme")
async def hf_readme_ep(repo_id: str):
    return await hf_module.hf_readme(route_context().http, repo_id)


@router.post("/api/hf/download")
async def hf_download_ep(request: Request):
    return await hf_module.hf_download(route_context().http, request)


@router.get("/api/hf/compat")
async def hf_compat_ep(repo_id: str, filename: str):
    """Per-file compatibility facts: exact pull tag, GGUF header, what the HF
    registry will serve (model + projector bytes) and whether the local Ollama
    already runs that architecture. Every probe degrades to null on failure."""
    import os
    safe = os.path.basename(filename)
    if not repo_id or not safe or safe != filename or not safe.lower().endswith(".gguf"):
        from fastapi import HTTPException
        raise HTTPException(400, "repo_id and a .gguf filename are required")
    return await hf_compat.compat_info(route_context().http, repo_id, filename)
