from pydantic import BaseModel
from typing import Optional, Literal


class ResignationCreate(BaseModel):
    requestedLastWorkingDay: str   # YYYY-MM-DD
    reason: str


class ResignationDecision(BaseModel):
    action: Literal["APPROVE", "REJECT"]
    approvedLastWorkingDay: Optional[str] = None  # required on APPROVE
    note: Optional[str] = ""


class ExitTaskStatusUpdate(BaseModel):
    taskId: str
    status: Literal["PENDING", "DONE"]
    note: Optional[str] = ""


class FFSUpdate(BaseModel):
    """All amounts INR. Server recomputes totalPayable on save."""
    pendingSalary: Optional[float] = None
    leaveEncashment: Optional[float] = None
    bonus: Optional[float] = None
    deductions: Optional[float] = None
    notes: Optional[str] = None
