"""Offline test: tools a turn started are cancelled when the turn ends early (Stop, disconnect)."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from .optional_deps import HAS_AIOSQLITE, HAS_CHROMADB, install_aiosqlite_stub, install_rag_stub

_BACKEND = Path(__file__).resolve().parent.parent
for _p in (_BACKEND, _BACKEND / "agents"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
if not HAS_AIOSQLITE:
    install_aiosqlite_stub()
if not HAS_CHROMADB:
    install_rag_stub()

from agents import chat as chat_mod  # noqa: E402


def test_stopping_a_reply_cancels_the_tools_it_started(monkeypatch):
    # Found 2026-11-13: Stop closed the model stream, but code kept executing and images kept rendering,
    # and their results were attached to the message afterwards.
    state = {}

    async def turn(*args, **kwargs):
        async def tool():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["tool"] = "cancelled"
                raise
            state["tool"] = "finished"

        state["task"] = chat_mod._spawn_tool(tool())
        yield 'data: {"type": "keepalive"}\n\n'
        await asyncio.sleep(30)

    monkeypatch.setattr(chat_mod, "_chat_stream_generate", turn)
    monkeypatch.setattr(chat_mod.skill_library, "current_turn", lambda conv_id: None)
    request = SimpleNamespace(conversation_id="c", model="m", ephemeral=True, persona_id="")

    async def scenario():
        async def consume():
            async for _ in chat_mod.chat_stream_generate(request, None, None, {}, {}):
                pass
        reply = asyncio.create_task(consume())
        await asyncio.sleep(0.1)
        reply.cancel()
        try:
            await reply
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert state["tool"] == "cancelled" and state["task"].cancelled()


def test_a_finished_turn_leaves_finished_tools_alone(monkeypatch):
    state = {}

    async def turn(*args, **kwargs):
        async def tool():
            state["tool"] = "finished"

        state["task"] = chat_mod._spawn_tool(tool())
        await asyncio.sleep(0.01)
        yield 'data: {"type": "done"}\n\n'

    monkeypatch.setattr(chat_mod, "_chat_stream_generate", turn)
    monkeypatch.setattr(chat_mod.skill_library, "current_turn", lambda conv_id: None)
    request = SimpleNamespace(conversation_id="c", model="m", ephemeral=True, persona_id="")

    async def scenario():
        return [chunk async for chunk in chat_mod.chat_stream_generate(request, None, None, {}, {})]

    assert asyncio.run(scenario()) and state["tool"] == "finished" and not state["task"].cancelled()
