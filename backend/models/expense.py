from pydantic import BaseModel
from typing import Optional


class ExpenseCreate(BaseModel):
    title: str
    amount: float
    category: str        # free string, e.g. "UTILITIES", "OFFICE_SUPPLIES"
    date: str            # YYYY-MM-DD (date of expense)
    description: Optional[str] = ""
    receiptUrl: Optional[str] = None
    vendor: Optional[str] = ""
    paymentMethod: Optional[str] = None  # CASH, CARD, BANK_TRANSFER, etc.


class ExpenseUpdate(BaseModel):
    title: Optional[str] = None
    amount: Optional[float] = None
    category: Optional[str] = None
    date: Optional[str] = None
    description: Optional[str] = None
    receiptUrl: Optional[str] = None
    vendor: Optional[str] = None
    paymentMethod: Optional[str] = None
