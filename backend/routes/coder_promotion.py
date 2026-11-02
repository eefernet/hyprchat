"""Coding project versions: list them and let the owner accept an exact delivered revision."""
from pydantic import BaseModel, Field

from fastapi import APIRouter, HTTPException

import database as db
from db import coder_promotion


router = APIRouter()


class AcceptRevision(BaseModel):
    revision_id: str = Field(min_length=7, max_length=64)


@router.get("/api/coder/projects/{project_id}/versions")
async def project_versions(project_id: str):
    project = await db.get_coding_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return {"project_id": project_id,
            "working": {"job_id": project.get("working_job_id") or "", "revision_id": project.get("working_revision_id") or "",
                        "artifact_id": project.get("working_artifact_id") or ""},
            "accepted": {"job_id": project.get("accepted_job_id") or "", "revision_id": project.get("accepted_revision_id") or "",
                         "artifact_id": project.get("accepted_artifact_id") or ""},
            "promotions": await coder_promotion.promotions(project_id)}


@router.post("/api/coder/projects/{project_id}/accept")
async def accept_revision(project_id: str, request: AcceptRevision):
    try:
        return await coder_promotion.accept(project_id, request.revision_id)
    except LookupError as error:
        raise HTTPException(404, str(error))
    except ValueError as error:
        raise HTTPException(409, str(error))
