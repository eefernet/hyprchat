"""User/session and local reset routes."""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import database as db

from .context import route_context


router = APIRouter()


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
async def login_user_ep(req: UserLogin):
    user, session_token = await db.login_user(req.user_id, req.password or "")
    if not user:
        raise HTTPException(401, "Invalid user or password")
    return {"user": user, "session_token": session_token}


@router.post("/api/users/logout")
async def logout_user_ep(request: Request):
    user_id = _request_user_id(request)
    await db.logout_user_session(user_id, _request_session_token(request) or None)
    return {"ok": True}


@router.patch("/api/users/{user_id}")
async def update_user_ep(user_id: str, req: UserUpdate, request: Request):
    current = await _validated_request_user(request)
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
    await _validated_request_user(request)
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
    current = await _validated_request_user(request)
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
    await _validated_request_user(request)
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
