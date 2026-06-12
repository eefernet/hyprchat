"""
HyprChat — FastAPI Backend
Full-stack backend with Ollama streaming, Codebox execution,
SearXNG research, n8n webhook proxy, and SSE status events.
"""
import asyncio
import csv
import hashlib
import html
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
import posixpath
import stat
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
from agents.chat import chat_stream_generate, TOOL_TEMPLATES, detect_template_family
from agents.personas import seed_coder_bot as _seed_coder_bot, seed_coder_bot_v2 as _seed_coder_bot_v2, seed_conspiracy_bot as _seed_conspiracy_bot, seed_based_bot as _seed_based_bot, seed_all_defaults as _seed_all_defaults
import hf as hf_module
from hf import parse_ollama_progress
import rag
import connectors
import comfyui
import voice
import model_providers
from research import (
    REPORT_TEMPLATES,
    REPORT_TEMPLATE_MAP,
    close_web_fetch_client,
    fetch_bytes_safely,
    run_research_report,
    web_get,
)

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


def _coerce_service_url(value: str, env_key: str, default: str) -> str:
    """Return a runtime service URL, accepting bare host:port input from Settings."""
    raw = (value or "").strip()
    if not raw:
        return os.getenv(env_key, default)
    if "://" not in raw:
        raw = f"http://{raw}"
    return raw.rstrip("/")


def _decode_preview_bytes(content: bytes, headers: dict) -> str:
    ct = headers.get("content-type", "") or ""
    m = re.search(r"charset=([^;\s]+)", ct, re.I)
    enc = (m.group(1) if m else "utf-8").strip("\"'")
    try:
        return content.decode(enc, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


def _sanitize_preview_html(html: str, base_url: str) -> str:
    from html import escape as html_escape

    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"\son[a-z]+\s*=\s*(['\"]).*?\1", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"\son[a-z]+\s*=\s*[^\s>]+", "", html, flags=re.IGNORECASE)
    html = re.sub(r"javascript\s*:", "", html, flags=re.IGNORECASE)
    base_tag = f'<base href="{html_escape(base_url, quote=True)}" target="_blank">'
    if "<head" in html.lower():
        return re.sub(r"(<head[^>]*>)", r"\1" + base_tag, html, count=1, flags=re.IGNORECASE)
    return base_tag + html


def _resolve_download_path(filename: str) -> tuple[str, str] | tuple[None, str]:
    safe_name = os.path.basename(filename or "")
    if not safe_name or safe_name != filename:
        raise HTTPException(400, "Invalid filename")
    for search_dir in [config.SANDBOX_OUTPUTS_DIR, config.UPLOAD_DIR]:
        filepath = os.path.join(search_dir, safe_name)
        if not os.path.abspath(filepath).startswith(os.path.abspath(search_dir)):
            continue
        if os.path.exists(filepath):
            return filepath, safe_name
    return None, safe_name


_ARCHIVE_PREVIEW_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".html", ".htm", ".py",
    ".js", ".ts", ".css", ".sh", ".yaml", ".yml", ".toml", ".xml", ".log",
    ".ini", ".conf",
}
_ARCHIVE_ENTRY_MAX_BYTES = 512 * 1024
_ARCHIVE_ENTRY_MAX_CHARS = 200000


def _normalize_archive_path(path: str) -> str:
    raw = str(path or "").replace("\\", "/").strip()
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        raise HTTPException(400, "Invalid archive path")
    norm = posixpath.normpath(raw)
    if norm in {"", "."} or norm.startswith("../") or norm == ".." or "/../" in f"/{norm}/":
        raise HTTPException(400, "Invalid archive path")
    return norm


def _archive_path_is_safe(path: str) -> bool:
    try:
        _normalize_archive_path(path)
        return True
    except HTTPException:
        return False


def _zipinfo_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return stat.S_ISLNK(mode)


def _archive_entry_previewable(path: str, size: int, is_dir: bool, unsafe: bool = False, encrypted: bool = False, symlink: bool = False) -> bool:
    if is_dir or unsafe or encrypted or symlink or int(size or 0) > _ARCHIVE_ENTRY_MAX_BYTES:
        return False
    return os.path.splitext(path.lower())[1] in _ARCHIVE_PREVIEW_EXTS


def _archive_entry_record(
    path: str,
    *,
    size: int = 0,
    is_dir: bool = False,
    compressed_size: int | None = None,
    modified_at: str | None = None,
    encrypted: bool = False,
    symlink: bool = False,
) -> dict:
    display_path = str(path or "").replace("\\", "/")
    unsafe = not _archive_path_is_safe(display_path.rstrip("/") or display_path)
    clean = display_path.strip("/")
    if not unsafe:
        clean = _normalize_archive_path(display_path.rstrip("/") or display_path)
    clean = clean.rstrip("/") if is_dir else clean
    parent = posixpath.dirname(clean) if "/" in clean else ""
    name = posixpath.basename(clean) + ("/" if is_dir else "")
    ext = os.path.splitext(clean.lower())[1]
    depth = 0 if not parent else len(parent.split("/"))
    previewable = _archive_entry_previewable(clean, size, is_dir, unsafe, encrypted, symlink)
    reason = ""
    if unsafe:
        reason = "unsafe path"
    elif symlink:
        reason = "symlink"
    elif encrypted:
        reason = "encrypted"
    elif is_dir:
        reason = "directory"
    elif int(size or 0) > _ARCHIVE_ENTRY_MAX_BYTES:
        reason = "oversized"
    elif ext not in _ARCHIVE_PREVIEW_EXTS:
        reason = "unsupported type"
    return {
        "name": clean + ("/" if is_dir and not clean.endswith("/") else ""),
        "path": clean,
        "display_name": name,
        "parent": parent,
        "depth": depth,
        "size": int(size or 0),
        "compressed_size": compressed_size,
        "is_dir": bool(is_dir),
        "modified_at": modified_at,
        "ext": ext,
        "previewable": previewable,
        "unsafe": unsafe,
        "encrypted": encrypted,
        "symlink": symlink,
        "preview_blocked_reason": "" if previewable else reason,
    }


def _archive_contents_for_path(filepath: str, safe_name: str) -> dict:
    import tarfile

    entries = []
    archive_type = ""
    dirs = set()
    if tarfile.is_tarfile(filepath):
        archive_type = "tar"
        with tarfile.open(filepath, "r:*") as tf:
            for m in tf.getmembers():
                entries.append(_archive_entry_record(
                    m.name,
                    size=m.size,
                    is_dir=m.isdir(),
                    modified_at=datetime.utcfromtimestamp(m.mtime).isoformat() if m.mtime else None,
                    symlink=m.issym() or m.islnk(),
                ))
    elif zipfile.is_zipfile(filepath):
        archive_type = "zip"
        with zipfile.ZipFile(filepath) as zf:
            for info in zf.infolist():
                modified_at = None
                try:
                    modified_at = datetime(*info.date_time).isoformat()
                except Exception:
                    modified_at = None
                entries.append(_archive_entry_record(
                    info.filename,
                    size=info.file_size,
                    compressed_size=info.compress_size,
                    is_dir=info.is_dir(),
                    modified_at=modified_at,
                    encrypted=bool(info.flag_bits & 0x1),
                    symlink=_zipinfo_is_symlink(info),
                ))
    else:
        raise HTTPException(400, "Not a supported archive")

    for entry in list(entries):
        path = entry.get("path") or ""
        if entry.get("unsafe"):
            continue
        parts = path.split("/")[:-1] if not entry.get("is_dir") else path.split("/")
        accum = []
        for part in parts:
            if not part:
                continue
            accum.append(part)
            dirs.add("/".join(accum))
    existing_paths = {e["path"] for e in entries if not e.get("unsafe")}
    for directory in sorted(dirs):
        if directory and directory not in existing_paths:
            entries.append(_archive_entry_record(directory, is_dir=True))

    def _sort_key(e):
        parts = (e.get("path") or "").rstrip("/").split("/")
        key = []
        for i, p in enumerate(parts):
            is_last = i == len(parts) - 1
            key.append((0 if (e.get("is_dir") or not is_last) else 1, p.lower()))
        return key

    entries.sort(key=_sort_key)
    file_count = len([e for e in entries if not e["is_dir"] and not e.get("unsafe")])
    folder_count = len([e for e in entries if e["is_dir"] and not e.get("unsafe")])
    return {
        "filename": safe_name,
        "archive_type": archive_type,
        "file_count": file_count,
        "folder_count": folder_count,
        "entries": entries,
        "preview_entry_max_bytes": _ARCHIVE_ENTRY_MAX_BYTES,
        "preview_max_chars": _ARCHIVE_ENTRY_MAX_CHARS,
    }


