from pydantic import BaseModel
from typing import Optional, Literal


DocStatus = Literal["PENDING", "UPLOADED", "VERIFIED", "REJECTED"]
TaskStatus = Literal["PENDING", "DONE"]


class OnboardingCreate(BaseModel):
    userId: str


class DocumentUpload(BaseModel):
    documentId: str       # uuid of the checklist item
    fileUrl: str          # frontend uploads to storage, sends URL


class DocumentStatusUpdate(BaseModel):
    documentId: str
    status: DocStatus     # HR sets VERIFIED / REJECTED / etc.
    note: Optional[str] = ""


class TaskStatusUpdate(BaseModel):
    taskId: str
    status: TaskStatus
    note: Optional[str] = ""
