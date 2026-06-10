"""
Cloud model provider adapters for HyprChat.

Ollama remains the default provider. Cloud model IDs are prefixed:
`openai:<model>` or `anthropic:<model>`. Unprefixed IDs are treated as
Ollama for backward compatibility with existing conversations and personas.
"""
import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

import database as db


CLOUD_PROVIDERS = {"openai", "anthropic"}
SUPPORTED_PROVIDERS = {
    "openai": {
        "label": "OpenAI",
        "env_key": "OPENAI_API_KEY",
        "models_url": "https://api.openai.com/v1/models",
        "responses_url": "https://api.openai.com/v1/responses",
    },
    "anthropic": {
        "label": "Anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "models_url": "https://api.anthropic.com/v1/models",
        "messages_url": "https://api.anthropic.com/v1/messages",
        "version": "2023-06-01",
    },
}


class ProviderError(Exception):
    def __init__(self, message: str, provider: str = "", status_code: int | None = None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


def split_model_id(model_id: str) -> tuple[str, str]:
    raw = (model_id or "").strip()
    if ":" in raw:
        provider, name = raw.split(":", 1)
        if provider in CLOUD_PROVIDERS or provider == "ollama":
            return provider, name
    return "ollama", raw


def is_cloud_model(model_id: str) -> bool:
    provider, _ = split_model_id(model_id)
    return provider in CLOUD_PROVIDERS


def provider_label(provider: str) -> str:
    return SUPPORTED_PROVIDERS.get(provider, {}).get("label", provider.title())


def prefixed_model_id(provider: str, model_name: str) -> str:
    return f"{provider}:{model_name}"


def _mask_key(key: str) -> str:
    key = key or ""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def _env_key(provider: str) -> str:
    env_name = SUPPORTED_PROVIDERS.get(provider, {}).get("env_key", "")
    return os.getenv(env_name, "") if env_name else ""


async def provider_statuses() -> list[dict]:
    rows = {
        r["provider"]: r
        for r in await db.list_model_provider_credentials(include_secret=True)
        if r.get("provider") in CLOUD_PROVIDERS
    }
    out = []
    for provider, meta in SUPPORTED_PROVIDERS.items():
        row = rows.get(provider)
        env_key = _env_key(provider)
        row_key = (row or {}).get("api_key", "") or ""
        enabled = bool(row.get("enabled")) if row else bool(env_key)
        source = "user" if row_key else "environment" if env_key and enabled else "none"
        effective_key = row_key or (env_key if enabled else "")
        out.append({
            "provider": provider,
            "label": meta["label"],
            "enabled": enabled,
            "has_key": bool(row_key or env_key),
            "key_source": source,
            "key_hint": _mask_key(effective_key),
            "last_test_status": (row or {}).get("last_test_status", ""),
            "last_test_error": (row or {}).get("last_test_error", ""),
            "last_test_at": (row or {}).get("last_test_at"),
        })
    return out


async def get_api_key(provider: str) -> str:
    if provider not in CLOUD_PROVIDERS:
        return ""
    row = await db.get_model_provider_credential(provider, include_secret=True)
    if row and not row.get("enabled"):
        return ""
    if row and row.get("api_key"):
        return row["api_key"]
    return _env_key(provider)


async def save_provider(provider: str, api_key: str | None = None, enabled: bool | None = None) -> dict:
    if provider not in CLOUD_PROVIDERS:
        raise ProviderError(f"Unsupported model provider: {provider}", provider=provider)
    await db.upsert_model_provider_credential(
        provider,
        api_key=(api_key.strip() if isinstance(api_key, str) and api_key.strip() else None),
        enabled=enabled,
    )
    invalidate_model_cache()
    return next(s for s in await provider_statuses() if s["provider"] == provider)


async def delete_provider(provider: str) -> dict:
    if provider not in CLOUD_PROVIDERS:
        raise ProviderError(f"Unsupported model provider: {provider}", provider=provider)
    await db.delete_model_provider_credential(provider)
    invalidate_model_cache()
    return next(s for s in await provider_statuses() if s["provider"] == provider)


def _openai_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _anthropic_headers(key: str) -> dict:
    return {
        "x-api-key": key,
        "anthropic-version": SUPPORTED_PROVIDERS["anthropic"]["version"],
        "content-type": "application/json",
    }


def _is_openai_chat_model(model_id: str) -> bool:
    m = (model_id or "").lower()
    if not m:
        return False
    blocked = (
        "embed", "embedding", "audio", "tts", "whisper", "dall-e", "image",
        "moderation", "transcribe", "realtime", "search", "babbage", "davinci",
    )
    if any(x in m for x in blocked):
        return False
    return m.startswith(("gpt-", "o", "chatgpt-")) or "gpt" in m


def _sort_model_ids(ids: list[str]) -> list[str]:
    def key(mid: str):
        m = mid.lower()
        rank = 0
        if "mini" in m or "haiku" in m:
            rank = 1
        if "nano" in m:
            rank = 2
        return (rank, m)
    return sorted(set(ids), key=key)


async def list_openai_models(http: httpx.AsyncClient, key: str) -> tuple[list[str], dict]:
    r = await http.get(
        SUPPORTED_PROVIDERS["openai"]["models_url"],
        headers=_openai_headers(key),
        timeout=20,
    )
    if r.status_code >= 400:
        raise ProviderError(_http_error_text(r, "OpenAI model list failed"), provider="openai", status_code=r.status_code)
    raw = r.json().get("data", [])
    ids = _sort_model_ids([m.get("id", "") for m in raw if _is_openai_chat_model(m.get("id", ""))])
    details = {}
    by_id = {m.get("id"): m for m in raw if isinstance(m, dict)}
    for mid in ids:
        item = by_id.get(mid, {})
        pref = prefixed_model_id("openai", mid)
        details[pref] = {
            "size": 0,
            "modified_at": "",
            "digest": "",
            "details": {
                "provider": "openai",
                "provider_label": "OpenAI",
                "owned_by": item.get("owned_by", ""),
                "created": item.get("created"),
            },
        }
    return [prefixed_model_id("openai", mid) for mid in ids], details


async def list_anthropic_models(http: httpx.AsyncClient, key: str) -> tuple[list[str], dict]:
    r = await http.get(
        SUPPORTED_PROVIDERS["anthropic"]["models_url"],
        headers=_anthropic_headers(key),
        timeout=20,
    )
    if r.status_code >= 400:
        raise ProviderError(_http_error_text(r, "Anthropic model list failed"), provider="anthropic", status_code=r.status_code)
    raw = r.json().get("data", [])
    ids = _sort_model_ids([m.get("id", "") for m in raw if str(m.get("id", "")).startswith("claude")])
    details = {}
    by_id = {m.get("id"): m for m in raw if isinstance(m, dict)}
    for mid in ids:
        item = by_id.get(mid, {})
        pref = prefixed_model_id("anthropic", mid)
        details[pref] = {
            "size": 0,
            "modified_at": item.get("created_at", "") or "",
            "digest": "",
            "details": {
                "provider": "anthropic",
                "provider_label": "Anthropic",
                "display_name": item.get("display_name", ""),
            },
        }
    return [prefixed_model_id("anthropic", mid) for mid in ids], details


# /api/models is hit every time the model picker opens; without a short cache
# each open costs two cloud round-trips.
_MODEL_LIST_CACHE: dict[str, Any] = {"at": 0.0, "result": None}
_MODEL_LIST_TTL = 60.0


def invalidate_model_cache() -> None:
    _MODEL_LIST_CACHE["result"] = None


async def list_cloud_models(http: httpx.AsyncClient) -> tuple[list[str], dict, dict]:
    now = time.monotonic()
    cached = _MODEL_LIST_CACHE["result"]
    if cached is not None and now - _MODEL_LIST_CACHE["at"] < _MODEL_LIST_TTL:
        return cached
    models: list[str] = []
    details: dict = {}
    errors: dict = {}
    for provider in SUPPORTED_PROVIDERS:
        key = await get_api_key(provider)
        if not key:
            continue
        try:
            if provider == "openai":
                p_models, p_details = await list_openai_models(http, key)
            else:
                p_models, p_details = await list_anthropic_models(http, key)
            models.extend(p_models)
            details.update(p_details)
        except Exception as e:
            errors[provider] = str(e)
            print(f"[model-providers] {provider} model list skipped: {e}")
    result = (models, details, errors)
    if not errors:
        _MODEL_LIST_CACHE["at"] = now
        _MODEL_LIST_CACHE["result"] = result
    return result


async def test_provider(http: httpx.AsyncClient, provider: str, api_key: str | None = None) -> dict:
    if provider not in CLOUD_PROVIDERS:
        raise ProviderError(f"Unsupported model provider: {provider}", provider=provider)
    stored_key = await get_api_key(provider)
    key = (api_key or "").strip() or stored_key
    if not key:
        raise ProviderError(f"{provider_label(provider)} API key is not configured.", provider=provider)
    # Only persist the test result when the tested key is the stored/effective
    # one — testing an unsaved key must not relabel the saved credential.
    persist = key == stored_key
    try:
        if provider == "openai":
            models, _ = await list_openai_models(http, key)
        else:
            models, _ = await list_anthropic_models(http, key)
        status = {
            "provider": provider,
            "ok": True,
            "model_count": len(models),
            "sample_models": models[:5],
        }
        if persist:
            await db.upsert_model_provider_credential(
                provider,
                last_test_status="ok",
                last_test_error="",
                last_test_at=datetime.now(timezone.utc).isoformat(),
            )
        return status
    except Exception as e:
        if persist:
            await db.upsert_model_provider_credential(
                provider,
                last_test_status="error",
                last_test_error=str(e)[:500],
                last_test_at=datetime.now(timezone.utc).isoformat(),
            )
        raise


def _http_error_text(resp: httpx.Response, fallback: str) -> str:
    try:
        data = resp.json()
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or fallback)
        if isinstance(err, str):
            return err
    except Exception:
        pass
    text = (resp.text or "").strip()
    return text[:500] if text else fallback


