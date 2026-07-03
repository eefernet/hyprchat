"""Shared artifact service helpers for routes and cleanup flows."""
from __future__ import annotations

import os

import config
import database as db
from artifact_files import artifact_path_for_row


async def annotate_artifact_staleness(artifacts: list[dict]) -> list[dict]:
    """Annotate archive artifacts with stale/latest flags."""
    if not artifacts:
        return artifacts
    edit_ts_cache: dict[str, str | None] = {}
    newest_by_project: dict[str, str] = {}
    for artifact in artifacts:
        if (artifact.get("kind") or "") != "archive":
            continue
        project_id = str(((artifact.get("metadata") or {}).get("project_id") or "")).strip()
        if not project_id:
            continue
        created_at = artifact.get("created_at") or ""
        if created_at > newest_by_project.get(project_id, ""):
            newest_by_project[project_id] = created_at
    for artifact in artifacts:
        if (artifact.get("kind") or "") != "archive":
            continue
        project_id = str(((artifact.get("metadata") or {}).get("project_id") or "")).strip()
        if not project_id:
            continue
        if project_id not in edit_ts_cache:
            try:
                edit_ts_cache[project_id] = await db.latest_edit_run_after(project_id)
            except Exception as e:
                print(f"[artifacts] stale lookup failed for {project_id}: {e}")
                edit_ts_cache[project_id] = None
        edit_ts = edit_ts_cache[project_id]
        created_at = artifact.get("created_at") or ""
        artifact["stale"] = bool(edit_ts and created_at and edit_ts > created_at)
        artifact["latest_for_project"] = bool(created_at and created_at == newest_by_project.get(project_id))
    return artifacts


async def delete_artifact_row_and_file(artifact: dict) -> dict:
    """Delete an artifact row and, when safe, its sandbox-output file."""
    artifact_id = artifact.get("id") or ""
    filepath, _ = artifact_path_for_row(artifact)
    ok = await db.delete_artifact(artifact_id)
    if not ok:
        return {"deleted": False, "deleted_file": False}
    deleted_file = False
    if filepath:
        outputs_root = os.path.abspath(config.SANDBOX_OUTPUTS_DIR) + os.sep
        in_outputs = os.path.abspath(filepath).startswith(outputs_root)
        refs = await db.count_artifacts_with_storage_path(
            artifact.get("storage_path") or "",
            exclude_id=artifact_id,
        )
        if in_outputs and refs == 0:
            try:
                os.remove(filepath)
                deleted_file = True
            except OSError as e:
                print(f"[ARTIFACT] file delete failed for {filepath}: {e}")
    return {"deleted": True, "deleted_file": deleted_file}


async def delete_artifact_files_for_user_ids(user_ids: list[str]) -> int:
    """Remove sandbox-output artifact files owned only by the given users."""
    user_set = {user_id for user_id in user_ids if user_id}
    if not user_set:
        return 0
    conn = await db.get_db()
    try:
        rows = [dict(row) for row in await conn.execute_fetchall("SELECT * FROM artifacts")]
    finally:
        await conn.close()
    outputs_root = os.path.abspath(config.SANDBOX_OUTPUTS_DIR) + os.sep
    keep_paths: set[str] = set()
    target_paths: set[str] = set()
    for artifact in rows:
        filepath, _ = artifact_path_for_row(artifact)
        if not filepath:
            continue
        abs_path = os.path.abspath(filepath)
        if not abs_path.startswith(outputs_root):
            continue
        if artifact.get("user_id") in user_set:
            target_paths.add(abs_path)
        else:
            keep_paths.add(abs_path)
    deleted = 0
    for filepath in target_paths - keep_paths:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                deleted += 1
        except OSError as e:
            print(f"[ARTIFACT] bulk file delete failed for {filepath}: {e}")
    return deleted
