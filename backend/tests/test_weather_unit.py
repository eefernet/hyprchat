"""Offline unit tests for weather.py — report formatting and the gatherer's
no-location skip. No network: Open-Meteo calls are never reached.

Run: python -m pytest tests/test_weather_unit.py -v
"""

import asyncio
import sys
from pathlib import Path

from .optional_deps import HAS_AIOSQLITE, install_aiosqlite_stub

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

if not HAS_AIOSQLITE:
    install_aiosqlite_stub()

import weather  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


_FORECAST = {
    "current": {"temperature_2m": 31.2, "apparent_temperature": 35.0,
                "precipitation": 0.0, "weather_code": 1, "wind_speed_10m": 12.3},
    "current_units": {"temperature_2m": "°C"},
    "daily": {
        "time": ["2026-07-12", "2026-07-13"],
        "weather_code": [1, 95],
        "temperature_2m_max": [33.1, 28.4],
        "temperature_2m_min": [24.0, 21.7],
        "precipitation_probability_max": [5, 80],
    },
}


def test_wmo_mapping():
    assert weather._wmo(0) == "clear"
    assert weather._wmo(95) == "thunderstorm"
    assert weather._wmo(12345) == "code 12345"
    assert weather._wmo(None) == "unknown"


def test_format_report_full_payload():
    out = weather._format_report("Austin, Texas, US", _FORECAST)
    assert "Austin, Texas, US: mostly clear, 31.2°C (feels 35.0°C)" in out
    assert "Today: mostly clear, 24.0–33.1°C, precip 5%" in out
    assert "Tomorrow: thunderstorm, 21.7–28.4°C, precip 80%" in out


def test_format_report_missing_daily_degrades():
    out = weather._format_report("Nowhere", {"current": {"weather_code": 3}})
    assert out.startswith("Nowhere: overcast")
    assert "Today" not in out  # no daily days → no day lines


def test_get_weather_text_requires_location():
    assert _run(weather.get_weather_text("")) == "No location given."
    assert _run(weather.get_weather_text("   ")) == "No location given."


def test_gatherer_skips_without_location(monkeypatch):
    async def _profile(user_id=None):
        return {"location": ""}
    monkeypatch.setattr(weather.db, "get_assistant_profile", _profile)
    assert _run(weather._gather_weather("u1")) == ""


def test_gatherer_uses_profile_location(monkeypatch):
    async def _profile(user_id=None):
        return {"location": "Austin"}

    async def _fake_text(location):
        assert location == "Austin"
        return "Austin: clear, 30°C"
    monkeypatch.setattr(weather.db, "get_assistant_profile", _profile)
    monkeypatch.setattr(weather, "get_weather_text", _fake_text)
    assert _run(weather._gather_weather("u1")) == "Austin: clear, 30°C"


def test_gatherer_registered():
    from agents import assistant as assistant_mod
    assert "weather" in assistant_mod.gatherer_names()
