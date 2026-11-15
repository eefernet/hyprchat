"""How many tool rounds a chat reply gets, and what happens when they run out.

Seen live 2026-11-14: "look at the code base" stopped three replies in a row at 12 rounds while using
6 % of a 262K window, and each reply was only "Ran out of round budget". Nothing asked the model to
answer from what its tools had already returned, and the limit had no setting.

A round is one model call. The loop in ``agents/chat.py`` runs up to ``limit()`` of them; a reply that
is still calling tools after the last one gets one more call here, ``wrap_up``, that asks for the answer.
"""
from __future__ import annotations

import asyncio
import json
import time

import chat_context
import config
import model_providers

DEFAULT = 40
CHOICES = (12, 20, 40, 60, 100)      # what Settings offers; any whole number in range is accepted
MINIMUM, MAXIMUM = 4, 200

WRAP_UP = (
    "You have used all {rounds} tool rounds available for this reply, so no more tools can run. "
    "Do not call a tool. Write your answer to the user now, from what you found above. "
    "If part of the task is not finished, say briefly what is left. "
    "Do not repeat text you already wrote in this reply."
)
# The model went quiet after its tools had run (small models do this): the same request, without the
# claim about the limit.
SILENT = (
    "Your last reply was empty. Do not call a tool. Write your answer to the user now, from what you "
    "found above. If part of the task is not finished, say briefly what is left. "
    "Do not repeat text you already wrote in this reply."
)
NOTICE = "_[The tool rounds ran out before a final answer was written.]_"
SILENCE_SECONDS = 15


def setting(value=None) -> int:
    """The Settings value, clamped. ``value`` is what a settings request sent; None reads the current one."""
    return config.coerce_int(config.MAX_AGENT_ROUNDS if value is None else value, DEFAULT,
                             minimum=MINIMUM, maximum=MAXIMUM)


def limit(*, daedalus: bool) -> int:
    """Rounds for this reply. Daedalus coding chats keep their own number (they hand work to jobs)."""
    source, fallback = (config.MAX_AGENT_ROUNDS_CODER, 30) if daedalus else (config.MAX_AGENT_ROUNDS, DEFAULT)
    return config.coerce_int(source, fallback, minimum=1, maximum=MAXIMUM)


def wrap_up_messages(messages: list, rounds: int, silent: bool = False) -> list:
    """The request for the answer: everything the model has, plus one instruction. Never saved."""
    return [*messages, {"role": "user", "content": SILENT if silent else WRAP_UP.format(rounds=rounds)}]


def fits(messages, tools, window, ratio) -> bool:
    """Room for the request and a reply. A window of 0 (cloud models) is not known here."""
    if not window:
        return True
    needed = chat_context.payload_estimate(messages, tools) * (ratio or chat_context.DEFAULT_RATIO)
    return needed <= window - chat_context.reply_reserve(window)


def visible_text(raw: str, streaming: bool = False) -> str:
    """The reply without reasoning some models write inline as <think>…</think>. An unclosed block
    hides everything after it. While streaming, the start of a tag that is still arriving is held back."""
    text, out = raw, []
    while True:
        start = text.find("<think>")
        if start < 0:
            break
        out.append(text[:start])
        end = text.find("</think>", start)
        if end < 0:
            text = ""
            break
        text = text[end + len("</think>"):]
    shown = "".join(out) + text
    if not streaming:
        return shown
    for size in range(min(len("<think>") - 1, len(shown)), 0, -1):
        if "<think>".startswith(shown[-size:]):
            return shown[:-size]
    return shown


