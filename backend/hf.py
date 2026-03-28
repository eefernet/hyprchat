"""
HuggingFace model browser — search, info, readme, download endpoints.
"""
import json
import os
import re

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

import config


HF_MODELS_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), "..", "hf_models")


def parse_ollama_progress(line: str, final_name: str = "") -> tuple[str | None, str | None]:
    """Parse a single Ollama streaming JSON line into a normalized SSE event.

    Returns (sse_string, status_key) where status_key is one of:
    "downloading", "done", "error", "creating", or None.
    """
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None, None
    if d.get("error"):
        return f"data: {json.dumps({'status': 'error', 'message': d['error']})}\n\n", "error"
    status = d.get("status", "")
    completed = d.get("completed") or 0
    total = d.get("total") or 0
    sl = status.lower()
    if ("pulling" in sl or "downloading" in sl or "verifying" in sl) and total:
        pct = int(completed / total * 100)
        mb_d, mb_t = completed / 1048576, total / 1048576
        msg = f"Downloading {mb_d:.0f}/{mb_t:.0f} MB ({pct}%)"
        return f"data: {json.dumps({'status': 'downloading', 'pct': pct, 'message': msg, 'completed': completed, 'total': total})}\n\n", "downloading"
    elif status in ("success", "done"):
        label = final_name or "model"
        return f"data: {json.dumps({'status': 'done', 'message': f'✓ {label!r} ready!', 'model_name': final_name})}\n\n", "done"
    elif status:
        return f"data: {json.dumps({'status': 'creating', 'message': status})}\n\n", "creating"
    return None, None


