"""How many models HyprChat keeps loaded in Ollama at once (Settings → Model & Generation).

Ollama has its own limit (``OLLAMA_MAX_LOADED_MODELS``, set on the Ollama machine) and unloads models by
itself when a new one does not fit in memory. Checked on the homelab 2026-11-16: the limit there is 3,
Ollama held two or three models at once 417 times in a week, and 478 unloads were logged as "predicted
to exceed available memory". HyprChat cannot read or change that limit. What it can do is unload models
itself, which is what this setting is: before a chat reply loads its model, the least recently used
other models are unloaded until at most ``limit`` reply models remain, this chat's included. It can only
go below what Ollama allows, never above, and it does not make more models fit in memory.

The small models HyprChat uses beside a chat (embeddings, the workspace model for titles and search
planning) are not counted and never unloaded here: counting them would unload the chat model after
every reply, and its prompt cache with it. A model that is busy with another request finishes it first.

0 is off: Ollama decides, which is how HyprChat behaved before this setting. Everything fails open.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime

import config

OFF = 0
MAX = 8
WAIT_SECONDS = 5.0          # how long a reply waits for unloaded models to leave memory
_STEP = 0.25
_lock = asyncio.Lock()      # two replies starting together must not both pick victims from one listing


def setting(value=None) -> int:
    """The configured limit: 0 (off) to MAX. Anything unreadable is off."""
    raw = getattr(config, "CHAT_MAX_LOADED_MODELS", OFF) if value is None else value
    return config.coerce_int(raw, OFF, minimum=OFF, maximum=MAX)


def canonical(name) -> str:
    """An Ollama model name with its tag: ``nomic-embed-text`` and ``nomic-embed-text:latest`` are one model."""
    text = str(name or "").strip().removeprefix("ollama:")
    if not text:
        return ""
    return text if ":" in text.rsplit("/", 1)[-1] else f"{text}:latest"


def same(first, second) -> bool:
    return bool(canonical(first)) and canonical(first) == canonical(second)


def helper_names() -> set[str]:
    """The models HyprChat loads beside a chat by itself."""
    names = {os.getenv("EMBED_MODEL", "nomic-embed-text"),
             getattr(config, "WORKSPACE_MODEL", ""),
             getattr(config, "QUICK_SEARCH_TRIAGE_MODEL", "")}
    return {canonical(name) for name in names if name}


def is_helper(entry: dict, helpers: set[str] | None = None) -> bool:
    """True for an embedding model or one of HyprChat's own helper models."""
    helpers = helper_names() if helpers is None else helpers
    if canonical(entry.get("name") or entry.get("model")) in helpers:
        return True
    details = entry.get("details") or {}
    families = [details.get("family"), *(details.get("families") or [])]
    return any("bert" in str(family or "").lower() for family in families)


_STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$")


def _when(text) -> float:
    """``expires_at`` as a timestamp. Ollama writes nanoseconds, which ``fromisoformat`` does not take."""
    match = _STAMP.match(str(text or "").strip())
    if not match:
        return float("inf")
    seconds, fraction, zone = match.groups()
    try:
        stamp = datetime.fromisoformat(f"{seconds}.{(fraction or '0')[:6]}{'+00:00' if zone in (None, 'Z') else zone}")
    except ValueError:
        return float("inf")
    return stamp.timestamp()


def rows(payload, helpers: set[str] | None = None) -> list[dict]:
    """An ``/api/ps`` reply as plain rows, the model that will be unloaded first on top."""
    helpers = helper_names() if helpers is None else helpers
    out = []
    for entry in (payload or {}).get("models") or []:
        name = str(entry.get("name") or entry.get("model") or "").strip()
        if not name:
            continue
        size = int(entry.get("size") or 0)
        on_gpu = int(entry.get("size_vram") or 0)
        details = entry.get("details") or {}
        out.append({
            "name": name,
            "size_bytes": size,
            "gpu_bytes": min(on_gpu, size) if size else on_gpu,
            "context": int(entry.get("context_length") or 0),
            "parameters": str(details.get("parameter_size") or ""),
            "expires_at": str(entry.get("expires_at") or ""),
            "helper": is_helper(entry, helpers),
        })
    out.sort(key=lambda row: (_when(row["expires_at"]), row["name"]))
    return out


def victims(listing: list[dict], target: str, limit: int) -> list[str]:
    """The models to unload so that at most ``limit`` reply models stay loaded once ``target`` is.

    Helpers and the target itself are never chosen. The least recently used go first: with one keep-alive
    for every call, the model whose ``expires_at`` comes soonest is the one used longest ago.
    """
    if limit <= OFF or not canonical(target):
        return []
    others = [row for row in listing if not row.get("helper") and not same(row["name"], target)]
    excess = len(others) - (limit - 1)
    if excess <= 0:
        return []
    others.sort(key=lambda row: (_when(row.get("expires_at")), row["name"]))
    return [row["name"] for row in others[:excess]]


async def loaded(http) -> list[dict]:
    """What Ollama has loaded now. Raises when Ollama cannot be asked."""
    response = await http.get(f"{config.OLLAMA_URL}/api/ps", timeout=4)
    response.raise_for_status()
    return rows(response.json())


async def unload(http, name: str) -> bool:
    """Ask Ollama to unload one model. An empty prompt with ``keep_alive: 0`` loads nothing and returns at once."""
    try:
        response = await http.post(f"{config.OLLAMA_URL}/api/generate",
                                   json={"model": name, "keep_alive": 0}, timeout=10)
        return response.status_code < 400
    except Exception as error:
        print(f"[MODELS] could not unload {name} (non-fatal): {error}")
        return False


async def make_room(http, model: str, limit: int | None = None, *, wait: float = WAIT_SECONDS) -> list[str]:
    """Unload what the limit says must go before ``model`` is used. Returns the names asked to unload.

    Never raises and never refuses the reply: on any failure Ollama decides, as it did before.
    """
    limit = setting() if limit is None else limit
    if limit <= OFF or not canonical(model):
        return []
    try:
        async with _lock:
            chosen = victims(await loaded(http), model, limit)
            if not chosen:
                return []
            asked = [name for name in chosen if await unload(http, name)]
            waited = 0.0
            while asked and waited < wait:          # a busy model leaves when its request ends: do not wait for that
                await asyncio.sleep(_STEP)
                waited += _STEP
                still = {canonical(row["name"]) for row in await loaded(http)}
                if not any(canonical(name) in still for name in asked):
                    break
            if asked:
                print(f"[MODELS] Unloaded {', '.join(asked)} before {model}: at most {limit} model{'s' if limit != 1 else ''} loaded at once")
            return asked
    except Exception as error:
        print(f"[MODELS] loaded-model limit skipped (non-fatal): {error}")
        return []


async def status(http) -> dict:
    """What the Settings page shows: the limit and what is loaded now."""
    limit = setting()
    try:
        listing = await loaded(http)
    except Exception as error:
        return {"limit": limit, "max": MAX, "models": [], "counted": 0, "reachable": False,
                "error": str(error)[:160] or error.__class__.__name__}
    return {"limit": limit, "max": MAX, "models": listing, "reachable": True,
            "counted": sum(1 for row in listing if not row["helper"])}
