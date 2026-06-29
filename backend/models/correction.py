from pydantic import BaseModel
from typing import Optional, Literal, List


AttendanceTypeLiteral = Literal["OFFICE", "WFH", "LEAVE", "HOLIDAY"]


class CorrectionRequestCreate(BaseModel):
    """Employee-submitted correction to one attendance row.

    Any subset of fields can be requested — the route validates that at
    least ONE editable field is provided alongside the (required) reason.
    `requestedCheckOut` is kept for backward compatibility with older
    clients; new clients can set any combination.
    """
    reason: str
    # New fields — every editable attendance field is exposed.
    requestedDate: Optional[str] = None            # YYYY-MM-DD
    requestedCheckIn: Optional[str] = None         # ISO 8601
    requestedCheckOut: Optional[str] = None        # ISO 8601
    requestedAttendanceType: Optional[AttendanceTypeLiteral] = None
    requestedWorkNotes: Optional[str] = None


class CorrectionDecision(BaseModel):
    action: Literal["APPROVE", "REJECT"]
    note: Optional[str] = ""
    # On APPROVE, HR/Manager can override each requested field before
    # stamping. Anything omitted falls back to what the user requested
    # (or, if the user didn't request that field, leaves it unchanged).
    overrideDate: Optional[str] = None
    overrideCheckIn: Optional[str] = None
    overrideCheckOut: Optional[str] = None
    overrideAttendanceType: Optional[AttendanceTypeLiteral] = None
    overrideWorkNotes: Optional[str] = None


class CorrectionBulkDecision(BaseModel):
    """Approve/reject many correction requests in one call.

    `ids` is the explicit list of correction-request ids to act on
    (used for both "approve selected" and "approve all" — the client
    passes whichever set it wants). Field overrides are intentionally
    NOT supported in bulk: each request keeps whatever the employee
    asked for. A per-item result list is returned so the UI can report
    partial success.
    """
    ids: List[str]
    action: Literal["APPROVE", "REJECT"]
    note: Optional[str] = ""
