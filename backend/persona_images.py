"""
Local-only routing for persona chat image recipes.

Profile contents live beside settings.json in persona_image_profiles.json. That
directory is ignored by git, so checkpoint, workflow, LoRA, and embedding trigger
names stay local to the deployment.
"""
import json
import os
import re

import config


PROFILE_FILENAME = "persona_image_profiles.json"

_RESERVED_TOP_LEVEL = {"profiles", "routes", "persona_overrides", "persona_profiles", "defaults"}
_SFW_RATINGS = {"G", "PG", "PG-13"}
_ADULT_RATINGS = {"R", "NC-17", "Unrated"}

_ADULT_REQUEST_RE = re.compile(
    r"\b(?:adult(?:-only)?|nsfw|explicit|unrated|max\s*rating|mature|"
    r"nudes?|naked|undressed|without\s+clothes|no\s+clothes|topless|"
    r"bottomless|lingerie|lewd|naughty|spicy|sexy|sensual|erotic|sexual)\b",
    re.IGNORECASE,
)

_INTENT_PATTERNS = (
    ("another", re.compile(r"\b(?:another|one\s+more|again|new\s+(?:one|photo|pic|picture)|different\s+(?:one|photo|pic|picture))\b", re.I)),
    ("consistency", re.compile(r"\b(?:same\s+face|consistent|identity|reference|ref\s+image|faceid|ipadapter|ip-adapter|keep\s+(?:her|his|their|your)\s+look)\b", re.I)),
    ("mirror_selfie", re.compile(r"\bmirror\s+(?:selfie|shot|photo|pic|picture)\b", re.I)),
    ("selfie", re.compile(r"\b(?:selfie|front\s+camera)\b", re.I)),
    ("full_body", re.compile(r"\b(?:full\s+body|head\s+to\s+toe|standing\s+pose|whole\s+body)\b", re.I)),
    ("outfit", re.compile(r"\b(?:outfit|clothes|clothing|wearing|dressed|wardrobe|costume|uniform|fit\s+check)\b", re.I)),
    ("action", re.compile(r"\b(?:action|doing|running|walking|dancing|fighting|working|cooking|driving|playing|holding|sitting|kneeling|crouching|lying|laying|reclining|arms?\s+up)\b", re.I)),
    ("location", re.compile(r"\b(?:at|in|inside|outside|near|by|on)\s+(?:a\s+|the\s+)?(?:cafe|bar|bed|bedroom|kitchen|street|office|studio|forest|beach|city|room|car|park|school|library|restaurant|club|pool|gym|shop|store)\b", re.I)),
    ("identity", re.compile(r"\b(?:same\s+face|consistent|identity|reference|ref\s+image|faceid|ipadapter|ip-adapter)\b", re.I)),
    ("pose", re.compile(r"\b(?:pose|posing|posture|standing|sitting|kneeling|crouching|lying|laying|reclining|facing\s+away|turned\s+away|back\s+to\s+(?:the\s+)?camera|from\s+behind|back\s+view|rear\s+view|profile\s+view|arms?\s+up)\b", re.I)),
    ("portrait", re.compile(r"\b(?:portrait|headshot|face\s+shot|close[\s-]?up)\b", re.I)),
    ("realism", re.compile(r"\b(?:realistic|photoreal|photo[\s-]?real|dslr|cinematic|real\s+photo|lifelike)\b", re.I)),
    ("anime", re.compile(r"\b(?:anime|manga|cartoon|illustration|drawn|cel\s+shaded)\b", re.I)),
    ("photo", re.compile(r"\b(?:photo|pic|picture|image|snapshot|candid)\b", re.I)),
)