def _looks_binary(raw: bytes) -> bool:
    if b"\x00" in raw:
        return True
    if not raw:
        return False
    sample = raw[:4096]
    control = sum(1 for b in sample if b < 32 and b not in {9, 10, 12, 13})
    return control / max(1, len(sample)) > 0.08


def _archive_entry_preview_for_path(filepath: str, safe_name: str, entry_path: str) -> dict:
    import tarfile

    wanted = _normalize_archive_path(entry_path)
    if not zipfile.is_zipfile(filepath) and not tarfile.is_tarfile(filepath):
        raise HTTPException(400, "Artifact is not a supported archive")
    ext = os.path.splitext(wanted.lower())[1]
    if ext not in _ARCHIVE_PREVIEW_EXTS:
        raise HTTPException(415, "Archive entry type is not safe to preview")

    raw = b""
    meta = {"path": wanted, "filename": safe_name}
    if zipfile.is_zipfile(filepath):
        with zipfile.ZipFile(filepath) as zf:
            matches = [info for info in zf.infolist() if _archive_path_is_safe(info.filename.rstrip("/")) and _normalize_archive_path(info.filename.rstrip("/")) == wanted]
            if not matches:
                raise HTTPException(404, "Archive entry not found")
            info = matches[0]
            record = _archive_entry_record(
                info.filename,
                size=info.file_size,
                compressed_size=info.compress_size,
                is_dir=info.is_dir(),
                encrypted=bool(info.flag_bits & 0x1),
                symlink=_zipinfo_is_symlink(info),
            )
            if record["is_dir"]:
                raise HTTPException(400, "Archive entry is a directory")
            if record["symlink"]:
                raise HTTPException(400, "Archive entry is a symlink")
            if record["encrypted"]:
                raise HTTPException(400, "Archive entry is encrypted")
            if record["size"] > _ARCHIVE_ENTRY_MAX_BYTES:
                raise HTTPException(413, "Archive entry is too large to preview")
            raw = zf.read(info)
            meta.update(record)
    else:
        with tarfile.open(filepath, "r:*") as tf:
            members = [m for m in tf.getmembers() if _archive_path_is_safe(m.name.rstrip("/")) and _normalize_archive_path(m.name.rstrip("/")) == wanted]
            if not members:
                raise HTTPException(404, "Archive entry not found")
            member = members[0]
            record = _archive_entry_record(
                member.name,
                size=member.size,
                is_dir=member.isdir(),
                symlink=member.issym() or member.islnk(),
            )
            if record["is_dir"]:
                raise HTTPException(400, "Archive entry is a directory")
            if record["symlink"]:
                raise HTTPException(400, "Archive entry is a symlink")
            if record["size"] > _ARCHIVE_ENTRY_MAX_BYTES:
                raise HTTPException(413, "Archive entry is too large to preview")
            extracted = tf.extractfile(member)
            if not extracted:
                raise HTTPException(400, "Archive entry cannot be read")
            raw = extracted.read(_ARCHIVE_ENTRY_MAX_BYTES + 1)
            meta.update(record)

    if len(raw) > _ARCHIVE_ENTRY_MAX_BYTES:
        raise HTTPException(413, "Archive entry is too large to preview")
    if _looks_binary(raw):
        raise HTTPException(415, "Archive entry appears to be binary")
    content = _decode_preview_bytes(raw, {"content-type": ""})[:_ARCHIVE_ENTRY_MAX_CHARS]
    return {
        "artifact_filename": safe_name,
        "path": wanted,
        "preview_type": "archive_entry",
        "content": content,
        "language": _language_hint(wanted, "text"),
        "size": len(raw),
        "truncated": len(content) >= _ARCHIVE_ENTRY_MAX_CHARS,
        "entry": meta,
    }


def _artifact_text_preview_allowed(artifact: dict) -> bool:
    kind = (artifact.get("kind") or "").lower()
    if kind in {"html", "markdown", "code", "data", "text"}:
        return True
    ext = os.path.splitext((artifact.get("filename") or "").lower())[1]
    return ext in {
        ".txt", ".log", ".md", ".html", ".htm", ".py", ".js", ".ts", ".json",
        ".css", ".sh", ".rs", ".go", ".java", ".c", ".cpp", ".yaml", ".yml",
        ".toml", ".xml", ".csv", ".ini", ".conf", ".cfg",
    }


def _safe_local_artifact_path(path: str | None) -> str | None:
    if not path:
        return None
    try:
        abs_path = os.path.abspath(os.path.expanduser(path))
    except Exception:
        return None
    roots = [
        config.SANDBOX_OUTPUTS_DIR,
        config.UPLOAD_DIR,
        config.KB_DIR,
    ]
    for root in roots:
        root_abs = os.path.abspath(root)
        if abs_path == root_abs or abs_path.startswith(root_abs + os.sep):
            return abs_path
    return None


def _artifact_path_for_row(artifact: dict) -> tuple[str | None, str]:
    storage_path = _safe_local_artifact_path(artifact.get("storage_path"))
    if storage_path:
        return (storage_path if os.path.exists(storage_path) else None), os.path.basename(storage_path)
    filename = os.path.basename(artifact.get("filename") or "")
    if not filename and artifact.get("url"):
        filename = os.path.basename(urllib.parse.urlparse(artifact["url"]).path)
    if not filename:
        return None, ""
    return _resolve_download_path(filename)


def _artifact_file_metadata(filepath: str) -> dict:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return {"size_bytes": os.path.getsize(filepath), "sha256": h.hexdigest()}


def _language_hint(filename: str, kind: str = "") -> str:
    ext = os.path.splitext((filename or "").lower())[1].lstrip(".")
    aliases = {"md": "markdown", "markdown": "markdown", "py": "python", "js": "javascript", "ts": "typescript", "sh": "bash", "yml": "yaml"}
    if ext:
        return aliases.get(ext, ext)
    return (kind or "text").lower()


def _strip_html_text(raw_html: str, max_chars: int) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", raw_html, flags=re.I | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:max_chars]


def _extract_indexable_text(filepath: str, kind: str, mime_type: str = "", max_chars: int = 500000) -> str:
    filename = os.path.basename(filepath)
    lower = filename.lower()
    kind = (kind or db.artifact_kind_for_filename(filename, mime_type)).lower()
    try:
        if kind in {"image", "pdf"}:
            meta = _artifact_file_metadata(filepath)
            return f"{filename}\n{kind.upper()} artifact\nsize_bytes={meta['size_bytes']}\nsha256={meta['sha256']}"
        if kind == "archive":
            preview = _archive_contents_for_path(filepath, filename)
            lines = [f"Archive: {filename}", f"Files: {preview.get('file_count', 0)}"]
            for entry in preview.get("entries", [])[:500]:
                suffix = "/" if entry.get("is_dir") and not str(entry.get("name", "")).endswith("/") else ""
                lines.append(f"{entry.get('name','')}{suffix} {entry.get('size',0)} bytes")
            return "\n".join(lines)[:max_chars]
        with open(filepath, "rb") as f:
            raw = f.read(min(max_chars * 4, 4 * 1024 * 1024))
        text = _decode_preview_bytes(raw, {"content-type": mime_type or ""})
        if kind == "html" or lower.endswith((".html", ".htm")):
            return _strip_html_text(text, max_chars)
        return text[:max_chars]
    except Exception:
        return ""


