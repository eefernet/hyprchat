"""
HyprFit hardware detection and model-fit ranking.

This module keeps the Model Manager recommendation logic independent from the
FastAPI app. The scoring model is adapted from the hardware-fit ideas in
llmfit/Odysseus Cookbook and keeps the runtime scope deliberately narrow:
recommend models and fill pull names, never download or serve them.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable

try:
    import config
except ImportError:  # pragma: no cover - package import path during some tests
    from . import config  # type: ignore


FitSearch = Callable[..., Any]


HYPRFIT_CATEGORIES: list[dict[str, str]] = [
    {"id": "for_you", "label": "For You", "summary": "Best fit for the saved hardware profile."},
    {"id": "popular", "label": "Popular", "summary": "High-interest local model families and live HF download leaders."},
    {"id": "newest", "label": "Newest", "summary": "Recent GGUF releases from Hugging Face discovery."},
    {"id": "coding", "label": "Coding", "summary": "Code generation, refactor, and debugging models."},
    {"id": "chat", "label": "Chat", "summary": "General assistants, writing, and daily conversation."},
    {"id": "tool_calling", "label": "Tool Calling", "summary": "Models with good tool-use or tool-patching prospects."},
    {"id": "reasoning", "label": "Reasoning", "summary": "Thinking and deliberate problem-solving models."},
    {"id": "moe", "label": "MoE", "summary": "Mixture-of-experts model families."},
    {"id": "long_context", "label": "Long Context", "summary": "Models worth considering for larger context windows."},
    {"id": "small_fast", "label": "Small/Fast", "summary": "Fast utility models that can stay warm beside larger models."},
    {"id": "vision", "label": "Vision", "summary": "Multimodal models where Ollama support is available."},
    {"id": "embeddings", "label": "Embeddings", "summary": "Local retrieval and semantic search models."},
]


CATEGORY_USE_CASE: dict[str, str] = {
    "for_you": "general",
    "popular": "general",
    "newest": "general",
    "coding": "coding",
    "chat": "chat",
    "tool_calling": "tool_calling",
    "reasoning": "reasoning",
    "moe": "reasoning",
    "long_context": "long_context",
    "small_fast": "small_fast",
    "vision": "multimodal",
    "embeddings": "embeddings",
}


USE_CASE_WEIGHTS: dict[str, dict[str, float]] = {
    "general": {"quality": 0.30, "speed": 0.20, "fit": 0.30, "context": 0.12, "popularity": 0.08},
    "coding": {"quality": 0.34, "speed": 0.16, "fit": 0.22, "context": 0.16, "popularity": 0.12},
    "reasoning": {"quality": 0.38, "speed": 0.10, "fit": 0.22, "context": 0.20, "popularity": 0.10},
    "chat": {"quality": 0.28, "speed": 0.22, "fit": 0.28, "context": 0.12, "popularity": 0.10},
    "tool_calling": {"quality": 0.28, "speed": 0.18, "fit": 0.24, "context": 0.18, "popularity": 0.12},
    "long_context": {"quality": 0.24, "speed": 0.12, "fit": 0.22, "context": 0.34, "popularity": 0.08},
    "small_fast": {"quality": 0.18, "speed": 0.36, "fit": 0.36, "context": 0.04, "popularity": 0.06},
    "multimodal": {"quality": 0.32, "speed": 0.14, "fit": 0.24, "context": 0.10, "popularity": 0.20},
    "embeddings": {"quality": 0.28, "speed": 0.28, "fit": 0.28, "context": 0.12, "popularity": 0.04},
    "tts": {"quality": 0.22, "speed": 0.38, "fit": 0.30, "context": 0.02, "popularity": 0.08},
    "stt": {"quality": 0.28, "speed": 0.30, "fit": 0.28, "context": 0.06, "popularity": 0.08},
}


QUANT_PROFILES: dict[str, dict[str, float]] = {
    "f32": {"bytes": 4.0, "quality": 1.0, "speed": 0.45},
    "fp32": {"bytes": 4.0, "quality": 1.0, "speed": 0.45},
    "f16": {"bytes": 2.0, "quality": 0.99, "speed": 0.70},
    "fp16": {"bytes": 2.0, "quality": 0.99, "speed": 0.70},
    "bf16": {"bytes": 2.0, "quality": 0.99, "speed": 0.70},
    "fp8": {"bytes": 1.05, "quality": 0.96, "speed": 0.90},
    "fp4": {"bytes": 0.58, "quality": 0.86, "speed": 1.08},
    "awq": {"bytes": 0.56, "quality": 0.89, "speed": 1.10},
    "gptq": {"bytes": 0.58, "quality": 0.88, "speed": 1.02},
    "int8": {"bytes": 1.05, "quality": 0.95, "speed": 0.92},
    "int4": {"bytes": 0.56, "quality": 0.86, "speed": 1.10},
    "q8": {"bytes": 1.05, "quality": 0.96, "speed": 0.90},
    "q6": {"bytes": 0.82, "quality": 0.93, "speed": 0.98},
    "q5": {"bytes": 0.70, "quality": 0.90, "speed": 1.04},
    "q4": {"bytes": 0.58, "quality": 0.86, "speed": 1.12},
    "q3": {"bytes": 0.46, "quality": 0.78, "speed": 1.18},
    "iq3": {"bytes": 0.44, "quality": 0.79, "speed": 1.20},
    "q2": {"bytes": 0.36, "quality": 0.68, "speed": 1.25},
    "iq2": {"bytes": 0.34, "quality": 0.70, "speed": 1.27},
    "mlx": {"bytes": 0.60, "quality": 0.87, "speed": 1.08},
}


HYPRFIT_CATALOG: list[dict[str, Any]] = [
    {
        "id": "qwen3-32b",
        "name": "Qwen 3 32B",
        "pull_name": "qwen3:32b",
        "family": "Qwen",
        "params_b": 32,
        "active_params_b": 32,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 32768,
        "release_date": "2025-04-29",
        "quality": 0.91,
        "purpose": "Daily reasoning",
        "summary": "Strong general reasoning and coding on high-VRAM Ollama hosts.",
        "categories": ["popular", "chat", "coding", "tool_calling", "reasoning", "long_context"],
        "badges": ["balanced", "thinking"],
        "gguf_sources": [{"repo": "Qwen/Qwen3-32B-GGUF", "priority": "official"}],
    },
    {
        "id": "qwen3-14b",
        "name": "Qwen 3 14B",
        "pull_name": "qwen3:14b",
        "family": "Qwen",
        "params_b": 14,
        "active_params_b": 14,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 32768,
        "release_date": "2025-04-29",
        "quality": 0.84,
        "purpose": "Efficient reasoning",
        "summary": "Smaller Qwen reasoning loadout that leaves more room for warm models.",
        "categories": ["chat", "tool_calling", "reasoning", "small_fast"],
        "badges": ["efficient"],
        "gguf_sources": [{"repo": "Qwen/Qwen3-14B-GGUF", "priority": "official"}],
    },
    {
        "id": "qwen25-coder-32b",
        "name": "Qwen 2.5 Coder 32B",
        "pull_name": "qwen2.5-coder:32b",
        "family": "Qwen Coder",
        "params_b": 32,
        "active_params_b": 32,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 32768,
        "release_date": "2024-09-19",
        "quality": 0.90,
        "purpose": "Coding",
        "summary": "Best fit when code quality matters more than keeping several large models warm.",
        "categories": ["popular", "coding", "tool_calling", "long_context"],
        "badges": ["code"],
        "gguf_sources": [{"repo": "Qwen/Qwen2.5-Coder-32B-Instruct-GGUF", "priority": "official"}],
    },
    {
        "id": "qwen25-coder-14b",
        "name": "Qwen 2.5 Coder 14B",
        "pull_name": "qwen2.5-coder:14b",
        "family": "Qwen Coder",
        "params_b": 14,
        "active_params_b": 14,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 32768,
        "release_date": "2024-09-19",
        "quality": 0.82,
        "purpose": "Fast coding",
        "summary": "Code-focused daily driver that is easier to keep warm.",
        "categories": ["coding", "tool_calling", "small_fast"],
        "badges": ["code", "fast"],
    },
    {
        "id": "deepseek-r1-32b",
        "name": "DeepSeek R1 32B",
        "pull_name": "deepseek-r1:32b",
        "family": "DeepSeek",
        "params_b": 32,
        "active_params_b": 32,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 32768,
        "release_date": "2025-01-20",
        "quality": 0.90,
        "purpose": "Reasoning",
        "summary": "Good for deliberate reasoning turns when latency is acceptable.",
        "categories": ["popular", "reasoning"],
        "badges": ["thinking"],
    },
    {
        "id": "deepseek-r1-14b",
        "name": "DeepSeek R1 14B",
        "pull_name": "deepseek-r1:14b",
        "family": "DeepSeek",
        "params_b": 14,
        "active_params_b": 14,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 32768,
        "release_date": "2025-01-20",
        "quality": 0.82,
        "purpose": "Fast reasoning",
        "summary": "Reasoning-focused model that is more practical for warm-load setups.",
        "categories": ["reasoning", "small_fast"],
        "badges": ["thinking", "fast"],
    },
    {
        "id": "mistral-small-24b",
        "name": "Mistral Small 24B",
        "pull_name": "mistral-small:24b",
        "family": "Mistral",
        "params_b": 24,
        "active_params_b": 24,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 32768,
        "release_date": "2025-02-26",
        "quality": 0.84,
        "purpose": "Fast balanced chat",
        "summary": "Balanced model size for chat, tools, and moderate context.",
        "categories": ["popular", "chat", "tool_calling"],
        "badges": ["balanced"],
    },
    {
        "id": "mistral-nemo-12b",
        "name": "Mistral Nemo 12B",
        "pull_name": "mistral-nemo:12b",
        "family": "Mistral",
        "params_b": 12,
        "active_params_b": 12,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 32768,
        "release_date": "2024-07-18",
        "quality": 0.78,
        "purpose": "Small chat",
        "summary": "Useful general assistant for fast responses and lower VRAM pressure.",
        "categories": ["chat", "tool_calling", "small_fast"],
        "badges": ["fast"],
    },
    {
        "id": "codestral-22b",
        "name": "Codestral 22B",
        "pull_name": "codestral:22b",
        "family": "Mistral",
        "params_b": 22,
        "active_params_b": 22,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 32768,
        "release_date": "2024-05-29",
        "quality": 0.82,
        "purpose": "Coding",
        "summary": "Code-specialized model that fits between 14B utility and 32B coder loadouts.",
        "categories": ["coding", "long_context"],
        "badges": ["code"],
    },
    {
        "id": "gemma3-27b",
        "name": "Gemma 3 27B",
        "pull_name": "gemma3:27b",
        "family": "Gemma",
        "params_b": 27,
        "active_params_b": 27,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 32768,
        "release_date": "2025-03-12",
        "quality": 0.83,
        "purpose": "Creative chat",
        "summary": "Useful for polished chat and writing while staying below 32B-class weight.",
        "categories": ["popular", "chat", "vision"],
        "badges": ["writing"],
    },
    {
        "id": "gemma3-12b",
        "name": "Gemma 3 12B",
        "pull_name": "gemma3:12b",
        "family": "Gemma",
        "params_b": 12,
        "active_params_b": 12,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 32768,
        "release_date": "2025-03-12",
        "quality": 0.77,
        "purpose": "Efficient chat",
        "summary": "Compact chat model for writing, summaries, and quick assistant turns.",
        "categories": ["chat", "vision", "small_fast"],
        "badges": ["fast"],
    },
    {
        "id": "llama31-8b",
        "name": "Llama 3.1 8B",
        "pull_name": "llama3.1:8b",
        "family": "Llama",
        "params_b": 8,
        "active_params_b": 8,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 32768,
        "release_date": "2024-07-23",
        "quality": 0.74,
        "purpose": "Fast utility",
        "summary": "Small, quick model that can stay warm alongside larger loadouts.",
        "categories": ["popular", "chat", "tool_calling", "small_fast"],
        "badges": ["fast"],
    },
    {
        "id": "llama33-70b",
        "name": "Llama 3.3 70B",
        "pull_name": "llama3.3:70b",
        "family": "Llama",
        "params_b": 70,
        "active_params_b": 70,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 16384,
        "release_date": "2024-12-06",
        "quality": 0.93,
        "purpose": "Large-model ceiling",
        "summary": "Possible only as a tight solo loadout on 48 GB VRAM; avoid keeping other large models warm.",
        "categories": ["popular", "chat", "tool_calling"],
        "badges": ["solo"],
    },
    {
        "id": "command-r-35b",
        "name": "Command R 35B",
        "pull_name": "command-r:35b",
        "family": "Command R",
        "params_b": 35,
        "active_params_b": 35,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 32768,
        "release_date": "2024-03-11",
        "quality": 0.82,
        "purpose": "Tool-friendly chat",
        "summary": "Good candidate for RAG and tool-heavy assistant workflows when run solo.",
        "categories": ["chat", "tool_calling", "long_context"],
        "badges": ["tools"],
    },
    {
        "id": "mixtral-8x7b",
        "name": "Mixtral 8x7B",
        "pull_name": "mixtral:8x7b",
        "family": "Mixtral",
        "params_b": 47,
        "active_params_b": 12.9,
        "quant": "Q4_K_M",
        "format": "gguf",
        "architecture": "moe",
        "experts": 8,
        "active_experts": 2,
        "context_tokens": 32768,
        "release_date": "2023-12-11",
        "quality": 0.78,
        "purpose": "MoE chat",
        "summary": "Classic MoE option for broad chat and instruction-following tests.",
        "categories": ["chat", "moe", "tool_calling"],
        "badges": ["moe"],
    },
    {
        "id": "mixtral-8x22b",
        "name": "Mixtral 8x22B",
        "pull_name": "mixtral:8x22b",
        "family": "Mixtral",
        "params_b": 141,
        "active_params_b": 39,
        "quant": "Q4_K_M",
        "format": "gguf",
        "architecture": "moe",
        "experts": 8,
        "active_experts": 2,
        "context_tokens": 16384,
        "release_date": "2024-04-17",
        "quality": 0.86,
        "purpose": "Large MoE",
        "summary": "Aspirational MoE loadout; useful mainly to show fit limits on this hardware.",
        "categories": ["moe"],
        "badges": ["moe", "solo"],
    },
    {
        "id": "llama32-vision-11b",
        "name": "Llama 3.2 Vision 11B",
        "pull_name": "llama3.2-vision:11b",
        "family": "Llama Vision",
        "params_b": 11,
        "active_params_b": 11,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 16384,
        "release_date": "2024-09-25",
        "quality": 0.74,
        "purpose": "Vision",
        "summary": "Multimodal model for image-aware chats when vision support is needed.",
        "categories": ["vision", "chat", "small_fast"],
        "badges": ["vision"],
    },
    {
        "id": "llava-13b",
        "name": "LLaVA 13B",
        "pull_name": "llava:13b",
        "family": "LLaVA",
        "params_b": 13,
        "active_params_b": 13,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 8192,
        "release_date": "2023-10-05",
        "quality": 0.68,
        "purpose": "Vision",
        "summary": "Established image-understanding option for local multimodal checks.",
        "categories": ["vision"],
        "badges": ["vision"],
    },
    {
        "id": "phi3-mini",
        "name": "Phi-3 Mini",
        "pull_name": "phi3:mini",
        "family": "Phi",
        "params_b": 3.8,
        "active_params_b": 3.8,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 8192,
        "release_date": "2024-04-23",
        "quality": 0.61,
        "purpose": "Tiny utility",
        "summary": "Very small model for quick routing, summaries, and low-latency utility work.",
        "categories": ["small_fast", "chat"],
        "badges": ["tiny"],
    },
    {
        "id": "nomic-embed-text",
        "name": "Nomic Embed Text",
        "pull_name": "nomic-embed-text:latest",
        "family": "Embedding",
        "params_b": 0.3,
        "active_params_b": 0.3,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 8192,
        "release_date": "2024-02-01",
        "quality": 0.70,
        "purpose": "Embeddings",
        "summary": "Lightweight embedding model for local retrieval and utility work.",
        "categories": ["embeddings", "small_fast"],
        "badges": ["rag"],
    },
    {
        "id": "mxbai-embed-large",
        "name": "MXBAI Embed Large",
        "pull_name": "mxbai-embed-large:latest",
        "family": "Embedding",
        "params_b": 0.3,
        "active_params_b": 0.3,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 512,
        "release_date": "2024-04-01",
        "quality": 0.72,
        "purpose": "Embeddings",
        "summary": "Popular local embedding option for semantic retrieval experiments.",
        "categories": ["embeddings", "small_fast"],
        "badges": ["rag"],
    },
    {
        "id": "bge-m3",
        "name": "BGE-M3",
        "pull_name": "bge-m3:latest",
        "family": "Embedding",
        "params_b": 0.6,
        "active_params_b": 0.6,
        "quant": "Q4_K_M",
        "format": "gguf",
        "context_tokens": 8192,
        "release_date": "2024-01-30",
        "quality": 0.75,
        "purpose": "Embeddings",
        "summary": "Multilingual embedding candidate for broader retrieval workloads.",
        "categories": ["embeddings", "long_context"],
        "badges": ["rag"],
    },
]


HYPRFIT_LIVE_SEARCHES: dict[str, list[dict[str, Any]]] = {
    "popular": [{"q": "", "sort": "downloads", "limit": 12}],
    "newest": [{"q": "", "sort": "lastModified", "limit": 12}],
    "coding": [{"q": "coder", "sort": "downloads", "limit": 8}, {"q": "codestral", "sort": "downloads", "limit": 6}],
    "chat": [{"q": "instruct", "sort": "downloads", "limit": 8}, {"q": "chat", "sort": "downloads", "limit": 6}],
    "tool_calling": [{"q": "tool", "sort": "downloads", "limit": 6}, {"q": "function calling", "sort": "downloads", "limit": 6}],
    "reasoning": [{"q": "reasoning", "sort": "downloads", "limit": 6}, {"q": "deepseek r1", "sort": "downloads", "limit": 6}],
    "moe": [{"q": "moe", "sort": "downloads", "limit": 6}, {"q": "mixtral", "sort": "downloads", "limit": 6}],
}

_HYPRFIT_HF_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(max(value, minimum), maximum)


def _num(src: dict[str, Any], key: str, fallback: float, minimum: float, maximum: float) -> float:
    try:
        value = float(src.get(key, fallback))
    except (TypeError, ValueError):
        value = fallback
    return _clamp(value, minimum, maximum)


def _int(src: dict[str, Any], key: str, fallback: int, minimum: int, maximum: int) -> int:
    return int(round(_num(src, key, fallback, minimum, maximum)))


def _system_ram_gb() -> float:
    try:
        if hasattr(os, "sysconf"):
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            if pages and page_size:
                return round((pages * page_size) / (1024**3), 1)
    except (OSError, ValueError):
        pass
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=1.5, check=False)
            return round(int((out.stdout or "0").strip()) / (1024**3), 1)
        except Exception:
            pass
    return float(config.DEFAULT_SETTINGS.get("model_hardware_profile", {}).get("system_ram_gb", 16))


def _run_probe(args: list[str], timeout: float = 2.0) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _gpu_groups(devices: list[dict[str, Any]], backend: str) -> list[dict[str, Any]]:
    groups: dict[tuple[str, float], dict[str, Any]] = {}
    for dev in devices:
        name = str(dev.get("name") or "GPU").strip()[:100]
        vram = round(float(dev.get("vram_gb") or 0), 1)
        key = (name, vram)
        groups.setdefault(key, {"name": name, "count": 0, "vram_gb": vram, "total_vram_gb": 0.0, "backend": backend})
        groups[key]["count"] += 1
        groups[key]["total_vram_gb"] = round(float(groups[key]["total_vram_gb"]) + vram, 1)
    return list(groups.values())


def _detect_nvidia() -> dict[str, Any] | None:
    if not shutil.which("nvidia-smi"):
        return None
    out = _run_probe(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    devices: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            vram_gb = round(float(parts[-1]) / 1024, 1)
        except ValueError:
            continue
        devices.append({"name": ",".join(parts[:-1]).strip() or "NVIDIA GPU", "vram_gb": vram_gb})
    if not devices:
        return None
    total = round(sum(float(d["vram_gb"]) for d in devices), 1)
    groups = _gpu_groups(devices, "cuda")
    return {
        "detected": True,
        "source": "nvidia-smi",
        "backend": "cuda",
        "accelerator": "nvidia",
        "gpu_name": " + ".join(f'{g["count"]}x {g["name"]}' for g in groups),
        "gpu_count": len(devices),
        "total_vram_gb": total,
        "per_gpu_vram_gb": max(float(d["vram_gb"]) for d in devices),
        "gpu_groups": groups,
    }


def _detect_rocm() -> dict[str, Any] | None:
    if not shutil.which("rocm-smi"):
        return None
    json_out = _run_probe(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"])
    devices: list[dict[str, Any]] = []
    if json_out:
        try:
            data = json.loads(json_out)
            for value in data.values():
                if not isinstance(value, dict):
                    continue
                name = str(value.get("Card series") or value.get("Card model") or value.get("Product Name") or "AMD GPU")
                raw_vram = value.get("VRAM Total Memory (B)") or value.get("VRAM Total Memory")
                vram_gb = 0.0
                if raw_vram:
                    match = re.search(r"([\d.]+)", str(raw_vram))
                    if match:
                        number = float(match.group(1))
                        vram_gb = number / (1024**3) if number > 100000 else number / 1024 if number > 256 else number
                if vram_gb > 0:
                    devices.append({"name": name, "vram_gb": round(vram_gb, 1)})
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if not devices:
        return None
    total = round(sum(float(d["vram_gb"]) for d in devices), 1)
    groups = _gpu_groups(devices, "rocm")
    return {
        "detected": True,
        "source": "rocm-smi",
        "backend": "rocm",
        "accelerator": "amd",
        "gpu_name": " + ".join(f'{g["count"]}x {g["name"]}' for g in groups),
        "gpu_count": len(devices),
        "total_vram_gb": total,
        "per_gpu_vram_gb": max(float(d["vram_gb"]) for d in devices),
        "gpu_groups": groups,
    }


def _detect_apple(system_ram_gb: float) -> dict[str, Any] | None:
    if platform.system() != "Darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
        return None
    brand = _run_probe(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=1.0) or "Apple Silicon"
    usable = round(system_ram_gb * 0.70, 1)
    return {
        "detected": True,
        "source": "sysctl",
        "backend": "metal",
        "accelerator": "apple",
        "gpu_name": brand.strip()[:100],
        "gpu_count": 1,
        "total_vram_gb": usable,
        "per_gpu_vram_gb": usable,
        "unified_memory": True,
        "gpu_groups": [{"name": brand.strip()[:100], "count": 1, "vram_gb": usable, "total_vram_gb": usable, "backend": "metal"}],
    }


def detect_hardware_profile() -> dict[str, Any]:
    """Best-effort local hardware detector with no hard optional dependencies."""
    system_ram_gb = _system_ram_gb()
    base = {
        "name": "Detected host",
        "os": platform.system().lower() or "unknown",
        "arch": platform.machine() or "unknown",
        "system_ram_gb": system_ram_gb,
        "detected": False,
        "source": "cpu-fallback",
    }
    detected = _detect_nvidia() or _detect_rocm() or _detect_apple(system_ram_gb)
    if detected:
        return clean_hardware_profile({**base, **detected})
    return clean_hardware_profile({
        **base,
        "backend": "cpu",
        "accelerator": "cpu",
        "gpu_name": "CPU only",
        "gpu_count": 0,
        "total_vram_gb": 0,
        "per_gpu_vram_gb": 0,
        "unified_memory": False,
    })


def _clean_gpu_groups(values: Any, backend: str) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for raw in values or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("gpu_name") or "GPU").strip()[:100]
        count = int(_clamp(float(raw.get("count") or 1), 1, 64))
        vram = round(_clamp(float(raw.get("vram_gb") or raw.get("per_gpu_vram_gb") or 0), 0, 512), 1)
        total = round(_clamp(float(raw.get("total_vram_gb") or vram * count), 0, 4096), 1)
        if total <= 0 and vram <= 0:
            continue
        groups.append({"name": name, "count": count, "vram_gb": vram or round(total / count, 1), "total_vram_gb": total or round(vram * count, 1), "backend": str(raw.get("backend") or backend)})
    return groups


def clean_hardware_profile(raw: Any | None = None) -> dict[str, Any]:
    prior = dict(config.DEFAULT_SETTINGS.get("model_hardware_profile", {}))
    src = raw if isinstance(raw, dict) else {}
    merged = {**prior, **src}
    backend = str(merged.get("backend") or ("cuda" if merged.get("gpu_count", 0) else "cpu")).strip().lower()[:32]
    if backend not in {"cuda", "rocm", "metal", "directml", "cpu", "unknown"}:
        backend = "unknown"
    kv = str(merged.get("kv_cache_type") or prior.get("kv_cache_type") or "q8_0").strip().lower()
    if kv not in {"f16", "q8_0", "q4_0"}:
        kv = "q8_0"
    gpu_count = _int(merged, "gpu_count", int(prior.get("gpu_count", 1)), 0, 64)
    total_vram = round(_num(merged, "total_vram_gb", float(prior.get("total_vram_gb", 0)), 0, 4096), 1)
    per_gpu = round(_num(merged, "per_gpu_vram_gb", total_vram / gpu_count if gpu_count else 0, 0, 512), 1)
    groups = _clean_gpu_groups(merged.get("gpu_groups"), backend)
    if groups:
        gpu_count = sum(int(g["count"]) for g in groups)
        total_vram = round(sum(float(g["total_vram_gb"]) for g in groups), 1)
        per_gpu = max(float(g["vram_gb"]) for g in groups)
    return {
        "name": str(merged.get("name") or prior.get("name") or "Ollama host").strip()[:80],
        "gpu_name": str(merged.get("gpu_name") or prior.get("gpu_name") or "GPU").strip()[:120],
        "gpu_count": gpu_count,
        "total_vram_gb": total_vram,
        "per_gpu_vram_gb": per_gpu,
        "system_ram_gb": round(_num(merged, "system_ram_gb", float(prior.get("system_ram_gb", 16)), 1, 4096), 1),
        "kv_cache_type": kv,
        "num_parallel": _int(merged, "num_parallel", int(prior.get("num_parallel", 1)), 1, 64),
        "max_loaded_models": _int(merged, "max_loaded_models", int(prior.get("max_loaded_models", 1)), 1, 64),
        "sched_spread": bool(merged.get("sched_spread", prior.get("sched_spread", False))),
        "backend": backend,
        "accelerator": str(merged.get("accelerator") or ("gpu" if gpu_count else "cpu")).strip().lower()[:32],
        "unified_memory": bool(merged.get("unified_memory", backend == "metal")),
        "gpu_groups": groups,
        "os": str(merged.get("os") or platform.system().lower() or "unknown").strip().lower()[:32],
        "arch": str(merged.get("arch") or platform.machine() or "unknown").strip()[:32],
        "detected": bool(merged.get("detected", False)),
        "source": str(merged.get("source") or ("manual" if raw else "default")).strip()[:80],
    }


def detected_profile_is_meaningful(profile: dict[str, Any]) -> bool:
    clean = clean_hardware_profile(profile)
    return bool(clean.get("detected")) and (
        float(clean.get("total_vram_gb") or 0) > 0
        or bool(clean.get("unified_memory"))
        or clean.get("backend") in {"cuda", "rocm", "metal", "directml"}
    )


def saved_profile_has_hardware(profile: dict[str, Any]) -> bool:
    clean = clean_hardware_profile(profile)
    gpu_count = int(clean.get("gpu_count") or 0)
    total_vram = float(clean.get("total_vram_gb") or 0)
    backend = str(clean.get("backend") or "").lower()
    return bool(
        (gpu_count > 0 and total_vram > 0)
        or bool(clean.get("unified_memory"))
        or (backend in {"cuda", "rocm", "metal", "directml"} and total_vram > 0)
    )


def ollama_origin(ollama_url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(ollama_url or "").strip())
        if not parsed.scheme or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError:
            return ""
        return f"{parsed.scheme.lower()}://{host}{port}"
    except Exception:
        return ""


def ollama_url_is_local(ollama_url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(str(ollama_url or "").strip())
    except Exception:
        return True
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return True
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}:
        return True
    if host.startswith("127."):
        return True
    return False


def _remote_detected_profile(profile: dict[str, Any], *, reachable: bool) -> dict[str, Any]:
    return {
        **clean_hardware_profile(profile),
        "detected": False,
        "source": "remote-ollama-http" if reachable else "remote-ollama-unreachable",
    }


async def probe_remote_ollama(http_client: Any, ollama_url: str) -> tuple[bool, str]:
    origin = ollama_origin(ollama_url)
    if not origin:
        return False, "Invalid Ollama URL"
    first_error = ""
    reachable = False
    for endpoint in ("/api/version", "/api/tags"):
        try:
            response = await http_client.get(f"{origin}{endpoint}", timeout=3.0)
            response.raise_for_status()
            reachable = True
        except Exception as exc:
            if not first_error:
                first_error = str(exc)
    if reachable:
        try:
            response = await http_client.get(f"{origin}/api/ps", timeout=3.0)
            response.raise_for_status()
        except Exception:
            pass
    return reachable, "" if reachable else first_error or "Ollama did not respond"


async def resolve_hardware_rescan(settings_profile: Any, ollama_url: str, http_client: Any) -> dict[str, Any]:
    saved = clean_hardware_profile(settings_profile)
    sanitized_origin = ollama_origin(ollama_url)
    if ollama_url_is_local(ollama_url):
        detected = detect_hardware_profile()
        persisted = detected_profile_is_meaningful(detected)
        profile = detected if persisted else saved
        return {
            "profile": profile,
            "detected_profile": detected,
            "persisted": persisted,
            "target": "backend",
            "ollama_url": sanitized_origin,
            "ollama_reachable": False,
            "detection_mode": "local_detector" if persisted else "cpu_fallback",
            "message": "Detected local accelerator hardware profile saved." if persisted else "No local accelerator was detected; keeping the saved hardware profile.",
        }

    reachable, error = await probe_remote_ollama(http_client, ollama_url)
    has_saved_hardware = saved_profile_has_hardware(saved)
    detected = _remote_detected_profile(saved, reachable=reachable)
    if reachable and has_saved_hardware:
        mode = "remote_ollama_saved_profile"
        message = "Ollama is remote and reachable; using saved Ollama hardware profile."
    elif reachable:
        mode = "cpu_fallback"
        message = "Ollama is remote and reachable, but no saved hardware profile is available; using CPU fallback profile."
    elif has_saved_hardware:
        mode = "remote_ollama_unreachable"
        detail = f" ({error})" if error else ""
        message = f"Ollama is remote but unreachable{detail}; using saved Ollama hardware profile."
    else:
        mode = "remote_ollama_unreachable"
        detail = f" ({error})" if error else ""
        message = f"Ollama is remote and unreachable{detail}, and no saved hardware profile is available."
    return {
        "profile": saved,
        "detected_profile": detected,
        "persisted": False,
        "target": "ollama",
        "ollama_url": sanitized_origin,
        "ollama_reachable": reachable,
        "detection_mode": mode,
        "message": message,
    }


def quant_family(quant: str) -> str:
    q = (quant or "").lower().replace("-", "_")
    if q.startswith(("fp32", "f32")):
        return "f32"
    if q.startswith(("fp16", "f16", "bf16")):
        return q.split("_", 1)[0]
    if q.startswith(("fp8", "fp4", "awq", "gptq", "int8", "int4", "mlx")):
        return re.match(r"[a-z]+\d*", q).group(0) if re.match(r"[a-z]+\d*", q) else "q4"
    if q.startswith("iq3"):
        return "iq3"
    if q.startswith("iq2"):
        return "iq2"
    match = re.match(r"q[0-9]", q)
    return match.group(0) if match else "q4"


def quant_bytes(quant: str) -> float:
    return QUANT_PROFILES.get(quant_family(quant), QUANT_PROFILES["q4"])["bytes"]


def quant_quality(quant: str) -> float:
    return QUANT_PROFILES.get(quant_family(quant), QUANT_PROFILES["q4"])["quality"]


def quant_speed_factor(quant: str) -> float:
    return QUANT_PROFILES.get(quant_family(quant), QUANT_PROFILES["q4"])["speed"]


def model_key(name: str) -> str:
    s = (name or "").lower().replace("hf.co/", "")
    return re.sub(r"[:/_\-.]+", "", s)


def installed_match(pull_name: str, installed: list[str]) -> str:
    wanted = model_key((pull_name or "").split(":")[0])
    for name in installed:
        key = model_key(name)
        if wanted and wanted in key:
            return name
    return ""


def clean_categories(values: Any) -> list[str]:
    valid = {c["id"] for c in HYPRFIT_CATEGORIES}
    cats: list[str] = []
    for value in values or []:
        cat = str(value or "").strip()
        if cat in valid and cat != "for_you" and cat not in cats:
            cats.append(cat)
    return cats


def infer_params_b(text: str, pipeline_tag: str = "") -> tuple[float, bool, float | None]:
    lower = text.lower()
    moe = re.search(r"(?<![a-z0-9])(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*b\b", lower)
    if moe:
        total = round(float(moe.group(1)) * float(moe.group(2)), 1)
        active = round(min(total, float(moe.group(2)) * 2.0), 1)
        return total, False, active
    matches = re.findall(r"(?<![a-z0-9])(\d+(?:\.\d+)?)\s*b\b", lower)
    if matches:
        value = float(matches[-1])
        return value, False, value
    if "embed" in lower or pipeline_tag == "feature-extraction":
        return 0.5, True, 0.5
    return 7.0, True, 7.0


def infer_quant(text: str) -> str:
    upper = text.upper().replace("-", "_")
    for pattern in (
        r"FP8", r"FP4", r"AWQ", r"GPTQ", r"INT8", r"INT4", r"MLX",
        r"IQ4_[A-Z0-9_]+", r"IQ3_[A-Z0-9_]+", r"IQ2_[A-Z0-9_]+",
        r"Q8_0", r"Q6_K(?:_[A-Z])?", r"Q5_K(?:_[A-Z])?", r"Q4_K(?:_[A-Z])?",
        r"Q4_0", r"Q3_K(?:_[A-Z])?", r"Q2_K",
    ):
        match = re.search(pattern, upper)
        if match:
            return match.group(0)
    return "Q4_K_M"


def infer_format(text: str, quant: str) -> str:
    lower = text.lower()
    qfam = quant_family(quant)
    if "mlx" in lower or qfam == "mlx":
        return "mlx"
    if "awq" in lower or qfam == "awq":
        return "awq"
    if "gptq" in lower or qfam == "gptq":
        return "gptq"
    if "fp8" in lower or "fp4" in lower:
        return "native"
    return "gguf"


def infer_context(text: str, categories: list[str]) -> int:
    lower = text.lower()
    if re.search(r"1m|1000k|1024k", lower):
        return 1_000_000
    for value in (512, 256, 200, 128, 64, 32, 16, 8):
        if re.search(rf"(?<!\d){value}\s*k\b", lower):
            return value * 1024
    if "long_context" in categories:
        return 65_536
    if "vision" in categories:
        return 16_384
    if "embeddings" in categories:
        return 8_192
    return 32_768


def infer_categories(text: str, pipeline_tag: str, params_b: float) -> list[str]:
    lower = text.lower()
    cats: list[str] = []

    def add(cat: str) -> None:
        if cat not in cats:
            cats.append(cat)

    if pipeline_tag == "feature-extraction" or re.search(r"embed|bge|e5-|gte-|nomic", lower):
        add("embeddings")
    if pipeline_tag in {"image-text-to-text", "image-to-text"} or re.search(r"llava|vision|\bvl\b|multimodal|mmproj", lower):
        add("vision")
    if re.search(r"coder|coding|code|codestral|starcoder|devstral|deepseek-coder", lower):
        add("coding")
    if re.search(r"tool|function|command-r|hermes|firefunction", lower):
        add("tool_calling")
    if re.search(r"reason|thinking|deepseek-r1|r1-|qwq|reflection|qwen3", lower):
        add("reasoning")
    if re.search(r"mixtral|moe|dbrx|jamba|arctic|8x\d+", lower):
        add("moe")
    if re.search(r"128k|200k|256k|512k|1m|long-context|long_context", lower):
        add("long_context")
    if 0 < params_b <= 14:
        add("small_fast")
    if not any(c in cats for c in ("embeddings", "vision")):
        add("chat")
    return cats


def pretty_name(repo_id: str) -> str:
    base = repo_id.split("/")[-1]
    base = re.sub(r"[-_]+", " ", base)
    base = re.sub(r"\bgguf\b", "", base, flags=re.I)
    return re.sub(r"\s+", " ", base).strip()[:80] or repo_id


def live_entry(model: dict[str, Any], category: str) -> dict[str, Any] | None:
    repo_id = str(model.get("id") or "").strip()
    if not repo_id:
        return None
    tags = [str(t) for t in model.get("tags") or []]
    pipeline_tag = str(model.get("pipeline_tag") or "")
    text = " ".join([repo_id, pipeline_tag, *tags])
    params_b, estimated_params, active_params_b = infer_params_b(text, pipeline_tag)
    inferred = infer_categories(text, pipeline_tag, params_b)
    categories = clean_categories([category, *inferred])
    quant = infer_quant(text)
    fmt = infer_format(text, quant)
    ctx = infer_context(text, categories)
    badges = ["HF"]
    if estimated_params:
        badges.append("estimated size")
    if category == "newest":
        badges.append("recent")
    gguf_sources = [{"repo": repo_id, "priority": "live"}] if fmt == "gguf" else []
    return {
        "id": f"hf-{model_key(repo_id)[:60]}",
        "name": pretty_name(repo_id),
        "pull_name": f"hf.co/{repo_id}",
        "family": repo_id.split("/")[0],
        "params_b": params_b,
        "active_params_b": active_params_b or params_b,
        "params_estimated": estimated_params,
        "quant": quant,
        "format": fmt,
        "context_tokens": ctx,
        "purpose": next((c["label"] for c in HYPRFIT_CATEGORIES if c["id"] == category), "Hugging Face"),
        "summary": "Live GGUF discovery from Hugging Face; inspect the repo if it contains multiple quant files.",
        "categories": categories,
        "badges": badges,
        "source": "Hugging Face",
        "downloads": int(model.get("downloads") or 0),
        "likes": int(model.get("likes") or 0),
        "lastModified": model.get("lastModified") or "",
        "createdAt": model.get("createdAt") or "",
        "release_date": (model.get("lastModified") or model.get("createdAt") or "")[:10],
        "pipeline_tag": pipeline_tag,
        "gguf_sources": gguf_sources,
    }


async def hf_search(category: str, spec: dict[str, Any], refresh: bool, http_client: Any, hf_search_func: FitSearch) -> tuple[list[dict[str, Any]], str]:
    q = str(spec.get("q") or "")
    sort = str(spec.get("sort") or "downloads")
    limit = int(spec.get("limit") or 8)
    cache_key = json.dumps({"category": category, "q": q, "sort": sort, "limit": limit}, sort_keys=True)
    cached = _HYPRFIT_HF_CACHE.get(cache_key)
    if cached and not refresh and time.time() - cached[0] < 900:
        return cached[1], ""
    try:
        data = await asyncio.wait_for(
            hf_search_func(http_client, q=q, limit=limit, gguf_only=True, sort=sort, direction=-1),
            timeout=5,
        )
        entries = [entry for item in data if (entry := live_entry(item, category))]
        _HYPRFIT_HF_CACHE[cache_key] = (time.time(), entries)
        return entries, ""
    except Exception as exc:
        return [], str(exc)


async def live_catalog(refresh: bool, http_client: Any, hf_search_func: FitSearch) -> tuple[list[dict[str, Any]], list[str]]:
    tasks = [
        hf_search(category, spec, refresh, http_client, hf_search_func)
        for category, specs in HYPRFIT_LIVE_SEARCHES.items()
        for spec in specs
    ]
    if not tasks:
        return [], []
    results = await asyncio.gather(*tasks, return_exceptions=True)
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_error: set[str] = set()
    for result in results:
        if isinstance(result, Exception):
            err = str(result)
            if err and err not in seen_error:
                errors.append(err)
                seen_error.add(err)
            continue
        batch, err = result
        entries.extend(batch)
        if err and err not in seen_error:
            errors.append(err)
            seen_error.add(err)
    return entries, errors[:3]


def merge_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for entry in entries:
        pull_name = str(entry.get("pull_name") or entry.get("id") or "")
        key = model_key(pull_name)
        if not key:
            continue
        if key not in merged:
            merged[key] = {
                **entry,
                "categories": clean_categories(entry.get("categories")),
                "badges": list(dict.fromkeys(entry.get("badges") or [])),
                "source": entry.get("source") or "Curated",
                "format": entry.get("format") or "gguf",
                "gguf_sources": list(entry.get("gguf_sources") or []),
            }
            continue
        prior = merged[key]
        prior["categories"] = clean_categories([*prior.get("categories", []), *entry.get("categories", [])])
        prior["badges"] = list(dict.fromkeys([*prior.get("badges", []), *entry.get("badges", [])]))
        prior["gguf_sources"] = list({json.dumps(src, sort_keys=True): src for src in [*prior.get("gguf_sources", []), *entry.get("gguf_sources", [])]}.values())
        for field in ("downloads", "likes"):
            prior[field] = max(int(prior.get(field) or 0), int(entry.get(field) or 0))
        for field in ("lastModified", "createdAt", "release_date"):
            if str(entry.get(field) or "") > str(prior.get(field) or ""):
                prior[field] = entry.get(field)
        if prior.get("source") != entry.get("source") and entry.get("source"):
            prior["source"] = "Curated + HF" if prior.get("source") == "Curated" else prior.get("source")
    return list(merged.values())


def estimate_weight_gb(params_b: float, quant: str, fmt: str = "gguf") -> float:
    overhead = 1.08 if fmt in {"gguf", "mlx"} else 1.03
    return round(max(params_b, 0) * quant_bytes(quant) * overhead, 2)


def estimate_kv_gb(entry: dict[str, Any], profile: dict[str, Any]) -> float:
    params_b = float(entry.get("params_b") or 0)
    active_b = float(entry.get("active_params_b") or params_b)
    ctx_tokens = int(entry.get("context_tokens") or 32768)
    kv_model_b = float(entry.get("kv_params_b") or (min(params_b, active_b * 1.8) if entry.get("architecture") == "moe" else params_b))
    kv_factor = {"q4_0": 0.5, "q8_0": 1.0, "f16": 2.0}.get(str(profile.get("kv_cache_type") or "q8_0"), 1.0)
    gb = (kv_model_b * max(ctx_tokens, 0) / 262144.0) * kv_factor * max(int(profile.get("num_parallel") or 1), 1)
    return round(gb, 2)


def estimate_required_gb(entry: dict[str, Any], profile: dict[str, Any]) -> dict[str, float]:
    params_b = float(entry.get("params_b") or 0)
    quant = str(entry.get("quant") or "Q4_K_M")
    fmt = str(entry.get("format") or "gguf").lower()
    weight = estimate_weight_gb(params_b, quant, fmt)
    kv = estimate_kv_gb(entry, profile)
    overhead = max(0.6, weight * (0.06 if fmt in {"awq", "gptq", "native"} else 0.08))
    total = round(weight + kv + overhead, 2)
    return {"total": total, "weights": weight, "kv": kv, "overhead": round(overhead, 2)}


def memory_budget(profile: dict[str, Any], fmt: str) -> dict[str, Any]:
    total_vram = float(profile.get("total_vram_gb") or 0)
    per_gpu = float(profile.get("per_gpu_vram_gb") or (total_vram / max(int(profile.get("gpu_count") or 1), 1) if total_vram else 0))
    gpu_count = int(profile.get("gpu_count") or 0)
    max_loaded = max(int(profile.get("max_loaded_models") or 1), 1)
    backend = str(profile.get("backend") or "unknown")
    unified = bool(profile.get("unified_memory"))
    fmt = (fmt or "gguf").lower()
    if unified:
        solo = round(float(profile.get("system_ram_gb") or 0) * 0.68, 1)
        return {"solo_gb": solo, "warm_gb": round(solo / max_loaded, 1), "run_mode": "unified_memory", "budget_note": "Unified-memory budget"}
    if total_vram > 0:
        if fmt == "gguf":
            if bool(profile.get("sched_spread")) and gpu_count > 1:
                solo = total_vram * 0.78
                mode = "gguf_gpu_spread"
            else:
                solo = per_gpu * 0.88
                mode = "single_gpu_gguf"
        elif backend in {"cuda", "rocm"} and gpu_count > 1:
            solo = total_vram * 0.90
            mode = "native_gpu_sharded"
        else:
            solo = max(per_gpu, total_vram) * 0.88
            mode = "gpu"
        return {"solo_gb": round(solo, 1), "warm_gb": round(solo / max_loaded, 1), "run_mode": mode, "budget_note": "Accelerator memory budget"}
    ram_budget = round(float(profile.get("system_ram_gb") or 0) * 0.55, 1)
    return {"solo_gb": ram_budget, "warm_gb": round(ram_budget / max_loaded, 1), "run_mode": "cpu_ram", "budget_note": "CPU/system RAM fallback"}


def fit_for_required(required_gb: float, budget: dict[str, Any], profile: dict[str, Any]) -> tuple[str, str, str, float, str]:
    solo = float(budget.get("solo_gb") or 0)
    warm = float(budget.get("warm_gb") or 0)
    mode = str(budget.get("run_mode") or "unknown")
    if solo <= 0:
        return "unknown", "Unknown", "Add hardware details for fit guidance.", 0.25, mode
    if required_gb <= warm:
        return "great", "Warm-load friendly", "Fits inside the per-model warm-load budget.", 1.0, "warm_" + mode
    if required_gb <= solo:
        return "fits", "Fits solo", "Fits the solo runtime budget; reduce warm model count for long-context use.", 0.78, mode
    if required_gb <= solo * 1.18 and float(profile.get("system_ram_gb") or 0) >= 32:
        return "tight", "Tight", "May spill to system RAM or require a shorter context window.", 0.48, mode + "_spill"
    return "too_large", "Too large", "Estimated load exceeds the saved hardware profile.", 0.08, "unsupported"


def estimate_speed_tps(entry: dict[str, Any], profile: dict[str, Any], fit: str) -> float:
    if fit == "too_large":
        return 0.0
    params_b = max(float(entry.get("active_params_b") or entry.get("params_b") or 1), 0.25)
    backend = str(profile.get("backend") or "unknown")
    if backend in {"cuda", "rocm"} and float(profile.get("total_vram_gb") or 0) > 0:
        base = 700.0 if backend == "cuda" else 520.0
        gpu_scale = min(max(float(profile.get("total_vram_gb") or 0) / 24.0, 0.7), 2.0)
        speed = (base * math.sqrt(gpu_scale)) / (params_b ** 0.90)
    elif backend == "metal" or bool(profile.get("unified_memory")):
        speed = 260.0 / (params_b ** 0.88)
    else:
        speed = 42.0 / (params_b ** 0.90)
    speed *= quant_speed_factor(str(entry.get("quant") or "Q4_K_M"))
    if fit == "tight":
        speed *= 0.55
    return round(max(speed, 0.2), 1)


def speed_label(speed_tps: float, fit: str) -> str:
    if fit == "too_large" or speed_tps <= 0:
        return "not recommended"
    suffix = ", RAM-spill risk" if fit == "tight" else ""
    return f"~{speed_tps:g} tok/s{suffix}"


def _date_score(value: str) -> float:
    if not value:
        return 0.35
    raw = str(value)[:10]
    try:
        dt = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.35
    age_days = max((datetime.now(timezone.utc) - dt).days, 0)
    return round(max(0.15, min(1.0, 1.0 - (age_days / 1460.0))), 3)


def score_entry(entry: dict[str, Any], profile: dict[str, Any], fit_score: float, speed_tps: float, category: str) -> dict[str, float]:
    use_case = CATEGORY_USE_CASE.get(category, category or "general")
    weights = USE_CASE_WEIGHTS.get(use_case, USE_CASE_WEIGHTS["general"])
    params_b = max(float(entry.get("params_b") or 0), 0.1)
    quality_base = float(entry.get("quality") or min(0.93, 0.50 + math.log10(params_b + 1) / 3.2))
    quality = _clamp((quality_base * 0.82) + (quant_quality(str(entry.get("quant") or "")) * 0.18), 0, 1)
    if use_case in {"coding", "reasoning", "tool_calling", "multimodal", "embeddings"} and category in clean_categories(entry.get("categories")):
        quality = min(1.0, quality + 0.08)
    speed = _clamp(speed_tps / 120.0, 0, 1)
    context = _clamp(math.log2(max(int(entry.get("context_tokens") or 2048), 2048) / 2048) / 8.0, 0.05, 1.0)
    popularity = _clamp(math.log10(int(entry.get("downloads") or 0) + 1) / 6.0 + math.log10(int(entry.get("likes") or 0) + 1) / 10.0, 0, 1)
    if category == "newest":
        popularity = max(popularity, _date_score(str(entry.get("release_date") or entry.get("lastModified") or entry.get("createdAt") or "")))
    scores = {
        "quality": round(quality, 3),
        "speed": round(speed, 3),
        "fit": round(_clamp(fit_score, 0, 1), 3),
        "context": round(context, 3),
        "popularity": round(popularity, 3),
    }
    total = sum(scores[k] * weights.get(k, 0) for k in weights)
    return {**scores, "total": round(_clamp(total, 0, 1), 3)}


def card_for_entry(entry: dict[str, Any], profile: dict[str, Any], installed: list[str], category: str = "for_you") -> dict[str, Any]:
    fmt = str(entry.get("format") or "gguf").lower()
    required = estimate_required_gb(entry, profile)
    budget = memory_budget(profile, fmt)
    fit, fit_label, fit_note, fit_score, run_mode = fit_for_required(required["total"], budget, profile)
    speed_tps = estimate_speed_tps(entry, profile, fit)
    scores = score_entry(entry, profile, fit_score, speed_tps, category)
    installed_name = installed_match(str(entry.get("pull_name") or ""), installed)
    rank = scores["total"] + (0.04 if installed_name else 0) + (0.03 if entry.get("source", "Curated") == "Curated" else 0)
    context = int(entry.get("context_tokens") or 32768)
    quant = str(entry.get("quant") or "Q4_K_M")
    return {
        **entry,
        "categories": clean_categories(entry.get("categories")),
        "badges": list(dict.fromkeys(entry.get("badges") or [])),
        "source": entry.get("source") or "Curated",
        "installed": bool(installed_name),
        "installed_name": installed_name,
        "fit": fit,
        "fit_level": fit,
        "fit_label": fit_label,
        "fit_note": fit_note,
        "run_mode": run_mode,
        "rank": round(rank, 3),
        "score": round(scores["total"], 3),
        "scores": scores,
        "required_gb": required["total"],
        "required": required,
        "estimated_vram_gb": required["total"],
        "estimated_weight_gb": required["weights"],
        "estimated_kv_gb": required["kv"],
        "speed_tps": speed_tps,
        "estimated_speed": speed_label(speed_tps, fit),
        "single_model_budget_gb": budget["solo_gb"],
        "warm_model_budget_gb": budget["warm_gb"],
        "quant": quant,
        "context": context,
        "context_tokens": context,
        "format": fmt,
        "release_date": entry.get("release_date") or str(entry.get("lastModified") or entry.get("createdAt") or "")[:10],
        "gguf_sources": list(entry.get("gguf_sources") or []),
    }


def sort_key(card: dict[str, Any], category: str) -> tuple:
    if category == "newest":
        return (str(card.get("release_date") or card.get("lastModified") or card.get("createdAt") or ""), float(card.get("score") or 0), int(card.get("downloads") or 0))
    if category == "popular":
        return (int(card.get("downloads") or 0), int(card.get("likes") or 0), float(card.get("score") or 0), float(card.get("rank") or 0))
    if category == "small_fast":
        return (float(card.get("score") or 0), float(card.get("speed_tps") or 0), -float(card.get("params_b") or 0))
    return (float(card.get("score") or 0), float(card.get("rank") or 0), 1 if card.get("source") == "Curated" else 0, int(card.get("downloads") or 0))


def group_cards(base_entries: list[dict[str, Any]], profile: dict[str, Any], installed: list[str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {c["id"]: [] for c in HYPRFIT_CATEGORIES}
    for category in grouped:
        if category == "for_you":
            continue
        category_cards = [
            card_for_entry(entry, profile, installed, category)
            for entry in base_entries
            if category in clean_categories(entry.get("categories"))
        ]
        grouped[category] = sorted(category_cards, key=lambda card, cat=category: sort_key(card, cat), reverse=True)[:32]
    for_you_cards = [card_for_entry(entry, profile, installed, "for_you") for entry in base_entries]
    usable = [card for card in for_you_cards if card.get("fit") != "too_large"]
    grouped["for_you"] = sorted(usable or for_you_cards, key=lambda card: (float(card.get("score") or 0), float(card.get("rank") or 0)), reverse=True)[:24]
    return grouped


def system_summary(profile: dict[str, Any]) -> dict[str, Any]:
    profile = clean_hardware_profile(profile)
    gguf_budget = memory_budget(profile, "gguf")
    native_budget = memory_budget(profile, "native")
    return {
        "name": profile.get("name"),
        "backend": profile.get("backend"),
        "accelerator": profile.get("accelerator"),
        "gpu_name": profile.get("gpu_name"),
        "gpu_count": profile.get("gpu_count"),
        "total_vram_gb": profile.get("total_vram_gb"),
        "per_gpu_vram_gb": profile.get("per_gpu_vram_gb"),
        "system_ram_gb": profile.get("system_ram_gb"),
        "unified_memory": profile.get("unified_memory"),
        "kv_cache_type": profile.get("kv_cache_type"),
        "num_parallel": profile.get("num_parallel"),
        "max_loaded_models": profile.get("max_loaded_models"),
        "gguf_budget": gguf_budget,
        "native_budget": native_budget,
        "detected": profile.get("detected"),
        "source": profile.get("source"),
    }


async def build_response(
    settings_profile: Any,
    installed_details: dict[str, Any],
    *,
    refresh: bool,
    live: bool,
    http_client: Any,
    hf_search_func: FitSearch,
) -> dict[str, Any]:
    profile = clean_hardware_profile(settings_profile)
    installed = list(installed_details.keys())
    live_entries: list[dict[str, Any]] = []
    hf_errors: list[str] = []
    if live:
        live_entries, hf_errors = await live_catalog(refresh, http_client, hf_search_func)
    entries = merge_entries([*HYPRFIT_CATALOG, *live_entries])
    grouped = group_cards(entries, profile, installed)
    all_models = sorted(
        [card_for_entry(entry, profile, installed, "for_you") for entry in entries],
        key=lambda card: (float(card.get("score") or 0), float(card.get("rank") or 0)),
        reverse=True,
    )
    category_counts = {category["id"]: len(grouped.get(category["id"], [])) for category in HYPRFIT_CATEGORIES}
    return {
        "system": system_summary(profile),
        "profile": profile,
        "categories": [{**category, "count": category_counts.get(category["id"], 0)} for category in HYPRFIT_CATEGORIES],
        "models": all_models,
        "recommendations": grouped.get("for_you", []),
        "grouped_recommendations": grouped,
        "installed": installed_details,
        "hf_error": "; ".join(hf_errors),
        "live_enabled": live,
        "live_count": len(live_entries),
        "assumptions": [
            "Estimates are advisory and vary by architecture, quantization, context length, and runtime settings.",
            "GGUF on multi-GPU CUDA/ROCm hosts uses a conservative spread budget; native/AWQ/GPTQ rows can use more total VRAM.",
            "Warm-load budget divides usable memory by max_loaded_models; solo fit assumes other large models can unload.",
            "Hugging Face discovery is best-effort and falls back to curated entries when unavailable.",
        ],
    }
