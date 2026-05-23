from pydantic import BaseModel
from typing import Optional, Literal


PaymentMode = Literal[
    "Cash", "Bank Transfer", "UPI", "Credit Card", "Debit Card", "Company Wallet",
]


class ReimbursementCreate(BaseModel):
    title: str
    category: str            # Travel / Food / Cab / WFH / etc.
    expenseDate: str         # YYYY-MM-DD
    amount: float
    paymentMode: Optional[PaymentMode] = None
    vendorName: Optional[str] = None
    invoiceNumber: Optional[str] = None
    taxAmount: Optional[float] = None
    description: Optional[str] = None
    attachments: Optional[list[str]] = None  # bill/receipt URLs


class ReimbursementDecision(BaseModel):
    action: Literal["APPROVE", "REJECT"]
    note: Optional[str] = ""
