"""Shared runtime objects for extracted API routers.

The first refactor phase keeps the long-lived HTTP client and small app
helpers owned by main.py while routes move into feature modules.
"""
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class RouteContext:
    http: Any
    track_bg: Callable[[Any], Any] | None = None
    artifact_file_metadata: Callable[..., dict] | None = None
    storage_health_check: Callable[[], dict] | None = None
    load_settings: Callable[[], dict] | None = None
    save_settings: Callable[[dict], None] | None = None
    public_settings_payload: Callable[[dict], dict] | None = None
    coerce_service_url: Callable[[str, str, str], str] | None = None
    sandbox_size_bytes: Callable[[], int] | None = None
    request_user_id: Callable[[Any], str] | None = None
    request_session_token: Callable[[Any], str] | None = None
    validated_request_user: Callable[[Any], Any] | None = None
    delete_artifact_files_for_user_ids: Callable[[list[str]], Any] | None = None
    delete_all_models: Callable[[], Any] | None = None


_context: RouteContext | None = None


def configure_context(
    *,
    http: Any,
    track_bg: Callable[[Any], Any] | None = None,
    artifact_file_metadata: Callable[..., dict] | None = None,
    storage_health_check: Callable[[], dict] | None = None,
    load_settings: Callable[[], dict] | None = None,
    save_settings: Callable[[dict], None] | None = None,
    public_settings_payload: Callable[[dict], dict] | None = None,
    coerce_service_url: Callable[[str, str, str], str] | None = None,
    sandbox_size_bytes: Callable[[], int] | None = None,
    request_user_id: Callable[[Any], str] | None = None,
    request_session_token: Callable[[Any], str] | None = None,
    validated_request_user: Callable[[Any], Any] | None = None,
    delete_artifact_files_for_user_ids: Callable[[list[str]], Any] | None = None,
    delete_all_models: Callable[[], Any] | None = None,
) -> None:
    global _context
    _context = RouteContext(
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


def route_context() -> RouteContext:
    if _context is None:
        raise RuntimeError("Route context has not been configured")
    return _context