def _render_markdown_safe(markdown_text: str) -> str:
    blocks = []
    in_code = False
    code_lines = []
    for line in (markdown_text or "").splitlines()[:2000]:
        if line.strip().startswith("```"):
            if in_code:
                blocks.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        escaped = html.escape(line)
        if not escaped.strip():
            blocks.append("<br>")
        elif escaped.startswith("### "):
            blocks.append(f"<h3>{escaped[4:]}</h3>")
        elif escaped.startswith("## "):
            blocks.append(f"<h2>{escaped[3:]}</h2>")
        elif escaped.startswith("# "):
            blocks.append(f"<h1>{escaped[2:]}</h1>")
        elif escaped.startswith("- "):
            blocks.append(f"<div>&bull; {escaped[2:]}</div>")
        else:
            blocks.append(f"<p>{escaped}</p>")
    if in_code:
        blocks.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
    return "\n".join(blocks)


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
    os.makedirs(config.UPLOAD_DIR, exist_ok=True)
    os.makedirs(config.TOOLS_DIR, exist_ok=True)
    os.makedirs(config.KB_DIR, exist_ok=True)
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
    # Run cleanup once on startup to clear any stale files
    _run_cleanup_sync()
    # Start background cleanup loop
    _cleanup_task_ref = asyncio.create_task(_cleanup_loop())
    # Start health check loop (every 5 min)
    _health_task_ref = asyncio.create_task(_health_check_loop())
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

class ToolCreate(BaseModel):
    name: str
    description: str = ""
    filename: str
    code: str

class ToolUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    code: Optional[str] = None

class MCPServerCreate(BaseModel):
    name: str
    transport: str = "stdio"
    command: str = ""
    url: str = ""
    args: list[str] = []
    env: dict = {}
    headers: dict = {}
    allow_private: bool = False
    enabled: bool = True

class MCPServerUpdate(BaseModel):
    name: Optional[str] = None
    transport: Optional[str] = None
    command: Optional[str] = None
    url: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict] = None
    headers: Optional[dict] = None
    allow_private: Optional[bool] = None
    enabled: Optional[bool] = None

class OpenAPIConnectorCreate(BaseModel):
    name: str
    spec_url: str = ""
    spec_json: Any = ""
    base_url: str = ""
    auth: dict = {}
    headers: dict = {}
    allow_private: bool = False
    enabled: bool = True

class OpenAPIConnectorUpdate(BaseModel):
    name: Optional[str] = None
    spec_url: Optional[str] = None
    spec_json: Optional[Any] = None
    base_url: Optional[str] = None
    auth: Optional[dict] = None
    headers: Optional[dict] = None
    allow_private: Optional[bool] = None
    enabled: Optional[bool] = None

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

class ConversationSearchRequest(BaseModel):
    query: str
    limit: int = 20

class ForkRequest(BaseModel):
    message_id: int

class UserCreate(BaseModel):
    name: str
    password: Optional[str] = ""

class UserLogin(BaseModel):
    user_id: str
    password: Optional[str] = ""

class UserUpdate(BaseModel):
    name: Optional[str] = None
    password: Optional[str] = None
    clear_password: Optional[bool] = False

class ArtifactUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    workspace_id: Optional[str] = None
    workspace_ids: Optional[list[str]] = None
    status: Optional[str] = None
    pinned: Optional[bool] = None
    tags: Optional[list[str]] = None
    parent_artifact_id: Optional[str] = None
    supersedes_artifact_id: Optional[str] = None

class ArtifactUseInChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    max_chars: int = 20000

class ArtifactAddToKBRequest(BaseModel):
    kb_id: str
    filename: Optional[str] = None
    max_chars: int = 500000

class ArtifactResearchRequest(BaseModel):
    query: Optional[str] = None
    title: Optional[str] = None
    focus: str = ""
    report_type: str = "analyst"
    depth: Optional[int] = None
    model: str = ""
    planner_model: str = ""
    auditor_model: str = ""
    kb_ids: list[str] = []
    max_chars: int = 50000

class ArtifactReviseRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    filename: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[list[str]] = None
    workspace_ids: Optional[list[str]] = None

class ArtifactBundleRequest(BaseModel):
    artifact_ids: list[str]
    title: Optional[str] = None
    filename: Optional[str] = None
    workspace_ids: Optional[list[str]] = None

class ArtifactMergeDuplicateRequest(BaseModel):
    duplicate_id: str


# ============================================================
# USERS — lightweight local profile switching
# ============================================================
@app.get("/api/users")
async def list_users_ep():
    return {"users": await db.list_users()}


@app.get("/api/users/current")
async def current_user_ep(request: Request):
    user = await _validated_request_user(request)
    return {"user": await db.get_user(user["id"])}


@app.post("/api/users")
async def create_user_ep(req: UserCreate):
    user = await db.create_user(req.name, req.password or "")
    return {"user": user}


@app.post("/api/users/login")
async def login_user_ep(req: UserLogin):
    user, session_token = await db.login_user(req.user_id, req.password or "")
    if not user:
        raise HTTPException(401, "Invalid user or password")
    return {"user": user, "session_token": session_token}


@app.post("/api/users/logout")
async def logout_user_ep(request: Request):
    user_id = _request_user_id(request)
    await db.logout_user_session(user_id, _request_session_token(request) or None)
    return {"ok": True}


@app.patch("/api/users/{user_id}")
async def update_user_ep(user_id: str, req: UserUpdate, request: Request):
    current = await _validated_request_user(request)
    user, _ = await db.update_user(
        user_id,
        name=req.name,
        password=req.password,
        clear_password=bool(req.clear_password),
    )
    if not user:
        raise HTTPException(404, "User not found")
    session_token = None
    if user_id == current["id"] and req.password is not None and not req.clear_password and req.password:
        session_token = await db.create_user_session(user_id)
    return {"user": user, "session_token": session_token}


@app.delete("/api/users/{user_id}")
async def delete_user_ep(user_id: str, request: Request):
    await _validated_request_user(request)
    if user_id == db.DEFAULT_USER_ID:
        raise HTTPException(400, "The Main user cannot be deleted")
    ok = await db.delete_user(user_id)
    if not ok:
        raise HTTPException(404, "User not found")
    return {"ok": True}


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


async def _run_health_checks() -> dict:
    """Run all health checks and log to DB."""
    checks = {}
    for name, url_fn in _HEALTH_ENDPOINTS.items():
        result = await _check_service(name, url_fn())
        checks[name] = result
    # SearXNG gets its own special check (rate-limit detection)
    checks["searxng"] = await _check_searxng()
    # Optional services — only checked (and reported) when configured
    if config.COMFYUI_URL:
        checks["comfyui"] = await comfyui.check_health(http)
    if config.STT_URL:
        checks["stt"] = await _check_service("stt", f"{config.STT_URL}/v1/models")
    if config.TTS_URL:
        checks["tts"] = await _check_service("tts", f"{config.TTS_URL}/v1/models")
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
# ARTIFACTS
# ============================================================
async def _annotate_artifact_staleness(artifacts: list[dict]) -> list[dict]:
    """For archive artifacts: set `stale` when the project changed (a successful
    edit run started) after the artifact was packaged, and `latest_for_project`
    for the newest archive of each project within the given set. Edit-run lookups
    are cached per project_id so a list page costs one query per distinct project."""
    if not artifacts:
        return artifacts
    edit_ts_cache: dict[str, str | None] = {}
    newest_by_project: dict[str, str] = {}
    for a in artifacts:
        if (a.get("kind") or "") != "archive":
            continue
        pid = str(((a.get("metadata") or {}).get("project_id") or "")).strip()
        if not pid:
            continue
        ca = a.get("created_at") or ""
        if ca > newest_by_project.get(pid, ""):
            newest_by_project[pid] = ca
    for a in artifacts:
        if (a.get("kind") or "") != "archive":
            continue
        pid = str(((a.get("metadata") or {}).get("project_id") or "")).strip()
        if not pid:
            continue
        if pid not in edit_ts_cache:
            try:
                edit_ts_cache[pid] = await db.latest_edit_run_after(pid)
            except Exception as e:
                print(f"[artifacts] stale lookup failed for {pid}: {e}")
                edit_ts_cache[pid] = None
        edit_ts = edit_ts_cache[pid]
        ca = a.get("created_at") or ""
        a["stale"] = bool(edit_ts and ca and edit_ts > ca)
        a["latest_for_project"] = bool(ca and ca == newest_by_project.get(pid))
    return artifacts


