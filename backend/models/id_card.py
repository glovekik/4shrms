from pydantic import BaseModel, Field
from typing import Optional


class IDCardPhotoSubmit(BaseModel):
    """Employee submits (or re-submits) the photo for their ID card."""
    photoUrl: str


class IDCardReject(BaseModel):
    """HR rejects a submitted photo, ideally with a reason the employee sees."""
    reason: Optional[str] = None


class IDCardFraming(BaseModel):
    """How the photo is positioned inside the card's fixed 3:4 frame.

    Needed because expo-image-picker has no cropper on web at all, so a web
    upload arrives raw and would otherwise just be centre-cropped by the frame.
    Bounds are enforced here so a client can't push the photo out of view.
    """
    zoom: float = Field(default=1.0, ge=1.0, le=3.0)
    offsetX: float = Field(default=0.0, ge=-600.0, le=600.0)
    offsetY: float = Field(default=0.0, ge=-600.0, le=600.0)
