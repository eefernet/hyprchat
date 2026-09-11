"""Settings-owned context budgets shared by HyprChat and the standalone worker.

Numeric defaults belong here, never in inference call sites. Workers receive a
resolved policy; the backend resolves it afresh at each model-call boundary.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass


DEFAULTS = {
    "default_num_ctx": 16384,
    "openhands_num_ctx": 32768,
    "aider_num_ctx": 0,
    "research_num_ctx": 40960,
    "daedalus_role_contexts": {},
    "generation_num_predict": 4096,
    "context_headroom_percent": 5,
    "daedalus_compaction": "inherit",
    "context_compaction_threshold": 75,
    "helper_contexts": {
        "workspace": 4096, "title": 2048, "classifier": 2048,
        "compaction": 8192, "extraction": 8192, "image": 4096,
    },
    "daedalus_v3_enabled": False,
    "daedalus_job_seconds": 3600,
    "daedalus_model_calls": 120,
    "daedalus_attempt_turns": 20,
    "daedalus_command_seconds": 600,
    "daedalus_browser_step_seconds": 15,
    "daedalus_upload_mb": 250,
    "daedalus_extracted_mb": 1000,
    "daedalus_storage_mb": 10000,
    "daedalus_exclude_dirs": [
        ".git", "node_modules", ".venv", "venv", "__pycache__",
        "dist", "build", "target", ".cache", ".pytest_cache",
    ],
}
ROLES = ("chat", "architect", "builder", "reviewer", "acceptance", "qa", "fixer", "aider", "compaction")
_CONFIG_KEYS = {
    "default_num_ctx": "DEFAULT_NUM_CTX", "openhands_num_ctx": "OPENHANDS_NUM_CTX",
    "aider_num_ctx": "AIDER_NUM_CTX", "research_num_ctx": "RESEARCH_NUM_CTX",
}


def positive_int(value, name: str, *, inherit=False) -> int:
    if inherit and value in (None, "", 0, "0", "inherit", "default"):
        return 0
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{name} must be a positive integer") from None
    if number <= 0 or str(value).strip() not in (str(number), f"{number}.0"):
        raise ValueError(f"{name} must be a positive integer")
    return number


def runtime_settings() -> dict:
    import config
    values = {**DEFAULTS, **getattr(config, "CONTEXT_SETTINGS", {})}
    for key, attribute in _CONFIG_KEYS.items():
        values[key] = getattr(config, attribute, values[key])
    values["context_compaction"] = getattr(config, "CONTEXT_COMPACTION", "off")
    return values


def validate_patch(patch: dict, current: dict) -> dict:
    """Validate before callers change runtime state or persist any setting."""
    clean = {}
    for key, value in patch.items():
        if key not in DEFAULTS:
            continue
        if key in ("openhands_num_ctx", "aider_num_ctx"):
            clean[key] = positive_int(value, key, inherit=True)
        elif key in ("daedalus_role_contexts", "helper_contexts"):
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be an object")
            allowed = ROLES if key == "daedalus_role_contexts" else DEFAULTS["helper_contexts"]
            if set(value) - set(allowed):
                raise ValueError(f"Unknown {key} entries: {', '.join(sorted(set(value) - set(allowed)))}")
            entries = {k: positive_int(v, f"{key}.{k}", inherit=key == "daedalus_role_contexts") for k,v in value.items()}
            clean[key] = {**DEFAULTS[key], **(current.get(key) or {}), **entries}
        elif key in ("context_headroom_percent", "context_compaction_threshold"):
            number = float(value)
            if not math.isfinite(number) or not 0 < number < 100:
                raise ValueError(f"{key} must be between 0 and 100")
            clean[key] = number
        elif key == "daedalus_compaction":
            if value not in ("inherit", "on", "off"):
                raise ValueError("daedalus_compaction must be inherit, on, or off")
            clean[key] = value
        elif key == "daedalus_v3_enabled":
            if not isinstance(value, bool):
                raise ValueError("daedalus_v3_enabled must be a boolean")
            clean[key] = value
        elif key == "daedalus_exclude_dirs":
            if not isinstance(value, list) or any(not isinstance(v, str) or not v or "/" in v or "\\" in v for v in value):
                raise ValueError("daedalus_exclude_dirs must be directory names")
            clean[key] = list(dict.fromkeys(value))
        else:
            clean[key] = positive_int(value, key)
    if "aider_num_ctx" in clean and "aider" not in (patch.get("daedalus_role_contexts") or {}):
        clean["daedalus_role_contexts"] = {**(current.get("daedalus_role_contexts") or {}), **clean.get("daedalus_role_contexts", {}), "aider":clean["aider_num_ctx"]}
    merged = {**DEFAULTS, **current, **clean}
    for role in ROLES:
        resolve(role, merged)
    return clean


def apply_settings(settings: dict) -> None:
    import config
    values = {**DEFAULTS, **getattr(config, "CONTEXT_SETTINGS", {}),
              **{k: v for k, v in settings.items() if k in DEFAULTS}}
    # Old global Auto was already resolved to the global default. Materialize
    # that value in the returned settings so it is visible instead of hidden.
    if not values.get("default_num_ctx"):
        values["default_num_ctx"] = DEFAULTS["default_num_ctx"]
    config.CONTEXT_SETTINGS = values
    for key, attribute in _CONFIG_KEYS.items():
        setattr(config, attribute, values[key])


@dataclass(frozen=True)
class ContextPolicy:
    num_ctx: int
    num_predict: int
    input_budget: int
    compact_at: int
    compaction: bool
    source: str
    settings_version: str

    def as_dict(self):
        return asdict(self)


def resolve(role="builder", settings: dict | None = None) -> ContextPolicy:
    values = runtime_settings() if settings is None else {**DEFAULTS, **settings}
    overrides = values.get("daedalus_role_contexts") or {}
    override = overrides.get(role)
    if role == "aider" and not override:
        override = values.get("aider_num_ctx")
    if override:
        context, source = positive_int(override, role), f"stage:{role}"
    elif values.get("openhands_num_ctx"):
        context, source = positive_int(values["openhands_num_ctx"], "openhands_num_ctx"), "daedalus"
    else:
        context, source = positive_int(values["default_num_ctx"], "default_num_ctx"), "global"
    output = positive_int(values["generation_num_predict"], "generation_num_predict")
    available = context - output - math.ceil(context * float(values["context_headroom_percent"]) / 100)
    if available <= 0:
        raise ValueError(f"{role}: context {context} cannot fit the configured completion allowance and headroom; adjust Settings")
    mode = values.get("daedalus_compaction", "inherit")
    if mode == "inherit":
        mode = values.get("context_compaction", "off")
    version = hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return ContextPolicy(context, output, available,
                         int(available * float(values["context_compaction_threshold"]) / 100),
                         str(mode).lower() in ("on", "true", "1"), source, version)


def helper_context(profile="workspace") -> int:
    values = runtime_settings()
    profiles = {**DEFAULTS["helper_contexts"], **values.get("helper_contexts", {})}
    return positive_int(profiles[profile], f"helper_contexts.{profile}")


def estimate_tokens(value) -> int:
    """Approximate byte estimate when the local tokenizer is unavailable.

    Includes tool schemas/arguments, Unicode, and message envelopes. Runtime
    usage is stored separately; this is deliberately labelled an estimate.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return math.ceil(len(text.encode("utf-8")) / 3)


