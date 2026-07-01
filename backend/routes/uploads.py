"""File upload endpoint.

POST /uploads accepts a single file in a multipart form and stores it under
``<year>/<month>/<uuid>-<original-name>``. The storage backend is chosen by
config (S3/MinIO in production, local disk in dev — see utils/storage.py),
but the response contract is identical either way:

    { url, fileName, size, mimeType, uploadedBy }

The returned ``url`` is always ``/static/uploads/<key>`` (optionally prefixed
with PUBLIC_BASE_URL), which the frontend stores in any of the *Url fields
across the app and which the backend serves back via the same path.
"""

import os
import uuid
from pathlib import Path
from datetime import datetime

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from starlette.concurrency import run_in_threadpool

from config import (
    UPLOAD_DIR,
    MAX_UPLOAD_BYTES,
    PUBLIC_BASE_URL,
    is_s3_enabled,
)
from utils import storage
from utils.dependencies import get_current_user


router = APIRouter()


def _safe_filename(name: str) -> str:
    """Strip directory traversal characters from a filename."""
    base = os.path.basename(name or "file")
    # Allow letters, digits, dot, hyphen, underscore. Replace the rest.
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in base)
    return cleaned or "file"


def _public_url_for(rel_path: str) -> str:
    """Builds a publicly-fetchable URL for a stored file.

    rel_path is the storage key (e.g. '2026/05/uuid-name.pdf'). Returns
    either an absolute URL (if PUBLIC_BASE_URL is set) or a relative one
    starting with /static/uploads/ for the frontend to resolve against the
    backend origin. Identical for both storage backends.
    """
    rel_path = rel_path.replace("\\", "/")
    path = f"/static/uploads/{rel_path}"
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip("/") + path
    return path


async def _read_capped(file: UploadFile) -> bytes:
    """Read the whole upload into memory, enforcing MAX_UPLOAD_BYTES.

    Uploads are capped at 20 MB, so buffering in memory is fine and lets us
    hand a single bytes object to either storage backend.
    """
    buf = bytearray()
    chunk = 64 * 1024
    while True:
        part = await file.read(chunk)
        if not part:
            break
        buf.extend(part)
        if len(buf) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413,
                f"File exceeds the {MAX_UPLOAD_BYTES} byte limit",
            )
    return bytes(buf)


@router.post("")
async def upload_file(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
):
    """Accepts a single file and stores it. Auth-required."""
    if not file or not file.filename:
        raise HTTPException(400, "file is required")

    today = datetime.now()
    year_dir = f"{today.year:04d}"
    month_dir = f"{today.month:02d}"

    safe_name = _safe_filename(file.filename)
    unique_name = f"{uuid.uuid4().hex}-{safe_name}"
    key = f"{year_dir}/{month_dir}/{unique_name}"

    data = await _read_capped(file)

    try:
        if is_s3_enabled():
            # boto3 is blocking — keep the event loop free.
            await run_in_threadpool(
                storage.put_object, key, data, file.content_type
            )
        else:
            folder = Path(UPLOAD_DIR) / year_dir / month_dir
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / unique_name
            async with aiofiles.open(target, "wb") as out:
                await out.write(data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to store file: {e}")

    return {
        "url": _public_url_for(key),
        "fileName": safe_name,
        "size": len(data),
        "mimeType": file.content_type,
        "uploadedBy": user_id,
    }
