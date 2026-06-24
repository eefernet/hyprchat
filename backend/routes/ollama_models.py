"""Ollama model pull/delete/template routes."""
import json
import re as _re

import httpx
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse

import config
import model_management
from agents.chat import TOOL_TEMPLATES, detect_template_family
from hf import parse_ollama_progress

from .context import route_context

router = APIRouter()


@router.post("/api/models/pull")
async def pull_model(request: Request):
    """Pull a model from Ollama library — streams progress."""
    body = await request.json()
    model_name = body.get("name", "")
    if not model_name:
        raise HTTPException(400, "Model name required")

    async def generate():
        http = route_context().http
        try:
            got_done = False
            async with http.stream(
                "POST",
                f"{config.OLLAMA_URL}/api/pull",
                json={"name": model_name, "stream": True},
                timeout=httpx.Timeout(7200.0, connect=10.0),
            ) as response:
                if response.status_code != 200:
                    err_body = (await response.aread()).decode()[:300]
                    yield f"data: {json.dumps({'error': f'Ollama returned HTTP {response.status_code}: {err_body}'})}\n\n"
                    return
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    sse, key = parse_ollama_progress(line, model_name)
                    if not sse:
                        continue
                    if key == "error":
                        yield sse
                        return
                    if key == "done":
                        got_done = True
                    yield sse
            if not got_done:
                # Stream ended without success — verify model exists
                try:
                    check = await http.post(f"{config.OLLAMA_URL}/api/show", json={"name": model_name})
                    if check.status_code == 200:
                        yield f"data: {json.dumps({'status': 'done', 'message': 'Pull complete'})}\n\n"
                    else:
                        yield f"data: {json.dumps({'error': 'Pull stream ended without confirmation'})}\n\n"
                except Exception:
                    yield f"data: {json.dumps({'error': 'Pull stream ended — could not verify model'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.delete("/api/models/{model_name:path}")
async def delete_model(model_name: str):
    return await model_management.delete_ollama_model(route_context().http, model_name)


