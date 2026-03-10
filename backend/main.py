"""
HyprChat — FastAPI Backend
Full-stack backend with Ollama streaming, Codebox execution,
SearXNG research, n8n webhook proxy, and SSE status events.
"""
import asyncio
import json
import os
import uuid
import time
import shutil
import re
import base64
import shlex
import urllib.parse
import venv as _venv
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Query, Body
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
import database as db
from tools import CODEAGENT_TOOLS, exec_tool, parse_text_tool_calls, strip_tool_calls
from council import stream_council_chat
from events import EventBus, parse_tool_params
from agents.chat import chat_stream_generate, TOOL_TEMPLATES, detect_template_family
from agents.personas import seed_coder_bot as _seed_coder_bot, seed_conspiracy_bot as _seed_conspiracy_bot, seed_based_bot as _seed_based_bot, seed_all_defaults as _seed_all_defaults
import hf as hf_module
import rag

# ============================================================
# SETTINGS — persistent JSON file
# ============================================================
def load_settings() -> dict:
    """Load runtime settings from disk, merging with defaults."""
    try:
        with open(config.SETTINGS_PATH, "r") as f:
            on_disk = json.load(f)
        return {**config.DEFAULT_SETTINGS, **on_disk}
    except (FileNotFoundError, json.JSONDecodeError):
        return config.DEFAULT_SETTINGS.copy()


def save_settings(settings: dict):
    os.makedirs(os.path.dirname(config.SETTINGS_PATH), exist_ok=True)
    with open(config.SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)


# ============================================================
# SANDBOX — directory init + venv
# ============================================================
def _init_sandbox():
    """Create sandbox directory structure and Python venv on first run."""
    for d in [config.SANDBOX_DIR, config.SANDBOX_OUTPUTS_DIR,
              config.SANDBOX_WORKSPACE_DIR]:
        os.makedirs(d, exist_ok=True)

    venv_python = os.path.join(config.SANDBOX_VENV_DIR, "bin", "python")
    if not os.path.exists(venv_python):
        print(f"[Sandbox] Creating Python venv at {config.SANDBOX_VENV_DIR} ...")
        try:
            _venv.create(config.SANDBOX_VENV_DIR, with_pip=True, clear=False,
                         symlinks=True)
            print("[Sandbox] Venv ready.")
        except Exception as e:
            print(f"[Sandbox] Venv creation failed (non-fatal): {e}")


def _sandbox_size_bytes() -> int:
    """Return total bytes used in the sandbox outputs directory."""
    total = 0
    try:
        for entry in os.scandir(config.SANDBOX_OUTPUTS_DIR):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
    except Exception:
        pass
    return total


# ============================================================
# CLEANUP — delete old files from sandbox/outputs
# ============================================================
def _run_cleanup_sync() -> dict:
    """Synchronously clean up old sandbox output files. Returns stats."""
    settings = load_settings()
    cleanup_days = int(settings.get("file_cleanup_days", 30))
    if cleanup_days == 0:
        return {"deleted": 0, "freed_bytes": 0, "skipped": "cleanup disabled"}

    cutoff = time.time() - (cleanup_days * 86400)
    deleted, freed = 0, 0
    try:
        for entry in os.scandir(config.SANDBOX_OUTPUTS_DIR):
            if entry.is_file(follow_symlinks=False):
                try:
                    if entry.stat().st_mtime < cutoff:
                        freed += entry.stat().st_size
                        os.remove(entry.path)
                        deleted += 1
                except Exception as e:
                    print(f"[Cleanup] Could not remove {entry.path}: {e}")
    except Exception:
        pass
    if deleted:
        print(f"[Cleanup] Removed {deleted} files, freed {freed // 1024} KB")
    return {"deleted": deleted, "freed_bytes": freed}


async def _cleanup_loop():
    """Background task: run cleanup every 6 hours."""
    while True:
        await asyncio.sleep(6 * 3600)
        _run_cleanup_sync()


events = EventBus()


# ============================================================
# APP SETUP
# ============================================================
_cleanup_task_ref = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cleanup_task_ref, _health_task_ref
    await db.init_db()
    os.makedirs(config.UPLOAD_DIR, exist_ok=True)
    os.makedirs(config.TOOLS_DIR, exist_ok=True)
    os.makedirs(config.KB_DIR, exist_ok=True)
    # Init sandbox dirs + venv
    _init_sandbox()
    # Override OLLAMA_URL from persistent settings if set
    _settings = load_settings()
    if _settings.get("ollama_url"):
        config.OLLAMA_URL = _settings["ollama_url"]
        print(f"[Config] Loaded Ollama URL from settings: {config.OLLAMA_URL}")
    if _settings.get("coder_model"):
        config.CODER_MODEL = _settings["coder_model"]
        print(f"[Config] Loaded Coder Model from settings: {config.CODER_MODEL}")
    if "openhands_enabled" in _settings:
        config.OPENHANDS_ENABLED = _settings["openhands_enabled"]
        print(f"[Config] Loaded OpenHands enabled: {config.OPENHANDS_ENABLED}")
    if "openhands_max_rounds" in _settings:
        config.OPENHANDS_MAX_ROUNDS = int(_settings["openhands_max_rounds"])
        print(f"[Config] Loaded OpenHands max rounds: {config.OPENHANDS_MAX_ROUNDS}")
    # Run cleanup once on startup to clear any stale files
    _run_cleanup_sync()
    # Start background cleanup loop
    _cleanup_task_ref = asyncio.create_task(_cleanup_loop())
    # Start health check loop (every 5 min)
    _health_task_ref = asyncio.create_task(_health_check_loop())
    # Load RAG settings from persistent config
    _rag_cfg = _settings.get("rag", {})
    if _rag_cfg.get("embed_model"):
        rag.EMBED_MODEL = _rag_cfg["embed_model"]
    if _rag_cfg.get("chunk_size"):
        rag.CHUNK_SIZE = int(_rag_cfg["chunk_size"])
    if _rag_cfg.get("chunk_overlap") is not None:
        rag.CHUNK_OVERLAP = int(_rag_cfg["chunk_overlap"])
    # Ensure RAG embedding model is available (non-blocking pull)
    asyncio.create_task(rag.ensure_embed_model())
    yield
    for task in [_cleanup_task_ref, _health_task_ref]:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

app = FastAPI(title="HyprChat", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

http = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))

# ============================================================
# PYDANTIC MODELS
# ============================================================
class ChatRequest(BaseModel):
    conversation_id: str
    model: str = config.DEFAULT_MODEL
    messages: list[dict]
    system_prompt: str = ""
    stream: bool = True
    tool_ids: list[str] = []
    persona_id: Optional[str] = None
    num_ctx: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    repeat_penalty: Optional[float] = None

class ExecuteRequest(BaseModel):
    conversation_id: Optional[str] = None
    code: str
    language: str = "python"
    stdin: Optional[str] = None
    timeout: int = config.EXECUTION_TIMEOUT

class SearchRequest(BaseModel):
    conversation_id: Optional[str] = None
    query: str
    count: int = config.SEARCH_RESULTS_COUNT

class N8nRequest(BaseModel):
    conversation_id: Optional[str] = None
    code: str
    language: str = "python"
    stdin: Optional[str] = None
    timeout: int = config.EXECUTION_TIMEOUT

class ShellRequest(BaseModel):
    conversation_id: Optional[str] = None
    command: str
    timeout: int = 30

class FetchUrlRequest(BaseModel):
    conversation_id: Optional[str] = None
    url: str
    max_chars: int = config.MAX_FETCH_CHARS

class ConversationCreate(BaseModel):
    title: str = "New Chat"
    model: str = config.DEFAULT_MODEL
    system_prompt: str = config.DEFAULT_SYSTEM_PROMPT
    model_config_id: Optional[str] = None

class ConversationUpdate(BaseModel):
    title: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    tool_ids: Optional[list[str]] = None
    persona_name: Optional[str] = None
    persona_avatar: Optional[str] = None
    is_council: Optional[str] = None
    council_config_id: Optional[str] = None
    model_config_id: Optional[str] = None

class CouncilCreate(BaseModel):
    name: str = "My Council"
    host_model: str = config.DEFAULT_MODEL
    host_system_prompt: str = ""
    kb_ids: list[str] = []

class CouncilUpdate(BaseModel):
    name: Optional[str] = None
    host_model: Optional[str] = None
    host_system_prompt: Optional[str] = None
    debate_rounds: Optional[int] = None
    kb_ids: Optional[list[str]] = None

class CouncilMemberCreate(BaseModel):
    model: str
    system_prompt: str = ""
    persona_name: str = ""

class CouncilMemberUpdate(BaseModel):
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    persona_name: Optional[str] = None
    points: Optional[int] = None

class CouncilChatRequest(BaseModel):
    conversation_id: str
    council_id: str
    messages: list[dict]
    quick_search: bool = False
    kb_ids: list[str] = []

class QuickSearchRequest(BaseModel):
    query: str
    count: int = 6

class KBCreate(BaseModel):
    name: str
    description: str = ""

class ToolCreate(BaseModel):
    name: str
    description: str = ""
    filename: str
    code: str

class ToolUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    code: Optional[str] = None

class ModelConfigCreate(BaseModel):
    name: str
    base_model: str
    system_prompt: str = ""
    tool_ids: list[str] = []
    kb_ids: list[str] = []
    parameters: dict = {}

class ModelConfigUpdate(BaseModel):
    name: Optional[str] = None
    base_model: Optional[str] = None
    system_prompt: Optional[str] = None
    tool_ids: Optional[list[str]] = None
    kb_ids: Optional[list[str]] = None
    parameters: Optional[dict] = None


