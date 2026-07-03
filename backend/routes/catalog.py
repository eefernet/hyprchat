"""Built-in tool catalog and default persona seed routes."""
from fastapi import APIRouter

from agents.personas import (
    seed_all_defaults,
    seed_based_bot,
    seed_coder_bot,
    seed_coder_bot_v2,
    seed_conspiracy_bot,
)


router = APIRouter()


@router.get("/api/builtin-tools")
async def list_builtin_tools():
    """Return the integrated tool suites."""
    return [
        {"id": "codeagent", "name": "⚡ CodeAgent", "description": "Code execution, shell, file management, downloads", "icon": "cpu", "builtin": True},
        {"id": "deep_research", "name": "🔬 Agent Research", "description": "Agent-focused web research for current APIs, coding blockers, repeated errors, and concise implementation guidance", "icon": "search", "builtin": True},
        {"id": "conspiracy_research", "name": "🕵️ Conspiracy Research", "description": "Uncensored deep-dive into theories, cover-ups, and hidden agendas", "icon": "search", "builtin": True},
    ]


@router.post("/api/seed/all-defaults")
async def seed_all_defaults_ep():
    return await seed_all_defaults()


@router.post("/api/seed/coder-bot")
async def seed_coder_bot_ep():
    return await seed_coder_bot()


@router.post("/api/seed/coder-bot-v2")
async def seed_coder_bot_v2_ep():
    return await seed_coder_bot_v2()


@router.post("/api/seed/conspiracy-bot")
async def seed_conspiracy_bot_ep():
    return await seed_conspiracy_bot()


@router.post("/api/seed/based-bot")
async def seed_based_bot_ep():
    return await seed_based_bot()