_INTENT_PROFILE_KEYS = {
    "adult": ["adult_photo"],
    "another": ["variation_photo", "portrait_photo", "sfw_photo"],
    "consistency": ["identity_photo", "ipadapter_photo", "reference_photo", "persona_consistency_photo"],
    "mirror_selfie": ["selfie_photo", "closeup_photo", "portrait_photo", "sfw_photo"],
    "selfie": ["selfie_photo", "closeup_photo", "portrait_photo", "sfw_photo"],
    "full_body": ["full_body_photo", "pose_photo", "control_photo", "controlnet_photo"],
    "outfit": ["outfit_photo", "full_body_photo", "pose_photo", "sfw_photo"],
    "action": ["action_photo", "pose_photo", "control_photo", "sfw_photo"],
    "location": ["location_photo", "environment_photo", "portrait_photo", "sfw_photo"],
    "identity": ["identity_photo", "ipadapter_photo", "reference_photo", "persona_consistency_photo"],
    "pose": ["pose_photo", "control_photo", "controlnet_photo"],
    "portrait": ["portrait_photo", "closeup_photo", "selfie_photo"],
    "realism": ["realism_photo", "photo_realism", "photoreal_photo"],
    "anime": ["anime_photo", "illustration_photo"],
    "photo": ["portrait_photo", "sfw_photo", "photo"],
    "default": ["portrait_photo", "sfw_photo", "photo"],
}

_PROMPT_REFUSAL_RE = re.compile(
    r"\b(?:as an ai|i can'?t|i cannot|i won'?t|sorry|unable to|i'll create|here'?s|okay|ok\b|sure|alright)\b",
    re.IGNORECASE,
)
_TOOLISH_RE = re.compile(r"<tool|</tool|```|generate_image\s*\(|\"name\"\s*:\s*\"generate_image\"", re.IGNORECASE)
_SCENE_CUE_RE = re.compile(
    r"\b(?:wearing|dressed|outfit|sitting|standing|walking|running|smiling|laughing|"
    r"crying|angry|nervous|happy|tired|excited|in the|in a|at the|inside|outside|near|"
    r"lying|laying|reclining|facing\s+away|from\s+behind|back\s+view|rear\s+view|"
    r"bed|bedroom|kitchen|cafe|street|office|studio|forest|beach|city|room|library)\b",
    re.IGNORECASE,
)
_CONTINUITY_REQUEST_RE = re.compile(
    r"\b(?:another|one\s+more|again|same(?:\s+(?:scene|setting|place|location|"
    r"outfit|pose|shot|photo|pic|picture|image|one))?|similar|continue|"
    r"continuing|variation|variant|different\s+(?:one|photo|pic|picture|image)|"
    r"like\s+(?:that|this|the\s+last)|as\s+before|last\s+(?:one|photo|pic|picture|image)|"
    r"previous\s+(?:one|photo|pic|picture|image))\b",
    re.IGNORECASE,
)
_PROMPT_LEAK_FRAGMENT_RE = re.compile(
    r"\b(?:caption|previous|prior|last\s+(?:image|photo|pic|picture)|generated\s+image|"
    r"assistant(?:\s+(?:said|says|response|reply))?|user\s*:|assistant\s*:|"
    r"i\s+(?:am|was|sent|send|created|create|will|would)|here'?s|as\s+described)\b",
    re.IGNORECASE,
)
_TOKEN_STOPWORDS = {
    "the", "and", "with", "without", "for", "that", "this", "from", "into",
    "onto", "near", "inside", "outside", "while", "when", "where", "there",
    "their", "they", "them", "your", "you", "hers", "his", "her", "him",
    "send", "show", "share", "give", "make", "create", "take", "post",
    "photo", "photos", "pic", "pics", "picture", "pictures", "image",
    "images", "snapshot", "snap", "portrait", "candid", "camera", "view",
    "framing", "composition", "quality", "detailed", "detail", "natural",
    "casual", "request", "specific", "character", "persona",
}
_REQUEST_ONLY_STOPWORDS = _TOKEN_STOPWORDS | {
    "another", "again", "same", "similar", "continue", "continuing", "scene",
    "setting", "one", "more", "different", "variation", "variant", "selfie",
}

_MIRROR_SELFIE_RE = re.compile(r"\bmirror\s+(?:selfie|shot|photo|pic|picture)\b", re.I)
_VISIBLE_PHONE_RE = re.compile(
    r"\b(?:holding\s+(?:a\s+)?(?:smart)?phone|visible\s+(?:smart)?phone|"
    r"(?:smart)?phone\s+in\s+(?:her|his|their|the)\s+hand|phone\s+prop)\b",
    re.I,
)
_PHONE_REQUEST_RE = re.compile(r"\b(?:phone|smartphone)\b", re.I)

