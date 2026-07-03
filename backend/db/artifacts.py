"""Artifact row classification and normalization helpers."""
from __future__ import annotations

import json
import mimetypes
import os
import re


ARTIFACT_EXT_GROUPS = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico", ".avif"},
    "html": {".html", ".htm"},
    "markdown": {".md", ".markdown", ".mdx"},
    "code": {
        ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".css", ".scss",
        ".rs", ".go", ".java", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
        ".cs", ".rb", ".php", ".swift", ".kt", ".kts", ".lua", ".r", ".sh",
        ".bash", ".zsh", ".fish", ".sql", ".dockerfile",
    },
    "data": {
        ".csv", ".tsv", ".json", ".jsonl", ".xml", ".yaml", ".yml", ".toml",
        ".parquet", ".ndjson", ".sqlite", ".db", ".xls", ".xlsx",
    },
    "archive": {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar"},
    "pdf": {".pdf"},
    "text": {".txt", ".text", ".log", ".ini", ".cfg", ".conf", ".env", ".out", ".err"},
}
ARTIFACT_KIND_SET = set(ARTIFACT_EXT_GROUPS) | {"file", "project"}
ARTIFACT_STATUSES = {"draft", "accepted", "archived"}


def artifact_kind_for_filename(filename: str, mime_type: str = "") -> str:
    lower = (filename or "").lower()
    if lower.endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        return "archive"
    ext = os.path.splitext(lower)[1]
    for kind, exts in ARTIFACT_EXT_GROUPS.items():
        if ext in exts:
            return kind
    mt = (mime_type or "").lower()
    if mt.startswith("image/"):
        return "image"
    if mt in {"text/html", "application/xhtml+xml"}:
        return "html"
    if mt in {"text/markdown", "text/x-markdown"}:
        return "markdown"
    if mt == "application/pdf":
        return "pdf"
    if any(x in mt for x in ("zip", "tar", "gzip", "x-7z", "rar")):
        return "archive"
    if mt.startswith("text/"):
        return "text"
    return "file"


def artifact_mime_type_for_filename(filename: str) -> str:
    lower = (filename or "").lower()
    if lower.endswith(".tar.gz") or lower.endswith(".tgz"):
        return "application/gzip"
    if lower.endswith(".tar.bz2") or lower.endswith(".tbz2"):
        return "application/x-bzip2"
    if lower.endswith(".tar.xz") or lower.endswith(".txz"):
        return "application/x-xz"
    mime, _ = mimetypes.guess_type(filename or "")
    return mime or ""


def normalize_artifact_kind(kind: str, filename: str = "", mime_type: str = "") -> str:
    value = (kind or "").strip().lower()
    if value == "project":
        return "archive"
    return value if value in ARTIFACT_KIND_SET - {"project"} else artifact_kind_for_filename(filename, mime_type)


def normalize_artifact_row(row) -> dict:
    d = dict(row)
    try:
        d["metadata"] = json.loads(d.pop("metadata_json", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["metadata"] = {}
    d["status"] = (d.get("status") or "draft").strip().lower()
    if d["status"] not in ARTIFACT_STATUSES:
        d["status"] = "draft"
    d["pinned"] = 1 if str(d.get("pinned") or "0").lower() in {"1", "true", "yes", "on"} else 0
    d["exists_status"] = (d.get("exists_status") or "unknown").strip().lower() or "unknown"
    d["size_bytes"] = int(d.get("size_bytes") or 0)
    d.setdefault("tags", [])
    d.setdefault("workspaces", [])
    d.setdefault("workspace_ids", [])
    return d


def clean_artifact_tags(tags) -> list[str]:
    cleaned: list[str] = []
    seen = set()
    for tag in tags or []:
        value = re.sub(r"\s+", "-", str(tag or "").strip().lower())
        value = re.sub(r"[^a-z0-9_.:-]", "", value)[:48]
        if value and value not in seen:
            seen.add(value)
            cleaned.append(value)
    return cleaned[:24]


_ARTIFACT_EXT_GROUPS = ARTIFACT_EXT_GROUPS
_ARTIFACT_KIND_SET = ARTIFACT_KIND_SET
_ARTIFACT_STATUSES = ARTIFACT_STATUSES
_normalize_artifact_kind = normalize_artifact_kind
_normalize_artifact_row = normalize_artifact_row
_clean_artifact_tags = clean_artifact_tags
