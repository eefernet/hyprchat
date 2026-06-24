"""Health check routes and background health polling."""
import asyncio
import time

import httpx
from fastapi import APIRouter, Query

import comfyui
import config
import database as db

from .context import route_context


router = APIRouter()


def _http():
    return route_context().http


async def _check_service(name: str, url: str, timeout: float = 8) -> dict:
    """Check a single service, return status + response time."""
    t0 = time.time()
    try:
        r = await _http().get(url, timeout=timeout)
        ms = int((time.time() - t0) * 1000)
        if r.status_code < 400:
            # Degraded if response > 3s
            status = "degraded" if ms > 3000 else "ok"
            return {"status": status, "response_ms": ms}
        return {"status": "error", "response_ms": ms, "error": f"HTTP {r.status_code}"}
    except httpx.TimeoutException:
        ms = int((time.time() - t0) * 1000)
        return {"status": "error", "response_ms": ms, "error": f"timed out after {timeout}s"}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return {"status": "error", "response_ms": ms, "error": str(e)[:200]}


async def _check_searxng() -> dict:
    """Check SearXNG: healthz for uptime, then a test search for rate-limit detection."""
    t0 = time.time()
    try:
        r = await _http().get(f"{config.SEARXNG_URL}/healthz", timeout=8)
        ms = int((time.time() - t0) * 1000)
        if r.status_code >= 400:
            return {"status": "error", "response_ms": ms, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return {"status": "error", "response_ms": ms, "error": str(e)[:200]}
    # Service is up — now check if rate-limited by doing a real search
    # Use a specific-enough query that won't be trivially cached but should always have results
    try:
        r2 = await _http().get(
            f"{config.SEARXNG_URL}/search",
            params={"q": "united states population 2024", "format": "json"},
            timeout=10,
        )
        if r2.status_code == 429:
            return {"status": "degraded", "response_ms": ms, "rate_limited": True}
        if r2.status_code >= 400:
            return {"status": "degraded", "response_ms": ms, "rate_limited": True}
        data = r2.json()
        results = data.get("results", [])
        unresponsive = data.get("unresponsive_engines", [])
        # Rate-limited: no results at all
        if not results:
            return {"status": "degraded", "response_ms": ms, "rate_limited": True}
        # Filter out permanently suspended engines (SearXNG auto-disables these — not rate limiting)
        active_unresponsive = [e for e in unresponsive
                               if not (isinstance(e, (list, tuple)) and len(e) > 1
                                       and "Suspended" in str(e[1]))]
        # Only flag rate-limited if many active engines are failing or results are very thin
        if len(active_unresponsive) >= 3 or len(results) < 5:
            return {"status": "degraded", "response_ms": ms, "rate_limited": True,
                    "unresponsive_engines": [e[0] if isinstance(e, (list, tuple)) else str(e) for e in unresponsive[:5]]}
        return {"status": "ok", "response_ms": ms, "rate_limited": False}
    except Exception:
        # Search failed but healthz was ok — mark as degraded
        return {"status": "degraded", "response_ms": ms, "rate_limited": True}


_HEALTH_ENDPOINTS = {
    "ollama": lambda: f"{config.OLLAMA_URL}/api/tags",
    "codebox": lambda: f"{config.CODEBOX_URL}/health",
    "n8n": lambda: f"{config.N8N_URL}/healthz",
}


async def run_health_checks() -> dict:
    """Run all health checks and log to DB."""
    checks = {"storage": route_context().storage_health_check()}
    for name, url_fn in _HEALTH_ENDPOINTS.items():
        result = await _check_service(name, url_fn())
        checks[name] = result
    # SearXNG gets its own special check (rate-limit detection)
    checks["searxng"] = await _check_searxng()
    # Optional services — only checked (and reported) when configured
    if config.COMFYUI_URL:
        checks["comfyui"] = await comfyui.check_health(_http())
    if config.STT_URL:
        checks["stt"] = await _check_service("stt", f"{config.STT_URL}/v1/models")
    if config.TTS_URL:
        checks["tts"] = await _check_service("tts", f"{config.TTS_URL}/v1/models", timeout=12)
    # Log to DB (non-blocking)
    try:
        conn = await db.get_db()
        try:
            for name, result in checks.items():
                await conn.execute(
                    "INSERT INTO service_health_log (service, status, response_ms, error) VALUES (?, ?, ?, ?)",
                    (name, result["status"], result.get("response_ms", 0), result.get("error", ""))
                )
            await conn.commit()
        finally:
            await conn.close()
    except Exception as e:
        print(f"[Health] DB log error: {e}")
    return checks


async def health_check_loop():
    """Background: check all services every 5 minutes."""
    while True:
        try:
            await run_health_checks()
        except Exception as e:
            print(f"[Health] Loop error: {e}")
        await asyncio.sleep(300)  # 5 minutes


@router.get("/api/health")
async def health():
    checks = await run_health_checks()
    return {"status": "ok", "version": "2.0.0", "services": checks}


@router.get("/api/health/history")
async def health_history(days: int = Query(default=90, ge=1, le=365)):
    """Return daily uptime aggregates per service for the last N days."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT service, date(checked_at) as day,
                      COUNT(*) as total,
                      SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) as ok_count,
                      SUM(CASE WHEN status='degraded' THEN 1 ELSE 0 END) as degraded_count,
                      SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as error_count,
                      AVG(response_ms) as avg_ms
               FROM service_health_log
               WHERE checked_at >= datetime('now', ?)
               GROUP BY service, day
               ORDER BY service, day""",
            (f"-{days} days",)
        )
        # Organize by service
        services = {}
        for row in rows:
            svc = row["service"]
            if svc not in services:
                services[svc] = []
            total = row["total"]
            ok_pct = round((row["ok_count"] / total) * 100, 1) if total else 0
            degraded_pct = round((row["degraded_count"] / total) * 100, 1) if total else 0
            error_pct = round((row["error_count"] / total) * 100, 1) if total else 0
            services[svc].append({
                "day": row["day"],
                "total_checks": total,
                "ok_pct": ok_pct,
                "degraded_pct": degraded_pct,
                "error_pct": error_pct,
                "avg_ms": round(row["avg_ms"] or 0),
            })
        # Calculate overall uptime per service
        summary = {}
        for svc, days_data in services.items():
            total_checks = sum(d["total_checks"] for d in days_data)
            total_ok = sum(d["ok_pct"] * d["total_checks"] / 100 for d in days_data)
            uptime = round((total_ok / total_checks) * 100, 2) if total_checks else 0
            # Current status from most recent check
            last_row = await conn.execute_fetchall(
                "SELECT status, response_ms FROM service_health_log WHERE service=? ORDER BY checked_at DESC LIMIT 1",
                (svc,)
            )
            current = last_row[0]["status"] if last_row else "unknown"
            summary[svc] = {
                "uptime_pct": uptime,
                "current_status": current,
                "avg_response_ms": round(sum(d["avg_ms"] for d in days_data) / len(days_data)) if days_data else 0,
                "days": days_data,
            }
        return {"services": summary, "period_days": days}
    finally:
        await conn.close()

