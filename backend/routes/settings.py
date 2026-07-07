"""Runtime settings, cleanup, RAG stats, changelog, and analytics routes."""
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query

import comfyui
import config
import database as db
import hyprfit
import model_providers
import rag
import voice

from .context import route_context


router = APIRouter()


def load_settings() -> dict:
    return route_context().load_settings()


def save_settings(settings: dict) -> None:
    route_context().save_settings(settings)


def _public_settings_payload(settings: dict) -> dict:
    return route_context().public_settings_payload(settings)


def _coerce_service_url(value: str, env_key: str, default: str) -> str:
    return route_context().coerce_service_url(value, env_key, default)


def _sandbox_size_bytes() -> int:
    return route_context().sandbox_size_bytes()


@router.get("/api/settings")
async def get_app_settings():
    settings = load_settings()
    size = _sandbox_size_bytes()
    venv_exists = os.path.exists(os.path.join(config.SANDBOX_VENV_DIR, "bin", "python"))
    try:
        file_count = sum(1 for e in os.scandir(config.SANDBOX_OUTPUTS_DIR) if e.is_file())
    except Exception:
        file_count = 0
    return {
        **_public_settings_payload(settings),
        "current_ollama_url": config.OLLAMA_URL,
        "current_codebox_url": config.CODEBOX_URL,
        "current_searxng_url": config.SEARXNG_URL,
        "current_n8n_url": config.N8N_URL,
        "current_comfyui_url": config.COMFYUI_URL,
        "current_stt_url": config.STT_URL,
        "current_tts_url": config.TTS_URL,
        "current_tts_voice": config.TTS_VOICE,
        "current_planning_model": config.PLANNING_MODEL,
        "current_coder_model": config.CODER_MODEL,
        "current_workspace_model": config.WORKSPACE_MODEL,
        "context_compaction": config.CONTEXT_COMPACTION,
        "history_recall": config.HISTORY_RECALL,
        "model_routing": config.MODEL_ROUTING,
        # Coder Bot v2 per-agent overrides — empty string = inherit from umbrella
        "current_architect_model": config.ARCHITECT_MODEL,
        "current_reviewer_model":  config.REVIEWER_MODEL,
        "current_acceptance_model": config.ACCEPTANCE_MODEL,
        "current_builder_model":   config.BUILDER_MODEL,
        "current_fixer_model":     config.FIXER_MODEL,
        "current_qa_model":        config.QA_MODEL,
        "openhands_enabled": config.OPENHANDS_ENABLED,
        "openhands_max_rounds": config.OPENHANDS_MAX_ROUNDS,
        "openhands_num_ctx": config.OPENHANDS_NUM_CTX,
        "openhands_reasoning_effort": config.OPENHANDS_REASONING_EFFORT,
        "openhands_disable_thinking": config.OPENHANDS_DISABLE_THINKING,
        "aider_enabled": config.AIDER_ENABLED,
        "aider_model": config.AIDER_MODEL,
        "aider_num_ctx": config.AIDER_NUM_CTX,
        "aider_auto_test": config.AIDER_AUTO_TEST,
        "aider_worker_url": config.AIDER_WORKER_URL,
        "default_num_ctx": config.DEFAULT_NUM_CTX,
        "research_num_ctx": config.RESEARCH_NUM_CTX,
        "quick_search_mode": config.QUICK_SEARCH_MODE,
        "image_chat_checkpoint": config.IMAGE_CHAT_CHECKPOINT,
        "image_chat_workflow": config.IMAGE_CHAT_WORKFLOW,
        "image_chat_resolution": config.IMAGE_CHAT_RESOLUTION,
        "image_chat_vae": config.IMAGE_CHAT_VAE,
        "image_chat_prompt_prefix": config.IMAGE_CHAT_PROMPT_PREFIX,
        "image_chat_negative": config.IMAGE_CHAT_NEGATIVE,
        "image_chat_compose_model": config.IMAGE_CHAT_COMPOSE_MODEL,
        "image_chat_auto_enhance": config.IMAGE_CHAT_AUTO_ENHANCE,
        "model_hardware_profile": hyprfit.clean_hardware_profile(settings.get("model_hardware_profile")),
        "sandbox_dir": config.SANDBOX_DIR,
        "sandbox_outputs_dir": config.SANDBOX_OUTPUTS_DIR,
        "sandbox_size_bytes": size,
        "sandbox_file_count": file_count,
        "sandbox_venv_ready": venv_exists,
    }


