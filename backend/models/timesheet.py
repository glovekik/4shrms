from pydantic import BaseModel
from typing import Optional, Literal


class TimesheetEntry(BaseModel):
    """One day of a weekly timesheet.

    checkIn / checkOut / notes are all REQUIRED on a working day — that rule
    is enforced in the submit route, not here, so the API can name the exact
    dates that are incomplete instead of returning an opaque 422.

    `hours` is accepted but never trusted: the server recomputes it from
    checkIn/checkOut with the same classifier the live checkout path uses,
    so nobody can type in hours they didn't work.
    """

    date: str                             # YYYY-MM-DD
    checkIn: Optional[str] = None         # ISO 8601
    checkOut: Optional[str] = None        # ISO 8601
    hours: Optional[float] = None         # advisory only — server recomputes
    attendanceType: Optional[str] = None  # OFFICE | WFH | CLIENT | LEAVE | HOLIDAY
    projectId: Optional[str] = None
    notes: Optional[str] = None
    billable: Optional[bool] = None


class TimesheetSubmit(BaseModel):
    """Employee finalizes their week. weekStart = Monday (YYYY-MM-DD).

    `entries` is optional — if omitted, the backend falls back to whatever
    attendance already holds for that week. That only succeeds when the week
    is already complete; otherwise submit is rejected with the exact dates.
    """

    weekStart: str
    entries: Optional[list[TimesheetEntry]] = None
    note: Optional[str] = None


class TimesheetDecision(BaseModel):
    action: Literal["APPROVE", "REJECT"]
    note: Optional[str] = ""
