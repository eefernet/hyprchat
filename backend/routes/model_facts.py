"""What Ollama reports about the installed models (``model_facts.py``), for the Models page."""
from __future__ import annotations

from fastapi import APIRouter

import model_facts

from .context import route_context

router = APIRouter()


@router.get("/api/models/facts")
async def installed_model_facts():
    return {"models": await model_facts.installed(route_context().http)}
