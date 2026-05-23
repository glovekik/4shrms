from pydantic import BaseModel
from typing import Optional, Literal


ProjectStatus = Literal["Active", "OnHold", "Completed"]


class ProjectCreate(BaseModel):
    name: str
    code: str  # short unique identifier (e.g. "ALPHA")
    description: Optional[str] = None
    departmentId: Optional[str] = None
    projectManagerIds: Optional[list[str]] = None
    memberIds: Optional[list[str]] = None
    status: Optional[ProjectStatus] = "Active"
    startDate: Optional[str] = None  # YYYY-MM-DD
    endDate: Optional[str] = None
    billable: Optional[bool] = False


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    departmentId: Optional[str] = None
    projectManagerIds: Optional[list[str]] = None
    memberIds: Optional[list[str]] = None
    status: Optional[ProjectStatus] = None
    startDate: Optional[str] = None
    endDate: Optional[str] = None
    billable: Optional[bool] = None
