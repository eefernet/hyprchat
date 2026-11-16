"""A local model's own context limit, and the window a chat really gets.

Ollama loads a model with at most the context it was built for. Checked on the homelab 2026-11-15:
``lstep/neuraldaredevil-8b`` asked for 32,768 was loaded at 8,192, and a prompt that did not fit was
cut to about half of that with no error. The chat used the number it had asked for everywhere else, so
auto-compaction and the context-full check never fired before the real window filled, and the context
pill showed one number or the other depending on how many model calls the reply made.

``context_limit`` reads the limit from Ollama's model metadata. ``effective`` is the window to ask for
and to plan with. Both are safe to call for any model: an unknown limit is 0 and changes nothing.
"""
from __future__ import annotations

import asyncio
import time

import config

_TTL = 300          # seconds a limit is trusted; a model re-created under the same name is rare
_MAX = 256
_cache: dict[tuple[str, str], tuple[float, int]] = {}


def local_name(model) -> str:
    """The Ollama name of a model id, or '' when it is not a local model."""
    name = str(model or "").strip().removeprefix("ollama:")
    if not name or name == "auto" or name.startswith(("openai:", "anthropic:", "custom:")):
        return ""
    return name


def from_show(payload) -> int:
    """The limit in an ``/api/show`` reply: ``<architecture>.context_length`` in the model metadata."""
    info = (payload or {}).get("model_info") if isinstance(payload, dict) else None
    for key, value in (info or {}).items():
        if str(key).endswith(".context_length"):
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0
    return 0


async def context_limit(http, model) -> int:
    """The model's own limit in tokens. 0 when it is not known: a cloud model, or Ollama did not answer.
    A lookup that fails is never cached and never raises: a chat must not fail over this."""
    name = local_name(model)
    if not name:
        return 0
    key = (config.OLLAMA_URL, name)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < _TTL:
        return cached[1]
    try:
        async with asyncio.timeout(4):
            response = await http.post(f"{config.OLLAMA_URL}/api/show", json={"model": name}, timeout=3)
            response.raise_for_status()
            limit = from_show(response.json())
    except (Exception, TimeoutError):
        return cached[1] if cached else 0
    if len(_cache) >= _MAX:
        _cache.clear()
    _cache[key] = (time.monotonic(), limit)
    return limit


def effective(asked, limit) -> int:
    """The window the model will run with: what was asked for, but never more than the model has."""
    try:
        asked, limit = int(asked or 0), int(limit or 0)
    except (TypeError, ValueError):
        return 0
    return min(asked, limit) if asked > 0 and limit > 0 else max(0, asked)
