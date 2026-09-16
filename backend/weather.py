"""Weather for Jarvis briefings — Open-Meteo (keyless, no account).

Registers the "weather" check-in gatherer: when the assistant profile has a
location, briefs open with today/tomorrow conditions. Also backs the
`get_weather` chat tool (tools.py dispatch). Geocoding results are cached
in-process per location string; every network call is bounded so a slow API
can never stall a check-in build.
"""

from __future__ import annotations

import httpx

import database as db

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 10.0
_geocode_cache: dict[str, dict] = {}  # location string -> {name, lat, lon}

# WMO weather interpretation codes (Open-Meteo uses these verbatim)
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def _wmo(code) -> str:
    try:
        return _WMO.get(int(code), f"code {code}")
    except (TypeError, ValueError):
        return "unknown"


async def _geocode(location: str) -> dict | None:
    key = location.strip().lower()
    if key in _geocode_cache:
        return _geocode_cache[key]
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(_GEOCODE_URL, params={
            "name": location, "count": 1, "language": "en", "format": "json"})
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
    if not results:
        return None
    hit = results[0]
    place = {
        "name": ", ".join(x for x in (hit.get("name"), hit.get("admin1"),
                                      hit.get("country_code")) if x),
        "lat": hit["latitude"], "lon": hit["longitude"],
    }
    _geocode_cache[key] = place
    return place


async def _forecast(lat: float, lon: float) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(_FORECAST_URL, params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": 2, "timezone": "auto",
        })
        r.raise_for_status()
        return r.json() or {}


def _format_report(place_name: str, data: dict) -> str:
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    unit = ((data.get("current_units") or {}).get("temperature_2m") or "°C")
    lines = [f"{place_name}: {_wmo(cur.get('weather_code'))}, "
             f"{cur.get('temperature_2m')}{unit} "
             f"(feels {cur.get('apparent_temperature')}{unit}), "
             f"wind {cur.get('wind_speed_10m')} km/h"]
    days = daily.get("time") or []
    for i, label in enumerate(("Today", "Tomorrow")):
        if i >= len(days):
            break
        lines.append(
            f"{label}: {_wmo((daily.get('weather_code') or [None]*2)[i])}, "
            f"{(daily.get('temperature_2m_min') or ['?']*2)[i]}–"
            f"{(daily.get('temperature_2m_max') or ['?']*2)[i]}{unit}, "
            f"precip {(daily.get('precipitation_probability_max') or ['?']*2)[i]}%")
    return "\n".join(lines)


async def get_weather_text(location: str) -> str:
    """Weather summary for a free-text location. Raises on network failure
    (callers surface it); returns a hint string for unknown locations."""
    location = (location or "").strip()
    if not location:
        return "No location given."
    place = await _geocode(location)
    if not place:
        return f"Could not find a place called \"{location}\"."
    return _format_report(place["name"], await _forecast(place["lat"], place["lon"]))


async def _gather_weather(user_id: str) -> str:
    """Check-in gatherer: empty string (section skipped) when no location is
    configured; network errors propagate so the brief shows '(unavailable)'."""
    profile = await db.get_assistant_profile(user_id=user_id) or {}
    location = (profile.get("location") or "").strip()
    if not location:
        return ""
    return await get_weather_text(location)


from agents import assistant as _assistant_mod
_assistant_mod.register_gatherer("weather", _gather_weather)
