from pydantic import BaseModel
from typing import Optional, Literal


# ================= GOALS =================
GoalStatus = Literal["DRAFT", "ACTIVE", "COMPLETED", "CANCELLED"]


class GoalCreate(BaseModel):
    userId: str               # employee the goal is for
    title: str
    description: Optional[str] = None
    dueDate: Optional[str] = None  # YYYY-MM-DD
    targetValue: Optional[float] = None   # numeric KPI target, optional
    unit: Optional[str] = None            # "%", "hours", "deals", etc.
    weight: Optional[float] = None        # 0..1, weight in review score


class GoalUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    dueDate: Optional[str] = None
    targetValue: Optional[float] = None
    unit: Optional[str] = None
    weight: Optional[float] = None
    status: Optional[GoalStatus] = None


class GoalProgress(BaseModel):
    achievedValue: float
    note: Optional[str] = None


# ================= REVIEWS =================
ReviewType = Literal[
    "QUARTERLY", "HALF_YEARLY", "ANNUAL", "PROMOTION", "PROBATION",
]
ReviewStatus = Literal[
    "DRAFT",             # manager set up; employee hasn't self-evaled
    "SELF_EVAL",         # employee filling self section
    "MANAGER_EVAL",      # manager filling manager section
    "SUBMITTED",         # manager submitted; awaiting employee acknowledge
    "ACKNOWLEDGED",      # employee acknowledged — review finalized
]


class ReviewCreate(BaseModel):
    employeeId: str
    type: ReviewType
    periodStart: str  # YYYY-MM-DD
    periodEnd: str
    dimensions: Optional[list[str]] = None  # e.g. ["Quality","Ownership","Collaboration"]


class ReviewDimensionRating(BaseModel):
    dimension: str
    rating: int          # 1..5
    comment: Optional[str] = None


class ReviewSelfEval(BaseModel):
    accomplishments: Optional[str] = None
    challenges: Optional[str] = None
    ratings: Optional[list[ReviewDimensionRating]] = None
    overallSelfRating: Optional[int] = None  # 1..5


class ReviewManagerEval(BaseModel):
    strengths: Optional[str] = None
    areasToImprove: Optional[str] = None
    ratings: Optional[list[ReviewDimensionRating]] = None
    overallRating: Optional[int] = None  # 1..5
    promotionRecommendation: Optional[bool] = None
    nextSteps: Optional[str] = None


class ReviewAcknowledge(BaseModel):
    note: Optional[str] = ""


# ================= FEEDBACK =================
FeedbackType = Literal[
    "POSITIVE", "CONSTRUCTIVE", "PEER", "MANAGER_TO_REPORT", "REPORT_TO_MANAGER",
]


class FeedbackCreate(BaseModel):
    toUserId: str
    type: FeedbackType
    text: str
    anonymous: Optional[bool] = False
