from pydantic import BaseModel
from typing import Optional


class HolidayCreate(BaseModel):
    date: str  # YYYY-MM-DD, unique
    name: str
    description: Optional[str] = ""


class HolidayUpdate(BaseModel):
    date: Optional[str] = None  # YYYY-MM-DD
    name: Optional[str] = None
    description: Optional[str] = None
