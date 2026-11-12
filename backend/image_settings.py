"""Owner-scoped image defaults and presets; legacy files only seed new records."""
import copy
import json
import math
import re

import comfyui
import config
import database as db

PREF_KEY = "image-generation"
IMAGE_KEYS = (
    "image_chat_checkpoint", "image_chat_workflow", "image_chat_resolution",
    "image_chat_vae", "image_chat_prompt_prefix", "image_chat_negative",
    "image_chat_compose_model", "image_chat_auto_enhance",
)


def validate_settings(body: dict, *, check_workflow: bool = True) -> dict:
    if not isinstance(body, dict) or set(body) - set(IMAGE_KEYS):
        raise ValueError("Expected only image generation settings")
    clean = {}
    for key, value in body.items():
        if key == "image_chat_auto_enhance":
            if isinstance(value, str) and value.lower() in {"true", "false"}:
                value = value.lower() == "true"
            if type(value) is not bool:
                raise ValueError("image_chat_auto_enhance must be a boolean")
        else:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            value = value.strip()[:500 if key in {"image_chat_prompt_prefix", "image_chat_negative"} else 200]
            if key == "image_chat_resolution":
                if not re.fullmatch(r"\d{3,4}x\d{3,4}", value) or any(
                    n < 256 or n > 2048 or n % 8 for n in map(int, value.split("x"))
                ):
                    raise ValueError("Resolution must use multiples of 8 between 256 and 2048")
            if key == "image_chat_workflow" and check_workflow and value and value not in comfyui.list_workflows():
                raise ValueError("Saved image workflow was not found")
        clean[key] = value
    return clean


def validate_preset(body: dict) -> dict:
    if not isinstance(body, dict) or set(body) - set(comfyui._SETTING_KEYS):
        raise ValueError("Expected only checkpoint preset settings")
    clean = {}
    for key, value in body.items():
        if key in {"cfg", "steps"}:
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number") from None
            if isinstance(value, bool) or not math.isfinite(number):
                raise ValueError(f"{key} must be a finite number")
            value = max(1, min(60, int(number))) if key == "steps" else max(1.0, min(20.0, number))
        else:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            value = value.strip()
            choices = {"model_sampling": {"", "vpred", "flow"}, "sampler": comfyui.ALLOWED_SAMPLERS,
                       "scheduler": comfyui.ALLOWED_SCHEDULERS}.get(key)
            if choices is not None and value not in choices:
                raise ValueError(f"Invalid {key}")
            if key in {"prompt_prefix", "negative_prefix"}:
                value = value[:500]
        clean[key] = value
    return clean


def legacy_snapshot() -> dict:
    return {"settings": {key: getattr(config, key.upper()) for key in IMAGE_KEYS},
            "presets": comfyui.load_model_settings()}


def _clean_snapshot(raw: dict) -> dict:
    # /api/prefs accepts opaque JSON. Never trust even an owner-written blob at
    # generation time. A present empty preset map is an intentional reset.
    result = {"settings": {}, "presets": {}}
    seed = legacy_snapshot()
    settings = raw.get("settings") if isinstance(raw, dict) else None
    settings = settings if isinstance(settings, dict) else {}
    for key in IMAGE_KEYS:
        value = settings.get(key, seed["settings"][key])
        try:
            result["settings"].update(validate_settings({key: value}, check_workflow=False))
        except ValueError:
            result["settings"][key] = seed["settings"][key]
    presets = raw.get("presets") if isinstance(raw, dict) else None
    if isinstance(presets, dict):
        for checkpoint, preset in presets.items():
            if not isinstance(checkpoint, str) or not isinstance(preset, dict):
                continue
            clean = {}
            for key, value in preset.items():
                try:
                    clean.update(validate_preset({key: value}))
                except ValueError:
                    continue
            if clean:
                result["presets"][checkpoint] = clean
    return result


async def _update(*, settings=None, checkpoint=None, preset=None, delete=False) -> dict:
    owner = db.current_user_id()
    conn = await db.get_db()
    try:
        # Serialize initialization and partial merges across concurrent requests.
        await conn.execute("BEGIN IMMEDIATE")
        rows = await conn.execute_fetchall("SELECT value_json FROM user_prefs WHERE user_id=? AND key=?", (owner, PREF_KEY))
        raw = legacy_snapshot()
        if rows:
            try:
                raw = json.loads(rows[0]["value_json"])
            except (ValueError, TypeError):
                raw = {}
        snapshot = _clean_snapshot(raw)
        if settings is not None:
            snapshot["settings"].update(settings)
        if checkpoint is not None:
            if delete:
                if checkpoint not in snapshot["presets"]:
                    raise KeyError("No saved defaults for this model")
                snapshot["presets"].pop(checkpoint)
            else:
                snapshot["presets"][checkpoint] = {**snapshot["presets"].get(checkpoint, {}), **preset}
        if not rows or settings is not None or checkpoint is not None:
            await conn.execute("""INSERT INTO user_prefs(user_id,key,value_json) VALUES(?,?,?)
                ON CONFLICT(user_id,key) DO UPDATE SET value_json=excluded.value_json,updated_at=CURRENT_TIMESTAMP""",
                               (owner, PREF_KEY, json.dumps(snapshot)))
        await conn.commit()
        return copy.deepcopy(snapshot)
    finally:
        await conn.close()


async def get_snapshot() -> dict:
    return await _update()


async def patch_settings(body: dict) -> dict:
    return await _update(settings=validate_settings(body))


async def save_preset(checkpoint: str, body: dict) -> dict:
    return await _update(checkpoint=checkpoint, preset=validate_preset(body))


async def delete_preset(checkpoint: str) -> dict:
    return await _update(checkpoint=checkpoint, delete=True)
