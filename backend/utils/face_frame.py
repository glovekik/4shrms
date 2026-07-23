"""Auto-compose an ID card photo.

Finds the face in an uploaded photo and works out the zoom/offset that places
it to badge standard — not merely centred, but composed: face roughly 45% of
the frame height, sitting about 40% down, horizontally centred. That's the
convention passport and ID photos follow, and it's what stops a badge looking
like a casual snapshot.

Detection runs on OUR server with OpenCV's bundled Haar cascade. Deliberately
no third-party vision API: these are employee face photos, and shipping
biometric data to an outside service for a cosmetic crop isn't a trade worth
making.

Everything degrades safely — if OpenCV isn't installed, the image can't be
read, or no face is found, this returns None and the card just uses the
default framing, which the employee can still adjust by hand.
"""

from typing import Optional
from pathlib import Path

import config
from utils import storage

# Card frame geometry — MUST stay in sync with src/components/IDCard.tsx
# (PHOTO_FRAME_W / PHOTO_FRAME_H, both PHOTO_SIZE — the frame is square).
FRAME_W = 190.0
FRAME_H = 190.0

ZOOM_MIN = 1.0
ZOOM_MAX = 3.0

# Badge composition targets.
TARGET_FACE_H = 0.45   # face-box height as a fraction of the frame height
TARGET_FACE_Y = 0.40   # where the face centre sits, measured from the top

DEFAULT_FRAMING = {"zoom": 1.0, "offsetX": 0.0, "offsetY": 0.0}

# Optional dependency — absent installs simply skip auto-framing.
try:  # pragma: no cover - depends on the deployed image
    import cv2
    import numpy as np
except Exception:  # pragma: no cover
    cv2 = None
    np = None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def key_from_url(url: str) -> Optional[str]:
    """Reverse uploads._public_url_for: a stored URL back into its storage key."""
    if not url:
        return None
    u = url.strip()
    base = getattr(config, "PUBLIC_BASE_URL", "") or ""
    if base and u.startswith(base):
        u = u[len(base):]
    marker = "/static/uploads/"
    idx = u.find(marker)
    if idx == -1:
        return None
    return u[idx + len(marker):].lstrip("/") or None


def read_upload_bytes(url: str) -> Optional[bytes]:
    """Read a previously-uploaded file back, from local disk or S3/MinIO."""
    key = key_from_url(url)
    if not key:
        return None

    # Local-disk mode first — cheaper than a round trip when it's right there.
    try:
        p: Path = storage.local_path_for(key)
        if p and p.exists():
            return p.read_bytes()
    except Exception:
        pass

    try:
        opened = storage.open_stream(key)
        if not opened:
            return None
        body = opened[0]
        try:
            return body.read()
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
    except Exception:
        return None


def _largest_face(image_bytes: bytes):
    """Return (x, y, w, h, nat_w, nat_h) for the biggest detected face."""
    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None

    nat_h, nat_w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)

    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    if cascade.empty():
        return None

    min_side = max(24, int(min(nat_w, nat_h) * 0.08))
    faces = cascade.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(min_side, min_side),
    )
    if faces is None or len(faces) == 0:
        return None

    x, y, w, h = max(faces, key=lambda f: int(f[2]) * int(f[3]))
    return int(x), int(y), int(w), int(h), nat_w, nat_h


def compute_auto_framing(image_bytes: bytes) -> Optional[dict]:
    """Zoom + offsets that compose the detected face to badge standard.

    Mirrors exactly how the card renders the photo: the image is cover-fitted
    into the frame, then transformed as translate(scale(p)) — so the offsets
    are in final (post-scale) card pixels, matching the client.
    """
    if cv2 is None or np is None or not image_bytes:
        return None

    try:
        found = _largest_face(image_bytes)
    except Exception:
        return None
    if not found:
        return None

    x, y, w, h, nat_w, nat_h = found
    if not nat_w or not nat_h or not h:
        return None

    # How the frame cover-fits the image before any user transform.
    cover = max(FRAME_W / nat_w, FRAME_H / nat_h)

    # Zoom so the face occupies the target share of the frame height.
    zoom = _clamp((TARGET_FACE_H * FRAME_H / h) / cover, ZOOM_MIN, ZOOM_MAX)

    # Where the face centre currently lands, relative to the frame centre.
    face_cx = x + w / 2.0
    face_cy = y + h / 2.0
    ux = (face_cx - nat_w / 2.0) * cover * zoom
    uy = (face_cy - nat_h / 2.0) * cover * zoom

    # Translate so it lands centred horizontally and TARGET_FACE_Y down.
    tx = -ux
    ty = (TARGET_FACE_Y * FRAME_H) - (FRAME_H / 2.0) - uy

    # Never expose blank edges.
    max_x = max(0.0, (nat_w * cover * zoom - FRAME_W) / 2.0)
    max_y = max(0.0, (nat_h * cover * zoom - FRAME_H) / 2.0)

    return {
        "zoom": round(zoom, 4),
        "offsetX": round(_clamp(tx, -max_x, max_x), 2),
        "offsetY": round(_clamp(ty, -max_y, max_y), 2),
    }


def auto_framing_for_url(url: str) -> Optional[dict]:
    """Convenience: read a stored photo and compose it. None if not possible."""
    data = read_upload_bytes(url)
    if not data:
        return None
    return compute_auto_framing(data)