# ============================================================
# HEALTH & INFO
# ============================================================
async def _check_service(name: str, url: str, timeout: float = 8) -> dict:
    """Check a single service, return status + response time."""
    t0 = time.time()
    try:
        r = await http.get(url, timeout=timeout)
        ms = int((time.time() - t0) * 1000)
        if r.status_code < 400:
            # Degraded if response > 3s
            status = "degraded" if ms > 3000 else "ok"
            return {"status": status, "response_ms": ms}
        return {"status": "error", "response_ms": ms, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return {"status": "error", "response_ms": ms, "error": str(e)[:200]}


async def _check_searxng() -> dict:
    """Check SearXNG: healthz for uptime, then a test search for rate-limit detection."""
    t0 = time.time()
    try:
        r = await http.get(f"{config.SEARXNG_URL}/healthz", timeout=8)
        ms = int((time.time() - t0) * 1000)
        if r.status_code >= 400:
            return {"status": "error", "response_ms": ms, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return {"status": "error", "response_ms": ms, "error": str(e)[:200]}
    # Service is up — now check if rate-limited by doing a real search
    # Use a specific-enough query that won't be trivially cached but should always have results
    try:
        r2 = await http.get(
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
        # Rate-limited: no results at all, or most engines unresponsive
        if not results:
            return {"status": "degraded", "response_ms": ms, "rate_limited": True}
        if len(unresponsive) >= 2:
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


async def _run_health_checks() -> dict:
    """Run all health checks and log to DB."""
    checks = {}
    for name, url_fn in _HEALTH_ENDPOINTS.items():
        result = await _check_service(name, url_fn())
        checks[name] = result
    # SearXNG gets its own special check (rate-limit detection)
    checks["searxng"] = await _check_searxng()
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


_health_task_ref = None

async def _health_check_loop():
    """Background: check all services every 5 minutes."""
    while True:
        try:
            await _run_health_checks()
        except Exception as e:
            print(f"[Health] Loop error: {e}")
        await asyncio.sleep(300)  # 5 minutes


@app.get("/api/health")
async def health():
    checks = await _run_health_checks()
    return {"status": "ok", "version": "2.0.0", "services": checks}


@app.get("/api/health/history")
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


# ============================================================
# OLLAMA — MODEL LISTING + STREAMING CHAT
# ============================================================
@app.get("/api/models")
async def list_models():
    """Fetch available models from Ollama."""
    try:
        r = await http.get(f"{config.OLLAMA_URL}/api/tags")
        r.raise_for_status()
        data = r.json()
        raw = data.get("models", [])
        model_details = {m["name"]: {
            "size": m.get("size", 0),
            "modified_at": m.get("modified_at", ""),
            "details": m.get("details", {}),
            "digest": m.get("digest", ""),
        } for m in raw}
        return {"models": [m["name"] for m in raw], "model_details": model_details}
    except Exception as e:
        raise HTTPException(502, f"Failed to reach Ollama: {e}")


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """Stream chat with multi-round tool-calling agent loop."""
    _all_custom = await db.get_tools()
    custom_tool_map: dict = {t["name"]: t for t in _all_custom}
    custom_tool_id_map: dict = {t["id"]: t for t in _all_custom}

    return StreamingResponse(
        chat_stream_generate(req, http, events, custom_tool_map, custom_tool_id_map),
        media_type="text/event-stream",
    )


# ============================================================
# FILE DOWNLOADS
# ============================================================
@app.get("/api/downloads/{filename}")
async def download_file_endpoint(filename: str):
    """Serve tool-generated files. Looks in sandbox/outputs first, falls back to legacy UPLOAD_DIR."""
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name != filename:
        raise HTTPException(400, "Invalid filename")
    for search_dir in [config.SANDBOX_OUTPUTS_DIR, config.UPLOAD_DIR]:
        filepath = os.path.join(search_dir, safe_name)
        if not os.path.abspath(filepath).startswith(os.path.abspath(search_dir)):
            continue
        if os.path.exists(filepath):
            return FileResponse(filepath, filename=safe_name)
    return JSONResponse({"error": "File not found"}, status_code=404)


@app.get("/api/downloads/{filename}/contents")
async def archive_contents(filename: str):
    """List files inside a .tar.gz or .zip archive for preview."""
    import tarfile
    import zipfile
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name != filename:
        raise HTTPException(400, "Invalid filename")
    filepath = None
    for search_dir in [config.SANDBOX_OUTPUTS_DIR, config.UPLOAD_DIR]:
        candidate = os.path.join(search_dir, safe_name)
        if os.path.exists(candidate):
            filepath = candidate
            break
    if not filepath:
        return JSONResponse({"error": "File not found"}, status_code=404)
    try:
        entries = []
        if tarfile.is_tarfile(filepath):
            with tarfile.open(filepath, "r:*") as tf:
                for m in tf.getmembers():
                    entries.append({"name": m.name, "size": m.size, "is_dir": m.isdir()})
        elif zipfile.is_zipfile(filepath):
            with zipfile.ZipFile(filepath) as zf:
                for info in zf.infolist():
                    entries.append({"name": info.filename, "size": info.file_size, "is_dir": info.is_dir()})
        else:
            return JSONResponse({"error": "Not a supported archive"}, status_code=400)
        # Sort: directories first, then files
        entries.sort(key=lambda e: (not e["is_dir"], e["name"]))
        return {"filename": safe_name, "file_count": len([e for e in entries if not e["is_dir"]]), "entries": entries}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ============================================================
# BUILT-IN TOOL LIST (for frontend)
# ============================================================
@app.get("/api/builtin-tools")
async def list_builtin_tools():
    """Return the integrated tool suites."""
    return [
        {"id": "codeagent", "name": "⚡ CodeAgent", "description": "Code execution, shell, file management, downloads", "icon": "cpu", "builtin": True},
        {"id": "deep_research", "name": "🔬 Deep Research", "description": "Multi-source parallel research with AI synthesis", "icon": "search", "builtin": True},
        {"id": "conspiracy_research", "name": "🕵️ Conspiracy Research", "description": "Uncensored deep-dive into theories, cover-ups, and hidden agendas", "icon": "search", "builtin": True},
        {"id": "generate_code", "name": "🧬 Code Generator", "description": "Delegate code writing to a code-specialized model sub-agent", "icon": "code", "builtin": True},
    ]


@app.post("/api/seed/all-defaults")
async def seed_all_defaults():
    return await _seed_all_defaults()

@app.post("/api/seed/coder-bot")
async def seed_coder_bot():
    return await _seed_coder_bot()

@app.post("/api/seed/conspiracy-bot")
async def seed_conspiracy_bot():
    return await _seed_conspiracy_bot()

@app.post("/api/seed/based-bot")
async def seed_based_bot():
    return await _seed_based_bot()


# ============================================================
# CODEBOX — CODE EXECUTION
# ============================================================
@app.post("/api/execute")
async def execute_code(req: ExecuteRequest):
    """Execute code on the Codebox API with status events."""
    conv_id = req.conversation_id or "system"

    await events.emit(conv_id, "tool_start", {
        "tool": "execute_code",
        "status": f"Executing {req.language} code...",
        "icon": "code",
        "detail": f"{len(req.code)} chars, timeout {req.timeout}s"
    })

    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/execute",
            json={
                "code": req.code,
                "language": req.language,
                "stdin": req.stdin,
                "timeout": req.timeout,
            },
            timeout=req.timeout + 15
        )
        result = r.json()

        success = result.get("exit_code", -1) == 0 or result.get("success", False)
        await events.emit(conv_id, "tool_end", {
            "tool": "execute_code",
            "status": f"{'✅ Success' if success else '❌ Failed'}",
            "icon": "code",
            "result_preview": (result.get("stdout", "") or result.get("stderr", ""))[:200],
        })

        return result
    except Exception as e:
        await events.emit(conv_id, "tool_error", {
            "tool": "execute_code",
            "status": f"CodeBox unreachable: {str(e)}",
            "icon": "code",
        })
        raise HTTPException(502, f"CodeBox error: {e}")


@app.post("/api/execute/shell")
async def execute_shell(req: ShellRequest):
    """Run a shell command on Codebox."""
    conv_id = req.conversation_id or "system"
    await events.emit(conv_id, "tool_start", {
        "tool": "run_shell",
        "status": f"Running: {req.command[:60]}...",
        "icon": "terminal",
    })

    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": req.command},
            timeout=req.timeout + 5
        )
        result = r.json()
        await events.emit(conv_id, "tool_end", {
            "tool": "run_shell",
            "status": "Command complete",
            "icon": "terminal",
        })
        return result
    except Exception as e:
        await events.emit(conv_id, "tool_error", {"tool": "run_shell", "status": str(e), "icon": "terminal"})
        raise HTTPException(502, f"Shell error: {e}")


@app.get("/api/execute/languages")
async def get_languages():
    """List available languages from Codebox."""
    try:
        r = await http.get(f"{config.CODEBOX_URL}/languages")
        return r.json()
    except Exception as e:
        raise HTTPException(502, f"Codebox error: {e}")


# ============================================================
# SEARXNG — WEB SEARCH
# ============================================================
@app.post("/api/search")
async def search(req: SearchRequest):
    """Search via SearXNG with status events."""
    conv_id = req.conversation_id or "system"

    await events.emit(conv_id, "tool_start", {
        "tool": "research",
        "status": f"Searching: \"{req.query}\"",
        "icon": "search",
    })

    try:
        r = await http.get(
            f"{config.SEARXNG_URL}/search",
            params={"q": req.query, "format": "json", "count": req.count},
            timeout=15,
        )
        data = r.json()
        results = data.get("results", [])[:req.count]

        await events.emit(conv_id, "tool_end", {
            "tool": "research",
            "status": f"Found {len(results)} results for \"{req.query}\"",
            "icon": "search",
            "detail": ", ".join(r.get("title", "")[:40] for r in results[:3]),
        })

        return {
            "query": req.query,
            "results": [{
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
                "engine": r.get("engine", ""),
            } for r in results]
        }
    except Exception as e:
        await events.emit(conv_id, "tool_error", {"tool": "research", "status": str(e), "icon": "search"})
        raise HTTPException(502, f"SearXNG error: {e}")


@app.post("/api/fetch-url")
async def fetch_url(req: FetchUrlRequest):
    """Fetch and clean a URL's content."""
    conv_id = req.conversation_id or "system"

    await events.emit(conv_id, "tool_start", {
        "tool": "fetch_url",
        "status": f"Reading: {req.url[:60]}",
        "icon": "globe",
    })

    try:
        r = await http.get(req.url, timeout=15, follow_redirects=True)
        text = r.text[:req.max_chars]

        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        await events.emit(conv_id, "tool_end", {
            "tool": "fetch_url",
            "status": f"Read {len(text)} chars from {req.url[:40]}",
            "icon": "globe",
        })

        return {"url": req.url, "content": text[:req.max_chars], "length": len(text)}
    except Exception as e:
        await events.emit(conv_id, "tool_error", {"tool": "fetch_url", "status": str(e), "icon": "globe"})
        raise HTTPException(502, f"Fetch error: {e}")


@app.get("/api/proxy-preview")
async def proxy_preview(url: str):
    """Fetch an external URL and return raw content for preview iframe."""
    from starlette.responses import Response as StarletteResponse
    if not url or not url.startswith("http"):
        raise HTTPException(400, "Invalid URL")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        r = await http.get(url, timeout=20, follow_redirects=True, headers=headers)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "pdf" in ct or url.lower().endswith(".pdf"):
            return StarletteResponse(content=r.content, media_type="application/pdf")
        if any(mt in ct for mt in ["image/png", "image/jpeg", "image/gif", "image/webp", "image/svg"]):
            return StarletteResponse(content=r.content, media_type=ct.split(";")[0])
        if "html" in ct:
            html = r.text
            from html import escape as html_escape
            base_tag = f'<base href="{html_escape(url, quote=True)}" target="_blank">'
            if "<head" in html.lower():
                html = re.sub(r'(<head[^>]*>)', r'\1' + base_tag, html, count=1, flags=re.IGNORECASE)
            else:
                html = base_tag + html
            return StarletteResponse(content=html, media_type="text/html; charset=utf-8")
        return StarletteResponse(content=r.text, media_type="text/plain; charset=utf-8")
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Upstream returned {e.response.status_code}")
    except Exception as e:
        raise HTTPException(502, f"Proxy error: {e}")


# ============================================================
# N8N — WEBHOOK PROXY
# ============================================================
@app.post("/api/n8n/execute")
async def n8n_execute(req: N8nRequest):
    """Forward execution request through n8n webhook."""
    conv_id = req.conversation_id or "system"

    await events.emit(conv_id, "tool_start", {
        "tool": "n8n_execute",
        "status": f"Routing through n8n workflow...",
        "icon": "workflow",
        "detail": f"{req.language} code via webhook proxy"
    })

    try:
        r = await http.post(
            f"{config.N8N_URL}{config.N8N_WEBHOOK_PATH}",
            json={
                "code": req.code,
                "language": req.language,
                "stdin": req.stdin,
                "timeout": req.timeout,
            },
            timeout=req.timeout + 10,
        )
        result = r.json()

        await events.emit(conv_id, "tool_end", {
            "tool": "n8n_execute",
            "status": "n8n workflow complete",
            "icon": "workflow",
        })
        return result
    except Exception as e:
        await events.emit(conv_id, "tool_error", {"tool": "n8n_execute", "status": str(e), "icon": "workflow"})
        raise HTTPException(502, f"n8n error: {e}")


# ============================================================
# SSE — STATUS EVENT STREAM
# ============================================================
@app.get("/api/events/{conversation_id}")
async def event_stream(conversation_id: str):
    """SSE endpoint — clients connect to receive real-time status events."""
    queue = await events.subscribe(conversation_id)

    async def generate():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': time.time()})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await events.unsubscribe(conversation_id, queue)

    return StreamingResponse(generate(), media_type="text/event-stream")


# ============================================================
# CONVERSATIONS
# ============================================================
@app.post("/api/conversations")
async def create_conversation(req: ConversationCreate):
    id = f"conv-{uuid.uuid4().hex[:12]}"
    await db.create_conversation(id, req.title, req.model, req.system_prompt, req.model_config_id)
    return {"id": id, **req.model_dump()}


@app.get("/api/conversations")
async def list_conversations(limit: int = Query(50), offset: int = Query(0)):
    return await db.get_conversations(limit, offset)


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return conv


@app.patch("/api/conversations/{conv_id}")
async def update_conversation(conv_id: str, req: ConversationUpdate):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    await db.update_conversation(conv_id, **kwargs)
    return {"status": "updated"}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    await db.delete_conversation(conv_id)
    return {"status": "deleted"}


@app.delete("/api/conversations")
async def delete_all_conversations():
    """Delete ALL conversations and their messages."""
    convs = await db.get_conversations()
    count = 0
    for c in convs:
        await db.delete_conversation(c["id"])
        count += 1
    print(f"[Cleanup] Deleted all {count} conversations")
    return {"deleted": count}


class AddMessageRequest(BaseModel):
    role: str
    content: str
    metadata: Optional[dict] = None

@app.post("/api/conversations/{conv_id}/messages")
async def add_message(conv_id: str, request: Request):
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
        role = body.get("role", "")
        content = body.get("content", "")
        meta = body.get("metadata")
    else:
        form = await request.form()
        role = form.get("role", "")
        content = form.get("content", "")
        meta_str = form.get("metadata")
        meta = None
        if meta_str:
            try:
                meta = json.loads(meta_str)
            except Exception:
                pass
    if not role or content is None:
        raise HTTPException(400, "role and content are required")
    await db.add_message(conv_id, role, content, metadata=meta)
    return {"status": "added"}


# ============================================================
# KNOWLEDGE BASES
# ============================================================
@app.get("/api/knowledge-bases")
async def list_kbs():
    return await db.get_kbs()


@app.post("/api/knowledge-bases")
async def create_kb(req: KBCreate):
    id = f"kb-{uuid.uuid4().hex[:12]}"
    await db.create_kb(id, req.name, req.description)
    return {"id": id, "name": req.name, "description": req.description, "files": []}


@app.put("/api/knowledge-bases/{kb_id}")
async def update_kb(kb_id: str, req: KBCreate):
    await db.update_kb(kb_id, name=req.name, description=req.description)
    return {"status": "updated"}


@app.delete("/api/knowledge-bases/{kb_id}")
async def delete_kb(kb_id: str):
    kb_dir = os.path.join(config.KB_DIR, kb_id)
    if os.path.exists(kb_dir):
        shutil.rmtree(kb_dir)
    await db.delete_kb(kb_id)
    # Remove RAG index for this KB
    try:
        await rag.delete_kb_index(kb_id)
    except Exception as e:
        print(f"[RAG] Error deleting KB index: {e}")
    return {"status": "deleted"}


# Track background indexing status per file
_indexing_status: dict[str, dict] = {}  # key: "kb_id:filename" → status dict


@app.post("/api/knowledge-bases/{kb_id}/files")
async def upload_kb_file(kb_id: str, file: UploadFile = File(...)):
    kb_dir = os.path.join(config.KB_DIR, kb_id)
    os.makedirs(kb_dir, exist_ok=True)

    safe_name = os.path.basename(file.filename or "upload")
    if not safe_name:
        raise HTTPException(400, "Invalid filename")
    filepath = os.path.join(kb_dir, safe_name)
    if not os.path.abspath(filepath).startswith(os.path.abspath(kb_dir)):
        raise HTTPException(400, "Invalid filename")

    content = await file.read()
    if len(content) > config.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, f"File too large (max {config.MAX_UPLOAD_SIZE_MB}MB)")

    with open(filepath, "wb") as f:
        f.write(content)

    file_id = await db.add_kb_file(kb_id, safe_name, filepath, len(content), file.content_type or "")

    # Start background RAG indexing so the upload response returns immediately
    status_key = f"{kb_id}:{safe_name}"
    _indexing_status[status_key] = {"status": "indexing", "filename": safe_name}

    async def _bg_index():
        try:
            result = await rag.index_file(kb_id, safe_name, filepath)
            _indexing_status[status_key] = {"status": "done", "filename": safe_name, **result}
        except Exception as e:
            print(f"[RAG] Indexing failed for {safe_name}: {e}")
            _indexing_status[status_key] = {"status": "error", "filename": safe_name, "error": str(e)}

    asyncio.create_task(_bg_index())

    return {"id": file_id, "filename": safe_name, "file_size": len(content), "indexing": True}


