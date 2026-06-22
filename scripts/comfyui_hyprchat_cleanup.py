"""HyprChat control routes for ComfyUI.

Vanilla ComfyUI has no API to delete saved output files, so HyprChat's
"Delete all" purge can't remove ComfyUI's own copies of generated images
without help. This custom node adds that route plus model-RAM controls:

    POST /hyprchat/cleanup  ->  {"deleted": n, "errors": m}
    POST /hyprchat/free     ->  unload idle models now
    POST /hyprchat/restart  ->  restart the ComfyUI service/process
    GET  /hyprchat/memory   ->  queue/idleness and memory status

It deletes ONLY HyprChat-generated images — files whose name starts with
the "hyprchat" filename prefix that HyprChat stamps on every workflow it
submits — from ComfyUI's output and temp directories. Your own ComfyUI
canvas outputs are untouched.

It also unloads resident ComfyUI models after HYPRCHAT_COMFY_IDLE_UNLOAD_SECONDS
seconds with no running or queued prompt. Default: 300 seconds.

Install (inside the ComfyUI LXC):
    cp comfyui_hyprchat_cleanup.py /opt/comfyui/custom_nodes/
    systemctl restart comfyui
"""
import asyncio
import ctypes
import gc
import os
import shlex
import subprocess
import time

import folder_paths
from aiohttp import web
import comfy.model_management as model_management
from server import PromptServer

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_IDLE_UNLOAD_SECONDS = max(0, int(os.getenv("HYPRCHAT_COMFY_IDLE_UNLOAD_SECONDS", "300")))
_WATCH_INTERVAL_SECONDS = max(5, int(os.getenv("HYPRCHAT_COMFY_WATCH_INTERVAL_SECONDS", "10")))
_RESTART_COMMAND = os.getenv("HYPRCHAT_COMFY_RESTART_COMMAND", "systemctl restart comfyui")

_last_busy_at = time.monotonic()
_unloaded_for_current_idle = False
_last_unload_at = None
_watchdog_task = None


def _count_queue_items(value) -> int:
    if value is None:
        return 0
    try:
        return len(value)
    except TypeError:
        return 0


def _queue_counts() -> tuple[int | None, int | None]:
    """Return (running, pending), or (None, None) if ComfyUI internals changed."""
    queue = getattr(PromptServer.instance, "prompt_queue", None)
    getter = getattr(queue, "get_current_queue", None)
    if not callable(getter):
        return None, None
    try:
        running, pending = getter()
    except Exception as exc:
        print(f"[HyprChat ComfyUI] queue check failed: {exc}")
        return None, None
    return _count_queue_items(running), _count_queue_items(pending)


def _queue_status() -> dict:
    running, pending = _queue_counts()
    if running is None or pending is None:
        return {"queue_known": False, "running": None, "pending": None, "active": True}
    return {
        "queue_known": True,
        "running": running,
        "pending": pending,
        "active": (running + pending) > 0,
    }


def _read_proc_meminfo() -> dict:
    wanted = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    out = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in wanted:
                    out[key] = int(rest.strip().split()[0]) * 1024
    except Exception:
        pass
    return out


def _read_process_rss() -> int | None:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return None


def _cuda_meminfo() -> dict:
    try:
        import torch

        if not torch.cuda.is_available():
            return {}
        device = model_management.get_torch_device()
        free, total = torch.cuda.mem_get_info(device)
        out = {"cuda_device": str(device), "vram_free": free, "vram_total": total}
        try:
            out["torch_allocated"] = torch.cuda.memory_allocated(device)
            out["torch_reserved"] = torch.cuda.memory_reserved(device)
        except Exception:
            pass
        return out
    except Exception:
        return {}


def _loaded_model_count() -> int | None:
    """Return ComfyUI's loaded-model count across old/new internals."""
    try:
        fn = getattr(model_management, "loaded_models", None)
        if callable(fn):
            loaded = fn()
            return len(loaded) if loaded is not None else 0
    except Exception:
        pass
    for attr in ("current_loaded_models", "loaded_models"):
        value = getattr(model_management, attr, None)
        if isinstance(value, list):
            return len(value)
    return None


def _status_payload() -> dict:
    now = time.monotonic()
    q = _queue_status()
    idle_seconds = 0 if q["active"] else int(max(0, now - _last_busy_at))
    mem = _read_proc_meminfo()
    rss = _read_process_rss()
    if rss is not None:
        mem["ProcessRSS"] = rss
    mem.update(_cuda_meminfo())
    loaded_models = _loaded_model_count()
    return {
        **q,
        "idle_seconds": idle_seconds,
        "idle_unload_seconds": _IDLE_UNLOAD_SECONDS,
        "unloaded_for_current_idle": _unloaded_for_current_idle,
        "last_unload_at": _last_unload_at,
        "loaded_models": loaded_models,
        "memory": mem,
    }


def _soft_empty_cache():
    try:
        model_management.soft_empty_cache(force=True)
    except TypeError:
        model_management.soft_empty_cache()


def _malloc_trim():
    """Return freed glibc arenas to the OS when available.

    PyTorch/ComfyUI can unload model objects while the Python process keeps
    large freed CPU arenas mapped. That makes Proxmox/LXC memory graphs look
    unchanged even though the objects are gone. malloc_trim(0) is Linux/glibc
    only, so failure is harmless.
    """
    try:
        libc = ctypes.CDLL("libc.so.6")
        trim = getattr(libc, "malloc_trim", None)
        if trim:
            trim(0)
            return True
    except Exception:
        pass
    return False


