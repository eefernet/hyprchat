import ast
import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

import comfyui
import config
import database as db
import image_settings
from access_policy import authorize, request_policy


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setattr(config, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "COMFYUI_URL", "http://fixture")
    for key in image_settings.IMAGE_KEYS:
        monkeypatch.setattr(config, key.upper(), False if key.endswith("auto_enhance") else "")
    monkeypatch.setattr(config, "IMAGE_CHAT_RESOLUTION", "1024x1024")
    monkeypatch.setattr(config, "IMAGE_CHAT_CHECKPOINT", "legacy.safetensors")
    comfyui.save_model_settings({"legacy.safetensors": {"steps": 17, "model_sampling": "vpred", "prompt_prefix": "legacy style"}})
    asyncio.run(db.init_db())
    return tmp_path


async def as_owner(owner, call, *args, **kwargs):
    token = db.set_current_user_id(owner)
    try:
        return await call(*args, **kwargs)
    finally:
        db.reset_current_user_id(token)


def image_app(monkeypatch):
    from routes import settings
    monkeypatch.setattr(settings, "load_settings", lambda: {"image_chat_checkpoint": "legacy.safetensors"})
    monkeypatch.setattr(settings, "_public_settings_payload", lambda data: data)
    monkeypatch.setattr(settings, "save_settings", lambda _: pytest.fail("Image patch wrote shared settings"))
    app = FastAPI()
    app.include_router(settings.router)
    names = {"save_model_settings_ep", "clear_model_settings_ep", "image_checkpoints"}
    source = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
    nodes = [node for node in source.body if isinstance(node, ast.AsyncFunctionDef)
             and (node.name in names or any(isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant)
                                          and d.args[0].value == "/api/images/checkpoints" for d in node.decorator_list))]
    namespace = {"app": app, "config": config, "comfyui": comfyui, "image_settings": image_settings,
                 "Body": Body, "HTTPException": HTTPException, "time": time,
                 "_image_checkpoints_cache": {"ts": time.time(), "checkpoints": ["legacy.safetensors"], "vaes": []}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "image-routes", "exec"), namespace)

    @app.middleware("http")
    async def access(request, call_next):
        owner, error = await authorize(request_policy(app, request.scope),
                                       request.headers.get("x-hyprchat-user", "default"),
                                       request.headers.get("x-hyprchat-session", ""), db)
        if error:
            return JSONResponse({"detail": error[1]}, status_code=error[0])
        return await as_owner(owner, call_next, request)
    return app


def test_profile_routes_isolate_defaults_and_presets_and_keep_admin_boundary(env, monkeypatch):
    async def run():
        other = await db.create_user("testUser")
        locked = await db.create_user("Locked", "secret")
        app = image_app(monkeypatch)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = {"X-HyprChat-User": other["id"]}
            original = (await client.get("/api/images/settings")).json()
            assert (await client.get("/api/images/settings", headers=headers)).json() == original
            result = await client.patch("/api/images/settings", headers=headers, json={"image_chat_checkpoint": "other.safetensors", "image_chat_auto_enhance": True})
            assert result.status_code == 200
            assert result.json()["image_chat_checkpoint"] == "other.safetensors"
            assert (await client.get("/api/images/settings")).json() == original
            assert (await client.patch("/api/settings", headers=headers, json={"comfyui_url": "http://wrong"})).status_code == 403
            assert (await client.patch("/api/images/settings", headers=headers, json={"comfyui_url": "http://wrong"})).status_code == 422
            assert (await client.get("/api/images/settings", headers={"X-HyprChat-User": locked["id"]})).status_code == 401
            result = await client.put("/api/images/model-settings/legacy.safetensors", headers=headers, json={"prompt_prefix": "private style"})
            assert result.status_code == 200
            assert result.json()["settings"]["steps"] == 17
            assert result.json()["settings"]["model_sampling"] == "vpred"
            listing = (await client.get("/api/images/checkpoints", headers=headers)).json()
            assert listing["settings"]["legacy.safetensors"]["prompt_prefix"] == "private style"
            assert (await client.get("/api/images/checkpoints")).json()["settings"]["legacy.safetensors"]["prompt_prefix"] == "legacy style"
            assert (await client.delete("/api/images/model-settings/legacy.safetensors", headers=headers)).status_code == 200
            assert (await client.get("/api/images/checkpoints", headers=headers)).json()["settings"]["legacy.safetensors"]["user_override"] is False
            assert (await client.patch("/api/settings", json={"image_chat_checkpoint": "main-only.safetensors"})).status_code == 200
            assert config.IMAGE_CHAT_CHECKPOINT == "legacy.safetensors"
            assert (await client.get("/api/images/settings", headers=headers)).json()["image_chat_checkpoint"] == "other.safetensors"
    asyncio.run(run())


