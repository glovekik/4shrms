"""Employee ID card (badge) issuance.

Flow, per product spec:

    employee uploads a photo  ->  status PENDING  (card stays hidden)
    HR approves               ->  status APPROVED (full card is rendered)
    HR rejects                ->  status REJECTED (reason shown, re-upload)
    employee re-uploads       ->  status PENDING  again (card hidden again)

So the card is ONLY ever shown while status == APPROVED. State lives as one
document per user in the `id_cards` collection, keyed by userId.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from typing import Optional

from database import db
from utils.ist import now_ist_naive, iso_naive
from utils.dependencies import get_current_user, require_hr
from utils.notify import create_notification, notify_hr
from utils.push import push_to_user
from utils.audit import log_audit
from models.id_card import IDCardPhotoSubmit, IDCardReject, IDCardFraming
from utils.face_frame import auto_framing_for_url, DEFAULT_FRAMING


router = APIRouter()      # employee-facing, mounted at /id-card
hr_router = APIRouter()   # HR-facing, mounted at /hr


NONE = "NONE"
PENDING = "PENDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"


def _serialize(doc: Optional[dict]) -> dict:
    """Public shape of an ID-card record. A missing doc means 'never submitted'."""
    if not doc:
        return {
            "status": NONE,
            "photoUrl": None,
            "submittedAt": None,
            "reviewedAt": None,
            "reviewedBy": None,
            "rejectionReason": None,
            "framing": {"zoom": 1.0, "offsetX": 0.0, "offsetY": 0.0},
            "autoFramed": False,
        }
    return {
        "status": doc.get("status", NONE),
        "photoUrl": doc.get("photoUrl"),
        "submittedAt": iso_naive(doc.get("submittedAt")),
        "reviewedAt": iso_naive(doc.get("reviewedAt")),
        "reviewedBy": doc.get("reviewedBy"),
        "rejectionReason": doc.get("rejectionReason"),
        "framing": doc.get("framing")
        or {"zoom": 1.0, "offsetX": 0.0, "offsetY": 0.0},
        "autoFramed": bool(doc.get("autoFramed")),
    }


async def _user_brief(user_id: str) -> Optional[dict]:
    try:
        u = await db.users.find_one(
            {"_id": ObjectId(user_id)},
            {"name": 1, "email": 1, "employeeCode": 1, "profilePictureUrl": 1},
        )
    except (InvalidId, TypeError):
        return None
    if not u:
        return None
    return {
        "id": str(u["_id"]),
        "name": u.get("name"),
        "email": u.get("email"),
        "employeeCode": u.get("employeeCode"),
        "profilePictureUrl": u.get("profilePictureUrl"),
    }


# ===================== EMPLOYEE =====================

@router.get("/me")
async def get_my_id_card(user_id: str = Depends(get_current_user)):
    """Current ID-card state for the logged-in employee."""
    doc = await db.id_cards.find_one({"userId": user_id})
    return _serialize(doc)


@router.post("/photo")
async def submit_id_card_photo(
    payload: IDCardPhotoSubmit,
    user_id: str = Depends(get_current_user),
):
    """Submit / re-submit the ID-card photo. Always lands in PENDING, which
    hides the card until HR approves — including for a re-upload of an
    already-approved card."""
    photo = (payload.photoUrl or "").strip()
    if not photo:
        raise HTTPException(status_code=400, detail="A photo is required.")

    # Find the face and compose the photo to badge standard so the employee
    # doesn't have to. Falls back to an untouched frame if OpenCV is missing,
    # the file can't be read, or no face is found — never blocks the upload.
    try:
        framing = auto_framing_for_url(photo) or DEFAULT_FRAMING
    except Exception:
        framing = DEFAULT_FRAMING

    now = now_ist_naive()
    await db.id_cards.update_one(
        {"userId": user_id},
        {
            "$set": {
                "userId": user_id,
                "photoUrl": photo,
                "status": PENDING,
                "submittedAt": now,
                "reviewedAt": None,
                "reviewedBy": None,
                "rejectionReason": None,
                # Auto-composed from the detected face (default if none found).
                "framing": framing,
                "autoFramed": framing is not DEFAULT_FRAMING,
            }
        },
        upsert=True,
    )

    who = await _user_brief(user_id)
    name = (who or {}).get("name") or "An employee"
    await notify_hr(
        "id_card_request",
        "ID card photo submitted",
        f"{name} submitted a photo for ID card approval.",
        {"userId": user_id},
    )

    await log_audit(
        actor_id=user_id,
        action="id_card.submit",
        entity_type="id_cards",
        entity_id=user_id,
    )

    doc = await db.id_cards.find_one({"userId": user_id})
    return _serialize(doc)


@router.post("/framing")
async def set_my_id_card_framing(
    payload: IDCardFraming,
    user_id: str = Depends(get_current_user),
):
    """Reposition / zoom the photo inside the card frame.

    Per product decision, re-framing an ALREADY-APPROVED card sends it back to
    PENDING — HR approves the framing they'll actually print, not just the file.
    While it's still pending or rejected, adjusting is free and doesn't re-notify.
    """
    doc = await db.id_cards.find_one({"userId": user_id})
    if not doc or not doc.get("photoUrl"):
        raise HTTPException(status_code=404, detail="Upload a photo first.")

    framing = {
        "zoom": payload.zoom,
        "offsetX": payload.offsetX,
        "offsetY": payload.offsetY,
    }
    was_approved = doc.get("status") == APPROVED
    update: dict = {"framing": framing}

    if was_approved:
        update.update({
            "status": PENDING,
            "submittedAt": now_ist_naive(),
            "reviewedAt": None,
            "reviewedBy": None,
            "rejectionReason": None,
        })

    await db.id_cards.update_one({"userId": user_id}, {"$set": update})

    # Only bother HR when this actually re-enters their queue.
    if was_approved:
        who = await _user_brief(user_id)
        name = (who or {}).get("name") or "An employee"
        await notify_hr(
            "id_card_request",
            "ID card photo re-framed",
            f"{name} changed their ID card photo framing — needs approval again.",
            {"userId": user_id},
        )

    await log_audit(
        actor_id=user_id,
        action="id_card.reframe",
        entity_type="id_cards",
        entity_id=user_id,
        metadata={"reopened": was_approved},
    )

    return _serialize(await db.id_cards.find_one({"userId": user_id}))


# ===================== HR =====================

@hr_router.post("/id-cards/{user_id}/framing")
async def hr_set_id_card_framing(
    user_id: str,
    payload: IDCardFraming,
    hr: dict = Depends(require_hr),
):
    """HR nudges a badly-framed photo during review, then approves — avoids a
    reject-and-resubmit round trip. Deliberately does NOT change status."""
    doc = await db.id_cards.find_one({"userId": user_id})
    if not doc or not doc.get("photoUrl"):
        raise HTTPException(status_code=404, detail="No photo to adjust.")

    await db.id_cards.update_one(
        {"userId": user_id},
        {
            "$set": {
                "framing": {
                    "zoom": payload.zoom,
                    "offsetX": payload.offsetX,
                    "offsetY": payload.offsetY,
                }
            }
        },
    )

    await log_audit(
        actor_id=str(hr["_id"]),
        action="id_card.reframe_by_hr",
        entity_type="id_cards",
        entity_id=user_id,
    )

    return _serialize(await db.id_cards.find_one({"userId": user_id}))


@hr_router.get("/id-cards")
async def list_id_cards(
    status: Optional[str] = Query(None, description="PENDING / APPROVED / REJECTED"),
    hr: dict = Depends(require_hr),
):
    """ID-card requests for HR review. Defaults to everything; pass
    ?status=PENDING for the approval queue."""
    q: dict = {}
    if status:
        q["status"] = status.upper()

    out = []
    async for doc in db.id_cards.find(q).sort("submittedAt", -1):
        item = _serialize(doc)
        item["userId"] = doc.get("userId")
        item["user"] = await _user_brief(doc.get("userId"))
        out.append(item)
    return out


@hr_router.post("/id-cards/{user_id}/approve")
async def approve_id_card(user_id: str, hr: dict = Depends(require_hr)):
    """Approve the submitted photo — the employee's card becomes visible."""
    doc = await db.id_cards.find_one({"userId": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="No ID card request found.")
    if doc.get("status") == APPROVED:
        raise HTTPException(status_code=409, detail="Already approved.")

    await db.id_cards.update_one(
        {"userId": user_id},
        {
            "$set": {
                "status": APPROVED,
                "reviewedAt": now_ist_naive(),
                "reviewedBy": str(hr["_id"]),
                "rejectionReason": None,
            }
        },
    )

    title = "ID card approved"
    body = "Your ID card photo was approved — your card is now available."
    try:
        await push_to_user(user_id, title, body, {"type": "id_card_decision"})
    except Exception:
        pass
    await create_notification(
        user_id,
        "id_card_decision",
        title,
        body,
        {"outcome": APPROVED},
    )

    await log_audit(
        actor_id=str(hr["_id"]),
        action="id_card.approve",
        entity_type="id_cards",
        entity_id=user_id,
    )

    return _serialize(await db.id_cards.find_one({"userId": user_id}))


@hr_router.post("/id-cards/{user_id}/reject")
async def reject_id_card(
    user_id: str,
    payload: IDCardReject,
    hr: dict = Depends(require_hr),
):
    """Reject the submitted photo. The employee sees the reason and re-uploads."""
    doc = await db.id_cards.find_one({"userId": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="No ID card request found.")

    reason = (payload.reason or "").strip() or "Photo did not meet requirements."

    await db.id_cards.update_one(
        {"userId": user_id},
        {
            "$set": {
                "status": REJECTED,
                "reviewedAt": now_ist_naive(),
                "reviewedBy": str(hr["_id"]),
                "rejectionReason": reason,
            }
        },
    )

    title = "ID card photo rejected"
    body = reason
    try:
        await push_to_user(user_id, title, body, {"type": "id_card_decision"})
    except Exception:
        pass
    await create_notification(
        user_id,
        "id_card_decision",
        title,
        body,
        {"outcome": REJECTED, "reason": reason},
    )

    await log_audit(
        actor_id=str(hr["_id"]),
        action="id_card.reject",
        entity_type="id_cards",
        entity_id=user_id,
        metadata={"reason": reason},
    )

    return _serialize(await db.id_cards.find_one({"userId": user_id}))


@hr_router.get("/id-cards/{user_id}")
async def get_user_id_card(user_id: str, hr: dict = Depends(require_hr)):
    """State for one employee — used when HR opens their profile / card."""
    doc = await db.id_cards.find_one({"userId": user_id})
    item = _serialize(doc)
    item["userId"] = user_id
    item["user"] = await _user_brief(user_id)
    return item
