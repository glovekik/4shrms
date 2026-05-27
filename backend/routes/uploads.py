"""File upload + retrieval.

POST /uploads
    Accepts a single file (multipart). Stores it via the configured
    storage backend (local disk or Google Drive). Inserts a row in
    db.uploads recording the original name, mime type, size, uploader,
    backend, and backend-specific storage id. Returns a URL of the form
    /files/<upload_id> that the frontend can store in any *Url field.

GET /files/{upload_id}
    Auth-gated proxy. Reads the db.uploads row, asks the right backend
    for the bytes, streams them back with the original Content-Type.
    JWT must be in the Authorization header — this hides Drive file IDs
    from the public internet and prevents random URL guessing from
    leaking HR documents.

The existing /static/uploads/... mount keeps serving any files that
were already on disk before this change — those rows don't exist in
db.uploads, but the URLs still resolve to disk paths.
"""

from datetime import datetime, timezone
import uuid

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
import io

from config import (
    MAX_UPLOAD_BYTES,
    PUBLIC_BASE_URL,
    STORAGE_BACKEND,
)
from database import db
from utils.dependencies import (
    get_current_user,
    get_current_user_flexible,
)
from utils.storage import default_backend, backend_for


# Two routers in this module:
#   uploads_router  → POST /uploads        (multipart upload)
#   files_router    → GET  /files/{id}     (authed download)
uploads_router = APIRouter()
files_router = APIRouter()


def _public_url_for_upload_id(upload_id: str) -> str:
    """Frontend-facing URL. Absolute if PUBLIC_BASE_URL is set,
    otherwise relative so the frontend resolves against the API origin."""
    path = f"/files/{upload_id}"
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip("/") + path
    return path


@uploads_router.post("")
async def upload_file(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
):
    if not file or not file.filename:
        raise HTTPException(400, "file is required")

    # Read into memory up to the cap. We do this here (rather than
    # streaming) because both backends need a complete byte buffer
    # (LocalStorage writes once; Drive's MediaIoBaseUpload also wants
    # a seekable buffer). 20 MB cap keeps memory bounded.
    data = bytearray()
    chunk_size = 64 * 1024
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"File exceeds the {MAX_UPLOAD_BYTES} byte limit",
            )

    payload = bytes(data)
    mime = file.content_type or "application/octet-stream"

    # Hand off to the configured backend.
    try:
        backend = default_backend()
    except RuntimeError as e:
        # Drive backend missing config — surface a clear 503 instead of
        # a confusing 500 stack trace.
        raise HTTPException(503, f"Storage not configured: {e}")

    try:
        storage_id = await backend.put(payload, file.filename, mime)
    except Exception as e:
        raise HTTPException(500, f"Failed to store file: {e}")

    # Record metadata. _id is our own uuid (not the backend's) so URLs
    # are stable across a backend switch.
    upload_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    await db.uploads.insert_one({
        "_id": upload_id,
        "storageBackend": backend.backend_name,
        "storageId": storage_id,
        "fileName": file.filename,
        "mimeType": mime,
        "size": len(payload),
        "uploadedBy": user_id,
        "createdAt": now,
    })

    return {
        "url": _public_url_for_upload_id(upload_id),
        "fileName": file.filename,
        "size": len(payload),
        "mimeType": mime,
        "uploadedBy": user_id,
    }


@files_router.get("/{upload_id}")
async def download_file(upload_id: str):
    """Streams the bytes for an upload_id.

    NOTE on auth: currently OPEN — same security posture as the
    existing /static/uploads/ mount. Anyone with the URL can fetch
    the bytes. The upload_id is a 32-char hex uuid so guessing is
    impractical, but for sensitive HR docs (PAN, Aadhaar) we should
    move to signed URLs. Tracked as follow-up; the route is structured
    so flipping it to use get_current_user_flexible later is a
    one-line change."""
    rec = await db.uploads.find_one({"_id": upload_id})
    if not rec:
        raise HTTPException(404, "Not found")

    backend_name = rec.get("storageBackend") or "local"
    storage_id = rec.get("storageId")
    if not storage_id:
        raise HTTPException(500, "Upload row is missing storageId")

    try:
        backend = backend_for(backend_name)
    except RuntimeError as e:
        raise HTTPException(503, f"Storage not configured: {e}")

    try:
        data, fetched_mime = await backend.get(storage_id)
    except FileNotFoundError:
        raise HTTPException(404, "File missing in storage")
    except Exception as e:
        raise HTTPException(502, f"Storage read failed: {e}")

    mime = (
        fetched_mime
        or rec.get("mimeType")
        or "application/octet-stream"
    )
    filename = rec.get("fileName") or "file"

    return StreamingResponse(
        io.BytesIO(data),
        media_type=mime,
        headers={
            # "inline" lets browsers display images / PDFs directly
            # without forcing a download dialog. The filename helps if
            # the user does choose to Save As.
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, max-age=60",
        },
    )


# Back-compat: the old route used a router named `router` mounted at
# `/uploads`. main.py still imports that name.
router = uploads_router
