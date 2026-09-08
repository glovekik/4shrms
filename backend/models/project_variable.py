from pydantic import BaseModel
from typing import Literal, Optional


# Who may read a variable.
#   managers  — project managers only (and HR/CEO, who see everything)
#   selected  — the managers plus an explicit list of member ids
#   team      — everyone currently on the project
Visibility = Literal["managers", "selected", "team"]


class ProjectVariableCreate(BaseModel):
    """A reusable snippet stored against a project.

    `content` is stored verbatim — it is code, config or a connection string
    that a teammate will copy and paste, so nothing is trimmed, reformatted or
    escaped on the way in or out.
    """
    fileName: str
    content: str = ""
    language: Optional[str] = None      # display hint only, e.g. "python"
    description: Optional[str] = None
    visibility: Visibility = "team"
    # Only meaningful when visibility == "selected".
    allowedUserIds: Optional[list[str]] = None


class ProjectVariableUpdate(BaseModel):
    fileName: Optional[str] = None
    content: Optional[str] = None
    language: Optional[str] = None
    description: Optional[str] = None
    visibility: Optional[Visibility] = None
    allowedUserIds: Optional[list[str]] = None
