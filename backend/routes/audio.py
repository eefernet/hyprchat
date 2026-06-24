"""Voice STT/TTS proxy routes."""
import time
import uuid

import httpx
from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

import config
import voice


router = APIRouter()

_TTS_REQUEST_TTL = 300
_tts_requests: dict[str, dict] = {}


def _cleanup_tts_requests():
    now = time.time()
    expired = [rid for rid, req in _tts_requests.items() if req.get("expires_at", 0) <= now]
    for rid in expired:
        _tts_requests.pop(rid, None)
    if len(_tts_requests) > 500:
        for rid, _ in sorted(_tts_requests.items(), key=lambda kv: kv[1].get("expires_at", 0))[:100]:
            _tts_requests.pop(rid, None)


async def _tts_streaming_response(text: str, voice_name: str, request_id: str = ""):
    try:
        stream = await voice.open_speech_stream(text, voice_name)
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"TTS service error: HTTP {e.response.status_code}")
    except httpx.TimeoutException:
        raise HTTPException(504, "TTS service timed out before audio could start")
    except Exception as e:
        raise HTTPException(502, f"TTS service unreachable: {str(e)[:200]}")
    headers = {"Cache-Control": "no-store"}
    if request_id:
        headers["X-Hyprchat-TTS-Request"] = request_id
    return StreamingResponse(stream.aiter_bytes(), media_type="audio/mpeg", headers=headers)


@router.post("/api/audio/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    if not config.STT_URL:
        raise HTTPException(503, "Speech-to-text is not configured. Set the STT URL in Settings → Connections.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty audio upload")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "Audio upload too large (25MB max)")
    try:
        return await voice.transcribe(data, file.filename or "recording.webm", file.content_type or "")
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"STT service error: HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(502, f"STT service unreachable: {str(e)[:200]}")


@router.post("/api/audio/speech")
async def synthesize_speech(body: dict = Body(...)):
    if not config.TTS_URL:
        raise HTTPException(503, "Text-to-speech is not configured. Set the TTS URL in Settings → Connections.")
    text = voice.strip_for_tts(body.get("text") or "")
    if not text:
        raise HTTPException(400, "Nothing speakable in the provided text")
    voice_name = (body.get("voice") or "").strip()
    return await _tts_streaming_response(text, voice_name)


@router.post("/api/audio/speech/request")
async def create_speech_request(body: dict = Body(...)):
    if not config.TTS_URL:
        raise HTTPException(503, "Text-to-speech is not configured. Set the TTS URL in Settings → Connections.")
    text = voice.strip_for_tts(body.get("text") or "")
    if not text:
        raise HTTPException(400, "Nothing speakable in the provided text")
    _cleanup_tts_requests()
    request_id = uuid.uuid4().hex
    _tts_requests[request_id] = {
        "text": text,
        "voice": (body.get("voice") or "").strip(),
        "expires_at": time.time() + _TTS_REQUEST_TTL,
    }
    return {
        "id": request_id,
        "url": f"/api/audio/speech/{request_id}",
        "expires_in": _TTS_REQUEST_TTL,
        "chars": len(text),
    }


@router.get("/api/audio/speech/{request_id}")
async def stream_speech_request(request_id: str):
    if not config.TTS_URL:
        raise HTTPException(503, "Text-to-speech is not configured. Set the TTS URL in Settings → Connections.")
    _cleanup_tts_requests()
    req = _tts_requests.get(request_id)
    if not req:
        raise HTTPException(404, "Speech request expired or was not found")
    return await _tts_streaming_response(req["text"], req.get("voice", ""), request_id=request_id)


@router.get("/api/audio/voices")
async def list_tts_voices():
    if not config.TTS_URL:
        return {"voices": [], "default": config.TTS_VOICE}
    return {"voices": await voice.list_voices(), "default": config.TTS_VOICE}
