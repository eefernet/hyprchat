"""
HyprChat — FastAPI Backend
Full-stack backend with Ollama streaming, Codebox execution,
SearXNG research, n8n webhook proxy, and SSE status events.
"""
import asyncio
import io
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
import zipfile
from datetime import datetime
from typing import Optional, Any
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Query, Body
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
import database as db
from council import stream_council_chat
from events import EventBus
import quick_search as qs_module
from agents.chat import chat_stream_generate
from agents.personas import (
    ensure_philosopher_persona as _ensure_philosopher_persona,
)
import hf as hf_module
import hyprfit
import rag
import storage_diagnostics
import comfyui
import voice
import model_management
import model_providers
from image_prompt_enhancer import normalize_enhancer_response
from research import (
    REPORT_TEMPLATES,
    REPORT_TEMPLATE_MAP,
    close_web_fetch_client,
    fetch_bytes_safely,
    run_research_report,
)
from artifact_files import (
    archive_contents_for_path as _archive_contents_for_path,
    archive_entry_preview_for_path as _archive_entry_preview_for_path,
    artifact_file_metadata as _artifact_file_metadata,
    artifact_path_for_row as _artifact_path_for_row,
    artifact_text_preview_allowed as _artifact_text_preview_allowed,
    decode_preview_bytes as _decode_preview_bytes,
    extract_indexable_text as _extract_indexable_text,
    language_hint as _language_hint,
    render_markdown_safe as _render_markdown_safe,
    resolve_download_path as _resolve_download_path,
    sanitize_preview_html as _sanitize_preview_html,
)
from artifact_service import (
    delete_artifact_files_for_user_ids,
    delete_artifact_row_and_file,
)
from routes import register_extracted_routes
from routes.health import health_check_loop

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


def _public_settings_payload(settings: dict) -> dict:
    public = {k: v for k, v in settings.items() if k != "ollama_scan_ssh_password"}
    public.update(hyprfit.clean_ollama_scan_ssh_settings(settings, config.OLLAMA_URL, include_password=False))
    return public


def _coerce_service_url(value: str, env_key: str, default: str) -> str:
    """Return a runtime service URL, accepting bare host:port input from Settings."""
    raw = (value or "").strip()
    if not raw:
        return os.getenv(env_key, default)
    if "://" not in raw:
        raw = f"http://{raw}"
    return raw.rstrip("/")


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

# Strong refs for fire-and-forget background jobs (indexing, research runners).
# Without this, the event loop only weakly references a bare create_task() result,
# so an in-flight job can be garbage-collected and silently cancelled mid-run.
_BG_TASKS: set = set()


def _track_bg(coro):
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t


def _runtime_storage_dirs() -> list[str]:
    data_dir = os.path.dirname(config.DATABASE_PATH)
    settings_dir = os.path.dirname(config.SETTINGS_PATH)
    connector_dir = os.path.dirname(config.CONNECTOR_SECRETS_PATH)
    dirs = [
        data_dir,
        config.UPLOAD_DIR,
        os.path.join(config.UPLOAD_DIR, "avatars"),
        config.TOOLS_DIR,
        config.KB_DIR,
        rag.CHROMA_DIR,
        settings_dir,
        os.path.join(settings_dir, "comfy_workflows"),
        config.SANDBOX_DIR,
        config.SANDBOX_OUTPUTS_DIR,
        config.SANDBOX_WORKSPACE_DIR,
        config.SANDBOX_VENV_DIR,
        connector_dir,
    ]
    seen = set()
    out = []
    for path in dirs:
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _ensure_runtime_storage_dirs() -> None:
    for path in _runtime_storage_dirs():
        os.makedirs(path, exist_ok=True)


def _storage_health_check() -> dict:
    started = time.time()
    try:
        _ensure_runtime_storage_dirs()
        result = storage_diagnostics.runtime_storage_status(config.DATABASE_PATH, rag.CHROMA_DIR)
    except Exception as exc:
        result = {"status": "error", "error": str(exc)}
    result["response_ms"] = round((time.time() - started) * 1000)
    return result


def _raise_if_rag_storage_unwritable() -> None:
    try:
        _ensure_runtime_storage_dirs()
    except Exception as exc:
        print(f"[RAG] Storage directory setup failed: {exc}")
        raise HTTPException(500, storage_diagnostics.readonly_storage_message())
    status = storage_diagnostics.directory_storage_status(rag.CHROMA_DIR, "rag_chroma", scan_children=True)
    if status.get("status") != "ok":
        err = status.get("write_error") or status.get("error") or "RAG Chroma storage is not writable"
        print(f"[RAG] Storage preflight failed: {err}")
        raise HTTPException(500, storage_diagnostics.readonly_storage_message())


def _format_reindex_error(kb_name: str, error: Exception) -> str:
    if storage_diagnostics.is_readonly_storage_error(error):
        return f"{kb_name}: {storage_diagnostics.readonly_storage_message()}"
    return f"{kb_name}: {error}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cleanup_task_ref, _health_task_ref
    await db.init_db()
    try:
        _reaped = await db.reap_stale_runs()
        if _reaped.get("runs_reaped") or _reaped.get("workflows_blocked"):
            print(f"[Startup] Reaped stale runs: {_reaped}")
    except Exception as _re:
        print(f"[Startup] Stale-run reaper failed (non-fatal): {_re}")
    try:
        _ensure_runtime_storage_dirs()
    except Exception as exc:
        print(f"[Startup] Runtime storage setup failed: {exc}")
    # Init sandbox dirs + venv
    _init_sandbox()
    # Override service URLs from persistent settings if set. Empty values inherit
    # from environment/config defaults.
    _settings = load_settings()
    if _settings.get("ollama_url"):
        config.OLLAMA_URL = _coerce_service_url(_settings["ollama_url"], "OLLAMA_URL", "http://127.0.0.1:11434")
        print(f"[Config] Loaded Ollama URL from settings: {config.OLLAMA_URL}")
    if _settings.get("codebox_url"):
        config.CODEBOX_URL = _coerce_service_url(_settings["codebox_url"], "CODEBOX_URL", "http://127.0.0.1:8585")
        print(f"[Config] Loaded Codebox URL from settings: {config.CODEBOX_URL}")
    if _settings.get("searxng_url"):
        config.SEARXNG_URL = _coerce_service_url(_settings["searxng_url"], "SEARXNG_URL", "http://127.0.0.1:8888")
        print(f"[Config] Loaded SearXNG URL from settings: {config.SEARXNG_URL}")
    if _settings.get("n8n_url"):
        config.N8N_URL = _coerce_service_url(_settings["n8n_url"], "N8N_URL", "http://127.0.0.1:5678")
        print(f"[Config] Loaded N8N URL from settings: {config.N8N_URL}")
    if _settings.get("comfyui_url"):
        config.COMFYUI_URL = _coerce_service_url(_settings["comfyui_url"], "COMFYUI_URL", "")
        print(f"[Config] Loaded ComfyUI URL from settings: {config.COMFYUI_URL}")
    if _settings.get("stt_url"):
        config.STT_URL = _coerce_service_url(_settings["stt_url"], "STT_URL", "")
        print(f"[Config] Loaded STT URL from settings: {config.STT_URL}")
    if _settings.get("tts_url"):
        config.TTS_URL = _coerce_service_url(_settings["tts_url"], "TTS_URL", "")
        print(f"[Config] Loaded TTS URL from settings: {config.TTS_URL}")
    if _settings.get("tts_voice"):
        config.TTS_VOICE = str(_settings["tts_voice"])
    # Use `in _settings` (not `.get(...)` truthy check) so an explicitly-saved
    # empty string — meaning "inherit from chat model" in the UI — is honored
    # on startup. Otherwise the env default (e.g. PLANNING_MODEL=qwen3.5:27b
    # in config.py) silently overrides the user's choice every restart.
    if "planning_model" in _settings:
        config.PLANNING_MODEL = _settings["planning_model"] or ""
        print(f"[Config] Loaded Planning Model from settings: {config.PLANNING_MODEL or '(use chat model)'}")
    if "coder_model" in _settings:
        config.CODER_MODEL = _settings["coder_model"] or ""
        print(f"[Config] Loaded Coder Model from settings: {config.CODER_MODEL or '(use chat model)'}")
    # Workspace model (small/fast model used for quick_search triage,
    # auto-titles, and topic auto-detection). Empty = inherit chat model.
    if "workspace_model" in _settings:
        config.WORKSPACE_MODEL = _settings["workspace_model"] or ""
        print(f"[Config] Loaded Workspace Model from settings: {config.WORKSPACE_MODEL or '(use chat model)'}")
    # Coder Bot v2 per-agent overrides — each empty by default; only seen here
    # when the user has explicitly pinned a model for that agent.
    for _key, _attr in (
        ("architect_model", "ARCHITECT_MODEL"),
        ("reviewer_model",  "REVIEWER_MODEL"),
        ("acceptance_model", "ACCEPTANCE_MODEL"),
        ("builder_model",   "BUILDER_MODEL"),
        ("fixer_model",     "FIXER_MODEL"),
        ("qa_model",        "QA_MODEL"),
    ):
        if _key in _settings:
            setattr(config, _attr, _settings[_key] or "")
            if _settings[_key]:
                print(f"[Config] Loaded {_attr} from settings: {_settings[_key]}")
    if "openhands_enabled" in _settings:
        config.OPENHANDS_ENABLED = _settings["openhands_enabled"]
        print(f"[Config] Loaded OpenHands enabled: {config.OPENHANDS_ENABLED}")
    if "openhands_max_rounds" in _settings:
        config.OPENHANDS_MAX_ROUNDS = config.coerce_int(_settings["openhands_max_rounds"], config.OPENHANDS_MAX_ROUNDS, minimum=1, maximum=200)
        print(f"[Config] Loaded OpenHands max rounds: {config.OPENHANDS_MAX_ROUNDS}")
    if "openhands_num_ctx" in _settings:
        config.OPENHANDS_NUM_CTX = config.coerce_num_ctx(
            _settings["openhands_num_ctx"],
            fallback=config.OPENHANDS_NUM_CTX,
        )
        print(f"[Config] Loaded OpenHands num_ctx: {config.OPENHANDS_NUM_CTX}")
    if "openhands_reasoning_effort" in _settings:
        _re = (_settings["openhands_reasoning_effort"] or "medium").strip().lower()
        if _re not in ("low", "medium", "high"):
            _re = "medium"
        config.OPENHANDS_REASONING_EFFORT = _re
        print(f"[Config] Loaded OpenHands reasoning effort: {config.OPENHANDS_REASONING_EFFORT}")
    if "aider_enabled" in _settings:
        config.AIDER_ENABLED = bool(_settings["aider_enabled"])
        print(f"[Config] Loaded Aider enabled: {config.AIDER_ENABLED}")
    if "aider_model" in _settings:
        config.AIDER_MODEL = _settings["aider_model"] or ""
        print(f"[Config] Loaded Aider Model from settings: {config.AIDER_MODEL or '(inherit fixer/coder)'}")
    if "aider_num_ctx" in _settings:
        config.AIDER_NUM_CTX = config.coerce_num_ctx(
            _settings["aider_num_ctx"],
            fallback=config.AIDER_NUM_CTX,
        )
        print(f"[Config] Loaded Aider num_ctx: {config.AIDER_NUM_CTX}")
    if "aider_auto_test" in _settings:
        config.AIDER_AUTO_TEST = bool(_settings["aider_auto_test"])
        print(f"[Config] Loaded Aider auto-test: {config.AIDER_AUTO_TEST}")
    if "aider_worker_url" in _settings:
        config.AIDER_WORKER_URL = _settings["aider_worker_url"] or config.OPENHANDS_URL
        print(f"[Config] Loaded Aider worker URL: {config.AIDER_WORKER_URL}")
    if "default_num_ctx" in _settings:
        # Single knob the user controls. Drives the chat-side fallback in chat.py and
        # every internal LLM call (plan_project, critic) — so increasing the chat ctx
        # in Settings doesn't get silently capped by a hardcoded 16K downstream.
        config.DEFAULT_NUM_CTX = config.coerce_num_ctx(
            _settings["default_num_ctx"],
            fallback=config.DEFAULT_NUM_CTX,
        )
        print(f"[Config] Loaded default num_ctx: {config.DEFAULT_NUM_CTX}")
    if "research_num_ctx" in _settings:
        config.RESEARCH_NUM_CTX = config.coerce_num_ctx(
            _settings["research_num_ctx"],
            fallback=config.RESEARCH_NUM_CTX,
        )
        print(f"[Config] Loaded research num_ctx: {config.RESEARCH_NUM_CTX}")
    if "quick_search_mode" in _settings:
        _qsm = (_settings["quick_search_mode"] or "balanced").strip().lower()
        if _qsm not in ("speed", "balanced", "quality"):
            _qsm = "balanced"
        config.QUICK_SEARCH_MODE = _qsm
        print(f"[Config] Loaded Quick Search mode: {config.QUICK_SEARCH_MODE}")
    if "image_chat_checkpoint" in _settings:
        config.IMAGE_CHAT_CHECKPOINT = str(_settings["image_chat_checkpoint"] or "").strip()[:200]
        if config.IMAGE_CHAT_CHECKPOINT:
            print(f"[Config] Loaded chat image checkpoint: {config.IMAGE_CHAT_CHECKPOINT}")
    if "image_chat_workflow" in _settings:
        config.IMAGE_CHAT_WORKFLOW = str(_settings["image_chat_workflow"] or "").strip()[:200]
        if config.IMAGE_CHAT_WORKFLOW:
            print(f"[Config] Loaded chat image workflow: {config.IMAGE_CHAT_WORKFLOW}")
    if "image_chat_resolution" in _settings:
        _res = str(_settings["image_chat_resolution"] or "").strip()
        config.IMAGE_CHAT_RESOLUTION = _res if re.fullmatch(r"\d{3,4}x\d{3,4}", _res) else "1024x1024"
    if "image_chat_vae" in _settings:
        config.IMAGE_CHAT_VAE = str(_settings["image_chat_vae"] or "").strip()[:200]
    if "image_chat_prompt_prefix" in _settings:
        config.IMAGE_CHAT_PROMPT_PREFIX = str(_settings["image_chat_prompt_prefix"] or "").strip()[:500]
    if "image_chat_negative" in _settings:
        config.IMAGE_CHAT_NEGATIVE = str(_settings["image_chat_negative"] or "").strip()[:500]
    if "image_chat_compose_model" in _settings:
        config.IMAGE_CHAT_COMPOSE_MODEL = str(_settings["image_chat_compose_model"] or "").strip()[:200]
    # Run cleanup once on startup to clear any stale files
    _run_cleanup_sync()
    # Start background cleanup loop
    _cleanup_task_ref = asyncio.create_task(_cleanup_loop())
    # Start health check loop (every 5 min)
    _health_task_ref = asyncio.create_task(health_check_loop())
    # Load RAG settings from persistent config — clamped like the PATCH path,
    # so a legacy settings.json with junk values (e.g. chunk_size -5) can't
    # poison the runtime chunker after a restart.
    _rag_cfg = _settings.get("rag", {})
    if _rag_cfg.get("embed_model"):
        rag.EMBED_MODEL = _rag_cfg["embed_model"]
    if _rag_cfg.get("chunk_size"):
        rag.CHUNK_SIZE = config.coerce_int(_rag_cfg["chunk_size"], rag.CHUNK_SIZE, minimum=100, maximum=8000)
    if _rag_cfg.get("chunk_overlap") is not None:
        rag.CHUNK_OVERLAP = config.coerce_int(_rag_cfg["chunk_overlap"], rag.CHUNK_OVERLAP, minimum=0, maximum=2000)
    # Ensure RAG embedding model is available (non-blocking pull)
    _track_bg(rag.ensure_embed_model())
    # Backfill the kb_chunks_fts keyword index from existing Chroma documents
    # (no-op once populated; non-blocking)
    _track_bg(rag.backfill_fts())
    yield
    for task in [_cleanup_task_ref, _health_task_ref]:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await close_web_fetch_client()

