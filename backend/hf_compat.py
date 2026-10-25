"""
Hugging Face GGUF compatibility helpers for the Model Manager.

Pure, testable pieces behind `GET /api/hf/compat` and the exact-file pull in
`hf.hf_download`:

- `pull_tag(filename)` — the `hf.co/<repo>:<tag>` tag Ollama should pull. The
  Hugging Face Ollama registry resolves a bare quant tag (`Q4_K_M`) to ONE file
  of its choosing, which is wrong when a repo ships several files per quant
  (fused-MTP vs `noMTP`, `draft-*` heads). It also resolves the exact filename
  (with or without `.gguf`), so we pull by filename whenever Ollama's tag rules
  allow it (<=80 chars of `[A-Za-z0-9_.-]`, first char alphanumeric/`_`).
- `classify_file(name, siblings)` — main / mmproj / draft / imatrix / other, the
  quant, the MTP variant and split-part info.
- `read_gguf_header(http, url)` — reads just enough of a remote GGUF (HTTP Range)
  to learn `general.architecture`, `general.type`, context length and the
  tensor count. Stops at the first `tokenizer.*` key, so a 20 GB file costs a
  few hundred KB.
- `resolve_manifest(http, repo_id, tag)` — what the HF Ollama registry would
  serve for a tag: model and projector layer sizes.
- `installed_architectures(http)` — `general.architecture` of every model the
  local Ollama already runs (cached), the only honest "will this load" signal
  we have without trying the pull.
"""
from __future__ import annotations

import asyncio
import re
import struct
import time
from typing import Any

import config

OLLAMA_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}")
QUANT_RE = re.compile(r"[.\-_]((?:IQ|Q)\d+[_A-Za-z0-9]*|F\d+|BF16)$", re.IGNORECASE)
SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})(?=\.gguf$|$)", re.IGNORECASE)
HF_MANIFEST_ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"

GGUF_MAGIC = b"GGUF"
_HEADER_CHUNK = 256 * 1024
_HEADER_MAX = 16 * 1024 * 1024
_header_cache: dict[str, dict[str, Any]] = {}
_HEADER_CACHE_MAX = 64
_arch_cache: dict[str, Any] = {"at": 0.0, "value": None}
ARCH_CACHE_TTL = 600.0


# ───────────────────────── filenames ─────────────────────────

def strip_gguf(name: str) -> str:
    return name[:-5] if name.lower().endswith(".gguf") else name


def quant_of(name: str) -> str | None:
    base = SPLIT_RE.sub("", strip_gguf(name))
    m = QUANT_RE.search(base)
    return m.group(1).upper() if m else None


def pull_tag(filename: str) -> str | None:
    """Tag for `hf.co/<repo>:<tag>`: the filename without `.gguf` when Ollama
    accepts it, else the quant, else None (plain `hf.co/<repo>`)."""
    base = SPLIT_RE.sub("", strip_gguf(filename or ""))
    if base and OLLAMA_TAG_RE.fullmatch(base):
        return base
    return quant_of(filename or "")


def classify_file(name: str, siblings: list[str] | None = None) -> dict[str, Any]:
    """Describe one repo file. `siblings` (other filenames in the repo) lets a
    fused-MTP file be recognised by the presence of its `noMTP` twin."""
    lower = (name or "").lower()
    base = lower.rsplit("/", 1)[-1]
    quant = quant_of(name or "")
    kind = "other"
    if base.startswith("mmproj") or ".mmproj" in base:
        kind = "mmproj"
    elif base.endswith(".dat") and "imatrix" in base:
        kind = "imatrix"
    elif re.search(r"(^|[-_.])draft([-_.]|$)", strip_gguf(base)):
        kind = "draft"
    elif base.endswith(".gguf"):
        kind = "main"
    mtp = None
    if kind in ("main", "draft"):
        if "nomtp" in base:
            mtp = "stripped"
        elif quant and any(
            "nomtp" in s.lower() and quant_of(s) == quant and (classify_file(s)["kind"] == kind)
            for s in (siblings or []) if s != name
        ):
            mtp = "fused"
    split = None
    m = SPLIT_RE.search(name or "")
    if m:
        split = {"part": int(m.group(1)), "of": int(m.group(2))}
    return {"name": name, "kind": kind, "quant": quant, "mtp": mtp, "split": split}


# ───────────────────────── GGUF header ─────────────────────────