def _image_media_type(image_b64: str) -> str:
    """Detect the image MIME type from a data URL header or base64 magic bytes.

    The frontend strips the data-URL prefix before sending, so JPEG/GIF/WebP
    uploads arrive as raw base64 — Anthropic rejects them if media_type says png.
    """
    raw = (image_b64 or "").strip()
    if raw.startswith("data:"):
        mime = raw.split(",", 1)[0][5:].split(";", 1)[0].strip()
        if mime:
            return mime
        return "image/png"
    try:
        head = base64.b64decode(raw[:32])
    except Exception:
        return "image/png"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _data_url(image_b64: str) -> str:
    raw = image_b64 or ""
    if raw.startswith("data:"):
        return raw
    return f"data:{_image_media_type(raw)};base64,{raw}"


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _openai_input(messages: list[dict]) -> list[dict]:
    out = []
    for msg in messages:
        role = msg.get("role") or "user"
        if role == "tool":
            role = "user"
            content = f"[Tool result]\n{_coerce_text(msg.get('content'))}"
            out.append({"role": role, "content": content})
            continue
        if role not in {"user", "assistant", "system", "developer"}:
            role = "user"
        text = _coerce_text(msg.get("content"))
        images = msg.get("images") or []
        if role == "user" and images:
            parts = []
            if text:
                parts.append({"type": "input_text", "text": text})
            for image in images:
                parts.append({"type": "input_image", "image_url": _data_url(str(image)), "detail": "auto"})
            out.append({"role": role, "content": parts})
        else:
            out.append({"role": role, "content": text})
    return out


