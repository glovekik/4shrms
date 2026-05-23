"""Public (no-auth) endpoints for the careers page + offer accept flow.

These are the only routes in the app that don't require a Bearer token.
Keep this file small and obviously-scoped so it's easy to audit security
boundaries.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from secrets import token_urlsafe
from typing import Optional, Literal

from config import COMPANY_NAME
from database import db
from utils.audit import log_audit
from utils.notify import notify_user


careers_router = APIRouter()    # /careers/...
offers_public_router = APIRouter()  # /public/offers/...


# ================= CAREERS — LIST + DETAIL =================
def _careers_serialize(o: dict) -> dict:
    """Public view — strips internal-only fields (salaryMin/Max etc. are
    kept; HR can clear them in the opening doc if they don't want them
    visible)."""
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
    }


@careers_router.get("/openings")
async def list_public_openings():
    out = []
    async for o in db.job_openings.find(
        {"status": "Open"}
    ).sort("createdAt", -1):
        out.append(_careers_serialize(o))
    return out


@careers_router.get("/openings/{id}")
async def get_public_opening(id: str):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(404, "Opening not found")
    o = await db.job_openings.find_one({"_id": oid, "status": "Open"})
    if not o:
        raise HTTPException(404, "Opening not found")
    return _careers_serialize(o)


# ================= CAREERS — APPLY =================
class CareersApply(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    resumeUrl: Optional[str] = None
    currentCompany: Optional[str] = None
    currentSalary: Optional[float] = None
    expectedSalary: Optional[float] = None
    noticePeriodDays: Optional[int] = None
    coverLetter: Optional[str] = None


@careers_router.post("/openings/{id}/apply")
async def apply_to_opening(id: str, data: CareersApply):
    """Public application — creates a candidate row at stage APPLIED.

    Dedupes on (jobOpeningId, email) so a refresh doesn't create dupes.
    """
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(404, "Opening not found")

    opening = await db.job_openings.find_one(
        {"_id": oid, "status": "Open"}
    )
    if not opening:
        raise HTTPException(404, "Opening not found")

    name = (data.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")

    existing = await db.candidates.find_one({
        "jobOpeningId": id,
        "email": data.email,
    })
    if existing:
        # Don't 4xx — silently update fields the candidate may want to
        # refresh (resume, phone). Status stays where it is.
        await db.candidates.update_one(
            {"_id": existing["_id"]},
            {"$set": {
                "name": name,
                "phone": data.phone or existing.get("phone"),
                "resumeUrl": data.resumeUrl or existing.get("resumeUrl"),
                "currentCompany": (
                    data.currentCompany or existing.get("currentCompany")
                ),
                "currentSalary": (
                    data.currentSalary
                    if data.currentSalary is not None
                    else existing.get("currentSalary")
                ),
                "expectedSalary": (
                    data.expectedSalary
                    if data.expectedSalary is not None
                    else existing.get("expectedSalary")
                ),
                "noticePeriodDays": (
                    data.noticePeriodDays
                    if data.noticePeriodDays is not None
                    else existing.get("noticePeriodDays")
                ),
                "coverLetter": (
                    data.coverLetter or existing.get("coverLetter")
                ),
                "updatedAt": datetime.now(timezone.utc),
            }},
        )
        return {
            "id": str(existing["_id"]),
            "message": "Application updated",
        }

    now = datetime.now(timezone.utc)
    doc = {
        "name": name,
        "email": data.email,
        "phone": data.phone,
        "jobOpeningId": id,
        "resumeUrl": data.resumeUrl,
        "currentCompany": data.currentCompany,
        "currentSalary": data.currentSalary,
        "expectedSalary": data.expectedSalary,
        "noticePeriodDays": data.noticePeriodDays,
        "coverLetter": data.coverLetter,
        "source": "Website",
        "stage": "APPLIED",
        "stageHistory": [{
            "stage": "APPLIED",
            "at": now,
            "actorId": None,
            "note": "Applied via public careers page",
        }],
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.candidates.insert_one(doc)
    await log_audit(
        actor_id=None,
        action="candidate.apply_public",
        entity_type="candidates",
        entity_id=str(result.inserted_id),
        after={
            "jobOpeningId": id,
            "email": data.email,
        },
    )
    return {
        "id": str(result.inserted_id),
        "message": "Application received",
    }


# ================= PUBLIC OFFER ACCEPT =================
class OfferResponse(BaseModel):
    note: Optional[str] = ""


def _offer_public_view(offer: dict, candidate: Optional[dict]) -> dict:
    """Sanitized view shown on the candidate-facing accept page."""
    return {
        "candidateName": candidate.get("name") if candidate else None,
        "candidateEmail": candidate.get("email") if candidate else None,
        "position": offer.get("position"),
        "annualCtc": offer.get("annualCtc"),
        "joiningDate": offer.get("joiningDate"),
        "validUntil": offer.get("validUntil"),
        "notes": offer.get("notes"),
        "salaryBreakdown": offer.get("salaryBreakdown"),
        "company": COMPANY_NAME,
        "status": offer.get("status"),
    }


async def _load_offer_by_token(token: str) -> tuple[dict, Optional[dict]]:
    if not token:
        raise HTTPException(404, "Offer not found")
    offer = await db.offers.find_one({"publicToken": token})
    if not offer:
        raise HTTPException(404, "Offer not found")
    candidate = None
    try:
        candidate = await db.offers.database.candidates.find_one(
            {"_id": ObjectId(offer["candidateId"])}
        )
    except (InvalidId, TypeError, KeyError):
        candidate = None
    return offer, candidate


@offers_public_router.get("/{token}")
async def get_offer_public(token: str):
    offer, candidate = await _load_offer_by_token(token)
    return _offer_public_view(offer, candidate)


async def _record_candidate_response(
    offer: dict,
    candidate: Optional[dict],
    outcome: Literal["ACCEPTED", "REJECTED"],
    note: str,
) -> None:
    now = datetime.now(timezone.utc)
    new_status = outcome
    await db.offers.update_one(
        {"_id": offer["_id"]},
        {"$set": {
            "status": new_status,
            "decidedAt": now,
            "decisionNote": note or "",
            "decidedBy": "candidate",
            "updatedAt": now,
        }},
    )

    if candidate:
        next_stage = "HIRED" if outcome == "ACCEPTED" else "REJECTED"
        await db.candidates.update_one(
            {"_id": candidate["_id"]},
            {
                "$set": {"stage": next_stage, "updatedAt": now},
                "$push": {
                    "stageHistory": {
                        "stage": next_stage,
                        "at": now,
                        "actorId": None,
                        "note": (
                            note
                            or f"Candidate {outcome.lower()} the offer "
                            "via public link"
                        ),
                    }
                },
            },
        )

    # Notify the HR user who created the offer (if recoverable).
    actor_id = offer.get("createdBy")
    if actor_id:
        await notify_user(
            actor_id,
            "offer_response",
            f"Offer {outcome.lower()} by candidate",
            (candidate.get("name") if candidate else "Candidate")
            + f": {note or ''}",
            {"offerId": str(offer["_id"]), "outcome": outcome},
        )

    await log_audit(
        actor_id=None,
        action=f"offer.public_{outcome.lower()}",
        entity_type="offers",
        entity_id=str(offer["_id"]),
    )


@offers_public_router.post("/{token}/accept")
async def accept_offer_public(token: str, data: OfferResponse):
    offer, candidate = await _load_offer_by_token(token)
    if offer.get("status") != "SENT":
        raise HTTPException(
            400, f"Offer is in status {offer.get('status')} — cannot accept",
        )

    # Reject expired offers based on validUntil
    valid_until = offer.get("validUntil")
    if valid_until:
        try:
            vu = datetime.strptime(valid_until, "%Y-%m-%d")
            if datetime.now() > vu:
                await db.offers.update_one(
                    {"_id": offer["_id"]},
                    {"$set": {
                        "status": "EXPIRED",
                        "updatedAt": datetime.now(timezone.utc),
                    }},
                )
                raise HTTPException(410, "This offer has expired")
        except ValueError:
            pass

    await _record_candidate_response(
        offer, candidate, "ACCEPTED", data.note or "",
    )
    return {"message": "Offer accepted"}


@offers_public_router.post("/{token}/reject")
async def reject_offer_public(token: str, data: OfferResponse):
    offer, candidate = await _load_offer_by_token(token)
    if offer.get("status") != "SENT":
        raise HTTPException(
            400, f"Offer is in status {offer.get('status')} — cannot reject",
        )

    await _record_candidate_response(
        offer, candidate, "REJECTED", data.note or "",
    )
    return {"message": "Offer rejected"}


# ================= TOKEN ISSUANCE (called by HR send-offer) =================
async def ensure_offer_public_token(offer: dict) -> str:
    """Returns the existing publicToken or mints a new one. Used by the
    HR send-offer flow to embed a link in the email."""
    existing = offer.get("publicToken")
    if existing:
        return existing
    token = token_urlsafe(32)
    await db.offers.update_one(
        {"_id": offer["_id"]},
        {"$set": {"publicToken": token}},
    )
    return token
