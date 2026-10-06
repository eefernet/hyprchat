"""Shared, bounded schemas for model-facing Office content and preflight validation."""
import json
import math
import re

S = {"type": "string"}
N = {"type": "number"}
VALUE = {"type": ["string", "number", "boolean", "null"]}

def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}

def array(items, minimum=0, maximum=20000):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}

STYLE = obj({"bold": {"type": "boolean"}, "italic": {"type": "boolean"}, "size": N, "font": S,
             "color": S, "fill": S, "number_format": S, "align": S, "wrap": {"type": "boolean"}})
ROWS = array(array(VALUE, 1), 1)
SERIES = array(obj({"name": S, "values": array(N, 1)}, ["name", "values"]), 1)
BLOCK = obj({"type": {"type": "string", "enum": ["paragraph", "table", "image", "page_break"]},
             "text": S, "heading": {"type": "integer", "minimum": 0, "maximum": 9}, "style": S,
             "runs": array(obj({"text": S, "bold": {"type": "boolean"}, "italic": {"type": "boolean"}, "size": N, "color": S}, ["text"]), 1),
             "rows": ROWS, "image_artifact_id": S, "width": N, "caption": S})
ELEMENT = obj({"type": {"type": "string", "enum": ["text", "table", "image", "chart"]}, "text": S, "rows": ROWS,
               "image_artifact_id": S, "categories": array(S, 1), "series": SERIES,
               "chart_type": {"type": "string", "enum": ["bar", "line", "pie"]},
               **{k: N for k in ("x", "y", "width", "height", "size")}})
SLIDE = obj({"title": S, "notes": S, "bullets": array(S), "elements": array(ELEMENT)}, ["title"])
SHEET = obj({"name": S, "rows": ROWS, "cells": {"type": "object", "additionalProperties": VALUE},
             "formats": array(obj({"range": S, "style": STYLE}, ["range", "style"])),
             "column_widths": {"type": "object", "additionalProperties": N}, "freeze_panes": S,
             "tables": array(obj({"name": S, "range": S}, ["name", "range"])),
             "charts": array(obj({"type": {"type": "string", "enum": ["bar", "line", "pie"]}, "range": S, "title": S, "anchor": S}, ["range"]))}, ["name"])
CONTENT = obj({"blocks": array(BLOCK, 1, 2000), "header": S, "footer": S,
               "slides": array(SLIDE, 1, 200), "sheets": array(SHEET, 1, 100)})
EXAMPLES = {"docx": {"blocks": [{"text": "Report text"}, {"type": "table", "rows": [["Name", "Value"], ["A", "10"]]}]},
            "pptx": {"slides": [{"title": "Report", "bullets": ["Finding"]}]},
            "xlsx": {"sheets": [{"name": "Data", "rows": [["Name", "Value"], ["A", 10]]}]}}


def markdown_content(text):
    """Convert basic chat Markdown without network access or dropping unknown syntax."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("markdown must be a nonempty string")
    if len(text.encode("utf-8")) > 1024 * 1024:
        raise ValueError("markdown exceeds 1 MB")
    lines, blocks, index, fence = text.splitlines(), [], 0, None

    def cells(line):
        interior = re.sub(r"^\||(?<!\\)\|$", "", line.strip())
        return [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", interior)]

    def inline(value):
        runs = []
        for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", value):
            if not part:
                continue
            if part.startswith("**") and part.endswith("**"):
                runs.append({"text": part[2:-2], "bold": True})
            elif part.startswith("`") and part.endswith("`"):
                runs.append({"text": part[1:-1]})
            else:
                runs.append({"text": part})
        return runs

    while index < len(lines):
        line = lines[index]
        index += 1
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker and fence is None:
            fence = marker.group(1)
            continue
        if fence is not None:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
            else:
                blocks.append({"text": line})
            continue
        if not line.strip():
            continue
        if "|" in line and index < len(lines):
            header, separators = cells(line), cells(lines[index])
            if len(header) == len(separators) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in separators):
                rows = [header]
                index += 1
                while index < len(lines) and "|" in lines[index] and len(cells(lines[index])) == len(header):
                    rows.append(cells(lines[index]))
                    index += 1
                blocks.append({"type": "table", "rows": [["".join(r["text"] for r in inline(cell)) for cell in row] for row in rows]})
                continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        block = {"runs": inline(heading.group(2) if heading else line)}
        if heading:
            block["heading"] = len(heading.group(1))
        blocks.append(block)
    return validate_content("docx", {"blocks": blocks})


def validate(value, schema, path):
    types = schema.get("type", [])
    if isinstance(types, str):
        types = [types]
    actual = "null" if value is None else "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "string" if isinstance(value, str) else "array" if isinstance(value, list) else "object" if isinstance(value, dict) else "unknown"
    if types and actual not in types and not (actual == "integer" and "number" in types):
        raise ValueError(f"{path} must be {' or '.join(types)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    if actual in {"number", "integer"}:
        if not math.isfinite(value) or value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError(f"{path} is outside the allowed range")
    if actual == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 20000):
            raise ValueError(f"{path} has an invalid number of items")
        for i, item in enumerate(value):
            validate(item, schema.get("items", {}), f"{path}[{i}]")
    if actual == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}.{key} is required")
        for key, item in value.items():
            child = schema.get("properties", {}).get(key, schema.get("additionalProperties", {}))
            if child is False:
                raise ValueError(f"{path}.{key} is unsupported")
            if isinstance(child, dict):
                validate(item, child, f"{path}.{key}")


def validate_content(fmt, content):
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (ValueError, RecursionError) as e:
            location = f" at line {e.lineno}, column {e.colno}" if isinstance(e, json.JSONDecodeError) else ""
            raise ValueError(f"content must be an object; encoded JSON is malformed{location}") from e
    validate(content, CONTENT, "content")
    key = {"docx": "blocks", "pptx": "slides", "xlsx": "sheets"}[fmt]
    if not content.get(key):
        raise ValueError(f"content.{key} must contain at least one item")
    for other in {"blocks", "slides", "sheets"} - {key}:
        if other in content:
            raise ValueError(f"content.{other} does not apply to {fmt}")
    elements = [(f"content.blocks[{i}]", b) for i, b in enumerate(content.get("blocks", []))]
    elements += [(f"content.slides[{i}].elements[{j}]", b) for i, slide in enumerate(content.get("slides", [])) for j, b in enumerate(slide.get("elements", []))]
    for path, b in elements:
        kind = b.get("type", "paragraph")
        required = {"table": ["rows"], "image": ["image_artifact_id"], "chart": ["categories", "series"]}.get(kind, [])
        for field in required:
            if not b.get(field):
                raise ValueError(f"{path}.{field} is required")
        if kind == "table" and any(len(row) != len(b["rows"][0]) for row in b["rows"]):
            raise ValueError(f"{path}.rows must have equal column counts")
        if kind == "chart" and any(len(s["values"]) != len(b["categories"]) for s in b["series"]):
            raise ValueError(f"{path}.series values must match categories")
    return content