async def _ollama_events(http, payload):
    """Chunks of one streamed /api/chat call. Yields None while the model is silent, so the caller can
    keep the browser connection alive."""
    async with http.stream("POST", f"{config.OLLAMA_URL}/api/chat", json=payload, timeout=300) as response:
        if response.status_code != 200:
            raise RuntimeError((await response.aread()).decode(errors="replace")[:300])
        lines, finished = asyncio.Queue(), object()

        async def drain():
            try:
                async for line in response.aiter_lines():
                    await lines.put(line)
            except Exception as error:      # handed to the reader below
                await lines.put(error)
            finally:
                await lines.put(finished)

        task = asyncio.create_task(drain())
        try:
            while True:
                try:
                    line = await asyncio.wait_for(lines.get(), timeout=SILENCE_SECONDS)
                except asyncio.TimeoutError:
                    yield None
                    continue
                if line is finished:
                    return
                if isinstance(line, Exception):
                    raise line
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise RuntimeError(str(chunk["error"])[:300])
                message = chunk.get("message") or {}
                yield {"text": message.get("content") or "", "called": bool(message.get("tool_calls")),
                       "done": bool(chunk.get("done")), "reason": chunk.get("done_reason") or "",
                       "prompt_tokens": int(chunk.get("prompt_eval_count") or 0),
                       "gen_tokens": int(chunk.get("eval_count") or 0)}
        finally:
            task.cancel()


async def _provider_events(http, model_id, messages, options, tools):
    async for event in model_providers.stream_provider_chat(http, model_id, messages, options=options, tools=tools):
        kind = event.get("type")
        if kind == "token":
            yield {"text": event.get("content") or ""}
        elif kind == "tool_call":
            yield {"called": True}
        elif kind == "usage":
            yield {"prompt_tokens": int(event.get("prompt_tokens") or 0),
                   "gen_tokens": int(event.get("gen_tokens") or 0)}
        elif kind == "finish":
            yield {"reason": event.get("reason") or "", "done": True}


async def wrap_up(*, http, provider: str, model: str, request_model: str, messages: list, options: dict,
                  tools=None, provider_tools=None, think=None, rounds: int, looks_like_call=None,
                  silent: bool = False):
    """One more model call that asks for the answer. An async generator of steps:

    ``{"keepalive": True}`` while the model is silent, ``{"token": text}`` for text to show,
    ``{"usage": {...}}`` with the call's real counts, and last ``{"result": {"ok", "text", "reason",
    "prompt_tokens", "gen_tokens", "elapsed"}}``. ``ok`` is false when the call failed, wrote nothing,
    or answered with a tool call anyway: the caller then takes the text back and shows ``NOTICE``.

    The tool definitions stay in the request although no tool will run. Leaving them out would change
    the prompt from its first tokens (no cache hit on a prompt this long) and providers refuse earlier
    tool calls in the messages without them. A tool call that comes back is ignored.
    """
    sent = wrap_up_messages(messages, rounds, silent)
    started = time.monotonic()
    raw, shown, called, reason = "", "", False, ""
    usage = {"prompt_tokens": 0, "gen_tokens": 0}
    failure = ""
    try:
        if provider == "ollama":
            payload = {"model": model, "messages": sent, "stream": True, "options": options, "keep_alive": "10m"}
            if tools:
                payload["tools"] = tools
            if think is not None:
                payload["think"] = False      # the answer should start at once; the thinking was done in the rounds
            source = _ollama_events(http, payload)
        else:
            source = _provider_events(http, request_model, sent, options, provider_tools)
        async for step in source:
            if step is None:
                yield {"keepalive": True}
                continue
            called = called or bool(step.get("called"))
            reason = step.get("reason") or reason
            if step.get("prompt_tokens") or step.get("gen_tokens"):
                usage = {"prompt_tokens": step.get("prompt_tokens") or usage["prompt_tokens"],
                         "gen_tokens": step.get("gen_tokens") or usage["gen_tokens"]}
            if step.get("text"):
                raw += step["text"]
                now = visible_text(raw, streaming=True)
                if len(now) > len(shown) and now.startswith(shown):
                    yield {"token": now[len(shown):]}
                    shown = now
    except asyncio.CancelledError:
        raise
    except Exception as error:
        failure = f"{type(error).__name__}: {error}"[:200]
        print(f"[CHAT] wrap-up call failed: {failure}")
    final = visible_text(raw)
    if not failure and len(final) > len(shown) and final.startswith(shown):
        yield {"token": final[len(shown):]}       # what was held back as a possible tag
    if usage["prompt_tokens"] or usage["gen_tokens"]:
        yield {"usage": dict(usage)}
    text = final.strip()
    ok = not failure and bool(text) and not (looks_like_call and looks_like_call(text))
    yield {"result": {"ok": ok, "text": text, "reason": failure or reason, "called": called,
                      "elapsed": round(time.monotonic() - started, 3), **usage}}