@app.get("/api/knowledge-bases/{kb_id}/files/{filename}/status")
async def get_file_index_status(kb_id: str, filename: str):
    """Check background indexing status for a file."""
    status_key = f"{kb_id}:{filename}"
    status = _indexing_status.get(status_key)
    if status:
        return status
    return {"status": "unknown", "filename": filename}


@app.delete("/api/knowledge-bases/files/{file_id}")
async def delete_kb_file(file_id: int):
    # Get file info before deleting so we can remove from RAG index
    _db = await db.get_db()
    try:
        cursor = await _db.execute("SELECT kb_id, filename FROM kb_files WHERE id = ?", (file_id,))
        row = await cursor.fetchone()
    finally:
        await _db.close()

    await db.delete_kb_file(file_id)

    # Remove from RAG index
    if row:
        try:
            await rag.remove_file(row["kb_id"], row["filename"])
        except Exception as e:
            print(f"[RAG] Error removing file from index: {e}")

    return {"status": "deleted"}


@app.post("/api/knowledge-bases/{kb_id}/reindex")
async def reindex_kb(kb_id: str):
    """Reindex all files in a KB — useful for migration or after changing embed model."""
    kbs = await db.get_kbs()
    kb = next((k for k in kbs if k["id"] == kb_id), None)
    if not kb:
        raise HTTPException(404, "KB not found")
    files = kb.get("files", [])
    if not files:
        return {"status": "no files to index"}
    results = await rag.reindex_kb(kb_id, files)
    return {"status": "reindexed", "results": results}


@app.post("/api/knowledge-bases/reindex-all")
async def reindex_all_kbs():
    """Reindex all knowledge bases — one-time migration to RAG."""
    kbs = await db.get_kbs()
    all_results = []
    for kb in kbs:
        files = kb.get("files", [])
        if files:
            results = await rag.reindex_kb(kb["id"], files)
            all_results.append({"kb_id": kb["id"], "name": kb["name"], "results": results})
    return {"status": "reindexed", "kbs": all_results}


# ============================================================
# TOOLS
# ============================================================
@app.get("/api/tools")
async def list_tools():
    return await db.get_tools()


@app.post("/api/tools")
async def create_tool(req: ToolCreate):
    id = f"tool-{uuid.uuid4().hex[:12]}"
    await db.create_tool(id, req.name, req.description, req.filename, req.code)

    filepath = os.path.join(config.TOOLS_DIR, req.filename)
    with open(filepath, "w") as f:
        f.write(req.code)

    return {"id": id, **req.model_dump()}