@router.patch("/api/settings")
async def update_app_settings(body: dict = Body(...)):
    settings = load_settings()
    allowed = {"file_cleanup_days", "ollama_url", "codebox_url", "searxng_url", "n8n_url",
               "comfyui_url", "stt_url", "tts_url", "tts_voice",
               "rag", "planning_model", "coder_model",
               "workspace_model", "context_compaction", "history_recall", "model_routing",
               "architect_model", "reviewer_model", "acceptance_model", "builder_model", "fixer_model", "qa_model",
               "openhands_enabled", "openhands_max_rounds", "openhands_num_ctx",
               "openhands_reasoning_effort", "openhands_disable_thinking",
               "aider_enabled", "aider_model", "aider_num_ctx", "aider_auto_test", "aider_worker_url",
               "default_num_ctx", "research_num_ctx", "quick_search_mode",
               "image_chat_checkpoint", "image_chat_workflow", "image_chat_resolution", "image_chat_vae",
               "image_chat_prompt_prefix", "image_chat_negative", "image_chat_compose_model",
               "image_chat_auto_enhance",
               "ollama_scan_ssh_host", "ollama_scan_ssh_port", "ollama_scan_ssh_user",
               "ollama_scan_ssh_auth_mode", "ollama_scan_ssh_key_path", "ollama_scan_ssh_password",
               "model_hardware_profile"}
    for k, v in body.items():
        if k in allowed:
            settings[k] = v
    if any(k.startswith("ollama_scan_ssh_") for k in body):
        ssh_cfg = hyprfit.clean_ollama_scan_ssh_settings(settings, config.OLLAMA_URL, include_password=True)
        settings["ollama_scan_ssh_host"] = ssh_cfg["ollama_scan_ssh_host"]
        settings["ollama_scan_ssh_port"] = ssh_cfg["ollama_scan_ssh_port"]
        settings["ollama_scan_ssh_user"] = ssh_cfg["ollama_scan_ssh_user"]
        settings["ollama_scan_ssh_auth_mode"] = ssh_cfg["ollama_scan_ssh_auth_mode"]
        settings["ollama_scan_ssh_key_path"] = ssh_cfg["ollama_scan_ssh_key_path"]
        if "ollama_scan_ssh_password" in body:
            settings["ollama_scan_ssh_password"] = ssh_cfg.get("ollama_scan_ssh_password", "")
    # Sanitize + apply RAG settings. The clamped values are what get PERSISTED
    # (settings["rag"]), not the raw body — a junk PATCH (e.g. chunk_size -5)
    # used to be saved verbatim and re-poison the chunker on every restart.
    if "rag" in body and isinstance(body["rag"], dict):
        rag_cfg = body["rag"]
        prior = {**config.DEFAULT_SETTINGS.get("rag", {}), **(settings.get("rag") or {})}

        def _rag_int(key, default, lo, hi):
            fallback = config.coerce_int(prior.get(key), default, minimum=lo, maximum=hi)
            value = rag_cfg.get(key, fallback)
            return config.coerce_int(value, fallback, minimum=lo, maximum=hi)

        clean = {
            "embed_model": str(rag_cfg.get("embed_model") or prior.get("embed_model") or "nomic-embed-text"),
            "chunk_size": _rag_int("chunk_size", 500, 100, 8000),
            "chunk_overlap": _rag_int("chunk_overlap", 50, 0, 2000),
            "top_k": _rag_int("top_k", 6, 1, 20),
            "max_context_chars": _rag_int("max_context_chars", 6000, 500, 60000),
            "research_top_k": _rag_int("research_top_k", 4, 1, 20),
            "research_max_chars": _rag_int("research_max_chars", 3000, 500, 60000),
        }
        settings["rag"] = clean
        rag.EMBED_MODEL = clean["embed_model"]
        rag.CHUNK_SIZE = clean["chunk_size"]
        rag.CHUNK_OVERLAP = clean["chunk_overlap"]
        print(f"[Config] Updated RAG settings: model={rag.EMBED_MODEL} chunk={rag.CHUNK_SIZE}/{rag.CHUNK_OVERLAP}")
    if "ollama_url" in body and body["ollama_url"]:
        config.OLLAMA_URL = _coerce_service_url(body["ollama_url"], "OLLAMA_URL", "http://127.0.0.1:11434")
        settings["ollama_url"] = config.OLLAMA_URL
        print(f"[Config] Updated Ollama URL to: {config.OLLAMA_URL}")
    elif "ollama_url" in body and not body["ollama_url"]:
        settings["ollama_url"] = ""
        config.OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    if "codebox_url" in body and body["codebox_url"]:
        config.CODEBOX_URL = _coerce_service_url(body["codebox_url"], "CODEBOX_URL", "http://127.0.0.1:8585")
        settings["codebox_url"] = config.CODEBOX_URL
        print(f"[Config] Updated Codebox URL to: {config.CODEBOX_URL}")
    elif "codebox_url" in body and not body["codebox_url"]:
        settings["codebox_url"] = ""
        config.CODEBOX_URL = os.getenv("CODEBOX_URL", "http://127.0.0.1:8585")
    if "searxng_url" in body and body["searxng_url"]:
        config.SEARXNG_URL = _coerce_service_url(body["searxng_url"], "SEARXNG_URL", "http://127.0.0.1:8888")
        settings["searxng_url"] = config.SEARXNG_URL
        print(f"[Config] Updated SearXNG URL to: {config.SEARXNG_URL}")
    elif "searxng_url" in body and not body["searxng_url"]:
        settings["searxng_url"] = ""
        config.SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8888")
    if "n8n_url" in body and body["n8n_url"]:
        config.N8N_URL = _coerce_service_url(body["n8n_url"], "N8N_URL", "http://127.0.0.1:5678")
        settings["n8n_url"] = config.N8N_URL
        print(f"[Config] Updated N8N URL to: {config.N8N_URL}")
    elif "n8n_url" in body and not body["n8n_url"]:
        settings["n8n_url"] = ""
        config.N8N_URL = os.getenv("N8N_URL", "http://127.0.0.1:5678")
    if "comfyui_url" in body and body["comfyui_url"]:
        config.COMFYUI_URL = _coerce_service_url(body["comfyui_url"], "COMFYUI_URL", "")
        settings["comfyui_url"] = config.COMFYUI_URL
        print(f"[Config] Updated ComfyUI URL to: {config.COMFYUI_URL}")
    elif "comfyui_url" in body and not body["comfyui_url"]:
        settings["comfyui_url"] = ""
        config.COMFYUI_URL = os.getenv("COMFYUI_URL", "")
    if "stt_url" in body and body["stt_url"]:
        config.STT_URL = _coerce_service_url(body["stt_url"], "STT_URL", "")
        settings["stt_url"] = config.STT_URL
        print(f"[Config] Updated STT URL to: {config.STT_URL}")
    elif "stt_url" in body and not body["stt_url"]:
        settings["stt_url"] = ""
        config.STT_URL = os.getenv("STT_URL", "")
    if "tts_url" in body and body["tts_url"]:
        config.TTS_URL = _coerce_service_url(body["tts_url"], "TTS_URL", "")
        settings["tts_url"] = config.TTS_URL
        voice.clear_voice_cache()
        print(f"[Config] Updated TTS URL to: {config.TTS_URL}")
    elif "tts_url" in body and not body["tts_url"]:
        settings["tts_url"] = ""
        config.TTS_URL = os.getenv("TTS_URL", "")
        voice.clear_voice_cache()
    if "tts_voice" in body:
        config.TTS_VOICE = str(body["tts_voice"] or os.getenv("TTS_VOICE", "af_heart"))
        settings["tts_voice"] = config.TTS_VOICE
    if "planning_model" in body:
        config.PLANNING_MODEL = body["planning_model"] or ""
        print(f"[Config] Updated Planning Model to: {config.PLANNING_MODEL or '(use chat model)'}")
    if "coder_model" in body:
        config.CODER_MODEL = body["coder_model"] or ""
        print(f"[Config] Updated Coder Model to: {config.CODER_MODEL or '(use orchestrator model)'}")
    if "workspace_model" in body:
        config.WORKSPACE_MODEL = body["workspace_model"] or ""
        print(f"[Config] Updated Workspace Model to: {config.WORKSPACE_MODEL or '(use chat model)'}")
    if "context_compaction" in body:
        config.CONTEXT_COMPACTION = "on" if str(body["context_compaction"]).lower() in {"1", "true", "on", "yes"} else "off"
        settings["context_compaction"] = config.CONTEXT_COMPACTION
        print(f"[Config] Updated context compaction: {config.CONTEXT_COMPACTION}")
    if "history_recall" in body:
        config.HISTORY_RECALL = "on" if str(body["history_recall"]).lower() in {"1", "true", "on", "yes"} else "off"
        settings["history_recall"] = config.HISTORY_RECALL
        print(f"[Config] Updated history recall: {config.HISTORY_RECALL}")
    if "model_routing" in body and isinstance(body["model_routing"], dict):
        _mr = body["model_routing"]
        _clean_mr = {
            "enabled": bool(_mr.get("enabled")),
            "chat": str(_mr.get("chat") or ""),
            "code": str(_mr.get("code") or ""),
            "reasoning": str(_mr.get("reasoning") or ""),
            "long_context": str(_mr.get("long_context") or ""),
        }
        config.MODEL_ROUTING = _clean_mr
        settings["model_routing"] = _clean_mr
        print(f"[Config] Updated model routing: {_clean_mr}")
    # Coder Bot v2 per-agent overrides
    for _key, _attr, _label in (
        ("architect_model", "ARCHITECT_MODEL", "Architect"),
        ("reviewer_model",  "REVIEWER_MODEL",  "Reviewer"),
        ("acceptance_model", "ACCEPTANCE_MODEL", "Acceptance"),
        ("builder_model",   "BUILDER_MODEL",   "Builder"),
        ("fixer_model",     "FIXER_MODEL",     "Fixer"),
        ("qa_model",        "QA_MODEL",        "ProjectQA"),
    ):
        if _key in body:
            setattr(config, _attr, body[_key] or "")
            print(f"[Config] Updated {_label} Model to: {body[_key] or '(inherit umbrella)'}")
    if "openhands_enabled" in body:
        config.OPENHANDS_ENABLED = bool(body["openhands_enabled"])
        print(f"[Config] OpenHands enabled: {config.OPENHANDS_ENABLED}")
    if "openhands_max_rounds" in body:
        config.OPENHANDS_MAX_ROUNDS = config.coerce_int(body["openhands_max_rounds"], config.OPENHANDS_MAX_ROUNDS, minimum=1, maximum=200)
        print(f"[Config] OpenHands max rounds: {config.OPENHANDS_MAX_ROUNDS}")
    if "openhands_num_ctx" in body:
        config.OPENHANDS_NUM_CTX = config.coerce_num_ctx(
            body["openhands_num_ctx"],
            fallback=config.OPENHANDS_NUM_CTX,
        )
        settings["openhands_num_ctx"] = config.OPENHANDS_NUM_CTX
        print(f"[Config] OpenHands num_ctx: {config.OPENHANDS_NUM_CTX}")
    if "openhands_reasoning_effort" in body:
        _re_in = (body["openhands_reasoning_effort"] or "medium").strip().lower()
        if _re_in not in ("low", "medium", "high"):
            _re_in = "medium"
        config.OPENHANDS_REASONING_EFFORT = _re_in
        # Persist the validated value, not the raw input.
        settings["openhands_reasoning_effort"] = _re_in
        print(f"[Config] OpenHands reasoning effort: {config.OPENHANDS_REASONING_EFFORT}")
    if "openhands_disable_thinking" in body:
        config.OPENHANDS_DISABLE_THINKING = bool(body["openhands_disable_thinking"])
        print(f"[Config] OpenHands disable thinking: {config.OPENHANDS_DISABLE_THINKING}")
    if "aider_enabled" in body:
        config.AIDER_ENABLED = bool(body["aider_enabled"])
        print(f"[Config] Aider enabled: {config.AIDER_ENABLED}")
    if "aider_model" in body:
        config.AIDER_MODEL = body["aider_model"] or ""
        print(f"[Config] Aider Model: {config.AIDER_MODEL or '(inherit fixer/coder)'}")
    if "aider_num_ctx" in body:
        # 0 / empty = inherit the Daedalus context-window slider
        # (OPENHANDS_NUM_CTX) at call time; a positive value is an explicit
        # per-Aider override.
        _actx_raw = body["aider_num_ctx"]
        if not _actx_raw:
            config.AIDER_NUM_CTX = 0
        else:
            config.AIDER_NUM_CTX = config.coerce_num_ctx(
                _actx_raw,
                fallback=config.AIDER_NUM_CTX or config.OPENHANDS_NUM_CTX,
            )
        settings["aider_num_ctx"] = config.AIDER_NUM_CTX
        print(f"[Config] Aider num_ctx: {config.AIDER_NUM_CTX or '(inherit Daedalus slider)'}")
    if "aider_auto_test" in body:
        config.AIDER_AUTO_TEST = bool(body["aider_auto_test"])
        print(f"[Config] Aider auto-test: {config.AIDER_AUTO_TEST}")
    if "aider_worker_url" in body:
        config.AIDER_WORKER_URL = body["aider_worker_url"] or config.OPENHANDS_URL
        settings["aider_worker_url"] = config.AIDER_WORKER_URL
        print(f"[Config] Aider worker URL: {config.AIDER_WORKER_URL}")
    if "default_num_ctx" in body:
        config.DEFAULT_NUM_CTX = config.coerce_num_ctx(
            body["default_num_ctx"],
            fallback=config.DEFAULT_NUM_CTX,
        )
        settings["default_num_ctx"] = config.DEFAULT_NUM_CTX
        print(f"[Config] Default num_ctx: {config.DEFAULT_NUM_CTX}")
    if "research_num_ctx" in body:
        config.RESEARCH_NUM_CTX = config.coerce_num_ctx(
            body["research_num_ctx"],
            fallback=config.RESEARCH_NUM_CTX,
        )
        settings["research_num_ctx"] = config.RESEARCH_NUM_CTX
        print(f"[Config] Research num_ctx: {config.RESEARCH_NUM_CTX}")
    if "quick_search_mode" in body:
        _qsm = (body["quick_search_mode"] or "balanced").strip().lower()
        if _qsm not in ("speed", "balanced", "quality"):
            _qsm = "balanced"
        config.QUICK_SEARCH_MODE = _qsm
        settings["quick_search_mode"] = _qsm
        print(f"[Config] Quick Search mode: {config.QUICK_SEARCH_MODE}")
    if "model_hardware_profile" in body:
        settings["model_hardware_profile"] = hyprfit.clean_hardware_profile(body.get("model_hardware_profile"))
    # Chat image generation defaults. Checkpoint/VAE values come from the live
    # ComfyUI dropdowns in the UI; stored as-is (a stale name surfaces as a
    # tool_error at generation time, never a crash here).
    if "image_chat_checkpoint" in body:
        config.IMAGE_CHAT_CHECKPOINT = str(body["image_chat_checkpoint"] or "").strip()[:200]
        settings["image_chat_checkpoint"] = config.IMAGE_CHAT_CHECKPOINT
        print(f"[Config] Chat image checkpoint: {config.IMAGE_CHAT_CHECKPOINT or '(built-in template)'}")
    if "image_chat_workflow" in body:
        _wf = str(body["image_chat_workflow"] or "").strip()[:200]
        # Only accept a workflow that actually exists; blank an unknown/deleted
        # name so chat image gen can't be stranded pointing at a missing graph.
        if _wf and _wf not in comfyui.list_workflows():
            print(f"[Config] Ignoring unknown chat image workflow: {_wf}")
            _wf = ""
        config.IMAGE_CHAT_WORKFLOW = _wf
        settings["image_chat_workflow"] = _wf
        print(f"[Config] Chat image workflow: {_wf or '(built-in template)'}")
    if "image_chat_resolution" in body:
        _res = str(body["image_chat_resolution"] or "").strip()
        if not re.fullmatch(r"\d{3,4}x\d{3,4}", _res):
            _res = "1024x1024"
        config.IMAGE_CHAT_RESOLUTION = _res
        settings["image_chat_resolution"] = _res
    if "image_chat_vae" in body:
        config.IMAGE_CHAT_VAE = str(body["image_chat_vae"] or "").strip()[:200]
        settings["image_chat_vae"] = config.IMAGE_CHAT_VAE
    if "image_chat_prompt_prefix" in body:
        config.IMAGE_CHAT_PROMPT_PREFIX = str(body["image_chat_prompt_prefix"] or "").strip()[:500]
        settings["image_chat_prompt_prefix"] = config.IMAGE_CHAT_PROMPT_PREFIX
    if "image_chat_negative" in body:
        config.IMAGE_CHAT_NEGATIVE = str(body["image_chat_negative"] or "").strip()[:500]
        settings["image_chat_negative"] = config.IMAGE_CHAT_NEGATIVE
    if "image_chat_compose_model" in body:
        config.IMAGE_CHAT_COMPOSE_MODEL = str(body["image_chat_compose_model"] or "").strip()[:200]
        settings["image_chat_compose_model"] = config.IMAGE_CHAT_COMPOSE_MODEL
        print(f"[Config] Photo prompt model: {config.IMAGE_CHAT_COMPOSE_MODEL or '(conversation chat model)'}")
    if "image_chat_auto_enhance" in body:
        # The frontend persists string values; bool("false") is True, so parse.
        config.IMAGE_CHAT_AUTO_ENHANCE = str(body["image_chat_auto_enhance"]).strip().lower() in ("1", "true", "yes", "on")
        settings["image_chat_auto_enhance"] = config.IMAGE_CHAT_AUTO_ENHANCE
        print(f"[Config] Chat image auto-enhance: {'on' if config.IMAGE_CHAT_AUTO_ENHANCE else 'off'}")
    save_settings(settings)
    return {
        **_public_settings_payload(settings),
        "current_ollama_url": config.OLLAMA_URL,
        "current_codebox_url": config.CODEBOX_URL,
        "current_searxng_url": config.SEARXNG_URL,
        "current_n8n_url": config.N8N_URL,
        "current_comfyui_url": config.COMFYUI_URL,
        "current_stt_url": config.STT_URL,
        "current_tts_url": config.TTS_URL,
        "current_tts_voice": config.TTS_VOICE,
        "current_planning_model": config.PLANNING_MODEL,
        "current_coder_model": config.CODER_MODEL,
        "current_workspace_model": config.WORKSPACE_MODEL,
        "context_compaction": config.CONTEXT_COMPACTION,
        "current_architect_model": config.ARCHITECT_MODEL,
        "current_reviewer_model":  config.REVIEWER_MODEL,
        "current_acceptance_model": config.ACCEPTANCE_MODEL,
        "current_builder_model":   config.BUILDER_MODEL,
        "current_fixer_model":     config.FIXER_MODEL,
        "current_qa_model":        config.QA_MODEL,
        "openhands_enabled": config.OPENHANDS_ENABLED,
        "openhands_max_rounds": config.OPENHANDS_MAX_ROUNDS,
        "openhands_num_ctx": config.OPENHANDS_NUM_CTX,
        "openhands_reasoning_effort": config.OPENHANDS_REASONING_EFFORT,
        "openhands_disable_thinking": config.OPENHANDS_DISABLE_THINKING,
        "aider_enabled": config.AIDER_ENABLED,
        "aider_model": config.AIDER_MODEL,
        "aider_num_ctx": config.AIDER_NUM_CTX,
        "aider_auto_test": config.AIDER_AUTO_TEST,
        "aider_worker_url": config.AIDER_WORKER_URL,
        "default_num_ctx": config.DEFAULT_NUM_CTX,
        "research_num_ctx": config.RESEARCH_NUM_CTX,
        "quick_search_mode": config.QUICK_SEARCH_MODE,
    }


