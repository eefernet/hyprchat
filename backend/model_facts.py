"""What Ollama itself reports about each installed model, for the Models page and the model picker.

The page used to guess a model's abilities from its name with three different rules, which called
any name ending in "it" an instruct model and every Llama 3 a tool user. Ollama's `/api/show` says it
outright (`capabilities`: completion, tools, thinking, vision, embedding, insert) and also gives the
model's own context limit. One `/api/show` per model, a few at a time, kept for five minutes; a model
that cannot be read is simply left out, and the page falls back to its name guess for that one.
"""
from __future__ import annotations

import asyncio
import time

import config
import model_limits

_TTL = 300
_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
KNOWN = ("completion", "tools", "thinking", "vision", "embedding", "insert")


def from_show(payload) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    reported = payload.get("capabilities")
    capabilities = [c for c in reported if isinstance(c, str)] if isinstance(reported, list) else None
    return {"capabilities": capabilities, "context_limit": model_limits.from_show(payload)}


async def _one(http, name: str, digest: str, gate: asyncio.Semaphore):
    key = (config.OLLAMA_URL, name, digest)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < _TTL:
        return name, cached[1]
    async with gate:
        try:
            response = await http.post(f"{config.OLLAMA_URL}/api/show", json={"model": name}, timeout=4)
            response.raise_for_status()
            facts = from_show(response.json())
        except Exception:
            return name, None
    if len(_cache) >= 512:
        _cache.clear()
    _cache[key] = (time.monotonic(), facts)
    return name, facts


async def installed(http) -> dict:
    """{model name: {"capabilities": [...] | None, "context_limit": int}} for the local models."""
    try:
        tags = await http.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
        tags.raise_for_status()
        listed = [(m.get("name") or "", m.get("digest") or "") for m in tags.json().get("models", [])]
    except Exception:
        return {}
    gate = asyncio.Semaphore(4)
    try:
        async with asyncio.timeout(20):
            results = await asyncio.gather(*[_one(http, name, digest, gate) for name, digest in listed if name])
    except TimeoutError:
        return {}
    return {name: facts for name, facts in results if facts}


def forget(name: str | None = None) -> None:
    """Drop what is remembered: for one model after it was re-created, or everything."""
    for key in [k for k in _cache if name is None or k[1] == name]:
        _cache.pop(key, None)
