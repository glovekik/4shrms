from pydantic import BaseModel
from typing import Optional, Literal


TodoPriority = Literal["LOW", "MEDIUM", "HIGH"]
# Three columns, but "DONE" keeps its name rather than becoming
# "COMPLETED": the attendance screen pulls `status=DONE` to fill in work
# notes and the reminder cron skips `status != DONE`. Renaming it would
# have broken both silently.
TodoStatus = Literal["OPEN", "ONGOING", "DONE"]


class TodoCreate(BaseModel):
    title: str
    description: Optional[str] = None
    dueDate: Optional[str] = None  # YYYY-MM-DD
    priority: Optional[TodoPriority] = "MEDIUM"
    # ISO 8601 datetime — UI uses this to schedule a local reminder.
    reminderAt: Optional[str] = None
    # Hidden from the owner's manager. Everything else on a personal board
    # is visible up the reporting line; this is the opt-out for the item
    # you don't want surfaced.
    isPrivate: Optional[bool] = False


class TodoUpdate(BaseModel):
    status: Optional[TodoStatus] = None
    isPrivate: Optional[bool] = None
    title: Optional[str] = None
    description: Optional[str] = None
    dueDate: Optional[str] = None
    priority: Optional[TodoPriority] = None
    reminderAt: Optional[str] = None
