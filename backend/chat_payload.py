"""The shape of a chat request for models whose template allows one system message.

Seen live 2026-11-11: every chat with two "Qwen3.8 uncensored" GGUF builds failed three times with
HTTP 500, "Jinja Exception: System message must be at the beginning". Their chat template raises on any
system message that is not the very first message, and an ordinary chat sends several: the instructions,
the rendering rules, the summary of earlier messages, document instructions after the history.

``single_system`` folds every system message into one leading message. It is applied to a copy, and only
for models that refused the ordinary shape (``remember``): the chat loop keeps its own list, because
compaction and the context breakdown find blocks in it by position and identity, and models with a
tolerant template keep the layout they have always had.
"""
from __future__ import annotations

import re

_REFUSAL = re.compile(r"system message must be (?:at the beginning|first)", re.IGNORECASE)
_strict: set[str] = set()      # models that refused more than one system message, for this run of the service


def wants_single_system(error_text) -> bool:
    """True when a model's error says its template takes the system message first and only there."""
    return bool(_REFUSAL.search(str(error_text or "")))


def remember(model) -> None:
    if model:
        _strict.add(str(model))


def is_strict(model) -> bool:
    return str(model or "") in _strict


def forget_all() -> None:
    _strict.clear()


def single_system(messages: list) -> list:
    """The same request with every system message folded into one, first. Other messages keep their
    order and are the same objects. A request that already has that shape is returned as it is."""
    system = [m for m in messages if m.get("role") == "system"]
    if not system or (len(system) == 1 and messages[0] is system[0]):
        return messages
    texts = [str(m.get("content") or "").strip() for m in system]
    merged = {"role": "system", "content": "\n\n".join(text for text in texts if text)}
    return [merged, *[m for m in messages if m.get("role") != "system"]]


def shape(model, messages: list) -> list:
    """What to send to ``model``: the list itself, or its single-system form for a strict model."""
    return single_system(messages) if is_strict(model) else messages
