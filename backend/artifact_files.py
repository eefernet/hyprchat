"""Shared artifact and download file helpers.

This module owns filesystem-safe artifact path resolution, archive previews,
and indexable text extraction so route handlers and tools do not carry parallel
implementations.
"""
from __future__ import annotations

import hashlib
import html
import os
import posixpath
import re
import stat
import tarfile
import urllib.parse
import zipfile
from datetime import datetime

try:
    from fastapi import HTTPException
except ModuleNotFoundError:  # tests import tools without the FastAPI extra installed
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

import config
import database as db


ARCHIVE_PREVIEW_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".html", ".htm", ".py",
    ".js", ".ts", ".css", ".sh", ".yaml", ".yml", ".toml", ".xml", ".log",
    ".ini", ".conf",
}
ARCHIVE_ENTRY_MAX_BYTES = 512 * 1024
ARCHIVE_ENTRY_MAX_CHARS = 200000


def decode_preview_bytes(content: bytes, headers: dict) -> str:
    ct = headers.get("content-type", "") or ""
    m = re.search(r"charset=([^;\s]+)", ct, re.I)
    enc = (m.group(1) if m else "utf-8").strip("\"'")
    try:
        return content.decode(enc, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


def sanitize_preview_html(raw_html: str, base_url: str) -> str:
    from html import escape as html_escape

    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", raw_html, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"\son[a-z]+\s*=\s*(['\"]).*?\1", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"\son[a-z]+\s*=\s*[^\s>]+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"javascript\s*:", "", cleaned, flags=re.IGNORECASE)
    base_tag = f'<base href="{html_escape(base_url, quote=True)}" target="_blank">'
    if "<head" in cleaned.lower():
        return re.sub(r"(<head[^>]*>)", r"\1" + base_tag, cleaned, count=1, flags=re.IGNORECASE)
    return base_tag + cleaned


def resolve_download_path(filename: str) -> tuple[str | None, str]:
    safe_name = os.path.basename(filename or "")
    if not safe_name or safe_name != filename:
        raise HTTPException(400, "Invalid filename")
    for search_dir in [config.SANDBOX_OUTPUTS_DIR, config.UPLOAD_DIR]:
        filepath = os.path.join(search_dir, safe_name)
        if not os.path.abspath(filepath).startswith(os.path.abspath(search_dir)):
            continue
        if os.path.exists(filepath):
            return filepath, safe_name
    return None, safe_name


def normalize_archive_path(path: str) -> str:
    raw = str(path or "").replace("\\", "/").strip()
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        raise HTTPException(400, "Invalid archive path")
    norm = posixpath.normpath(raw)
    if norm in {"", "."} or norm.startswith("../") or norm == ".." or "/../" in f"/{norm}/":
        raise HTTPException(400, "Invalid archive path")
    return norm


def archive_path_is_safe(path: str) -> bool:
    try:
        normalize_archive_path(path)
        return True
    except HTTPException:
        return False


def zipinfo_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return stat.S_ISLNK(mode)


def archive_entry_previewable(
    path: str,
    size: int,
    is_dir: bool,
    unsafe: bool = False,
    encrypted: bool = False,
    symlink: bool = False,
) -> bool:
    if is_dir or unsafe or encrypted or symlink or int(size or 0) > ARCHIVE_ENTRY_MAX_BYTES:
        return False
    return os.path.splitext(path.lower())[1] in ARCHIVE_PREVIEW_EXTS


def archive_entry_record(
    path: str,
    *,
    size: int = 0,
    is_dir: bool = False,
    compressed_size: int | None = None,
    modified_at: str | None = None,
    encrypted: bool = False,
    symlink: bool = False,
) -> dict:
    display_path = str(path or "").replace("\\", "/")
    unsafe = not archive_path_is_safe(display_path.rstrip("/") or display_path)
    clean = display_path.strip("/")
    if not unsafe:
        clean = normalize_archive_path(display_path.rstrip("/") or display_path)
    clean = clean.rstrip("/") if is_dir else clean
    parent = posixpath.dirname(clean) if "/" in clean else ""
    name = posixpath.basename(clean) + ("/" if is_dir else "")
    ext = os.path.splitext(clean.lower())[1]
    depth = 0 if not parent else len(parent.split("/"))
    previewable = archive_entry_previewable(clean, size, is_dir, unsafe, encrypted, symlink)
    reason = ""
    if unsafe:
        reason = "unsafe path"
    elif symlink:
        reason = "symlink"
    elif encrypted:
        reason = "encrypted"
    elif is_dir:
        reason = "directory"
    elif int(size or 0) > ARCHIVE_ENTRY_MAX_BYTES:
        reason = "oversized"
    elif ext not in ARCHIVE_PREVIEW_EXTS:
        reason = "unsupported type"
    return {
        "name": clean + ("/" if is_dir and not clean.endswith("/") else ""),
        "path": clean,
        "display_name": name,
        "parent": parent,
        "depth": depth,
        "size": int(size or 0),
        "compressed_size": compressed_size,
        "is_dir": bool(is_dir),
        "modified_at": modified_at,
        "ext": ext,
        "previewable": previewable,
        "unsafe": unsafe,
        "encrypted": encrypted,
        "symlink": symlink,
        "preview_blocked_reason": "" if previewable else reason,
    }


def archive_contents_for_path(filepath: str, safe_name: str) -> dict:
    entries = []
    archive_type = ""
    dirs = set()
    if tarfile.is_tarfile(filepath):
        archive_type = "tar"
        with tarfile.open(filepath, "r:*") as tf:
            for m in tf.getmembers():
                entries.append(archive_entry_record(
                    m.name,
                    size=m.size,
                    is_dir=m.isdir(),
                    modified_at=datetime.utcfromtimestamp(m.mtime).isoformat() if m.mtime else None,
                    symlink=m.issym() or m.islnk(),
                ))
    elif zipfile.is_zipfile(filepath):
        archive_type = "zip"
        with zipfile.ZipFile(filepath) as zf:
            for info in zf.infolist():
                modified_at = None
                try:
                    modified_at = datetime(*info.date_time).isoformat()
                except Exception:
                    modified_at = None
                entries.append(archive_entry_record(
                    info.filename,
                    size=info.file_size,
                    compressed_size=info.compress_size,
                    is_dir=info.is_dir(),
                    modified_at=modified_at,
                    encrypted=bool(info.flag_bits & 0x1),
                    symlink=zipinfo_is_symlink(info),
                ))
    else:
        raise HTTPException(400, "Not a supported archive")

    for entry in list(entries):
        path = entry.get("path") or ""
        if entry.get("unsafe"):
            continue
        parts = path.split("/")[:-1] if not entry.get("is_dir") else path.split("/")
        accum = []
        for part in parts:
            if not part:
                continue
            accum.append(part)
            dirs.add("/".join(accum))
    existing_paths = {e["path"] for e in entries if not e.get("unsafe")}
    for directory in sorted(dirs):
        if directory and directory not in existing_paths:
            entries.append(archive_entry_record(directory, is_dir=True))

    def _sort_key(e):
        parts = (e.get("path") or "").rstrip("/").split("/")
        key = []
        for i, p in enumerate(parts):
            is_last = i == len(parts) - 1
            key.append((0 if (e.get("is_dir") or not is_last) else 1, p.lower()))
        return key

    entries.sort(key=_sort_key)
    file_count = len([e for e in entries if not e["is_dir"] and not e.get("unsafe")])
    folder_count = len([e for e in entries if e["is_dir"] and not e.get("unsafe")])
    return {
        "filename": safe_name,
        "archive_type": archive_type,
        "file_count": file_count,
        "folder_count": folder_count,
        "entries": entries,
        "preview_entry_max_bytes": ARCHIVE_ENTRY_MAX_BYTES,
        "preview_max_chars": ARCHIVE_ENTRY_MAX_CHARS,
    }


def looks_binary(raw: bytes) -> bool:
    if b"\x00" in raw:
        return True
    if not raw:
        return False
    sample = raw[:4096]
    control = sum(1 for b in sample if b < 32 and b not in {9, 10, 12, 13})
    return control / max(1, len(sample)) > 0.08


def archive_entry_preview_for_path(filepath: str, safe_name: str, entry_path: str) -> dict:
    wanted = normalize_archive_path(entry_path)
    if not zipfile.is_zipfile(filepath) and not tarfile.is_tarfile(filepath):
        raise HTTPException(400, "Artifact is not a supported archive")
    ext = os.path.splitext(wanted.lower())[1]
    if ext not in ARCHIVE_PREVIEW_EXTS:
        raise HTTPException(415, "Archive entry type is not safe to preview")

    raw = b""
    meta = {"path": wanted, "filename": safe_name}
    if zipfile.is_zipfile(filepath):
        with zipfile.ZipFile(filepath) as zf:
            matches = [
                info for info in zf.infolist()
                if archive_path_is_safe(info.filename.rstrip("/"))
                and normalize_archive_path(info.filename.rstrip("/")) == wanted
            ]
            if not matches:
                raise HTTPException(404, "Archive entry not found")
            info = matches[0]
            record = archive_entry_record(
                info.filename,
                size=info.file_size,
                compressed_size=info.compress_size,
                is_dir=info.is_dir(),
                encrypted=bool(info.flag_bits & 0x1),
                symlink=zipinfo_is_symlink(info),
            )
            if record["is_dir"]:
                raise HTTPException(400, "Archive entry is a directory")
            if record["symlink"]:
                raise HTTPException(400, "Archive entry is a symlink")
            if record["encrypted"]:
                raise HTTPException(400, "Archive entry is encrypted")
            if record["size"] > ARCHIVE_ENTRY_MAX_BYTES:
                raise HTTPException(413, "Archive entry is too large to preview")
            raw = zf.read(info)
            meta.update(record)
    else:
        with tarfile.open(filepath, "r:*") as tf:
            members = [
                m for m in tf.getmembers()
                if archive_path_is_safe(m.name.rstrip("/"))
                and normalize_archive_path(m.name.rstrip("/")) == wanted
            ]
            if not members:
                raise HTTPException(404, "Archive entry not found")
            member = members[0]
            record = archive_entry_record(
                member.name,
                size=member.size,
                is_dir=member.isdir(),
                symlink=member.issym() or member.islnk(),
            )
            if record["is_dir"]:
                raise HTTPException(400, "Archive entry is a directory")
            if record["symlink"]:
                raise HTTPException(400, "Archive entry is a symlink")
            if record["size"] > ARCHIVE_ENTRY_MAX_BYTES:
                raise HTTPException(413, "Archive entry is too large to preview")
            extracted = tf.extractfile(member)
            if not extracted:
                raise HTTPException(400, "Archive entry cannot be read")
            raw = extracted.read(ARCHIVE_ENTRY_MAX_BYTES + 1)
            meta.update(record)

    if len(raw) > ARCHIVE_ENTRY_MAX_BYTES:
        raise HTTPException(413, "Archive entry is too large to preview")
    if looks_binary(raw):
        raise HTTPException(415, "Archive entry appears to be binary")
    content = decode_preview_bytes(raw, {"content-type": ""})[:ARCHIVE_ENTRY_MAX_CHARS]
    return {
        "artifact_filename": safe_name,
        "path": wanted,
        "preview_type": "archive_entry",
        "content": content,
        "language": language_hint(wanted, "text"),
        "size": len(raw),
        "truncated": len(content) >= ARCHIVE_ENTRY_MAX_CHARS,
        "entry": meta,
    }


def artifact_text_preview_allowed(artifact: dict) -> bool:
    kind = (artifact.get("kind") or "").lower()
    if kind in {"html", "markdown", "code", "data", "text"}:
        return True
    ext = os.path.splitext((artifact.get("filename") or "").lower())[1]
    return ext in {
        ".txt", ".log", ".md", ".html", ".htm", ".py", ".js", ".ts", ".json",
        ".css", ".sh", ".rs", ".go", ".java", ".c", ".cpp", ".yaml", ".yml",
        ".toml", ".xml", ".csv", ".ini", ".conf", ".cfg",
    }


def safe_local_artifact_path(path: str | None) -> str | None:
    if not path:
        return None
    try:
        abs_path = os.path.abspath(os.path.expanduser(path))
    except Exception:
        return None
    roots = [
        config.SANDBOX_OUTPUTS_DIR,
        config.UPLOAD_DIR,
        config.KB_DIR,
    ]
    for root in roots:
        root_abs = os.path.abspath(root)
        if abs_path == root_abs or abs_path.startswith(root_abs + os.sep):
            return abs_path
    return None


def artifact_path_for_row(artifact: dict) -> tuple[str | None, str]:
    storage_path = safe_local_artifact_path(artifact.get("storage_path"))
    if storage_path:
        return (storage_path if os.path.exists(storage_path) else None), os.path.basename(storage_path)
    filename = os.path.basename(artifact.get("filename") or "")
    if not filename and artifact.get("url"):
        filename = os.path.basename(urllib.parse.urlparse(artifact["url"]).path)
    if not filename:
        return None, ""
    return resolve_download_path(filename)


def artifact_file_metadata(filepath: str) -> dict:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return {"size_bytes": os.path.getsize(filepath), "sha256": h.hexdigest()}


def language_hint(filename: str, kind: str = "") -> str:
    ext = os.path.splitext((filename or "").lower())[1].lstrip(".")
    aliases = {"md": "markdown", "markdown": "markdown", "py": "python", "js": "javascript", "ts": "typescript", "sh": "bash", "yml": "yaml"}
    if ext:
        return aliases.get(ext, ext)
    return (kind or "text").lower()


def strip_html_text(raw_html: str, max_chars: int) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", raw_html, flags=re.I | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:max_chars]


def extract_indexable_text(
    filepath: str,
    kind: str,
    mime_type: str = "",
    max_chars: int = 500000,
    *,
    filename: str | None = None,
) -> str:
    display_name = filename or os.path.basename(filepath)
    lower = display_name.lower()
    kind = (kind or db.artifact_kind_for_filename(display_name, mime_type)).lower()
    try:
        if kind in {"image", "pdf"}:
            meta = artifact_file_metadata(filepath)
            return f"{display_name}\n{kind.upper()} artifact\nsize_bytes={meta['size_bytes']}\nsha256={meta['sha256']}"
        if kind == "archive":
            preview = archive_contents_for_path(filepath, display_name)
            lines = [f"Archive: {display_name}", f"Files: {preview.get('file_count', 0)}"]
            for entry in preview.get("entries", [])[:500]:
                suffix = "/" if entry.get("is_dir") and not str(entry.get("name", "")).endswith("/") else ""
                lines.append(f"{entry.get('name','')}{suffix} {entry.get('size',0)} bytes")
            return "\n".join(lines)[:max_chars]
        with open(filepath, "rb") as f:
            raw = f.read(min(max_chars * 4, 4 * 1024 * 1024))
        text = decode_preview_bytes(raw, {"content-type": mime_type or ""})
        if kind == "html" or lower.endswith((".html", ".htm")):
            return strip_html_text(text, max_chars)
        return text[:max_chars]
    except Exception:
        return ""


def render_markdown_safe(markdown_text: str) -> str:
    blocks = []
    in_code = False
    code_lines = []
    for line in (markdown_text or "").splitlines()[:2000]:
        if line.strip().startswith("```"):
            if in_code:
                blocks.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        escaped = html.escape(line)
        if not escaped.strip():
            blocks.append("<br>")
        elif escaped.startswith("### "):
            blocks.append(f"<h3>{escaped[4:]}</h3>")
        elif escaped.startswith("## "):
            blocks.append(f"<h2>{escaped[3:]}</h2>")
        elif escaped.startswith("# "):
            blocks.append(f"<h1>{escaped[2:]}</h1>")
        elif escaped.startswith("- "):
            blocks.append(f"<div>&bull; {escaped[2:]}</div>")
        else:
            blocks.append(f"<p>{escaped}</p>")
    if in_code:
        blocks.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
    return "\n".join(blocks)


# Backward-compatible internal aliases for older tests and imports.
_decode_preview_bytes = decode_preview_bytes
_sanitize_preview_html = sanitize_preview_html
_resolve_download_path = resolve_download_path
_normalize_archive_path = normalize_archive_path
_archive_path_is_safe = archive_path_is_safe
_zipinfo_is_symlink = zipinfo_is_symlink
_archive_entry_previewable = archive_entry_previewable
_archive_entry_record = archive_entry_record
_archive_contents_for_path = archive_contents_for_path
_looks_binary = looks_binary
_archive_entry_preview_for_path = archive_entry_preview_for_path
_artifact_text_preview_allowed = artifact_text_preview_allowed
_safe_local_artifact_path = safe_local_artifact_path
_artifact_path_for_row = artifact_path_for_row
_artifact_file_metadata = artifact_file_metadata
_language_hint = language_hint
_strip_html_text = strip_html_text
_extract_indexable_text = extract_indexable_text
_render_markdown_safe = render_markdown_safe
