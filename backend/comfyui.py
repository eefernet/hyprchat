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
import uuid

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


def _has_model_sampling_mode(workflow: dict, mode: str) -> bool:
    """Return True only when the graph already has the requested sampler mode."""
    mode = (mode or "").strip().lower()
    if mode == "flow":
        return _find_by_class(workflow, "ModelSamplingSD3") is not None
    if mode == "vpred":
        for node in workflow.values():
            if not isinstance(node, dict) or node.get("class_type") != "ModelSamplingDiscrete":
                continue
            if node.get("inputs", {}).get("sampling") == "v_prediction":
                return True
    return False


# ── Per-checkpoint generation settings ──
# Resolved settings = built-in pattern defaults overridden by the user's saved
# defaults (data/comfy_model_settings.json). Keys: model_sampling ("", "vpred",
# "flow"), sampler, scheduler, cfg, steps, plus per-model prompt_prefix /
# negative_prefix strings used by the chat generate_image tool.

_SETTING_KEYS = ("model_sampling", "sampler", "scheduler", "cfg", "steps",
                 "prompt_prefix", "negative_prefix")


def _model_settings_path() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_PATH), "comfy_model_settings.json")


def load_model_settings() -> dict:
    try:
        with open(_model_settings_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_model_settings(settings: dict):
    with open(_model_settings_path(), "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=1)


def builtin_defaults_for(checkpoint: str) -> dict:
    """Pattern-matched defaults for known model families."""
    n = (checkpoint or "").lower()
    _no_prefix = {"prompt_prefix": "", "negative_prefix": ""}
    if "bigasp" in n and any(v in n for v in ("25", "2.5", "2_5")):
        # Flow Matching (per the author): ModelSamplingSD3, euler/beta, CFG 4-6
        return {"model_sampling": "flow", "sampler": "euler", "scheduler": "beta", "cfg": 5, "steps": 32, **_no_prefix}
    if "vpred" in n or "v-pred" in n or "v_pred" in n:
        return {"model_sampling": "vpred", "sampler": "euler", "scheduler": "normal", "cfg": 4, "steps": 28, **_no_prefix}
    if "pony" in n or "illustrious" in n or "noob" in n:
        # Pony-family models are trained on quality score tags
        return {"model_sampling": "", "sampler": "euler_ancestral", "scheduler": "normal", "cfg": 6, "steps": 28,
                "prompt_prefix": "score_9, score_8_up, score_7_up",
                "negative_prefix": "score_4, score_3, score_2, score_1"}
    if "lightning" in n or "turbo" in n or "lcm" in n:
        return {"model_sampling": "", "sampler": "euler", "scheduler": "sgm_uniform", "cfg": 1.5, "steps": 8, **_no_prefix}
    # Standard SDXL / unknown
    return {"model_sampling": "", "sampler": "euler", "scheduler": "normal", "cfg": 7, "steps": 25, **_no_prefix}


def settings_for_checkpoint(checkpoint: str) -> dict:
    """Built-in defaults merged with the user's saved override (user wins)."""
    resolved = dict(builtin_defaults_for(checkpoint))
    override = load_model_settings().get(checkpoint)
    if isinstance(override, dict):
        for k in _SETTING_KEYS:
            if k in override and override[k] is not None:
                resolved[k] = override[k]
        resolved["user_override"] = True
    else:
        resolved["user_override"] = False
    return resolved


def merge_generation_preset(body: dict, checkpoint: str) -> dict:
    """Fill omitted sampling fields from the checkpoint's resolved preset
    (request values win). The Image Studio UI always sends explicit values;
    this protects sparse API callers from silently sampling a flow/vpred
    checkpoint with the wrong objective."""
    body = body or {}
    preset = settings_for_checkpoint(checkpoint) if checkpoint else {}
    return {
        "steps": body.get("steps") or preset.get("steps") or 25,
        "cfg": body.get("cfg") or preset.get("cfg") or 7.0,
        "sampler": str(body.get("sampler") or "").strip() or (preset.get("sampler") or ""),
        "scheduler": str(body.get("scheduler") or "").strip() or (preset.get("scheduler") or ""),
        "model_sampling": str(body.get("model_sampling") or "").strip() or (preset.get("model_sampling") or ""),
    }


# ── Saved workflow library (API-format JSONs uploaded via Image Studio) ──

def workflows_dir() -> str:
    d = os.path.join(os.path.dirname(config.SETTINGS_PATH), "comfy_workflows")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_workflow_path(name: str) -> str | None:
    base = os.path.basename(str(name or "").strip())
    if not base:
        return None
    if not base.endswith(".json"):
        base += ".json"
    path = os.path.join(workflows_dir(), base)
    if not os.path.abspath(path).startswith(os.path.abspath(workflows_dir()) + os.sep):
        return None
    return path


def list_workflows() -> list[str]:
    try:
        return sorted(f[:-5] for f in os.listdir(workflows_dir()) if f.endswith(".json"))
    except Exception:
        return []


def load_workflow(name: str) -> dict | None:
    path = _safe_workflow_path(name)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            wf = json.load(f)
        return wf if isinstance(wf, dict) and wf else None
    except Exception as e:
        print(f"[COMFYUI] workflow load error for {name}: {e}")
        return None


def save_workflow(name: str, workflow: dict) -> str:
    """Validate (must patch cleanly) and persist an API-format workflow."""
    build_workflow(workflow, prompt="validation test")  # raises ValueError if unusable
    path = _safe_workflow_path(name)
    if not path:
        raise ValueError("Invalid workflow name")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(workflow, f, indent=1)
    return os.path.basename(path)[:-5]


def delete_workflow(name: str) -> bool:
    path = _safe_workflow_path(name)
    if path and os.path.exists(path):
        os.remove(path)
        return True
    return False


def workflow_from_png(data: bytes) -> dict | None:
    """Extract the API-format workflow ComfyUI embeds in its PNGs.

    ComfyUI writes two text chunks: 'prompt' (API-format graph — what we want)
    and 'workflow' (UI-format — not executable via the API). Returns None when
    no usable chunk exists (e.g. metadata stripped by an image host).
    """
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    texts = {}
    pos = 8
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if ctype == b"tEXt":
            key, _, val = chunk.partition(b"\x00")
            try:
                texts[key.decode("latin-1")] = val.decode("latin-1")
            except Exception:
                pass
        elif ctype == b"iTXt":
            key, _, rest = chunk.partition(b"\x00")
            # iTXt: compression flag+method (2 bytes), then lang\0 translated\0 text
            if len(rest) >= 2 and rest[0] == 0:  # uncompressed only
                rest = rest[2:]
                _, _, rest = rest.partition(b"\x00")
                _, _, text = rest.partition(b"\x00")
                try:
                    texts[key.decode("latin-1")] = text.decode("utf-8")
                except Exception:
                    pass
        if ctype == b"IEND":
            break
        pos += 12 + length
    raw = texts.get("prompt")
    if not raw:
        return None
    try:
        wf = json.loads(raw)
        return wf if isinstance(wf, dict) and wf else None
    except Exception:
        return None


def describe_workflow(wf: dict) -> dict:
    """Extract the form-relevant base settings so the UI can prefill controls."""
    out = {}
    sampler_id = _find_by_class(wf, "KSampler") or _find_by_class(wf, "KSamplerAdvanced")
    if sampler_id:
        s = wf[sampler_id].get("inputs", {})
        out.update(steps=s.get("steps"), cfg=s.get("cfg"),
                   sampler=s.get("sampler_name"), scheduler=s.get("scheduler"))
    elif _find_by_class(wf, "SamplerCustomAdvanced"):
        # Flux-style graph: settings live in separate nodes
        bs_id = _find_by_class(wf, "BasicScheduler")
        if bs_id:
            out.update(steps=wf[bs_id]["inputs"].get("steps"),
                       scheduler=wf[bs_id]["inputs"].get("scheduler"))
        ks_id = _find_by_class(wf, "KSamplerSelect")
        if ks_id:
            out["sampler"] = wf[ks_id]["inputs"].get("sampler_name")
        fg_id = _find_by_class(wf, "FluxGuidance")
        if fg_id:
            out["cfg"] = wf[fg_id]["inputs"].get("guidance")
    latent_id = _find_by_class(wf, "EmptyLatentImage") or _find_by_class(wf, "EmptySD3LatentImage")
    if latent_id:
        li = wf[latent_id].get("inputs", {})
        out.update(width=li.get("width"), height=li.get("height"))
    ckpt_id = _find_by_class(wf, "CheckpointLoaderSimple")
    if ckpt_id:
        out["checkpoint"] = wf[ckpt_id].get("inputs", {}).get("ckpt_name")
    else:
        unet_id = _find_by_class(wf, "UNETLoader")
        if unet_id:
            out["checkpoint"] = wf[unet_id].get("inputs", {}).get("unet_name")
    ms_id = _find_by_class(wf, "ModelSamplingDiscrete")
    out["v_prediction_builtin"] = bool(
        ms_id and wf[ms_id].get("inputs", {}).get("sampling") == "v_prediction"
    )
    if _find_by_class(wf, "SamplerCustomAdvanced"):
        out["model_sampling_builtin"] = "flux-graph"
    elif _find_by_class(wf, "ModelSamplingSD3"):
        out["model_sampling_builtin"] = "flow"
    elif out["v_prediction_builtin"]:
        out["model_sampling_builtin"] = "vpred"
    else:
        out["model_sampling_builtin"] = ""
    out["has_lora"] = any(
        isinstance(n, dict) and "Lora" in str(n.get("class_type", "")) for n in wf.values()
    )
    return out


def _clamp_dim(v, default: int) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    v = max(256, min(2048, v))
    return (v // 8) * 8


# Ordered for UI dropdowns (served via /api/images/checkpoints); the sets
# below are what validation checks against.
SAMPLER_CHOICES = (
    "euler", "euler_ancestral", "heun", "dpm_2", "dpm_2_ancestral", "lms",
    "dpmpp_2s_ancestral", "dpmpp_sde", "dpmpp_2m", "dpmpp_2m_sde",
    "dpmpp_3m_sde", "ddim", "uni_pc", "lcm",
)
SCHEDULER_CHOICES = ("normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform", "beta")
ALLOWED_SAMPLERS = set(SAMPLER_CHOICES)
ALLOWED_SCHEDULERS = set(SCHEDULER_CHOICES)


def _is_link(value) -> bool:
    return isinstance(value, list) and len(value) >= 2


def _same_link(a, b) -> bool:
    if not (_is_link(a) and _is_link(b) and str(a[0]) == str(b[0])):
        return False
    try:
        return int(a[1]) == int(b[1])
    except (TypeError, ValueError):
        return str(a[1]) == str(b[1])


def _coerce_lora_strength(value, fallback: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = fallback
    return max(-5.0, min(5.0, n))


def _coerce_lora_chain(loras) -> list[dict]:
    if not loras:
        return []
    if isinstance(loras, (str, dict)):
        loras = [loras]
    if not isinstance(loras, list):
        return []
    out = []
    for entry in loras:
        if isinstance(entry, str):
            name = entry.strip()
            strength_model = strength_clip = 1.0
        elif isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("lora_name") or "").strip()
            strength = entry.get("strength", 1.0)
            strength_model = _coerce_lora_strength(
                entry.get("strength_model", entry.get("model_strength", strength)),
                1.0,
            )
            strength_clip = _coerce_lora_strength(
                entry.get("strength_clip", entry.get("clip_strength", strength)),
                strength_model,
            )
        else:
            continue
        if name:
            out.append({
                "name": name,
                "strength_model": strength_model,
                "strength_clip": strength_clip,
            })
    return out


# Public name for route-level validation of user-supplied lora chains.
coerce_lora_chain = _coerce_lora_chain


def _new_node_id(workflow: dict, prefix: str) -> str:
    i = 1
    while f"{prefix}_{i}" in workflow:
        i += 1
    return f"{prefix}_{i}"


def _find_link_input(workflow: dict, input_name: str, preferred_classes: tuple[str, ...]) -> list | None:
    fallback = None
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") or {}
        link = inputs.get(input_name)
        if not _is_link(link):
            continue
        if node.get("class_type") in preferred_classes:
            return list(link)
        if fallback is None:
            fallback = list(link)
    return fallback


def _rewire_link(workflow: dict, old_link: list, new_link: list, *,
                 input_names: set[str], skip_ids: set[str]):
    for node_id, node in workflow.items():
        if node_id in skip_ids or not isinstance(node, dict):
            continue
        inputs = node.get("inputs") or {}
        for key, value in list(inputs.items()):
            if key in input_names and _same_link(value, old_link):
                inputs[key] = list(new_link)


def _insert_lora_chain(workflow: dict, loras, *,
                       model_link: list | None = None,
                       clip_link: list | None = None) -> int:
    """Insert core ComfyUI LoraLoader nodes into an API graph.

    Names and strengths come only from local runtime config. If no valid chain
    or no model/clip links are present, the graph is left unchanged.
    """
    chain = _coerce_lora_chain(loras)
    if not chain:
        return 0
    model_link = list(model_link) if _is_link(model_link) else _find_link_input(
        workflow, "model", ("KSampler", "BasicScheduler", "BasicGuider", "ModelSamplingFlux")
    )
    clip_link = list(clip_link) if _is_link(clip_link) else _find_link_input(
        workflow, "clip", ("CLIPTextEncode", "CLIPTextEncodeFlux")
    )
    if not (_is_link(model_link) and _is_link(clip_link)):
        return 0

    old_model = list(model_link)
    old_clip = list(clip_link)
    current_model = list(model_link)
    current_clip = list(clip_link)
    new_ids: set[str] = set()
    for item in chain:
        node_id = _new_node_id(workflow, "profile_lora")
        new_ids.add(node_id)
        workflow[node_id] = {
            "class_type": "LoraLoader",
            "inputs": {
                "lora_name": item["name"],
                "strength_model": item["strength_model"],
                "strength_clip": item["strength_clip"],
                "model": current_model,
                "clip": current_clip,
            },
        }
        current_model = [node_id, 0]
        current_clip = [node_id, 1]

    _rewire_link(workflow, old_model, current_model, input_names={"model"}, skip_ids=new_ids)
    _rewire_link(workflow, old_clip, current_clip, input_names={"clip"}, skip_ids=new_ids)
    return len(new_ids)


def _patch_flux_graph(wf: dict, *, prompt: str, width: int, height: int,
                      steps: int, cfg, seed: int, sampler_name: str,
                      scheduler: str, batch_size: int, loras=None):
    """Patch a Flux-style graph (SamplerCustomAdvanced family — no KSampler).

    Maps the Image Studio controls onto Flux nodes: seed → RandomNoise,
    steps/scheduler → BasicScheduler, sampler → KSamplerSelect,
    CFG → FluxGuidance (Flux has no negative prompt, all text encoders get
    the positive prompt).
    """
    for node in wf.values():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        ins = node.setdefault("inputs", {})
        if ct == "RandomNoise":
            ins["noise_seed"] = seed
        elif ct == "BasicScheduler":
            ins["steps"] = steps
            if scheduler and scheduler in ALLOWED_SCHEDULERS:
                ins["scheduler"] = scheduler
        elif ct == "KSamplerSelect":
            if sampler_name and sampler_name in ALLOWED_SAMPLERS:
                ins["sampler_name"] = sampler_name
        elif ct == "FluxGuidance":
            try:
                ins["guidance"] = float(cfg)
            except (TypeError, ValueError):
                pass
        elif ct == "CLIPTextEncode":
            ins["text"] = prompt or ""
        elif ct == "CLIPTextEncodeFlux":
            ins["clip_l"] = prompt or ""
            ins["t5xxl"] = prompt or ""
        elif ct in ("EmptyLatentImage", "EmptySD3LatentImage"):
            ins["width"] = _clamp_dim(width, 1024)
            ins["height"] = _clamp_dim(height, 1024)
            ins["batch_size"] = batch_size
        elif ct == "ModelSamplingFlux":
            ins["width"] = _clamp_dim(width, 1024)
            ins["height"] = _clamp_dim(height, 1024)
    _insert_lora_chain(wf, loras)


def build_workflow(template: dict, *, prompt: str, negative_prompt: str = "",
                   width: int = 1024, height: int = 1024, steps: int = 25,
                   cfg: float = 7.0, seed: int | None = None,
                   checkpoint: str = "", batch_size: int = 1,
                   sampler_name: str = "", scheduler: str = "",
                   v_prediction: bool = False,
                   model_sampling: str = "", vae: str = "",
                   loras=None) -> tuple[dict, int]:
    """Patch a copy of an API-format workflow with the requested parameters.

    Locates nodes by class_type and follows the sampler's positive/negative
    graph links to the right CLIPTextEncode nodes. Raises ValueError when a
    required node is missing.
    """
    wf = copy.deepcopy(template)

    if seed is None:
        seed = random.randint(0, 2**32 - 1)
    try:
        steps = int(steps)
    except (TypeError, ValueError):
        steps = 25
    steps = max(1, min(60, steps))
    try:
        batch_size = max(1, min(4, int(batch_size)))
    except (TypeError, ValueError):
        batch_size = 1

    sampler_id = _find_by_class(wf, "KSampler")
    sampler_class = "KSampler"
    if not sampler_id:
        sampler_id = _find_by_class(wf, "KSamplerAdvanced")
        sampler_class = "KSamplerAdvanced"
    if not sampler_id:
        if _find_by_class(wf, "SamplerCustomAdvanced"):
            _patch_flux_graph(wf, prompt=prompt, width=width, height=height,
                              steps=steps, cfg=cfg, seed=int(seed),
                              sampler_name=sampler_name, scheduler=scheduler,
                              batch_size=batch_size, loras=loras)
            save_id = _find_by_class(wf, "SaveImage")
            if save_id:
                wf[save_id]["inputs"]["filename_prefix"] = "hyprchat"
            return wf, int(seed)
        raise ValueError("Workflow has no KSampler, KSamplerAdvanced, or SamplerCustomAdvanced node")
    sampler = wf[sampler_id]["inputs"]

    if sampler_class == "KSamplerAdvanced":
        sampler["noise_seed"] = int(seed)
    else:
        sampler["seed"] = int(seed)
    sampler["steps"] = steps
    try:
        sampler["cfg"] = float(cfg)
    except (TypeError, ValueError):
        pass
    if sampler_name and sampler_name in ALLOWED_SAMPLERS:
        sampler["sampler_name"] = sampler_name
    if scheduler and scheduler in ALLOWED_SCHEDULERS:
        sampler["scheduler"] = scheduler

    _insert_lora_chain(wf, loras, model_link=sampler.get("model"))

    # Non-epsilon checkpoints produce garbage when sampled as standard SDXL:
    # v-prediction models (NoobAI vpred, ...) need ModelSamplingDiscrete +
    # RescaleCFG; Flow Matching models (BigASP v2.5) need ModelSamplingSD3.
    # Splice the node between the sampler's existing model source and the
    # sampler, preserving any LoRA chain — and skip only when the workflow
    # already carries the requested sampling mode.
    if not model_sampling and v_prediction:
        model_sampling = "vpred"
    if model_sampling in ("vpred", "flow") and not _has_model_sampling_mode(wf, model_sampling):
        model_src = sampler.get("model")
        if isinstance(model_src, list) and model_src:
            if model_sampling == "flow":
                wf["ms_flow"] = {
                    "class_type": "ModelSamplingSD3",
                    "inputs": {"shift": 3.0, "model": model_src},
                }
                sampler["model"] = ["ms_flow", 0]
            else:
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
    latent["batch_size"] = batch_size

    if checkpoint:
        ckpt_id = _find_by_class(wf, "CheckpointLoaderSimple")
        if ckpt_id:
            wf[ckpt_id]["inputs"]["ckpt_name"] = checkpoint

    # Custom VAE: load it explicitly and rewire every decode/encode node away
    # from the checkpoint's baked VAE. Empty = keep the baked one. (Flux
    # graphs return early above and keep their own VAELoader — an SDXL VAE
    # would corrupt Flux output anyway.)
    if vae:
        wf["vae_loader"] = {"class_type": "VAELoader", "inputs": {"vae_name": vae}}
        for node in wf.values():
            if isinstance(node, dict) and node.get("class_type") in ("VAEDecode", "VAEDecodeTiled", "VAEEncode"):
                node.setdefault("inputs", {})["vae"] = ["vae_loader", 0]

    save_id = _find_by_class(wf, "SaveImage")
    if save_id:
        wf[save_id]["inputs"]["filename_prefix"] = "hyprchat"

    return wf, int(seed)


# ── Async client helpers ──

def _base() -> str:
    return (config.COMFYUI_URL or "").rstrip("/")


# In-flight HyprChat generations (prompt_ids plus short-lived pending
# submission sentinels). Guards cleanup_outputs against a cross-job race: it
# deletes ALL hyprchat-prefixed files on ComfyUI, which would destroy another
# job's output before HyprChat downloads it. In-process only — a restart clears
# it (and reaped jobs error out anyway).
_ACTIVE_JOBS: set[str] = set()


def finish_job(prompt_id: str):
    """Unregister an in-flight generation (done, errored, or cancelled)."""
    _ACTIVE_JOBS.discard(prompt_id)


async def submit(workflow: dict) -> str:
    """Queue a workflow; returns the prompt_id."""
    pending_id = f"pending-{uuid.uuid4().hex}"
    _ACTIVE_JOBS.add(pending_id)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{_base()}/prompt", json={"prompt": workflow})
            r.raise_for_status()
            data = r.json()
        pid = data.get("prompt_id")
        if not pid:
            raise RuntimeError(f"ComfyUI rejected the workflow: {json.dumps(data)[:300]}")
        _ACTIVE_JOBS.add(pid)
        return pid
    finally:
        _ACTIVE_JOBS.discard(pending_id)


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


def params_from_history(history: dict) -> dict:
    """Best-effort UI params recovered from a ComfyUI history entry — used
    when a restart wiped the in-memory job registry mid-render. The history
    "prompt" field is [priority, prompt_id, graph, extra, outputs]."""
    try:
        graph = (history or {}).get("prompt", [None, None, None])[2]
    except (TypeError, IndexError, KeyError):
        return {}
    if not isinstance(graph, dict):
        return {}
    params: dict = {}
    sampler = None
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        inputs = node.get("inputs") or {}
        if ct in ("KSampler", "KSamplerAdvanced") and sampler is None:
            sampler = node
            params["seed"] = inputs.get("noise_seed" if ct == "KSamplerAdvanced" else "seed")
            params["steps"] = inputs.get("steps")
            params["cfg"] = inputs.get("cfg")
            params["sampler"] = inputs.get("sampler_name") or ""
            params["scheduler"] = inputs.get("scheduler") or ""
        elif ct in ("EmptyLatentImage", "EmptySD3LatentImage"):
            params["width"] = inputs.get("width")
            params["height"] = inputs.get("height")
            params["count"] = inputs.get("batch_size") or 1
        elif ct == "CheckpointLoaderSimple":
            params["checkpoint"] = inputs.get("ckpt_name") or ""
    if sampler is not None:
        for key, out in (("positive", "prompt"), ("negative", "negative_prompt")):
            link = (sampler.get("inputs") or {}).get(key)
            if isinstance(link, list) and link:
                enc = graph.get(str(link[0])) or {}
                if enc.get("class_type") == "CLIPTextEncode":
                    params[out] = str((enc.get("inputs") or {}).get("text") or "")
    return {k: v for k, v in params.items() if v is not None}


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


async def hyprchat_free() -> dict:
    """Unload resident ComfyUI models now.

    Prefer HyprChat's custom ComfyUI node because it unloads CPU RAM and clears
    caches immediately. Fall back to ComfyUI's built-in /free route when the
    node is not installed. Even when the custom node exists, also call the
    built-in route; newer ComfyUI builds can release additional VRAM there that
    model_management.unload_all_models() alone may leave reserved.
    """
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{_base()}/hyprchat/free")
        if r.status_code in (404, 405):
            fallback = await client.post(
                f"{_base()}/free",
                json={"unload_models": True, "free_memory": True},
            )
            fallback.raise_for_status()
            return {
                "ok": True,
                "custom_node": False,
                "status": "requested",
                "message": "ComfyUI accepted built-in free request; install the HyprChat custom node for idle CPU RAM unload.",
            }
        data = {}
        try:
            data = r.json()
        except Exception:
            pass
        if r.status_code == 409:
            return {**data, "ok": False, "custom_node": True, "status": "busy"}
        r.raise_for_status()
        builtin_free = await client.post(
            f"{_base()}/free",
            json={"unload_models": True, "free_memory": True},
        )
        builtin_free.raise_for_status()
        if isinstance(data, dict):
            return {**data, "custom_node": True, "builtin_free": True}
        return {"ok": True, "custom_node": True, "builtin_free": True}


async def hyprchat_memory() -> dict | None:
    """Return optional HyprChat custom-node memory/queue status."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_base()}/hyprchat/memory")
            if r.status_code in (404, 405):
                return None
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else None
    except Exception as e:
        print(f"[COMFYUI] hyprchat_memory error: {e}")
        return None


async def hyprchat_restart() -> dict:
    """Ask the HyprChat ComfyUI control node to restart ComfyUI.

    This is intentionally custom-node only. ComfyUI's built-in /free endpoint can
    release VRAM, but it does not reliably return system RAM to the LXC; a
    process/service restart does.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{_base()}/hyprchat/restart")
        if r.status_code in (404, 405):
            return {
                "ok": False,
                "status": "missing_restart_route",
                "custom_node": False,
                "error": "HyprChat ComfyUI control node needs to be updated to enable restart.",
            }
        data = {}
        try:
            data = r.json()
        except Exception:
            pass
        if r.status_code == 409:
            return {**data, "ok": False, "custom_node": True, "status": "busy"}
        r.raise_for_status()
        if isinstance(data, dict):
            return {**data, "custom_node": True}
        return {"ok": True, "custom_node": True, "status": "restart_scheduled"}


async def clear_history():
    """Best-effort wipe of ComfyUI's in-RAM job history (POST /history with
    {"clear": true}). Output-dir PNG copies on the LXC have no deletion API —
    the existing daily cron there handles those."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{_base()}/history", json={"clear": True})
            return True
    except Exception as e:
        print(f"[COMFYUI] clear_history error: {e}")
        return False


async def delete_history(prompt_id: str):
    """Best-effort: remove one job's entry from ComfyUI's in-RAM history."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{_base()}/history", json={"delete": [prompt_id]})
    except Exception as e:
        print(f"[COMFYUI] delete_history error: {e}")


async def cleanup_outputs() -> dict | None:
    """Delete HyprChat-prefixed images from ComfyUI's output/temp dirs via the
    optional cleanup custom node (scripts/comfyui_hyprchat_cleanup.py).
    Returns the node's {"deleted": n, "errors": m} response, or None when the
    node isn't installed (404) or ComfyUI is unreachable."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{_base()}/hyprchat/cleanup")
            # 404/405 = node not installed (405: ComfyUI's static GET handler
            # owns unknown paths, so POST gets Method Not Allowed)
            if r.status_code in (404, 405):
                return None
            r.raise_for_status()
            return r.json()
    except Exception as e:
        print(f"[COMFYUI] cleanup_outputs error: {e}")
        return None


async def forget_job(prompt_id: str):
    """Erase a finished job's traces on ComfyUI — history entry + saved file
    copies. Called after HyprChat has downloaded the images, so HyprChat holds
    the only remaining copy. File cleanup is skipped while ANY other job is in
    flight (the cleanup node deletes all hyprchat-prefixed files, which would
    destroy a not-yet-downloaded sibling job's output)."""
    finish_job(prompt_id)
    await delete_history(prompt_id)
    if not _ACTIVE_JOBS:
        await cleanup_outputs()


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


async def list_vaes() -> list[str]:
    """Available standalone VAE names from /object_info (models/vae dir)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{_base()}/object_info/VAELoader")
            r.raise_for_status()
            info = r.json()
        opts = (info.get("VAELoader", {})
                    .get("input", {}).get("required", {}).get("vae_name", []))
        if opts and isinstance(opts[0], list):
            return [str(v) for v in opts[0]]
    except Exception as e:
        print(f"[COMFYUI] list_vaes error: {e}")
    return []


async def list_loras() -> list[str]:
    """Available LoRA names from /object_info (models/loras dir)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{_base()}/object_info/LoraLoader")
            r.raise_for_status()
            info = r.json()
        opts = (info.get("LoraLoader", {})
                    .get("input", {}).get("required", {}).get("lora_name", []))
        if opts and isinstance(opts[0], list):
            return [str(l) for l in opts[0]]
    except Exception as e:
        print(f"[COMFYUI] list_loras error: {e}")
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