def _unload_once():
    model_management.unload_all_models()
    cleanup_gc = getattr(model_management, "cleanup_models_gc", None)
    if callable(cleanup_gc):
        cleanup_gc()
    cleanup_models = getattr(model_management, "cleanup_models", None)
    if callable(cleanup_models):
        cleanup_models()
    gc.collect()
    _soft_empty_cache()
    _malloc_trim()


def _unload_models_sync(reason: str) -> dict:
    global _last_unload_at, _unloaded_for_current_idle

    before = _status_payload()
    unload_passes = 0
    for _ in range(3):
        unload_passes += 1
        _unload_once()
        count = _loaded_model_count()
        if count in (0, None):
            break
    _last_unload_at = time.time()
    _unloaded_for_current_idle = True
    after = _status_payload()
    ok = after.get("loaded_models") in (0, None)
    print(
        f"[HyprChat ComfyUI] unloaded models ({reason}); "
        f"loaded_models={after.get('loaded_models')} passes={unload_passes}"
    )
    return {
        "ok": ok,
        "reason": reason,
        "before": before,
        "after": after,
        "unload_passes": unload_passes,
        "loaded_models_before": before.get("loaded_models"),
        "loaded_models_after": after.get("loaded_models"),
    }


async def _unload_models(reason: str) -> dict:
    return await asyncio.to_thread(_unload_models_sync, reason)


async def _restart_after_response(reason: str, delay: float = 0.35):
    await asyncio.sleep(delay)
    try:
        args = shlex.split(_RESTART_COMMAND)
        if not args:
            raise RuntimeError("restart command is empty")
        print(f"[HyprChat ComfyUI] restarting ComfyUI ({reason}): {_RESTART_COMMAND}")
        subprocess.Popen(args, start_new_session=True)
    except Exception as exc:
        print(f"[HyprChat ComfyUI] restart command failed: {exc}")


async def _idle_watchdog():
    global _last_busy_at, _unloaded_for_current_idle

    if _IDLE_UNLOAD_SECONDS <= 0:
        print("[HyprChat ComfyUI] idle model unload disabled")
        return

    print(f"[HyprChat ComfyUI] idle model unload enabled: {_IDLE_UNLOAD_SECONDS}s")
    while True:
        await asyncio.sleep(_WATCH_INTERVAL_SECONDS)
        q = _queue_status()
        now = time.monotonic()
        if q["active"]:
            _last_busy_at = now
            _unloaded_for_current_idle = False
            continue
        if not q["queue_known"]:
            continue
        if _unloaded_for_current_idle:
            continue
        if now - _last_busy_at < _IDLE_UNLOAD_SECONDS:
            continue
        try:
            await _unload_models("idle_timeout")
        except Exception as exc:
            print(f"[HyprChat ComfyUI] idle unload failed: {exc}")


def _ensure_watchdog_started():
    global _watchdog_task

    if _watchdog_task and not _watchdog_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _watchdog_task = loop.create_task(_idle_watchdog())


async def _on_startup(app):
    _ensure_watchdog_started()


def _register_watchdog_startup():
    app = getattr(PromptServer.instance, "app", None)
    startup = getattr(app, "on_startup", None)
    if startup is not None:
        try:
            startup.append(_on_startup)
        except Exception as exc:
            print(f"[HyprChat ComfyUI] startup hook registration skipped: {exc}")


_register_watchdog_startup()


@PromptServer.instance.routes.post("/hyprchat/cleanup")
async def hyprchat_cleanup(request):
    _ensure_watchdog_started()
    deleted = 0
    errors = 0
    for base in (folder_paths.get_output_directory(), folder_paths.get_temp_directory()):
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for name in entries:
            if name.startswith("hyprchat") and name.lower().endswith(_IMAGE_EXTS):
                try:
                    os.remove(os.path.join(base, name))
                    deleted += 1
                except OSError:
                    errors += 1
    return web.json_response({"deleted": deleted, "errors": errors})


@PromptServer.instance.routes.post("/hyprchat/free")
async def hyprchat_free(request):
    _ensure_watchdog_started()
    q = _queue_status()
    if q["active"]:
        return web.json_response(
            {"ok": False, "error": "ComfyUI queue is active", **q},
            status=409,
        )
    try:
        data = await _unload_models("manual")
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)[:300]}, status=500)
    return web.json_response(data)


@PromptServer.instance.routes.post("/hyprchat/restart")
async def hyprchat_restart(request):
    _ensure_watchdog_started()
    q = _queue_status()
    if q["active"]:
        return web.json_response(
            {"ok": False, "error": "ComfyUI queue is active", **q},
            status=409,
        )
    payload = {
        "ok": True,
        "status": "restart_scheduled",
        "restart_command": _RESTART_COMMAND,
        "before": _status_payload(),
    }
    asyncio.create_task(_restart_after_response("manual"))
    return web.json_response(payload)


@PromptServer.instance.routes.get("/hyprchat/memory")
async def hyprchat_memory(request):
    _ensure_watchdog_started()
    return web.json_response(_status_payload())


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
