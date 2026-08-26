from pydantic import BaseModel
from typing import Optional, Literal


TaskPriority = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
TaskStatus = Literal["PENDING", "ONGOING", "COMPLETED"]


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = ""
    assigneeId: str
    # Optional by design: personal to-dos and one-off manager requests are
    # real work that belongs to no project.
    projectId: Optional[str] = None
    priority: Optional[TaskPriority] = "MEDIUM"
    # Frontend uses this for local notification cadence; null = no reminder.
    reminderIntervalMinutes: Optional[int] = None
    dueDate: Optional[str] = None  # YYYY-MM-DD
    attachments: Optional[list[str]] = None  # file URLs


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    assigneeId: Optional[str] = None
    projectId: Optional[str] = None
    # Project managers move work across their board directly. The assignee's
    # own start/complete endpoints remain the path for self-service.
    status: Optional[TaskStatus] = None
    priority: Optional[TaskPriority] = None
    reminderIntervalMinutes: Optional[int] = None
    dueDate: Optional[str] = None
    attachments: Optional[list[str]] = None