_VISUAL_CONSTRAINT_PATTERNS = (
    (
        "facing away from camera",
        re.compile(r"\b(?:facing\s+away|turned\s+away|back\s+to\s+(?:the\s+)?camera|from\s+behind|back\s+view|rear\s+view)\b", re.I),
    ),
    (
        "lying on a bed",
        re.compile(r"\b(?:(?:lying|laying|reclining|stretched\s+out)\s+(?:on|in)\s+(?:a\s+|the\s+)?bed|(?:on|in)\s+(?:a\s+|the\s+)?bed)\b", re.I),
    ),
    ("sitting pose", re.compile(r"\b(?:sitting|seated)\b", re.I)),
    ("standing pose", re.compile(r"\bstanding\b", re.I)),
    ("profile view", re.compile(r"\bprofile\s+view\b", re.I)),
)


def profiles_path() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_PATH), PROFILE_FILENAME)


def load_persona_image_profiles(path: str | None = None) -> dict:
    path = path or profiles_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[persona-images] profile config load failed: {e}")
        return {}


def normalize_persona_rating(value) -> str:
    s = re.sub(r"[\s_]+", "", str(value or "PG-13").strip().lower())
    if s in {"g", "general", "allages"}:
        return "G"
    if s == "pg":
        return "PG"
    if s in {"pg13", "pg-13", "teen"}:
        return "PG-13"
    if s in {"r", "mature"}:
        return "R"
    if s in {"nc17", "nc-17"}:
        return "NC-17"
    if s in {"unrated", "xxx", "maxxxx"}:
        return "Unrated"
    return "PG-13"


def is_adult_request(text: str) -> bool:
    return bool(_ADULT_REQUEST_RE.search(text or ""))


def detect_image_intents(text: str) -> list[str]:
    found = []
    for intent, pattern in _INTENT_PATTERNS:
        if pattern.search(text or ""):
            found.append(intent)
    return found or ["photo"]


def is_continuity_request(text: str) -> bool:
    """Return true when the latest request explicitly asks to reuse context."""
    return bool(_CONTINUITY_REQUEST_RE.search(text or ""))


def _clip_text(value, limit: int = 320) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "..."


def _join_compact(parts: list[str], *, limit: int = 480) -> str:
    out = []
    total = 0
    for part in parts:
        part = _clip_text(part, max(80, limit))
        if not part:
            continue
        if total + len(part) > limit:
            break
        out.append(part)
        total += len(part) + 2
    return "; ".join(out)


def _message_text(message) -> str:
    if not isinstance(message, dict):
        return ""
    role = str(message.get("role") or "").strip()
    content = _clip_text(message.get("content"), 260)
    if not content:
        return ""
    return f"{role}: {content}" if role else content


def _token_set(text: str, *, request_only: bool = False) -> set[str]:
    stopwords = _REQUEST_ONLY_STOPWORDS if request_only else _TOKEN_STOPWORDS
    return {
        token
        for token in re.findall(r"[a-z][a-z0-9]{2,}", str(text or "").lower())
        if token not in stopwords
    }


def _latest_request_tokens(context: dict) -> set[str]:
    return _token_set(context.get("latest_user_request") or "", request_only=True)


def _tool_prompt_matches_latest(tool_prompt: str, latest_user_request: str) -> bool:
    tool_prompt = str(tool_prompt or "").strip()
    if not tool_prompt:
        return False
    latest_tokens = _token_set(latest_user_request or "", request_only=True)
    if not latest_tokens:
        return True
    return bool(latest_tokens & _token_set(tool_prompt))


def _excluded_context_tokens(context: dict) -> set[str]:
    if context.get("continuity_requested"):
        return set()
    excluded = _token_set(context.get("_excluded_context_text") or "")
    allowed = _token_set(
        "\n".join(str(v or "") for v in (
            context.get("latest_user_request"),
            context.get("appearance"),
        ))
    )
    return excluded - allowed