@app.get("/api/artifacts")
async def list_artifacts_ep(
    conversation_id: Optional[str] = Query(None),
    workspace_id: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None),
    workflow_id: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    pinned: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(80, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    artifacts = await db.list_artifacts(
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        run_id=run_id,
        workflow_id=workflow_id,
        kind=kind,
        status=status,
        pinned=pinned,
        tag=tag,
        q=q,
        date_from=date_from,
        date_to=date_to,
        source=source,
        limit=limit,
        offset=offset,
    )
    return await _annotate_artifact_staleness(artifacts)


@app.get("/api/artifacts/{artifact_id}")
async def get_artifact_ep(artifact_id: str):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    # Annotate across the full lineage so `latest_for_project` reflects the whole
    # version set, not just this single row. Mutates `versions` in place too.
    await _annotate_artifact_staleness([artifact] + (artifact.get("versions") or []))
    return artifact


@app.get("/api/artifacts/{artifact_id}/download")
async def download_artifact_ep(artifact_id: str):
    """Serve THIS artifact's exact bytes (immutable), keyed on artifact id rather
    than a shared basename — so an old pill always downloads what it packaged even
    after a newer delivery overwrote the friendly /api/downloads/{name} file."""
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, safe_name = _artifact_path_for_row(artifact)
    if not filepath:
        raise HTTPException(404, "Artifact file is missing")
    download_name = os.path.basename(artifact.get("filename") or "") or safe_name
    return FileResponse(filepath, filename=download_name)


@app.get("/api/artifacts/{artifact_id}/duplicates")
async def get_artifact_duplicates_ep(artifact_id: str):
    duplicates = await db.get_artifact_duplicates(artifact_id)
    if duplicates is None:
        raise HTTPException(404, "Artifact not found")
    return {"artifact_id": artifact_id, "duplicates": duplicates}


@app.get("/api/artifacts/{artifact_id}/timeline")
async def get_artifact_timeline_ep(artifact_id: str):
    timeline = await db.get_artifact_timeline(artifact_id)
    if timeline is None:
        raise HTTPException(404, "Artifact not found")
    return {"artifact_id": artifact_id, "events": timeline}


@app.post("/api/artifacts/{artifact_id}/merge-duplicate")
async def merge_artifact_duplicate_ep(artifact_id: str, req: ArtifactMergeDuplicateRequest):
    try:
        merged = await db.merge_duplicate_artifact(artifact_id, req.duplicate_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not merged:
        raise HTTPException(404, "Artifact not found")
    return merged


@app.patch("/api/artifacts/{artifact_id}")
async def update_artifact_ep(artifact_id: str, req: ArtifactUpdate):
    artifact = await db.update_artifact(
        artifact_id,
        **{k: v for k, v in req.dict(exclude_unset=True).items()},
    )
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    return artifact


@app.delete("/api/artifacts/{artifact_id}")
async def delete_artifact_ep(artifact_id: str):
    # Resolve the on-disk file BEFORE the row disappears
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, _ = _artifact_path_for_row(artifact)
    ok = await db.delete_artifact(artifact_id)
    if not ok:
        raise HTTPException(404, "Artifact not found")
    # Delete the file too — but only sandbox-output files (never KB/upload
    # paths), and only when no other artifact row still references it.
    deleted_file = False
    if filepath:
        outputs_root = os.path.abspath(config.SANDBOX_OUTPUTS_DIR) + os.sep
        in_outputs = os.path.abspath(filepath).startswith(outputs_root)
        refs = await db.count_artifacts_with_storage_path(artifact.get("storage_path") or "", exclude_id=artifact_id)
        if in_outputs and refs == 0:
            try:
                os.remove(filepath)
                deleted_file = True
            except OSError as e:
                print(f"[ARTIFACT] file delete failed for {filepath}: {e}")
    return {"status": "deleted", "deleted_file": deleted_file}


@app.get("/api/artifacts/{artifact_id}/preview")
async def preview_artifact_ep(artifact_id: str, max_chars: int = Query(200000, ge=1000, le=1000000)):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, safe_name = _artifact_path_for_row(artifact)
    if not filepath:
        updated = await db.update_artifact_file_metadata(artifact_id, exists_status="missing")
        return {
            **(updated or artifact),
            "id": artifact_id,
            "filename": safe_name or artifact.get("filename") or "",
            "kind": artifact.get("kind") or "file",
            "mime_type": artifact.get("mime_type") or "",
            "exists_status": "missing",
            "preview_type": "missing",
            "error": "File not found. It may have been cleaned up.",
        }

    kind = (artifact.get("kind") or "file").lower()
    lower_name = (safe_name or artifact.get("filename") or "").lower()
    file_meta = _artifact_file_metadata(filepath)
    if kind == "archive":
        try:
            return {
                **artifact,
                "preview": _archive_contents_for_path(filepath, safe_name),
                "preview_type": "archive",
                "file_size": file_meta["size_bytes"],
                "download_url": artifact.get("url"),
            }
        except HTTPException as e:
            return JSONResponse({"error": e.detail, "preview_type": "archive"}, status_code=e.status_code)
        except Exception as e:
            return JSONResponse({"error": str(e), "preview_type": "archive"}, status_code=500)
    if kind in {"image", "pdf"}:
        return {
            **artifact,
            "preview_type": kind,
            "file_size": file_meta["size_bytes"],
            "sha256": file_meta["sha256"],
            "download_url": artifact.get("url"),
        }
    if not _artifact_text_preview_allowed(artifact):
        return {
            **artifact,
            "preview_type": "metadata",
            "file_size": file_meta["size_bytes"],
            "sha256": file_meta["sha256"],
            "message": "Binary preview is not available for this artifact type.",
        }
    try:
        def _read_preview_bytes():
            with open(filepath, "rb") as f:
                return f.read(min(max_chars * 4, 4 * 1024 * 1024))
        raw = await asyncio.to_thread(_read_preview_bytes)
        content = _decode_preview_bytes(raw, {"content-type": artifact.get("mime_type") or ""})[:max_chars]
        truncated = os.path.getsize(filepath) > len(raw) or len(content) >= max_chars
        if lower_name.endswith((".csv", ".tsv")):
            delimiter = "\t" if lower_name.endswith(".tsv") else ","
            reader = csv.reader(io.StringIO(content), delimiter=delimiter)
            parsed = list(reader)
            columns = parsed[0] if parsed else []
            rows = parsed[1:201] if len(parsed) > 1 else []
            return {
                **artifact,
                "preview_type": "table",
                "table_type": "csv" if delimiter == "," else "tsv",
                "columns": columns,
                "rows": rows,
                "row_count": max(0, len(parsed) - 1),
                "truncated": truncated or len(parsed) > 201,
                "file_size": file_meta["size_bytes"],
            }
        if lower_name.endswith((".json", ".jsonl", ".ndjson")):
            parsed = None
            parse_error = ""
            try:
                if lower_name.endswith((".jsonl", ".ndjson")):
                    parsed = [json.loads(line) for line in content.splitlines() if line.strip()][:200]
                else:
                    parsed = json.loads(content)
            except Exception as e:
                parse_error = str(e)
            return {
                **artifact,
                "preview_type": "json",
                "content": content,
                "json": parsed,
                "parse_error": parse_error,
                "truncated": truncated,
                "file_size": file_meta["size_bytes"],
            }
        if kind == "markdown" or lower_name.endswith((".md", ".markdown", ".mdx")):
            return {
                **artifact,
                "preview_type": "markdown",
                "content": content,
                "html": _render_markdown_safe(content),
                "truncated": truncated,
                "file_size": file_meta["size_bytes"],
            }
        if kind == "html" or lower_name.endswith((".html", ".htm")):
            title_match = re.search(r"<title[^>]*>(.*?)</title>", content, flags=re.I | re.S)
            return {
                **artifact,
                "preview_type": "html",
                "content": _sanitize_preview_html(content, artifact.get("url") or ""),
                "metadata": {"title": html.unescape(re.sub(r"\s+", " ", title_match.group(1)).strip()) if title_match else ""},
                "download_url": artifact.get("url"),
                "truncated": truncated,
                "file_size": file_meta["size_bytes"],
            }
        return {
            **artifact,
            "preview_type": "text",
            "content": content,
            "language": _language_hint(safe_name, kind),
            "truncated": truncated,
            "file_size": file_meta["size_bytes"],
        }
    except Exception as e:
        return JSONResponse({"error": f"Failed to read preview: {e}"}, status_code=500)


@app.get("/api/artifacts/{artifact_id}/archive-entry")
async def preview_artifact_archive_entry_ep(artifact_id: str, path: str = Query(..., min_length=1)):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    if (artifact.get("kind") or "").lower() != "archive":
        raise HTTPException(400, "Artifact is not an archive")
    filepath, safe_name = _artifact_path_for_row(artifact)
    if not filepath:
        await db.update_artifact_file_metadata(artifact_id, exists_status="missing")
        raise HTTPException(404, "Artifact file is missing")
    try:
        return _archive_entry_preview_for_path(filepath, safe_name or artifact.get("filename") or "", path)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to preview archive entry: {e}")


@app.post("/api/artifacts/{artifact_id}/check-file")
async def check_artifact_file_ep(artifact_id: str):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, _safe_name = _artifact_path_for_row(artifact)
    if not filepath:
        updated = await db.update_artifact_file_metadata(artifact_id, exists_status="missing")
        return updated or {**artifact, "exists_status": "missing"}
    meta = _artifact_file_metadata(filepath)
    content_text = _extract_indexable_text(filepath, artifact.get("kind") or "", artifact.get("mime_type") or "")
    updated = await db.update_artifact_file_metadata(
        artifact_id,
        storage_path=filepath,
        size_bytes=meta["size_bytes"],
        sha256=meta["sha256"],
        exists_status="present",
        content_text=content_text,
    )
    return updated


@app.post("/api/artifacts/{artifact_id}/use-in-chat")
async def use_artifact_in_chat_ep(artifact_id: str, req: ArtifactUseInChatRequest):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, _safe_name = _artifact_path_for_row(artifact)
    content = (artifact.get("content_text") or "").strip()
    if not content and filepath:
        content = _extract_indexable_text(filepath, artifact.get("kind") or "", artifact.get("mime_type") or "", max_chars=req.max_chars)
    if not content:
        content = f"[Artifact: {artifact.get('title') or artifact.get('filename')} ({artifact.get('kind') or 'file'})]"
    content = content[: max(1000, min(req.max_chars or 20000, 100000))]
    attachment = {
        "name": artifact.get("title") or artifact.get("filename") or artifact_id,
        "content": content,
        "type": "text",
        "artifact_id": artifact_id,
        "url": artifact.get("url"),
    }
    return {
        "artifact_id": artifact_id,
        "conversation_id": req.conversation_id,
        "attachment": attachment,
        "context": f"Artifact: {attachment['name']}\n{content}",
    }


@app.post("/api/artifacts/{artifact_id}/add-to-kb")
async def add_artifact_to_kb_ep(artifact_id: str, req: ArtifactAddToKBRequest):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    owned = await db.get_kb(req.kb_id)
    if not owned:
        raise HTTPException(404, "KB not found")
    filepath, _safe_name = _artifact_path_for_row(artifact)
    content = (artifact.get("content_text") or "").strip()
    if not content and filepath:
        content = _extract_indexable_text(filepath, artifact.get("kind") or "", artifact.get("mime_type") or "", max_chars=req.max_chars)
    if not content or (artifact.get("kind") or "").lower() in {"image", "pdf"}:
        raise HTTPException(400, "Artifact does not have safe text content to import")
    kb_dir = os.path.join(config.KB_DIR, req.kb_id)
    os.makedirs(kb_dir, exist_ok=True)
    base_name = os.path.basename(req.filename or artifact.get("filename") or f"{artifact_id}.txt")
    if not os.path.splitext(base_name)[1]:
        base_name += ".txt"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", base_name)[:180] or f"{artifact_id}.txt"
    if not safe_name.lower().endswith((".txt", ".md", ".csv", ".json", ".jsonl", ".html", ".htm", ".py", ".js", ".ts", ".css", ".sh", ".yaml", ".yml", ".toml", ".xml")):
        safe_name += ".txt"
    dest = os.path.abspath(os.path.join(kb_dir, safe_name))
    if not dest.startswith(os.path.abspath(kb_dir) + os.sep):
        raise HTTPException(400, "Invalid KB filename")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(content[: max(1000, min(req.max_chars or 500000, 1000000))])
    file_id = await db.add_kb_file(req.kb_id, safe_name, dest, os.path.getsize(dest), "text/plain")

    async def _bg_index_artifact_import():
        try:
            await rag.index_file(req.kb_id, safe_name, dest)
        except Exception as e:
            print(f"[RAG] Artifact KB import indexing failed for {safe_name}: {e}")

    _track_bg(_bg_index_artifact_import())
    await db.add_artifact_event(
        artifact_id,
        "added_to_kb",
        f"Added to knowledge base {owned.get('name') or req.kb_id}",
        {"kb_id": req.kb_id, "kb_name": owned.get("name"), "file_id": file_id, "filename": safe_name},
    )
    return {"artifact_id": artifact_id, "kb_id": req.kb_id, "file_id": file_id, "filename": safe_name, "indexing": True}


@app.post("/api/artifacts/{artifact_id}/send-to-research")
async def send_artifact_to_research_ep(artifact_id: str, req: ArtifactResearchRequest):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, _safe_name = _artifact_path_for_row(artifact)
    content = (artifact.get("content_text") or "").strip()
    if not content and filepath:
        content = _extract_indexable_text(filepath, artifact.get("kind") or "", artifact.get("mime_type") or "", max_chars=req.max_chars)
    title = req.title or f"Research: {artifact.get('title') or artifact.get('filename')}"
    query = req.query or f"Analyze this artifact: {artifact.get('title') or artifact.get('filename')}"
    report = await _create_and_start_research_report(ResearchReportCreate(
        title=title,
        query=query,
        focus=req.focus or f"Use artifact {artifact_id} as primary context.",
        report_type=req.report_type or "analyst",
        depth=req.depth,
        model=req.model,
        planner_model=req.planner_model,
        auditor_model=req.auditor_model,
        kb_ids=req.kb_ids,
        inputs=[{
            "type": "artifact",
            "artifact_id": artifact_id,
            "title": artifact.get("title") or artifact.get("filename"),
            "filename": artifact.get("filename"),
            "content": content[: max(1000, min(req.max_chars or 50000, 200000))],
        }],
    ))
    await db.add_artifact_event(
        artifact_id,
        "sent_to_research",
        f"Sent to research report {report.get('title') or report.get('id') or ''}".strip(),
        {"report_id": report.get("id"), "title": report.get("title"), "query": query},
    )
    return report


@app.post("/api/artifacts/{artifact_id}/fork-to-codebox")
async def fork_artifact_to_codebox_ep(artifact_id: str):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, safe_name = _artifact_path_for_row(artifact)
    if not filepath:
        raise HTTPException(404, "Artifact file is missing")
    size = os.path.getsize(filepath)
    if size > config.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, f"Artifact exceeds {config.MAX_UPLOAD_SIZE_MB}MB Codebox copy limit")
    def _read_b64():
        with open(filepath, "rb") as f:
            return base64.b64encode(f.read()).decode()
    b64 = await asyncio.to_thread(_read_b64)
    remote_dir = f"/root/artifacts/{shlex.quote(artifact_id)}"
    remote_path = f"/root/artifacts/{artifact_id}/{safe_name}"
    cmd = f"mkdir -p {remote_dir} && printf '%s' {shlex.quote(b64)} | base64 -d > {shlex.quote(remote_path)} && ls -lah {shlex.quote(remote_path)}"
    try:
        r = await http.post(f"{config.CODEBOX_URL}/command", json={"command": cmd, "timeout": 60}, timeout=70)
        data = r.json()
        if r.status_code >= 400 or data.get("exit_code", 0) != 0:
            raise HTTPException(502, data.get("stderr") or "Codebox copy failed")
        result = {
            "artifact_id": artifact_id,
            "path": remote_path,
            "project_path": f"/root/artifacts/{artifact_id}",
            "filename": safe_name,
            "size_bytes": size,
            "stdout": data.get("stdout", "")[:1000],
        }
        await db.add_artifact_event(
            artifact_id,
            "forked_to_codebox",
            f"Copied to Codebox at {remote_path}",
            {"path": remote_path, "project_path": result["project_path"], "size_bytes": size},
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Codebox copy failed: {e}")


@app.post("/api/artifacts/{artifact_id}/revise")
async def revise_artifact_ep(artifact_id: str, req: ArtifactReviseRequest):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    filepath, safe_name = _artifact_path_for_row(artifact)
    os.makedirs(config.SANDBOX_OUTPUTS_DIR, exist_ok=True)
    filename = os.path.basename(req.filename or safe_name or artifact.get("filename") or f"{artifact_id}.txt")
    stem, ext = os.path.splitext(filename)
    if not ext:
        ext = ".txt"
    new_name = f"{stem}-revision-{uuid.uuid4().hex[:6]}{ext}"
    dest = os.path.abspath(os.path.join(config.SANDBOX_OUTPUTS_DIR, new_name))
    if not dest.startswith(os.path.abspath(config.SANDBOX_OUTPUTS_DIR) + os.sep):
        raise HTTPException(400, "Invalid revision filename")
    if req.content is not None:
        with open(dest, "w", encoding="utf-8") as f:
            f.write(req.content)
    elif filepath:
        shutil.copy2(filepath, dest)
    else:
        raise HTTPException(404, "Original artifact file is missing; provide content to revise")
    mime_type = db.artifact_mime_type_for_filename(new_name)
    kind = db.artifact_kind_for_filename(new_name, mime_type)
    meta = _artifact_file_metadata(dest)
    content_text = _extract_indexable_text(dest, kind, mime_type)
    parent_id = artifact.get("parent_artifact_id") or artifact["id"]
    new_artifact = await db.add_artifact(
        conversation_id=artifact.get("conversation_id"),
        message_id=artifact.get("message_id"),
        workspace_ids=req.workspace_ids if req.workspace_ids is not None else artifact.get("workspace_ids", []),
        run_id=artifact.get("run_id"),
        workflow_id=artifact.get("workflow_id"),
        filename=new_name,
        url=f"/api/downloads/{new_name}",
        kind=kind,
        mime_type=mime_type,
        title=req.title or f"{artifact.get('title') or artifact.get('filename')} revision",
        description=req.description if req.description is not None else artifact.get("description", ""),
        storage_path=dest,
        size_bytes=meta["size_bytes"],
        sha256=meta["sha256"],
        exists_status="present",
        parent_artifact_id=parent_id,
        supersedes_artifact_id=artifact["id"],
        content_text=content_text,
        tags=req.tags if req.tags is not None else artifact.get("tags", []),
        metadata={**(artifact.get("metadata") or {}), "source_tool": "artifact_revise", "revised_from": artifact["id"]},
    )
    await db.add_artifact_event(
        artifact["id"],
        "revised",
        f"Created revision {new_artifact.get('id')}",
        {"related_artifact_id": new_artifact.get("id"), "filename": new_name},
    )
    await db.add_artifact_event(
        new_artifact["id"],
        "supersedes",
        f"Supersedes {artifact['id']}",
        {"related_artifact_id": artifact["id"], "parent_artifact_id": parent_id},
    )
    return new_artifact


@app.post("/api/artifacts/bundle")
async def bundle_artifacts_ep(req: ArtifactBundleRequest):
    ids = []
    seen = set()
    for artifact_id in req.artifact_ids or []:
        if artifact_id and artifact_id not in seen:
            seen.add(artifact_id)
            ids.append(artifact_id)
    if not ids:
        raise HTTPException(400, "No artifacts selected")
    artifacts = []
    for artifact_id in ids[:100]:
        artifact = await db.get_artifact(artifact_id)
        if not artifact:
            raise HTTPException(404, f"Artifact not found: {artifact_id}")
        filepath, safe_name = _artifact_path_for_row(artifact)
        if not filepath:
            raise HTTPException(404, f"Artifact file is missing: {artifact.get('filename') or artifact_id}")
        artifacts.append((artifact, filepath, safe_name or os.path.basename(filepath)))
    os.makedirs(config.SANDBOX_OUTPUTS_DIR, exist_ok=True)
    raw_name = os.path.basename(req.filename or (req.title or f"artifact-bundle-{uuid.uuid4().hex[:6]}.zip"))
    if not raw_name.lower().endswith(".zip"):
        raw_name += ".zip"
    safe_bundle = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name)[:180] or f"artifact-bundle-{uuid.uuid4().hex[:6]}.zip"
    dest = os.path.abspath(os.path.join(config.SANDBOX_OUTPUTS_DIR, safe_bundle))
    if not dest.startswith(os.path.abspath(config.SANDBOX_OUTPUTS_DIR) + os.sep):
        raise HTTPException(400, "Invalid bundle filename")
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        used_names = set()
        for artifact, filepath, safe_name in artifacts:
            arcname = safe_name
            if arcname in used_names:
                stem, ext = os.path.splitext(arcname)
                arcname = f"{stem}-{artifact['id']}{ext}"
            used_names.add(arcname)
            zf.write(filepath, arcname)
    meta = _artifact_file_metadata(dest)
    content_text = _extract_indexable_text(dest, "archive", "application/zip")
    workspace_ids = req.workspace_ids
    if workspace_ids is None:
        ws_seen = set()
        workspace_ids = []
        for artifact, _path, _safe in artifacts:
            for ws_id in artifact.get("workspace_ids", []):
                if ws_id not in ws_seen:
                    ws_seen.add(ws_id)
                    workspace_ids.append(ws_id)
    bundle = await db.add_artifact(
        filename=safe_bundle,
        url=f"/api/downloads/{safe_bundle}",
        kind="archive",
        mime_type="application/zip",
        title=req.title or "Artifact bundle",
        description=f"Bundle of {len(artifacts)} artifact(s).",
        storage_path=dest,
        size_bytes=meta["size_bytes"],
        sha256=meta["sha256"],
        exists_status="present",
        status="draft",
        workspace_ids=workspace_ids,
        content_text=content_text,
        tags=["bundle"],
        metadata={"source_tool": "artifact_bundle", "artifact_ids": ids[:100]},
    )
    await db.add_artifact_event(
        bundle["id"],
        "bundled_from",
        f"Bundled {len(artifacts)} artifact(s)",
        {"related_artifact_ids": ids[:100]},
    )
    for artifact, _path, _safe in artifacts:
        await db.add_artifact_event(
            artifact["id"],
            "included_in_bundle",
            f"Included in bundle {bundle.get('id')}",
            {"related_artifact_id": bundle.get("id"), "filename": safe_bundle},
        )
    return bundle


# ============================================================
# FILE DOWNLOADS
# ============================================================
@app.get("/api/downloads/{filename}")
async def download_file_endpoint(filename: str):
    """Serve tool-generated files. Looks in sandbox/outputs first, falls back to legacy UPLOAD_DIR."""
    filepath, safe_name = _resolve_download_path(filename)
    if filepath:
        return FileResponse(filepath, filename=safe_name)
    return JSONResponse({"error": "File not found"}, status_code=404)


@app.get("/api/downloads/{filename}/contents")
async def archive_contents(filename: str):
    """List files inside a .tar.gz or .zip archive for preview."""
    filepath, safe_name = _resolve_download_path(filename)
    if not filepath:
        return JSONResponse({"error": "File not found"}, status_code=404)
    try:
        return _archive_contents_for_path(filepath, safe_name)
    except HTTPException as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
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
        {"id": "deep_research", "name": "🔬 Agent Research", "description": "Agent-focused web research for current APIs, coding blockers, repeated errors, and concise implementation guidance", "icon": "search", "builtin": True},
        {"id": "conspiracy_research", "name": "🕵️ Conspiracy Research", "description": "Uncensored deep-dive into theories, cover-ups, and hidden agendas", "icon": "search", "builtin": True},
    ]


