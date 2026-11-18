"""What Ollama has loaded now, for Settings → Model & Generation (``loaded_models.py``)."""
from __future__ import annotations

from fastapi import APIRouter

import loaded_models

from .context import route_context

router = APIRouter()


@router.get("/api/models/loaded")
async def loaded_models_status():
    return await loaded_models.status(route_context().http)
