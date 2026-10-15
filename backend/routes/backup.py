"""Backup export / staged-restore routes."""
import asyncio
import os
import tempfile

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

import backup as backup_svc

router = APIRouter()


@router.get("/api/backup/export")
async def export_backup():
    """Build and stream a full data backup; the archive is deleted after send."""
    try:
        path = await asyncio.to_thread(backup_svc.create_backup_archive)
    except Exception as e:
        raise HTTPException(500, f"Backup failed: {e}")

    def _cleanup():
        try:
            os.remove(path)
        except OSError:
            pass

    return FileResponse(
        path,
        filename=os.path.basename(path),
        media_type="application/gzip",
        background=BackgroundTask(_cleanup),
    )


@router.post("/api/backup/restore")
async def restore_backup(file: UploadFile = File(...)):
    """Stage an uploaded backup for restore at next service restart.

    Never applies in-process — SQLite is live; apply_pending_restore runs at
    lifespan startup before init_db."""
    os.makedirs(backup_svc.BACKUP_DIR, exist_ok=True)
    fd, upload_path = tempfile.mkstemp(prefix="restore-upload-", suffix=".tar.gz", dir=backup_svc.BACKUP_DIR)
    os.close(fd)
    uploaded = 0
    try:
        with open(upload_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                uploaded += len(chunk)
                if uploaded > backup_svc.MAX_ARCHIVE_BYTES:
                    raise ValueError("Backup upload exceeds size limit")
                # Disk write off the event loop — a multi-GB restore upload on
                # slow storage otherwise stalls every other request.
                await asyncio.to_thread(f.write, chunk)
        manifest = await asyncio.to_thread(backup_svc.stage_restore, upload_path)
    except ValueError as e:
        raise HTTPException(400, f"Invalid backup archive: {e}")
    except Exception as e:
        raise HTTPException(500, f"Restore staging failed: {e}")
    finally:
        try:
            os.remove(upload_path)
        except OSError:
            pass
    return {"staged": True, "restart_required": True, "manifest": manifest}


@router.delete("/api/backup/restore")
async def cancel_staged_restore():
    """Cancel a staged restore before the restart applies it."""
    try:
        await asyncio.to_thread(backup_svc.cancel_staged_restore)
    except FileNotFoundError:
        raise HTTPException(404, "No restore is staged")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@router.get("/api/backup/status")
async def get_backup_status():
    return backup_svc.backup_status()
