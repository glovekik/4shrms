from pydantic import BaseModel
from typing import List, Optional


class MessageCreate(BaseModel):
    text: str
    # Resolved user IDs the FE matched from @-mentions in `text`.
    # Optional so legacy clients keep working — when absent, no mention
    # notifications fire.
    mentions: Optional[List[str]] = None