def _anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role") or "user"
        text = _coerce_text(msg.get("content"))
        if role in {"system", "developer"}:
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            role = "user"
            text = f"[Tool result]\n{text}"
        if role not in {"user", "assistant"}:
            role = "user"
        images = msg.get("images") or []
        if role == "user" and images:
            content = []
            if text:
                content.append({"type": "text", "text": text})
            for image in images:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _image_media_type(str(image)),
                        "data": str(image).split(",", 1)[-1],
                    },
                })
        else:
            content = text
        if out and out[-1]["role"] == role:
            prev = out[-1]["content"]
            if isinstance(prev, str) and isinstance(content, str):
                out[-1]["content"] = f"{prev}\n\n{content}".strip()
            else:
                prev_parts = prev if isinstance(prev, list) else [{"type": "text", "text": prev}]
                next_parts = content if isinstance(content, list) else [{"type": "text", "text": content}]
                out[-1]["content"] = prev_parts + next_parts
        else:
            out.append({"role": role, "content": content})
    if not out:
        out.append({"role": "user", "content": ""})
    return "\n\n".join(system_parts).strip(), out


def _max_output_tokens(options: dict | None, default: int = 4096) -> int:
    options = options or {}
    for key in ("num_predict", "max_tokens", "max_output_tokens"):
        try:
            value = int(options.get(key) or 0)
            if value > 0:
                return min(value, 32768)
        except Exception:
            pass
    return default


