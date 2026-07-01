"""Object-storage abstraction for uploaded files.

The app stores user uploads (documents, avatars, reimbursement receipts,
asset photos, …) behind a single public URL shape: ``/static/uploads/<key>``.
This module is the storage layer sitting behind that shape.

Two backends, selected by ``config.STORAGE_BACKEND``:

* ``"s3"``    — any S3-compatible store (MinIO, AWS S3, Cloudflare R2). Used
                in production. Objects are written with ``put_object`` and
                streamed back with ``open_stream``. The public URL still
                points at the backend, which proxies the bytes from S3 — so
                the MinIO endpoint never has to be reachable from clients and
                the URL contract is identical to local mode.
* ``"local"`` — filesystem under ``UPLOAD_DIR``. Default for dev so a fresh
                checkout needs no MinIO running.

boto3 is synchronous; callers in async routes must wrap these functions with
``starlette.concurrency.run_in_threadpool`` (the upload/serve routes do).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

import config

logger = logging.getLogger("storage")

# boto3's default read chunk for streaming responses back to the client.
STREAM_CHUNK = 64 * 1024

# Lazily-built singleton S3 client + a latch so ensure_bucket() only runs its
# head/create round-trip once per process.
_s3_client = None
_bucket_ready = False


def _client():
    """Build (once) and return the boto3 S3 client from config."""
    global _s3_client
    if _s3_client is None:
        import boto3  # imported lazily so local-mode dev needn't have creds
        from botocore.config import Config as BotoConfig

        _s3_client = boto3.client(
            "s3",
            endpoint_url=config.S3_ENDPOINT_URL or None,
            aws_access_key_id=config.S3_ACCESS_KEY_ID,
            aws_secret_access_key=config.S3_SECRET_ACCESS_KEY,
            region_name=config.S3_REGION,
            # Path-style addressing is what MinIO expects; virtual-host style
            # (bucket.host) doesn't resolve against a bare IP / custom host.
            config=BotoConfig(s3={"addressing_style": "path"}),
        )
    return _s3_client


def ensure_bucket() -> None:
    """Create the configured bucket if it doesn't exist. Idempotent and
    cheap after the first call. No-op when S3 isn't enabled so it's safe to
    call unconditionally on startup."""
    global _bucket_ready
    if _bucket_ready or not config.is_s3_enabled():
        return

    from botocore.exceptions import ClientError

    client = _client()
    bucket = config.S3_BUCKET
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # 404 / NoSuchBucket → create it. Any other error (403, network) is
        # a real problem the caller should see.
        if code in ("404", "NoSuchBucket", "NotFound"):
            client.create_bucket(Bucket=bucket)
            logger.info("Created object-storage bucket %r", bucket)
        else:
            raise
    _bucket_ready = True


def put_object(key: str, data: bytes, content_type: Optional[str]) -> None:
    """Write ``data`` to S3 under ``key`` (relative object path)."""
    ensure_bucket()
    _client().put_object(
        Bucket=config.S3_BUCKET,
        Key=key,
        Body=data,
        ContentType=content_type or "application/octet-stream",
    )


def open_stream(
    key: str,
) -> Optional[Tuple[object, str, Optional[int]]]:
    """Open ``key`` for reading. Returns ``(body, content_type, length)``
    where ``body`` is a boto3 StreamingBody (call ``.iter_chunks()`` /
    ``.read()`` / ``.close()``), or ``None`` if the object is missing."""
    from botocore.exceptions import ClientError

    try:
        resp = _client().get_object(Bucket=config.S3_BUCKET, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return (
        resp["Body"],
        resp.get("ContentType") or "application/octet-stream",
        resp.get("ContentLength"),
    )


# ---------------------------------------------------------------------------
# Local-filesystem helpers — used when STORAGE_BACKEND != "s3". Kept here so
# the upload/serve routes stay backend-agnostic.
# ---------------------------------------------------------------------------

def local_path_for(key: str) -> Path:
    """Resolve a storage key to an absolute path under UPLOAD_DIR, guarding
    against ``..`` traversal escaping the uploads root."""
    root = Path(config.UPLOAD_DIR).resolve()
    target = (root / key).resolve()
    if root != target and root not in target.parents:
        raise ValueError("Resolved path escapes UPLOAD_DIR")
    return target
