import asyncio
import pathlib
import sys


BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import hyprfit  # noqa: E402


class _FakeOllamaResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload or {}
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeOllamaClient:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.urls = []

    async def get(self, url, **_kwargs):
        self.urls.append(url)
        if self.fail:
            raise RuntimeError("connection refused")
        if url.endswith("/api/version"):
            return _FakeOllamaResponse({"version": "0.9.0"})
        if url.endswith("/api/tags"):
            return _FakeOllamaResponse({"models": []})
        return _FakeOllamaResponse({})


def _run(coro):
    return asyncio.run(coro)


def test_hardware_profile_manual_override_normalization():
    profile = hyprfit.clean_hardware_profile({
        "name": "Lab box",
        "gpu_name": "sim",
        "gpu_count": "2",
        "total_vram_gb": "48",
        "system_ram_gb": "128",
        "kv_cache_type": "bad-value",
        "backend": "cuda",
        "sched_spread": True,
        "gpu_groups": [{"name": "RTX 3090", "count": 2, "vram_gb": 24}],
    })

    assert profile["name"] == "Lab box"
    assert profile["gpu_count"] == 2
    assert profile["total_vram_gb"] == 48
    assert profile["per_gpu_vram_gb"] == 24
    assert profile["kv_cache_type"] == "q8_0"
    assert profile["backend"] == "cuda"
    assert profile["gpu_groups"][0]["total_vram_gb"] == 48


def test_quant_memory_estimates_order_by_precision():
    q4 = hyprfit.estimate_weight_gb(32, "Q4_K_M", "gguf")
    q8 = hyprfit.estimate_weight_gb(32, "Q8_0", "gguf")
    fp16 = hyprfit.estimate_weight_gb(32, "fp16", "native")

    assert q4 < q8 < fp16
    assert hyprfit.quant_bytes("AWQ") < hyprfit.quant_bytes("Q8_0")
    assert hyprfit.quant_quality("Q4_K_M") < hyprfit.quant_quality("Q8_0")


def test_moe_active_parameters_drive_speed_not_weight_memory():
    profile = hyprfit.clean_hardware_profile({
        "backend": "cuda",
        "gpu_count": 2,
        "total_vram_gb": 48,
        "per_gpu_vram_gb": 24,
        "system_ram_gb": 128,
        "sched_spread": True,
        "max_loaded_models": 1,
    })
    dense = {"id": "dense", "name": "Dense 47B", "pull_name": "dense:47b", "params_b": 47, "active_params_b": 47, "quant": "Q4_K_M", "format": "gguf", "context_tokens": 32768, "categories": ["chat"]}
    moe = {**dense, "id": "moe", "name": "MoE 8x7B", "pull_name": "moe:8x7b", "architecture": "moe", "active_params_b": 12.9, "categories": ["moe", "chat"]}

    dense_card = hyprfit.card_for_entry(dense, profile, [], "chat")
    moe_card = hyprfit.card_for_entry(moe, profile, [], "chat")

    assert moe_card["speed_tps"] > dense_card["speed_tps"]
    assert moe_card["estimated_weight_gb"] == dense_card["estimated_weight_gb"]
    assert moe_card["estimated_kv_gb"] < dense_card["estimated_kv_gb"]


def test_gguf_budget_is_more_conservative_than_native_multi_gpu():
    profile = hyprfit.clean_hardware_profile({
        "backend": "cuda",
        "gpu_count": 2,
        "total_vram_gb": 48,
        "per_gpu_vram_gb": 24,
        "system_ram_gb": 128,
        "sched_spread": True,
        "max_loaded_models": 1,
    })

    gguf_budget = hyprfit.memory_budget(profile, "gguf")
    native_budget = hyprfit.memory_budget(profile, "awq")

    assert gguf_budget["run_mode"] == "gguf_gpu_spread"
    assert native_budget["run_mode"] == "native_gpu_sharded"
    assert native_budget["solo_gb"] > gguf_budget["solo_gb"]


def test_category_ranking_prefers_matching_use_case():
    profile = hyprfit.clean_hardware_profile({"backend": "cuda", "gpu_count": 1, "total_vram_gb": 24, "system_ram_gb": 64, "max_loaded_models": 1})
    coder = {"id": "coder", "name": "Coder 14B", "pull_name": "coder:14b", "params_b": 14, "active_params_b": 14, "quant": "Q4_K_M", "format": "gguf", "context_tokens": 32768, "quality": 0.78, "categories": ["coding"]}
    chat = {"id": "chat", "name": "Chat 14B", "pull_name": "chat:14b", "params_b": 14, "active_params_b": 14, "quant": "Q4_K_M", "format": "gguf", "context_tokens": 32768, "quality": 0.78, "categories": ["chat"]}

    coder_card = hyprfit.card_for_entry(coder, profile, [], "coding")
    chat_card = hyprfit.card_for_entry(chat, profile, [], "coding")

    assert coder_card["score"] > chat_card["score"]