@router.get("/api/rag/stats")
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


@router.delete("/api/rag/collections")
async def delete_all_rag_collections():
    """Delete ALL ChromaDB collections (RAG indices) and reclaim disk space."""
    try:
        client = rag.get_chroma()
        collections = client.list_collections()
        count = 0
        for c in collections:
            client.delete_collection(c.name)
            count += 1
        print(f"[RAG] Purged all {count} collections")
        # Reset ChromaDB client and remove data directory to reclaim disk space
        import shutil
        rag._chroma_client = None
        if os.path.exists(rag.CHROMA_DIR):
            shutil.rmtree(rag.CHROMA_DIR)
            print(f"[RAG] Removed ChromaDB data directory: {rag.CHROMA_DIR}")
        return {"deleted": count}
    except Exception as e:
        print(f"[RAG] Purge error: {e}")
        return {"deleted": 0, "error": str(e)}


@router.post("/api/settings/cleanup-now")
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


@router.post("/api/settings/cleanup-codebox")
async def cleanup_codebox():
    """Delete all project files on the CodeBox sandbox."""
    openhands_url = config.OPENHANDS_URL
    try:
        r = await route_context().http.post(f"{openhands_url}/clean", timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[Cleanup] Codebox clean failed: {e}")
        return {"deleted": 0, "error": str(e)}


@router.get("/api/changelog")
async def get_changelog():
    """Return the CHANGELOG.md content."""
    changelog_path = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
    try:
        with open(changelog_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"content": content}
    except FileNotFoundError:
        return {"content": "# Changelog\n\nNo changelog available."}


# ============================================================
# USER PREFERENCE KV — server-side home for per-user UI state
# (prompt library, model params, custom quotes, conversation tags,
# per-chat effort). Values are opaque JSON blobs owned by the frontend.
# ============================================================
_PREF_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_PREF_MAX_BYTES = 256 * 1024


def _validate_pref_key(key: str) -> str:
    if not _PREF_KEY_RE.fullmatch(key or ""):
        raise HTTPException(400, "Invalid pref key (lowercase alphanumerics, ., _, -; max 64 chars)")
    return key


@router.get("/api/prefs")
async def get_prefs():
    return {"prefs": await db.get_user_prefs()}


@router.get("/api/prefs/{key}")
async def get_pref(key: str):
    value = await db.get_user_pref(_validate_pref_key(key))
    return {"key": key, "value": value}


@router.put("/api/prefs/{key}")
async def put_pref(key: str, value=Body(...)):
    _validate_pref_key(key)
    import json as _json
    if len(_json.dumps(value)) > _PREF_MAX_BYTES:
        raise HTTPException(413, f"Pref value exceeds {_PREF_MAX_BYTES // 1024}KB")
    await db.set_user_pref(key, value)
    return {"ok": True, "key": key}


@router.delete("/api/prefs/{key}")
async def delete_pref(key: str):
    deleted = await db.delete_user_pref(_validate_pref_key(key))
    return {"ok": True, "deleted": deleted}


# ============================================================
# TOKEN USAGE ANALYTICS
# ============================================================
def _annotate_cost(rows: list[dict]) -> list[dict]:
    """Attach cost_usd to rows that have a `model` column (priced models only)."""
    for r in rows:
        c = model_providers.cost_usd(r.get("model", ""), r.get("prompt_tokens") or 0, r.get("completion_tokens") or 0)
        if c is not None:
            r["cost_usd"] = round(c, 4)
    return rows


def _sum_cost(rows: list[dict]) -> float:
    return round(sum(r.get("cost_usd") or 0 for r in rows), 4)


async def _daily_costs(days: int) -> dict[str, float]:
    """date → summed cloud cost, from per-day-per-model aggregation."""
    rows = _annotate_cost(await db.get_token_usage(days, "day_model"))
    out: dict[str, float] = {}
    for r in rows:
        if r.get("cost_usd"):
            out[r["date"]] = round(out.get(r["date"], 0) + r["cost_usd"], 4)
    return out


@router.get("/api/analytics/tokens")
async def get_token_analytics(days: int = Query(30), group_by: str = Query("day")):
    if group_by not in ("day", "model", "persona", "day_model"):
        raise HTTPException(400, "group_by must be: day, model, persona, or day_model")
    rows = await db.get_token_usage(days, group_by)
    if group_by in ("model", "day_model"):
        _annotate_cost(rows)
    elif group_by == "day":
        by_date = await _daily_costs(days)
        for r in rows:
            if by_date.get(r.get("date")):
                r["cost_usd"] = by_date[r["date"]]
    return rows


@router.delete("/api/analytics/tokens")
async def clear_token_analytics():
    deleted = await db.clear_token_usage()
    return {"ok": True, "deleted": deleted}


_USAGE_SUM_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "request_count")