def reject_cloud(model_id: str) -> str:
    """Return model_id unless it is cloud-prefixed, else "".

    Used in agent model fallback chains: cloud models are honored only when
    the user explicitly selected them in Settings (per-agent override or
    Planning Model). Inherited fallbacks like the conversation's chat model
    must not silently route agent calls to a paid API.
    """
    return "" if is_cloud_model(model_id or "") else (model_id or "")


async def complete_chat(http, model_id: str, prompt: str, *,
                        temperature: float = 0.2, num_ctx: int = 16384,
                        num_predict: int | None = None,
                        format_json: bool = False,
                        timeout: int = 600, ollama_url: str = "") -> str:
    """Single-prompt completion for agent (non-chat) calls.

    Works for both Ollama model names and cloud-prefixed IDs
    (openai:*/anthropic:*). Returns the full text content; "" on HTTP failure.

    The Ollama path STREAMS internally even though callers get one string:
    with `stream: false` Ollama only notices a client disconnect after the
    generation finishes, so a cancelled agent run kept burning GPU for the
    whole response. Streaming makes Ollama abort the runner the moment
    cancel_registry.await_cancellable drops the connection.

    `num_predict` bounds generation length — structured-output agents
    (plan/review/acceptance JSON) should cap it so a rambling model can't
    hold the inference slot indefinitely.
    """
    provider, _ = split_model_id(model_id)
    if provider in CLOUD_PROVIDERS:
        options: dict = {"temperature": temperature}
        if num_predict:
            options["num_predict"] = num_predict
        parts: list[str] = []
        async for ev in stream_provider_chat(
            http, model_id,
            [{"role": "user", "content": prompt}],
            options=options,
        ):
            if ev.get("type") == "token":
                parts.append(ev.get("content", ""))
        return "".join(parts).strip()

    ollama_options: dict = {"temperature": temperature, "num_ctx": num_ctx}
    if num_predict:
        ollama_options["num_predict"] = num_predict
    payload: dict = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "think": False,
        "options": ollama_options,
    }
    if format_json:
        # Constrains Ollama decoding to valid JSON — kills the parse-fallback
        # cases. think stays disabled AND the thinking fallback below stays:
        # format=json on a reasoning model can route output to thinking.
        payload["format"] = "json"
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    async with http.stream(
        "POST",
        f"{ollama_url}/api/chat",
        json=payload,
        timeout=timeout,
    ) as resp:
        if resp.status_code != 200:
            return ""
        async for line in resp.aiter_lines():
            line = (line or "").strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except Exception:
                continue
            msg = chunk.get("message") if isinstance(chunk.get("message"), dict) else {}
            if isinstance(msg.get("content"), str):
                content_parts.append(msg["content"])
            # Reasoning models can route output to thinking even with think
            # disabled — collect it as a fallback.
            if isinstance(msg.get("thinking"), str):
                thinking_parts.append(msg["thinking"])
            if isinstance(chunk.get("thinking"), str):
                thinking_parts.append(chunk["thinking"])
            if chunk.get("done"):
                break
    text = "".join(content_parts).strip()
    return text or "".join(thinking_parts).strip()


async def stream_provider_chat(
    http: httpx.AsyncClient,
    model_id: str,
    messages: list[dict],
    options: dict | None = None,
) -> AsyncIterator[dict]:
    provider, model_name = split_model_id(model_id)
    if provider == "openai":
        async for event in _stream_openai(http, model_name, messages, options or {}):
            yield event
        return
    if provider == "anthropic":
        async for event in _stream_anthropic(http, model_name, messages, options or {}):
            yield event
        return
    raise ProviderError(f"Unsupported cloud model provider: {provider}", provider=provider)