def test_no_gpu_and_unified_memory_fallbacks_are_rankable():
    small = {"id": "small", "name": "Small 7B", "pull_name": "small:7b", "params_b": 7, "active_params_b": 7, "quant": "Q4_K_M", "format": "gguf", "context_tokens": 8192, "categories": ["chat"]}
    cpu = hyprfit.clean_hardware_profile({"backend": "cpu", "gpu_count": 0, "total_vram_gb": 0, "system_ram_gb": 128, "max_loaded_models": 1})
    unified = hyprfit.clean_hardware_profile({"backend": "metal", "gpu_count": 1, "total_vram_gb": 48, "system_ram_gb": 64, "unified_memory": True, "max_loaded_models": 1})

    cpu_card = hyprfit.card_for_entry(small, cpu, [], "chat")
    unified_card = hyprfit.card_for_entry(small, unified, [], "chat")

    assert cpu_card["fit"] in {"great", "fits", "tight"}
    assert cpu_card["run_mode"].endswith("cpu_ram")
    assert cpu_card["speed_tps"] > 0
    assert unified_card["run_mode"].endswith("unified_memory")
    assert unified_card["score"] >= cpu_card["score"]


def test_remote_rescan_keeps_saved_gpu_profile(monkeypatch):
    def fail_detector():
        raise AssertionError("remote Ollama rescan must not probe backend hardware")

    monkeypatch.setattr(hyprfit, "detect_hardware_profile", fail_detector)
    saved = {
        "name": "Ollama host",
        "gpu_name": "2x RTX 3090",
        "gpu_count": 2,
        "total_vram_gb": 48,
        "system_ram_gb": 32,
        "backend": "cuda",
        "kv_cache_type": "q8_0",
        "max_loaded_models": 3,
        "sched_spread": True,
    }
    client = _FakeOllamaClient()

    result = _run(hyprfit.resolve_hardware_rescan(saved, "http://192.168.1.110:11434", client))

    assert result["target"] == "ollama"
    assert result["ollama_url"] == "http://192.168.1.110:11434"
    assert result["ollama_reachable"] is True
    assert result["detection_mode"] == "remote_ollama_saved_profile"
    assert result["persisted"] is False
    assert result["profile"]["total_vram_gb"] == 48
    assert result["profile"]["gpu_count"] == 2
    assert result["detected_profile"]["detected"] is False
    assert client.urls == [
        "http://192.168.1.110:11434/api/version",
        "http://192.168.1.110:11434/api/tags",
        "http://192.168.1.110:11434/api/ps",
    ]


def test_local_rescan_uses_local_detector(monkeypatch):
    detected = hyprfit.clean_hardware_profile({
        "name": "Detected host",
        "gpu_name": "RTX 4090",
        "gpu_count": 1,
        "total_vram_gb": 24,
        "per_gpu_vram_gb": 24,
        "system_ram_gb": 64,
        "backend": "cuda",
        "accelerator": "nvidia",
        "detected": True,
        "source": "nvidia-smi",
    })
    monkeypatch.setattr(hyprfit, "detect_hardware_profile", lambda: detected)

    result = _run(hyprfit.resolve_hardware_rescan({}, "http://127.0.0.1:11434", _FakeOllamaClient(fail=True)))

    assert result["target"] == "backend"
    assert result["detection_mode"] == "local_detector"
    assert result["persisted"] is True
    assert result["profile"]["gpu_name"] == "RTX 4090"
    assert result["profile"]["total_vram_gb"] == 24


def test_unreachable_remote_rescan_returns_saved_profile_warning(monkeypatch):
    monkeypatch.setattr(hyprfit, "detect_hardware_profile", lambda: (_ for _ in ()).throw(AssertionError("should not run")))
    saved = {
        "name": "Remote Ollama",
        "gpu_name": "2x RTX 3090",
        "gpu_count": 2,
        "total_vram_gb": 48,
        "system_ram_gb": 32,
        "backend": "cuda",
    }

    result = _run(hyprfit.resolve_hardware_rescan(saved, "http://192.168.1.110:11434", _FakeOllamaClient(fail=True)))

    assert result["target"] == "ollama"
    assert result["ollama_reachable"] is False
    assert result["detection_mode"] == "remote_ollama_unreachable"
    assert result["persisted"] is False
    assert result["profile"]["total_vram_gb"] == 48
    assert "using saved Ollama hardware profile" in result["message"]


def test_remote_rescan_without_saved_hardware_uses_cpu_fallback(monkeypatch):
    monkeypatch.setattr(hyprfit, "detect_hardware_profile", lambda: (_ for _ in ()).throw(AssertionError("should not run")))
    saved = {
        "name": "Unknown host",
        "gpu_name": "CPU only",
        "gpu_count": 0,
        "total_vram_gb": 0,
        "per_gpu_vram_gb": 0,
        "system_ram_gb": 16,
        "backend": "cpu",
        "accelerator": "cpu",
        "unified_memory": False,
    }

    result = _run(hyprfit.resolve_hardware_rescan(saved, "http://192.168.1.110:11434", _FakeOllamaClient()))

    assert result["target"] == "ollama"
    assert result["ollama_reachable"] is True
    assert result["detection_mode"] == "cpu_fallback"
    assert result["persisted"] is False
    assert result["profile"]["backend"] == "cpu"
    assert result["profile"]["total_vram_gb"] == 0
    assert result["detected_profile"]["detected"] is False
    assert "no saved hardware profile" in result["message"]