def _usage_cutoff_date(days: int) -> str:
    """Date floor for an N-day window over date-grouped rows. Matches the SQL
    rolling window at date granularity (the boundary day counts in full)."""
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")


def _agg_by_day(rows: list[dict], days: int = 0) -> list[dict]:
    cutoff = _usage_cutoff_date(days) if days > 0 else ""
    out: dict[str, dict] = {}
    for r in rows:
        d = r.get("date") or ""
        if cutoff and d < cutoff:
            continue
        agg = out.setdefault(d, {"date": d, **{k: 0 for k in _USAGE_SUM_KEYS}})
        for k in _USAGE_SUM_KEYS:
            agg[k] += r.get(k) or 0
    return sorted(out.values(), key=lambda x: x["date"])


def _agg_by_model(rows: list[dict], days: int = 0) -> list[dict]:
    cutoff = _usage_cutoff_date(days) if days > 0 else ""
    out: dict[str, dict] = {}
    for r in rows:
        if cutoff and (r.get("date") or "") < cutoff:
            continue
        m = r.get("model")
        agg = out.setdefault(m, {"model": m, **{k: 0 for k in _USAGE_SUM_KEYS}})
        for k in _USAGE_SUM_KEYS:
            agg[k] += r.get(k) or 0
    return sorted(out.values(), key=lambda x: -(x["total_tokens"] or 0))


@router.get("/api/analytics/tokens/summary")
async def get_token_summary():
    # One day×model scan covers every window/grouping below (was 8 queries);
    # persona grouping and ratings are the only extra fetches.
    day_model_all = _annotate_cost(await db.get_token_usage(0, "day_model"))
    by_persona_all = await db.get_token_usage(0, "persona")
    by_model_all = _annotate_cost(_agg_by_model(day_model_all))
    cutoff_1 = _usage_cutoff_date(1)
    cutoff_30 = _usage_cutoff_date(30)
    return {
        "today": _agg_by_day(day_model_all, 1),
        "by_model_7d": _annotate_cost(_agg_by_model(day_model_all, 7)),
        "daily_30d": _agg_by_day(day_model_all, 30),
        "all_time": _agg_by_day(day_model_all),
        "by_model_all": by_model_all,
        "by_persona_all": by_persona_all,
        "cost": {
            "today": _sum_cost([r for r in day_model_all if (r.get("date") or "") >= cutoff_1]),
            "last_30d": _sum_cost([r for r in day_model_all if (r.get("date") or "") >= cutoff_30]),
            "all_time": _sum_cost(by_model_all),
        },
        "ratings": await db.get_message_rating_counts(0),
    }