async def hf_search(http, q: str = "", limit: int = 20, gguf_only: bool = True):
    """Search HuggingFace models."""
    try:
        params: dict = {"search": q, "limit": limit, "sort": "downloads", "direction": -1}
        if gguf_only:
            params["filter"] = "gguf"
        r = await http.get("https://huggingface.co/api/models", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        return [
            {
                "id": m.get("id", ""),
                "downloads": m.get("downloads", 0),
                "likes": m.get("likes", 0),
                "lastModified": m.get("lastModified", ""),
                "tags": (m.get("tags") or [])[:10],
                "pipeline_tag": m.get("pipeline_tag", ""),
            }
            for m in data
        ]
    except Exception as e:
        raise HTTPException(502, f"HuggingFace search failed: {e}")


async def hf_model_info(http, repo_id: str):
    """Get HuggingFace model details including GGUF file listing with sizes."""
    try:
        # Fetch model info and file tree in parallel for sizes
        import asyncio
        model_req = http.get(f"https://huggingface.co/api/models/{repo_id}", timeout=15)
        tree_req = http.get(f"https://huggingface.co/api/models/{repo_id}/tree/main", timeout=15)
        r, tree_r = await asyncio.gather(model_req, tree_req, return_exceptions=True)
        if isinstance(r, Exception):
            raise r
        r.raise_for_status()
        data = r.json()

        # Build size lookup from tree endpoint (has actual file sizes)
        tree_sizes = {}
        if not isinstance(tree_r, Exception) and tree_r.status_code == 200:
            for f in tree_r.json():
                path = f.get("path", "")
                if path.lower().endswith(".gguf"):
                    tree_sizes[path] = f.get("size") or f.get("lfs", {}).get("size") or 0

        gguf_files = []
        for s in data.get("siblings", []):
            fname = s.get("rfilename", "")
            if not fname.lower().endswith(".gguf"):
                continue
            size = tree_sizes.get(fname) or s.get("lfs", {}).get("size") or s.get("size") or 0
            gguf_files.append({"name": fname, "size": size})
        return {
            "id": data.get("id", ""),
            "downloads": data.get("downloads", 0),
            "likes": data.get("likes", 0),
            "lastModified": data.get("lastModified", ""),
            "tags": data.get("tags", []),
            "gguf_files": gguf_files,
        }
    except Exception as e:
        raise HTTPException(502, f"HuggingFace model info failed: {e}")


async def hf_readme(http, repo_id: str):
    """Fetch model README from HuggingFace, stripping YAML front matter."""
    try:
        r = await http.get(f"https://huggingface.co/{repo_id}/raw/main/README.md", timeout=15)
        if r.status_code == 200:
            content = r.text
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    content = parts[2].strip()
            return {"content": content[:20000]}
        return {"content": "No README available for this model."}
    except Exception as e:
        return {"content": f"Failed to fetch README: {e}"}


async def hf_download(http, request: Request):
    """Register a HuggingFace GGUF model with Ollama via pull or legacy modelfile."""
    body = await request.json()
    repo_id = body.get("repo_id", "")
    filenames = body.get("filenames", [])
    model_name = body.get("model_name", "")

    if not repo_id or not filenames:
        raise HTTPException(400, "repo_id and filenames required")

    for fn in filenames:
        safe = os.path.basename(fn)
        if not safe.lower().endswith(".gguf") or safe != fn:
            raise HTTPException(400, f"Invalid filename: {fn}")

    if not model_name:
        base = re.sub(r'\.gguf$', '', filenames[0], flags=re.IGNORECASE)
        base = re.sub(r'-\d{5}-of-\d{5}$', '', base)
        model_name = re.sub(r"[^a-z0-9\-:.]", "-", base.lower())[:60].strip("-")
    model_name = re.sub(r"[^a-z0-9\-:.]", "-", model_name.lower())[:60].strip("-")
    if not model_name:
        raise HTTPException(400, "Invalid model name")

    base_fn = re.sub(r'\.gguf$', '', filenames[0], flags=re.IGNORECASE)
    base_fn = re.sub(r'-\d{5}-of-\d{5}$', '', base_fn)
    quant_m = re.search(r'[.\-_]((?:IQ|Q)\d+[_A-Za-z0-9]*|F\d+|BF16)$', base_fn, re.IGNORECASE)
    quant = quant_m.group(1).upper() if quant_m else None

    hf_pull_name = f"hf.co/{repo_id}" + (f":{quant}" if quant else "")
    hf_url = f"https://huggingface.co/{repo_id}/resolve/main/{filenames[0]}"

    async def generate():
        try:
            yield f"data: {json.dumps({'status': 'creating', 'message': f'Pulling {hf_pull_name} via Ollama...'})}\n\n"

            pull_ok = False
            pull_err = None
            async with http.stream(
                "POST", f"{config.OLLAMA_URL}/api/pull",
                json={"name": hf_pull_name, "stream": True},
                timeout=httpx.Timeout(7200.0, connect=10.0),
            ) as resp:
                if resp.status_code == 200:
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        sse, key = parse_ollama_progress(line, model_name)
                        if not sse:
                            continue
                        if key == "error":
                            pull_err = sse
                            break
                        yield sse
                    if not pull_err:
                        pull_ok = True
                else:
                    pull_err = (await resp.aread()).decode()[:200]

            if pull_ok:
                if model_name.lower() != hf_pull_name.lower():
                    yield f"data: {json.dumps({'status': 'creating', 'message': f'Creating alias {model_name!r}...'})}\n\n"
                    async with http.stream(
                        "POST", f"{config.OLLAMA_URL}/api/create",
                        json={"name": model_name, "from": hf_pull_name, "stream": True},
                        timeout=httpx.Timeout(60.0, connect=10.0),
                    ) as resp2:
                        async for line in resp2.aiter_lines():
                            sse, _ = parse_ollama_progress(line, model_name)
                            if sse:
                                yield sse
                yield f"data: {json.dumps({'status': 'done', 'message': f'✓ {model_name!r} ready!', 'model_name': model_name})}\n\n"
                return

            # Legacy modelfile fallback
            yield f"data: {json.dumps({'status': 'creating', 'message': 'Trying legacy modelfile approach...'})}\n\n"
            async with http.stream(
                "POST", f"{config.OLLAMA_URL}/api/create",
                json={"name": model_name, "modelfile": f"FROM {hf_url}\n", "stream": True},
                timeout=httpx.Timeout(7200.0, connect=10.0),
            ) as resp:
                if resp.status_code != 200:
                    err = (await resp.aread()).decode()[:400]
                    yield f"data: {json.dumps({'status': 'error', 'message': f'All download methods failed. Pull: {pull_err} | Modelfile: {err}'})}\n\n"
                    return
                had_error = False
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    sse, key = parse_ollama_progress(line, model_name)
                    if sse:
                        yield sse
                        if key == "done":
                            return
                        if key == "error":
                            had_error = True
                            return
                if not had_error:
                    # Stream ended without explicit success — verify model exists
                    try:
                        check = await http.post(f"{config.OLLAMA_URL}/api/show", json={"name": model_name})
                        if check.status_code == 200:
                            yield f"data: {json.dumps({'status': 'done', 'message': f'✓ {model_name!r} ready!', 'model_name': model_name})}\n\n"
                        else:
                            yield f"data: {json.dumps({'status': 'error', 'message': 'Stream ended without confirmation — model may not be ready'})}\n\n"
                    except Exception:
                        yield f"data: {json.dumps({'status': 'error', 'message': 'Stream ended without confirmation — could not verify model'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