def _cleanup_prompt_fragment(fragment: str) -> str:
    text = re.sub(r"\s+", " ", str(fragment or "")).strip(" ,;:-")
    text = re.sub(r"\s+([,;:.])", r"\1", text)
    text = re.sub(r"\b(?:at|in|on|by|near|during|with)\s*(?:,|$)", "", text, flags=re.I)
    return text.strip(" ,;:-")


def _remove_context_leakage(prompt: str, context: dict) -> str:
    if context.get("continuity_requested"):
        return str(prompt or "")
    stale_tokens = _excluded_context_tokens(context)
    latest_tokens = _latest_request_tokens(context)
    appearance_tokens = _token_set(context.get("appearance") or "")
    allowed_tokens = latest_tokens | appearance_tokens
    cleaned: list[str] = []
    for fragment in re.split(r"\s*[,;]\s*", str(prompt or "")):
        fragment = _cleanup_prompt_fragment(fragment)
        if not fragment:
            continue
        fragment_tokens = _token_set(fragment)
        stale_overlap = fragment_tokens & stale_tokens
        if _PROMPT_LEAK_FRAGMENT_RE.search(fragment) and not (fragment_tokens & allowed_tokens):
            continue
        if stale_overlap and not (fragment_tokens & allowed_tokens):
            continue
        if stale_overlap:
            for token in sorted(stale_overlap - allowed_tokens, key=len, reverse=True):
                fragment = re.sub(rf"\b{re.escape(token)}\b", "", fragment, flags=re.I)
            fragment = _cleanup_prompt_fragment(fragment)
        if fragment:
            cleaned.append(fragment)
    return ", ".join(cleaned) or str(prompt or "")


def _has_context_leakage(prompt: str, context: dict) -> bool:
    if context.get("continuity_requested"):
        return False
    if _PROMPT_LEAK_FRAGMENT_RE.search(prompt or ""):
        return True
    stale_tokens = _excluded_context_tokens(context)
    return bool(stale_tokens & _token_set(prompt or ""))


def _missing_latest_request(prompt: str, context: dict) -> bool:
    latest_tokens = _latest_request_tokens(context)
    if not latest_tokens:
        return False
    prompt_tokens = _token_set(prompt or "")
    # If the user gave concrete visual terms, at least one should survive in any
    # model-authored prompt. Deterministic fallback and visual constraints then
    # preserve the full request text.
    return not bool(latest_tokens & prompt_tokens)


def _extract_scene_cues(*texts: str, limit: int = 420) -> str:
    cues = []
    for text in texts:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", str(text or "")):
            sentence = _clip_text(sentence, 180)
            if sentence and _SCENE_CUE_RE.search(sentence):
                cues.append(sentence)
            if len(cues) >= 4:
                break
        if len(cues) >= 4:
            break
    return _join_compact(cues, limit=limit)


def _extract_visual_constraints(text: str) -> list[str]:
    constraints = []
    for phrase, pattern in _VISUAL_CONSTRAINT_PATTERNS:
        if pattern.search(text or ""):
            constraints.append(phrase)
    return constraints


def _constraint_present(prompt: str, constraint: str) -> bool:
    text = str(prompt or "").lower()
    if constraint == "facing away from camera":
        return bool(re.search(r"\b(?:facing\s+away|turned\s+away|back\s+to\s+(?:the\s+)?camera|from\s+behind|back\s+view|rear\s+view)\b", text))
    if constraint == "lying on a bed":
        return "bed" in text and bool(re.search(r"\b(?:lying|laying|reclining|stretched\s+out)\b", text))
    if constraint == "sitting pose":
        return bool(re.search(r"\b(?:sitting|seated)\b", text))
    if constraint == "standing pose":
        return bool(re.search(r"\bstanding\b", text))
    if constraint == "profile view":
        return bool(re.search(r"\bprofile\s+view\b", text))
    return constraint.lower() in text


def _append_missing_constraints(prompt: str, constraints: list[str]) -> str:
    additions = [c for c in (constraints or []) if not _constraint_present(prompt, c)]
    if not additions:
        return prompt
    return ", ".join([p for p in (prompt, *additions) if str(p or "").strip()])


