from pydantic import BaseModel
from typing import Literal, Optional


# A phase is either not started, being worked on, or finished. Deliberately
# the same three states as a task, so "this phase is in progress" means the
# same thing to a reader as "this task is in progress".
PhaseStatus = Literal["PENDING", "ONGOING", "COMPLETED"]


class ProjectPhaseCreate(BaseModel):
    """A stage of a project — "Phase 1: Survey", "Phase 2: Installation".

    Tasks belong to a phase by carrying its id, so a phase's progress is its
    tasks' progress and there is no separate percentage to maintain.

    `weight` is how much of the project this phase represents. Phases are
    rarely equal — a two-week survey and a three-month rollout should not
    each count for half — so overall progress is the weighted average rather
    than a plain count of phases done.
    """
    name: str
    description: Optional[str] = None
    # Display order. Gaps are fine; the client sorts by it and falls back to
    # creation time when two phases share a number.
    order: Optional[int] = None
    status: Optional[PhaseStatus] = "PENDING"
    startDate: Optional[str] = None  # YYYY-MM-DD
    endDate: Optional[str] = None
    weight: Optional[float] = 1.0


class ProjectPhaseUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    order: Optional[int] = None
    status: Optional[PhaseStatus] = None
    startDate: Optional[str] = None
    endDate: Optional[str] = None
    weight: Optional[float] = None


class ProjectPhaseReorder(BaseModel):
    """New order for the whole set, as phase ids top to bottom."""
    phaseIds: list[str]
