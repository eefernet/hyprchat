"""
Voice proxy — STT (speech-to-text) and TTS (text-to-speech).

Targets OpenAI-compatible self-hosted servers (Speaches for STT, kokoro-fastapi
for TTS). The browser only talks to HyprChat; these helpers proxy to the LAN
services so no CORS or extra exposure is needed. URLs are admin-configured
settings (config.STT_URL / config.TTS_URL, empty = disabled) read at call time
so a settings PATCH applies live.
"""
import re
import time

import httpx

import config

TTS_MAX_CHARS = 4000
TTS_VOICES_TTL = 300

_voices_cache = {"base": "", "expires_at": 0.0, "voices": []}


def strip_for_tts(text: str) -> str:
    """Reduce assistant markdown to speakable plain text.

    Drops think blocks and code/diagram fences entirely, converts links and
    images to their text, strips markdown markers and citation chips, and
    collapses whitespace. Capped at TTS_MAX_CHARS.
    """
    if not text:
        return ""
    t = text
    # Model reasoning and fenced blocks are not for reading aloud
    t = re.sub(r"<think>[\s\S]*?</think>", " ", t)
    t = re.sub(r"(?:^|\n)[ \t]{0,3}```[\s\S]*?(?:\n[ \t]{0,3}```|$)", " ", t)
    # Images → alt text; links → link text; bare URLs dropped
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", " ", t)
    # Citation markers [1]..[99] and footnote refs [^x]
    t = re.sub(r"\[\^?\d{1,2}\]", " ", t)
    t = re.sub(r"\[\^[^\]]+\]:?", " ", t)
    # Inline code: keep the content, drop the backticks
    t = t.replace("`", "")
    # Headings / emphasis / blockquote / list / table markers
    t = re.sub(r"(?m)^#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^>\s?", "", t)
    t = re.sub(r"(?m)^[ \t]*[-*+]\s+", "", t)
    t = re.sub(r"(?m)^\|.*\|\s*$", " ", t)        # table rows
    t = re.sub(r"(?m)^[-=_*]{3,}\s*$", " ", t)    # hr / table separators
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"(?<!\w)[*_](\S[^*_\n]*?)[*_](?!\w)", r"\1", t)
    # kbd / leftover simple tags
    t = re.sub(r"</?[a-zA-Z][^>]*>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:TTS_MAX_CHARS]


async def transcribe(file_bytes: bytes, filename: str, content_type: str) -> dict:
    """POST audio to the OpenAI-compatible /v1/audio/transcriptions endpoint."""
    base = config.STT_URL.rstrip("/")
    files = {"file": (filename or "recording.webm", file_bytes, content_type or "application/octet-stream")}
    data = {"model": config.STT_MODEL, "response_format": "json"}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{base}/v1/audio/transcriptions", files=files, data=data)
        r.raise_for_status()
        out = r.json()
    return {"text": (out.get("text") or "").strip()}


class SpeechStream:
    """Open upstream TTS response that can be streamed by FastAPI."""

    def __init__(self, client: httpx.AsyncClient, response: httpx.Response, started: float, chars: int, voice_name: str):
        self.client = client
        self.response = response
        self.started = started
        self.chars = chars
        self.voice_name = voice_name

    async def aiter_bytes(self):
        total = 0
        first_chunk_at = None
        try:
            async for chunk in self.response.aiter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if first_chunk_at is None:
                    first_chunk_at = time.time()
                    print(
                        f"[VOICE] TTS first chunk in {int((first_chunk_at - self.started) * 1000)}ms "
                        f"voice={self.voice_name or config.TTS_VOICE} chars={self.chars}"
                    )
                yield chunk
        finally:
            elapsed = int((time.time() - self.started) * 1000)
            print(f"[VOICE] TTS stream closed after {elapsed}ms bytes={total}")
            await self.aclose()

    async def aclose(self):
        await self.response.aclose()
        await self.client.aclose()


async def open_speech_stream(text: str, voice_name: str = "") -> SpeechStream:
    """Open /v1/audio/speech and return an object that streams MP3 chunks."""
    base = config.TTS_URL.rstrip("/")
    payload = {
        "model": "kokoro",
        "input": text,
        "voice": voice_name or config.TTS_VOICE,
        "response_format": "mp3",
    }
    started = time.time()
    client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15, read=180, write=30, pool=10))
    response = None
    try:
        request = client.build_request("POST", f"{base}/v1/audio/speech", json=payload)
        response = await client.send(request, stream=True)
        accepted_ms = int((time.time() - started) * 1000)
        response.raise_for_status()
        print(
            f"[VOICE] TTS upstream accepted in {accepted_ms}ms "
            f"voice={payload['voice']} chars={len(text)}"
        )
        return SpeechStream(client, response, started, len(text), payload["voice"])
    except Exception:
        if response is not None:
            await response.aclose()
        await client.aclose()
        raise


async def speech_stream(text: str, voice_name: str = ""):
    """Async generator yielding MP3 chunks from /v1/audio/speech."""
    stream = await open_speech_stream(text, voice_name)
    async for chunk in stream.aiter_bytes():
        yield chunk


def clear_voice_cache():
    _voices_cache.update({"base": "", "expires_at": 0.0, "voices": []})


async def list_voices() -> list:
    """Voice names from /v1/audio/voices (kokoro-fastapi); [] on any failure."""
    base = config.TTS_URL.rstrip("/")
    now = time.time()
    if _voices_cache["base"] == base and _voices_cache["expires_at"] > now:
        return list(_voices_cache["voices"])
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{base}/v1/audio/voices")
            r.raise_for_status()
            data = r.json()
        voices = data.get("voices") if isinstance(data, dict) else data
        if isinstance(voices, list):
            out = [v if isinstance(v, str) else (v.get("id") or v.get("name") or "") for v in voices]
            out = [v for v in out if v]
            _voices_cache.update({"base": base, "expires_at": now + TTS_VOICES_TTL, "voices": out})
            return out
    except Exception as e:
        print(f"[VOICE] list_voices error: {e}")
        if _voices_cache["base"] == base and _voices_cache["voices"]:
            return list(_voices_cache["voices"])
    return []
