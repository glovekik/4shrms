from pydantic import BaseModel
from typing import Optional, Literal


class TimesheetEntry(BaseModel):
    date: str               # YYYY-MM-DD
    hours: float
    projectId: Optional[str] = None
    notes: Optional[str] = None
    billable: Optional[bool] = None


class TimesheetSubmit(BaseModel):
    """Employee finalizes their week. weekStart = Monday (YYYY-MM-DD).

    `entries` is optional — if omitted, backend uses the hours already
    recorded in attendance for each day of that week.
    """
    weekStart: str
    entries: Optional[list[TimesheetEntry]] = None
    note: Optional[str] = None


class TimesheetDecision(BaseModel):
    action: Literal["APPROVE", "REJECT"]
    note: Optional[str] = ""
