from pydantic import BaseModel, EmailStr
from typing import Optional, Literal


# ================= JOB OPENINGS =================
EmploymentType = Literal[
    "Full-time", "Part-time", "Contract", "Internship",
]
OpeningStatus = Literal["Open", "OnHold", "Closed"]


class JobOpeningCreate(BaseModel):
    title: str
    departmentId: Optional[str] = None
    location: Optional[str] = None
    employmentType: Optional[EmploymentType] = "Full-time"
    description: Optional[str] = None
    requirements: Optional[str] = None
    salaryMin: Optional[float] = None
    salaryMax: Optional[float] = None
    openings: Optional[int] = 1
    status: Optional[OpeningStatus] = "Open"


class JobOpeningUpdate(BaseModel):
    title: Optional[str] = None
    departmentId: Optional[str] = None
    location: Optional[str] = None
    employmentType: Optional[EmploymentType] = None
    description: Optional[str] = None
    requirements: Optional[str] = None
    salaryMin: Optional[float] = None
    salaryMax: Optional[float] = None
    openings: Optional[int] = None
    status: Optional[OpeningStatus] = None


# ================= CANDIDATES =================
CandidateStage = Literal[
    "APPLIED", "SCREENING", "INTERVIEW",
    "OFFER", "HIRED", "REJECTED", "WITHDRAWN",
]
CandidateSource = Literal[
    "Referral", "Job Portal", "LinkedIn", "Website",
    "Walk-in", "Agency", "Other",
]


class CandidateCreate(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    jobOpeningId: Optional[str] = None
    resumeUrl: Optional[str] = None
    source: Optional[CandidateSource] = None
    referredByUserId: Optional[str] = None
    currentCompany: Optional[str] = None
    currentSalary: Optional[float] = None
    expectedSalary: Optional[float] = None
    noticePeriodDays: Optional[int] = None
    notes: Optional[str] = None


class CandidateUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    jobOpeningId: Optional[str] = None
    resumeUrl: Optional[str] = None
    source: Optional[CandidateSource] = None
    referredByUserId: Optional[str] = None
    currentCompany: Optional[str] = None
    currentSalary: Optional[float] = None
    expectedSalary: Optional[float] = None
    noticePeriodDays: Optional[int] = None
    notes: Optional[str] = None


class CandidateMove(BaseModel):
    stage: CandidateStage
    note: Optional[str] = ""


# ================= INTERVIEWS =================
InterviewMode = Literal["In-person", "Phone", "Video"]
InterviewStatus = Literal[
    "SCHEDULED", "COMPLETED", "CANCELLED", "NO_SHOW",
]
InterviewRecommendation = Literal[
    "STRONG_HIRE", "HIRE", "NO_HIRE", "STRONG_NO_HIRE",
]


class InterviewCreate(BaseModel):
    candidateId: str
    scheduledAt: str  # ISO 8601
    durationMinutes: Optional[int] = 45
    mode: Optional[InterviewMode] = "Video"
    location: Optional[str] = None  # room or meeting link
    interviewerIds: list[str]
    round: Optional[str] = None     # "Technical 1", "HR", etc.
    notes: Optional[str] = None


class InterviewUpdate(BaseModel):
    scheduledAt: Optional[str] = None
    durationMinutes: Optional[int] = None
    mode: Optional[InterviewMode] = None
    location: Optional[str] = None
    interviewerIds: Optional[list[str]] = None
    round: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[InterviewStatus] = None


class InterviewFeedback(BaseModel):
    rating: int  # 1..5
    recommendation: InterviewRecommendation
    strengths: Optional[str] = None
    concerns: Optional[str] = None
    notes: Optional[str] = None


# ================= OFFERS =================
OfferStatus = Literal[
    "DRAFT", "SENT", "ACCEPTED", "REJECTED", "EXPIRED", "REVOKED",
]


class OfferCreate(BaseModel):
    candidateId: str
    jobOpeningId: Optional[str] = None
    position: str
    annualCtc: float
    joiningDate: str          # YYYY-MM-DD
    validUntil: Optional[str] = None  # YYYY-MM-DD
    notes: Optional[str] = None
    salaryBreakdown: Optional[dict] = None


class OfferUpdate(BaseModel):
    position: Optional[str] = None
    annualCtc: Optional[float] = None
    joiningDate: Optional[str] = None
    validUntil: Optional[str] = None
    notes: Optional[str] = None
    salaryBreakdown: Optional[dict] = None


class OfferDecisionRecord(BaseModel):
    """HR records the candidate's response (accepted/rejected externally).

    A public token-based candidate-facing accept flow can be added later.
    """
    outcome: Literal["ACCEPTED", "REJECTED"]
    note: Optional[str] = ""
