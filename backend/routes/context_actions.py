"""What the owner can do to a chat's context from the context breakdown: summarize the earlier messages
now, and forget the tool results the chat keeps (``chat_compact_now.py``)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import chat_compact_now

from .context import route_context

router = APIRouter()


class CompactRequest(BaseModel):
    model: str = ""
    num_ctx: Optional[int] = None


@router.post("/api/conversations/{conv_id}/compact")
async def compact_conversation(conv_id: str, body: CompactRequest | None = None):
    body = body or CompactRequest()
    try:
        return await chat_compact_now.compact_now(route_context().http, conv_id, model=body.model, asked=int(body.num_ctx or 0))
    except LookupError:
        raise HTTPException(404, "Conversation not found")


@router.delete("/api/conversations/{conv_id}/tool-results")
async def forget_tool_results(conv_id: str):
    try:
        return await chat_compact_now.forget_tool_results(conv_id)
    except LookupError:
        raise HTTPException(404, "Conversation not found")