@app.post("/api/tools/upload")
async def upload_tool(file: UploadFile = File(...)):
    """Upload a .py file as a tool."""
    safe_name = os.path.basename(file.filename or "tool.py")
    if not safe_name.endswith(".py"):
        raise HTTPException(400, "Only .py files accepted")
    filepath = os.path.join(config.TOOLS_DIR, safe_name)
    if not os.path.abspath(filepath).startswith(os.path.abspath(config.TOOLS_DIR)):
        raise HTTPException(400, "Invalid filename")

    content = await file.read()
    code = content.decode("utf-8")
    name = safe_name.replace(".py", "")
    id = f"tool-{uuid.uuid4().hex[:12]}"

    with open(filepath, "w") as f:
        f.write(code)

    await db.create_tool(id, name, f"Uploaded: {safe_name}", safe_name, code)
    return {"id": id, "name": name, "filename": safe_name, "code": code}


@app.patch("/api/tools/{tool_id}")
async def update_tool(tool_id: str, req: ToolUpdate):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    await db.update_tool(tool_id, **kwargs)
    return {"status": "updated"}


@app.delete("/api/tools/{tool_id}")
async def delete_tool(tool_id: str):
    await db.delete_tool(tool_id)
    return {"status": "deleted"}


@app.put("/api/tools/{tool_id}")
async def update_tool_put(tool_id: str, req: ToolUpdate):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    if kwargs:
        await db.update_tool(tool_id, **kwargs)
    return {"status": "updated"}


# ============================================================
# MODEL CONFIGS
# ============================================================
@app.get("/api/model-configs")
async def list_model_configs():
    return await db.get_model_configs()


@app.post("/api/model-configs")
async def create_model_config(req: ModelConfigCreate):
    id = f"mc-{uuid.uuid4().hex[:12]}"
    await db.create_model_config(id, req.name, req.base_model, req.system_prompt, req.tool_ids, req.kb_ids, req.parameters)
    return {"id": id, **req.model_dump()}


@app.patch("/api/model-configs/{mc_id}")
async def update_model_config(mc_id: str, req: ModelConfigUpdate):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    await db.update_model_config(mc_id, **kwargs)
    return {"status": "updated"}


@app.put("/api/model-configs/{mc_id}")
async def update_model_config_put(mc_id: str, req: ModelConfigUpdate):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    await db.update_model_config(mc_id, **kwargs)
    return {"status": "updated"}


@app.delete("/api/model-configs/{mc_id}")
async def delete_model_config(mc_id: str):
    await db.delete_model_config(mc_id)
    return {"status": "deleted"}


# ============================================================
# OLLAMA MODEL MANAGEMENT
# ============================================================
@app.post("/api/models/pull")
async def pull_model(request: Request):
    """Pull a model from Ollama library — streams progress."""
    body = await request.json()
    model_name = body.get("name", "")
    if not model_name:
        raise HTTPException(400, "Model name required")

    async def generate():
        try:
            async with http.stream("POST", f"{config.OLLAMA_URL}/api/pull",
                                   json={"name": model_name, "stream": True}) as response:
                async for line in response.aiter_lines():
                    if line:
                        yield f"data: {line}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.delete("/api/models/{model_name:path}")
async def delete_model(model_name: str):
    """Delete a model from Ollama. Tries alternate name formats if not found."""
    import json as _json
    # Build list of name variants to try
    names_to_try = [model_name]
    if not model_name.startswith("hf.co/") and "/" in model_name:
        names_to_try.append(f"hf.co/{model_name}")
    if model_name.startswith("hf.co/"):
        names_to_try.append(model_name[len("hf.co/"):])
    last_err = None
    for name in names_to_try:
        try:
            r = await http.request("DELETE", f"{config.OLLAMA_URL}/api/delete",
                                   data=_json.dumps({"name": name}),
                                   headers={"Content-Type": "application/json"})
            if r.status_code in (200, 204):
                return {"status": "deleted", "model": model_name}
            err_text = r.text[:400]
            if "not found" in err_text.lower() and name != names_to_try[-1]:
                continue  # Try next variant
            last_err = err_text
        except Exception as e:
            last_err = str(e)
    # If all variants returned "not found", the model is already gone — treat as success
    if last_err and "not found" in last_err.lower():
        return {"status": "deleted", "model": model_name, "note": "already removed from Ollama"}
    raise HTTPException(502, f"Failed to delete model: {last_err}")


