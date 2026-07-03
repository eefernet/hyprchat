"""Shared helpers for local Ollama model management."""
from fastapi import HTTPException

import config


async def delete_ollama_model(http, model_name: str) -> dict:
    """Delete a local Ollama model. Tries alternate HF name formats if needed."""
    names_to_try = [model_name]
    if not model_name.startswith("hf.co/") and "/" in model_name:
        names_to_try.append(f"hf.co/{model_name}")
    if model_name.startswith("hf.co/"):
        names_to_try.append(model_name[len("hf.co/"):])
    last_err = None
    for name in names_to_try:
        try:
            r = await http.request("DELETE", f"{config.OLLAMA_URL}/api/delete", json={"name": name})
            if r.status_code in (200, 204):
                return {"status": "deleted", "model": model_name}
            err_text = r.text[:400]
            if "not found" in err_text.lower() and name != names_to_try[-1]:
                continue
            last_err = err_text
        except Exception as e:
            last_err = str(e)
    if last_err and "not found" in last_err.lower():
        return {"status": "deleted", "model": model_name, "note": "already removed from Ollama"}
    raise HTTPException(502, f"Failed to delete model: {last_err}")
