"""
ComfyUI client — local Stable Diffusion image generation.

HTTP API: POST /prompt → prompt_id, poll GET /history/{id}, fetch bytes via
GET /view. Workflow templates are API-format graphs; build_workflow patches
parameters by class_type + graph-link following (not hardcoded node IDs) so
any user-exported API-format workflow works as a COMFYUI_WORKFLOW_PATH
override. All client helpers read config.COMFYUI_URL at call time so a
settings PATCH applies live.
"""
import copy
import json
import os
import random

import httpx

import config

# Embedded default: SDXL txt2img using the canonical node IDs (3-9) from the
# stock ComfyUI workflow. Placeholders are patched by build_workflow().
SDXL_T2I_TEMPLATE = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0,
            "steps": 25,
            "cfg": 7.0,
            "sampler_name": "euler",
            "scheduler": "normal",
            "denoise": 1.0,
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"},
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
    },
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["4", 1]},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["4", 1]},
    },
    "8": {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
    },
    "9": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "hyprchat", "images": ["8", 0]},
    },
}


def load_template() -> dict:
    """Return the workflow template: COMFYUI_WORKFLOW_PATH override or embedded SDXL."""
    path = getattr(config, "COMFYUI_WORKFLOW_PATH", "") or ""
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                wf = json.load(f)
            if isinstance(wf, dict) and wf:
                return wf
            print(f"[COMFYUI] Workflow override {path} is not a dict — using embedded SDXL")
        except Exception as e:
            print(f"[COMFYUI] Failed to load workflow override {path}: {e}")
    return SDXL_T2I_TEMPLATE


def _find_by_class(workflow: dict, class_type: str) -> str | None:
    for node_id, node in workflow.items():
        if isinstance(node, dict) and node.get("class_type") == class_type:
            return node_id
    return None


def _clamp_dim(v, default: int) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    v = max(256, min(2048, v))
    return (v // 8) * 8


ALLOWED_SAMPLERS = {
    "euler", "euler_ancestral", "heun", "dpm_2", "dpm_2_ancestral", "lms",
    "dpmpp_2s_ancestral", "dpmpp_sde", "dpmpp_2m", "dpmpp_2m_sde",
    "dpmpp_3m_sde", "ddim", "uni_pc", "lcm",
}
ALLOWED_SCHEDULERS = {"normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform", "beta"}


def build_workflow(template: dict, *, prompt: str, negative_prompt: str = "",
                   width: int = 1024, height: int = 1024, steps: int = 25,
                   cfg: float = 7.0, seed: int | None = None,
                   checkpoint: str = "", batch_size: int = 1,
                   sampler_name: str = "", scheduler: str = "",
                   v_prediction: bool = False) -> tuple[dict, int]:
    """Patch a copy of an API-format workflow with the requested parameters.

    Locates nodes by class_type and follows the KSampler's positive/negative
    graph links to the right CLIPTextEncode nodes. Raises ValueError when a
    required node is missing.
    """
    wf = copy.deepcopy(template)

    sampler_id = _find_by_class(wf, "KSampler")
    if not sampler_id:
        raise ValueError("Workflow has no KSampler node")
    sampler = wf[sampler_id]["inputs"]

    if seed is None:
        seed = random.randint(0, 2**32 - 1)
    sampler["seed"] = int(seed)
    try:
        steps = int(steps)
    except (TypeError, ValueError):
        steps = 25
    sampler["steps"] = max(1, min(60, steps))
    try:
        sampler["cfg"] = float(cfg)
    except (TypeError, ValueError):
        pass
    if sampler_name and sampler_name in ALLOWED_SAMPLERS:
        sampler["sampler_name"] = sampler_name
    if scheduler and scheduler in ALLOWED_SCHEDULERS:
        sampler["scheduler"] = scheduler

    # v-prediction checkpoints (BigASP v2.5, NoobAI vpred, ...) produce solid-color
    # garbage when sampled as epsilon. Splice ModelSamplingDiscrete(v_prediction,
    # zsnr) + RescaleCFG between the sampler's existing model source and the
    # sampler, preserving any LoRA chain a custom workflow may have.
    if v_prediction:
        model_src = sampler.get("model")
        if isinstance(model_src, list) and model_src:
            wf["vpred_ms"] = {
                "class_type": "ModelSamplingDiscrete",
                "inputs": {"sampling": "v_prediction", "zsnr": True, "model": model_src},
            }
            wf["vpred_rc"] = {
                "class_type": "RescaleCFG",
                "inputs": {"multiplier": 0.7, "model": ["vpred_ms", 0]},
            }
            sampler["model"] = ["vpred_rc", 0]

    # Follow graph links to the prompt encoders
    for link_key, text in (("positive", prompt), ("negative", negative_prompt)):
        link = sampler.get(link_key)
        if not (isinstance(link, list) and link):
            raise ValueError(f"KSampler has no {link_key} link")
        enc_id = str(link[0])
        enc = wf.get(enc_id)
        if not enc or enc.get("class_type") != "CLIPTextEncode":
            raise ValueError(f"KSampler {link_key} link does not point at a CLIPTextEncode node")
        enc["inputs"]["text"] = text or ""

    latent_id = _find_by_class(wf, "EmptyLatentImage")
    if not latent_id:
        raise ValueError("Workflow has no EmptyLatentImage node")
    latent = wf[latent_id]["inputs"]
    latent["width"] = _clamp_dim(width, 1024)
    latent["height"] = _clamp_dim(height, 1024)
    try:
        batch_size = int(batch_size)
    except (TypeError, ValueError):
        batch_size = 1
    latent["batch_size"] = max(1, min(4, batch_size))

    if checkpoint:
        ckpt_id = _find_by_class(wf, "CheckpointLoaderSimple")
        if ckpt_id:
            wf[ckpt_id]["inputs"]["ckpt_name"] = checkpoint

    save_id = _find_by_class(wf, "SaveImage")
    if save_id:
        wf[save_id]["inputs"]["filename_prefix"] = "hyprchat"

    return wf, int(seed)


# ── Async client helpers ──

def _base() -> str:
    return (config.COMFYUI_URL or "").rstrip("/")


async def submit(workflow: dict) -> str:
    """Queue a workflow; returns the prompt_id."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{_base()}/prompt", json={"prompt": workflow})
        r.raise_for_status()
        data = r.json()
    pid = data.get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI rejected the workflow: {json.dumps(data)[:300]}")
    return pid


async def get_history(prompt_id: str) -> dict | None:
    """History entry for a prompt, or None while still queued/executing."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{_base()}/history/{prompt_id}")
        r.raise_for_status()
        data = r.json()
    return data.get(prompt_id)