class _RangeReader:
    """Pulls a remote file forward in Range-request chunks; never more than _HEADER_MAX."""

    def __init__(self, http, url: str):
        self.http, self.url, self.buf, self.pos, self.eof = http, url, bytearray(), 0, False

    async def _fill(self, need: int) -> None:
        while len(self.buf) - self.pos < need and not self.eof:
            start = len(self.buf)
            if start >= _HEADER_MAX:
                raise ValueError("GGUF header exceeds the read budget")
            end = min(start + _HEADER_CHUNK, _HEADER_MAX) - 1
            r = await self.http.get(self.url, headers={"Range": f"bytes={start}-{end}"},
                                    timeout=20, follow_redirects=True)
            if r.status_code not in (200, 206):
                raise ValueError(f"HTTP {r.status_code} reading GGUF header")
            data = r.content
            if r.status_code == 200 and start:
                data = data[start:end + 1]
            if not data:
                self.eof = True
                break
            self.buf += data
            if len(data) < end - start + 1:
                self.eof = True

    async def take(self, n: int) -> bytes:
        await self._fill(n)
        if len(self.buf) - self.pos < n:
            raise ValueError("truncated GGUF header")
        out = bytes(self.buf[self.pos:self.pos + n])
        self.pos += n
        return out


_SCALARS = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2), 4: ("<I", 4), 5: ("<i", 4),
            6: ("<f", 4), 7: ("<B", 1), 10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8)}
_T_STRING, _T_ARRAY = 8, 9


async def _read_value(rd: _RangeReader, vtype: int, keep: bool):
    if vtype in _SCALARS:
        fmt, size = _SCALARS[vtype]
        v = struct.unpack(fmt, await rd.take(size))[0]
        return bool(v) if vtype == 7 else v
    if vtype == _T_STRING:
        n = struct.unpack("<Q", await rd.take(8))[0]
        raw = await rd.take(n)
        return raw.decode("utf-8", "replace") if keep else None
    if vtype == _T_ARRAY:
        etype = struct.unpack("<I", await rd.take(4))[0]
        count = struct.unpack("<Q", await rd.take(8))[0]
        out = []
        for i in range(count):
            v = await _read_value(rd, etype, keep and i < 8)
            if keep and i < 8:
                out.append(v)
        return out if keep else None
    raise ValueError(f"unknown GGUF value type {vtype}")


def parse_gguf_header(data: bytes) -> dict[str, Any]:
    """Synchronous parser over an in-memory buffer (tests, small files)."""
    class _Mem:
        def __init__(self, b): self.b, self.pos = b, 0
        async def take(self, n):
            if self.pos + n > len(self.b): raise ValueError("truncated GGUF header")
            out = self.b[self.pos:self.pos + n]; self.pos += n; return out
    return asyncio.run(_parse_header(_Mem(data)))


async def _parse_header(rd) -> dict[str, Any]:
    if await rd.take(4) != GGUF_MAGIC:
        raise ValueError("not a GGUF file")
    version = struct.unpack("<I", await rd.take(4))[0]
    n_tensors = struct.unpack("<Q", await rd.take(8))[0]
    n_kv = struct.unpack("<Q", await rd.take(8))[0]
    kv: dict[str, Any] = {}
    for _ in range(n_kv):
        klen = struct.unpack("<Q", await rd.take(8))[0]
        key = (await rd.take(klen)).decode("utf-8", "replace")
        vtype = struct.unpack("<I", await rd.take(4))[0]
        if key.startswith("tokenizer.") and "general.architecture" in kv:
            break  # everything we need precedes the vocab; stop before it
        keep = key.startswith("general.") or key.endswith((".context_length", ".block_count", ".embedding_length",
                                                            ".nextn_predict_layers", ".vision.image_size"))
        v = await _read_value(rd, vtype, keep)
        if keep:
            kv[key] = v
    arch = kv.get("general.architecture")
    return {
        "version": version,
        "n_tensors": n_tensors,
        "architecture": arch,
        "type": kv.get("general.type") or "model",
        "name": kv.get("general.name"),
        "file_type": kv.get("general.file_type"),
        "context_length": kv.get(f"{arch}.context_length") if arch else None,
        "block_count": kv.get(f"{arch}.block_count") if arch else None,
        "mtp_layers": kv.get(f"{arch}.nextn_predict_layers") if arch else None,
    }


async def read_gguf_header(http, url: str) -> dict[str, Any]:
    cached = _header_cache.get(url)
    if cached:
        return cached
    result = await _parse_header(_RangeReader(http, url))
    if len(_header_cache) >= _HEADER_CACHE_MAX:
        _header_cache.pop(next(iter(_header_cache)))
    _header_cache[url] = result
    return result


# ───────────────────────── HF Ollama registry ─────────────────────────

def parse_manifest(doc: dict[str, Any]) -> dict[str, Any] | None:
    layers = doc.get("layers") if isinstance(doc, dict) else None
    if not isinstance(layers, list):
        return None
    out = {"model_bytes": 0, "projector_bytes": 0, "layers": []}
    for layer in layers:
        media = str(layer.get("mediaType", ""))
        kind = media.rsplit(".", 1)[-1] if "." in media else media
        size = int(layer.get("size") or 0)
        out["layers"].append({"type": kind, "size": size})
        if kind == "model":
            out["model_bytes"] += size
        elif kind == "projector":
            out["projector_bytes"] += size
    return out


