"""Feature routers extracted from backend.main."""
from fastapi import FastAPI

from .context import configure_context
from . import audio, health, hf, model_providers, settings, users


def register_extracted_routes(
    app: FastAPI,
    *,
    http,
    track_bg=None,
    artifact_file_metadata=None,
    storage_health_check=None,
    load_settings=None,
    save_settings=None,
    public_settings_payload=None,
    coerce_service_url=None,
    sandbox_size_bytes=None,
    request_user_id=None,
    request_session_token=None,
    validated_request_user=None,
    delete_artifact_files_for_user_ids=None,
    delete_all_models=None,
) -> None:
    configure_context(
        http=http,
        track_bg=track_bg,
        artifact_file_metadata=artifact_file_metadata,
        storage_health_check=storage_health_check,
        load_settings=load_settings,
        save_settings=save_settings,
        public_settings_payload=public_settings_payload,
        coerce_service_url=coerce_service_url,
        sandbox_size_bytes=sandbox_size_bytes,
        request_user_id=request_user_id,
        request_session_token=request_session_token,
        validated_request_user=validated_request_user,
        delete_artifact_files_for_user_ids=delete_artifact_files_for_user_ids,
        delete_all_models=delete_all_models,
    )
    app.include_router(health.router)
    app.include_router(settings.router)
    app.include_router(users.router)
    app.include_router(audio.router)
    app.include_router(model_providers.router)
    app.include_router(hf.router)
