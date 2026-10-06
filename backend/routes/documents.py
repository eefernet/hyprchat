"""Document upload, job submission and owner-scoped binary delivery."""
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

import documents
from .context import route_context

router = APIRouter()


class DocumentJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    conversation_id: str
    artifact_id: str = ""
    expected_sha256: str = ""
    filename: str = ""
    format: str = ""
    content: dict = {}
    markdown: str = ""
    operations: list[dict] = []
    offset: int = 0
    limit: int = 100
    sheet: str = ""
    range: str = ""


@router.get("/api/documents/health")
async def health():
    try:
        return {"enabled": documents.enabled(), **await documents.worker(route_context().http, "health")}
    except Exception as e:
        return {"enabled": documents.enabled(), "ready": False, "error": str(e)}


@router.post("/api/documents/upload")
async def upload(file: UploadFile = File(...), conversation_id: str = Form(...)):
    return await documents.upload_document(route_context().http, file, conversation_id)


@router.post("/api/documents/jobs", status_code=202)
async def job(body: DocumentJob):
    return await documents.submit(route_context().http, body.model_dump(exclude_defaults=True))


@router.get("/api/documents/files/{artifact_id}")
async def download(artifact_id: str):
    artifact, path = await documents.owned_source(artifact_id)
    return FileResponse(path, filename=artifact["filename"], media_type=artifact["mime_type"],
                        content_disposition_type="inline" if artifact["kind"] in {"pdf", "image"} else "attachment")