app = FastAPI(title="HyprChat", version="2.0.0", lifespan=lifespan)

ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000",
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-HyprChat-User", "X-HyprChat-Session"],
)

_USER_SCOPED_PREFIXES = (
    "/api/conversations",
    "/api/artifacts",
    "/api/chat/stream",
    "/api/council/chat/stream",
    "/api/coder",
    "/api/runs",
    "/api/knowledge-bases",
    "/api/tools",
    "/api/model-configs",
    "/api/workspaces",
    "/api/memory",
    "/api/research/reports",
    "/api/councils",
    "/api/analytics",
    "/api/danger-zone",
    "/api/events",
)


def _request_user_id(request: Request) -> str:
    raw = (
        request.headers.get("x-hyprchat-user")
        or request.query_params.get("user_id")
        or db.DEFAULT_USER_ID
    )
    raw = re.sub(r"[^a-zA-Z0-9_.:-]", "", str(raw or ""))[:80]
    return raw or db.DEFAULT_USER_ID


def _request_session_token(request: Request) -> str:
    return (
        request.headers.get("x-hyprchat-session")
        or request.query_params.get("session")
        or ""
    )


def _is_user_scoped_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _USER_SCOPED_PREFIXES)


async def _validated_request_user(request: Request) -> dict:
    user_id = _request_user_id(request)
    user = await db.get_user_private(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.get("password_hash") and not await db.validate_user_session(user_id, _request_session_token(request)):
        raise HTTPException(401, "Password required")
    return user


@app.middleware("http")
async def user_context_middleware(request: Request, call_next):
    user_id = _request_user_id(request)
    user = await db.get_user_private(user_id)
    if not user:
        user_id = db.DEFAULT_USER_ID
        user = await db.get_user_private(user_id)
    valid = True
    if user and user.get("password_hash"):
        valid = await db.validate_user_session(user_id, _request_session_token(request))
    if _is_user_scoped_path(request.url.path) and not valid:
        return JSONResponse({"detail": "Password required"}, status_code=401)
    token = db.set_current_user_id(user_id if valid else db.DEFAULT_USER_ID)
    try:
        return await call_next(request)
    finally:
        db.reset_current_user_id(token)

HTTP_VERIFY_SSL = config.HTTP_VERIFY_SSL
http = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), verify=HTTP_VERIFY_SSL)
register_extracted_routes(
    app,
    http=http,
    track_bg=_track_bg,
    artifact_file_metadata=_artifact_file_metadata,
    storage_health_check=_storage_health_check,
    load_settings=load_settings,
    save_settings=save_settings,
    public_settings_payload=_public_settings_payload,
    coerce_service_url=_coerce_service_url,
    sandbox_size_bytes=_sandbox_size_bytes,
    request_user_id=_request_user_id,
    request_session_token=_request_session_token,
    validated_request_user=_validated_request_user,
    delete_artifact_files_for_user_ids=delete_artifact_files_for_user_ids,
    create_research_report=lambda payload: _create_and_start_research_report(ResearchReportCreate(**payload)),
    delete_all_models=lambda: delete_all_models(),
)

# Workspace helpers only do short classification/title/suggestion work. Keep
# their KV cache small even when the user sets chat context to 128K/256K or the
# selected helper model advertises a huge native context.
_WORKSPACE_HELPER_NUM_CTX = 4096
_WORKSPACE_TITLE_NUM_CTX = 2048

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
    think_budget: Optional[int] = None  # None=auto, 0=disable thinking, 1=enable thinking
    effort_rounds: Optional[int] = None  # 0=off, 1-3=extra self-review passes after the initial answer
    # Clean user-visible content for the latest user message (without the appended
    # `[Attached N image: ...]` hint that messages[].content carries for the model).
    # Used by chat.py's defensive user-message save so the persisted row matches what
    # the user typed, not the bloated model-facing string.
    display_content: Optional[str] = None
    # Metadata for the latest user message (e.g. {"images":[{name,dataUrl,mime}], "pdfs":[...]})
    # so image previews survive page reload.
    user_metadata: Optional[dict] = None
    # Optional workspace context. When memory is enabled for this workspace, the
    # chat agent injects accepted workspace memories and queues new suggestions.
    workspace_id: Optional[str] = None
    # Optional global HyprChat memory context. When true, the chat agent injects
    # accepted user-profile/global memories and queues new suggestions.
    use_memories: Optional[bool] = None
    # Ghost/private mode. When true, this stream must not persist messages,
    # workspace memories, token usage, or RAG/research memory for the turn.
    ephemeral: bool = False

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
    system_prompt: str = ""
    model_config_id: Optional[str] = None
    use_memories: Optional[str] = "0"

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
    pinned: Optional[str] = None
    use_memories: Optional[str] = None

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
    model_config_id: Optional[str] = None
    system_prompt: str = ""
    persona_name: str = ""

class CouncilMemberUpdate(BaseModel):
    model: Optional[str] = None
    model_config_id: Optional[str] = None
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

class ResearchReportCreate(BaseModel):
    title: Optional[str] = None
    query: str
    focus: str = ""
    report_type: str = "analyst"
    depth: Optional[int] = None
    model: str = ""
    planner_model: str = ""
    auditor_model: str = ""
    kb_ids: list[str] = []
    inputs: list[dict] = []

class KBCreate(BaseModel):
    name: str
    description: str = ""


class ConversationSearchRequest(BaseModel):
    query: str
    limit: int = 20

class ForkRequest(BaseModel):
    message_id: int

# ============================================================
# HEALTH & INFO
# ============================================================
_health_task_ref = None


# ============================================================
# OLLAMA — MODEL LISTING + STREAMING CHAT
# ============================================================
@app.get("/api/models")
async def list_models():
    """Fetch available models from Ollama plus enabled cloud providers."""
    models: list[str] = []
    model_details: dict[str, Any] = {}
    ollama_error: Exception | None = None
    try:
        r = await http.get(f"{config.OLLAMA_URL}/api/tags")
        r.raise_for_status()
        data = r.json()
        raw = data.get("models", [])
        model_details.update({m["name"]: {
            "size": m.get("size", 0),
            "modified_at": m.get("modified_at", ""),
            "details": m.get("details", {}),
            "digest": m.get("digest", ""),
        } for m in raw})
        models.extend([m["name"] for m in raw])
    except Exception as e:
        ollama_error = e

    cloud_models, cloud_details, cloud_errors = await model_providers.list_cloud_models(http)
    models.extend(cloud_models)
    model_details.update(cloud_details)
    if not models and ollama_error:
        raise HTTPException(502, f"Failed to reach Ollama: {ollama_error}")
    return {
        "models": models,
        "model_details": model_details,
        **({"provider_errors": cloud_errors} if cloud_errors else {}),
    }


async def _local_ollama_model_names() -> list[str]:
    try:
        r = await http.get(f"{config.OLLAMA_URL}/api/tags")
        r.raise_for_status()
        data = r.json()
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception as e:
        raise HTTPException(502, f"Failed to reach Ollama: {e}")



@app.get("/api/models/hyprfit")
async def hyprfit_recommendations(refresh: bool = False, live: bool = True):
    settings = load_settings()
    installed_details: dict[str, Any] = {}
    ollama_error = ""
    try:
        r = await http.get(f"{config.OLLAMA_URL}/api/tags")
        r.raise_for_status()
        raw = r.json().get("models", [])
        installed_details = {
            m.get("name", ""): {
                "size": m.get("size", 0),
                "details": m.get("details", {}),
                "modified_at": m.get("modified_at", ""),
            }
            for m in raw
            if m.get("name")
        }
    except Exception as e:
        ollama_error = str(e)
    response = await hyprfit.build_response(
        settings.get("model_hardware_profile"),
        installed_details,
        refresh=refresh,
        live=live,
        http_client=http,
        hf_search_func=hf_module.hf_search,
    )
    response["ollama_error"] = ollama_error
    return response


@app.post("/api/models/hyprfit/rescan")
async def hyprfit_rescan_hardware():
    settings = load_settings()
    result = await hyprfit.resolve_hardware_rescan(
        settings.get("model_hardware_profile"),
        config.OLLAMA_URL,
        http,
        hyprfit.clean_ollama_scan_ssh_settings(settings, config.OLLAMA_URL, include_password=True),
    )
    if result.get("persisted"):
        settings["model_hardware_profile"] = result["profile"]
        save_settings(settings)
    return result


async def _delete_ollama_model(model_name: str) -> dict:
    return await model_management.delete_ollama_model(http, model_name)


@app.delete("/api/models")
async def delete_all_models():
    names = await _local_ollama_model_names()
    deleted: list[str] = []
    failed: list[dict] = []
    for name in names:
        try:
            await _delete_ollama_model(name)
            deleted.append(name)
        except HTTPException as e:
            failed.append({"model": name, "error": str(e.detail)})
        except Exception as e:
            failed.append({"model": name, "error": str(e)})
    return {
        "status": "deleted" if not failed else "partial",
        "deleted": len(deleted),
        "models": deleted,
        "failed": failed,
    }


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """Stream chat with multi-round tool-calling agent loop."""
    _all_custom = await db.get_tools()
    custom_tool_map: dict = {t["name"]: t for t in _all_custom}
    custom_tool_id_map: dict = {t["id"]: t for t in _all_custom}
    _all_connector_tools = await db.get_connector_tools(enabled_only=True)
    connector_tool_id_map: dict = {t["id"]: t for t in _all_connector_tools}
    connector_tool_name_map: dict = {t["tool_name"]: t for t in _all_connector_tools}
    user_id = db.current_user_id()

    async def _stream():
        token = db.set_current_user_id(user_id)
        try:
            async for chunk in chat_stream_generate(
                req,
                http,
                events,
                custom_tool_map,
                custom_tool_id_map,
                connector_tool_id_map,
                connector_tool_name_map,
            ):
                yield chunk
        finally:
            db.reset_current_user_id(token)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
    )


