"""Jarvis Personal Assistant — headless chat driver, gatherer registry, and
per-user assistant bootstrap.

`run_headless_chat` drives the normal multi-round tool-calling loop
(`agents.chat.chat_stream_generate`) with no SSE client attached: the
generator persists the user prompt and the final assistant message itself, so
scheduled runs simply drain the chunks. The duck-typed request mirrors
main.ChatRequest (importing main here would be circular).

Gatherers feed check-in briefs: each registered gatherer returns a titled text
block for the data dump. Phase 1 ships tasks/notifications; calendar, notes,
and email register themselves in later phases.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from types import SimpleNamespace

import config
import database as db
import cancel_registry

ASSISTANT_CONFIG_NAME = "Personal Assistant"

DEFAULT_ASSISTANT_PROMPT = """You are the user's personal assistant inside HyprChat — proactive, concise, and useful.

Rules of engagement:
- Use your tools to take action instead of describing what could be done.
- When the user asks for something recurring ("every morning", "each week"), create a scheduled task with manage_tasks rather than doing it once.
- Prioritize ruthlessly: lead with what matters today, group the rest by importance, and flag anything that needs preparation.
- Autonomy limits: you may add calendar events and draft replies on your own, but NEVER send email without explicit permission, and never delete anything unless directly instructed.
- Keep briefs scannable — short lines, no filler, no restating raw data the user can already see."""

CHECKIN_INSTRUCTIONS = """## Instructions
The data dump above is everything gathered for this check-in. YOU decide what matters:
- Lead with the most important/urgent items for the user right now.
- Group the rest by importance; drop noise entirely.
- Flag anything that needs preparation or a decision.
- If something warrants action you can take with your tools, take it (within your autonomy limits) and say what you did.
Write a short, scannable brief."""

# name -> async fn(user_id: str) -> str block ("" to skip)
_GATHERERS: dict = {}


def register_gatherer(name: str, fn) -> None:
    _GATHERERS[name] = fn


def gatherer_names() -> list[str]:
    return sorted(_GATHERERS.keys())


# ------------------------------------------------------------------
# headless chat
# ------------------------------------------------------------------

_HISTORY_LIMIT = 20


async def run_headless_chat(*, conversation_id: str, prompt: str, model: str = "",
                            tool_ids: list | None = None, run_id: str = "",
                            task_id: str = "", http=None, events=None) -> None:
    """Run one agent turn in `conversation_id`; messages persist as normal rows."""
    from agents.chat import chat_stream_generate  # call-time: chat → tools → scheduler cycle

    conv = await db.get_conversation(conversation_id)
    if not conv:
        raise RuntimeError(f"conversation {conversation_id} not found")

    history = []
    for m in (conv.get("messages") or [])[-_HISTORY_LIMIT:]:
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip():
            history.append({"role": m["role"], "content": m["content"]})
    messages = history + [{"role": "user", "content": prompt}]

    resolved_model = model or conv.get("model") or config.DEFAULT_MODEL
    resolved_tools = tool_ids if tool_ids is not None else (conv.get("tool_ids") or [])
    # Scheduled runs never auto-search the web; quick_search is a per-request opt-in.
    resolved_tools = [t for t in resolved_tools if t != "quick_search"]

    req = SimpleNamespace(
        conversation_id=conversation_id,
        model=resolved_model,
        messages=messages,
        system_prompt="",
        stream=True,
        tool_ids=resolved_tools,
        persona_id=conv.get("model_config_id"),
        num_ctx=None,
        temperature=None,
        top_p=None,
        top_k=None,
        repeat_penalty=None,
        think_budget=None,
        effort_rounds=None,
        display_content=None,
        user_metadata={"source": "scheduled_task", "task_id": task_id},
        workspace_id=None,
        use_memories=None,
        ephemeral=False,
        continue_message_id=None,
    )

    _all_custom = await db.get_tools()
    custom_tool_map = {t["name"]: t for t in _all_custom}
    custom_tool_id_map = {t["id"]: t for t in _all_custom}
    _all_connector = await db.get_connector_tools(enabled_only=True)
    connector_tool_id_map = {t["id"]: t for t in _all_connector}
    connector_tool_name_map = {t["tool_name"]: t for t in _all_connector}

    async def _drain():
        async for _chunk in chat_stream_generate(
            req, http, events, custom_tool_map, custom_tool_id_map,
            connector_tool_id_map, connector_tool_name_map,
        ):
            pass

    await cancel_registry.await_cancellable(_drain(), run_id)


# ------------------------------------------------------------------
# check-ins
# ------------------------------------------------------------------

async def build_checkin_prompt(task: dict, base_prompt: str) -> str:
    """Data dump from the enabled gatherers + the task prompt + the fixed
    instruction block. Gatherer failures degrade to a note, never abort."""
    user_id = task.get("user_id") or db.current_user_id()
    profile = await db.get_assistant_profile(user_id=user_id) or {}
    enabled = profile.get("enabled_gatherers") or []
    names = [n for n in gatherer_names() if not enabled or n in enabled]

    blocks: list[str] = []
    for name in names:
        try:
            block = await _GATHERERS[name](user_id)
            if (block or "").strip():
                blocks.append(f"### {name}\n{block.strip()}")
        except Exception as e:
            blocks.append(f"### {name}\n(unavailable: {e})")

    now_local = datetime.utcnow().isoformat(timespec="minutes")
    dump = "\n\n".join(blocks) if blocks else "(nothing gathered)"
    focus = (base_prompt or "").strip()
    focus_block = f"\n\n## Focus for this check-in\n{focus}" if focus else ""
    return (f"## Check-in data dump (gathered {now_local} UTC)\n\n{dump}"
            f"{focus_block}\n\n{CHECKIN_INSTRUCTIONS}")


async def _gather_tasks(user_id: str) -> str:
    tasks = await db.list_scheduled_tasks(user_id=user_id)
    lines = []
    for t in tasks:
        if not t["enabled"]:
            continue
        status = f" (last: {t['last_status']})" if t.get("last_status") else ""
        nxt = f" next {t['next_run']}" if t.get("next_run") else ""
        lines.append(f"- [{t['schedule_kind']}] {t['title']}{nxt}{status}")
    return "\n".join(lines[:15])


async def _gather_notifications(user_id: str) -> str:
    rows = await db.list_notifications(limit=10, unseen_only=True, user_id=user_id)
    return "\n".join(f"- [{n['kind']}] {n['title']}: {n['body'][:120]}" for n in rows)


register_gatherer("tasks", _gather_tasks)
register_gatherer("notifications", _gather_notifications)


# ------------------------------------------------------------------
# per-user bootstrap
# ------------------------------------------------------------------

async def ensure_assistant(user_id: str | None = None) -> dict:
    """Idempotently seed the current user's assistant: model_config persona +
    pinned conversation + profile row. Returns the profile."""
    uid = db._scope_user(user_id)
    profile = await db.get_assistant_profile(user_id=uid)

    model_config_id = (profile or {}).get("model_config_id") or ""
    if model_config_id:
        existing = await db.get_model_config(model_config_id)
        if not existing:
            model_config_id = ""
    if not model_config_id:
        configs = await db.get_model_configs()
        match = next((c for c in configs
                      if (c.get("parameters") or {}).get("profile_type") == "assistant"), None)
        if match:
            model_config_id = match["id"]
        else:
            model_config_id = f"assistant-{uuid.uuid4().hex[:8]}"
            await db.create_model_config(
                model_config_id, ASSISTANT_CONFIG_NAME, config.DEFAULT_MODEL,
                system_prompt=DEFAULT_ASSISTANT_PROMPT,
                tool_ids=["codeagent"],
                parameters={"profile_type": "assistant"},
            )

    conversation_id = (profile or {}).get("conversation_id") or ""
    if conversation_id and not await db.get_conversation(conversation_id):
        conversation_id = ""
    if not conversation_id:
        conversation_id = f"assistant-{uuid.uuid4().hex[:10]}"
        await db.create_conversation(
            conversation_id, title="🤖 Assistant", model=config.DEFAULT_MODEL,
            model_config_id=model_config_id, use_memories="1",
        )
        try:
            await db.update_conversation(conversation_id, pinned="1")
        except Exception:
            pass

    return await db.upsert_assistant_profile(
        model_config_id=model_config_id,
        conversation_id=conversation_id,
        user_id=uid,
    )