@app.post("/api/seed/all-defaults")
async def seed_all_defaults():
    return await _seed_all_defaults()

@app.post("/api/seed/coder-bot")
async def seed_coder_bot():
    return await _seed_coder_bot()

@app.post("/api/seed/coder-bot-v2")
async def seed_coder_bot_v2():
    return await _seed_coder_bot_v2()

@app.post("/api/seed/conspiracy-bot")
async def seed_conspiracy_bot():
    return await _seed_conspiracy_bot()

@app.post("/api/seed/based-bot")
async def seed_based_bot():
    return await _seed_based_bot()


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
    try:
        results = await rag.reindex_kb(kb_id, files)
    except Exception as e:
        raise HTTPException(500, f"Reindex failed for {kb.get('name', kb_id)}: {e}")
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
            results = await rag.reindex_kb(kb["id"], files)
            all_results.append({"kb_id": kb["id"], "name": kb["name"], "results": results})
        except Exception as e:
            # One broken KB shouldn't abort the rest of the sweep.
            errors.append({"kb_id": kb["id"], "name": kb["name"], "error": str(e)[:300]})
    if errors and not all_results:
        raise HTTPException(500, f"Reindex failed: {errors[0]['name']}: {errors[0]['error']}")
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
            sha = hashlib.sha256(open(filepath, "rb").read()).hexdigest()
            artifact = await db.add_artifact(
                filename=filename,
                url=url,
                kind="image",
                mime_type="image/png",
                storage_path=filepath,
                size_bytes=os.path.getsize(filepath),
                sha256=sha,
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
    return {"status": "done", "images": images, "params": params}


@app.post("/api/images/jobs/{job_id}/cancel")
async def cancel_image_job(job_id: str):
    if not config.COMFYUI_URL:
        raise HTTPException(503, "ComfyUI is not configured")
    await comfyui.cancel(job_id)
    if job_id in _image_jobs:
        _image_jobs[job_id]["status"] = "error"
        _image_jobs[job_id]["error"] = "cancelled"
    return {"status": "cancelled"}


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
        _image_checkpoints_cache["ts"] = time.time()
    cks = _image_checkpoints_cache["checkpoints"]
    return {
        "checkpoints": cks,
        "default": cks[0] if cks else "",
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
    settings = comfyui.load_model_settings()
    settings[checkpoint] = clean
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


# ============================================================
# AUDIO — voice STT/TTS proxy
# ============================================================
@app.post("/api/audio/transcribe")
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


@app.post("/api/audio/speech")
async def synthesize_speech(body: dict = Body(...)):
    if not config.TTS_URL:
        raise HTTPException(503, "Text-to-speech is not configured. Set the TTS URL in Settings → Connections.")
    text = voice.strip_for_tts(body.get("text") or "")
    if not text:
        raise HTTPException(400, "Nothing speakable in the provided text")
    voice_name = (body.get("voice") or "").strip()
    return StreamingResponse(voice.speech_stream(text, voice_name), media_type="audio/mpeg")


@app.get("/api/audio/voices")
async def list_tts_voices():
    if not config.TTS_URL:
        return {"voices": [], "default": config.TTS_VOICE}
    return {"voices": await voice.list_voices(), "default": config.TTS_VOICE}


# ============================================================
# TOOLS
# ============================================================
@app.get("/api/tools")
async def list_tools():
    return await db.get_tools()


@app.post("/api/tools")
async def create_tool(req: ToolCreate):
    id = f"tool-{uuid.uuid4().hex[:12]}"
    safe_name = os.path.basename(req.filename or "tool.py")
    if not safe_name or safe_name != (req.filename or ""):
        raise HTTPException(400, "Invalid filename")
    filepath = os.path.abspath(os.path.join(config.TOOLS_DIR, safe_name))
    tools_root = os.path.abspath(config.TOOLS_DIR)
    if filepath != tools_root and not filepath.startswith(tools_root + os.sep):
        raise HTTPException(400, "Invalid filename")

    await db.create_tool(id, req.name, req.description, safe_name, req.code)
    os.makedirs(config.TOOLS_DIR, exist_ok=True)
    with open(filepath, "w") as f:
        f.write(req.code)

    return {"id": id, **req.model_dump(exclude={"filename"}), "filename": safe_name}


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
# CONNECTORS
# ============================================================
def _spec_json_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _connector_tool_public(tl: dict) -> dict:
    out = dict(tl)
    out["name"] = tl.get("display_name") or tl.get("external_name") or tl.get("tool_name")
    out["description"] = tl.get("description", "")
    out["icon"] = "plug"
    out["connector"] = True
    return out


@app.get("/api/connector-tools")
async def list_connector_tools(enabled_only: bool = Query(True)):
    tools = await db.get_connector_tools(enabled_only=enabled_only)
    return [_connector_tool_public(t) for t in tools]


@app.get("/api/mcp-servers")
async def list_mcp_servers():
    return await db.get_mcp_servers()


@app.post("/api/mcp-servers")
async def create_mcp_server(req: MCPServerCreate):
    server_id = f"mcp-{uuid.uuid4().hex[:12]}"
    transport = (req.transport or "stdio").strip().lower()
    if transport not in {"stdio", "http", "streamable_http", "sse"}:
        raise HTTPException(400, "transport must be stdio, http, streamable_http, or sse")
    await db.create_mcp_server(
        server_id,
        req.name,
        transport=transport,
        command=req.command,
        url=req.url,
        args=req.args,
        env=req.env,
        headers=req.headers,
        allow_private=req.allow_private,
        enabled=req.enabled,
    )
    return await db.get_mcp_server(server_id)


@app.patch("/api/mcp-servers/{server_id}")
async def update_mcp_server(server_id: str, req: MCPServerUpdate):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    if "transport" in kwargs:
        kwargs["transport"] = (kwargs["transport"] or "stdio").strip().lower()
        if kwargs["transport"] not in {"stdio", "http", "streamable_http", "sse"}:
            raise HTTPException(400, "transport must be stdio, http, streamable_http, or sse")
    await db.update_mcp_server(server_id, **kwargs)
    return await db.get_mcp_server(server_id)


@app.delete("/api/mcp-servers/{server_id}")
async def delete_mcp_server(server_id: str):
    await db.delete_mcp_server(server_id)
    return {"status": "deleted"}


@app.post("/api/mcp-servers/{server_id}/discover")
async def discover_mcp_server(server_id: str):
    try:
        return await connectors.discover_mcp_server(http, server_id)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/mcp-servers/{server_id}/health")
async def health_mcp_server(server_id: str):
    try:
        result = await connectors.discover_mcp_server(http, server_id)
        return {"status": "ok", "tool_count": result.get("tool_count", 0)}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/mcp-servers/{server_id}/tools")
async def list_mcp_server_tools(server_id: str):
    return [_connector_tool_public(t) for t in await db.get_connector_tools("mcp", server_id, enabled_only=False)]


@app.get("/api/openapi-connectors")
async def list_openapi_connectors():
    return await db.get_openapi_connectors()


@app.post("/api/openapi-connectors")
async def create_openapi_connector(req: OpenAPIConnectorCreate):
    connector_id = f"openapi-{uuid.uuid4().hex[:12]}"
    await db.create_openapi_connector(
        connector_id,
        req.name,
        spec_url=req.spec_url,
        spec_json=_spec_json_to_text(req.spec_json),
        base_url=req.base_url,
        auth=req.auth,
        headers=req.headers,
        allow_private=req.allow_private,
        enabled=req.enabled,
    )
    return await db.get_openapi_connector(connector_id)


@app.patch("/api/openapi-connectors/{connector_id}")
async def update_openapi_connector(connector_id: str, req: OpenAPIConnectorUpdate):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    if "spec_json" in kwargs:
        kwargs["spec_json"] = _spec_json_to_text(kwargs["spec_json"])
    await db.update_openapi_connector(connector_id, **kwargs)
    return await db.get_openapi_connector(connector_id)


@app.delete("/api/openapi-connectors/{connector_id}")
async def delete_openapi_connector(connector_id: str):
    await db.delete_openapi_connector(connector_id)
    return {"status": "deleted"}


@app.post("/api/openapi-connectors/{connector_id}/discover")
async def discover_openapi_connector(connector_id: str):
    try:
        return await connectors.discover_openapi_connector(http, connector_id)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/openapi-connectors/{connector_id}/health")
async def health_openapi_connector(connector_id: str):
    try:
        result = await connectors.discover_openapi_connector(http, connector_id)
        return {"status": "ok", "tool_count": result.get("tool_count", 0)}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/openapi-connectors/{connector_id}/tools")
async def list_openapi_connector_tools(connector_id: str):
    return [_connector_tool_public(t) for t in await db.get_connector_tools("openapi", connector_id, enabled_only=False)]


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
            got_done = False
            async with http.stream("POST", f"{config.OLLAMA_URL}/api/pull",
                                   json={"name": model_name, "stream": True},
                                   timeout=httpx.Timeout(7200.0, connect=10.0)) as response:
                if response.status_code != 200:
                    err_body = (await response.aread()).decode()[:300]
                    yield f"data: {json.dumps({'error': f'Ollama returned HTTP {response.status_code}: {err_body}'})}\n\n"
                    return
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    sse, key = parse_ollama_progress(line, model_name)
                    if not sse:
                        continue
                    if key == "error":
                        yield sse
                        return
                    if key == "done":
                        got_done = True
                    yield sse
            if not got_done:
                # Stream ended without success — verify model exists
                try:
                    check = await http.post(f"{config.OLLAMA_URL}/api/show", json={"name": model_name})
                    if check.status_code == 200:
                        yield f"data: {json.dumps({'status': 'done', 'message': 'Pull complete'})}\n\n"
                    else:
                        yield f"data: {json.dumps({'error': 'Pull stream ended without confirmation'})}\n\n"
                except Exception:
                    yield f"data: {json.dumps({'error': 'Pull stream ended — could not verify model'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.delete("/api/models/{model_name:path}")
async def delete_model(model_name: str):
    """Delete a model from Ollama. Tries alternate name formats if not found."""
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
                                   json={"name": name})
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
        if r.status_code == 404:
            raise HTTPException(404, f"Model '{model_name}' not found in Ollama")
        r.raise_for_status()
        return r.json()
    except HTTPException:
        raise
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
    existing = await db.get_model_config(mc_id)
    if not existing:
        raise HTTPException(404, "Model config not found")
    avatar_dir = os.path.join(config.UPLOAD_DIR, "avatars")
    os.makedirs(avatar_dir, exist_ok=True)

    fname = file.filename or ""
    raw_ext = fname.rsplit(".", 1)[-1].lower()[:10] if "." in fname else "png"
    if raw_ext not in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
        raise HTTPException(400, "Invalid image type — allowed: png, jpg, jpeg, gif, webp, svg")
    ext = raw_ext
    avatar_path = os.path.join(avatar_dir, f"{mc_id}.{ext}")

    content = await file.read()
    with open(avatar_path, "wb") as f:
        f.write(content)

    params = existing.get("parameters", {}) if existing else {}
    await db.update_model_config(mc_id, parameters={**params, "avatar": f"/api/avatars/{mc_id}.{ext}"})
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
    try:
        r = await web_get(
            http,
            u, timeout=8, follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; image-proxy)",
                "Accept": "image/*",
                "Referer": f"{parsed.scheme}://{parsed.netloc}/",
            },
        )
    except Exception:
        raise HTTPException(status_code=502, detail="fetch failed")
    ct = r.headers.get("content-type", "")
    if r.status_code != 200 or not ct.lower().startswith("image/"):
        raise HTTPException(status_code=404, detail="not found")
    if len(r.content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="too large")
    return Response(
        content=r.content, media_type=ct,
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
@app.get("/api/model-providers")
async def get_model_provider_settings():
    return {"providers": await model_providers.provider_statuses()}


@app.patch("/api/model-providers/{provider}")
async def update_model_provider_settings(provider: str, body: dict = Body(...)):
    if provider not in model_providers.CLOUD_PROVIDERS:
        raise HTTPException(404, "Unsupported model provider")
    api_key = body.get("api_key")
    enabled = body.get("enabled") if "enabled" in body else None
    try:
        status = await model_providers.save_provider(
            provider,
            api_key=api_key if isinstance(api_key, str) and api_key.strip() else None,
            enabled=bool(enabled) if enabled is not None else None,
        )
        return status
    except model_providers.ProviderError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/model-providers/{provider}")
async def delete_model_provider_settings(provider: str):
    if provider not in model_providers.CLOUD_PROVIDERS:
        raise HTTPException(404, "Unsupported model provider")
    try:
        return await model_providers.delete_provider(provider)
    except model_providers.ProviderError as e:
        raise HTTPException(400, str(e))


@app.post("/api/model-providers/{provider}/test")
async def test_model_provider_settings(provider: str, body: dict = Body(default={})):
    if provider not in model_providers.CLOUD_PROVIDERS:
        raise HTTPException(404, "Unsupported model provider")
    api_key = body.get("api_key") if isinstance(body, dict) else None
    try:
        return await model_providers.test_provider(
            http,
            provider,
            api_key=api_key if isinstance(api_key, str) and api_key.strip() else None,
        )
    except model_providers.ProviderError as e:
        raise HTTPException(e.status_code or 400, str(e))
    except Exception as e:
        raise HTTPException(400, str(e))


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
        "aider_enabled": config.AIDER_ENABLED,
        "aider_model": config.AIDER_MODEL,
        "aider_num_ctx": config.AIDER_NUM_CTX,
        "aider_auto_test": config.AIDER_AUTO_TEST,
        "aider_worker_url": config.AIDER_WORKER_URL,
        "default_num_ctx": config.DEFAULT_NUM_CTX,
        "research_num_ctx": config.RESEARCH_NUM_CTX,
        "quick_search_mode": config.QUICK_SEARCH_MODE,
        "sandbox_dir": config.SANDBOX_DIR,
        "sandbox_outputs_dir": config.SANDBOX_OUTPUTS_DIR,
        "sandbox_size_bytes": size,
        "sandbox_file_count": file_count,
        "sandbox_venv_ready": venv_exists,
    }