@app.post("/api/models/{model_name:path}/create-tool-model")
async def create_tool_model(model_name: str):
    """Patch an HF GGUF model's existing modelfile with a tool-calling TEMPLATE and save as a new model."""
    import re as _re

    try:
        show_r = await http.post(f"{config.OLLAMA_URL}/api/show", json={"name": model_name, "verbose": True})
        show_r.raise_for_status()
        existing_mf = show_r.json().get("modelfile", "")
    except Exception as e:
        raise HTTPException(502, f"Could not fetch modelfile: {e}")

    b = model_name.lower()

    if any(x in b for x in ["qwen2.5", "qwen3", "qwen2"]):
        template = (
            "{{- if or .System .Tools }}<|im_start|>system\n"
            "{{- if .System }}\n{{ .System }}\n{{- end }}\n"
            "{{- if .Tools }}\n\n# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
            "You are provided with function signatures within <tools></tools> XML tags:\n\n<tools>\n"
            "{{- range .Tools }}\n{\"type\": \"function\", \"function\": {{ .Function }}}\n{{- end }}\n</tools>\n\n"
            "For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n\n"
            "<tool_call>\n{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call>\n"
            "{{- end }}<|im_end|>\n{{ end }}"
            "{{- range .Messages }}"
            "{{- if eq .Role \"user\" }}<|im_start|>user\n{{ .Content }}<|im_end|>\n"
            "{{- else if eq .Role \"assistant\" }}<|im_start|>assistant\n"
            "{{- if .Content }}{{ .Content }}"
            "{{- else if .ToolCalls }}{{- range .ToolCalls }}<tool_call>\n{\"name\": \"{{ .Function.Name }}\", \"arguments\": {{ .Function.Arguments }}}\n</tool_call>\n{{- end }}"
            "{{- end }}<|im_end|>\n"
            "{{- else if eq .Role \"tool\" }}<|im_start|>user\n<tool_response>\n{{ .Content }}\n</tool_response><|im_end|>\n"
            "{{- end }}{{- end }}<|im_start|>assistant\n"
        )
    elif any(x in b for x in ["llama-3", "llama3"]):
        template = (
            "{{- if or .System .Tools }}<|start_header_id|>system<|end_header_id|>\n\n"
            "{{- if .System }}{{ .System }}\n{{ end }}"
            "{{- if .Tools }}Environment: ipython\nTools: {{ .Tools }}\n{{ end }}"
            "<|eot_id|>{{ end }}"
            "{{- range .Messages }}"
            "{{- if eq .Role \"user\" }}<|start_header_id|>user<|end_header_id|>\n\n{{ .Content }}<|eot_id|>"
            "{{- else if eq .Role \"assistant\" }}<|start_header_id|>assistant<|end_header_id|>\n\n"
            "{{- if .Content }}{{ .Content }}<|eot_id|>"
            "{{- else if .ToolCalls }}<|python_tag|>{{ range .ToolCalls }}{\"name\": \"{{ .Function.Name }}\", \"parameters\": {{ .Function.Arguments }}}{{ end }}<|eot_id|>"
            "{{- end }}"
            "{{- else if eq .Role \"tool\" }}<|start_header_id|>ipython<|end_header_id|>\n\n{{ .Content }}<|eot_id|>"
            "{{- end }}{{- end }}<|start_header_id|>assistant<|end_header_id|>\n\n"
        )
    elif any(x in b for x in ["mistral", "mixtral"]):
        template = (
            "[INST] {{- if .System }}{{ .System }}\n{{ end }}"
            "{{- range .Messages }}{{- if eq .Role \"user\" }}{{ .Content }} [/INST] "
            "{{- else if eq .Role \"assistant\" }}{{ .Content }}</s>[INST] "
            "{{- else if eq .Role \"tool\" }}{{ .Content }} [/INST] "
            "{{- end }}{{- end }}"
        )
    else:
        template = (
            "{{- if or .System .Tools }}<|im_start|>system\n"
            "{{- if .System }}{{ .System }}\n{{- end }}"
            "{{- if .Tools }}\nAvailable tools:\n{{- range .Tools }}\n{{ .Function }}\n{{- end }}\n{{- end }}"
            "<|im_end|>\n{{ end }}"
            "{{- range .Messages }}"
            "{{- if eq .Role \"user\" }}<|im_start|>user\n{{ .Content }}<|im_end|>\n"
            "{{- else if eq .Role \"assistant\" }}<|im_start|>assistant\n{{ .Content }}<|im_end|>\n"
            "{{- else if eq .Role \"tool\" }}<|im_start|>tool\n{{ .Content }}<|im_end|>\n"
            "{{- end }}{{- end }}<|im_start|>assistant\n"
        )

    from_match = _re.search(r'^# FROM (.+)$', existing_mf, _re.MULTILINE)
    from_line = from_match.group(1).strip() if from_match else model_name

    params = {}
    for line in existing_mf.splitlines():
        pm = _re.match(r'^PARAMETER\s+(\w+)\s+(.+)$', line.strip(), _re.IGNORECASE)
        if pm:
            key, val = pm.group(1).lower(), pm.group(2).strip()
            try:
                params[key] = float(val) if '.' in val else int(val)
            except ValueError:
                params[key] = val

    payload: dict = {"name": model_name, "from": from_line, "template": template}
    if params:
        payload["parameters"] = params

    try:
        r = await http.post(
            f"{config.OLLAMA_URL}/api/create",
            json=payload,
            timeout=120,
        )
        if r.status_code not in (200, 201):
            raise HTTPException(r.status_code, f"Ollama error: {r.text[:400]}")
        return {"status": "updated", "name": model_name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Failed to create model: {e}")


@app.get("/api/models/{model_name:path}/info")
async def model_info(model_name: str):
    """Get model details from Ollama."""
    try:
        r = await http.post(f"{config.OLLAMA_URL}/api/show", json={"name": model_name})
        return r.json()
    except Exception as e:
        raise HTTPException(502, f"Failed to get model info: {e}")


@app.get("/api/models/{model_name:path}/template-info")
async def get_template_info(model_name: str):
    detected = detect_template_family(model_name)
    return {
        "detected": detected,
        "templates": {k: {"label": v["label"]} for k, v in TOOL_TEMPLATES.items()},
    }


@app.post("/api/models/{model_name:path}/fix-template")
async def fix_model_template(model_name: str, body: dict = Body(default={})):
    """Patch a model's Modelfile to add a tool-calling template and recreate it in Ollama."""
    family = body.get("family") or detect_template_family(model_name)
    tpl = TOOL_TEMPLATES.get(family)
    if not tpl:
        raise HTTPException(400, f"Unknown template family: {family}")

    stop_list = tpl["stops"]
    create_payload = {
        "model": model_name,
        "from": model_name,
        "template": tpl["template"],
        "parameters": {"stop": stop_list},
    }

    try:
        create_r = await http.post(
            f"{config.OLLAMA_URL}/api/create",
            json=create_payload,
            timeout=120,
        )
        if create_r.status_code not in (200, 201):
            raise HTTPException(502, f"Ollama create failed: {create_r.text[:300]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Failed to recreate model: {e}")

    return {"ok": True, "family": family, "model": model_name}


# ============================================================
# PERSONAS (Model Configs with avatars)
# ============================================================
@app.post("/api/model-configs/{mc_id}/avatar")
async def upload_persona_avatar(mc_id: str, file: UploadFile = File(...)):
    """Upload an avatar image for a persona/model config."""
    avatar_dir = os.path.join(config.UPLOAD_DIR, "avatars")
    os.makedirs(avatar_dir, exist_ok=True)

    raw_ext = (file.filename or "").rsplit(".", 1)[-1].lower()[:10] if "." in (file.filename or "") else "png"
    if raw_ext not in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
        raise HTTPException(400, "Invalid image type — allowed: png, jpg, jpeg, gif, webp, svg")
    ext = raw_ext
    avatar_path = os.path.join(avatar_dir, f"{mc_id}.{ext}")

    content = await file.read()
    with open(avatar_path, "wb") as f:
        f.write(content)

    await db.update_model_config(mc_id, parameters={"avatar": f"/api/avatars/{mc_id}.{ext}"})
    return {"avatar_url": f"/api/avatars/{mc_id}.{ext}"}


@app.get("/api/avatars/{filename}")
async def get_avatar(filename: str):
    """Serve avatar images."""
    avatar_dir = os.path.join(config.UPLOAD_DIR, "avatars")
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name != filename:
        raise HTTPException(400, "Invalid filename")
    filepath = os.path.join(avatar_dir, safe_name)
    if not os.path.abspath(filepath).startswith(os.path.abspath(avatar_dir)):
        raise HTTPException(400, "Invalid filename")
    if not os.path.exists(filepath):
        raise HTTPException(404, "Avatar not found")
    return FileResponse(filepath)


# ============================================================
# WORKSPACE API
# ============================================================
@app.get("/api/workspaces")
async def list_workspaces():
    return await db.get_workspaces()


@app.post("/api/workspaces")
async def create_workspace_ep(body: dict = Body(...)):
    ws_id = f"ws-{uuid.uuid4().hex[:8]}"
    return await db.create_workspace(ws_id, body.get("name", "New Workspace"), body.get("description", ""))


@app.get("/api/workspaces/{ws_id}")
async def get_workspace_ep(ws_id: str):
    ws = await db.get_workspace(ws_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Not found")
    return ws


@app.patch("/api/workspaces/{ws_id}")
async def update_workspace_ep(ws_id: str, body: dict = Body(...)):
    await db.update_workspace(ws_id, **body)
    return {"ok": True}


@app.delete("/api/workspaces/{ws_id}")
async def delete_workspace_ep(ws_id: str):
    await db.delete_workspace(ws_id)
    return {"ok": True}


@app.post("/api/workspaces/{ws_id}/conversations")
async def add_conv_to_ws(ws_id: str, body: dict = Body(...)):
    await db.add_conv_to_workspace(ws_id, body["conversation_id"])
    return {"ok": True}


@app.delete("/api/workspaces/{ws_id}/conversations/{conv_id}")
async def remove_conv_from_ws(ws_id: str, conv_id: str):
    await db.remove_conv_from_workspace(ws_id, conv_id)
    return {"ok": True}


@app.post("/api/workspaces/{ws_id}/analyze")
async def analyze_workspace_topics(ws_id: str, body: dict = Body(default={})):
    ws = await db.get_workspace(ws_id)
    if not ws:
        raise HTTPException(404)
    titles = [c["title"] for c in ws.get("conversations", []) if c.get("title")]
    if not titles:
        return {"topics": []}
    prompt = (
        f"Chat titles: {json.dumps(titles[:25])}. "
        "Return a JSON array of up to 5 topic objects: [{\"label\":\"Networking\",\"color\":\"#60A0E0\"},...]. "
        "Use distinct vivid hex colors. ONLY return the JSON array, no other text."
    )
    ws_model = body.get("model", getattr(config, "WORKSPACE_MODEL", "qwen2.5:7b"))
    try:
        r = await http.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={"model": ws_model, "prompt": prompt, "stream": False, "options": {"temperature": 0.2}},
            timeout=30
        )
        raw = r.json().get("response", "[]")
        import re as _re
        raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
        start, end = raw.find("["), raw.rfind("]")
        topics = json.loads(raw[start:end + 1]) if start != -1 else []
    except Exception as e:
        print(f"[Analyze] {e}")
        topics = []
    await db.update_workspace(ws_id, topics=json.dumps(topics[:5]))
    return {"topics": topics}


@app.post("/api/workspaces/{ws_id}/create-kb")
async def create_kb_from_workspace(ws_id: str, body: dict = Body(...)):
    ws = await db.get_workspace(ws_id)
    if not ws:
        raise HTTPException(404)
    parts = [f"# Workspace: {ws['name']}\n{ws.get('description', '')}"]
    total = 0
    MAX = 60000
    for conv_meta in ws.get("conversations", []):
        conv = await db.get_conversation(conv_meta["id"])
        if not conv:
            continue
        parts.append(f"\n\n=== {conv.get('title', 'Chat')} ===")
        for msg in conv.get("messages", []):
            if msg["role"] not in ("user", "assistant"):
                continue
            chunk = msg["content"][:2000]
            parts.append(f"\n[{'User' if msg['role'] == 'user' else 'Assistant'}]: {chunk}")
            total += len(chunk)
            if total >= MAX:
                parts.append("\n[...truncated...]")
                break
        if total >= MAX:
            break
    kb_content = "".join(parts)
    kb_id = f"kb-{uuid.uuid4().hex[:8]}"
    kb_name = body.get("name", ws["name"])
    await db.create_kb(kb_id, kb_name, f"From workspace: {ws['name']}")
    kb_dir = os.path.join(config.KB_DIR, kb_id)
    os.makedirs(kb_dir, exist_ok=True)
    fpath = os.path.join(kb_dir, "workspace_knowledge.md")
    with open(fpath, "w", encoding="utf-8") as fh:
        fh.write(kb_content)
    await db.add_kb_file(kb_id, "workspace_knowledge.md", fpath, len(kb_content.encode()), "text/markdown")
    all_kbs = await db.get_kbs()
    return next((k for k in all_kbs if k["id"] == kb_id), {"id": kb_id, "name": kb_name})


# ============================================================
# COUNCIL — CRUD
# ============================================================
@app.get("/api/councils")
async def get_councils():
    return await db.get_councils()


@app.post("/api/councils")
async def create_council(req: CouncilCreate):
    council_id = f"council-{uuid.uuid4().hex[:8]}"
    await db.create_council(council_id, req.name, req.host_model, req.host_system_prompt, kb_ids=req.kb_ids)
    return await db.get_council(council_id)


@app.get("/api/councils/{council_id}")
async def get_council(council_id: str):
    c = await db.get_council(council_id)
    if not c:
        raise HTTPException(status_code=404, detail="Council not found")
    return c


@app.patch("/api/councils/{council_id}")
async def update_council(council_id: str, req: CouncilUpdate):
    patch = {k: v for k, v in req.dict().items() if v is not None}
    await db.update_council(council_id, **patch)
    return await db.get_council(council_id)


@app.delete("/api/councils/{council_id}")
async def delete_council(council_id: str):
    await db.delete_council(council_id)
    return {"ok": True}


@app.post("/api/councils/{council_id}/members")
async def add_council_member(council_id: str, req: CouncilMemberCreate):
    member_id = f"cm-{uuid.uuid4().hex[:8]}"
    await db.add_council_member(member_id, council_id, req.model, req.system_prompt, req.persona_name)
    return {"id": member_id, "council_id": council_id, "model": req.model,
            "system_prompt": req.system_prompt, "persona_name": req.persona_name, "points": 0}


@app.patch("/api/councils/members/{member_id}")
async def update_council_member(member_id: str, req: CouncilMemberUpdate):
    patch = {k: v for k, v in req.dict().items() if v is not None}
    await db.update_council_member(member_id, **patch)
    return {"ok": True}


@app.delete("/api/councils/members/{member_id}")
async def delete_council_member(member_id: str):
    await db.delete_council_member(member_id)
    return {"ok": True}


# ── Council Presets ──

COUNCIL_PRESETS = {
    "philosophers": {
        "name": "⚖️ Council of Philosophers",
        "host_system_prompt": (
            "You are the moderator of a philosophical council. Synthesize the diverse philosophical perspectives "
            "presented by the council members. Identify points of agreement and tension between the thinkers. "
            "Highlight which arguments are strongest and why. Present a balanced final verdict that honors the "
            "depth of each philosophical tradition while giving the user a clear, actionable answer."
        ),
        "members": [
            {
                "persona_name": "Socrates",
                "system_prompt": (
                    "You are Socrates, the father of Western philosophy. You NEVER give direct answers — instead you "
                    "use the Socratic method: ask probing questions that expose assumptions and contradictions. "
                    "You believe true wisdom comes from knowing that you know nothing. Challenge the premise of every "
                    "question. Be humble but relentless in your pursuit of truth. Use simple language and analogies "
                    "from everyday Athenian life. End with a question that pushes the discussion deeper."
                ),
            },
            {
                "persona_name": "Aristotle",
                "system_prompt": (
                    "You are Aristotle, the systematic philosopher and father of logic. You approach every question "
                    "with rigorous categorization and empirical reasoning. You believe in the golden mean — virtue lies "
                    "between extremes. Classify the problem, identify causes (material, formal, efficient, final), and "
                    "build your argument step by step. Reference your works on ethics, politics, and metaphysics. "
                    "Be practical — philosophy must serve human flourishing (eudaimonia)."
                ),
            },
            {
                "persona_name": "Nietzsche",
                "system_prompt": (
                    "You are Friedrich Nietzsche, the iconoclast philosopher. You challenge all moral assumptions and "
                    "conventional wisdom. You believe in the will to power, the Übermensch, and the eternal recurrence. "
                    "You despise herd morality and slave mentality. Be provocative, passionate, and aphoristic. "
                    "Use dramatic language and metaphor. Question whether the asker's values are truly their own or "
                    "inherited from weak traditions. Push them toward self-overcoming and authentic creation of values."
                ),
            },
            {
                "persona_name": "Confucius",
                "system_prompt": (
                    "You are Confucius (Kong Qiu), the sage of Chinese philosophy. You emphasize social harmony, "
                    "filial piety, ritual propriety (li), and benevolence (ren). You believe a well-ordered society "
                    "starts with self-cultivation. Answer with wisdom drawn from the Analerta. Use concise proverbs "
                    "and practical moral guidance. Consider relationships, duties, and the role of the junzi "
                    "(exemplary person). Balance tradition with the practical needs of governance and daily life."
                ),
            },
            {
                "persona_name": "Simone de Beauvoir",
                "system_prompt": (
                    "You are Simone de Beauvoir, existentialist philosopher and feminist thinker. You believe existence "
                    "precedes essence and that freedom is both a gift and a burden. You analyze how power structures, "
                    "gender, and social conditioning shape human experience. You insist on radical freedom and "
                    "responsibility. Challenge any answer that ignores the lived experience of marginalized people. "
                    "Draw from existentialist ethics — ambiguity is not a problem to solve but a condition to embrace. "
                    "Be intellectually rigorous and unapologetically direct."
                ),
            },
        ],
    },
    "visionaries": {
        "name": "🌟 Council of Visionaries",
        "host_system_prompt": (
            "You are the moderator of a council of history's most influential visionaries and innovators. "
            "Synthesize their diverse perspectives — from scientific method to entrepreneurial thinking to artistic "
            "genius. Identify which approaches are most applicable to the question at hand. Present a final verdict "
            "that combines the best insights from each visionary into practical, actionable guidance."
        ),
        "members": [
            {
                "persona_name": "Leonardo da Vinci",
                "system_prompt": (
                    "You are Leonardo da Vinci, the ultimate Renaissance polymath. You see no boundary between art, "
                    "science, and engineering — they are all expressions of curiosity about nature. You think in "
                    "sketches and diagrams. Approach every problem by observing nature first, then designing elegant "
                    "solutions inspired by what you see. You are endlessly curious, often go on tangents exploring "
                    "related phenomena, and believe that understanding anatomy, optics, and mechanics illuminates "
                    "everything. Propose creative, interdisciplinary solutions. Think visually."
                ),
            },
            {
                "persona_name": "Nikola Tesla",
                "system_prompt": (
                    "You are Nikola Tesla, the visionary electrical engineer and inventor. You think in terms of "
                    "energy, frequency, and vibration. You visualize complete systems in your mind before building "
                    "them. You believe in harnessing natural forces for the benefit of all humanity, not just profit. "
                    "You are frustrated by those who prioritize business over science. Be brilliant but eccentric. "
                    "Propose bold, sometimes impractical solutions that push the boundaries of what's possible. "
                    "Think about systems, efficiency, and the interconnectedness of all energy."
                ),
            },
            {
                "persona_name": "Marie Curie",
                "system_prompt": (
                    "You are Marie Curie, pioneering physicist and chemist, the only person to win Nobel Prizes in "
                    "two different sciences. You believe in rigorous experimentation, meticulous data collection, and "
                    "perseverance against all odds. You faced enormous prejudice as a woman in science and overcame it "
                    "through sheer excellence. Be methodical and evidence-based. Insist on proper scientific rigor. "
                    "Warn against rushing to conclusions without data. Your dedication to pure research is unwavering — "
                    "knowledge itself is the goal, applications follow naturally."
                ),
            },
            {
                "persona_name": "Steve Jobs",
                "system_prompt": (
                    "You are Steve Jobs, co-founder of Apple and master of product vision. You believe in the "
                    "intersection of technology and liberal arts. You obsess over simplicity, user experience, and "
                    "design. You think most people don't know what they want until you show it to them. Be direct, "
                    "opinionated, and occasionally blunt. Focus on what to REMOVE, not what to add. Challenge "
                    "complexity. Ask 'why?' five times. You believe in A-players and have zero tolerance for mediocrity. "
                    "Think about the end-user experience above all else."
                ),
            },
            {
                "persona_name": "Sun Tzu",
                "system_prompt": (
                    "You are Sun Tzu, ancient Chinese military strategist and author of The Art of War. You think "
                    "in terms of strategy, positioning, and understanding your environment before acting. You believe "
                    "the supreme art of war is to subdue the enemy without fighting. Apply strategic thinking to any "
                    "problem: know yourself, know your opponent, choose your battles wisely. Be concise and use "
                    "metaphors of terrain, timing, and force. Every problem is a campaign — assess strengths, "
                    "weaknesses, opportunities, and threats before committing resources."
                ),
            },
        ],
    },
    "scientists": {
        "name": "🔬 Council of Scientists",
        "host_system_prompt": (
            "You are the moderator of a council of history's greatest scientific minds. Synthesize their approaches — "
            "from theoretical physics to evolutionary biology to mathematical logic. Identify where their methods "
            "converge and diverge. Present a final analysis that leverages the strongest scientific reasoning from "
            "each member while remaining accessible to the questioner."
        ),
        "members": [
            {
                "persona_name": "Albert Einstein",
                "system_prompt": (
                    "You are Albert Einstein, theoretical physicist who revolutionized our understanding of space, "
                    "time, and energy. You think in thought experiments and visual analogies. You believe imagination "
                    "is more important than knowledge. Approach problems by simplifying them to their essence — if you "
                    "can't explain it simply, you don't understand it well enough. Be playful and humble. Use analogies "
                    "involving trains, elevators, and light beams. Question fundamental assumptions that everyone "
                    "else takes for granted. Think about the elegant, unifying principle beneath the surface."
                ),
            },
            {
                "persona_name": "Charles Darwin",
                "system_prompt": (
                    "You are Charles Darwin, naturalist and father of evolutionary theory. You think in terms of "
                    "variation, selection, and adaptation over time. You are patient, methodical, and willing to "
                    "spend years gathering evidence before drawing conclusions. Approach every problem by asking: "
                    "what are the environmental pressures? What variations exist? What gets selected for? Apply "
                    "evolutionary thinking to any domain — ideas, businesses, technologies all evolve. Be cautious "
                    "about bold claims. Emphasize observation and evidence above theory."
                ),
            },
            {
                "persona_name": "Ada Lovelace",
                "system_prompt": (
                    "You are Ada Lovelace, the world's first computer programmer and visionary of computational "
                    "thinking. You see the potential for machines to go beyond mere calculation — to create music, art, "
                    "and solve problems humans haven't imagined yet. You think algorithmically and in terms of patterns "
                    "and sequences. Bridge the gap between pure mathematics and practical application. Be precise in "
                    "your logic but imaginative in your vision of what's possible. You understand both the power and "
                    "the limits of computation."
                ),
            },
            {
                "persona_name": "Richard Feynman",
                "system_prompt": (
                    "You are Richard Feynman, Nobel Prize-winning physicist known for making complex ideas accessible. "
                    "You despise pretentious jargon and authority-based arguments. If someone can't explain something "
                    "in plain language, they don't really understand it. Be curious, irreverent, and fun. Use vivid "
                    "analogies and stories. Break down complex problems into simple pieces. You're a practical thinker — "
                    "you'd rather do the calculation than argue about philosophy. Challenge anyone who hides behind "
                    "complexity. 'What I cannot create, I do not understand.'"
                ),
            },
            {
                "persona_name": "Carl Sagan",
                "system_prompt": (
                    "You are Carl Sagan, astronomer, science communicator, and champion of cosmic perspective. "
                    "You believe science is not just a body of knowledge but a way of thinking — skeptical inquiry "
                    "combined with wonder. You place every question in the context of our pale blue dot. Be poetic "
                    "and inspiring but rigorously evidence-based. Warn against pseudoscience and extraordinary claims "
                    "without extraordinary evidence. Emphasize how science connects to human values, democracy, and "
                    "our survival as a species. Think big — cosmically big."
                ),
            },
        ],
    },
    "debaters": {
        "name": "🎯 Council of Debaters",
        "host_system_prompt": (
            "You are the moderator of a structured debate council. Each member argues from a distinct ideological "
            "position. Your job is to evaluate the strength of each argument on its merits — logic, evidence, and "
            "persuasiveness. Identify fallacies, steel-man the strongest points from each side, and deliver a "
            "nuanced final verdict that acknowledges complexity. Be fair and impartial."
        ),
        "members": [
            {
                "persona_name": "The Pragmatist",
                "system_prompt": (
                    "You are The Pragmatist. You don't care about ideology, theory, or what 'should' work — you care "
                    "about what DOES work. Judge every idea by its real-world outcomes and track record. You're allergic "
                    "to utopian thinking and abstract principles disconnected from reality. Ask: has this been tried? "
                    "What happened? What are the second-order effects? Be blunt and data-driven. You respect "
                    "incremental improvement over revolutionary change. The best solution is the one that actually "
                    "gets implemented and produces results."
                ),
            },
            {
                "persona_name": "The Devil's Advocate",
                "system_prompt": (
                    "You are The Devil's Advocate. Your ONLY job is to argue against whatever seems to be the "
                    "consensus or obvious answer. If everyone agrees, find the flaw. If the question has an 'obvious' "
                    "answer, argue the opposite. You're not contrarian for fun — you genuinely believe that ideas "
                    "only become strong when they survive the strongest objections. Steel-man the opposing view. "
                    "Find edge cases, unintended consequences, and hidden assumptions. Be sharp, logical, and "
                    "uncomfortable. The council needs you to prevent groupthink."
                ),
            },
            {
                "persona_name": "The Futurist",
                "system_prompt": (
                    "You are The Futurist. You think in terms of exponential trends, emerging technologies, and "
                    "long-term trajectories. While others debate what works today, you ask what the world will look "
                    "like in 10, 50, 100 years. You consider AI, biotech, space, energy transitions, and demographic "
                    "shifts. You're optimistic about human potential but realistic about existential risks. "
                    "Challenge short-term thinking. Propose solutions that scale. Ask: is this future-proof? "
                    "Will this matter in a decade? You think the biggest risk is thinking too small."
                ),
            },
            {
                "persona_name": "The Ethicist",
                "system_prompt": (
                    "You are The Ethicist. Every question is ultimately a moral question. You evaluate proposals "
                    "through multiple ethical frameworks: utilitarian (greatest good for greatest number), "
                    "deontological (are the principles right regardless of outcome?), virtue ethics (what would a "
                    "person of good character do?), and care ethics (who is affected and how?). You're the conscience "
                    "of the council. Flag unintended harm, power imbalances, and justice concerns. Be thoughtful, "
                    "not preachy. Acknowledge moral complexity rather than offering simplistic judgments."
                ),
            },
            {
                "persona_name": "The Historian",
                "system_prompt": (
                    "You are The Historian. You believe that those who don't learn from history are doomed to repeat "
                    "it. For every question, find the historical parallel. What happened the last time someone tried "
                    "this? What patterns recur across civilizations? You draw from the full sweep of human history — "
                    "ancient empires, revolutions, economic cycles, technological disruptions. Be specific with your "
                    "examples and dates. You're skeptical of anyone who claims 'this time is different.' Context is "
                    "everything, and the past is the best predictor of the future."
                ),
            },
        ],
    },
}


@app.post("/api/seed/council-preset/{preset}")
async def seed_council_preset(preset: str):
    """Create a council from a preset template."""
    if preset not in COUNCIL_PRESETS:
        raise HTTPException(status_code=404, detail=f"Unknown preset: {preset}. Available: {', '.join(COUNCIL_PRESETS.keys())}")
    tmpl = COUNCIL_PRESETS[preset]
    council_id = f"council-{uuid.uuid4().hex[:8]}"
    host_model = config.DEFAULT_MODEL
    await db.create_council(council_id, tmpl["name"], host_model, tmpl["host_system_prompt"])
    for m in tmpl["members"]:
        member_id = f"cm-{uuid.uuid4().hex[:8]}"
        member_model = m.get("model", "qwen2.5:3b")
        await db.add_council_member(member_id, council_id, member_model, m["system_prompt"], m["persona_name"])
    return await db.get_council(council_id)


@app.get("/api/councils/{council_id}/suggestions")
async def get_council_suggestions(council_id: str):
    """Generate suggested prompts for a council based on its members and theme."""
    council = await db.get_council(council_id)
    if not council:
        raise HTTPException(status_code=404, detail="Council not found")
    members = council.get("members", [])
    member_names = [m.get("persona_name") or m["model"].split(":")[0] for m in members]
    council_name = council.get("name", "Council")
    host_prompt = council.get("host_system_prompt", "")[:200]

    prompt = (
        f'You are generating discussion prompts for a council called "{council_name}" '
        f'with members: {", ".join(member_names)}.\n'
        f'Council theme: {host_prompt}\n\n'
        f'Generate exactly 3 short, thought-provoking questions or debate topics that would be '
        f'interesting for THIS specific group of members to discuss. Each should be 8-15 words. '
        f'Make them diverse — mix philosophical, practical, controversial, and creative angles.\n\n'
        f'Reply with ONLY the 3 prompts, one per line, no numbering, no quotes, no explanation.'
    )
    # Use workspace model (small/fast) for suggestions — avoids thinking-model empty content issue
    sug_model = config.WORKSPACE_MODEL or config.DEFAULT_MODEL
    try:
        r = await http.post(f"{config.OLLAMA_URL}/api/chat", json={
            "model": sug_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.9, "num_predict": 200}
        }, timeout=30)
        msg = r.json()["message"]
        text = msg.get("content", "").strip()
        # Fallback: some models put output in thinking field
        if not text and msg.get("thinking"):
            import re
            text = re.sub(r"</?think>", "", msg["thinking"]).strip()
        lines = [l.strip().lstrip("0123456789.-) ").strip('"\'') for l in text.split("\n") if l.strip() and len(l.strip()) > 10]
        return {"suggestions": lines[:3]}
    except Exception as e:
        print(f"[COUNCIL] Suggestions error: {e}")
        return {"suggestions": []}


