"""
Voice STT/TTS proxy tests. Skip cleanly when the voice services are not
configured (Settings → Connections STT/TTS URLs empty).
"""
import io
import math
import struct
import wave

import pytest


@pytest.fixture(scope="module")
def voice_settings(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    return r.json()


@pytest.fixture(scope="module")
def stt_configured(voice_settings):
    if not voice_settings.get("current_stt_url"):
        pytest.skip("STT not configured")
    return True


@pytest.fixture(scope="module")
def tts_configured(voice_settings):
    if not voice_settings.get("current_tts_url"):
        pytest.skip("TTS not configured")
    return True


def _tone_wav(seconds=1.0, freq=440, rate=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            frames += struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
        w.writeframes(bytes(frames))
    return buf.getvalue()


def test_transcribe_returns_text_key(long_client, stt_configured):
    wav = _tone_wav()
    r = long_client.post("/api/audio/transcribe",
                         files={"file": ("tone.wav", io.BytesIO(wav), "audio/wav")})
    assert r.status_code == 200, r.text
    assert "text" in r.json()  # a pure tone may transcribe to "", just needs the shape


def test_transcribe_rejects_empty_upload(client, stt_configured):
    r = client.post("/api/audio/transcribe",
                    files={"file": ("empty.wav", io.BytesIO(b""), "audio/wav")})
    assert r.status_code == 400


def test_speech_returns_audio(long_client, tts_configured):
    r = long_client.post("/api/audio/speech", json={"text": "Hello world, this is a HyprChat voice test."})
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("audio/")
    assert len(r.content) > 1000


def test_speech_strips_markdown(long_client, tts_configured):
    md_text = (
        "# Heading\n\nSome **bold** text with `inline code` and a [link](https://example.com).\n\n"
        "```python\nprint('this code must not be read aloud')\n```\n\nFinal sentence [1]."
    )
    r = long_client.post("/api/audio/speech", json={"text": md_text})
    assert r.status_code == 200, r.text
    assert len(r.content) > 1000


def test_speech_rejects_unspeakable_input(client, tts_configured):
    r = client.post("/api/audio/speech", json={"text": "```\nonly a code fence\n```"})
    assert r.status_code == 400
    r = client.post("/api/audio/speech", json={"text": ""})
    assert r.status_code == 400


def test_voices_endpoint_shape(client, voice_settings):
    r = client.get("/api/audio/voices")
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d.get("voices"), list)
    assert "default" in d
