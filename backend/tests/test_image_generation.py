"""
ComfyUI image generation tests. Live tests skip unless /api/health reports a
healthy comfyui service; build_workflow tests run anywhere the backend code
is importable.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Pure-unit: workflow patching (no live dependency) ─────────────────────

def test_build_workflow_patches_by_class_type():
    import comfyui
    wf, seed = comfyui.build_workflow(
        comfyui.SDXL_T2I_TEMPLATE,
        prompt="a red fox in snow",
        negative_prompt="blurry",
        width=1216, height=832, steps=20, cfg=6.5, seed=12345,
    )
    assert seed == 12345
    sampler = next(n for n in wf.values() if n["class_type"] == "KSampler")["inputs"]
    assert sampler["seed"] == 12345 and sampler["steps"] == 20 and sampler["cfg"] == 6.5
    pos = wf[str(sampler["positive"][0])]["inputs"]["text"]
    neg = wf[str(sampler["negative"][0])]["inputs"]["text"]
    assert pos == "a red fox in snow" and neg == "blurry"
    latent = next(n for n in wf.values() if n["class_type"] == "EmptyLatentImage")["inputs"]
    assert latent["width"] == 1216 and latent["height"] == 832


def test_build_workflow_clamps_and_randomizes():
    import comfyui
    wf, seed = comfyui.build_workflow(
        comfyui.SDXL_T2I_TEMPLATE,
        prompt="x", width=99999, height=100, steps=500, batch_size=99,
    )
    latent = next(n for n in wf.values() if n["class_type"] == "EmptyLatentImage")["inputs"]
    assert latent["width"] == 2048 and latent["height"] == 256 and latent["batch_size"] == 4
    sampler = next(n for n in wf.values() if n["class_type"] == "KSampler")["inputs"]
    assert sampler["steps"] == 60
    assert 0 <= seed < 2**32
    # Template must not have been mutated
    assert comfyui.SDXL_T2I_TEMPLATE["6"]["inputs"]["text"] == ""


def test_build_workflow_rejects_broken_graph():
    import comfyui
    with pytest.raises(ValueError):
        comfyui.build_workflow({"1": {"class_type": "SaveImage", "inputs": {}}}, prompt="x")


# ── Live integration (needs a healthy ComfyUI behind the server) ──────────

@pytest.fixture(scope="module")
def comfyui_live(client):
    h = client.get("/api/health").json().get("services", {})
    if h.get("comfyui", {}).get("status") != "ok":
        pytest.skip("ComfyUI not configured or unhealthy")
    return True


def test_checkpoints_endpoint(client, comfyui_live):
    r = client.get("/api/images/checkpoints")
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d.get("checkpoints"), list)


def test_generate_job_lifecycle(long_client, comfyui_live):
    r = long_client.post("/api/images/generate", json={
        "prompt": "a small wooden cabin in a snowy forest, dusk",
        "width": 512, "height": 512, "steps": 8,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    job_id = d["job_id"]
    assert isinstance(d["seed"], int)
    deadline = time.time() + 180
    final = None
    while time.time() < deadline:
        s = long_client.get(f"/api/images/jobs/{job_id}").json()
        if s["status"] in ("done", "error"):
            final = s
            break
        time.sleep(2)
    assert final and final["status"] == "done", f"job did not finish: {final}"
    assert final["images"], "no images returned"
    img = final["images"][0]
    assert img["url"].startswith("/api/downloads/")
    png = long_client.get(img["url"])
    assert png.status_code == 200
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
    if img.get("artifact_id"):
        art = long_client.get(f"/api/artifacts/{img['artifact_id']}").json()
        assert art.get("kind") == "image"


def test_generate_validation(client, comfyui_live):
    assert client.post("/api/images/generate", json={"prompt": ""}).status_code == 400
    assert client.post("/api/images/generate", json={"prompt": "x", "checkpoint": "no-such-model.safetensors"}).status_code in (200, 400)


def test_cancel_endpoint(client, comfyui_live):
    r = client.post("/api/images/generate", json={"prompt": "cancel me", "width": 512, "height": 512, "steps": 30})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    rc = client.post(f"/api/images/jobs/{job_id}/cancel")
    assert rc.status_code == 200
