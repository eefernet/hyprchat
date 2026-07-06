"""
ComfyUI image generation tests. Live tests skip unless /api/health reports a
healthy comfyui service; build_workflow tests run anywhere the backend code
is importable.
"""
import asyncio
import json
import os
import sys
import time

import httpx
import pytest

from .optional_deps import HAS_AIOSQLITE, install_aiosqlite_stub

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if not HAS_AIOSQLITE:
    install_aiosqlite_stub()


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


def test_build_workflow_inserts_lora_chain():
    import comfyui

    wf, _ = comfyui.build_workflow(
        comfyui.SDXL_T2I_TEMPLATE,
        prompt="portrait",
        negative_prompt="blur",
        loras=[{
            "name": "dummy-private-lora.safetensors",
            "strength_model": 0.6,
            "strength_clip": 0.4,
        }],
    )

    lora_nodes = [
        (node_id, node) for node_id, node in wf.items()
        if isinstance(node, dict) and node.get("class_type") == "LoraLoader"
    ]
    assert len(lora_nodes) == 1
    lora_id, lora = lora_nodes[0]
    assert lora["inputs"]["lora_name"] == "dummy-private-lora.safetensors"
    assert lora["inputs"]["strength_model"] == 0.6
    assert lora["inputs"]["strength_clip"] == 0.4
    assert lora["inputs"]["model"] == ["4", 0]
    assert lora["inputs"]["clip"] == ["4", 1]

    sampler = next(n for n in wf.values() if n["class_type"] == "KSampler")["inputs"]
    assert sampler["model"] == [lora_id, 0]
    assert wf[str(sampler["positive"][0])]["inputs"]["clip"] == [lora_id, 1]
    assert wf[str(sampler["negative"][0])]["inputs"]["clip"] == [lora_id, 1]
    assert comfyui.SDXL_T2I_TEMPLATE["4"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"


def test_build_workflow_accepts_ksampler_advanced():
    import comfyui

    template = {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "dummy-private-checkpoint.safetensors"},
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
        "10": {
            "class_type": "KSamplerAdvanced",
            "inputs": {
                "add_noise": "enable",
                "noise_seed": 0,
                "steps": 25,
                "cfg": 8,
                "sampler_name": "euler",
                "scheduler": "normal",
                "start_at_step": 0,
                "end_at_step": 10000,
                "return_with_leftover_noise": "disable",
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "17": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["10", 0], "vae": ["49", 0]},
        },
        "19": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "ComfyUI", "images": ["17", 0]},
        },
        "49": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": "dummy-private-vae.safetensors"},
        },
    }

    wf, seed = comfyui.build_workflow(
        template,
        prompt="portrait",
        negative_prompt="spots",
        width=832,
        height=1216,
        steps=31,
        cfg=5.5,
        seed=123,
        checkpoint="dummy-private-other-checkpoint.safetensors",
        vae="dummy-private-other-vae.safetensors",
    )

    sampler = wf["10"]["inputs"]
    assert seed == 123
    assert sampler["noise_seed"] == 123
    assert "seed" not in sampler
    assert sampler["steps"] == 31
    assert sampler["cfg"] == 5.5
    assert wf["6"]["inputs"]["text"] == "portrait"
    assert wf["7"]["inputs"]["text"] == "spots"
    assert wf["5"]["inputs"]["width"] == 832
    assert wf["5"]["inputs"]["height"] == 1216
    assert wf["4"]["inputs"]["ckpt_name"] == "dummy-private-other-checkpoint.safetensors"
    assert wf["vae_loader"]["inputs"]["vae_name"] == "dummy-private-other-vae.safetensors"
    assert wf["17"]["inputs"]["vae"] == ["vae_loader", 0]
    assert comfyui.describe_workflow(wf)["steps"] == 31


def test_build_workflow_injects_required_sampling_when_existing_node_mismatches():
    import comfyui

    template = json.loads(json.dumps(comfyui.SDXL_T2I_TEMPLATE))
    template["wrong_ms"] = {
        "class_type": "ModelSamplingDiscrete",
        "inputs": {"sampling": "eps", "zsnr": False, "model": ["4", 0]},
    }
    template["3"]["inputs"]["model"] = ["wrong_ms", 0]

    wf, _ = comfyui.build_workflow(
        template,
        prompt="flow model prompt",
        model_sampling="flow",
    )

    assert wf["ms_flow"]["class_type"] == "ModelSamplingSD3"
    assert wf["ms_flow"]["inputs"]["model"] == ["wrong_ms", 0]
    assert wf["3"]["inputs"]["model"] == ["ms_flow", 0]

    template = json.loads(json.dumps(comfyui.SDXL_T2I_TEMPLATE))
    template["wrong_flow"] = {
        "class_type": "ModelSamplingSD3",
        "inputs": {"shift": 3.0, "model": ["4", 0]},
    }
    template["3"]["inputs"]["model"] = ["wrong_flow", 0]

    wf, _ = comfyui.build_workflow(
        template,
        prompt="vpred model prompt",
        model_sampling="vpred",
    )

    assert wf["vpred_ms"]["class_type"] == "ModelSamplingDiscrete"
    assert wf["vpred_ms"]["inputs"]["sampling"] == "v_prediction"
    assert wf["vpred_ms"]["inputs"]["model"] == ["wrong_flow", 0]
    assert wf["3"]["inputs"]["model"] == ["vpred_rc", 0]


def test_build_workflow_skips_sampling_injection_when_mode_already_matches():
    import comfyui

    template = json.loads(json.dumps(comfyui.SDXL_T2I_TEMPLATE))
    template["existing_flow"] = {
        "class_type": "ModelSamplingSD3",
        "inputs": {"shift": 3.0, "model": ["4", 0]},
    }
    template["3"]["inputs"]["model"] = ["existing_flow", 0]

    wf, _ = comfyui.build_workflow(
        template,
        prompt="flow model prompt",
        model_sampling="flow",
    )

    assert "ms_flow" not in wf
    assert wf["3"]["inputs"]["model"] == ["existing_flow", 0]


