"""Cloud model provider credential/settings routes."""
from fastapi import APIRouter, Body, HTTPException

import model_providers

from .context import route_context


router = APIRouter()


@router.get("/api/model-providers")
async def get_model_provider_settings():
    return {"providers": await model_providers.provider_statuses()}


@router.patch("/api/model-providers/{provider}")
async def update_model_provider_settings(provider: str, body: dict = Body(...)):
    if provider not in model_providers.CLOUD_PROVIDERS:
        raise HTTPException(404, "Unsupported model provider")
    api_key = body.get("api_key")
    enabled = body.get("enabled") if "enabled" in body else None
    # base_url/label only apply to the custom provider; an explicit empty
    # string clears the value (so "" must pass through, unlike api_key).
    base_url = body.get("base_url") if provider == "custom" and "base_url" in body else None
    label = body.get("label") if provider == "custom" and "label" in body else None
    try:
        status = await model_providers.save_provider(
            provider,
            api_key=api_key if isinstance(api_key, str) and api_key.strip() else None,
            enabled=bool(enabled) if enabled is not None else None,
            base_url=base_url if isinstance(base_url, str) else None,
            label=label if isinstance(label, str) else None,
        )
        return status
    except model_providers.ProviderError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/model-providers/{provider}")
async def delete_model_provider_settings(provider: str):
    if provider not in model_providers.CLOUD_PROVIDERS:
        raise HTTPException(404, "Unsupported model provider")
    try:
        return await model_providers.delete_provider(provider)
    except model_providers.ProviderError as e:
        raise HTTPException(400, str(e))


@router.post("/api/model-providers/{provider}/test")
async def test_model_provider_settings(provider: str, body: dict = Body(default={})):
    if provider not in model_providers.CLOUD_PROVIDERS:
        raise HTTPException(404, "Unsupported model provider")
    api_key = body.get("api_key") if isinstance(body, dict) else None
    try:
        return await model_providers.test_provider(
            route_context().http,
            provider,
            api_key=api_key if isinstance(api_key, str) and api_key.strip() else None,
        )
    except model_providers.ProviderError as e:
        raise HTTPException(e.status_code or 400, str(e))
    except Exception as e:
        raise HTTPException(400, str(e))