async def resolve_manifest(http, repo_id: str, tag: str | None) -> dict[str, Any] | None:
    url = f"https://huggingface.co/v2/{repo_id}/manifests/{tag or 'latest'}"
    r = await http.get(url, headers={"Accept": HF_MANIFEST_ACCEPT}, timeout=10, follow_redirects=True)
    if r.status_code != 200:
        return None
    try:
        return parse_manifest(r.json())
    except ValueError:
        return None


def tag_candidates(filename: str) -> list[str]:
    """Tags to try, most specific first: filename sans `.gguf`, the filename with
    `.gguf` (the registry resolves some names only in that form), then the quant."""
    base = SPLIT_RE.sub("", strip_gguf(filename or ""))
    out: list[str] = []
    for cand in (base, f"{base}.gguf" if base else "", quant_of(filename or "") or ""):
        if cand and OLLAMA_TAG_RE.fullmatch(cand) and cand not in out:
            out.append(cand)
    return out


async def resolve_tag(http, repo_id: str, filename: str) -> tuple[str | None, dict[str, Any] | None]:
    """(tag, manifest) for the first candidate the HF registry resolves; falls
    back to (pull_tag(filename), None) when nothing resolves or the registry is down."""
    for cand in tag_candidates(filename):
        try:
            m = await resolve_manifest(http, repo_id, cand)
        except Exception:
            m = None
        if m:
            return cand, m
    return pull_tag(filename), None


# ───────────────────────── local Ollama ─────────────────────────

async def installed_architectures(http, force: bool = False) -> dict[str, Any]:
    """{'version': str|None, 'architectures': sorted list, 'by_model': {name: arch}} — cached ARCH_CACHE_TTL."""
    now = time.monotonic()
    if not force and _arch_cache["value"] and now - _arch_cache["at"] < ARCH_CACHE_TTL:
        return _arch_cache["value"]
    version = None
    by_model: dict[str, str] = {}
    try:
        vr = await http.get(f"{config.OLLAMA_URL}/api/version", timeout=5)
        if vr.status_code == 200:
            version = (vr.json() or {}).get("version")
        tr = await http.get(f"{config.OLLAMA_URL}/api/tags", timeout=10)
        names = [m.get("name") for m in (tr.json() or {}).get("models", []) if m.get("name")] if tr.status_code == 200 else []
        sem = asyncio.Semaphore(6)

        async def show(name: str):
            async with sem:
                try:
                    sr = await http.post(f"{config.OLLAMA_URL}/api/show", json={"name": name}, timeout=10)
                    if sr.status_code == 200:
                        arch = ((sr.json() or {}).get("model_info") or {}).get("general.architecture")
                        if arch:
                            by_model[name] = str(arch)
                except Exception:
                    pass

        await asyncio.gather(*(show(n) for n in names))
    except Exception:
        pass
    value = {"version": version, "architectures": sorted(set(by_model.values())), "by_model": by_model}
    if by_model:
        _arch_cache.update(at=now, value=value)
    return value


# ───────────────────────── orchestration ─────────────────────────

async def _guard(coro, seconds: float):
    try:
        return await asyncio.wait_for(coro, seconds)
    except Exception:
        return None


async def compat_info(http, repo_id: str, filename: str) -> dict[str, Any]:
    """Everything the UI needs to judge ONE selected file: tag, classification,
    GGUF header, registry manifest and the local Ollama's architectures. Each
    probe degrades to null on its own; nothing here raises."""
    file_url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"

    async def siblings():
        r = await http.get(f"https://huggingface.co/api/models/{repo_id}", timeout=15)
        if r.status_code != 200:
            return None
        return [s.get("rfilename", "") for s in (r.json() or {}).get("siblings", [])]

    header, resolved, ollama, sibs = await asyncio.gather(
        _guard(read_gguf_header(http, file_url), 25),
        _guard(resolve_tag(http, repo_id, filename), 20),
        _guard(installed_architectures(http), 15),
        _guard(siblings(), 15),
    )
    tag, manifest = resolved if resolved else (pull_tag(filename), None)
    classification = classify_file(filename, sibs or [])
    arch = (header or {}).get("architecture")
    archs = (ollama or {}).get("architectures") or []
    supports = (arch in archs) if (arch and archs) else None
    return {
        "repo_id": repo_id,
        "filename": filename,
        "tag": tag,
        "pull_name": f"hf.co/{repo_id}:{tag}" if tag else f"hf.co/{repo_id}",
        "classification": classification,
        "header": header,
        "manifest": manifest,
        "ollama": {"version": (ollama or {}).get("version"), "architectures": archs, "supports_arch": supports},
    }
