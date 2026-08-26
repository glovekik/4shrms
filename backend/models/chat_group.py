from pydantic import BaseModel
from typing import Optional


class ChatGroupCreate(BaseModel):
    name: str
    description: Optional[str] = None
    memberIds: list[str] = []


class ChatGroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    memberIds: Optional[list[str]] = None