# ============================================================
# CODER BOT — upload an existing project archive
# ============================================================
@app.post("/api/coder/upload-project")
async def upload_coder_project(
    file: UploadFile = File(...),
    conv_id: str = Form(...),
):
    """Upload a .zip/.tar/.tar.gz of an existing project, extract it into the
    sandbox at /root/projects/{project_id}, and register it as the active
    coding project for this conversation. The chat agent's ACTIVE PROJECT
    injection then makes Coder Bot aware of the uploaded code automatically."""
    import io as _io
    import tarfile
    import zipfile

    if not conv_id:
        raise HTTPException(400, "conv_id required")

    safe_name = os.path.basename(file.filename or "project")
    lower = safe_name.lower()
    is_zip = lower.endswith(".zip")
    tar_suffixes = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar")
    is_tar = any(lower.endswith(s) for s in tar_suffixes)
    if not (is_zip or is_tar):
        raise HTTPException(400, "Upload must be .zip, .tar, .tar.gz, .tgz, .tar.bz2, or .tbz2")

    content = await file.read()
    if len(content) > config.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, f"Archive too large (max {config.MAX_UPLOAD_SIZE_MB}MB)")

    # Derive project name + id
    base = safe_name
    for suf in (".tar.gz", ".tar.bz2", ".tgz", ".tbz2", ".tar", ".zip"):
        if base.lower().endswith(suf):
            base = base[: -len(suf)]
            break
    base = re.sub(r"[^a-zA-Z0-9._-]", "-", base).strip("-_.") or "uploaded-project"
    project_id = f"proj-{uuid.uuid4().hex[:12]}"
    project_name = base[:60]

    # Extract locally to a staging dir with path sanitization
    staging_root = os.path.join(config.UPLOAD_DIR, "coder_projects", project_id)
    os.makedirs(staging_root, exist_ok=True)
    staging_abs = os.path.abspath(staging_root)

    def _safe_target(member_name: str) -> Optional[str]:
        if not member_name or member_name.startswith("/") or "\x00" in member_name:
            return None
        parts = [p for p in member_name.replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            return None
        target = os.path.abspath(os.path.join(staging_abs, *parts))
        if target != staging_abs and not target.startswith(staging_abs + os.sep):
            return None
        return target

    try:
        if is_zip:
            with zipfile.ZipFile(_io.BytesIO(content)) as zf:
                for info in zf.infolist():
                    target = _safe_target(info.filename)
                    if not target:
                        continue
                    if info.is_dir():
                        os.makedirs(target, exist_ok=True)
                    else:
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with zf.open(info) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
        else:
            if lower.endswith((".tar.gz", ".tgz")):
                mode = "r:gz"
            elif lower.endswith((".tar.bz2", ".tbz2")):
                mode = "r:bz2"
            else:
                mode = "r:"
            with tarfile.open(fileobj=_io.BytesIO(content), mode=mode) as tf:
                for m in tf.getmembers():
                    if m.islnk() or m.issym() or m.isdev():
                        continue
                    target = _safe_target(m.name)
                    if not target:
                        continue
                    if m.isdir():
                        os.makedirs(target, exist_ok=True)
                    elif m.isfile():
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        ef = tf.extractfile(m)
                        if ef is None:
                            continue
                        with open(target, "wb") as dst:
                            shutil.copyfileobj(ef, dst)
    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise HTTPException(400, f"Failed to extract archive: {e}")

    # Collapse a single top-level folder (typical of GitHub tarballs)
    try:
        entries = [e for e in os.listdir(staging_root) if not e.startswith("__MACOSX")]
        if len(entries) == 1:
            only = os.path.join(staging_root, entries[0])
            if os.path.isdir(only):
                for name in os.listdir(only):
                    shutil.move(os.path.join(only, name), os.path.join(staging_root, name))
                shutil.rmtree(only, ignore_errors=True)
    except Exception as e:
        print(f"[CoderUpload] top-level collapse failed (non-fatal): {e}")

    # Walk the cleaned tree for manifest + language detection
    manifest: list[str] = []
    ext_counts: dict[str, int] = {}
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv",
                 "dist", "build", ".next", ".cache", ".idea", ".vscode", "target"}
    for root, dirs, files in os.walk(staging_root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            rel = os.path.relpath(os.path.join(root, fname), staging_root)
            manifest.append(rel)
            ext = os.path.splitext(fname)[1].lower()
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

    if not manifest:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise HTTPException(400, "Archive contained no usable files")

    lang_map = {
        ".py": "python", ".js": "javascript", ".jsx": "javascript",
        ".ts": "typescript", ".tsx": "typescript",
        ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin",
        ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c",
        ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp",
        ".swift": "swift", ".scala": "scala", ".sh": "bash",
    }
    lang_totals: dict[str, int] = {}
    for ext, n in ext_counts.items():
        lang = lang_map.get(ext)
        if lang:
            lang_totals[lang] = lang_totals.get(lang, 0) + n
    language = max(lang_totals, key=lang_totals.get) if lang_totals else "unknown"
    try:
        from agents import language_adapters
        project_contract = language_adapters.detect_contract(manifest, language)
        language = project_contract.get("language") or language
    except Exception as e:
        print(f"[CoderUpload] contract detection failed (non-fatal): {e}")
        project_contract = {"language": language, "build_system": "unknown"}

    # Re-tar the sanitized tree (gzip) for transport to the sandbox
    tar_buf = _io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as out_tf:
        out_tf.add(staging_root, arcname=".")
    tar_bytes = tar_buf.getvalue()

    # Upload to codebox via the dedicated /upload-chunk endpoint. Sends raw
    # tarball bytes in chunks via multipart — bypasses both pitfalls of
    # routing-via-/command:
    #   1. No shell involved → no MAX_ARG_STRLEN limit (was capped at 100KB).
    #   2. No deny-list collisions (random bytes can contain "mkfs" without
    #      being mistaken for a system-format command).
    # 1MB chunks are an order of magnitude faster (10× fewer round-trips than
    # the old 100KB-of-base64 approach).
    remote_dir = f"/root/projects/{project_id}"
    remote_tmp = f"/tmp/{project_id}.tar.gz"
    git_baseline_status = "not_started"
    CHUNK = 1_000_000  # 1MB of raw bytes per multipart POST
    total_chunks = max(1, (len(tar_bytes) + CHUNK - 1) // CHUNK)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as http_client:
            for i in range(total_chunks):
                chunk = tar_bytes[i * CHUNK : (i + 1) * CHUNK]
                files = {"file": (f"chunk-{i}", chunk, "application/octet-stream")}
                # truncate=true on first chunk wipes any partial leftover from
                # a previous failed upload; subsequent chunks append.
                form = {"path": remote_tmp, "truncate": "true" if i == 0 else "false"}
                r = await http_client.post(
                    f"{config.CODEBOX_URL}/upload-chunk",
                    files=files, data=form,
                    timeout=60,
                )
                if r.status_code != 200:
                    raise HTTPException(
                        500,
                        f"Chunk {i+1}/{total_chunks} upload failed: "
                        f"HTTP {r.status_code} — {r.text[:200]}",
                    )

            # Make sure the project directory exists, then extract directly
            # from the file we just wrote. No base64 in this command.
            extract_cmd = (
                f"mkdir -p {shlex.quote(remote_dir)} && "
                f"tar -xzf {shlex.quote(remote_tmp)} -C {shlex.quote(remote_dir)} && "
                f"rm -f {shlex.quote(remote_tmp)}"
            )
            r = await http_client.post(
                f"{config.CODEBOX_URL}/command",
                json={"command": extract_cmd, "timeout": 120},
                timeout=180,
            )
            data = r.json()
            if data.get("exit_code", 1) != 0:
                raise HTTPException(
                    500,
                    f"Sandbox extraction failed: {(data.get('stderr') or '')[:300]}",
                )
            git_cmd = (
                f"cd {shlex.quote(remote_dir)} && "
                f"git config --global --add safe.directory {shlex.quote(remote_dir)} >/dev/null 2>&1 || true; "
                "if [ -d .git ]; then "
                "  git status --short >/dev/null 2>&1 && echo GIT_BASELINE:existing; "
                "else "
                "  git init >/dev/null 2>&1 && "
                "  git config user.email hyprchat.local && "
                "  git config user.name HyprChat && "
                "  git add -A && "
                "  git commit --allow-empty -m 'Baseline upload' >/dev/null 2>&1 && "
                "  echo GIT_BASELINE:created; "
                "fi"
            )
            gr = await http_client.post(
                f"{config.CODEBOX_URL}/command",
                json={"command": git_cmd, "timeout": 120},
                timeout=180,
            )
            gd = gr.json()
            gout = gd.get("stdout") or ""
            if gd.get("exit_code", 1) == 0 and "GIT_BASELINE:" in gout:
                git_baseline_status = gout.split("GIT_BASELINE:", 1)[1].strip().splitlines()[0]
            else:
                git_baseline_status = "failed"
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"CodeBox upload failed: {e}")

    # Register as the conversation's active coding project. The chat agent's
    # ACTIVE PROJECT injection (agents/chat.py) will pick this up on next turn,
    # and openhands_worker will reuse /root/projects/{project_id} because the
    # directory name matches the project_id.
    description = f"Uploaded project: {safe_name} — {len(manifest)} files, {language}"
    try:
        await db.upsert_coding_project(
            project_id=project_id,
            name=project_name,
            conversation_id=conv_id,
            description=description,
            language=language,
            file_manifest=manifest,
            openhands_project_id=project_id,
        )
    except Exception as e:
        print(f"[CoderUpload] DB save failed (non-fatal): {e}")

    workflow_id = f"cw-{uuid.uuid4().hex[:12]}"
    workflow_status = "created"
    for _wf_attempt in (1, 2):
        try:
            await db.create_coder_workflow(
                workflow_id,
                conv_id,
                project_id=project_id,
                mode="fix_uploaded_project",
                state="planning",
                user_task=f"Uploaded project: {safe_name}",
                contract=project_contract,
                artifact_status="not_ready",
            )
            break
        except Exception as e:
            print(f"[CoderUpload] workflow create failed (attempt {_wf_attempt}): {e}")
            if _wf_attempt == 2:
                workflow_id = ""
                workflow_status = "failed"

    # Phase 4.2 — fire the Project Indexer in the background. It walks the
    # uploaded tree, detects build system, and seeds ChromaDB code_memory so
    # that subsequent ask_project / generate_code calls have semantic
    # retrieval over the uploaded code. Non-blocking: even if it fails, the
    # upload itself succeeds.
    indexer_run_id = ""
    indexer_status = "not_started"
    try:
        from agents import project_indexer
        indexer_envelope = await project_indexer.run_project_indexer(
            http, events, conv_id,
            project_id=project_id,
            project_dir=remote_dir,
            project_name=project_name,
        )
        indexer_run_id = indexer_envelope.get("run_id", "") or ""
        indexer_status = indexer_envelope.get("status", "ok") or "ok"
        # The indexer may also have detected a more accurate language than the
        # upload-time mime/extension heuristic. If so, update the
        # coding_projects row so downstream tools see the corrected value.
        detected_lang = (indexer_envelope.get("language") or "").strip()
        if detected_lang and detected_lang != language:
            try:
                await db.upsert_coding_project(
                    project_id=project_id,
                    name=project_name,
                    conversation_id=conv_id,
                    description=description,
                    language=detected_lang,
                    file_manifest=manifest,
                    openhands_project_id=project_id,
                )
                language = detected_lang
                print(f"[CoderUpload] Indexer corrected language → {detected_lang}")
            except Exception as e:
                print(f"[CoderUpload] language correction failed (non-fatal): {e}")
    except Exception as e:
        print(f"[CoderUpload] Indexer failed (non-fatal): {e}")
        indexer_status = "failed"

    # Keep the local staging copy as a backup we can re-push if needed.
    return {
        "project_id": project_id,
        "workflow_id": workflow_id,
        "workflow_status": workflow_status,
        "name": project_name,
        "language": language,
        "file_count": len(manifest),
        "files": manifest[:30],
        "sandbox_path": remote_dir,
        "size_bytes": len(content),
        "indexer_run_id": indexer_run_id,
        "indexer_status": indexer_status,
        "git_baseline_status": git_baseline_status,
        "detected_build_system": project_contract.get("build_system", "unknown"),
        "detected_build_cmd": project_contract.get("build_cmd", ""),
        "detected_test_cmd": project_contract.get("test_cmd", ""),
        "contract": project_contract,
    }


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
        max_bytes = min(max(int(req.max_chars or config.MAX_FETCH_CHARS) * 8, 65536), 1024 * 1024)
        status, headers, final_url, content = await fetch_bytes_safely(
            http, req.url, timeout=15, max_bytes=max_bytes
        )
        if status >= 400:
            await events.emit(conv_id, "tool_error", {
                "tool": "fetch_url",
                "status": f"HTTP {status}: {req.url[:40]}",
                "icon": "globe",
            })
            raise HTTPException(502, f"Fetch error: HTTP {status}")
        text = _decode_preview_bytes(content, headers)[:req.max_chars]

        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        await events.emit(conv_id, "tool_end", {
            "tool": "fetch_url",
            "status": f"Read {len(text)} chars from {final_url[:40]}",
            "icon": "globe",
        })

        return {"url": final_url, "content": text[:req.max_chars], "length": len(text)}
    except HTTPException:
        raise
    except ValueError as e:
        await events.emit(conv_id, "tool_error", {"tool": "fetch_url", "status": str(e), "icon": "globe"})
        raise HTTPException(400, str(e))
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
        max_bytes = int(os.getenv("MAX_PROXY_PREVIEW_BYTES", str(12 * 1024 * 1024)))
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        status, resp_headers, final_url, content = await fetch_bytes_safely(
            http, url, timeout=20, headers=headers, max_bytes=max_bytes
        )
        if status >= 400:
            raise HTTPException(status, f"Upstream returned {status}")
        ct = resp_headers.get("content-type", "")
        safe_headers = {
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src data: http: https:; style-src 'unsafe-inline'; font-src data: http: https:;",
        }
        if "pdf" in ct or url.lower().endswith(".pdf"):
            return StarletteResponse(content=content, media_type="application/pdf", headers=safe_headers)
        if any(mt in ct for mt in ["image/png", "image/jpeg", "image/gif", "image/webp", "image/svg"]):
            return StarletteResponse(content=content, media_type=ct.split(";")[0], headers=safe_headers)
        if "html" in ct:
            html = _sanitize_preview_html(_decode_preview_bytes(content, resp_headers), final_url)
            return StarletteResponse(content=html, media_type="text/html; charset=utf-8", headers=safe_headers)
        return StarletteResponse(
            content=_decode_preview_bytes(content, resp_headers),
            media_type="text/plain; charset=utf-8",
            headers=safe_headers,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
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
# DEEP RESEARCH — durable report workspace
# ============================================================
def _research_default_depth(report_type: str, depth: Optional[int]) -> int:
    if depth is not None:
        return max(1, min(5, int(depth or 3)))
    tmpl = REPORT_TEMPLATE_MAP.get(report_type or "analyst") or REPORT_TEMPLATE_MAP["analyst"]
    return int(tmpl.get("default_depth") or 3)


async def _create_and_start_research_report(req: ResearchReportCreate) -> dict:
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(400, "Research query is required")
    report_type = req.report_type if req.report_type in REPORT_TEMPLATE_MAP else "analyst"
    depth = _research_default_depth(report_type, req.depth)
    report_id = f"research-{uuid.uuid4().hex[:10]}"
    await db.create_research_report(
        report_id,
        title=(req.title or query[:80]).strip(),
        query=query,
        focus=req.focus or "",
        report_type=report_type,
        depth=depth,
        model=req.model or config.DEFAULT_MODEL,
        planner_model=req.planner_model or "",
        auditor_model=req.auditor_model or "",
        kb_ids=req.kb_ids or [],
        inputs=req.inputs or [],
        status="queued",
    )
    user_id = db.current_user_id()

    async def _runner():
        token = db.set_current_user_id(user_id)
        try:
            await run_research_report(
                http, config.OLLAMA_URL, config.DEFAULT_MODEL, events, report_id,
                query=query,
                depth=depth,
                focus=req.focus or "",
                report_type=report_type,
                model=req.model or config.DEFAULT_MODEL,
                planner_model=req.planner_model or "",
                auditor_model=req.auditor_model or "",
                kb_ids=req.kb_ids or [],
                inputs=req.inputs or [],
            )
        except Exception as e:
            print(f"[RESEARCH REPORT] Background task failed: {e}")
            await db.update_research_report(
                report_id,
                status="failed",
                error=str(e),
                completed_at=datetime.utcnow().isoformat(),
            )
            await db.append_research_event(report_id, {
                "type": "research_error",
                "data": {"status": "failed", "error": str(e)},
                "timestamp": time.time(),
            })
            await events.emit(report_id, "research_error", {"status": "failed", "error": str(e)})
        finally:
            db.reset_current_user_id(token)

    _track_bg(_runner())
    return await db.get_research_report(report_id)


@app.get("/api/research/templates")
async def get_research_templates():
    return {"templates": REPORT_TEMPLATES}


@app.get("/api/research/reports")
async def list_research_reports(limit: int = Query(50), offset: int = Query(0),
                                q: str = Query("")):
    return await db.list_research_reports(limit=limit, offset=offset, query=q)


@app.post("/api/research/reports")
async def create_research_report(req: ResearchReportCreate):
    return await _create_and_start_research_report(req)


@app.get("/api/research/reports/{report_id}")
async def get_research_report(report_id: str):
    report = await db.get_research_report(report_id)
    if not report:
        raise HTTPException(404, "Research report not found")
    return report


@app.delete("/api/research/reports/{report_id}")
async def delete_research_report(report_id: str):
    await db.delete_research_report(report_id)
    return {"ok": True}


@app.post("/api/research/reports/{report_id}/cancel")
async def cancel_research_report(report_id: str):
    import cancel_registry

    signaled = cancel_registry.signal(report_id)
    report = await db.get_research_report(report_id)
    if not report:
        raise HTTPException(404, "Research report not found")
    marked = False
    if report.get("status") in ("queued", "running"):
        marked = True
        await db.update_research_report(
            report_id,
            status="cancelled",
            error="Cancelled by user",
            completed_at=datetime.utcnow().isoformat(),
        )
        ev = {
            "type": "research_error",
            "data": {"status": "cancelled", "error": "Cancelled by user"},
            "timestamp": time.time(),
        }
        await db.append_research_event(report_id, ev)
        await events.emit(report_id, "research_error", ev["data"])
    return {"report_id": report_id, "signaled": signaled, "marked": marked}


@app.post("/api/research/reports/{report_id}/rerun")
async def rerun_research_report(report_id: str):
    report = await db.get_research_report(report_id)
    if not report:
        raise HTTPException(404, "Research report not found")
    return await _create_and_start_research_report(ResearchReportCreate(
        title=report.get("title") or report.get("query", "")[:80],
        query=report.get("query", ""),
        focus=report.get("focus", ""),
        report_type=report.get("report_type", "analyst"),
        depth=report.get("depth") or None,
        model=report.get("model", "") or config.DEFAULT_MODEL,
        planner_model=report.get("planner_model", ""),
        auditor_model=report.get("auditor_model", ""),
        kb_ids=report.get("kb_ids", []),
        inputs=report.get("inputs", []),
    ))


# ============================================================
# RUNS — Coder Bot v2 durable agent invocations
# ============================================================
# Live updates flow through the existing /api/events/{conversation_id} stream;
# run-tagged events carry a `run_id` field that the frontend filters on.
# These endpoints exist so the UI can rebuild a run's state on reconnect.

@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    """Full run state including parsed result_envelope and events_log."""
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@app.get("/api/runs")
async def list_runs(conversation_id: str = Query(None), project_id: str = Query(None),
                    limit: int = Query(100)):
    """List runs filtered by conversation or project. Newest first.

    Exactly one of conversation_id / project_id must be provided.
    """
    if conversation_id and project_id:
        raise HTTPException(400, "Provide only one of conversation_id, project_id")
    if conversation_id:
        return await db.get_runs_by_conversation(conversation_id, limit=limit)
    if project_id:
        return await db.get_runs_by_project(project_id, limit=limit)
    raise HTTPException(400, "conversation_id or project_id is required")


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """Signal an in-flight run to abort.

    Two effects:
      1. Sets the asyncio.Event in `cancel_registry` so any agent waiting on a
         long Ollama / Codebox / OpenHands call via `await_cancellable` aborts
         its in-flight request immediately.
      2. Updates the `runs` row to `status='cancelled'` if it was still running
         or pending. Frontend polling sees the new status on its next tick,
         even if the in-process signal arrived too late (e.g. after restart).

    Always returns 200. Unknown / already-finished runs are a no-op rather
    than an error so the frontend can fan out cancels without per-run guards.
    """
    import cancel_registry

    signaled = cancel_registry.signal(run_id)

    db_marked = False
    try:
        row = await db.get_run(run_id)
        if row and row.get("status") in ("running", "pending", "queued"):
            envelope = row.get("result_envelope") or {}
            if not isinstance(envelope, dict):
                envelope = {}
            envelope = {
                **envelope,
                "status": "cancelled",
                "summary": envelope.get("summary") or "Cancelled by user (Stop pressed)",
            }
            await db.update_run(run_id, status="cancelled",
                                result_envelope=envelope, ended=True)
            try:
                await db.append_run_event(run_id, {
                    "type": "step",
                    "action": "cancelled",
                    "detail": "user pressed Stop",
                })
            except Exception:
                pass
            db_marked = True
    except Exception as e:
        print(f"[CANCEL] DB update failed for {run_id}: {e}")

    return {"run_id": run_id, "signaled": signaled, "db_marked": db_marked}


# ============================================================
# CODER WORKFLOWS — workflow-level Coder Bot v2 state
# ============================================================

@app.get("/api/coder/workflows/{workflow_id}")
async def get_coder_workflow(workflow_id: str):
    wf = await db.get_coder_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    return wf


@app.get("/api/coder/workflows")
async def list_coder_workflows(conversation_id: str = Query(...),
                               limit: int = Query(50)):
    return await db.get_coder_workflows_by_conversation(conversation_id, limit=limit)


@app.post("/api/coder/workflows/{workflow_id}/cancel")
async def cancel_coder_workflow(workflow_id: str):
    wf = await db.get_coder_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    active_run_id = wf.get("active_run_id") or ""
    signaled = False
    if active_run_id:
        import cancel_registry
        signaled = cancel_registry.signal(active_run_id)
    await db.update_coder_workflow(
        workflow_id,
        state="cancelled",
        cancel_requested=True,
        artifact_status="cancelled",
    )
    return {"workflow_id": workflow_id, "active_run_id": active_run_id, "signaled": signaled}


# ============================================================
# CONVERSATIONS
# ============================================================
@app.post("/api/conversations")
async def create_conversation(req: ConversationCreate):
    id = f"conv-{uuid.uuid4().hex[:12]}"
    await db.create_conversation(id, req.title, req.model, req.system_prompt, req.model_config_id, req.use_memories or "0")
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
    """Delete all conversations for the active local user/profile."""
    conn = await db.get_db()
    user_id = db.current_user_id()
    try:
        row = await conn.execute_fetchall("SELECT COUNT(*) AS n FROM conversations WHERE user_id=?", (user_id,))
        count = row[0]["n"] if row else 0
        await conn.execute(
            "DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id=?)",
            (user_id,),
        )
        await conn.execute(
            "DELETE FROM conversation_files WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id=?)",
            (user_id,),
        )
        await conn.execute(
            "DELETE FROM workspace_conversations WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id=?)",
            (user_id,),
        )
        await conn.execute("DELETE FROM conversations WHERE user_id=?", (user_id,))
        await conn.commit()
    finally:
        await conn.close()
    print(f"[Cleanup] Deleted {count} conversations and related data for user {user_id}")
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
    msg_id = await db.add_message(conv_id, role, content, metadata=meta)
    return {"status": "added", "message_id": msg_id}


@app.patch("/api/conversations/{conv_id}/messages/{msg_id}")
async def update_message(conv_id: str, msg_id: int, body: dict = Body(...)):
    """Update content and/or metadata of an existing message. Used by the frontend's
    stream-complete handler to finalize a message the chat agent created at stream-start —
    keeps the row count to one per assistant turn even if the chat agent already persisted
    progressive snapshots from the server side."""
    content = body.get("content")
    meta = body.get("metadata")
    if content is None and meta is None:
        raise HTTPException(400, "content or metadata required")
    await db.update_message(msg_id, content=content, metadata=meta)
    return {"status": "updated"}


@app.delete("/api/conversations/{conv_id}/messages/{msg_id}")
async def delete_message(conv_id: str, msg_id: int):
    ok = await db.delete_message(msg_id)
    if not ok:
        raise HTTPException(404, "message not found")
    return {"status": "deleted"}


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
    owned = await db.get_kb(kb_id)
    if not owned:
        raise HTTPException(404, "KB not found")
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
    owned = await db.get_kb(kb_id)
    if not owned:
        raise HTTPException(404, "KB not found")
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

    def _write_upload():
        with open(filepath, "wb") as f:
            f.write(content)
    await asyncio.to_thread(_write_upload)

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

    _track_bg(_bg_index())

    return {"id": file_id, "filename": safe_name, "file_size": len(content), "indexing": True}


@app.get("/api/knowledge-bases/{kb_id}/files/{filename}/status")
async def get_file_index_status(kb_id: str, filename: str):
    """Check background indexing status for a file."""
    owned = await db.get_kb(kb_id)
    if not owned:
        raise HTTPException(404, "KB not found")
    status_key = f"{kb_id}:{filename}"
    status = _indexing_status.get(status_key)
    if status:
        # Evict terminal statuses on read so the dict doesn't grow unbounded
        if status.get("status") in ("done", "error"):
            _indexing_status.pop(status_key, None)
        return status
    return {"status": "unknown", "filename": filename}


@app.get("/api/knowledge-bases/{kb_id}/files/{file_id}/preview")
async def preview_kb_file(kb_id: str, file_id: int, lines: int = 200):
    """Preview first N lines of a KB file."""
    user_id = db.current_user_id()
    _db = await db.get_db()
    try:
        cursor = await _db.execute(
            """SELECT f.filename FROM kb_files f
               JOIN knowledge_bases kb ON kb.id=f.kb_id
               WHERE f.id=? AND f.kb_id=? AND kb.user_id=?""",
            (file_id, kb_id, user_id),
        )
        row = await cursor.fetchone()
    finally:
        await _db.close()
    if not row:
        raise HTTPException(404, "File not found")
    filename = row["filename"]
    kb_dir = os.path.join(config.KB_DIR, kb_id)
    file_path = os.path.abspath(os.path.join(kb_dir, filename))
    if not file_path.startswith(os.path.abspath(kb_dir)):
        raise HTTPException(400, "Invalid path")
    if not os.path.exists(file_path):
        raise HTTPException(404, "File not found on disk")
    try:
        def _read_lines():
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                return f.readlines()
        all_lines = await asyncio.to_thread(_read_lines)
        total = len(all_lines)
        content = "".join(all_lines[:lines])
        return {"filename": filename, "content": content, "truncated": total > lines, "total_lines": total}
    except UnicodeDecodeError:
        return {"filename": filename, "content": "Binary file — preview not available", "truncated": False, "total_lines": 0}


@app.get("/api/knowledge-bases/{kb_id}/files/{file_id}/pdf-text")
async def pdf_text_preview(kb_id: str, file_id: int, pages: int = 10):
    """Extract text from first N pages of a PDF for quick preview."""
    user_id = db.current_user_id()
    _db = await db.get_db()
    try:
        cursor = await _db.execute(
            """SELECT f.filename FROM kb_files f
               JOIN knowledge_bases kb ON kb.id=f.kb_id
               WHERE f.id=? AND f.kb_id=? AND kb.user_id=?""",
            (file_id, kb_id, user_id),
        )
        row = await cursor.fetchone()
    finally:
        await _db.close()
    if not row:
        raise HTTPException(404, "File not found")
    filename = row["filename"]
    kb_dir = os.path.join(config.KB_DIR, kb_id)
    file_path = os.path.abspath(os.path.join(kb_dir, filename))
    if not file_path.startswith(os.path.abspath(kb_dir)):
        raise HTTPException(400, "Invalid path")
    if not os.path.exists(file_path):
        raise HTTPException(404, "File not found on disk")
    try:
        def _extract_pdf_text():
            from pypdf import PdfReader
            reader = PdfReader(file_path)
            total_pages = len(reader.pages)
            extracted = []
            for i, page in enumerate(reader.pages[:pages]):
                text = page.extract_text() or ""
                if text.strip():
                    extracted.append(f"[Page {i+1}]\n{text}")
            content = "\n\n".join(extracted) if extracted else "No extractable text found in this PDF."
            return content, total_pages
        content, total_pages = await asyncio.to_thread(_extract_pdf_text)
        return {"filename": filename, "content": content, "total_pages": total_pages, "previewed_pages": min(pages, total_pages), "truncated": total_pages > pages}
    except ImportError:
        return {"filename": filename, "content": "pypdf not installed — text extraction unavailable.", "total_pages": 0, "previewed_pages": 0, "truncated": False}
    except Exception as e:
        return {"filename": filename, "content": f"Failed to extract PDF text: {e}", "total_pages": 0, "previewed_pages": 0, "truncated": False}


@app.post("/api/extract-pdf")
async def extract_pdf(file: UploadFile = File(...)):
    """Extract text from an uploaded PDF file."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")
    content = await file.read()
    if len(content) > config.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, f"PDF too large (max {config.MAX_UPLOAD_SIZE_MB}MB)")
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(content))
        total_pages = len(reader.pages)
        extracted = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                extracted.append(f"[Page {i+1}]\n{text}")
        text = "\n\n".join(extracted) if extracted else "No extractable text found in this PDF."
        return {"filename": file.filename, "text": text, "total_pages": total_pages}
    except ImportError:
        raise HTTPException(500, "pypdf not installed on server")
    except Exception as e:
        raise HTTPException(422, f"Failed to extract PDF text: {e}")


@app.get("/api/knowledge-bases/{kb_id}/files/{file_id}/raw")
async def raw_kb_file(kb_id: str, file_id: int):
    """Serve a KB file raw (for PDF/image preview in browser)."""
    user_id = db.current_user_id()
    _db = await db.get_db()
    try:
        cursor = await _db.execute(
            """SELECT f.filename FROM kb_files f
               JOIN knowledge_bases kb ON kb.id=f.kb_id
               WHERE f.id=? AND f.kb_id=? AND kb.user_id=?""",
            (file_id, kb_id, user_id),
        )
        row = await cursor.fetchone()
    finally:
        await _db.close()
    if not row:
        raise HTTPException(404, "File not found")
    filename = row["filename"]
    kb_dir = os.path.join(config.KB_DIR, kb_id)
    file_path = os.path.abspath(os.path.join(kb_dir, filename))
    if not file_path.startswith(os.path.abspath(kb_dir)):
        raise HTTPException(400, "Invalid path")
    if not os.path.exists(file_path):
        raise HTTPException(404, "File not found on disk")
    return FileResponse(file_path, filename=filename)


@app.delete("/api/knowledge-bases/files/{file_id}")
async def delete_kb_file(file_id: int):
    # Get file info before deleting so we can remove from RAG index
    user_id = db.current_user_id()
    _db = await db.get_db()
    try:
        cursor = await _db.execute(
            """SELECT f.kb_id, f.filename FROM kb_files f
               JOIN knowledge_bases kb ON kb.id=f.kb_id
               WHERE f.id=? AND kb.user_id=?""",
            (file_id, user_id),
        )
        row = await cursor.fetchone()
    finally:
        await _db.close()
    if not row:
        raise HTTPException(404, "File not found")

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
    _raise_if_rag_storage_unwritable()
    try:
        results = await rag.reindex_kb(kb_id, files)
    except Exception as e:
        raise HTTPException(500, f"Reindex failed: {_format_reindex_error(kb.get('name', kb_id), e)}")
    return {"status": "reindexed", "results": results}


@app.post("/api/knowledge-bases/reindex-all")
async def reindex_all_kbs():
    """Reindex all knowledge bases — one-time migration to RAG."""
    kbs = await db.get_kbs()
    all_results = []
    errors = []
    for kb in kbs:
        files = kb.get("files", [])
        if not files:
            continue
        try:
            _raise_if_rag_storage_unwritable()
            results = await rag.reindex_kb(kb["id"], files)
            all_results.append({"kb_id": kb["id"], "name": kb["name"], "results": results})
        except Exception as e:
            # One broken KB shouldn't abort the rest of the sweep.
            if isinstance(e, HTTPException):
                err = str(e.detail)
            else:
                err = _format_reindex_error(kb["name"], e)
            errors.append({"kb_id": kb["id"], "name": kb["name"], "error": err[:300]})
    if errors and not all_results:
        raise HTTPException(500, f"Reindex failed: {errors[0]['error']}")
    return {"status": "reindexed", "kbs": all_results, "errors": errors}


@app.post("/api/knowledge-bases/query")
async def query_knowledge_bases(body: dict):
    """Hybrid retrieval probe (vector + keyword, RRF-fused). Used by tests/UI."""
    kb_ids = body.get("kb_ids") or []
    query = (body.get("query") or "").strip()
    if not kb_ids or not isinstance(kb_ids, list):
        raise HTTPException(400, "kb_ids list is required")
    if not query:
        raise HTTPException(400, "query is required")
    top_k = config.coerce_int(body.get("top_k"), 6, minimum=1, maximum=30)
    # Restrict to KBs the current user owns
    owned = {k["id"] for k in await db.get_kbs()}
    kb_ids = [k for k in kb_ids if k in owned]
    if not kb_ids:
        raise HTTPException(404, "No accessible KBs in kb_ids")
    chunks = await rag.hybrid_query(kb_ids, query, top_k=top_k)
    return {"chunks": chunks, "count": len(chunks)}


# ============================================================
# IMAGE STUDIO — ComfyUI job proxy
# ============================================================
# In-process job registry (restart-lossy, same posture as cancel_registry).
# On a cache miss after restart, GET falls back to ComfyUI history directly.
_image_jobs: dict[str, dict] = {}
_image_checkpoints_cache: dict = {"ts": 0.0, "checkpoints": []}


@app.post("/api/images/generate")
async def generate_image_job(body: dict = Body(...)):
    if not config.COMFYUI_URL:
        raise HTTPException(503, "ComfyUI is not configured. Set the URL in Settings → Connections.")
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    checkpoint = (body.get("checkpoint") or "").strip()
    if checkpoint:
        valid = await comfyui.list_checkpoints()
        if valid and checkpoint not in valid:
            raise HTTPException(400, f"Unknown checkpoint: {checkpoint}")
    vae = (body.get("vae") or "").strip()
    if vae:
        valid_vaes = await comfyui.list_vaes()
        if valid_vaes and vae not in valid_vaes:
            raise HTTPException(400, f"Unknown VAE: {vae}")
    count = config.coerce_int(body.get("count"), 1, minimum=1, maximum=4)
    wf_name = (body.get("workflow") or "").strip()
    template = None
    if wf_name:
        template = comfyui.load_workflow(wf_name)
        if template is None:
            raise HTTPException(404, f"Workflow not found: {wf_name}")
    try:
        workflow, seed = comfyui.build_workflow(
            template or comfyui.load_template(),
            prompt=prompt,
            negative_prompt=(body.get("negative_prompt") or ""),
            width=body.get("width") or 1024,
            height=body.get("height") or 1024,
            steps=body.get("steps") or 25,
            cfg=body.get("cfg") or 7.0,
            seed=body.get("seed"),
            checkpoint=checkpoint,
            batch_size=count,
            sampler_name=(body.get("sampler") or "").strip(),
            scheduler=(body.get("scheduler") or "").strip(),
            v_prediction=bool(body.get("v_prediction")),
            model_sampling=(body.get("model_sampling") or "").strip(),
            vae=vae,
        )
        prompt_id = await comfyui.submit(workflow)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"ComfyUI submit failed: {str(e)[:300]}")
    params = {
        "prompt": prompt, "negative_prompt": (body.get("negative_prompt") or ""),
        "width": body.get("width") or 1024, "height": body.get("height") or 1024,
        "steps": body.get("steps") or 25, "cfg": body.get("cfg") or 7.0,
        "seed": seed, "checkpoint": checkpoint, "count": count,
        "sampler": (body.get("sampler") or "").strip(),
        "scheduler": (body.get("scheduler") or "").strip(),
        "v_prediction": bool(body.get("v_prediction")),
        "model_sampling": (body.get("model_sampling") or "").strip(),
        "vae": vae,
        "workflow": wf_name,
    }
    _image_jobs[prompt_id] = {"status": "queued", "params": params, "created": time.time()}
    return {"job_id": prompt_id, "seed": seed, "params": params}


@app.get("/api/images/jobs/{job_id}")
async def get_image_job(job_id: str):
    if not config.COMFYUI_URL:
        raise HTTPException(503, "ComfyUI is not configured")
    job = _image_jobs.get(job_id) or {"status": "queued", "params": {}, "created": time.time()}
    if job.get("status") == "done":
        return {"status": "done", "images": job.get("images", []), "params": job.get("params", {})}
    if job.get("status") == "error":
        return {"status": "error", "error": job.get("error", ""), "params": job.get("params", {})}
    try:
        history = await comfyui.get_history(job_id)
    except Exception as e:
        raise HTTPException(502, f"ComfyUI unreachable: {str(e)[:200]}")
    if not history:
        qpos = await comfyui.queue_position(job_id)
        status = "running" if qpos == 0 else "queued"
        out = {"status": status, "params": job.get("params", {})}
        if qpos and qpos > 0:
            out["queue_position"] = qpos
        _image_jobs[job_id] = {**job, "status": status}
        return out
    if history.get("status", {}).get("status_str") == "error":
        job.update(status="error", error="ComfyUI workflow error (check checkpoint and VRAM)")
        _image_jobs[job_id] = job
        comfyui.finish_job(job_id)
        return {"status": "error", "error": job["error"], "params": job.get("params", {})}
    outputs = comfyui.outputs_from_history(history)
    if not outputs:
        return {"status": "running", "params": job.get("params", {})}
    # First completed poll: persist files + artifacts, cache so repeats are idempotent
    os.makedirs(config.SANDBOX_OUTPUTS_DIR, exist_ok=True)
    images = []
    params = job.get("params", {})
    for i, img in enumerate(outputs):
        filename = f"comfy_{job_id[:8]}_{i}.png"
        filepath = os.path.join(config.SANDBOX_OUTPUTS_DIR, filename)
        try:
            if not os.path.exists(filepath):
                data = await comfyui.fetch_image(img)
                with open(filepath, "wb") as f:
                    f.write(data)
        except Exception as e:
            print(f"[IMAGE STUDIO] fetch failed: {e}")
            continue
        url = f"/api/downloads/{filename}"
        artifact_id = None
        try:
            file_meta = await asyncio.to_thread(_artifact_file_metadata, filepath)
            artifact = await db.add_artifact(
                filename=filename,
                url=url,
                kind="image",
                mime_type="image/png",
                storage_path=filepath,
                size_bytes=file_meta["size_bytes"],
                sha256=file_meta["sha256"],
                exists_status="present",
                status="draft",
                metadata={"source_tool": "image_studio", **params},
            )
            artifact_id = (artifact or {}).get("id")
        except Exception as e:
            print(f"[IMAGE STUDIO] artifact create failed: {e}")
        images.append({"filename": filename, "url": url, "artifact_id": artifact_id})
    job.update(status="done", images=images)
    _image_jobs[job_id] = job
    # First completed poll only (cached afterwards): release VRAM back to Ollama
    _track_bg(comfyui.free_memory())
    # HyprChat now holds the only needed copy — erase ComfyUI's traces of this
    # job (history entry + hyprchat-prefixed file copies, when the cleanup
    # node is installed).
    _track_bg(comfyui.forget_job(job_id))
    return {"status": "done", "images": images, "params": params}


@app.post("/api/images/jobs/{job_id}/cancel")
async def cancel_image_job(job_id: str):
    if not config.COMFYUI_URL:
        raise HTTPException(503, "ComfyUI is not configured")
    await comfyui.cancel(job_id)
    comfyui.finish_job(job_id)
    if job_id in _image_jobs:
        _image_jobs[job_id]["status"] = "error"
        _image_jobs[job_id]["error"] = "cancelled"
    return {"status": "cancelled"}


@app.post("/api/images/free-memory")
async def free_image_memory():
    """Unload cached ComfyUI models from RAM/VRAM on demand."""
    if not config.COMFYUI_URL:
        raise HTTPException(503, "ComfyUI is not configured")
    try:
        result = await comfyui.hyprchat_free()
    except Exception as e:
        raise HTTPException(502, f"ComfyUI model unload failed: {str(e)[:200]}")
    if result.get("status") == "busy" or result.get("ok") is False:
        detail = result.get("error") or "ComfyUI queue is active"
        raise HTTPException(409, detail)
    return result


@app.post("/api/images/restart-comfyui")
async def restart_comfyui_image_service():
    """Restart ComfyUI to release system RAM held by the Python process."""
    if not config.COMFYUI_URL:
        raise HTTPException(503, "ComfyUI is not configured")
    try:
        result = await comfyui.hyprchat_restart()
    except Exception as e:
        raise HTTPException(502, f"ComfyUI restart failed: {str(e)[:200]}")
    if result.get("status") == "busy" or result.get("ok") is False:
        status = 409 if result.get("status") == "busy" else 502
        detail = result.get("error") or "ComfyUI restart was not accepted"
        raise HTTPException(status, detail)
    return result


@app.get("/api/images/memory-status")
async def get_image_memory_status():
    """Optional status from the HyprChat ComfyUI control node."""
    if not config.COMFYUI_URL:
        raise HTTPException(503, "ComfyUI is not configured")
    status = await comfyui.hyprchat_memory()
    if status is None:
        raise HTTPException(404, "HyprChat ComfyUI control node is not installed")
    return status


@app.get("/api/images/workflows")
async def list_image_workflows():
    if not config.COMFYUI_URL:
        raise HTTPException(503, "ComfyUI is not configured")
    out = []
    for name in comfyui.list_workflows():
        wf = comfyui.load_workflow(name)
        if wf:
            out.append({"name": name, **comfyui.describe_workflow(wf)})
    return {"workflows": out}


@app.post("/api/images/workflows")
async def upload_image_workflow(body: dict = Body(...)):
    if not config.COMFYUI_URL:
        raise HTTPException(503, "ComfyUI is not configured")
    name = (body.get("name") or "").strip()
    wf = body.get("workflow")
    if not name:
        raise HTTPException(400, "name is required")
    # PNG path: a ComfyUI-generated image carries its API workflow in metadata
    if body.get("png_base64"):
        try:
            png_bytes = base64.b64decode(body["png_base64"], validate=True)
        except Exception:
            raise HTTPException(400, "Invalid PNG upload")
        if len(png_bytes) > 50 * 1024 * 1024:
            raise HTTPException(413, "PNG too large (50MB max)")
        wf = comfyui.workflow_from_png(png_bytes)
        if not wf:
            raise HTTPException(400, "No workflow metadata in this image. The host may have "
                                     "stripped it — download the original file, or get the .json.")
    if not isinstance(wf, dict) or not wf:
        raise HTTPException(400, "workflow must be a JSON object")
    # UI-format saves have a top-level "nodes" array; only API exports run via the API.
    if isinstance(wf.get("nodes"), list):
        raise HTTPException(400, "This is a UI-format workflow. In ComfyUI enable Dev mode "
                                 "(Settings) and use 'Export (API)' — that file works here.")
    try:
        saved = comfyui.save_workflow(name, wf)
    except ValueError as e:
        raise HTTPException(400, f"Workflow not usable for text-to-image: {e}")
    return {"status": "saved", "name": saved, **comfyui.describe_workflow(wf)}


@app.delete("/api/images/workflows/{name}")
async def delete_image_workflow(name: str):
    if not comfyui.delete_workflow(name):
        raise HTTPException(404, "Workflow not found")
    return {"status": "deleted"}


@app.get("/api/images/checkpoints")
async def list_image_checkpoints():
    if not config.COMFYUI_URL:
        raise HTTPException(503, "ComfyUI is not configured. Set the URL in Settings → Connections.")
    if time.time() - _image_checkpoints_cache["ts"] > 60:
        _image_checkpoints_cache["checkpoints"] = await comfyui.list_checkpoints()
        _image_checkpoints_cache["vaes"] = await comfyui.list_vaes()
        _image_checkpoints_cache["ts"] = time.time()
    cks = _image_checkpoints_cache["checkpoints"]
    return {
        "checkpoints": cks,
        "default": cks[0] if cks else "",
        "vaes": _image_checkpoints_cache.get("vaes", []),
        # Resolved per-model generation settings (built-in family defaults
        # merged with user-saved overrides) so the UI can auto-configure.
        "settings": {c: comfyui.settings_for_checkpoint(c) for c in cks},
    }


@app.put("/api/images/model-settings/{checkpoint}")
async def save_model_settings_ep(checkpoint: str, body: dict = Body(...)):
    if not config.COMFYUI_URL:
        raise HTTPException(503, "ComfyUI is not configured")
    clean = {}
    ms = (body.get("model_sampling") or "").strip()
    if ms in ("", "vpred", "flow"):
        clean["model_sampling"] = ms
    if (body.get("sampler") or "") in comfyui.ALLOWED_SAMPLERS:
        clean["sampler"] = body["sampler"]
    if (body.get("scheduler") or "") in comfyui.ALLOWED_SCHEDULERS:
        clean["scheduler"] = body["scheduler"]
    try:
        if body.get("cfg") is not None:
            clean["cfg"] = max(1.0, min(20.0, float(body["cfg"])))
        if body.get("steps") is not None:
            clean["steps"] = config.coerce_int(body["steps"], 25, minimum=1, maximum=60)
    except (TypeError, ValueError):
        pass
    # Per-model chat prompt prefixes. Key-present-with-"" is an intentional
    # clear (overrides any builtin family prefix like pony score tags).
    for _pk in ("prompt_prefix", "negative_prefix"):
        if _pk in body:
            clean[_pk] = str(body[_pk] or "").strip()[:500]
    settings = comfyui.load_model_settings()
    # Merge, don't replace: Image Studio's Save defaults sends only sampling
    # keys and the Settings prompt fields send only prefix keys — each must
    # not wipe the other's saved values.
    settings[checkpoint] = {**(settings.get(checkpoint) or {}), **clean}
    comfyui.save_model_settings(settings)
    return {"status": "saved", "checkpoint": checkpoint, "settings": comfyui.settings_for_checkpoint(checkpoint)}


@app.delete("/api/images/model-settings/{checkpoint}")
async def clear_model_settings_ep(checkpoint: str):
    settings = comfyui.load_model_settings()
    if checkpoint not in settings:
        raise HTTPException(404, "No saved defaults for this model")
    settings.pop(checkpoint)
    comfyui.save_model_settings(settings)
    return {"status": "cleared", "settings": comfyui.settings_for_checkpoint(checkpoint)}


# NOTE: uses <IDEA> token replacement, not str.format — the JSON example's
# braces would otherwise need escaping and a stray { breaks .format at runtime.
_ENHANCE_PROMPT_TEMPLATE = """You are an expert Stable Diffusion XL prompt writer. Expand the user's idea into a high-quality SDXL generation prompt that stays tightly focused on the user's request.

Rules:
- Keep the user's subject and intent exactly — never replace or reinterpret the subject, and do not add people unless the user asked for them.
- Add concrete details that clarify the requested subject, pose, orientation, action, setting, materials, expression, lighting, color palette, and composition/camera. Prioritize details directly implied by the user's idea over generic style filler.
- If the user requests a specific pose, viewpoint, location, or activity, preserve it explicitly in the prompt. Do not turn a specific request into a generic portrait.
- Do not add unrelated props, phones, selfie framing, mirror framing, extra people, or extra actions unless the user asked for them.
- Write the positive prompt as comma-separated descriptive tags/phrases (roughly 40-90 words): request-specific subject details first, then scene/composition/lighting, then a few quality tags.
- Write a negative prompt of 5-15 short comma-separated tags: standard SDXL negatives plus anything that contradicts the user's idea. Never more than 15 tags, never prose.
- Both fields must be non-empty. No prose, no explanations. No Midjourney-style parameters (--ar, --v, --style) — SDXL does not understand them.

Example:
Idea: a fox in snow
{"prompt": "a red fox standing in deep fresh snow, winter forest clearing, fluffy orange fur with frost details, soft overcast daylight, gentle falling snowflakes, shallow depth of field, photorealistic wildlife photography, muted cool palette with warm orange accent, masterpiece, best quality, highly detailed, sharp focus", "negative_prompt": "lowres, bad anatomy, blurry, watermark, text, jpeg artifacts, worst quality, deformed, oversaturated"}

Now expand this idea. Respond with ONLY the JSON object, nothing else.
Idea: <IDEA>"""


@app.post("/api/images/enhance-prompt")
async def enhance_image_prompt(body: dict = Body(...)):
    """Expand a short user prompt into a detailed SDXL-style prompt via the
    local LLM. Pure LLM call — works even when ComfyUI is unconfigured."""
    idea = (body.get("prompt") or "").strip()[:600]
    if not idea:
        raise HTTPException(400, "prompt is required")
    model = (model_providers.reject_cloud((body.get("model") or "").strip())
             or model_providers.reject_cloud(config.IMAGE_CHAT_COMPOSE_MODEL or "")
             or model_providers.reject_cloud(config.WORKSPACE_MODEL or "")
             or config.DEFAULT_MODEL)
    raw = await model_providers.complete_chat(
        http, model, _ENHANCE_PROMPT_TEMPLATE.replace("<IDEA>", idea),
        temperature=0.7, num_ctx=2048, num_predict=400,
        format_json=True, timeout=45, ollama_url=config.OLLAMA_URL,
    )
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(502, "Prompt enhancer unavailable — check that Ollama is reachable")
    enhanced, negative = normalize_enhancer_response(raw)
    # Small models sometimes echo the schema with empty/placeholder values or
    # ignore JSON instructions entirely. Never fall back to raw text here,
    # because assistant reasoning becomes literal SDXL prompt tokens.
    if enhanced in ("", "...", "…"):
        raise HTTPException(502, "Prompt enhancer returned no usable prompt — try again")
    return {"prompt": enhanced[:1500], "negative_prompt": negative[:1500], "model": model}


@app.post("/api/images/purge")
async def purge_image_studio():
    """Delete ALL traces of the current user's generated images — Image Studio
    AND chat-tool generations: artifact rows, on-disk PNGs, chat file
    references, ComfyUI's job history and file copies (via the optional
    cleanup node), and the server's journald logs (which contain prompts)."""
    deleted_artifacts = 0
    deleted_files = 0
    purged_filenames: list[str] = []
    # Two-pass: gather every target first (offset pagination — the LIKE-based
    # `source` filter can fill a page with non-exact matches, so breaking on
    # an empty-target page would silently skip rows past it), then delete.
    targets: list[dict] = []
    for _src in ("image_studio", "generate_image"):
        offset = 0
        while True:
            page = await db.list_artifacts(kind="image", source=_src, limit=200, offset=offset)
            # The `source` filter is a metadata LIKE — re-check the exact tag.
            targets.extend(a for a in page if (a.get("metadata") or {}).get("source_tool") == _src)
            if len(page) < 200:
                break
            offset += 200
    for a in targets:
        result = await delete_artifact_row_and_file(a)
        if result["deleted"]:
            deleted_artifacts += 1
            if a.get("filename"):
                purged_filenames.append(a["filename"])
        if result["deleted_file"]:
            deleted_files += 1
        # Chat-tool images also leave a conversation_files attachment row —
        # keyed by its own cf- id (in metadata), NOT the artifact id
        try:
            _cf_id = (a.get("metadata") or {}).get("conversation_file_id") or ""
            if _cf_id:
                await db.delete_conversation_file(_cf_id)
        except Exception:
            pass
    # Rewrite chat messages that embedded the deleted images (inline markdown,
    # download links, seed footers, saved generate_image tool events).
    scrubbed_messages = 0
    try:
        scrubbed_messages = await db.scrub_image_traces(purged_filenames)
    except Exception as e:
        print(f"[IMAGE PURGE] message scrub failed: {e}")
    # Compact the DB so deleted rows leave no residual bytes in the file/WAL.
    if deleted_artifacts or scrubbed_messages:
        try:
            await db.vacuum_database()
        except Exception as e:
            print(f"[IMAGE PURGE] vacuum failed: {e}")
    # Drop finished jobs from the in-process registry so a stale poll can't
    # resurrect deleted image URLs. In-flight jobs stay.
    active = False
    for jid in list(_image_jobs.keys()):
        status = _image_jobs[jid].get("status")
        if status in ("done", "error"):
            _image_jobs.pop(jid, None)
        else:
            active = True
    # Chat-tool generations aren't in _image_jobs — comfyui._ACTIVE_JOBS
    # tracks every in-flight submit regardless of caller.
    if comfyui._ACTIVE_JOBS:
        active = True
    history_cleared = False
    comfyui_files = None
    cleanup_skipped = ""
    if not config.COMFYUI_URL:
        cleanup_skipped = "ComfyUI not configured"
    elif active:
        cleanup_skipped = "a generation is in flight"
    else:
        # Skip while a job is queued/running — clearing history mid-job would
        # lose the result before HyprChat's done-poll picks it up.
        history_cleared = bool(await comfyui.clear_history())
        comfyui_files = await comfyui.cleanup_outputs()
    # Scrub journald — historical backend log lines include generation
    # prompts. The service is unprivileged (User=hyprchat, NoNewPrivileges),
    # so the actual rotate+vacuum is done by a root path-unit helper
    # (scripts/install-journal-scrub.sh) watching for this trigger file.
    journal_cleared = False
    try:
        _trigger = os.path.join(os.path.dirname(config.SETTINGS_PATH), ".journal-scrub-request")
        with open(_trigger, "w") as f:
            f.write(datetime.utcnow().isoformat())
        for _ in range(20):  # helper consumes the trigger when done (~3s)
            await asyncio.sleep(0.5)
            if not os.path.exists(_trigger):
                journal_cleared = True
                break
        if not journal_cleared:
            # Helper not installed — don't leave a stale trigger behind
            try:
                os.remove(_trigger)
            except OSError:
                pass
    except Exception as e:
        print(f"[IMAGE PURGE] journal scrub request failed: {e}")
    notes = []
    if cleanup_skipped:
        notes.append(f"ComfyUI cleanup skipped ({cleanup_skipped}) — run Delete all again to clear its history/copies.")
    elif comfyui_files is None:
        notes.append("ComfyUI cleanup node not installed — its file copies remain "
                     "until the daily cron (install scripts/comfyui_hyprchat_cleanup.py).")
    if not journal_cleared:
        notes.append("Journal scrub helper not installed — old server log lines remain "
                     "(run scripts/install-journal-scrub.sh on the server once).")
    note = " ".join(notes) or "All traces removed."
    return {
        "status": "purged",
        "deleted_artifacts": deleted_artifacts,
        "deleted_files": deleted_files,
        "scrubbed_messages": scrubbed_messages,
        "comfyui_history_cleared": history_cleared,
        "comfyui_files_deleted": (comfyui_files or {}).get("deleted") if comfyui_files is not None else None,
        "journal_cleared": journal_cleared,
        "note": note,
    }


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


@app.post("/api/workspaces/{ws_id}/research-reports")
async def add_research_report_to_ws(ws_id: str, body: dict = Body(...)):
    report_id = body.get("report_id")
    if not report_id:
        raise HTTPException(400, "report_id is required")
    if not await db.get_workspace(ws_id):
        raise HTTPException(404, "Workspace not found")
    if not await db.get_research_report(report_id):
        raise HTTPException(404, "Research report not found")
    await db.add_research_report_to_workspace(ws_id, report_id)
    return {"ok": True}


@app.delete("/api/workspaces/{ws_id}/research-reports/{report_id}")
async def remove_research_report_from_ws(ws_id: str, report_id: str):
    await db.remove_research_report_from_workspace(ws_id, report_id)
    return {"ok": True}


def _extract_memory_json_array(text: str) -> list:
    raw = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _looks_like_secret_memory(text: str) -> bool:
    return bool(re.search(r"(password|api[_ -]?key|secret|private key|token)\s*[:=]", text or "", re.I))


@app.get("/api/memory/profile")
async def get_memory_profile_ep():
    return await db.get_user_profile()


@app.patch("/api/memory/profile")
async def update_memory_profile_ep(body: dict = Body(...)):
    return await db.update_user_profile(**body)


@app.get("/api/memory/memories")
async def list_global_memories_ep(
    status: str = Query("all"),
    type: str = Query("all"),
):
    memories = await db.list_global_memories(
        status=status,
        memory_type=type,
        include_archived=True,
    )
    return {"memories": memories}


@app.post("/api/memory/memories")
async def create_global_memory_ep(body: dict = Body(...)):
    try:
        mem = await db.create_global_memory(
            content=body.get("content", ""),
            memory_type=body.get("type", "semantic"),
            status=body.get("status", "suggested"),
            category=body.get("category", "General"),
            importance=body.get("importance", 3),
            pinned=body.get("pinned", 0),
            source_conv_id=body.get("source_conv_id") or body.get("source_conversation_id"),
            source_conversation_id=body.get("source_conversation_id") or body.get("source_conv_id"),
            source_message_id=body.get("source_message_id"),
            confidence=body.get("confidence", 0),
            valid_from=body.get("valid_from"),
            valid_until=body.get("valid_until"),
            supersedes_id=body.get("supersedes_id"),
            entities=body.get("entities") or [],
            metadata=body.get("metadata") or {},
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return mem


@app.patch("/api/memory/memories/{memory_id}")
async def update_global_memory_ep(memory_id: str, body: dict = Body(...)):
    mem = await db.update_global_memory(memory_id, **body)
    if not mem:
        raise HTTPException(404, "Memory not found")
    return mem


@app.delete("/api/memory/memories")
async def clear_all_memories_ep():
    deleted = await db.clear_user_memories()
    return {"ok": True, "deleted": deleted}


@app.delete("/api/memory/memories/{memory_id}")
async def delete_global_memory_ep(memory_id: str):
    ok = await db.delete_global_memory(memory_id)
    if not ok:
        raise HTTPException(404, "Memory not found")
    return {"ok": True}


@app.post("/api/memory/memories/{memory_id}/accept")
async def accept_global_memory_ep(memory_id: str, body: dict = Body(default={})):
    mem = await db.accept_global_memory(memory_id, supersedes_id=body.get("supersedes_id"))
    if not mem:
        raise HTTPException(404, "Memory not found")
    return mem


@app.post("/api/memory/memories/{memory_id}/reject")
async def reject_global_memory_ep(memory_id: str):
    mem = await db.reject_global_memory(memory_id)
    if not mem:
        raise HTTPException(404, "Memory not found")
    return mem


@app.post("/api/memory/suggest")
async def suggest_global_memories_ep(body: dict = Body(default={})):
    conv_id = body.get("conversation_id")
    conv_refs = []
    if conv_id:
        conv_refs = [{"id": conv_id}]
    else:
        conv_refs = [
            c for c in await db.get_conversations(limit=20)
            if str(c.get("use_memories") or "0").lower() in {"1", "true", "yes", "on"}
        ][:4]

    transcript_parts = []
    for conv_ref in conv_refs:
        conv = await db.get_conversation(conv_ref.get("id", ""))
        if not conv:
            continue
        bits = [f"Conversation: {conv.get('title') or conv.get('id')}"]
        for msg in (conv.get("messages") or [])[-10:]:
            if msg.get("role") not in {"user", "assistant"}:
                continue
            meta = msg.get("metadata") or {}
            if meta.get("persona_first_message"):
                continue
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            role = "User" if msg.get("role") == "user" else "Assistant"
            bits.append(f"{role}: {content[:2500]}")
        if len(bits) > 1:
            transcript_parts.append("\n".join(bits))

    transcript = "\n\n---\n\n".join(transcript_parts)[:18000]
    if len(transcript) < 80:
        return {"created": 0, "memories": [], "message": "No recent memory-enabled chat content to scan."}

    profile = await db.get_user_profile()
    profile_hint = json.dumps({
        "display_name": profile.get("display_name", ""),
        "interests": profile.get("interests", []),
        "bio": profile.get("bio", ""),
    }, ensure_ascii=False)[:2000]
    model = body.get("model") or getattr(config, "WORKSPACE_MODEL", "") or config.DEFAULT_MODEL
    prompt = (
        "You extract useful long-term personal memories for a cross-chat AI assistant.\n"
        "Return ONLY a JSON array with 0-8 objects. Do not include markdown.\n"
        "Each object shape:\n"
        "{\"type\":\"semantic|episodic|procedural\",\"content\":\"one durable memory\","
        "\"importance\":1-5,\"confidence\":0-1,\"entities\":[\"short names\"],"
        "\"reason\":\"why it should be remembered\"}\n\n"
        "Rules:\n"
        "- Suggest only durable information useful across many future chats.\n"
        "- Good semantic memories include user preferences, personal background, important people, birthdays, interests, names, and stable constraints.\n"
        "- Good episodic memories include dated user-confirmed events, decisions, or outcomes.\n"
        "- Good procedural memories include reusable workflows or recurring user instructions.\n"
        "- Do not save secrets, credentials, private keys, passwords, raw tokens, or credential-bearing URLs.\n"
        "- Do not infer sensitive facts; only save what the user clearly states or confirms.\n"
        "- Do not save generic assistant claims or one-off trivia.\n"
        "- Prefer fewer high-value memories over many weak ones.\n\n"
        f"Existing user profile summary: {profile_hint}\n\n{transcript}"
    )

    try:
        r = await http.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.1, "num_ctx": _WORKSPACE_HELPER_NUM_CTX, "num_predict": 900},
            },
            timeout=60,
        )
        if r.status_code != 200:
            detail = r.text[:240] if r.text else f"HTTP {r.status_code}"
            raise HTTPException(r.status_code, f"Ollama error ({model}): {detail}")
        suggestions = _extract_memory_json_array(r.json().get("response", ""))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Memory scan failed: {e}")

    existing = await db.list_global_memories(status="all")
    existing_norm = {re.sub(r"\s+", " ", (m.get("content") or "").strip().lower()) for m in existing}
    created = []
    for item in suggestions[:8]:
        if not isinstance(item, dict):
            continue
        content = re.sub(r"\s+", " ", str(item.get("content") or "").strip())
        if len(content) < 12 or len(content) > 800:
            continue
        norm = content.lower()
        if norm in existing_norm or _looks_like_secret_memory(content):
            continue
        mem = await db.create_global_memory(
            content=content,
            memory_type=item.get("type", "semantic"),
            status="suggested",
            importance=item.get("importance", 3),
            source_conv_id=conv_id,
            source_conversation_id=conv_id,
            confidence=item.get("confidence", 0),
            entities=item.get("entities") if isinstance(item.get("entities"), list) else [],
            metadata={"reason": item.get("reason", ""), "suggested_by": "global_scan", "model": model},
        )
        existing_norm.add(norm)
        created.append(mem)

    return {"created": len(created), "memories": created}