def test_initialization_persistence_concurrent_merges_and_reset(env):
    async def run():
        initial = await image_settings.get_snapshot()
        await asyncio.gather(
            image_settings.patch_settings({"image_chat_checkpoint": "changed"}),
            image_settings.patch_settings({"image_chat_resolution": "832x1216"}),
            image_settings.save_preset("legacy.safetensors", {"steps": 23}),
            image_settings.save_preset("legacy.safetensors", {"negative_prefix": "private negative"}),
        )
        saved = await image_settings.get_snapshot()
        assert saved["settings"]["image_chat_checkpoint"] == "changed"
        assert saved["settings"]["image_chat_resolution"] == "832x1216"
        assert saved["presets"]["legacy.safetensors"]["steps"] == 23
        assert saved["presets"]["legacy.safetensors"]["negative_prefix"] == "private negative"
        await db.init_db()  # Restart/startup migration does not reseed saved values.
        assert await image_settings.get_snapshot() == saved
        other = await db.create_user("New profile")
        assert await as_owner(other["id"], image_settings.get_snapshot) == initial
        await image_settings.delete_preset("legacy.safetensors")
        assert (await image_settings.get_snapshot())["presets"] == {}
        assert comfyui.load_model_settings()["legacy.safetensors"]["steps"] == 17
        assert config.IMAGE_CHAT_CHECKPOINT == "legacy.safetensors"
    asyncio.run(run())


@pytest.mark.parametrize("patch", [{"image_chat_auto_enhance": {}}, {"image_chat_resolution": "9999x9999"},
                                   {"image_chat_workflow": "does-not-exist"}, {"user_id": "default"}])
def test_invalid_patch_does_not_change_settings(env, patch):
    async def run():
        before = await image_settings.get_snapshot()
        with pytest.raises(ValueError):
            await image_settings.patch_settings(patch)
        assert await image_settings.get_snapshot() == before
    asyncio.run(run())


