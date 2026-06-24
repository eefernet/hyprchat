"""HuggingFace model browser routes."""
from fastapi import APIRouter, Request

import hf as hf_module

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
