from pydantic import BaseModel
from typing import Optional, Literal


# ================= LEAVE TYPES =================
class LeaveTypeCreate(BaseModel):
    code: str            # e.g. "EARNED" — unique
    name: str            # display name
    daysPerMonth: float = 0.0
    daysPerYear: float = 0.0
    allowHalfDay: bool = True
    requiresAttachment: bool = False
    description: Optional[str] = ""
    isActive: bool = True


class LeaveTypeUpdate(BaseModel):
    name: Optional[str] = None
    daysPerMonth: Optional[float] = None
    daysPerYear: Optional[float] = None
    allowHalfDay: Optional[bool] = None
    requiresAttachment: Optional[bool] = None
    description: Optional[str] = None
    isActive: Optional[bool] = None


# ================= LEAVE REQUESTS =================
HalfDayPart = Literal["FIRST", "SECOND"]


class LeaveRequestCreate(BaseModel):
    leaveTypeCode: str
    fromDate: str        # YYYY-MM-DD
    toDate: str          # YYYY-MM-DD
    reason: str
    halfDay: bool = False
    halfDayPart: Optional[HalfDayPart] = None
    attachmentUrl: Optional[str] = None


class LeaveDecision(BaseModel):
    action: Literal["APPROVE", "REJECT"]
    note: Optional[str] = ""


# ================= HR LEAVE BALANCE UPSERT =================
class LeaveBalanceUpsert(BaseModel):
    """HR-managed balance grant/adjustment.

    `year` defaults to current calendar year on the server when omitted.
    `used` and `pending` are optional adjustments — when set they overwrite
    the stored values, so HR can also use this to correct a miscounted
    balance, not just allocate more.
    """
    leaveTypeCode: str
    allocated: float
    year: Optional[int] = None
    used: Optional[float] = None
    pending: Optional[float] = None
    note: Optional[str] = None
