from pydantic import BaseModel
from typing import Optional, Literal


TaskPriority = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
TaskStatus = Literal["PENDING", "ONGOING", "COMPLETED"]


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = ""
    assigneeId: str
    priority: Optional[TaskPriority] = "MEDIUM"
    # Frontend uses this for local notification cadence; null = no reminder.
    reminderIntervalMinutes: Optional[int] = None
    dueDate: Optional[str] = None  # YYYY-MM-DD
    attachments: Optional[list[str]] = None  # file URLs


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    assigneeId: Optional[str] = None
    priority: Optional[TaskPriority] = None
    reminderIntervalMinutes: Optional[int] = None
    dueDate: Optional[str] = None
    attachments: Optional[list[str]] = None