@app.patch("/api/settings")
async def update_app_settings(body: dict = Body(...)):
    settings = load_settings()
    allowed = {"file_cleanup_days", "ollama_url", "codebox_url", "searxng_url", "n8n_url",
               "comfyui_url", "stt_url", "tts_url", "tts_voice",
               "rag", "planning_model", "coder_model",
               "workspace_model",
               "architect_model", "reviewer_model", "acceptance_model", "builder_model", "fixer_model", "qa_model",
               "openhands_enabled", "openhands_max_rounds", "openhands_num_ctx",
               "openhands_reasoning_effort",
               "aider_enabled", "aider_model", "aider_num_ctx", "aider_auto_test", "aider_worker_url",
               "default_num_ctx", "research_num_ctx", "quick_search_mode"}
    for k, v in body.items():
        if k in allowed:
            settings[k] = v
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
        print(f"[Config] Updated TTS URL to: {config.TTS_URL}")
    elif "tts_url" in body and not body["tts_url"]:
        settings["tts_url"] = ""
        config.TTS_URL = os.getenv("TTS_URL", "")
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
    if "aider_enabled" in body:
        config.AIDER_ENABLED = bool(body["aider_enabled"])
        print(f"[Config] Aider enabled: {config.AIDER_ENABLED}")
    if "aider_model" in body:
        config.AIDER_MODEL = body["aider_model"] or ""
        print(f"[Config] Aider Model: {config.AIDER_MODEL or '(inherit fixer/coder)'}")
    if "aider_num_ctx" in body:
        config.AIDER_NUM_CTX = config.coerce_num_ctx(
            body["aider_num_ctx"],
            fallback=config.AIDER_NUM_CTX,
        )
        settings["aider_num_ctx"] = config.AIDER_NUM_CTX
        print(f"[Config] Aider num_ctx: {config.AIDER_NUM_CTX}")
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
    save_settings(settings)
    return {
        **settings,
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
        "aider_enabled": config.AIDER_ENABLED,
        "aider_model": config.AIDER_MODEL,
        "aider_num_ctx": config.AIDER_NUM_CTX,
        "aider_auto_test": config.AIDER_AUTO_TEST,
        "aider_worker_url": config.AIDER_WORKER_URL,
        "default_num_ctx": config.DEFAULT_NUM_CTX,
        "research_num_ctx": config.RESEARCH_NUM_CTX,
        "quick_search_mode": config.QUICK_SEARCH_MODE,
    }


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
# TOKEN USAGE ANALYTICS
# ============================================================
@app.get("/api/analytics/tokens")
async def get_token_analytics(days: int = Query(30), group_by: str = Query("day")):
    if group_by not in ("day", "model", "persona"):
        raise HTTPException(400, "group_by must be: day, model, or persona")
    return await db.get_token_usage(days, group_by)


@app.get("/api/analytics/tokens/summary")
async def get_token_summary():
    today = await db.get_token_usage(1, "day")
    week = await db.get_token_usage(7, "model")
    month = await db.get_token_usage(30, "day")
    return {"today": today, "by_model_7d": week, "daily_30d": month}


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
