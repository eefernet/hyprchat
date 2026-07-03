"""Download and archive preview routes."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from artifact_files import archive_contents_for_path, resolve_download_path


router = APIRouter()


@router.get("/api/downloads/{filename}")
async def download_file_endpoint(filename: str):
    """Serve tool-generated files. Looks in sandbox/outputs first, falls back to legacy UPLOAD_DIR."""
    filepath, safe_name = resolve_download_path(filename)
    if filepath:
        return FileResponse(filepath, filename=safe_name)
    return JSONResponse({"error": "File not found"}, status_code=404)


@router.get("/api/downloads/{filename}/contents")
async def archive_contents(filename: str):
    """List files inside a .tar.gz or .zip archive for preview."""
    filepath, safe_name = resolve_download_path(filename)
    if not filepath:
        return JSONResponse({"error": "File not found"}, status_code=404)
    try:
        return archive_contents_for_path(filepath, safe_name)
    except HTTPException as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
