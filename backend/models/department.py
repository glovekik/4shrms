from pydantic import BaseModel
from typing import Optional


class DepartmentCreate(BaseModel):
    name: str
    description: Optional[str] = None
    headUserId: Optional[str] = None  # department head — typically a manager


class DepartmentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    headUserId: Optional[str] = None