async def _stream_openai(
    http: httpx.AsyncClient,
    model_name: str,
    messages: list[dict],
    options: dict,
) -> AsyncIterator[dict]:
    key = await get_api_key("openai")
    if not key:
        raise ProviderError("OpenAI API key is not configured.", provider="openai")
    payload: dict[str, Any] = {
        "model": model_name,
        "input": _openai_input(messages),
        "stream": True,
    }
    # Some current OpenAI models reject sampling controls on the Responses API.
    # HyprChat sends default temperature/top_p for Ollama, so omit them here
    # unless we add explicit per-model capability metadata later.
    # Reasoning models spend invisible reasoning tokens inside max_output_tokens,
    # so a 4096 default can produce a truncated or empty visible answer.
    _is_reasoning = bool(re.match(r"^o\d", model_name.lower())) or model_name.lower().startswith("gpt-5")
    max_tokens = _max_output_tokens(options, default=16384 if _is_reasoning else 4096)
    if max_tokens:
        payload["max_output_tokens"] = max_tokens

    async with http.stream(
        "POST",
        SUPPORTED_PROVIDERS["openai"]["responses_url"],
        headers=_openai_headers(key),
        json=payload,
        timeout=300,
    ) as resp:
        if resp.status_code >= 400:
            body = await resp.aread()
            text = body.decode("utf-8", errors="replace")
            raise ProviderError(text[:500] or f"OpenAI HTTP {resp.status_code}", provider="openai", status_code=resp.status_code)
        async for obj in _iter_sse_json(resp):
            typ = obj.get("type", "")
            if typ == "response.output_text.delta":
                delta = obj.get("delta", "")
                if delta:
                    yield {"type": "token", "content": delta}
            elif typ in ("response.completed", "response.incomplete"):
                resp_obj = obj.get("response") or {}
                usage = resp_obj.get("usage") or {}
                yield {
                    "type": "usage",
                    "prompt_tokens": int(usage.get("input_tokens") or 0),
                    "gen_tokens": int(usage.get("output_tokens") or 0),
                }
                if typ == "response.incomplete":
                    reason = (resp_obj.get("incomplete_details") or {}).get("reason") or "max_output_tokens"
                    yield {"type": "token", "content": f"\n\n[Response truncated by OpenAI: {reason}]"}
            elif typ == "response.failed":
                err = (obj.get("response") or {}).get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise ProviderError(msg or "OpenAI response failed", provider="openai")
            elif typ == "error" or obj.get("error"):
                err = obj.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise ProviderError(msg or "OpenAI stream error", provider="openai")


async def _stream_anthropic(
    http: httpx.AsyncClient,
    model_name: str,
    messages: list[dict],
    options: dict,
) -> AsyncIterator[dict]:
    key = await get_api_key("anthropic")
    if not key:
        raise ProviderError("Anthropic API key is not configured.", provider="anthropic")
    system, anth_messages = _anthropic_messages(messages)
    max_tokens = _max_output_tokens(options, default=4096)
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": anth_messages,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if system:
        payload["system"] = system
    if options.get("temperature") is not None:
        payload["temperature"] = options["temperature"]
    elif options.get("top_p") is not None:
        payload["top_p"] = options["top_p"]

    input_tokens = 0
    output_tokens = 0
    retried_max_tokens = False
    while True:
        async with http.stream(
            "POST",
            SUPPORTED_PROVIDERS["anthropic"]["messages_url"],
            headers=_anthropic_headers(key),
            json=payload,
            timeout=300,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                text = body.decode("utf-8", errors="replace")
                # Older Claude models cap max_tokens at 4096/8192 and 400 on
                # higher values; retry once at the universally safe cap.
                if (
                    not retried_max_tokens
                    and resp.status_code == 400
                    and "max_tokens" in text
                    and payload["max_tokens"] > 4096
                ):
                    retried_max_tokens = True
                    payload["max_tokens"] = 4096
                    continue
                raise ProviderError(text[:500] or f"Anthropic HTTP {resp.status_code}", provider="anthropic", status_code=resp.status_code)
            async for event in _anthropic_stream_events(resp, input_tokens, output_tokens):
                yield event
            return


async def _anthropic_stream_events(resp: httpx.Response, input_tokens: int, output_tokens: int) -> AsyncIterator[dict]:
    async for obj in _iter_sse_json(resp):
        typ = obj.get("type", "")
        if typ == "message_start":
            usage = (obj.get("message") or {}).get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or input_tokens or 0)
            output_tokens = int(usage.get("output_tokens") or output_tokens or 0)
        elif typ == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                yield {"type": "token", "content": delta["text"]}
            elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                yield {"type": "thinking", "content": delta["thinking"]}
        elif typ == "message_delta":
            usage = obj.get("usage") or {}
            output_tokens = int(usage.get("output_tokens") or output_tokens or 0)
        elif typ == "message_stop":
            yield {"type": "usage", "prompt_tokens": input_tokens, "gen_tokens": output_tokens}
        elif typ == "error" or obj.get("error"):
            err = obj.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ProviderError(msg or "Anthropic stream error", provider="anthropic")


async def _iter_sse_json(resp: httpx.Response) -> AsyncIterator[dict]:
    async for line in resp.aiter_lines():
        if not line:
            continue
        if not line.startswith("data:"):
            continue
        data = line.split(":", 1)[1].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if isinstance(obj, dict):
            yield obj
