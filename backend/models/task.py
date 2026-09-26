from pydantic import BaseModel
from typing import Optional, Literal


TaskPriority = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
TaskStatus = Literal["PENDING", "ONGOING", "COMPLETED"]


class TaskCreate(BaseModel):
    phaseId: Optional[str] = None
    # How much of its phase this task represents, as a percentage. Counting
    # tasks equally makes "write the README" worth as much as "install 40
    # cameras", which is how a progress bar ends up lying about a project.
    #
    # None means "whatever the phase has left" — the route works that out,
    # so it can't be defaulted here.
    weight: Optional[float] = None
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
    # Which phase this task belongs to; null detaches it from every phase.
    phaseId: Optional[str] = None
    weight: Optional[float] = None
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
