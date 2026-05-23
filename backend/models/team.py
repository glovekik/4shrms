from pydantic import BaseModel
from typing import List, Optional


class TeamCreate(BaseModel):
    name: str
    teamLeadId: str
    memberIds: List[str] = []


class TeamUpdate(BaseModel):
    name: Optional[str] = None
    teamLeadId: Optional[str] = None
    memberIds: Optional[List[str]] = None
