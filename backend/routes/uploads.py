"""Local file upload endpoint.

POST /uploads accepts a single file in a multipart form, writes it to
UPLOAD_DIR/<year>/<month>/<uuid>-<original-name>, and returns a URL the
frontend can store in any of the *Url fields across the app.

For production, swap the storage layer for S3/R2/GCS. The contract
(`POST` returns `{url, fileName, size, mimeType}`) stays unchanged.
"""

import os
import uuid
from pathlib import Path
from datetime import datetime

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from config import UPLOAD_DIR, MAX_UPLOAD_BYTES, PUBLIC_BASE_URL
from utils.dependencies import get_current_user


router = APIRouter()

# Ensure the uploads directory exists at import time so /static can mount it.
Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)


def _safe_filename(name: str) -> str:
    """Strip directory traversal characters from a filename."""
    base = os.path.basename(name or "file")
    # Allow letters, digits, dot, hyphen, underscore. Replace the rest.
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in base)
    return cleaned or "file"


def _public_url_for(rel_path: str) -> str:
    """Builds a publicly-fetchable URL for a stored file.

    rel_path is relative to UPLOAD_DIR (e.g. '2026/05/uuid-name.pdf').
    Returns either an absolute URL (if PUBLIC_BASE_URL is set) or a
    relative one starting with /static/uploads/ for the frontend to
    resolve against the backend origin.
    """
    rel_path = rel_path.replace("\\", "/")
    path = f"/static/uploads/{rel_path}"
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip("/") + path
    return path


@router.post("")
async def upload_file(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
):
    """Accepts a single file and stores it locally. Auth-required."""
    if not file or not file.filename:
        raise HTTPException(400, "file is required")

    today = datetime.now()
    year_dir = f"{today.year:04d}"
    month_dir = f"{today.month:02d}"
    folder = Path(UPLOAD_DIR) / year_dir / month_dir
    folder.mkdir(parents=True, exist_ok=True)

    safe_name = _safe_filename(file.filename)
    unique_name = f"{uuid.uuid4().hex}-{safe_name}"
    target = folder / unique_name

    # Stream to disk with a hard cap to avoid runaway uploads.
    written = 0
    chunk = 64 * 1024
    try:
        async with aiofiles.open(target, "wb") as out:
            while True:
                buf = await file.read(chunk)
                if not buf:
                    break
                written += len(buf)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        413,
                        f"File exceeds the {MAX_UPLOAD_BYTES} byte limit",
                    )
                await out.write(buf)
    except HTTPException:
        # Clean up partial file.
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(500, f"Failed to store file: {e}")

    rel = f"{year_dir}/{month_dir}/{unique_name}"
    return {
        "url": _public_url_for(rel),
        "fileName": safe_name,
        "size": written,
        "mimeType": file.content_type,
        "uploadedBy": user_id,
    }
