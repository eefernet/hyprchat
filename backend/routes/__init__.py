"""Feature routers extracted from backend.main."""
from fastapi import FastAPI

from .context import configure_context
from . import audio, hf, model_providers


def register_extracted_routes(
    app: FastAPI,
    *,
    http,
    track_bg=None,
    artifact_file_metadata=None,
) -> None:
    configure_context(
        http=http,
        track_bg=track_bg,
        artifact_file_metadata=artifact_file_metadata,
    )
    app.include_router(audio.router)
    app.include_router(model_providers.router)
    app.include_router(hf.router)
