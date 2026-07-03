from pydantic import BaseModel
from typing import Optional


class ClientVisitCreate(BaseModel):
    """An employee logging that they are working from a client location.
    Captures the device GPS fix (+ a best-effort reverse-geocoded address)
    and an optional note. `capturedAt` is the client ISO instant (the
    check-in); the server stores it as IST wall-clock."""
    date: str                      # YYYY-MM-DD
    latitude: float
    longitude: float
    address: Optional[str] = None
    notes: Optional[str] = None
    capturedAt: Optional[str] = None


class ClientVisitCheckout(BaseModel):
    """Closes today's open client-location visit. `checkOut` is the client
    ISO instant; `notes` optionally updates the work notes."""
    checkOut: Optional[str] = None
    notes: Optional[str] = None
