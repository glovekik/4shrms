from pydantic import BaseModel
from typing import Literal, Optional


class ManualAttendanceCreate(BaseModel):
    """Employee submits a request for HR/manager to add an attendance row
    on their behalf. checkIn/checkOut are ISO 8601 timestamps."""
    date: str             # YYYY-MM-DD
    checkIn: str          # ISO 8601 datetime
    checkOut: Optional[str] = None
    reason: str


class ManualAttendanceDecision(BaseModel):
    action: Literal["APPROVE", "REJECT"]
    note: Optional[str] = None