def compaction_segments(messages, tools, policy):
    """Choose complete older turns while preserving every system instruction."""
    estimate = estimate_tokens({"messages": messages, "tools": tools})
    if estimate <= policy.compact_at or (not policy.compaction and estimate <= policy.input_budget):
        return None
    if not policy.compaction:
        raise ValueError("Context is full and automatic compaction is disabled. Adjust Settings to continue.")
    prefix_end = next((index + 1 for index, message in enumerate(messages) if message.get("role") == "user"), 0)
    prefix = [*messages[:prefix_end], *[m for m in messages[prefix_end:] if m.get("role") == "system"]]
    body = [m for m in messages[prefix_end:] if m.get("role") != "system"]
    tail_start = max(0, len(body) - 4)
    while tail_start > 0 and body[tail_start].get("role") == "tool":
        tail_start -= 1
    if not tail_start:
        raise ValueError("Current instructions/tool result exceed configured context; request narrower source ranges or increase context in Settings")
    return prefix, body[:tail_start], body[tail_start:]


def public_settings() -> dict:
    values = runtime_settings()
    resolved = {}
    for role in ROLES:
        try:
            resolved[role] = resolve(role, values).as_dict()
        except ValueError as error:
            resolved[role] = {"error": str(error)}
    return {**{k: values[k] for k in DEFAULTS}, "resolved_contexts": resolved}