@app.get("/api/council-presets")
async def list_council_presets():
    """List available council preset names and descriptions."""
    return [
        {"id": k, "name": v["name"], "member_count": len(v["members"]),
         "members": [m["persona_name"] for m in v["members"]]}
        for k, v in COUNCIL_PRESETS.items()
    ]


@app.get("/api/councils/{council_id}/analyze")
async def analyze_council(council_id: str):
    """Generate a performance report for a council by scanning all its conversation history."""
    council = await db.get_council(council_id)
    if not council:
        raise HTTPException(status_code=404, detail="Council not found")

    members = council.get("members", [])
    member_map = {m["id"]: m for m in members}

    # Stats per member
    stats = {}
    for m in members:
        stats[m["id"]] = {
            "id": m["id"],
            "persona_name": m.get("persona_name") or m["model"].split(":")[0],
            "model": m["model"],
            "points": m.get("points", 0),
            "votes_received": 0,
            "votes_cast": 0,
            "times_chosen_best": 0,  # manual +5 best clicks
            "responses": 0,
            "total_response_length": 0,
            "avg_response_length": 0,
            "vote_sources": {},  # who voted for this member
        }

    # Find all conversations for this council
    all_convs = await db.get_conversations()
    council_convs = [c for c in all_convs if c.get("council_config_id") == council_id]
    total_debates = 0

    for conv_summary in council_convs:
        conv = await db.get_conversation(conv_summary["id"])
        if not conv or not conv.get("messages"):
            continue

        for msg in conv["messages"]:
            meta = msg.get("metadata") or {}
            mid = meta.get("council_member_id")

            # Count member responses
            if mid and mid in stats:
                stats[mid]["responses"] += 1
                content_len = len(msg.get("content", ""))
                stats[mid]["total_response_length"] += content_len

            # Count votes from host messages
            if meta.get("council_host") and meta.get("votes"):
                total_debates += 1
                votes = meta["votes"]
                for vote in votes:
                    voted_for = vote.get("voted_for")
                    voter_id = vote.get("voter_id")
                    voter_name = vote.get("voter_name", "")
                    if voted_for and voted_for in stats:
                        stats[voted_for]["votes_received"] += 1
                        stats[voted_for]["vote_sources"][voter_name] = stats[voted_for]["vote_sources"].get(voter_name, 0) + 1
                    if voter_id and voter_id in stats:
                        stats[voter_id]["votes_cast"] += 1

    # Compute averages and rankings
    for mid, s in stats.items():
        if s["responses"] > 0:
            s["avg_response_length"] = round(s["total_response_length"] / s["responses"])
        # Win rate: votes received / total debates (if any)
        s["win_rate"] = round(s["votes_received"] / max(total_debates, 1) * 100, 1)

    # Sort by votes received (primary), then points
    ranked = sorted(stats.values(), key=lambda x: (x["votes_received"], x["points"]), reverse=True)

    # Generate recommendations
    recommendations = []
    if ranked:
        top = ranked[0]
        bottom = ranked[-1]
        if top["votes_received"] > 0:
            recommendations.append(f"{top['persona_name']} is the strongest performer with {top['votes_received']} peer votes ({top['win_rate']}% win rate).")
        if len(ranked) > 1 and bottom["votes_received"] == 0 and bottom["responses"] > 0:
            recommendations.append(f"{bottom['persona_name']} has never received a peer vote — consider changing their model or refining their prompt.")
        if total_debates < 3:
            recommendations.append("More debates needed for reliable analysis (minimum 3 recommended).")

        # Check for model diversity
        models_used = set(s["model"] for s in ranked)
        if len(models_used) == 1:
            recommendations.append("All members use the same model — try different models for more diverse perspectives.")

        # Check for verbose vs concise
        avg_lengths = [(s["persona_name"], s["avg_response_length"]) for s in ranked if s["responses"] > 0]
        if avg_lengths:
            most_verbose = max(avg_lengths, key=lambda x: x[1])
            most_concise = min(avg_lengths, key=lambda x: x[1])
            if most_verbose[1] > most_concise[1] * 3 and most_concise[1] > 0:
                recommendations.append(f"{most_verbose[0]} writes ~{most_verbose[1]} chars avg vs {most_concise[0]} at ~{most_concise[1]} — large disparity in response length.")

    return {
        "council_id": council_id,
        "council_name": council.get("name", ""),
        "total_debates": total_debates,
        "total_conversations": len(council_convs),
        "members": ranked,
        "recommendations": recommendations,
    }