@router.post("/api/models/{model_name:path}/create-tool-model")
async def create_tool_model(model_name: str):
    """Patch an HF GGUF model's existing modelfile with a tool-calling TEMPLATE and save as a new model."""
    http = route_context().http
    try:
        show_r = await http.post(f"{config.OLLAMA_URL}/api/show", json={"name": model_name, "verbose": True})
        show_r.raise_for_status()
        existing_mf = show_r.json().get("modelfile", "")
    except Exception as e:
        raise HTTPException(502, f"Could not fetch modelfile: {e}")

    b = model_name.lower()

    if any(x in b for x in ["qwen2.5", "qwen3", "qwen2"]):
        template = (
            "{{- if or .System .Tools }}<|im_start|>system\n"
            "{{- if .System }}\n{{ .System }}\n{{- end }}\n"
            "{{- if .Tools }}\n\n# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
            "You are provided with function signatures within <tools></tools> XML tags:\n\n<tools>\n"
            "{{- range .Tools }}\n{\"type\": \"function\", \"function\": {{ .Function }}}\n{{- end }}\n</tools>\n\n"
            "For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n\n"
            "<tool_call>\n{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call>\n"
            "{{- end }}<|im_end|>\n{{ end }}"
            "{{- range .Messages }}"
            "{{- if eq .Role \"user\" }}<|im_start|>user\n{{ .Content }}<|im_end|>\n"
            "{{- else if eq .Role \"assistant\" }}<|im_start|>assistant\n"
            "{{- if .Content }}{{ .Content }}"
            "{{- else if .ToolCalls }}{{- range .ToolCalls }}<tool_call>\n{\"name\": \"{{ .Function.Name }}\", \"arguments\": {{ .Function.Arguments }}}\n</tool_call>\n{{- end }}"
            "{{- end }}<|im_end|>\n"
            "{{- else if eq .Role \"tool\" }}<|im_start|>user\n<tool_response>\n{{ .Content }}\n</tool_response><|im_end|>\n"
            "{{- end }}{{- end }}<|im_start|>assistant\n"
        )
    elif any(x in b for x in ["llama-3", "llama3"]):
        template = (
            "{{- if or .System .Tools }}<|start_header_id|>system<|end_header_id|>\n\n"
            "{{- if .System }}{{ .System }}\n{{ end }}"
            "{{- if .Tools }}Environment: ipython\nTools: {{ .Tools }}\n{{ end }}"
            "<|eot_id|>{{ end }}"
            "{{- range .Messages }}"
            "{{- if eq .Role \"user\" }}<|start_header_id|>user<|end_header_id|>\n\n{{ .Content }}<|eot_id|>"
            "{{- else if eq .Role \"assistant\" }}<|start_header_id|>assistant<|end_header_id|>\n\n"
            "{{- if .Content }}{{ .Content }}<|eot_id|>"
            "{{- else if .ToolCalls }}<|python_tag|>{{ range .ToolCalls }}{\"name\": \"{{ .Function.Name }}\", \"parameters\": {{ .Function.Arguments }}}{{ end }}<|eot_id|>"
            "{{- end }}"
            "{{- else if eq .Role \"tool\" }}<|start_header_id|>ipython<|end_header_id|>\n\n{{ .Content }}<|eot_id|>"
            "{{- end }}{{- end }}<|start_header_id|>assistant<|end_header_id|>\n\n"
        )
    elif any(x in b for x in ["mistral", "mixtral"]):
        template = (
            "[INST] {{- if .System }}{{ .System }}\n{{ end }}"
            "{{- range .Messages }}{{- if eq .Role \"user\" }}{{ .Content }} [/INST] "
            "{{- else if eq .Role \"assistant\" }}{{ .Content }}</s>[INST] "
            "{{- else if eq .Role \"tool\" }}{{ .Content }} [/INST] "
            "{{- end }}{{- end }}"
        )
    else:
        template = (
            "{{- if or .System .Tools }}<|im_start|>system\n"
            "{{- if .System }}{{ .System }}\n{{- end }}"
            "{{- if .Tools }}\nAvailable tools:\n{{- range .Tools }}\n{{ .Function }}\n{{- end }}\n{{- end }}"
            "<|im_end|>\n{{ end }}"
            "{{- range .Messages }}"
            "{{- if eq .Role \"user\" }}<|im_start|>user\n{{ .Content }}<|im_end|>\n"
            "{{- else if eq .Role \"assistant\" }}<|im_start|>assistant\n{{ .Content }}<|im_end|>\n"
            "{{- else if eq .Role \"tool\" }}<|im_start|>tool\n{{ .Content }}<|im_end|>\n"
            "{{- end }}{{- end }}<|im_start|>assistant\n"
        )

    from_match = _re.search(r'^# FROM (.+)$', existing_mf, _re.MULTILINE)
    from_line = from_match.group(1).strip() if from_match else model_name

    params = {}
    for line in existing_mf.splitlines():
        pm = _re.match(r'^PARAMETER\s+(\w+)\s+(.+)$', line.strip(), _re.IGNORECASE)
        if pm:
            key, val = pm.group(1).lower(), pm.group(2).strip()
            try:
                params[key] = float(val) if '.' in val else int(val)
            except ValueError:
                params[key] = val

    payload: dict = {"name": model_name, "from": from_line, "template": template}
    if params:
        payload["parameters"] = params

    try:
        r = await http.post(
            f"{config.OLLAMA_URL}/api/create",
            json=payload,
            timeout=120,
        )
        if r.status_code not in (200, 201):
            raise HTTPException(r.status_code, f"Ollama error: {r.text[:400]}")
        return {"status": "updated", "name": model_name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Failed to create model: {e}")


@router.get("/api/models/{model_name:path}/info")
async def model_info(model_name: str):
    """Get model details from Ollama."""
    try:
        r = await route_context().http.post(f"{config.OLLAMA_URL}/api/show", json={"name": model_name})
        if r.status_code == 404:
            raise HTTPException(404, f"Model '{model_name}' not found in Ollama")
        r.raise_for_status()
        return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Failed to get model info: {e}")


@router.get("/api/models/{model_name:path}/template-info")
async def get_template_info(model_name: str):
    detected = detect_template_family(model_name)
    return {
        "detected": detected,
        "templates": {k: {"label": v["label"]} for k, v in TOOL_TEMPLATES.items()},
    }


@router.post("/api/models/{model_name:path}/fix-template")
async def fix_model_template(model_name: str, body: dict = Body(default={})):
    """Patch a model's Modelfile to add a tool-calling template and recreate it in Ollama."""
    family = body.get("family") or detect_template_family(model_name)
    tpl = TOOL_TEMPLATES.get(family)
    if not tpl:
        raise HTTPException(400, f"Unknown template family: {family}")

    stop_list = tpl["stops"]
    create_payload = {
        "model": model_name,
        "from": model_name,
        "template": tpl["template"],
        "parameters": {"stop": stop_list},
    }

    try:
        create_r = await route_context().http.post(
            f"{config.OLLAMA_URL}/api/create",
            json=create_payload,
            timeout=120,
        )
        if create_r.status_code not in (200, 201):
            raise HTTPException(502, f"Ollama create failed: {create_r.text[:300]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Failed to recreate model: {e}")

    return {"ok": True, "family": family, "model": model_name}