@app.get("/api/workspaces/{ws_id}/memories")
async def list_workspace_memories_ep(
    ws_id: str,
    status: str = Query("all"),
    type: str = Query("all"),
):
    if not await db.get_workspace_basic(ws_id):
        raise HTTPException(404, "Workspace not found")
    memories = await db.list_workspace_memories(
        ws_id,
        status=status,
        memory_type=type,
        include_archived=True,
    )
    return {"memories": memories}


@app.post("/api/workspaces/{ws_id}/memories/suggest")
async def suggest_workspace_memories_ep(ws_id: str, body: dict = Body(default={})):
    ws = await db.get_workspace(ws_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")

    conv_id = body.get("conversation_id")
    conv_refs = []
    if conv_id:
        conv_refs = [{"id": conv_id}]
    else:
        conv_refs = sorted(
            ws.get("conversations", []),
            key=lambda c: c.get("updated_at") or "",
            reverse=True,
        )[:4]

    transcript_parts = []
    for conv_ref in conv_refs:
        conv = await db.get_conversation(conv_ref.get("id", ""))
        if not conv:
            continue
        bits = [f"Conversation: {conv.get('title') or conv.get('id')}"]
        for msg in (conv.get("messages") or [])[-8:]:
            if msg.get("role") not in {"user", "assistant"}:
                continue
            meta = msg.get("metadata") or {}
            if meta.get("persona_first_message"):
                continue
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            role = "User" if msg.get("role") == "user" else "Assistant"
            bits.append(f"{role}: {content[:2500]}")
        if len(bits) > 1:
            transcript_parts.append("\n".join(bits))

    transcript = "\n\n---\n\n".join(transcript_parts)[:18000]
    if len(transcript) < 80:
        return {"created": 0, "memories": [], "message": "No recent workspace chat content to scan."}

    model = body.get("model") or getattr(config, "WORKSPACE_MODEL", "") or config.DEFAULT_MODEL
    prompt = (
        "You extract useful long-term workspace memories from recent chat transcript.\n"
        "Return ONLY a JSON array with 0-8 objects. Do not include markdown.\n"
        "Each object shape:\n"
        "{\"type\":\"semantic|episodic|procedural\",\"content\":\"one durable memory\","
        "\"importance\":1-5,\"confidence\":0-1,\"entities\":[\"short names\"],"
        "\"reason\":\"why it should be remembered\"}\n\n"
        "Rules:\n"
        "- Suggest only durable, reusable information for this workspace.\n"
        "- semantic = stable facts/preferences/constraints.\n"
        "- episodic = dated decisions, outcomes, blockers, or events.\n"
        "- procedural = repeatable workflows, commands, deployment steps, lessons learned.\n"
        "- Do not save secrets, credentials, private keys, passwords, or raw tokens.\n"
        "- Do not save generic facts that are obvious from the chat app.\n"
        "- Prefer fewer high-value memories over many weak ones.\n\n"
        f"Workspace: {ws.get('name') or ws_id}\n\n{transcript}"
    )

    try:
        r = await http.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.1, "num_ctx": _WORKSPACE_HELPER_NUM_CTX, "num_predict": 900},
            },
            timeout=60,
        )
        if r.status_code != 200:
            detail = r.text[:240] if r.text else f"HTTP {r.status_code}"
            raise HTTPException(r.status_code, f"Ollama error ({model}): {detail}")
        suggestions = _extract_memory_json_array(r.json().get("response", ""))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Memory scan failed: {e}")

    existing = await db.list_workspace_memories(ws_id, status="all")
    existing_norm = {re.sub(r"\s+", " ", (m.get("content") or "").strip().lower()) for m in existing}
    created = []
    for item in suggestions[:8]:
        if not isinstance(item, dict):
            continue
        content = re.sub(r"\s+", " ", str(item.get("content") or "").strip())
        if len(content) < 12 or len(content) > 800:
            continue
        norm = content.lower()
        if norm in existing_norm:
            continue
        if re.search(r"(password|api[_ -]?key|secret|private key|token)\s*[:=]", content, re.I):
            continue
        mem = await db.create_workspace_memory(
            ws_id,
            content=content,
            memory_type=item.get("type", "semantic"),
            status="suggested",
            importance=item.get("importance", 3),
            source_conv_id=conv_id,
            source_conversation_id=conv_id,
            confidence=item.get("confidence", 0),
            entities=item.get("entities") if isinstance(item.get("entities"), list) else [],
            metadata={"reason": item.get("reason", ""), "suggested_by": "workspace_scan", "model": model},
        )
        existing_norm.add(norm)
        created.append(mem)

    return {"created": len(created), "memories": created}


