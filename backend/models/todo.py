from pydantic import BaseModel
from typing import Optional, Literal


TodoPriority = Literal["LOW", "MEDIUM", "HIGH"]
TodoStatus = Literal["OPEN", "DONE"]


class TodoCreate(BaseModel):
    title: str
    description: Optional[str] = None
    dueDate: Optional[str] = None  # YYYY-MM-DD
    priority: Optional[TodoPriority] = "MEDIUM"
    # ISO 8601 datetime — UI uses this to schedule a local reminder.
    reminderAt: Optional[str] = None


class TodoUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    dueDate: Optional[str] = None
    priority: Optional[TodoPriority] = None
    reminderAt: Optional[str] = None
