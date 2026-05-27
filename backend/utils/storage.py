"""File-storage abstraction.

Two implementations:
  - LocalStorageBackend   → bytes on the container disk (UPLOAD_DIR).
                            Convenient for dev; lost on every Render
                            redeploy because that filesystem is ephemeral.
  - DriveStorageBackend   → bytes uploaded to a Google Drive folder via a
                            service account. Survives redeploys because
                            the data lives in Google's cloud, not the
                            backend container.

The /uploads route reads STORAGE_BACKEND from config and calls
get_storage_backend(). Each backend exposes:
  - put(data, name, mime)   -> opaque storage_id
  - get(storage_id)         -> (bytes, mimetype)
  - delete(storage_id)      -> None  (best-effort)

The "storage_id" is provider-specific (a relative path for local, a
Drive file ID for drive). The /files/{id} route mediates: the public ID
in URLs is a uuid in db.uploads; that row carries the storageBackend
+ storageId so we know how to fetch even if the default backend
changed since the file was uploaded.
"""

import asyncio
import io
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from config import (
    GOOGLE_DRIVE_FOLDER_ID,
    GOOGLE_SERVICE_ACCOUNT_JSON,
    STORAGE_BACKEND,
    UPLOAD_DIR,
)


def _safe_name(name: str) -> str:
    base = name.split("/")[-1].split("\\")[-1] or "file"
    return "".join(
        c if c.isalnum() or c in "._-" else "_" for c in base
    ) or "file"


# ================= LOCAL =================
class LocalStorageBackend:
    """Writes to UPLOAD_DIR/<year>/<month>/<uuid>-name and returns the
    relative path as the storage id."""

    backend_name = "local"

    def __init__(self) -> None:
        Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)

    async def put(
        self, data: bytes, original_name: str, mimetype: Optional[str]
    ) -> str:
        today = datetime.now()
        y = f"{today.year:04d}"
        m = f"{today.month:02d}"
        folder = Path(UPLOAD_DIR) / y / m
        folder.mkdir(parents=True, exist_ok=True)
        unique = f"{uuid.uuid4().hex}-{_safe_name(original_name)}"
        target = folder / unique

        # Run blocking write off the event loop.
        await asyncio.to_thread(target.write_bytes, data)
        return f"{y}/{m}/{unique}"

    async def get(self, storage_id: str) -> Tuple[bytes, str]:
        path = Path(UPLOAD_DIR) / storage_id
        if not path.exists():
            raise FileNotFoundError(storage_id)
        data = await asyncio.to_thread(path.read_bytes)
        return data, ""

    async def delete(self, storage_id: str) -> None:
        try:
            await asyncio.to_thread(
                lambda: (Path(UPLOAD_DIR) / storage_id).unlink(missing_ok=True),
            )
        except Exception:
            pass


# ================= GOOGLE DRIVE =================
class DriveStorageBackend:
    """Uploads via a service account into GOOGLE_DRIVE_FOLDER_ID.

    Scopes: drive.file is enough — the service account can only touch
    files it created (or files explicitly shared with it). The folder we
    upload into MUST be shared with the service-account email.

    All Google client calls are sync; we shove them into asyncio.to_thread
    so we don't block uvicorn's event loop.
    """

    backend_name = "drive"

    def __init__(self) -> None:
        if not GOOGLE_SERVICE_ACCOUNT_JSON:
            raise RuntimeError(
                "STORAGE_BACKEND=drive but GOOGLE_SERVICE_ACCOUNT_JSON is not set"
            )
        if not GOOGLE_DRIVE_FOLDER_ID:
            raise RuntimeError(
                "STORAGE_BACKEND=drive but GOOGLE_DRIVE_FOLDER_ID is not set"
            )

        # Imports are local so the backend module can still be imported
        # for the local-only path even if google libs aren't installed.
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        try:
            info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}"
            )

        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        # cache_discovery=False silences the noisy file-cache warning
        # on every cold start when running under uvicorn.
        self._service = build(
            "drive", "v3", credentials=creds, cache_discovery=False,
        )

    # ---------- put ----------
    def _put_sync(
        self, data: bytes, name: str, mimetype: str
    ) -> str:
        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(
            io.BytesIO(data),
            mimetype=mimetype or "application/octet-stream",
            resumable=False,
        )
        result = (
            self._service.files()
            .create(
                body={
                    "name": name,
                    "parents": [GOOGLE_DRIVE_FOLDER_ID],
                },
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        return result["id"]

    async def put(
        self, data: bytes, original_name: str, mimetype: Optional[str]
    ) -> str:
        return await asyncio.to_thread(
            self._put_sync,
            data,
            _safe_name(original_name),
            mimetype or "application/octet-stream",
        )

    # ---------- get ----------
    def _get_sync(self, storage_id: str) -> Tuple[bytes, str]:
        from googleapiclient.http import MediaIoBaseDownload

        meta = (
            self._service.files()
            .get(
                fileId=storage_id,
                fields="mimeType",
                supportsAllDrives=True,
            )
            .execute()
        )
        mime = meta.get("mimeType") or "application/octet-stream"

        request = self._service.files().get_media(
            fileId=storage_id, supportsAllDrives=True,
        )
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue(), mime

    async def get(self, storage_id: str) -> Tuple[bytes, str]:
        return await asyncio.to_thread(self._get_sync, storage_id)

    # ---------- delete ----------
    def _delete_sync(self, storage_id: str) -> None:
        self._service.files().delete(
            fileId=storage_id, supportsAllDrives=True,
        ).execute()

    async def delete(self, storage_id: str) -> None:
        try:
            await asyncio.to_thread(self._delete_sync, storage_id)
        except Exception:
            # Best-effort — if Drive 404s we don't fail the caller.
            pass


# ================= FACTORY =================
_local: Optional[LocalStorageBackend] = None
_drive: Optional[DriveStorageBackend] = None


def _local_backend() -> LocalStorageBackend:
    global _local
    if _local is None:
        _local = LocalStorageBackend()
    return _local


def _drive_backend() -> DriveStorageBackend:
    global _drive
    if _drive is None:
        _drive = DriveStorageBackend()
    return _drive


def backend_for(name: str):
    """Return the storage backend that owns files with the given
    storageBackend tag stored in db.uploads. Lets us serve old local
    files even after the default flips to drive, and vice versa."""
    if (name or "").lower() == "drive":
        return _drive_backend()
    return _local_backend()


def default_backend():
    """The storage backend NEW uploads land in (driven by STORAGE_BACKEND
    env var). All other code paths should use backend_for(name) using
    the value recorded on the row."""
    return backend_for(STORAGE_BACKEND)