@app.post("/api/workspaces/{ws_id}/memories")
async def create_workspace_memory_ep(ws_id: str, body: dict = Body(...)):
    if not await db.get_workspace_basic(ws_id):
        raise HTTPException(404, "Workspace not found")
    try:
        mem = await db.create_workspace_memory(
            ws_id,
            content=body.get("content", ""),
            memory_type=body.get("type", "semantic"),
            status=body.get("status", "suggested"),
            category=body.get("category", "General"),
            importance=body.get("importance", 3),
            pinned=body.get("pinned", 0),
            source_conv_id=body.get("source_conv_id") or body.get("source_conversation_id"),
            source_conversation_id=body.get("source_conversation_id") or body.get("source_conv_id"),
            source_message_id=body.get("source_message_id"),
            confidence=body.get("confidence", 0),
            valid_from=body.get("valid_from"),
            valid_until=body.get("valid_until"),
            supersedes_id=body.get("supersedes_id"),
            entities=body.get("entities") or [],
            metadata=body.get("metadata") or {},
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return mem


@app.patch("/api/workspaces/{ws_id}/memories/{memory_id}")
async def update_workspace_memory_ep(ws_id: str, memory_id: str, body: dict = Body(...)):
    mem = await db.update_workspace_memory(memory_id, ws_id, **body)
    if not mem:
        raise HTTPException(404, "Memory not found")
    return mem


@app.delete("/api/workspaces/{ws_id}/memories/{memory_id}")
async def delete_workspace_memory_ep(ws_id: str, memory_id: str):
    ok = await db.delete_workspace_memory(memory_id, ws_id)
    if not ok:
        raise HTTPException(404, "Memory not found")
    return {"ok": True}


@app.post("/api/workspaces/{ws_id}/memories/{memory_id}/accept")
async def accept_workspace_memory_ep(ws_id: str, memory_id: str, body: dict = Body(default={})):
    mem = await db.accept_workspace_memory(memory_id, ws_id, supersedes_id=body.get("supersedes_id"))
    if not mem:
        raise HTTPException(404, "Memory not found")
    return mem


@app.post("/api/workspaces/{ws_id}/memories/{memory_id}/reject")
async def reject_workspace_memory_ep(ws_id: str, memory_id: str):
    mem = await db.reject_workspace_memory(memory_id, ws_id)
    if not mem:
        raise HTTPException(404, "Memory not found")
    return mem


@app.get("/api/workspaces/{ws_id}/memory-blocks")
async def get_workspace_memory_blocks_ep(ws_id: str):
    if not await db.get_workspace_basic(ws_id):
        raise HTTPException(404, "Workspace not found")
    return {"blocks": await db.get_workspace_memory_blocks(ws_id)}


@app.patch("/api/workspaces/{ws_id}/memory-blocks")
async def update_workspace_memory_blocks_ep(ws_id: str, body: dict = Body(...)):
    if not await db.get_workspace_basic(ws_id):
        raise HTTPException(404, "Workspace not found")
    blocks = await db.update_workspace_memory_blocks(ws_id, body.get("blocks") or [])
    return {"blocks": blocks}


@app.post("/api/workspaces/{ws_id}/analyze")
async def analyze_workspace_topics(ws_id: str, body: dict = Body(default={})):
    ws = await db.get_workspace(ws_id)
    if not ws:
        raise HTTPException(404)
    titles = [c["title"] for c in ws.get("conversations", []) if c.get("title")]
    titles += [r.get("title") or r.get("query", "") for r in ws.get("reports", []) if r.get("title") or r.get("query")]
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
            json={
                "model": ws_model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0.2,
                    "num_ctx": _WORKSPACE_HELPER_NUM_CTX,
                    "num_predict": 400,
                },
            },
            timeout=60
        )
        if r.status_code != 200:
            detail = r.text[:200] if r.text else f"HTTP {r.status_code}"
            raise HTTPException(r.status_code, f"Ollama error ({ws_model}): {detail}")
        raw = r.json().get("response", "[]")
        import re as _re
        raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
        start, end = raw.find("["), raw.rfind("]")
        topics = json.loads(raw[start:end + 1]) if start != -1 and end > start else []
    except HTTPException:
        raise
    except Exception as e:
        print(f"[Analyze] {e}")
        raise HTTPException(500, f"Analysis failed: {e}")
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
    if total < MAX:
        for report_meta in ws.get("reports", []):
            report = await db.get_research_report(report_meta["id"])
            if not report:
                continue
            parts.append(f"\n\n=== Research Report: {report.get('title') or report.get('query', 'Untitled')} ===")
            report_bits = [
                f"Query: {report.get('query', '')}",
                f"Type: {report.get('report_type', '')}",
                f"Summary: {report.get('summary', '')}",
                (report.get("report_markdown") or "")[:8000],
            ]
            chunk = "\n".join(bit for bit in report_bits if bit).strip()
            parts.append("\n" + chunk)
            total += len(chunk)
            if total >= MAX:
                parts.append("\n[...truncated...]")
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
    await db.add_council_member(member_id, council_id, req.model, req.system_prompt, req.persona_name, req.model_config_id)
    return {"id": member_id, "council_id": council_id, "model": req.model,
            "model_config_id": req.model_config_id, "system_prompt": req.system_prompt,
            "persona_name": req.persona_name, "points": 0}


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
            {"persona_key": "socrates", "persona_name": "Socrates"},
            {"persona_key": "aristotle", "persona_name": "Aristotle"},
            {"persona_key": "nietzsche", "persona_name": "Nietzsche"},
            {"persona_key": "confucius", "persona_name": "Confucius"},
            {"persona_key": "beauvoir", "persona_name": "Simone de Beauvoir"},
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
        if preset == "philosophers" and m.get("persona_key"):
            persona = await _ensure_philosopher_persona(m["persona_key"])
            await db.add_council_member(
                member_id,
                council_id,
                persona.get("base_model") or config.DEFAULT_MODEL,
                persona.get("system_prompt") or "",
                persona.get("name") or m["persona_name"],
                persona.get("id"),
            )
            continue

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
            "think": False,
            "options": {
                "temperature": 0.9,
                "num_ctx": _WORKSPACE_HELPER_NUM_CTX,
                "num_predict": 200,
            },
        }, timeout=30)
        data = r.json()
        if "error" in data:
            print(f"[COUNCIL] Suggestions model error: {data['error']}")
            return {"suggestions": []}
        msg = data["message"]
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
    user_id = db.current_user_id()

    async def _stream():
        token = db.set_current_user_id(user_id)
        try:
            async for chunk in stream_council_chat(http, events, council, req.messages, req.conversation_id, req.quick_search, kb_ids=kb_ids):
                yield chunk
        finally:
            db.reset_current_user_id(token)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ============================================================