def test_submit_pending_guard_blocks_cleanup_during_prompt_registration(monkeypatch):
    import comfyui

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"prompt_id": "prompt-new"}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *_args, **_kwargs):
            assert any(job.startswith("pending-") for job in comfyui._ACTIVE_JOBS)
            await comfyui.forget_job("prompt-finished")
            return _Resp()

    cleanup_calls = []

    async def _delete_history(_prompt_id):
        return None

    async def _cleanup_outputs():
        cleanup_calls.append("cleanup")
        return {"deleted": 0}

    comfyui._ACTIVE_JOBS.clear()
    monkeypatch.setattr(comfyui.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(comfyui, "delete_history", _delete_history)
    monkeypatch.setattr(comfyui, "cleanup_outputs", _cleanup_outputs)

    prompt_id = asyncio.run(comfyui.submit({"1": {"class_type": "SaveImage", "inputs": {}}}))

    assert prompt_id == "prompt-new"
    assert cleanup_calls == []
    assert comfyui._ACTIVE_JOBS == {"prompt-new"}

    asyncio.run(comfyui.forget_job("prompt-new"))

    assert cleanup_calls == ["cleanup"]
    assert comfyui._ACTIVE_JOBS == set()


def test_persona_image_profiles_load_from_data_dir(tmp_path, monkeypatch):
    import config
    import persona_images

    monkeypatch.setattr(config, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    profile_path = tmp_path / "persona_image_profiles.json"
    profile_path.write_text(json.dumps({
        "profiles": {
            "sfw_photo": {
                "workflow": "dummy-private-workflow",
                "checkpoint": "dummy-private-checkpoint",
            }
        }
    }), encoding="utf-8")

    loaded = persona_images.load_persona_image_profiles()
    assert loaded["profiles"]["sfw_photo"]["workflow"] == "dummy-private-workflow"
    assert persona_images.profiles_path() == str(profile_path)


def test_persona_profile_rating_gates():
    import persona_images

    cfg = {
        "profiles": {
            "sfw_photo": {"prompt_prefix": "dummy-sfw-prefix"},
            "adult_photo": {"adult": True, "prompt_prefix": "dummy-adult-prefix"},
        }
    }

    sfw = persona_images.select_persona_image_profile(
        cfg,
        persona_id="persona-1",
        persona_rating="PG-13",
        prompt="selfie",
        user_request="send an adult-only selfie",
    )
    assert sfw["key"] == "sfw_photo"

    adult_normal = persona_images.select_persona_image_profile(
        cfg,
        persona_id="persona-2",
        persona_rating="NC-17",
        prompt="selfie",
        user_request="send a normal selfie",
    )
    assert adult_normal["key"] == "sfw_photo"

    adult_requested = persona_images.select_persona_image_profile(
        cfg,
        persona_id="persona-2",
        persona_rating="NC-17",
        prompt="selfie",
        user_request="send an adult-only selfie",
    )
    assert adult_requested["key"] == "adult_photo"


def test_persona_visual_context_intents_and_metadata():
    import persona_images

    ctx = persona_images.build_visual_context(
        persona_id="persona-1",
        persona_name="Dummy Persona",
        appearance="short silver hair, green jacket",
        scenario="quiet dummy cafe scene",
        lore="keeps a consistent dummy camera style",
        rating="PG-13",
        user_request="send another full body outfit photo at the cafe",
        tool_prompt="full body outfit photo",
        current_reply="I am standing by the counter wearing the green jacket.",
        recent_messages=[
            {"role": "user", "content": "the cafe lighting is warm"},
            {"role": "assistant", "content": "I am smiling near the window."},
        ],
        prior_images=[{
            "created_at": "2026-01-01T00:00:00",
            "metadata": {
                "prompt": "previous dummy portrait prompt",
                "seed": 123,
                "persona_image_profile": {"intent": "selfie", "framing": "selfie framing"},
            },
        }],
    )

    assert ctx["primary_intent"] == "another"
    assert "full_body" in ctx["intents"]
    assert "outfit" in ctx["intents"]
    assert ctx["prior_images"][0]["seed"] == 123
    assert "green jacket" in ctx["appearance"]
    assert "standing by the counter" in ctx["recent_scene_cues"]


def test_persona_prompt_preserves_neutral_pose_constraints():
    import persona_images

    ctx = persona_images.build_visual_context(
        appearance="short silver hair, green jacket",
        rating="PG-13",
        user_request="send a photo facing away in the bedroom",
        tool_prompt="photo facing away in the bedroom",
        current_reply="The bedroom has soft window light.",
    )
    structured = {
        "prompt": "short silver hair, green jacket, cozy bedroom portrait, looking at viewer, soft window light",
        "negative_prompt": "motion blur",
        "framing": "portrait framing",
        "continuity_notes": "",
    }

    payload = persona_images.compose_persona_image_prompt(
        raw_prompt="photo facing away in the bedroom",
        visual_context=ctx,
        structured=structured,
    )
    prompt = payload["prompt"].lower()
    assert "facing away" in prompt or "back view" in prompt or "from behind" in prompt
    assert "bedroom" in prompt
    assert "looking at viewer" not in prompt
    assert "selfie" not in prompt
    assert "phone" not in prompt
    assert "mirror" not in prompt


def test_persona_prompt_preserves_bed_pose_without_selfie_artifacts():
    import persona_images

    ctx = persona_images.build_visual_context(
        appearance="short silver hair, green jacket",
        rating="PG-13",
        user_request="send a photo lying on a bed",
        tool_prompt="photo lying on a bed",
        current_reply="The room has warm lamplight.",
    )

    payload = persona_images.compose_persona_image_prompt(
        raw_prompt="photo lying on a bed",
        visual_context=ctx,
        structured={"prompt": "short silver hair, green jacket, warm room portrait", "negative_prompt": ""},
    )
    prompt = payload["prompt"].lower()
    assert "lying on a bed" in prompt
    assert "selfie" not in prompt
    assert "phone" not in prompt


def test_persona_prompt_ignores_prior_scene_without_continuity():
    import persona_images

    ctx = persona_images.build_visual_context(
        appearance="short silver hair, green jacket",
        rating="PG-13",
        user_request="send a photo in a library",
        tool_prompt="golden-hour beach portrait with a caption",
        current_reply="Caption: a relaxed beach photo at golden hour.",
        recent_messages=[
            {"role": "assistant", "content": "The last image was a beach photo with warm sunset light."},
        ],
        prior_images=[{"metadata": {"prompt": "previous golden-hour beach photo", "seed": 101}}],
    )

    payload = persona_images.compose_persona_image_prompt(
        raw_prompt="golden-hour beach portrait with a caption",
        visual_context=ctx,
    )
    prompt = payload["prompt"].lower()
    assert "library" in prompt
    assert "beach" not in prompt
    assert "golden" not in prompt
    assert "caption" not in prompt
    assert ctx["recent_scene_cues"] == ""
    assert ctx["prior_images"] == []


def test_persona_prompt_strips_assistant_caption_from_bed_request():
    import persona_images

    ctx = persona_images.build_visual_context(
        appearance="short silver hair, green jacket",
        rating="PG-13",
        user_request="send a photo lying on a bed",
        tool_prompt="beach caption portrait",
        current_reply="Assistant caption: standing on a beach at golden hour.",
        recent_messages=[
            {"role": "assistant", "content": "Caption: standing on a beach at golden hour."},
        ],
    )
    structured = {
        "prompt": "short silver hair, green jacket, caption: standing on a beach at golden hour",
        "negative_prompt": "",
    }

    payload = persona_images.compose_persona_image_prompt(
        raw_prompt="beach caption portrait",
        visual_context=ctx,
        structured=structured,
    )
    prompt = payload["prompt"].lower()
    assert "lying on a bed" in prompt
    assert "beach" not in prompt
    assert "golden" not in prompt
    assert "caption" not in prompt
    assert "standing" not in prompt


def test_persona_prompt_allows_prior_scene_for_continuity_request():
    import persona_images

    ctx = persona_images.build_visual_context(
        appearance="short silver hair, green jacket",
        rating="PG-13",
        user_request="another one in the same scene",
        tool_prompt="another photo in the same scene",
        current_reply="She is in a quiet library aisle under green desk lamps.",
        recent_messages=[
            {"role": "assistant", "content": "The library scene has green desk lamps and tall shelves."},
        ],
        prior_images=[{"metadata": {"prompt": "quiet library aisle, green desk lamps", "seed": 202}}],
    )

    payload = persona_images.compose_persona_image_prompt(
        raw_prompt="another photo in the same scene",
        visual_context=ctx,
    )
    prompt = payload["prompt"].lower()
    assert ctx["continuity_requested"] is True
    assert ctx["prior_images"]
    assert "library" in prompt
    assert "new composition" in prompt


def test_persona_prompt_fallback_prefers_latest_request_over_stale_tool_prompt():
    import persona_images

    ctx = persona_images.build_visual_context(
        appearance="short silver hair, green jacket",
        rating="PG-13",
        user_request="send a photo in a library",
        tool_prompt="golden-hour beach portrait",
        current_reply="The last generated caption was at the beach.",
    )

    payload = persona_images.compose_persona_image_prompt(
        raw_prompt="golden-hour beach portrait",
        visual_context=ctx,
        structured='{"prompt":"Okay, I can help!"}',
    )
    prompt = payload["prompt"].lower()
    assert payload["fallback_used"] is True
    assert "library" in prompt
    assert "beach" not in prompt
    assert "golden" not in prompt


def test_persona_selfie_and_phone_framing_are_explicit():
    import persona_images

    assert "selfie" not in persona_images.detect_image_intents("portrait looking at viewer in warm light")

    selfie_ctx = persona_images.build_visual_context(
        appearance="short silver hair, green jacket",
        rating="PG-13",
        user_request="send a selfie in the cafe",
        tool_prompt="selfie in the cafe",
    )
    selfie_payload = persona_images.compose_persona_image_prompt(
        raw_prompt="selfie in the cafe",
        visual_context=selfie_ctx,
    )
    assert "selfie" in selfie_payload["prompt"].lower()
    assert "phone" not in selfie_payload["prompt"].lower()

    mirror_ctx = persona_images.build_visual_context(
        appearance="short silver hair, green jacket",
        rating="PG-13",
        user_request="send a mirror selfie in the cafe",
        tool_prompt="mirror selfie in the cafe",
    )
    mirror_payload = persona_images.compose_persona_image_prompt(
        raw_prompt="mirror selfie in the cafe",
        visual_context=mirror_ctx,
    )
    mirror_prompt = mirror_payload["prompt"].lower()
    assert "mirror selfie" in mirror_prompt
    assert "phone" in mirror_prompt


def test_enhance_prompt_template_prioritizes_request_fidelity():
    from image_prompt_enhancer import _ENHANCE_PROMPT_TEMPLATE
    template = _ENHANCE_PROMPT_TEMPLATE.lower()
    assert "pose, orientation, action, setting" in template
    assert "do not turn a specific request into a generic portrait" in template
    assert "do not add unrelated props, phones, selfie framing, mirror framing, extra people" in template


def test_enhance_prompt_parser_accepts_valid_json():
    from image_prompt_enhancer import normalize_enhancer_response

    prompt, negative = normalize_enhancer_response(json.dumps({
        "prompt": "a red fox standing in deep fresh snow, winter forest clearing, soft overcast daylight, detailed orange fur, shallow depth of field",
        "negative_prompt": "lowres, blurry, watermark, text, jpeg artifacts",
    }))

    assert prompt.startswith("a red fox standing in deep fresh snow")
    assert negative == "lowres, blurry, watermark, text, jpeg artifacts"


def test_enhance_prompt_parser_extracts_embedded_json():
    from image_prompt_enhancer import normalize_enhancer_response

    raw = """Here is the JSON:
```json
{"prompt": "a crystal lighthouse on a black volcanic coast, storm waves, blue beacon light, wide angle view, cinematic atmosphere, highly detailed", "negative_prompt": "lowres, blurry, watermark, text, jpeg artifacts"}
```"""
    prompt, negative = normalize_enhancer_response(raw)

    assert prompt.startswith("a crystal lighthouse")
    assert "Here is" not in prompt
    assert negative == "lowres, blurry, watermark, text, jpeg artifacts"


def test_enhance_prompt_parser_strips_reasoning_wrapper():
    from image_prompt_enhancer import normalize_enhancer_response

    raw = """"We need to produce a prompt for SDXL generation: "american eagle, american flags"

a majestic bald eagle soaring over a field of waving American flags, sunrise sky, golden light, crisp wings, realistic feathers, dynamic lighting, patriotic landscape, detailed focus"""
    prompt, negative = normalize_enhancer_response(raw)

    assert prompt.startswith("a majestic bald eagle")
    assert "We need" not in prompt
    assert "american eagle, american flags" not in prompt
    assert "lowres" in negative


def test_enhance_prompt_parser_splits_labeled_prose():
    from image_prompt_enhancer import normalize_enhancer_response

    raw = """Prompt: "a crystal lighthouse on a black volcanic coast, storm waves, blue beacon light, wide angle view, cinematic atmosphere, highly detailed"
Negative prompt: "lowres, blurry, watermark, text, jpeg artifacts, dull colors" """
    prompt, negative = normalize_enhancer_response(raw)

    assert prompt == "a crystal lighthouse on a black volcanic coast, storm waves, blue beacon light, wide angle view, cinematic atmosphere, highly detailed"
    assert negative == "lowres, blurry, watermark, text, jpeg artifacts, dull colors"


def test_enhance_prompt_parser_salvages_quoted_prompt_after_explanation():
    from image_prompt_enhancer import normalize_enhancer_response

    raw = """The prompt should be: "a bronze airship drifting above a neon desert city, glowing engine rings, sweeping wide angle, sunset haze, reflective metal panels, cinematic lighting, ultra detailed" """
    prompt, negative = normalize_enhancer_response(raw)

    assert prompt.startswith("a bronze airship")
    assert "The prompt should be" not in prompt
    assert "lowres" in negative


def test_enhance_prompt_parser_rejects_unusable_ramble():
    from image_prompt_enhancer import normalize_enhancer_response

    prompt, negative = normalize_enhancer_response(
        "Sure, I can help with that. The prompt should be vivid and high quality."
    )

    assert prompt == ""
    assert "lowres" in negative


def test_persona_structured_prompt_validation_and_fallback():
    import persona_images

    ctx = persona_images.build_visual_context(
        appearance="short silver hair, green jacket",
        rating="PG-13",
        user_request="send another selfie in the dummy cafe",
        tool_prompt="another selfie",
        current_reply="I am sitting near the window and smiling.",
        prior_images=[{"metadata": {"prompt": "previous dummy selfie", "seed": 456}}],
    )
    structured = {
        "prompt": "short silver hair, green jacket, seated in a dummy cafe, selfie, phone camera angle, looking at viewer, warm window light, relaxed smile",
        "negative_prompt": "motion blur",
        "framing": "selfie framing",
        "continuity_notes": "keep the same dummy character identity",
    }

    payload = persona_images.compose_persona_image_prompt(
        raw_prompt="another selfie",
        negative_prompt="low quality",
        visual_context=ctx,
        structured=structured,
    )
    assert payload["fallback_used"] is False
    assert payload["framing"] == "selfie framing"
    assert "motion blur" in payload["negative_prompt"]
    assert "low quality" in payload["negative_prompt"]

    fallback = persona_images.compose_persona_image_prompt(
        raw_prompt="another selfie",
        visual_context=ctx,
        structured='{"prompt":"Okay, I can help!"}',
    )
    assert fallback["fallback_used"] is True
    assert "short silver hair" in fallback["prompt"]
    assert "new composition" in fallback["prompt"]
    assert "unsafe adult content" in fallback["negative_prompt"]


def test_persona_profile_pose_intent_beats_default_override():
    import persona_images

    cfg = {
        "profiles": {
            "sfw_photo": {"prompt_prefix": "dummy-default-prefix"},
            "pose_photo": {"prompt_prefix": "dummy-pose-prefix", "tags": ["pose"]},
        },
        "persona_overrides": {
            "persona-1": {"default": "sfw_photo"}
        },
    }

    selected = persona_images.select_persona_image_profile(
        cfg,
        persona_id="persona-1",
        persona_rating="PG-13",
        prompt="full body standing pose in a studio",
        user_request="send a specific pose photo",
    )

    assert selected["key"] == "pose_photo"


def test_chat_image_recipe_uses_persona_profile_workflow(tmp_path, monkeypatch):
    import comfyui
    import config
    import tools

    monkeypatch.setattr(config, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "IMAGE_CHAT_CHECKPOINT", "fallback-checkpoint")
    monkeypatch.setattr(config, "IMAGE_CHAT_RESOLUTION", "640x640")
    monkeypatch.setattr(config, "IMAGE_CHAT_PROMPT_PREFIX", "fallback-prefix")
    monkeypatch.setattr(config, "IMAGE_CHAT_NEGATIVE", "fallback-negative")
    monkeypatch.setattr(config, "IMAGE_CHAT_VAE", "fallback-vae")
    (tmp_path / "persona_image_profiles.json").write_text(json.dumps({
        "profiles": {
            "sfw_photo": {
                "workflow": "dummy-private-workflow",
                "checkpoint": "dummy-private-checkpoint",
                "vae": "dummy-private-vae",
                "prompt_prefix": "dummy-profile-prefix",
                "negative_prefix": "dummy-profile-negative",
                "width": 832,
                "height": 1216,
                "steps": 31,
                "cfg": 5.5,
                "sampler": "euler",
                "scheduler": "normal",
                "loras": [{"name": "dummy-private-lora.safetensors", "strength": 0.75}],
            }
        }
    }), encoding="utf-8")
    template = {"template": True}
    monkeypatch.setattr(comfyui, "load_workflow", lambda name: template if name == "dummy-private-workflow" else None)
    monkeypatch.setattr(comfyui, "settings_for_checkpoint", lambda _ckpt: {
        "steps": 22,
        "cfg": 6.5,
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
        "model_sampling": "",
        "prompt_prefix": "dummy-preset-prefix",
        "negative_prefix": "dummy-preset-negative",
    })

    recipe = tools._resolve_chat_image_recipe(
        {"prompt": "selfie in a cafe", "negative_prompt": "motion blur"},
        persona_context={
            "persona_id": "persona-1",
            "persona_rating": "PG-13",
            "appearance": "short silver hair, green jacket",
            "scenario": "dummy cafe",
            "user_request": "send a selfie in a cafe",
            "prior_images": [{"metadata": {"prompt": "previous dummy photo", "seed": 111}}],
        },
    )

    assert recipe["template"] is template
    assert recipe["workflow_name"] == "dummy-private-workflow"
    assert recipe["checkpoint"] == "dummy-private-checkpoint"
    assert recipe["vae"] == "dummy-private-vae"
    assert recipe["width"] == 640
    assert recipe["height"] == 640
    assert recipe["steps"] == 31
    assert recipe["cfg"] == 5.5
    assert recipe["sampler_name"] == "euler"
    assert "dummy-preset-prefix" in recipe["prompt"]
    assert "dummy-profile-prefix" in recipe["prompt"]
    assert "short silver hair" in recipe["prompt"]
    assert "selfie in a cafe" in recipe["prompt"]
    assert "dummy-profile-negative" in recipe["negative_prompt"]
    assert recipe["loras"][0]["name"] == "dummy-private-lora.safetensors"
    assert recipe["profile_metadata"]["active"] is True
    assert recipe["profile_metadata"]["prompt_intent"] == "selfie"
    assert recipe["profile_metadata"]["prior_image_count"] == 1
    assert recipe["profile_metadata"]["workflow_configured"] is True
    assert recipe["profile_metadata"]["lora_count"] == 1


def test_chat_image_recipe_falls_back_when_profile_workflow_missing(tmp_path, monkeypatch):
    import comfyui
    import config
    import tools

    monkeypatch.setattr(config, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "IMAGE_CHAT_CHECKPOINT", "fallback-checkpoint")
    monkeypatch.setattr(config, "IMAGE_CHAT_RESOLUTION", "640x768")
    monkeypatch.setattr(config, "IMAGE_CHAT_PROMPT_PREFIX", "fallback-prefix")
    monkeypatch.setattr(config, "IMAGE_CHAT_NEGATIVE", "fallback-negative")
    monkeypatch.setattr(config, "IMAGE_CHAT_VAE", "fallback-vae")
    (tmp_path / "persona_image_profiles.json").write_text(json.dumps({
        "profiles": {
            "sfw_photo": {
                "workflow": "missing-dummy-workflow",
                "checkpoint": "dummy-private-checkpoint",
                "prompt_prefix": "dummy-profile-prefix",
            }
        }
    }), encoding="utf-8")
    monkeypatch.setattr(comfyui, "load_workflow", lambda _name: None)
    monkeypatch.setattr(comfyui, "settings_for_checkpoint", lambda _ckpt: {
        "steps": 12,
        "cfg": 3.5,
        "sampler": "euler",
        "scheduler": "normal",
        "model_sampling": "",
        "prompt_prefix": "",
        "negative_prefix": "",
    })

    recipe = tools._resolve_chat_image_recipe(
        {"prompt": "portrait", "width": 512},
        persona_context={
            "persona_id": "persona-1",
            "persona_rating": "PG-13",
            "user_request": "send a photo",
        },
    )

    assert recipe["template"] is None
    assert recipe["workflow_name"] == ""
    assert recipe["checkpoint"] == "fallback-checkpoint"
    assert recipe["width"] == 640
    assert recipe["height"] == 768
    assert "fallback-prefix" in recipe["prompt"]
    assert "dummy-profile-prefix" not in recipe["prompt"]
    assert recipe["profile_metadata"]["active"] is False
    assert recipe["profile_metadata"]["fallback_reason"] == "missing_workflow"


def test_chat_image_recipe_global_resolution_overrides_profile_and_tool_args(tmp_path, monkeypatch):
    import comfyui
    import config
    import tools

    monkeypatch.setattr(config, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "IMAGE_CHAT_CHECKPOINT", "fallback-checkpoint")
    monkeypatch.setattr(config, "IMAGE_CHAT_RESOLUTION", "832x1216")
    monkeypatch.setattr(config, "IMAGE_CHAT_PROMPT_PREFIX", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_NEGATIVE", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_VAE", "")
    (tmp_path / "persona_image_profiles.json").write_text(json.dumps({
        "profiles": {
            "sfw_photo": {
                "workflow": "dummy-private-workflow",
                "checkpoint": "dummy-private-checkpoint",
                "width": 1024,
                "height": 1024,
            }
        }
    }), encoding="utf-8")
    monkeypatch.setattr(comfyui, "load_workflow", lambda name: {"template": True} if name == "dummy-private-workflow" else None)
    monkeypatch.setattr(comfyui, "settings_for_checkpoint", lambda _ckpt: {})

    recipe = tools._resolve_chat_image_recipe(
        {"prompt": "portrait", "width": 512, "height": 512},
        persona_context={
            "persona_id": "persona-1",
            "persona_rating": "PG-13",
            "user_request": "send a portrait",
        },
    )

    assert recipe["width"] == 832
    assert recipe["height"] == 1216
    assert recipe["profile_metadata"]["active"] is True


def test_chat_image_recipe_uses_model_prompt_via_structured_path(tmp_path, monkeypatch):
    import comfyui
    import config
    import tools

    monkeypatch.setattr(config, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "IMAGE_CHAT_CHECKPOINT", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_RESOLUTION", "640x640")
    monkeypatch.setattr(config, "IMAGE_CHAT_PROMPT_PREFIX", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_NEGATIVE", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_VAE", "")
    monkeypatch.setattr(comfyui, "settings_for_checkpoint", lambda _ckpt: {})

    recipe = tools._resolve_chat_image_recipe(
        {"prompt": "short silver hair, green jacket, reading a novel at the library, warm lamp light, candid framing"},
        persona_context={
            "persona_id": "persona-1",
            "persona_rating": "PG-13",
            "appearance": "short silver hair, green jacket",
            "user_request": "send me a photo of you reading at the library",
        },
    )

    # The model's scene wording survives instead of being rebuilt.
    assert "warm lamp light" in recipe["prompt"]
    assert "novel" in recipe["prompt"]
    assert recipe["profile_metadata"]["prompt_fallback"] is False


def test_persona_structured_prompt_ignored_for_sfw_adult_request():
    import persona_images

    ctx = persona_images.build_visual_context(
        persona_id="persona-1",
        appearance="short silver hair, green jacket",
        rating="PG-13",
        user_request="send me a naughty lingerie photo",
        tool_prompt="lingerie photo, bedroom",
    )
    assert ctx["adult_request"] is True

    payload = persona_images.compose_persona_image_prompt(
        raw_prompt="lingerie photo, bedroom",
        visual_context=ctx,
        structured={"prompt": "short silver hair, green jacket, lingerie in the bedroom, soft light"},
    )
    assert payload["fallback_used"] is True
    assert "lingerie" not in payload["prompt"].lower()
    assert "safe-for-work" in payload["prompt"]
    assert "explicit content" in payload["negative_prompt"]


def test_persona_structured_prompt_gets_appearance_prepended():
    import persona_images

    ctx = persona_images.build_visual_context(
        persona_id="persona-1",
        appearance="short silver hair, green jacket",
        rating="PG-13",
        user_request="send a photo of you hiking a mountain trail",
        tool_prompt="woman hiking a mountain trail at sunrise, misty ridgeline",
    )
    payload = persona_images.compose_persona_image_prompt(
        raw_prompt="woman hiking a mountain trail at sunrise, misty ridgeline",
        visual_context=ctx,
        structured={"prompt": "woman hiking a mountain trail at sunrise, misty ridgeline"},
    )
    assert payload["fallback_used"] is False
    assert payload["prompt"].startswith("short silver hair, green jacket")
    assert "mountain trail" in payload["prompt"]


def test_persona_appearance_adult_words_do_not_flip_adult_routing():
    import persona_images

    appearance = "long red hair, sexy black lingerie under a silk robe"
    cfg = {
        "profiles": {
            "sfw_photo": {"prompt_prefix": "dummy-sfw-prefix"},
            "adult_photo": {"adult": True, "prompt_prefix": "dummy-adult-prefix"},
        }
    }

    # Innocent request + adult words in the appearance (echoed into the tool
    # prompt per the system-prompt template) must not route to adult profiles.
    ctx = persona_images.build_visual_context(
        persona_id="persona-2",
        appearance=appearance,
        rating="NC-17",
        user_request="send me a photo of you drinking coffee at the cafe",
        tool_prompt=f"photo of {appearance}, drinking coffee at the cafe",
    )
    assert ctx["adult_request"] is False
    selection = persona_images.select_persona_image_profile(
        cfg,
        persona_id="persona-2",
        persona_rating="NC-17",
        prompt=f"photo of {appearance}, drinking coffee at the cafe",
        user_request="send me a photo of you drinking coffee at the cafe",
        adult_request=ctx["adult_request"],
    )
    assert selection["key"] == "sfw_photo"

    # A genuine adult request in the user's own words still routes adult.
    adult_ctx = persona_images.build_visual_context(
        persona_id="persona-2",
        appearance=appearance,
        rating="NC-17",
        user_request="send me a naughty lingerie photo",
        tool_prompt="lingerie photo",
    )
    assert adult_ctx["adult_request"] is True
    adult_sel = persona_images.select_persona_image_profile(
        cfg,
        persona_id="persona-2",
        persona_rating="NC-17",
        prompt="lingerie photo",
        user_request="send me a naughty lingerie photo",
        adult_request=adult_ctx["adult_request"],
    )
    assert adult_sel["key"] == "adult_photo"


def test_chat_image_recipe_reuses_prior_seed_for_consistency(tmp_path, monkeypatch):
    import comfyui
    import config
    import tools

    monkeypatch.setattr(config, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "IMAGE_CHAT_CHECKPOINT", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_RESOLUTION", "640x640")
    monkeypatch.setattr(comfyui, "settings_for_checkpoint", lambda _ckpt: {})

    persona_context = {
        "persona_id": "persona-1",
        "persona_rating": "PG-13",
        "appearance": "short silver hair, green jacket",
        "user_request": "another photo, keep your look the same",
        "prior_images": [
            {"metadata": {"prompt": "previous dummy photo", "seed": 4242}},
            {"metadata": {"prompt": "older dummy photo", "seed": 1111}},
        ],
    }
    recipe = tools._resolve_chat_image_recipe(
        {"prompt": "photo of short silver hair, green jacket, smiling"},
        persona_context=persona_context,
    )
    assert recipe["seed"] == 4242
    assert recipe["profile_metadata"]["continuity_seed"] is True

    # An explicit tool-arg seed always wins over the continuity seed.
    explicit = tools._resolve_chat_image_recipe(
        {"prompt": "photo of short silver hair, green jacket, smiling", "seed": 777},
        persona_context=persona_context,
    )
    assert explicit["seed"] == 777
    assert "continuity_seed" not in explicit["profile_metadata"]


def test_chat_image_recipe_fresh_seed_without_consistency_request(tmp_path, monkeypatch):
    import comfyui
    import config
    import tools

    monkeypatch.setattr(config, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "IMAGE_CHAT_CHECKPOINT", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_RESOLUTION", "640x640")
    monkeypatch.setattr(comfyui, "settings_for_checkpoint", lambda _ckpt: {})

    recipe = tools._resolve_chat_image_recipe(
        {"prompt": "photo of short silver hair, green jacket, smiling"},
        persona_context={
            "persona_id": "persona-1",
            "persona_rating": "PG-13",
            "appearance": "short silver hair, green jacket",
            "user_request": "send me another photo",
            "prior_images": [{"metadata": {"prompt": "previous dummy photo", "seed": 4242}}],
        },
    )
    assert recipe["seed"] is None
    assert "continuity_seed" not in recipe["profile_metadata"]


class _FakeComfyResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _patch_comfy_client(monkeypatch, responses):
    import comfyui

    calls = []

    class FakeClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None):
            calls.append(("POST", url, json))
            return responses.pop(0)

        async def get(self, url, params=None):
            calls.append(("GET", url, params))
            return responses.pop(0)

    monkeypatch.setattr(comfyui.config, "COMFYUI_URL", "http://comfy.local:8188")
    monkeypatch.setattr(comfyui.httpx, "AsyncClient", FakeClient)
    return calls


def test_hyprchat_free_uses_custom_node(monkeypatch):
    import comfyui

    calls = _patch_comfy_client(monkeypatch, [
        _FakeComfyResponse(200, {"ok": True, "reason": "manual"}),
        _FakeComfyResponse(200, {}),
    ])
    result = asyncio.run(comfyui.hyprchat_free())

    assert result["ok"] is True
    assert result["custom_node"] is True
    assert result["builtin_free"] is True
    assert calls == [
        ("POST", "http://comfy.local:8188/hyprchat/free", None),
        ("POST", "http://comfy.local:8188/free", {"unload_models": True, "free_memory": True}),
    ]


def test_hyprchat_free_falls_back_to_builtin_free(monkeypatch):
    import comfyui

    calls = _patch_comfy_client(monkeypatch, [
        _FakeComfyResponse(404, {}),
        _FakeComfyResponse(200, {}),
    ])
    result = asyncio.run(comfyui.hyprchat_free())

    assert result["ok"] is True
    assert result["custom_node"] is False
    assert calls[0] == ("POST", "http://comfy.local:8188/hyprchat/free", None)
    assert calls[1] == (
        "POST",
        "http://comfy.local:8188/free",
        {"unload_models": True, "free_memory": True},
    )


def test_hyprchat_free_reports_busy_queue(monkeypatch):
    import comfyui

    _patch_comfy_client(monkeypatch, [
        _FakeComfyResponse(409, {"ok": False, "error": "ComfyUI queue is active"}),
    ])
    result = asyncio.run(comfyui.hyprchat_free())

    assert result["ok"] is False
    assert result["custom_node"] is True
    assert result["status"] == "busy"
    assert result["error"] == "ComfyUI queue is active"


def test_hyprchat_restart_uses_custom_node(monkeypatch):
    import comfyui

    calls = _patch_comfy_client(monkeypatch, [
        _FakeComfyResponse(200, {"ok": True, "status": "restart_scheduled"}),
    ])
    result = asyncio.run(comfyui.hyprchat_restart())

    assert result["ok"] is True
    assert result["custom_node"] is True
    assert result["status"] == "restart_scheduled"
    assert calls == [("POST", "http://comfy.local:8188/hyprchat/restart", None)]


def test_hyprchat_restart_reports_missing_route(monkeypatch):
    import comfyui

    calls = _patch_comfy_client(monkeypatch, [
        _FakeComfyResponse(404, {}),
    ])
    result = asyncio.run(comfyui.hyprchat_restart())

    assert result["ok"] is False
    assert result["custom_node"] is False
    assert result["status"] == "missing_restart_route"
    assert calls == [("POST", "http://comfy.local:8188/hyprchat/restart", None)]


# ── Pure-unit: chat quality baseline, preset merge, loras, enhancer ────────

def test_chat_image_recipe_default_quality_baseline(monkeypatch):
    import config
    import tools
    from image_prompt_enhancer import DEFAULT_QUALITY_PREFIX, _DEFAULT_NEGATIVE_TAGS

    monkeypatch.setattr(config, "IMAGE_CHAT_CHECKPOINT", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_WORKFLOW", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_PROMPT_PREFIX", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_NEGATIVE", "")

    recipe = tools._resolve_chat_image_recipe({"prompt": "a cat"})

    assert recipe["prompt"].startswith(DEFAULT_QUALITY_PREFIX)
    assert recipe["prompt"].endswith("a cat")
    for tag in _DEFAULT_NEGATIVE_TAGS:
        assert tag in recipe["negative_prompt"]


def test_chat_image_recipe_config_prefix_beats_baseline(monkeypatch):
    import config
    import tools

    monkeypatch.setattr(config, "IMAGE_CHAT_CHECKPOINT", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_WORKFLOW", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_PROMPT_PREFIX", "custom-style-tags")
    monkeypatch.setattr(config, "IMAGE_CHAT_NEGATIVE", "custom-negative")

    recipe = tools._resolve_chat_image_recipe({"prompt": "a cat"})

    assert recipe["prompt"] == "custom-style-tags, a cat"
    assert "masterpiece" not in recipe["prompt"]
    assert recipe["negative_prompt"] == "custom-negative"


def test_chat_image_recipe_baseline_skipped_for_persona(tmp_path, monkeypatch):
    import config
    import tools

    monkeypatch.setattr(config, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "IMAGE_CHAT_CHECKPOINT", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_WORKFLOW", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_PROMPT_PREFIX", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_NEGATIVE", "")

    recipe = tools._resolve_chat_image_recipe(
        {"prompt": "selfie in a cafe"},
        persona_context={
            "persona_id": "persona-1",
            "persona_rating": "PG-13",
            "appearance": "short silver hair, green jacket",
            "user_request": "send a selfie",
        },
    )

    assert "masterpiece" not in recipe["prompt"]
    assert "lowres" not in recipe["negative_prompt"]


def test_chat_image_recipe_dedupes_negative_tags(monkeypatch):
    import config
    import tools

    monkeypatch.setattr(config, "IMAGE_CHAT_CHECKPOINT", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_WORKFLOW", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_PROMPT_PREFIX", "")
    monkeypatch.setattr(config, "IMAGE_CHAT_NEGATIVE", "")

    recipe = tools._resolve_chat_image_recipe(
        {"prompt": "a cat", "negative_prompt": "blurry, Watermark, motion streaks"})

    assert recipe["negative_prompt"].lower().count("blurry") == 1
    assert recipe["negative_prompt"].lower().count("watermark") == 1
    assert "motion streaks" in recipe["negative_prompt"]


def test_merge_generation_preset(monkeypatch):
    import comfyui

    monkeypatch.setattr(comfyui, "settings_for_checkpoint", lambda _c: {
        "steps": 32, "cfg": 5, "sampler": "euler", "scheduler": "beta",
        "model_sampling": "flow",
    })

    merged = comfyui.merge_generation_preset({}, "bigaspV25_v25.safetensors")
    assert merged == {"steps": 32, "cfg": 5, "sampler": "euler",
                      "scheduler": "beta", "model_sampling": "flow"}

    merged = comfyui.merge_generation_preset(
        {"steps": 12, "cfg": 2.0, "sampler": "dpmpp_2m", "scheduler": "karras",
         "model_sampling": "vpred"},
        "bigaspV25_v25.safetensors")
    assert merged == {"steps": 12, "cfg": 2.0, "sampler": "dpmpp_2m",
                      "scheduler": "karras", "model_sampling": "vpred"}

    merged = comfyui.merge_generation_preset({}, "")
    assert merged == {"steps": 25, "cfg": 7.0, "sampler": "", "scheduler": "",
                      "model_sampling": ""}


def test_list_loras_parses_object_info(monkeypatch):
    import comfyui

    calls = _patch_comfy_client(monkeypatch, [
        _FakeComfyResponse(200, {"LoraLoader": {"input": {"required": {
            "lora_name": [["style-a.safetensors", "style-b.safetensors"]],
        }}}}),
    ])
    loras = asyncio.run(comfyui.list_loras())

    assert loras == ["style-a.safetensors", "style-b.safetensors"]
    assert calls == [("GET", "http://comfy.local:8188/object_info/LoraLoader", None)]


def test_coerce_lora_chain_public_alias():
    import comfyui

    chain = comfyui.coerce_lora_chain([
        "plain.safetensors",
        {"name": "tuned.safetensors", "strength": 0.6},
        {"no_name": True},
    ])
    assert chain == [
        {"name": "plain.safetensors", "strength_model": 1.0, "strength_clip": 1.0},
        {"name": "tuned.safetensors", "strength_model": 0.6, "strength_clip": 0.6},
    ]


def test_enhance_prompt_helper_fail_open(monkeypatch):
    import image_prompt_enhancer as ipe
    import model_providers

    async def ok_chat(http, model, prompt, **kw):
        return json.dumps({"prompt": "a red fox in snow, detailed fur, winter light",
                           "negative_prompt": "blurry, lowres, watermark, text, deformed"})
    monkeypatch.setattr(model_providers, "complete_chat", ok_chat)
    result = asyncio.run(ipe.enhance_prompt(object(), "a fox"))
    assert result is not None
    prompt, negative, model = result
    assert "red fox" in prompt
    assert "blurry" in negative
    assert model

    async def broken_chat(http, model, prompt, **kw):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(model_providers, "complete_chat", broken_chat)
    assert asyncio.run(ipe.enhance_prompt(object(), "a fox")) is None

    async def empty_chat(http, model, prompt, **kw):
        return ""
    monkeypatch.setattr(model_providers, "complete_chat", empty_chat)
    assert asyncio.run(ipe.enhance_prompt(object(), "a fox")) is None

    async def placeholder_chat(http, model, prompt, **kw):
        return json.dumps({"prompt": "...", "negative_prompt": ""})
    monkeypatch.setattr(model_providers, "complete_chat", placeholder_chat)
    assert asyncio.run(ipe.enhance_prompt(object(), "a fox")) is None

    assert asyncio.run(ipe.enhance_prompt(object(), "")) is None


def test_persona_intent_detection_after_dedupe():
    import persona_images

    intents = persona_images.detect_image_intents("keep the same face as the reference")
    assert "consistency" in intents
    assert "identity" not in intents

    intents = persona_images.detect_image_intents("send a mirror selfie")
    assert "mirror_selfie" in intents

    assert "identity" not in persona_images._INTENT_PROFILE_KEYS


def test_params_from_history_recovers_sdxl_graph():
    import comfyui

    wf, seed = comfyui.build_workflow(
        comfyui.SDXL_T2I_TEMPLATE,
        prompt="a lighthouse at dusk",
        negative_prompt="blurry",
        width=1216, height=832, steps=20, cfg=6.5, seed=777,
        checkpoint="dummy.safetensors",
    )
    params = comfyui.params_from_history({"prompt": [0, "job-1", wf, {}, []]})

    assert params["prompt"] == "a lighthouse at dusk"
    assert params["negative_prompt"] == "blurry"
    assert params["seed"] == 777
    assert params["steps"] == 20 and params["cfg"] == 6.5
    assert params["width"] == 1216 and params["height"] == 832
    assert params["checkpoint"] == "dummy.safetensors"

    assert comfyui.params_from_history({}) == {}
    assert comfyui.params_from_history({"prompt": "garbage"}) == {}
    assert comfyui.params_from_history(None) == {}


# ── Live integration (needs a healthy ComfyUI behind the server) ──────────

@pytest.fixture(scope="module")
def comfyui_live(client, base_url):
    try:
        r = client.get("/api/health")
    except (httpx.HTTPError, OSError) as e:
        pytest.skip(f"HyprChat server unavailable at {base_url}: {e.__class__.__name__}")
    if r.status_code != 200:
        pytest.skip(f"HyprChat health endpoint unavailable at {base_url}: HTTP {r.status_code}")
    try:
        h = r.json().get("services", {})
    except ValueError:
        pytest.skip(f"HyprChat health endpoint returned non-JSON at {base_url}")
    if h.get("comfyui", {}).get("status") != "ok":
        pytest.skip("ComfyUI not configured or unhealthy")
    return True


def test_checkpoints_endpoint(client, comfyui_live):
    r = client.get("/api/images/checkpoints")
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d.get("checkpoints"), list)
    assert isinstance(d.get("loras"), list)
    assert isinstance(d.get("samplers"), list) and "euler" in d["samplers"]
    assert isinstance(d.get("schedulers"), list) and "normal" in d["schedulers"]


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
