"""User/session and local reset routes."""
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import database as db

from .context import route_context


router = APIRouter()

# ── Login rate limiting (in-process, fixed window) ──
# 5 failures per 300s per (user_id, client ip). Cleared on success. The map is
# capped so a scanner cycling ids/ips can't grow it unbounded. Deliberately
# NOT applied to validate_user_session (every request validates).
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_FAILURES = 5
_LOGIN_FAILURES_CAP = 512
_LOGIN_FAILURES: dict[tuple[str, str], tuple[int, float]] = {}  # key -> (count, window_start)


def _login_rate_key(user_id: str, request: Request) -> tuple[str, str]:
    ip = (request.client.host if request.client else "") or "unknown"
    return (str(user_id or ""), ip)


def _login_rate_retry_after(key: tuple[str, str]) -> int | None:
    """Seconds until the window resets when limited, else None."""
    entry = _LOGIN_FAILURES.get(key)
    if not entry:
        return None
    count, start = entry
    now = time.time()
    if now - start >= _LOGIN_WINDOW_SECONDS:
        _LOGIN_FAILURES.pop(key, None)
        return None
    if count >= _LOGIN_MAX_FAILURES:
        return max(1, int(_LOGIN_WINDOW_SECONDS - (now - start)))
    return None


def _login_rate_record_failure(key: tuple[str, str]) -> None:
    now = time.time()
    count, start = _LOGIN_FAILURES.get(key, (0, now))
    if now - start >= _LOGIN_WINDOW_SECONDS:
        count, start = 0, now
    _LOGIN_FAILURES[key] = (count + 1, start)
    if len(_LOGIN_FAILURES) > _LOGIN_FAILURES_CAP:
        cutoff = now - _LOGIN_WINDOW_SECONDS
        for stale in [k for k, (_c, s) in _LOGIN_FAILURES.items() if s < cutoff]:
            _LOGIN_FAILURES.pop(stale, None)
        while len(_LOGIN_FAILURES) > _LOGIN_FAILURES_CAP:
            _LOGIN_FAILURES.pop(next(iter(_LOGIN_FAILURES)), None)


class UserCreate(BaseModel):
    name: str
    password: Optional[str] = ""


class UserLogin(BaseModel):
    user_id: str
    password: Optional[str] = ""


class UserUpdate(BaseModel):
    name: Optional[str] = None
    password: Optional[str] = None
    clear_password: Optional[bool] = False


async def _validated_request_user(request: Request) -> dict:
    return await route_context().validated_request_user(request)


async def _require_profile_access(request: Request, target_id: str) -> dict:
    """The caller must be the target profile itself, or Main (the admin)."""
    current = await _validated_request_user(request)
    if current["id"] != target_id and current["id"] != db.DEFAULT_USER_ID:
        raise HTTPException(403, "You can only manage your own profile")
    return current


async def _require_main_user(request: Request) -> dict:
    current = await _validated_request_user(request)
    if current["id"] != db.DEFAULT_USER_ID:
        raise HTTPException(403, "Only the Main profile can do this")
    return current


def _request_user_id(request: Request) -> str:
    return route_context().request_user_id(request)


def _request_session_token(request: Request) -> str:
    return route_context().request_session_token(request)


async def _delete_artifact_files_for_user_ids(user_ids: list[str]) -> int:
    return await route_context().delete_artifact_files_for_user_ids(user_ids)


async def _delete_all_models() -> dict:
    return await route_context().delete_all_models()


@router.get("/api/users")
async def list_users_ep():
    return {"users": await db.list_users()}


@router.get("/api/users/current")
async def current_user_ep(request: Request):
    user = await _validated_request_user(request)
    return {"user": await db.get_user(user["id"])}


@router.post("/api/users")
async def create_user_ep(req: UserCreate):
    user = await db.create_user(req.name, req.password or "")
    return {"user": user}


@router.post("/api/users/login")
async def login_user_ep(req: UserLogin, request: Request):
    key = _login_rate_key(req.user_id, request)
    retry_after = _login_rate_retry_after(key)
    if retry_after is not None:
        raise HTTPException(
            429, "Too many failed logins; try again later",
            headers={"Retry-After": str(retry_after)},
        )
    user, session_token = await db.login_user(req.user_id, req.password or "")
    if not user:
        _login_rate_record_failure(key)
        raise HTTPException(401, "Invalid user or password")
    _LOGIN_FAILURES.pop(key, None)
    return {"user": user, "session_token": session_token}


@router.post("/api/users/logout")
async def logout_user_ep(request: Request):
    user_id = _request_user_id(request)
    await db.logout_user_session(user_id, _request_session_token(request) or None)
    return {"ok": True}


@router.patch("/api/users/{user_id}")
async def update_user_ep(user_id: str, req: UserUpdate, request: Request):
    current = await _require_profile_access(request, user_id)
    user, _ = await db.update_user(
        user_id,
        name=req.name,
        password=req.password,
        clear_password=bool(req.clear_password),
    )
    if not user:
        raise HTTPException(404, "User not found")
    session_token = None
    if user_id == current["id"] and req.password is not None and not req.clear_password and req.password:
        session_token = await db.create_user_session(user_id)
    return {"user": user, "session_token": session_token}


@router.delete("/api/users/{user_id}")
async def delete_user_ep(user_id: str, request: Request):
    await _require_profile_access(request, user_id)
    if user_id == db.DEFAULT_USER_ID:
        raise HTTPException(400, "The Main user cannot be deleted")
    ok = await db.delete_user(user_id)
    if not ok:
        raise HTTPException(404, "User not found")
    return {"ok": True}


async def _user_ids(exclude_user_id: str | None = None) -> list[str]:
    conn = await db.get_db()
    try:
        if exclude_user_id:
            rows = await conn.execute_fetchall("SELECT id FROM users WHERE id != ?", (exclude_user_id,))
        else:
            rows = await conn.execute_fetchall("SELECT id FROM users")
        return [r["id"] for r in rows]
    finally:
        await conn.close()


@router.delete("/api/users")
async def delete_other_users_ep(request: Request):
    current = await _require_main_user(request)
    other_user_ids = await _user_ids(exclude_user_id=current["id"])
    deleted_files = await _delete_artifact_files_for_user_ids(other_user_ids)
    result = await db.delete_users_except(current["id"])
    try:
        await db.vacuum_database()
    except Exception as e:
        print(f"[USERS] vacuum after delete-other-users failed: {e}")
    return {"ok": True, **result, "deleted_files": deleted_files}


@router.post("/api/danger-zone/fresh-install")
async def fresh_install_reset_ep(request: Request):
    await _require_main_user(request)
    user_ids = await _user_ids()
    deleted_files = await _delete_artifact_files_for_user_ids(user_ids)
    try:
        model_result = await _delete_all_models()
    except HTTPException as e:
        model_result = {"status": "failed", "error": str(e.detail), "deleted": 0, "models": [], "failed": []}
    except Exception as e:
        model_result = {"status": "failed", "error": str(e), "deleted": 0, "models": [], "failed": []}
    user_result = await db.delete_all_users_and_data()
    try:
        await db.vacuum_database()
    except Exception as e:
        print(f"[DANGER] fresh-install vacuum failed: {e}")
    return {
        "ok": True,
        "status": "reset",
        "requires_user_setup": True,
        "users_deleted": user_result.get("deleted", 0),
        "artifact_files_deleted": deleted_files,
        "models": model_result,
    }