# QUICK SEARCH
# ============================================================
@app.get("/api/img-proxy")
async def img_proxy(u: str):
    """Proxy a remote image so the user's browser never hits the source.
    Hides user IP/referrer/cookies from third-party servers, bypasses most
    hotlink protection (we send a domain-matched Referer), and resolves
    mixed-content warnings for http images on https pages.
    """
    parsed = urllib.parse.urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="bad url")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; image-proxy)",
        "Accept": "image/*",
        "Referer": f"{parsed.scheme}://{parsed.netloc}/",
    }
    try:
        status, resp_headers, _final_url, content = await fetch_bytes_safely(
            http, u, timeout=8, headers=headers, max_bytes=5 * 1024 * 1024
        )
    except ValueError as e:
        detail = str(e)
        if "too large" in detail.lower():
            raise HTTPException(status_code=413, detail="too large")
        raise HTTPException(status_code=400, detail=detail)
    except Exception:
        raise HTTPException(status_code=502, detail="fetch failed")
    ct = resp_headers.get("content-type", "")
    if status != 200 or not ct.lower().startswith("image/"):
        raise HTTPException(status_code=404, detail="not found")
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="too large")
    return Response(
        content=content, media_type=ct,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/api/quick-search")
async def quick_search(req: QuickSearchRequest):
    """Fast web search via SearXNG — returns structured results with type detection
    and OG-image enrichment for the frontend carousel. Delegates to the shared
    helper in `quick_search.py`.
    """
    try:
        return await qs_module.run_quick_search_for_api(http, req.query, count=req.count)
    except Exception as e:
        return {"results": [], "query": req.query, "error": str(e)}


# ============================================================
# SETTINGS & SANDBOX API
# ============================================================




# ============================================================
# FULL-TEXT CONVERSATION SEARCH
# ============================================================
@app.post("/api/conversations/search")
async def search_conversations(req: ConversationSearchRequest):
    if not req.query.strip():
        return []
    return await db.search_messages(req.query.strip(), req.limit)


# ============================================================
# CONVERSATION FORKING
# ============================================================
@app.post("/api/conversations/{conv_id}/fork")
async def fork_conversation(conv_id: str, req: ForkRequest):
    original = await db.get_conversation(conv_id)
    if not original:
        raise HTTPException(404, "Conversation not found")
    new_id = f"conv-{uuid.uuid4().hex[:12]}"
    forked = await db.fork_conversation(conv_id, req.message_id, new_id)
    if not forked:
        raise HTTPException(500, "Fork failed")
    return forked


@app.get("/api/conversations/{conv_id}/forks")
async def get_conversation_forks(conv_id: str):
    return await db.get_forks(conv_id)


# ============================================================
# AUTO-TITLE GENERATION
# ============================================================
@app.post("/api/daily-message")
async def generate_daily_message(body: dict = Body(default={})):
    model = body.get("model") or config.WORKSPACE_MODEL or config.DEFAULT_MODEL
    fallback = "Give the machine a worthy puzzle."
    try:
        resp = await http.post(f"{config.OLLAMA_URL}/api/chat", json={
            "model": model,
            "messages": [
                {"role": "system", "content": (
                    "Write one short welcome tagline for HyprChat's empty new-chat screen. "
                    "Tone: clever, playful, curious, a little mischievous, but still useful. "
                    "Prefer concrete verbs and odd-but-smart imagery over generic productivity slogans. "
                    "3-9 words. No quotes, no emoji, no markdown, no brand name, no period unless needed."
                )},
                {"role": "user", "content": f"Today is {time.strftime('%A, %B %d, %Y')}. Generate one fresh, memorable line."}
            ],
            "stream": False,
            "think": False,
            "options": {"num_ctx": 1024, "temperature": 1.05}
        }, timeout=20)
        msg = resp.json().get("message", {}).get("content", "").strip()
        msg = re.sub(r"^[\"'`“”]+|[\"'`“”]+$", "", msg)
        msg = re.sub(r"\s+", " ", msg.splitlines()[0] if msg else "").strip()
        msg = re.sub(r"^(tagline|line)\s*:\s*", "", msg, flags=re.I).strip()
        if not msg or len(msg) > 90:
            msg = fallback
        return {"message": msg, "model": model}
    except Exception as e:
        print(f"[DAILY-MESSAGE] Error: {e}")
        return {"message": fallback, "model": model, "fallback": True}


@app.post("/api/conversations/{conv_id}/generate-title")
async def generate_title(conv_id: str, body: dict = Body(default={})):
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    msgs = conv.get("messages", [])
    first_msgs = [m for m in msgs[:4] if m.get("role") in ("user", "assistant")]
    if not first_msgs:
        return {"title": ""}
    summary_input = "\n".join(f"{m['role']}: {(m.get('content') or '')[:200]}" for m in first_msgs)
    title_model = body.get("model") or config.WORKSPACE_MODEL or config.DEFAULT_MODEL
    try:
        resp = await http.post(f"{config.OLLAMA_URL}/api/chat", json={
            "model": title_model,
            "messages": [
                {"role": "system", "content": "Generate a concise title (5-8 words, no quotes, no punctuation at end) for this conversation. Reply with ONLY the title, nothing else."},
                {"role": "user", "content": summary_input}
            ],
            "stream": False,
            "think": False,
            "options": {"num_ctx": _WORKSPACE_TITLE_NUM_CTX, "temperature": 0.3}
        }, timeout=30)
        title = resp.json().get("message", {}).get("content", "").strip().strip('"\'')[:60]
        if title:
            await db.update_conversation(conv_id, title=title)
            return {"title": title}
        return {"title": ""}
    except Exception as e:
        print(f"[AUTO-TITLE] Error: {e}")
        return {"title": ""}


# ============================================================
# SERVE FRONTEND (production)
# ============================================================
class _FrontendStaticFiles(StaticFiles):
    """StaticFiles with deploy-safe cache headers.

    Vite assets are content-hashed, so they can be cached forever — but
    index.html must NOT be cached: deploys delete the old hashed assets, and a
    browser holding a stale cached index.html would request chunks that no
    longer exist (blank app until hard refresh).
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        path = str(getattr(resp, "path", "") or "")
        if "/assets/" in path.replace(os.sep, "/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.isfile(os.path.join(frontend_dir, "index.html")):
    app.mount("/", _FrontendStaticFiles(directory=frontend_dir, html=True), name="frontend")
else:
    print("[STARTUP] WARNING: frontend/dist/index.html not found — the UI will not be served.")
    print("[STARTUP]          frontend/dist/ is build output (not committed). Build it with:")
    print("[STARTUP]              cd frontend && npm install && npm run build")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=config.DEBUG)
