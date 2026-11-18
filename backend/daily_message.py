"""The greeting line on the empty new-chat screen.

One line per user per daypart is kept in memory, and the server makes at most one of these model calls at
a time: a request that arrives while another is being written gets the fallback line at once instead of
queueing behind it in Ollama. The call is streamed with a small output cap, so giving up on it closes the
connection and Ollama stops generating (a non-streamed call keeps running after the client has gone:
on 2026-11-17 a burst of page loads left one such call running for 8m38s on the shared server).
"""
import asyncio
import json
import re
import time
from collections import OrderedDict

DAYPARTS = ("morning", "afternoon", "evening", "night")
CACHE_MAX = 256
CALL_SECONDS = 15
MAX_TOKENS = 60
MAX_CHARS = 110

SYSTEM = (
    "You write the single greeting line for HyprChat's empty new-chat screen. "
    "Voice: unhinged but brilliant — a chaotic-genius AI that has been alone in the "
    "server rack too long and is thrilled the user showed up. Weird imagery, deadpan "
    "menace, feral enthusiasm, and the occasional gentle roast of the user by name are "
    "all fair game. ONE line, 4-14 words. If a name is given you may address them by it "
    "(first name only). Banned: corporate cheer, the words unleash/empower/journey/"
    "possibilities, generic productivity slogans, being boring. No quotes, no emoji, "
    "no markdown, no brand name. Roast lightly; never actually cruel."
)

_cache: OrderedDict = OrderedDict()
_busy = asyncio.Lock()


def daypart_of(hour: int) -> str:
    return "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 17 else "evening" if 17 <= hour < 22 else "night"


def clean_name(name) -> str:
    name = re.sub(r"[\r\n]+", " ", str(name or "")).strip()[:40]
    # Generic seed profile names are greeted namelessly.
    return "" if name.lower() in ("main", "default", "user") else name


def fallback(name: str) -> str:
    return f"Feed me a problem, {name}. I've been chewing drywall." if name else "The GPUs are warm and morally flexible."


def clean(text) -> str:
    """The model's reply as one plain line, or '' when it is unusable."""
    text = str(text or "").strip()
    text = re.sub(r"^[\"'`“”]+|[\"'`“”]+$", "", text)
    text = re.sub(r"\s+", " ", text.splitlines()[0] if text else "").strip()
    text = re.sub(r"^(tagline|line|greeting)\s*:\s*", "", text, flags=re.I).strip()
    text = re.sub(r"^[\"'`“”]+|[\"'`“”]+$", "", text).strip()
    return text if text and len(text) <= MAX_CHARS else ""


def messages(name: str, daypart: str) -> list:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"User's name: {name or 'unknown'}. It is {daypart} on {time.strftime('%A, %B %d, %Y')}. "
            "Write one fresh line greeting them into a new chat. Do not be tame."
        )},
    ]


async def ask_model(http, ollama_url: str, model: str, num_ctx: int, name: str, daypart: str) -> str:
    """One streamed call. Cancelling it (the caller's time limit) closes the connection, which stops Ollama."""
    parts = []
    async with http.stream("POST", f"{ollama_url}/api/chat", json={
        "model": model, "messages": messages(name, daypart), "stream": True, "think": False,
        "keep_alive": "10m",
        "options": {"num_ctx": num_ctx, "num_predict": MAX_TOKENS, "temperature": 1.15},
    }, timeout=CALL_SECONDS + 5) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(str(chunk["error"]))
            parts.append((chunk.get("message") or {}).get("content") or "")
            if chunk.get("done"):
                break
    return "".join(parts)


async def get(user_id: str, name: str, daypart: str, generate, *, date: str = "", seconds: float = CALL_SECONDS) -> dict:
    """The line for this user and daypart: ``{"message", "fallback"?}``.

    ``generate()`` is an awaitable that returns the model's raw text. It runs at most once at a time for the
    whole server and only on a cache miss; its result is kept for the rest of the daypart. A fallback is
    never kept, so the next request tries again.
    """
    name = clean_name(name)
    if daypart not in DAYPARTS:
        daypart = daypart_of(time.localtime().tm_hour)
    key = (str(user_id or ""), date or time.strftime("%Y-%m-%d"), daypart, name)
    if key in _cache:
        _cache.move_to_end(key)
        return {"message": _cache[key]}
    if _busy.locked():
        return {"message": fallback(name), "fallback": True, "busy": True}
    async with _busy:
        try:
            message = clean(await asyncio.wait_for(generate(), seconds))
        except Exception as error:
            print(f"[DAILY-MESSAGE] {type(error).__name__}: {error}")
            message = ""
    if not message:
        return {"message": fallback(name), "fallback": True}
    _cache[key] = message
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)
    return {"message": message}


def forget():
    _cache.clear()