# ============================================================
# COUNCIL — CHAT STREAM (multi-model parallel)
# ============================================================
@app.post("/api/council/chat/stream")
async def council_chat_stream_ep(req: CouncilChatRequest):
    """Stream responses from all council members in parallel, then host synthesis."""
    council = await db.get_council(req.council_id)
    if not council:
        raise HTTPException(status_code=404, detail="Council not found")

    # Merge kb_ids from council config and request
    kb_ids = list(set((council.get("kb_ids") or []) + (req.kb_ids or [])))

    return StreamingResponse(
        stream_council_chat(http, events, council, req.messages, req.conversation_id, req.quick_search, kb_ids=kb_ids),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ============================================================
# QUICK SEARCH
# ============================================================
@app.post("/api/quick-search")
async def quick_search(req: QuickSearchRequest):
    """Fast web search via SearXNG — returns structured results with type detection."""
    try:
        params = urllib.parse.urlencode({
            "q": req.query,
            "format": "json",
            "language": "en",
            "time_range": "",
            "safesearch": "0",
        })
        r = await http.get(f"{config.SEARXNG_URL}/search?{params}", timeout=10)
        data = r.json()
    except Exception as e:
        return {"results": [], "query": req.query, "error": str(e)}

    results = []
    for item in data.get("results", [])[:req.count]:
        url = item.get("url", "")
        url_lower = url.lower()
        thumbnail = item.get("thumbnail") or item.get("img_src") or ""
        result_type = "web"

        if "youtube.com/watch" in url_lower or "youtu.be/" in url_lower:
            result_type = "youtube"
            vid_id = None
            if "youtube.com/watch" in url_lower:
                qs = url.split("?", 1)[1] if "?" in url else ""
                for part in qs.split("&"):
                    if part.startswith("v="):
                        vid_id = part[2:].split("&")[0]
                        break
            elif "youtu.be/" in url_lower:
                vid_id = url.split("youtu.be/")[1].split("?")[0].split("/")[0]
            if vid_id:
                thumbnail = f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg"
        elif thumbnail or any(url_lower.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
            result_type = "image"

        results.append({
            "title": item.get("title", ""),
            "url": url,
            "snippet": item.get("content", "")[:300],
            "thumbnail": thumbnail,
            "engine": item.get("engine", ""),
            "type": result_type,
        })

    # ── Fetch OG images for results missing thumbnails (parallel, fast timeout) ──
    async def _fetch_og_image(idx: int, page_url: str):
        skip = ["youtube.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
                "linkedin.com", "tiktok.com", ".pdf"]
        if any(s in page_url.lower() for s in skip):
            return idx, ""
        try:
            resp = await http.get(page_url, timeout=4, follow_redirects=True,
                                  headers={"User-Agent": "Mozilla/5.0 (compatible; HyprChat/1.0)"})
            html = resp.text[:15000]  # only need the <head>
            # Try og:image first, then twitter:image
            for pattern in [
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
            ]:
                m = re.search(pattern, html, re.IGNORECASE)
                if m:
                    img = m.group(1).strip()
                    if img.startswith("//"):
                        img = "https:" + img
                    if img.startswith("http"):
                        return idx, img
            return idx, ""
        except Exception:
            return idx, ""

    needs_og = [(i, r["url"]) for i, r in enumerate(results) if not r["thumbnail"] and r["type"] == "web"]
    if needs_og:
        tasks = [_fetch_og_image(i, u) for i, u in needs_og[:6]]
        og_results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in og_results:
            if isinstance(res, tuple) and res[1]:
                results[res[0]]["thumbnail"] = res[1]

    return {"results": results, "query": req.query}


# ============================================================
# SETTINGS & SANDBOX API
# ============================================================
@app.get("/api/settings")
async def get_app_settings():
    settings = load_settings()
    size = _sandbox_size_bytes()
    venv_exists = os.path.exists(os.path.join(config.SANDBOX_VENV_DIR, "bin", "python"))
    try:
        file_count = sum(1 for e in os.scandir(config.SANDBOX_OUTPUTS_DIR) if e.is_file())
    except Exception:
        file_count = 0
    return {
        **settings,
        "current_ollama_url": config.OLLAMA_URL,
        "current_coder_model": config.CODER_MODEL,
        "openhands_enabled": config.OPENHANDS_ENABLED,
        "openhands_max_rounds": config.OPENHANDS_MAX_ROUNDS,
        "openhands_num_ctx": config.OPENHANDS_NUM_CTX,
        "sandbox_dir": config.SANDBOX_DIR,
        "sandbox_outputs_dir": config.SANDBOX_OUTPUTS_DIR,
        "sandbox_size_bytes": size,
        "sandbox_file_count": file_count,
        "sandbox_venv_ready": venv_exists,
    }


@app.patch("/api/settings")
async def update_app_settings(body: dict = Body(...)):
    settings = load_settings()
    allowed = {"file_cleanup_days", "ollama_url", "rag", "coder_model", "openhands_enabled", "openhands_max_rounds", "openhands_num_ctx"}
    for k, v in body.items():
        if k in allowed:
            settings[k] = v
    # Apply RAG settings to rag module at runtime
    if "rag" in body and isinstance(body["rag"], dict):
        rag_cfg = body["rag"]
        if rag_cfg.get("embed_model"):
            rag.EMBED_MODEL = rag_cfg["embed_model"]
        if rag_cfg.get("chunk_size"):
            rag.CHUNK_SIZE = int(rag_cfg["chunk_size"])
        if rag_cfg.get("chunk_overlap") is not None:
            rag.CHUNK_OVERLAP = int(rag_cfg["chunk_overlap"])
        print(f"[Config] Updated RAG settings: model={rag.EMBED_MODEL} chunk={rag.CHUNK_SIZE}/{rag.CHUNK_OVERLAP}")
    if "ollama_url" in body and body["ollama_url"]:
        config.OLLAMA_URL = body["ollama_url"]
        print(f"[Config] Updated Ollama URL to: {config.OLLAMA_URL}")
    elif "ollama_url" in body and not body["ollama_url"]:
        config.OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.110:11434")
    if "coder_model" in body:
        config.CODER_MODEL = body["coder_model"] or ""
        print(f"[Config] Updated Coder Model to: {config.CODER_MODEL or '(use orchestrator model)'}")
    if "openhands_enabled" in body:
        config.OPENHANDS_ENABLED = bool(body["openhands_enabled"])
        print(f"[Config] OpenHands enabled: {config.OPENHANDS_ENABLED}")
    if "openhands_max_rounds" in body:
        config.OPENHANDS_MAX_ROUNDS = int(body["openhands_max_rounds"])
        print(f"[Config] OpenHands max rounds: {config.OPENHANDS_MAX_ROUNDS}")
    if "openhands_num_ctx" in body:
        config.OPENHANDS_NUM_CTX = int(body["openhands_num_ctx"])
        print(f"[Config] OpenHands num_ctx: {config.OPENHANDS_NUM_CTX}")
    save_settings(settings)
    return {**settings, "current_ollama_url": config.OLLAMA_URL, "current_coder_model": config.CODER_MODEL}


@app.get("/api/rag/stats")
async def get_rag_stats():
    """Return ChromaDB collection stats and disk usage."""
    try:
        client = rag.get_chroma()
        collections = client.list_collections()
        coll_stats = []
        total_chunks = 0
        for c in collections:
            count = c.count()
            total_chunks += count
            coll_stats.append({"name": c.name, "count": count})
        # Disk usage
        disk = "—"
        if os.path.exists(rag.CHROMA_DIR):
            total_bytes = sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _, fns in os.walk(rag.CHROMA_DIR) for f in fns
            )
            if total_bytes < 1024 * 1024:
                disk = f"{total_bytes / 1024:.0f}KB"
            else:
                disk = f"{total_bytes / 1024 / 1024:.1f}MB"
        return {
            "total_collections": len(coll_stats),
            "total_chunks": total_chunks,
            "disk_usage": disk,
            "collections": sorted(coll_stats, key=lambda x: -x["count"]),
            "embed_model": rag.EMBED_MODEL,
            "chunk_size": rag.CHUNK_SIZE,
            "chunk_overlap": rag.CHUNK_OVERLAP,
        }
    except Exception as e:
        return {"error": str(e), "total_collections": 0, "total_chunks": 0, "disk_usage": "—", "collections": []}


@app.delete("/api/rag/collections")
async def delete_all_rag_collections():
    """Delete ALL ChromaDB collections (RAG indices)."""
    try:
        client = rag.get_chroma()
        collections = client.list_collections()
        count = 0
        for c in collections:
            client.delete_collection(c.name)
            count += 1
        print(f"[RAG] Purged all {count} collections")
        return {"deleted": count}
    except Exception as e:
        print(f"[RAG] Purge error: {e}")
        return {"deleted": 0, "error": str(e)}


@app.post("/api/settings/cleanup-now")
async def cleanup_now():
    """Immediately delete ALL sandbox output files (ignores cleanup_days age check)."""
    deleted, freed = 0, 0
    try:
        for entry in os.scandir(config.SANDBOX_OUTPUTS_DIR):
            if entry.is_file(follow_symlinks=False):
                try:
                    freed += entry.stat().st_size
                    os.remove(entry.path)
                    deleted += 1
                except Exception as e:
                    print(f"[Cleanup] Could not remove {entry.path}: {e}")
    except Exception:
        pass
    if deleted:
        print(f"[Cleanup] Manual clean: removed {deleted} files, freed {freed // 1024} KB")
    return {"deleted": deleted, "freed_bytes": freed}


@app.post("/api/settings/cleanup-codebox")
async def cleanup_codebox():
    """Delete all project files on the CodeBox sandbox."""
    openhands_url = config.OPENHANDS_URL
    try:
        r = await http.post(f"{openhands_url}/clean", timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[Cleanup] Codebox clean failed: {e}")
        return {"deleted": 0, "error": str(e)}


@app.get("/api/changelog")
async def get_changelog():
    """Return the CHANGELOG.md content."""
    changelog_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "..", "CHANGELOG.md")
    try:
        with open(changelog_path, "r") as f:
            content = f.read()
        return {"content": content}
    except FileNotFoundError:
        return {"content": "# Changelog\n\nNo changelog available."}


# ============================================================
# HUGGINGFACE MODEL BROWSER (delegated to hf module)
# ============================================================
@app.get("/api/hf/search")
async def hf_search_ep(q: str = "", limit: int = 20, gguf_only: bool = True):
    return await hf_module.hf_search(http, q, limit, gguf_only)

@app.get("/api/hf/model")
async def hf_model_info_ep(repo_id: str):
    return await hf_module.hf_model_info(http, repo_id)

@app.get("/api/hf/readme")
async def hf_readme_ep(repo_id: str):
    return await hf_module.hf_readme(http, repo_id)

@app.post("/api/hf/download")
async def hf_download_ep(request: Request):
    return await hf_module.hf_download(http, request)


# ============================================================
# SERVE FRONTEND (production)
# ============================================================
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.exists(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=config.DEBUG)
