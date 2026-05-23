from pydantic import BaseModel
from typing import Optional


class HolidayCreate(BaseModel):
    date: str  # YYYY-MM-DD, unique
    name: str
    description: Optional[str] = ""


class HolidayUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
