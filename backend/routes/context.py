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


_context: RouteContext | None = None


def configure_context(
    *,
    http: Any,
    track_bg: Callable[[Any], Any] | None = None,
    artifact_file_metadata: Callable[..., dict] | None = None,
) -> None:
    global _context
    _context = RouteContext(
        http=http,
        track_bg=track_bg,
        artifact_file_metadata=artifact_file_metadata,
    )


def route_context() -> RouteContext:
    if _context is None:
        raise RuntimeError("Route context has not been configured")
    return _context