def _clean_contradictory_constraints(prompt: str, constraints: list[str]) -> str:
    text = str(prompt or "")
    if "facing away from camera" in (constraints or []):
        text = re.sub(r"\b(?:looking\s+at\s+(?:the\s+)?viewer|eye\s+contact|facing\s+(?:the\s+)?camera|front-facing)\b", "", text, flags=re.I)
    text = re.sub(r"\s*,\s*,+", ",", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


def _clean_unrequested_framing(prompt: str, *, source_text: str, intents: list[str]) -> str:
    """Remove model-added selfie/phone artifacts that the request did not ask for."""
    text = str(prompt or "")
    source = str(source_text or "")
    intents = intents or []
    allow_selfie = "selfie" in intents or "mirror_selfie" in intents or bool(re.search(r"\bselfie\b|\bfront\s+camera\b", source, re.I))
    allow_mirror = "mirror_selfie" in intents or bool(_MIRROR_SELFIE_RE.search(source))
    allow_phone = allow_mirror or bool(_PHONE_REQUEST_RE.search(source) or _VISIBLE_PHONE_RE.search(source))

    removals: list[str] = []
    if not allow_selfie:
        removals.extend([
            r"\bselfie(?:\s+(?:photo|shot|framing|composition))?\b",
            r"\bfront\s+camera(?:\s+(?:angle|perspective|framing))?\b",
        ])
    if not allow_mirror:
        removals.extend([
            r"\bmirror\s+(?:selfie|shot|photo|pic|picture|composition|framing)\b",
            r"\bin\s+(?:a\s+|the\s+)?mirror\b",
        ])
    if not allow_phone:
        removals.extend([
            r"\bphone\s+camera(?:\s+(?:angle|perspective|framing))?\b",
            r"\bholding\s+(?:a\s+)?(?:smart)?phone\b",
            r"\bvisible\s+(?:smart)?phone\b",
            r"\b(?:smart)?phone\s+in\s+(?:her|his|their|the)\s+hand\b",
            r"\bsmartphone\b",
            r"\bphone\s+prop\b",
        ])

    for pattern in removals:
        text = re.sub(pattern, "", text, flags=re.I)
    text = re.sub(r"\s*,\s*,+", ",", text)
    text = re.sub(r"(?:^|,\s*)(?:,\s*)+", ", ", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


def _compact_prior_images(prior_images) -> list[dict]:
    out = []
    if not isinstance(prior_images, list):
        return out
    for item in prior_images[:3]:
        meta = item.get("metadata") if isinstance(item, dict) else item
        if not isinstance(meta, dict):
            continue
        profile_meta = meta.get("persona_image_profile") if isinstance(meta.get("persona_image_profile"), dict) else {}
        row = {
            "prompt": _clip_text(meta.get("prompt"), 220),
            "seed": meta.get("seed"),
            "framing": _clip_text(profile_meta.get("framing") or meta.get("framing"), 80),
            "intent": _clip_text(profile_meta.get("intent"), 40),
            "created_at": _clip_text(item.get("created_at") if isinstance(item, dict) else "", 40),
        }
        row = {k: v for k, v in row.items() if v not in (None, "")}
        if row:
            out.append(row)
    return out


def build_visual_context(
    *,
    persona_id: str = "",
    persona_name: str = "",
    appearance: str = "",
    scenario: str = "",
    lore: str = "",
    rating: str = "PG-13",
    user_request: str = "",
    tool_prompt: str = "",
    current_reply: str = "",
    recent_messages: list | None = None,
    prior_images: list | None = None,
) -> dict:
    """Build a compact, non-public context block for persona image prompting."""
    recent_text = [_message_text(m) for m in (recent_messages or [])[-8:]]
    recent_text = [t for t in recent_text if t]
    latest_request = _clip_text(user_request, 500)
    raw_tool_prompt = _clip_text(tool_prompt, 500)
    continuity_requested = is_continuity_request(latest_request)
    safe_tool_prompt = raw_tool_prompt if (
        continuity_requested or _tool_prompt_matches_latest(raw_tool_prompt, latest_request)
    ) else ""
    compact_prior_images = _compact_prior_images(prior_images or [])
    excluded_context = ""
    if not continuity_requested:
        excluded_context = _join_compact(
            [
                current_reply,
                "\n".join(recent_text[-6:]),
                "\n".join(str(img.get("prompt") or "") for img in compact_prior_images),
            ],
            limit=900,
        )
    combined = "\n".join([
        latest_request,
        safe_tool_prompt,
    ])
    intents = detect_image_intents(combined)
    adult_request = is_adult_request(combined)
    normalized_rating = normalize_persona_rating(rating)
    return {
        "persona_id": _clip_text(persona_id, 80),
        "persona_name": _clip_text(persona_name, 120),
        "appearance": _clip_text(appearance, 700),
        "scenario_hints": _clip_text(scenario, 360),
        "lore_hints": _clip_text(lore, 360),
        "rating": normalized_rating,
        "latest_user_request": latest_request,
        "tool_prompt": safe_tool_prompt,
        "current_reply": _clip_text(current_reply, 500) if continuity_requested else "",
        "recent_scene_cues": _extract_scene_cues(current_reply, "\n".join(recent_text)) if continuity_requested else "",
        "visual_constraints": _extract_visual_constraints(combined),
        "recent_messages": recent_text[-6:] if continuity_requested else [],
        "prior_images": compact_prior_images if continuity_requested else [],
        "prior_image_count": len(compact_prior_images),
        "recent_message_count": len(recent_text),
        "continuity_requested": continuity_requested,
        "_excluded_context_text": excluded_context,
        "intents": intents,
        "primary_intent": next((i for i in intents if i != "photo"), intents[0] if intents else "photo"),
        "adult_request": adult_request,
        "sfw_rating": normalized_rating in _SFW_RATINGS,
    }


def _clean_prompt_line(value, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().strip('"').strip("'").strip()
    return _clip_text(text, limit)


def _valid_prompt_text(text: str) -> bool:
    text = str(text or "").strip()
    if len(text) < 12 or len(text) > 1400:
        return False
    if _PROMPT_REFUSAL_RE.search(text) or _TOOLISH_RE.search(text):
        return False
    if re.search(r"\[[A-Z_ ]{3,}\]", text):
        return False
    if text.count("{") or text.count("}"):
        return False
    return True


def parse_structured_prompt(value) -> dict | None:
    """Validate composer output shaped as prompt/negative/framing/continuity JSON."""
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("```"):
            return None
        try:
            value = json.loads(raw)
        except Exception:
            return None
    if not isinstance(value, dict):
        return None
    prompt = _clean_prompt_line(value.get("prompt"), 1200)
    if not _valid_prompt_text(prompt):
        return None
    negative = _clean_prompt_line(value.get("negative_prompt"), 400)
    framing = _clean_prompt_line(value.get("framing"), 160)
    continuity = _clean_prompt_line(value.get("continuity_notes"), 240)
    return {
        "prompt": prompt,
        "negative_prompt": negative,
        "framing": framing,
        "continuity_notes": continuity,
    }


def framing_for_intents(intents: list[str] | None) -> str:
    intents = intents or ["photo"]
    if "mirror_selfie" in intents:
        return "mirror selfie composition, visible phone only as mirror prop, looking at viewer"
    if "selfie" in intents:
        return "selfie framing, close front-camera perspective, looking at viewer"
    if "full_body" in intents:
        return "full body framing, head to toe"
    if "outfit" in intents:
        return "outfit-focused portrait, clear clothing details"
    if "action" in intents:
        return "candid action photo, natural motion"
    if "location" in intents:
        return "environmental portrait, location visible"
    if "portrait" in intents:
        return "portrait framing, eye-level camera"
    return "candid photo framing"


def _safe_request_text(context: dict, fallback: str) -> str:
    latest = _clip_text(context.get("latest_user_request"), 360)
    tool_prompt = _clip_text(context.get("tool_prompt"), 360)
    fallback_text = _clip_text(fallback, 360)
    request = latest
    if not request and _tool_prompt_matches_latest(tool_prompt, latest):
        request = tool_prompt
    if not request and _tool_prompt_matches_latest(fallback_text, latest):
        request = fallback_text
    if context.get("sfw_rating") and context.get("adult_request"):
        return "safe-for-work in-character photo matching the conversation mood"
    return request or "request-specific in-character photo"


def compose_persona_image_prompt(
    *,
    raw_prompt: str,
    negative_prompt: str = "",
    visual_context: dict | None = None,
    structured: dict | str | None = None,
) -> dict:
    """Return validated prompt parts for a persona image.

    If structured JSON is invalid, a deterministic prompt is built from the
    compact visual context. This keeps roleplay image intelligence server-side
    while leaving the generate_image tool schema unchanged.
    """
    context = visual_context or {}
    parsed = parse_structured_prompt(structured) if structured is not None else None
    fallback_used = parsed is None
    intents = context.get("intents") or detect_image_intents(
        "\n".join(str(v or "") for v in (context.get("latest_user_request"), context.get("tool_prompt") or raw_prompt))
    )
    framing = framing_for_intents(intents)
    continuity = ""
    if context.get("prior_images") and ("another" in intents or "consistency" in intents):
        continuity = "preserve recognizable character identity while changing pose, framing, or setting"

    if parsed:
        prompt = _remove_context_leakage(parsed["prompt"], context)
        if _has_context_leakage(prompt, context) or _missing_latest_request(prompt, context):
            parsed = None
            fallback_used = True

    if parsed:
        neg = parsed.get("negative_prompt") or ""
        framing = parsed.get("framing") or framing
        continuity = parsed.get("continuity_notes") or continuity
    else:
        request = _safe_request_text(context, raw_prompt)
        prompt_parts = [
            context.get("appearance"),
            request,
            ", ".join(context.get("visual_constraints") or []),
            context.get("recent_scene_cues"),
            framing,
        ]
        if "another" in intents:
            prompt_parts.append("new composition, different pose and expression")
        if "consistency" in intents:
            prompt_parts.append("same character identity and facial features")
        prompt = ", ".join(p.strip(" ,") for p in prompt_parts if str(p or "").strip(" ,"))
        neg = ""

    source_text = "\n".join(str(v or "") for v in (
        raw_prompt,
        context.get("latest_user_request"),
        context.get("tool_prompt"),
    ))
    prompt = _remove_context_leakage(prompt, context)
    prompt = _clean_unrequested_framing(prompt, source_text=source_text, intents=intents)
    prompt = _clean_contradictory_constraints(prompt, context.get("visual_constraints") or [])
    prompt = _append_missing_constraints(prompt, context.get("visual_constraints") or [])
    prompt = _remove_context_leakage(prompt, context)

    base_negative = _clean_prompt_line(negative_prompt, 400)
    if base_negative and neg:
        neg = f"{neg}, {base_negative}"
    elif base_negative:
        neg = base_negative
    if context.get("sfw_rating"):
        sfw_guard = "unsafe adult content, explicit content"
        neg = f"{neg}, {sfw_guard}" if neg else sfw_guard

    prompt = _clean_prompt_line(prompt, 1200)
    if not _valid_prompt_text(prompt):
        prompt = _clean_prompt_line(
            ", ".join(p for p in (context.get("appearance"), _safe_request_text(context, raw_prompt), framing) if p),
            900,
        )
        fallback_used = True

    return {
        "prompt": prompt,
        "negative_prompt": _clean_prompt_line(neg, 500),
        "framing": framing,
        "continuity_notes": continuity,
        "fallback_used": fallback_used,
        "intents": intents,
        "primary_intent": next((i for i in intents if i != "photo"), intents[0] if intents else "photo"),
    }


def _profiles_map(config_data: dict) -> dict:
    if not isinstance(config_data, dict):
        return {}
    raw = config_data.get("profiles")
    if not isinstance(raw, dict):
        raw = {
            k: v for k, v in config_data.items()
            if k not in _RESERVED_TOP_LEVEL and isinstance(v, dict)
        }
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _route_list(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if str(v or "").strip()]
    if isinstance(value, dict):
        for key in ("profiles", "profile", "default"):
            if key in value:
                return _route_list(value.get(key))
    return []


def _profile_tags(profile: dict) -> set[str]:
    raw = profile.get("tags") or profile.get("intents") or profile.get("routing_tags") or []
    if isinstance(raw, str):
        raw = re.split(r"[\s,]+", raw)
    if not isinstance(raw, list):
        return set()
    return {str(t).strip().lower() for t in raw if str(t or "").strip()}


def _profile_is_adult(key: str, profile: dict) -> bool:
    if profile.get("adult") is True or profile.get("nsfw") is True:
        return True
    text = " ".join(
        str(v or "") for v in (
            key,
            profile.get("rating"),
            profile.get("safety"),
            profile.get("category"),
            " ".join(_profile_tags(profile)),
        )
    ).lower()
    return any(token in text for token in ("adult", "nsfw", "explicit", "xxx"))


def _profile_enabled(profile: dict) -> bool:
    return profile.get("enabled", True) is not False


def _profile_allowed(key: str, profile: dict, *, rating: str, adult_request: bool) -> bool:
    if not _profile_enabled(profile):
        return False
    if _profile_is_adult(key, profile):
        return rating in _ADULT_RATINGS and adult_request
    return True


def _persona_override_candidates(config_data: dict, persona_id: str, persona_name: str,
                                 intent: str | None = None) -> list[str]:
    out = []
    for container_name in ("persona_overrides", "persona_profiles"):
        container = config_data.get(container_name)
        if not isinstance(container, dict):
            continue
        for key in (persona_id, persona_name):
            if not key or key not in container:
                continue
            value = container.get(key)
            if isinstance(value, dict):
                if intent:
                    out.extend(_route_list(
                        value.get(intent)
                        or value.get(f"{intent}_profile")
                        or value.get(f"{intent}_profiles")
                    ))
                else:
                    out.extend(_route_list(value.get("profiles") or value.get("profile") or value.get("default")))
            else:
                if intent is None:
                    out.extend(_route_list(value))
    return out


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        key = str(item or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def select_persona_image_profile(
    config_data: dict,
    *,
    persona_id: str = "",
    persona_name: str = "",
    persona_rating: str = "PG-13",
    prompt: str = "",
    user_request: str = "",
) -> dict | None:
    """Return a selected profile envelope, or None to use chat defaults.

    The selector is intentionally conservative: it only runs for persona chats,
    never lets SFW ratings use adult profiles, and only lets adult-rated personas
    use adult profiles when the user's request has an adult cue.
    """
    if not persona_id:
        return None
    profiles = _profiles_map(config_data)
    if not profiles:
        return None

    text = f"{user_request or ''}\n{prompt or ''}"
    rating = normalize_persona_rating(persona_rating)
    adult_request = is_adult_request(text)
    intents = detect_image_intents(text)
    if adult_request:
        intents = ["adult", *[i for i in intents if i != "adult"]]

    routes = config_data.get("routes") if isinstance(config_data.get("routes"), dict) else {}
    candidates: list[str] = []
    for intent in intents:
        candidates.extend(_persona_override_candidates(config_data, persona_id, persona_name, intent=intent))
        candidates.extend(_route_list(routes.get(intent)))
        candidates.extend(_route_list(routes.get(f"{intent}_photo")))
        candidates.extend(_INTENT_PROFILE_KEYS.get(intent, []))
    candidates.extend(_persona_override_candidates(config_data, persona_id, persona_name))
    if rating in _ADULT_RATINGS and adult_request:
        candidates.extend(_route_list(routes.get("default_adult")))
    else:
        candidates.extend(_route_list(routes.get("default_sfw")))
    candidates.extend(_route_list(routes.get("default")))
    candidates.extend(_INTENT_PROFILE_KEYS["default"])

    for key in _dedupe(candidates):
        profile = profiles.get(key)
        if profile and _profile_allowed(key, profile, rating=rating, adult_request=adult_request):
            return {
                "key": key,
                "profile": profile,
                "intent": next((i for i in intents if i != "adult"), intents[0] if intents else "photo"),
                "adult_request": adult_request,
                "rating": rating,
            }

    # Last pass: allow local deployments to route by tags without declaring
    # route lists. This only considers already-allowed profiles.
    for intent in intents:
        for key, profile in profiles.items():
            tags = _profile_tags(profile)
            if intent in tags and _profile_allowed(key, profile, rating=rating, adult_request=adult_request):
                return {
                    "key": key,
                    "profile": profile,
                    "intent": intent,
                    "adult_request": adult_request,
                    "rating": rating,
                }
    return None