async def queue_position(prompt_id: str) -> int | None:
    """0 = running now, N = waiting behind N jobs, None = not in queue."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_base()}/queue")
            r.raise_for_status()
            q = r.json()
        for item in q.get("queue_running", []):
            if len(item) > 1 and item[1] == prompt_id:
                return 0
        for i, item in enumerate(q.get("queue_pending", [])):
            if len(item) > 1 and item[1] == prompt_id:
                return i + 1
    except Exception:
        pass
    return None


def outputs_from_history(history: dict) -> list[dict]:
    """Flatten output images from a history entry: [{filename, subfolder, type}]."""
    images = []
    for node_output in (history.get("outputs") or {}).values():
        for img in node_output.get("images") or []:
            if img.get("type") == "output":
                images.append(img)
    return images


async def fetch_image(image: dict) -> bytes:
    """Download one output image's bytes via GET /view."""
    params = {
        "filename": image.get("filename", ""),
        "subfolder": image.get("subfolder", ""),
        "type": image.get("type", "output"),
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(f"{_base()}/view", params=params)
        r.raise_for_status()
        return r.content


async def cancel(prompt_id: str):
    """Remove a job from the queue and interrupt it if currently running."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{_base()}/queue", json={"delete": [prompt_id]})
            await client.post(f"{_base()}/interrupt")
    except Exception as e:
        print(f"[COMFYUI] Cancel error for {prompt_id}: {e}")


async def free_memory():
    """Ask ComfyUI to unload models and free VRAM so Ollama gets the GPU back
    between generations. Best-effort — never fails the calling job."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{_base()}/free", json={"unload_models": True, "free_memory": True})
    except Exception as e:
        print(f"[COMFYUI] free_memory error: {e}")


async def list_checkpoints() -> list[str]:
    """Available checkpoint names from /object_info."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{_base()}/object_info/CheckpointLoaderSimple")
            r.raise_for_status()
            info = r.json()
        opts = (info.get("CheckpointLoaderSimple", {})
                    .get("input", {}).get("required", {}).get("ckpt_name", []))
        if opts and isinstance(opts[0], list):
            return [str(c) for c in opts[0]]
    except Exception as e:
        print(f"[COMFYUI] list_checkpoints error: {e}")
    return []


async def check_health(client: httpx.AsyncClient) -> dict:
    """Health-check shape matching main._check_service results."""
    import time
    t0 = time.time()
    try:
        r = await client.get(f"{_base()}/system_stats", timeout=8)
        ms = int((time.time() - t0) * 1000)
        if r.status_code < 400:
            return {"status": "degraded" if ms > 3000 else "ok", "response_ms": ms}
        return {"status": "error", "response_ms": ms, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return {"status": "error", "response_ms": ms, "error": str(e)[:200]}
