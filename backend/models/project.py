from pydantic import BaseModel, model_validator
from typing import Optional, Literal


ProjectStatus = Literal["Active", "OnHold", "Completed"]


class _ManagerAlias(BaseModel):
    """Accepts the legacy `projectManagerIds` spelling.

    The user profile also has a `work.projectManagerIds` meaning something
    different ("who manages this employee"), which made the two easy to confuse.
    Projects now say `managerIds`; the old key is still read so existing
    clients don't 422 mid-rollout.
    """

    @model_validator(mode="before")
    @classmethod
    def _alias_manager_ids(cls, data):
        if isinstance(data, dict) and "managerIds" not in data:
            legacy = data.get("projectManagerIds")
            if legacy is not None:
                data = {**data, "managerIds": legacy}
        return data


class ProjectCreate(_ManagerAlias):
    name: str
    code: str  # short unique identifier (e.g. "ALPHA")
    description: Optional[str] = None
    departmentId: Optional[str] = None
    managerIds: Optional[list[str]] = None
    memberIds: Optional[list[str]] = None
    status: Optional[ProjectStatus] = "Active"
    startDate: Optional[str] = None  # YYYY-MM-DD
    endDate: Optional[str] = None
    # Free-form stack tags ("FastAPI", "React Native"). Not a controlled
    # vocabulary on purpose: every project brings something the list wouldn't
    # have had, and a wrong dropdown here just means the tag goes unrecorded.
    technologies: Optional[list[str]] = None


class ProjectUpdate(_ManagerAlias):
    name: Optional[str] = None
    description: Optional[str] = None
    departmentId: Optional[str] = None
    managerIds: Optional[list[str]] = None
    memberIds: Optional[list[str]] = None
    status: Optional[ProjectStatus] = None
    technologies: Optional[list[str]] = None
    startDate: Optional[str] = None
    endDate: Optional[str] = None
