"""Serve uploaded files at /static/uploads/<key>.

Replaces the old StaticFiles mount so the same public path works for both
storage backends:

* S3/MinIO  — the object is streamed back through the backend, so clients
              never talk to MinIO directly (the endpoint can stay private).
* local     — the file is read from UPLOAD_DIR off disk.

No auth: files were publicly served under this path before (frontend <img>
tags and stored *Url fields rely on it); access control stays "unguessable
UUID in the key", unchanged from the previous behaviour.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from config import is_s3_enabled
from utils import storage


router = APIRouter()


def _iter_body(body):
    """Yield chunks from a boto3 StreamingBody, closing it when done."""
    try:
        for chunk in body.iter_chunks(storage.STREAM_CHUNK):
            yield chunk
    finally:
        body.close()


@router.get("/static/uploads/{file_path:path}")
async def serve_upload(file_path: str):
    if is_s3_enabled():
        result = await run_in_threadpool(storage.open_stream, file_path)
        if result is None:
            raise HTTPException(404, "File not found")
        body, content_type, length = result
        headers = {}
        if length is not None:
            headers["Content-Length"] = str(length)
        return StreamingResponse(
            _iter_body(body),
            media_type=content_type,
            headers=headers,
        )

    # Local filesystem backend.
    try:
        path = storage.local_path_for(file_path)
    except ValueError:
        raise HTTPException(404, "File not found")
    if not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path)
