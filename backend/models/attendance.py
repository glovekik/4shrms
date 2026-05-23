from pydantic import BaseModel
from typing import Optional

class ManualAttendance(BaseModel):
    date: str
    checkIn: Optional[str] = None
    checkOut: Optional[str] = None
    workNotes: Optional[str] = None
    type: str = "present"  # 🔥 NEW


class AttendanceUpdate(BaseModel):
    attendanceType: str
    workNotes: Optional[str] = ""
    # ISO 8601 datetime strings, e.g. "2026-05-09T09:30:00"
    checkIn: Optional[str] = None
    checkOut: Optional[str] = None


class AttendanceCheckIn(BaseModel):
    date: str            # YYYY-MM-DD (client local date)
    attendanceType: str  # OFFICE | WFH | LEAVE | HOLIDAY
    # ISO 8601 timestamp captured on the device at the moment the user
    # tapped the button. The route records this verbatim so the saved
    # time matches the user's local moment regardless of server clock /
    # timezone. Falls back to server now() if missing (older clients).
    checkIn: Optional[str] = None
    # Required when attendanceType=OFFICE; ignored otherwise.
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class AttendanceCheckOut(BaseModel):
    date: str        # YYYY-MM-DD (client local date)
    workNotes: str   # required, must be non-empty
    # ISO 8601 timestamp from the device (same rationale as checkIn).
    checkOut: Optional[str] = None


class AttendanceManualUpsert(BaseModel):
    """Atomic upsert by (userId, date). Replaces the prior 3-call dance.

    `userId` is HR/MANAGER-only: identifies the employee being marked.
    When omitted, the route falls back to the caller's own id.
    """
    date: str            # YYYY-MM-DD
    attendanceType: str  # OFFICE | WFH | LEAVE | HOLIDAY
    checkIn: Optional[str] = None   # ISO 8601
    checkOut: Optional[str] = None  # ISO 8601
    workNotes: Optional[str] = ""
    userId: Optional[str] = None