def test_generation_uses_owner_snapshot_and_prompt_model(env, monkeypatch):
    import tools
    import image_prompt_enhancer
    import model_providers
    import persona_images
    monkeypatch.setattr(persona_images, "load_persona_image_profiles", lambda: {})
    captured = []
    async def complete(http, model, prompt, **kwargs):
        captured.append(model)
        return json.dumps({"prompt": "a red fox, detailed fur", "negative_prompt": "blurry"})
    monkeypatch.setattr(model_providers, "complete_chat", complete)
    async def run():
        other = await db.create_user("Other")
        async def generate(owner, checkpoint, resolution, steps, compose):
            async def operation():
                await image_settings.patch_settings({"image_chat_checkpoint": checkpoint, "image_chat_resolution": resolution,
                    "image_chat_compose_model": compose, "image_chat_auto_enhance": True})
                await image_settings.save_preset(checkpoint, {"steps": steps, "prompt_prefix": owner})
                snapshot = await image_settings.get_snapshot()
                await image_settings.patch_settings({"image_chat_checkpoint": "later-change"})
                recipe = tools._resolve_chat_image_recipe({"prompt": "a fox"}, image_preferences=snapshot)
                graph, _ = comfyui.build_workflow(comfyui.SDXL_T2I_TEMPLATE, prompt=recipe["prompt"],
                    checkpoint=recipe["checkpoint"], width=recipe["width"], height=recipe["height"], steps=recipe["steps"])
                assert graph["4"]["inputs"]["ckpt_name"] == checkpoint
                assert graph["5"]["inputs"]["width"] == int(resolution.split("x")[0])
                assert graph["3"]["inputs"]["steps"] == steps
                assert owner in graph["6"]["inputs"]["text"]
                assert (await image_prompt_enhancer.enhance_prompt(object(), "a fox", image_preferences=snapshot))[2] == compose
                explicit = comfyui.merge_generation_preset({"steps": 9}, checkpoint, overrides=snapshot["presets"])
                assert explicit["steps"] == 9
            return await as_owner(owner, operation)
        await asyncio.gather(generate("default", "one", "832x1216", 12, "prompt-one"),
                             generate(other["id"], "two", "1216x832", 28, "prompt-two"))
        assert set(captured) == {"prompt-one", "prompt-two"}
    asyncio.run(run())


def test_chat_tool_dispatch_submits_each_owners_settings(env, monkeypatch):
    import tools
    import coder_jobs
    import persona_images
    monkeypatch.setattr(persona_images, "load_persona_image_profiles", lambda: {})
    graphs, enhanced = {}, []

    async def no_coder(*args, **kwargs):
        return None

    async def enhance(http, prompt, **kwargs):
        enhanced.append(kwargs["image_preferences"]["settings"]["image_chat_compose_model"])
        return "enhanced fox", "blurry", enhanced[-1]

    async def submit(graph):
        graphs[db.current_user_id()] = graph
        raise RuntimeError("fixture stops after capturing the real submitted graph")

    class Events:
        async def emit(self, *args):
            pass

    monkeypatch.setattr(coder_jobs, "route_tool", no_coder)
    monkeypatch.setattr(tools.image_prompt_enhancer, "enhance_prompt", enhance)
    monkeypatch.setattr(comfyui, "submit", submit)

    async def run():
        other = await db.create_user("Other")

        async def generate(checkpoint, resolution, auto):
            await image_settings.patch_settings({"image_chat_checkpoint": checkpoint, "image_chat_resolution": resolution,
                "image_chat_vae": "custom-vae", "image_chat_prompt_prefix": "profile style",
                "image_chat_negative": "profile negative", "image_chat_compose_model": "owner-helper",
                "image_chat_auto_enhance": auto})
            await image_settings.save_preset(checkpoint, {"steps": 13, "cfg": 4.0, "sampler": "euler"})
            result = await tools._exec_tool_scoped(None, Events(), "generate_image", {"prompt": "a fox"}, "fixture")
            assert result.startswith("ERROR: Could not start image generation:")

        await asyncio.gather(as_owner("default", generate, "one", "832x1216", False),
                             as_owner(other["id"], generate, "two", "1216x832", True))
        for owner, checkpoint, width in [("default", "one", 832), (other["id"], "two", 1216)]:
            graph = graphs[owner]
            assert graph["4"]["inputs"]["ckpt_name"] == checkpoint
            assert graph["5"]["inputs"]["width"] == width
            assert graph["3"]["inputs"]["steps"] == 13
            assert "profile style" in graph["6"]["inputs"]["text"]
            assert "profile negative" in graph["7"]["inputs"]["text"]
            assert any(n["class_type"] == "VAELoader" and n["inputs"]["vae_name"] == "custom-vae" for n in graph.values())
        assert "enhanced fox" in graphs[other["id"]]["6"]["inputs"]["text"]
        assert "enhanced fox" not in graphs["default"]["6"]["inputs"]["text"]
        assert enhanced == ["owner-helper"]
    asyncio.run(run())
