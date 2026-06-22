"""Prompt-enhancer response parsing for Image Studio.

The local model is asked for strict JSON, but smaller models sometimes wrap the
answer in explanations. This module salvages the useful SDXL prompt without
letting assistant prose leak into the UI.
"""
import json
import re


_DEFAULT_NEGATIVE_TAGS = (
    "lowres",
    "bad anatomy",
    "bad hands",
    "extra fingers",
    "blurry",
    "watermark",
    "text",
    "jpeg artifacts",
    "worst quality",
    "deformed",
)

_JSON_DECODER = json.JSONDecoder()
_LABEL_RE = re.compile(
    r"(?ims)^\s*(?:[-*]\s*)?"
    r"(?P<label>negative_prompt|negative prompt|negative|positive_prompt|positive prompt|positive|prompt)"
    r"\s*[:=]\s*"
    r"(?P<body>.*?)"
    r"(?=^\s*(?:[-*]\s*)?(?:negative_prompt|negative prompt|negative|positive_prompt|positive prompt|positive|prompt)\s*[:=]|\Z)"
)
_WRAPPER_LINE_RE = re.compile(
    r"(?i)\b("
    r"we need to|i need to|i will|i'll|here is|here's|sure[,!]?|okay[,!]?|"
    r"the prompt is|the prompt should be|this prompt|json object|respond with|"
    r"sdxl generation|stable diffusion prompt"
    r")\b"
)
_MJ_PARAM_RE = re.compile(r"\s*--\w+(?:\s+[\w:.]+)?")


def _strip_outer_quotes(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"^\s*```(?:json|text)?\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s*```\s*$", "", text).strip()
    quote_pairs = (('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u2018", "\u2019"))
    for left, right in quote_pairs:
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            return text[1:-1].strip()
    return text


def _clean_field(value: str) -> str:
    text = _strip_outer_quotes(value)
    text = re.sub(r"(?im)^\s*(?:positive\s+)?prompt\s*[:=]\s*", "", text).strip()
    text = re.sub(r"(?im)^\s*(?:negative\s+)?prompt\s*[:=]\s*", "", text).strip()
    return text


def _json_fields(parsed) -> tuple[str, str] | None:
    if not isinstance(parsed, dict):
        return None
    prompt = ""
    negative = ""
    for key in ("prompt", "positive_prompt", "positive prompt", "positive"):
        if parsed.get(key):
            prompt = _clean_field(parsed.get(key))
            break
    for key in ("negative_prompt", "negative prompt", "negative"):
        if parsed.get(key):
            negative = _clean_field(parsed.get(key))
            break
    return prompt, negative


def _extract_json_fields(raw: str) -> tuple[str, str] | None:
    try:
        fields = _json_fields(json.loads(raw))
        if fields:
            return fields
    except (json.JSONDecodeError, TypeError):
        pass

    for match in re.finditer(r"\{", raw or ""):
        try:
            parsed, _ = _JSON_DECODER.raw_decode(raw[match.start():])
        except (json.JSONDecodeError, TypeError):
            continue
        fields = _json_fields(parsed)
        if fields:
            return fields
    return None


def _extract_labeled_fields(raw: str) -> tuple[str, str, int | None]:
    prompt = ""
    negative = ""
    negative_start = None
    for match in _LABEL_RE.finditer(raw or ""):
        label = match.group("label").replace("_", " ").lower()
        body = _clean_field(match.group("body"))
        if label.startswith("negative"):
            if not negative:
                negative = body
            if negative_start is None:
                negative_start = match.start()
        elif not prompt:
            prompt = body
    return prompt, negative, negative_start


def _looks_like_image_prompt(text: str) -> bool:
    text = _strip_outer_quotes(text)
    if not text or _WRAPPER_LINE_RE.search(text):
        return False
    words = re.findall(r"[A-Za-z0-9]+", text)
    return len(words) >= 6 and (text.count(",") >= 2 or len(words) >= 14)


def _salvage_quoted_prompt(raw: str) -> str:
    for match in re.finditer(r"[\"“](.+?)[\"”]", raw or "", flags=re.DOTALL):
        candidate = re.sub(r"\s+", " ", match.group(1)).strip()
        if _looks_like_image_prompt(candidate):
            return candidate
    return ""


def _salvage_plain_prompt(raw: str) -> str:
    lines = []
    for line in (raw or "").splitlines():
        stripped = line.strip().strip("-*").strip()
        if not stripped or stripped.startswith("```"):
            lines.append("")
            continue
        if _WRAPPER_LINE_RE.search(stripped):
            continue
        lines.append(stripped)

    text = "\n".join(lines).strip()
    paragraphs = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in re.split(r"\n\s*\n+", text)
        if paragraph.strip()
    ]
    for paragraph in paragraphs:
        candidate = _strip_outer_quotes(paragraph)
        if _looks_like_image_prompt(candidate):
            return candidate
    return ""


def _parse_enhancer_output(raw: str) -> tuple[str, str]:
    raw = str(raw or "").strip()
    if not raw:
        return "", ""

    fields = _extract_json_fields(raw)
    if fields:
        return fields

    prompt, negative, negative_start = _extract_labeled_fields(raw)
    if prompt:
        return prompt, negative

    positive_source = raw[:negative_start].strip() if negative_start is not None else raw
    prompt = _salvage_quoted_prompt(positive_source) or _salvage_plain_prompt(positive_source)
    return prompt, negative


def normalize_enhancer_response(raw: str) -> tuple[str, str]:
    """Return a clean positive/negative prompt pair from model output."""
    prompt, negative = _parse_enhancer_output(raw)
    prompt = _MJ_PARAM_RE.sub("", prompt).strip().rstrip(",").strip()
    negative = _MJ_PARAM_RE.sub("", negative).strip().rstrip(",").strip()

    neg_tags = [tag.strip() for tag in negative.split(",") if tag.strip()][:15]
    if len(neg_tags) < 5:
        existing = {tag.lower() for tag in neg_tags}
        for tag in _DEFAULT_NEGATIVE_TAGS:
            if len(neg_tags) >= 10:
                break
            if tag not in existing:
                neg_tags.append(tag)
                existing.add(tag)
    negative = ", ".join(neg_tags[:15])
    return prompt[:1500], negative[:1500]
