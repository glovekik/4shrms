from pydantic import BaseModel
from typing import List, Optional


class DocumentCreate(BaseModel):
    category: str          # e.g. "Aadhaar", "PAN", "OfferLetter", "Resume"
    fileName: str
    fileUrl: str
    notes: Optional[str] = None
    expiresOn: Optional[str] = None  # YYYY-MM-DD, optional
    # When HR uploads on the employee's behalf, the doc is locked so the
    # employee can't replace/delete it. Set server-side based on the caller
    # role — accepting it from the client would let a user spoof the flag.
    # Kept on the model so the response serializer can echo it.
    lockedByHR: Optional[bool] = None


class RequiredDocumentItem(BaseModel):
    """A single document HR expects an employee to upload."""
    category: str
    note: Optional[str] = None


class RequiredDocumentsSet(BaseModel):
    """HR sets the full list of documents an employee owes. Replaces any
    existing list — categories the employee has already UPLOADED stay
    marked UPLOADED."""
    items: List[RequiredDocumentItem]
