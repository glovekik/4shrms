"""Recruitment / ATS — PRD section 14.

Modules under one file because they're tightly coupled (a Candidate
references a JobOpening; an Interview references a Candidate; an Offer
references both). Splitting them across multiple files would add import
churn without buying clarity.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from config import COMPANY_NAME, OFFER_ACCEPT_URL_TEMPLATE
from database import db
from utils.dependencies import (
    get_current_user,
    get_current_user_doc,
    require_hr,
)
from utils.audit import log_audit
from utils.email import send_notification_email
from utils.notify import notify_user
from models.recruitment import (
    JobOpeningCreate, JobOpeningUpdate,
    CandidateCreate, CandidateUpdate, CandidateMove,
    InterviewCreate, InterviewUpdate, InterviewFeedback,
    OfferCreate, OfferUpdate, OfferDecisionRecord,
)


# ================= ROUTERS =================
openings_hr_router = APIRouter()       # /hr/job-openings
candidates_hr_router = APIRouter()     # /hr/candidates
interviews_hr_router = APIRouter()     # /hr/interviews
interviews_my_router = APIRouter()     # /interviews/mine (assigned to me)
offers_hr_router = APIRouter()         # /hr/offers


# ================= SERIALIZERS =================
def _serialize_opening(o: dict) -> dict:
    return {
        "id": str(o["_id"]),
        "title": o.get("title"),
        "departmentId": o.get("departmentId"),
        "location": o.get("location"),
        "employmentType": o.get("employmentType"),
        "description": o.get("description"),
        "requirements": o.get("requirements"),
        "salaryMin": o.get("salaryMin"),
        "salaryMax": o.get("salaryMax"),
        "openings": o.get("openings", 1),
        "status": o.get("status", "Open"),
        "createdAt": (
            o["createdAt"].isoformat()
            if o.get("createdAt") else None
        ),
    }


def _serialize_candidate(c: dict) -> dict:
    return {
        "id": str(c["_id"]),
        "name": c.get("name"),
        "email": c.get("email"),
        "phone": c.get("phone"),
        "jobOpeningId": c.get("jobOpeningId"),
        "resumeUrl": c.get("resumeUrl"),
        "source": c.get("source"),
        "referredByUserId": c.get("referredByUserId"),
        "currentCompany": c.get("currentCompany"),
        "currentSalary": c.get("currentSalary"),
        "expectedSalary": c.get("expectedSalary"),
        "noticePeriodDays": c.get("noticePeriodDays"),
        "notes": c.get("notes"),
        "stage": c.get("stage", "APPLIED"),
        "stageHistory": c.get("stageHistory", []),
        "createdAt": (
            c["createdAt"].isoformat()
            if c.get("createdAt") else None
        ),
    }


def _serialize_interview(i: dict) -> dict:
    return {
        "id": str(i["_id"]),
        "candidateId": i.get("candidateId"),
        "scheduledAt": (
            i["scheduledAt"].isoformat()
            if hasattr(i.get("scheduledAt"), "isoformat")
            else i.get("scheduledAt")
        ),
        "durationMinutes": i.get("durationMinutes"),
        "mode": i.get("mode"),
        "location": i.get("location"),
        "interviewerIds": i.get("interviewerIds", []),
        "round": i.get("round"),
        "notes": i.get("notes"),
        "status": i.get("status", "SCHEDULED"),
        "feedback": i.get("feedback", []),
    }


def _serialize_offer(o: dict) -> dict:
    return {
        "id": str(o["_id"]),
        "candidateId": o.get("candidateId"),
        "jobOpeningId": o.get("jobOpeningId"),
        "position": o.get("position"),
        "annualCtc": o.get("annualCtc"),
        "joiningDate": o.get("joiningDate"),
        "validUntil": o.get("validUntil"),
        "notes": o.get("notes"),
        "salaryBreakdown": o.get("salaryBreakdown"),
        "status": o.get("status", "DRAFT"),
        "sentAt": (
            o["sentAt"].isoformat()
            if hasattr(o.get("sentAt"), "isoformat") else o.get("sentAt")
        ),
        "decidedAt": (
            o["decidedAt"].isoformat()
            if hasattr(o.get("decidedAt"), "isoformat") else o.get("decidedAt")
        ),
        "createdAt": (
            o["createdAt"].isoformat()
            if hasattr(o.get("createdAt"), "isoformat") else o.get("createdAt")
        ),
    }


# ================= JOB OPENINGS =================
@openings_hr_router.get("")
async def list_openings(
    status: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    query: dict = {}
    if status:
        query["status"] = status
    out = []
    async for o in db.job_openings.find(query).sort("createdAt", -1):
        out.append(_serialize_opening(o))
    return out


@openings_hr_router.post("")
async def create_opening(
    data: JobOpeningCreate,
    hr: dict = Depends(require_hr),
):
    title = (data.title or "").strip()
    if not title:
        raise HTTPException(400, "title is required")

    now = datetime.now(timezone.utc)
    doc = data.model_dump(exclude_none=True)
    doc.setdefault("status", "Open")
    doc.setdefault("openings", 1)
    doc["title"] = title
    doc["createdAt"] = now
    doc["updatedAt"] = now
    result = await db.job_openings.insert_one(doc)
    await log_audit(
        actor_id=str(hr["_id"]),
        action="job_opening.create",
        entity_type="job_openings",
        entity_id=str(result.inserted_id),
        after={"title": title},
    )
    return {"id": str(result.inserted_id), "message": "Opening created"}


@openings_hr_router.get("/{id}")
async def get_opening(
    id: str,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    o = await db.job_openings.find_one({"_id": oid})
    if not o:
        raise HTTPException(404, "Opening not found")
    return _serialize_opening(o)


@openings_hr_router.put("/{id}")
async def update_opening(
    id: str,
    data: JobOpeningUpdate,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    update = data.model_dump(exclude_none=True)
    update["updatedAt"] = datetime.now(timezone.utc)
    result = await db.job_openings.update_one({"_id": oid}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(404, "Opening not found")
    await log_audit(
        actor_id=str(hr["_id"]),
        action="job_opening.update",
        entity_type="job_openings",
        entity_id=id,
        after=update,
    )
    return {"message": "Opening updated"}


@openings_hr_router.delete("/{id}")
async def delete_opening(
    id: str,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    result = await db.job_openings.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(404, "Opening not found")
    await log_audit(
        actor_id=str(hr["_id"]),
        action="job_opening.delete",
        entity_type="job_openings",
        entity_id=id,
    )
    return {"message": "Opening deleted"}


# ================= CANDIDATES =================
@candidates_hr_router.get("")
async def list_candidates(
    stage: Optional[str] = Query(None),
    jobOpeningId: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    query: dict = {}
    if stage:
        query["stage"] = stage
    if jobOpeningId:
        query["jobOpeningId"] = jobOpeningId
    if search:
        rgx = {"$regex": search, "$options": "i"}
        query["$or"] = [{"name": rgx}, {"email": rgx}, {"phone": rgx}]
    out = []
    async for c in db.candidates.find(query).sort("createdAt", -1):
        out.append(_serialize_candidate(c))
    return out


@candidates_hr_router.post("")
async def create_candidate(
    data: CandidateCreate,
    hr: dict = Depends(require_hr),
):
    # Dedup on email per opening (or globally if no opening attached).
    dedup: dict = {"email": data.email}
    if data.jobOpeningId:
        dedup["jobOpeningId"] = data.jobOpeningId
    if await db.candidates.find_one(dedup):
        raise HTTPException(
            409,
            "A candidate with this email already exists for the same opening",
        )

    now = datetime.now(timezone.utc)
    doc = data.model_dump(exclude_none=True)
    doc["stage"] = "APPLIED"
    doc["stageHistory"] = [{
        "stage": "APPLIED",
        "at": now,
        "actorId": str(hr["_id"]),
        "note": "Application received",
    }]
    doc["createdAt"] = now
    doc["updatedAt"] = now
    result = await db.candidates.insert_one(doc)
    await log_audit(
        actor_id=str(hr["_id"]),
        action="candidate.create",
        entity_type="candidates",
        entity_id=str(result.inserted_id),
        after={"email": data.email, "name": data.name},
    )
    return {"id": str(result.inserted_id), "message": "Candidate created"}


@candidates_hr_router.get("/{id}")
async def get_candidate(
    id: str,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    c = await db.candidates.find_one({"_id": oid})
    if not c:
        raise HTTPException(404, "Candidate not found")
    return _serialize_candidate(c)


@candidates_hr_router.put("/{id}")
async def update_candidate(
    id: str,
    data: CandidateUpdate,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    update = data.model_dump(exclude_none=True)
    update["updatedAt"] = datetime.now(timezone.utc)
    result = await db.candidates.update_one({"_id": oid}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(404, "Candidate not found")
    await log_audit(
        actor_id=str(hr["_id"]),
        action="candidate.update",
        entity_type="candidates",
        entity_id=id,
        after=update,
    )
    return {"message": "Candidate updated"}


@candidates_hr_router.post("/{id}/move")
async def move_candidate_stage(
    id: str,
    data: CandidateMove,
    hr: dict = Depends(require_hr),
):
    """Transition the candidate to a new pipeline stage."""
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    c = await db.candidates.find_one({"_id": oid})
    if not c:
        raise HTTPException(404, "Candidate not found")

    now = datetime.now(timezone.utc)
    history_entry = {
        "stage": data.stage,
        "at": now,
        "actorId": str(hr["_id"]),
        "note": data.note or "",
    }
    await db.candidates.update_one(
        {"_id": oid},
        {
            "$set": {"stage": data.stage, "updatedAt": now},
            "$push": {"stageHistory": history_entry},
        },
    )
    await log_audit(
        actor_id=str(hr["_id"]),
        action="candidate.move",
        entity_type="candidates",
        entity_id=id,
        before={"stage": c.get("stage")},
        after={"stage": data.stage},
    )
    return {"message": f"Candidate moved to {data.stage}"}


@candidates_hr_router.delete("/{id}")
async def delete_candidate(
    id: str,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    result = await db.candidates.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(404, "Candidate not found")
    await log_audit(
        actor_id=str(hr["_id"]),
        action="candidate.delete",
        entity_type="candidates",
        entity_id=id,
    )
    return {"message": "Candidate deleted"}


# ================= INTERVIEWS =================
@interviews_hr_router.get("")
async def list_interviews(
    candidateId: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    query: dict = {}
    if candidateId:
        query["candidateId"] = candidateId
    if status:
        query["status"] = status
    out = []
    async for i in db.interviews.find(query).sort("scheduledAt", -1):
        out.append(_serialize_interview(i))
    return out


@interviews_hr_router.post("")
async def create_interview(
    data: InterviewCreate,
    hr: dict = Depends(require_hr),
):
    # Validate candidate exists
    try:
        cand_oid = ObjectId(data.candidateId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid candidateId")
    candidate = await db.candidates.find_one({"_id": cand_oid})
    if not candidate:
        raise HTTPException(400, "Candidate not found")

    # Parse scheduledAt
    s = data.scheduledAt
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        scheduled = datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, "Invalid scheduledAt (use ISO 8601)")

    # Validate interviewer ids
    if not data.interviewerIds:
        raise HTTPException(400, "At least one interviewer required")
    interviewer_oids = []
    for uid in data.interviewerIds:
        try:
            interviewer_oids.append(ObjectId(uid))
        except (InvalidId, TypeError):
            raise HTTPException(400, f"Invalid interviewer id: {uid}")
    found = await db.users.count_documents({"_id": {"$in": interviewer_oids}})
    if found != len(interviewer_oids):
        raise HTTPException(400, "One or more interviewers do not exist")

    now = datetime.now(timezone.utc)
    doc = {
        "candidateId": data.candidateId,
        "scheduledAt": scheduled,
        "durationMinutes": data.durationMinutes or 45,
        "mode": data.mode or "Video",
        "location": data.location,
        "interviewerIds": data.interviewerIds,
        "round": data.round,
        "notes": data.notes,
        "status": "SCHEDULED",
        "feedback": [],
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.interviews.insert_one(doc)

    # Notify interviewers
    for uid in data.interviewerIds:
        await notify_user(
            uid,
            "interview_scheduled",
            f"Interview scheduled: {candidate.get('name', '')}",
            f"{data.round or 'Interview'} on {scheduled.isoformat()}",
            {
                "interviewId": str(result.inserted_id),
                "candidateId": data.candidateId,
            },
        )

    await log_audit(
        actor_id=str(hr["_id"]),
        action="interview.create",
        entity_type="interviews",
        entity_id=str(result.inserted_id),
        after={
            "candidateId": data.candidateId,
            "scheduledAt": scheduled.isoformat(),
            "interviewerIds": data.interviewerIds,
        },
    )
    return {"id": str(result.inserted_id), "message": "Interview scheduled"}


@interviews_hr_router.put("/{id}")
async def update_interview(
    id: str,
    data: InterviewUpdate,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    update = data.model_dump(exclude_none=True)
    if "scheduledAt" in update:
        s = update["scheduledAt"]
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            update["scheduledAt"] = datetime.fromisoformat(s)
        except ValueError:
            raise HTTPException(400, "Invalid scheduledAt")
    update["updatedAt"] = datetime.now(timezone.utc)
    result = await db.interviews.update_one({"_id": oid}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(404, "Interview not found")
    return {"message": "Interview updated"}


@interviews_hr_router.post("/{id}/feedback")
async def submit_interview_feedback(
    id: str,
    data: InterviewFeedback,
    user: dict = Depends(get_current_user_doc),
):
    """Either an assigned interviewer or HR may submit feedback."""
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    interview = await db.interviews.find_one({"_id": oid})
    if not interview:
        raise HTTPException(404, "Interview not found")

    user_id = str(user["_id"])
    is_hr = user.get("role") == "HR"
    is_interviewer = user_id in interview.get("interviewerIds", [])
    if not (is_hr or is_interviewer):
        raise HTTPException(403, "Not an assigned interviewer")

    if not (1 <= int(data.rating) <= 5):
        raise HTTPException(400, "rating must be 1..5")

    now = datetime.now(timezone.utc)
    feedback_entry = {
        "interviewerId": user_id,
        "rating": int(data.rating),
        "recommendation": data.recommendation,
        "strengths": data.strengths,
        "concerns": data.concerns,
        "notes": data.notes,
        "at": now,
    }
    # Replace any prior feedback by the same interviewer.
    await db.interviews.update_one(
        {"_id": oid},
        {"$pull": {"feedback": {"interviewerId": user_id}}},
    )
    await db.interviews.update_one(
        {"_id": oid},
        {
            "$push": {"feedback": feedback_entry},
            "$set": {"status": "COMPLETED", "updatedAt": now},
        },
    )

    # Notify whoever scheduled the interview (recruiter/HR) that feedback
    # is in — unless they're the one who submitted it.
    owner = interview.get("createdBy")
    if owner and owner != user_id:
        candidate = await db.candidates.find_one(
            {"_id": ObjectId(interview["candidateId"])}, {"name": 1}
        ) if interview.get("candidateId") else None
        cand_name = (candidate or {}).get("name") or "a candidate"
        await notify_user(
            owner,
            "interview_feedback_submitted",
            "Interview feedback submitted",
            f"{user.get('name', 'An interviewer')} left feedback for "
            f"{cand_name} ({data.recommendation}).",
            {"interviewId": id, "candidateId": interview.get("candidateId")},
        )

    return {"message": "Feedback recorded"}


@interviews_my_router.get("")
async def my_interviews(
    user_id: str = Depends(get_current_user),
):
    out = []
    async for i in db.interviews.find(
        {"interviewerIds": user_id},
    ).sort("scheduledAt", -1):
        out.append(_serialize_interview(i))
    return out


# ================= OFFERS =================
@offers_hr_router.get("")
async def list_offers(
    candidateId: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    query: dict = {}
    if candidateId:
        query["candidateId"] = candidateId
    if status:
        query["status"] = status
    out = []
    async for o in db.offers.find(query).sort("createdAt", -1):
        out.append(_serialize_offer(o))
    return out


@offers_hr_router.post("")
async def create_offer(
    data: OfferCreate,
    hr: dict = Depends(require_hr),
):
    try:
        cand_oid = ObjectId(data.candidateId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid candidateId")
    candidate = await db.candidates.find_one({"_id": cand_oid})
    if not candidate:
        raise HTTPException(400, "Candidate not found")

    if data.annualCtc is None or data.annualCtc <= 0:
        raise HTTPException(400, "annualCtc must be positive")

    now = datetime.now(timezone.utc)
    doc = data.model_dump(exclude_none=True)
    doc["status"] = "DRAFT"
    doc["createdBy"] = str(hr["_id"])
    doc["createdAt"] = now
    doc["updatedAt"] = now
    result = await db.offers.insert_one(doc)
    await log_audit(
        actor_id=str(hr["_id"]),
        action="offer.create",
        entity_type="offers",
        entity_id=str(result.inserted_id),
        after={
            "candidateId": data.candidateId,
            "annualCtc": data.annualCtc,
        },
    )
    return {"id": str(result.inserted_id), "message": "Offer drafted"}


@offers_hr_router.put("/{id}")
async def update_offer(
    id: str,
    data: OfferUpdate,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    existing = await db.offers.find_one({"_id": oid})
    if not existing:
        raise HTTPException(404, "Offer not found")
    if existing.get("status") not in ("DRAFT",):
        raise HTTPException(
            400,
            f"Offer in status {existing.get('status')} cannot be edited",
        )

    update = data.model_dump(exclude_none=True)
    update["updatedAt"] = datetime.now(timezone.utc)
    await db.offers.update_one({"_id": oid}, {"$set": update})
    return {"message": "Offer updated"}


@offers_hr_router.post("/{id}/send")
async def send_offer(
    id: str,
    hr: dict = Depends(require_hr),
):
    """Mark DRAFT → SENT and email the candidate."""
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    offer = await db.offers.find_one({"_id": oid})
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.get("status") != "DRAFT":
        raise HTTPException(
            400,
            f"Only DRAFT offers can be sent (current: {offer.get('status')})",
        )

    try:
        candidate = await db.candidates.find_one(
            {"_id": ObjectId(offer["candidateId"])}
        )
    except (InvalidId, TypeError, KeyError):
        candidate = None
    if not candidate:
        raise HTTPException(400, "Candidate no longer exists")

    now = datetime.now(timezone.utc)
    await db.offers.update_one(
        {"_id": oid},
        {"$set": {"status": "SENT", "sentAt": now, "updatedAt": now}},
    )

    # Move candidate stage if currently before OFFER
    if candidate.get("stage") in ("APPLIED", "SCREENING", "INTERVIEW"):
        await db.candidates.update_one(
            {"_id": candidate["_id"]},
            {
                "$set": {"stage": "OFFER", "updatedAt": now},
                "$push": {
                    "stageHistory": {
                        "stage": "OFFER",
                        "at": now,
                        "actorId": str(hr["_id"]),
                        "note": "Offer sent",
                    }
                },
            },
        )

    # Mint (or reuse) a public token so the candidate can accept via link.
    from routes.public import ensure_offer_public_token
    token = await ensure_offer_public_token(
        {**offer, "_id": oid}
    )
    if OFFER_ACCEPT_URL_TEMPLATE and "{token}" in OFFER_ACCEPT_URL_TEMPLATE:
        link = OFFER_ACCEPT_URL_TEMPLATE.replace("{token}", token)
        link_line = f"\n\nAccept or decline online:\n{link}\n"
    else:
        link_line = (
            f"\n\nReference token (for reply): {token}\n"
        )

    body = (
        f"Hi {candidate.get('name', 'there')},\n\n"
        f"We're delighted to extend an offer for the {offer.get('position')} "
        f"position at {COMPANY_NAME}.\n\n"
        f"Annual CTC: {offer.get('annualCtc')}\n"
        f"Proposed joining date: {offer.get('joiningDate')}\n"
        + (
            f"Valid until: {offer.get('validUntil')}\n"
            if offer.get('validUntil') else ""
        )
        + (
            f"\nNotes:\n{offer.get('notes')}\n"
            if offer.get('notes') else ""
        )
        + link_line
        + f"\nWe look forward to hearing from you.\n\n"
        f"Regards,\n{COMPANY_NAME}"
    )
    await send_notification_email(
        candidate["email"],
        f"Your offer from {COMPANY_NAME}",
        body,
    )

    await log_audit(
        actor_id=str(hr["_id"]),
        action="offer.send",
        entity_type="offers",
        entity_id=id,
    )
    return {"message": "Offer sent"}


@offers_hr_router.post("/{id}/record-decision")
async def record_offer_decision(
    id: str,
    data: OfferDecisionRecord,
    hr: dict = Depends(require_hr),
):
    """HR records the candidate's response (accept/reject). When accepted,
    the candidate stage moves to HIRED."""
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    offer = await db.offers.find_one({"_id": oid})
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.get("status") not in ("SENT", "DRAFT"):
        raise HTTPException(
            400,
            f"Cannot record decision from status {offer.get('status')}",
        )

    now = datetime.now(timezone.utc)
    new_status = (
        "ACCEPTED" if data.outcome == "ACCEPTED" else "REJECTED"
    )
    await db.offers.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": new_status,
                "decidedAt": now,
                "decisionNote": data.note or "",
                "updatedAt": now,
            }
        },
    )

    # Move candidate
    next_stage = "HIRED" if data.outcome == "ACCEPTED" else "REJECTED"
    try:
        cand_oid = ObjectId(offer["candidateId"])
        await db.candidates.update_one(
            {"_id": cand_oid},
            {
                "$set": {"stage": next_stage, "updatedAt": now},
                "$push": {
                    "stageHistory": {
                        "stage": next_stage,
                        "at": now,
                        "actorId": str(hr["_id"]),
                        "note": data.note or f"Offer {new_status.lower()}",
                    }
                },
            },
        )
    except (InvalidId, TypeError, KeyError):
        pass

    await log_audit(
        actor_id=str(hr["_id"]),
        action=f"offer.{new_status.lower()}",
        entity_type="offers",
        entity_id=id,
    )
    return {"message": f"Offer marked {new_status.lower()}"}


@offers_hr_router.post("/{id}/revoke")
async def revoke_offer(
    id: str,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    now = datetime.now(timezone.utc)
    result = await db.offers.update_one(
        {"_id": oid, "status": {"$in": ["DRAFT", "SENT"]}},
        {"$set": {"status": "REVOKED", "updatedAt": now}},
    )
    if result.matched_count == 0:
        raise HTTPException(
            404, "Offer not found or not in a revocable state",
        )
    await log_audit(
        actor_id=str(hr["_id"]),
        action="offer.revoke",
        entity_type="offers",
        entity_id=id,
    )
    return {"message": "Offer revoked"}
