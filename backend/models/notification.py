from pydantic import BaseModel
from typing import Optional


class MarkReadBody(BaseModel):
    # Empty body — keeping a model so OpenAPI documents the POST clearly.
    note: Optional[str] = None
