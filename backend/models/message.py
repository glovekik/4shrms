from pydantic import BaseModel
from typing import List, Optional


class Attachment(BaseModel):
    url: str
    # "image" | "file" | "voice" | "sticker"
    type: str = "file"
    name: Optional[str] = None
    mimeType: Optional[str] = None
    durationMs: Optional[int] = None  # voice notes


class MessageCreate(BaseModel):
    text: str = ""
    # Resolved user IDs the FE matched from @-mentions in `text`.
    # Optional so legacy clients keep working — when absent, no mention
    # notifications fire.
    mentions: Optional[List[str]] = None
    attachments: Optional[List[Attachment]] = None


class MessageEdit(BaseModel):
    text: str
    mentions: Optional[List[str]] = None
