"""Download and archive preview routes."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from artifact_files import archive_contents_for_path, resolve_download_path, artifact_path_for_row
import database as db
import os


router = APIRouter()


async def owned_download(filename):
    if os.path.basename(filename) != filename:
        raise HTTPException(400, "Invalid filename")
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT * FROM artifacts WHERE url=? ORDER BY created_at DESC",
            (f"/api/downloads/{filename}",),
        )
        for row in rows:
            if row["user_id"] == db.current_user_id():
                return artifact_path_for_row(dict(row))
        if rows or db.current_user_id() != db.DEFAULT_USER_ID:
            raise HTTPException(404, "File not found")
        # Unattributed legacy links remain available to Main for review.
        return resolve_download_path(filename)
    finally:
        await conn.close()


@router.get("/api/downloads/{filename}")
async def download_file_endpoint(filename: str):
    """Serve tool-generated files. Looks in sandbox/outputs first, falls back to legacy UPLOAD_DIR."""
    filepath, safe_name = await owned_download(filename)
    if safe_name.startswith("hc-document-"):
        # Managed documents are served only by the owner-scoped artifact endpoint.
        raise HTTPException(404, "File not found")
    if filepath:
        return FileResponse(filepath, filename=safe_name)
    return JSONResponse({"error": "File not found"}, status_code=404)


@router.get("/api/downloads/{filename}/contents")
async def archive_contents(filename: str):
    """List files inside a .tar.gz or .zip archive for preview."""
    filepath, safe_name = await owned_download(filename)
    if safe_name.startswith("hc-document-"):
        raise HTTPException(415, "Use the document preview")
    if not filepath:
        return JSONResponse({"error": "File not found"}, status_code=404)
    try:
        return archive_contents_for_path(filepath, safe_name)
    except HTTPException as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
