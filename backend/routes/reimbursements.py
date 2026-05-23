"""Two-step reimbursement flow per PRD section 24.2:
Employee → Manager → HR (final).

Status transitions:
    PENDING_MANAGER → PENDING_HR | REJECTED
    PENDING_HR      → APPROVED   | REJECTED
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from database import db
from utils.dependencies import (
    get_current_user,
    get_current_user_doc,
    require_hr,
    require_manager_or_hr,
    can_decide_for_employee,
)
from utils.audit import log_audit
from utils.notify import notify_user
from models.reimbursement import (
    ReimbursementCreate,
    ReimbursementDecision,
)


user_router = APIRouter()      # /expenses/reimbursements/...
manager_router = APIRouter()   # /manager/reimbursements/...
hr_router = APIRouter()        # /hr/reimbursements/...


def _serialize(r: dict, user_info: Optional[dict] = None) -> dict:
    return {
        "id": str(r["_id"]),
        "userId": r.get("userId"),
        "user": user_info,
        "title": r.get("title"),
        "category": r.get("category"),
        "expenseDate": r.get("expenseDate"),
        "amount": r.get("amount"),
        "paymentMode": r.get("paymentMode"),
        "vendorName": r.get("vendorName"),
        "invoiceNumber": r.get("invoiceNumber"),
        "taxAmount": r.get("taxAmount"),
        "description": r.get("description"),
        "attachments": r.get("attachments", []),
        "status": r.get("status"),
        "managerDecision": r.get("managerDecision"),
        "managerNote": r.get("managerNote"),
        "managerDecidedBy": r.get("managerDecidedBy"),
        "managerDecidedAt": (
            r["managerDecidedAt"].isoformat()
            if r.get("managerDecidedAt") else None
        ),
        "hrDecision": r.get("hrDecision"),
        "hrNote": r.get("hrNote"),
        "hrDecidedBy": r.get("hrDecidedBy"),
        "hrDecidedAt": (
            r["hrDecidedAt"].isoformat()
            if r.get("hrDecidedAt") else None
        ),
        # Front-end expects camelCase decidedAt fields per the Reimbursement type.
        "decidedByManagerAt": (
            r["managerDecidedAt"].isoformat()
            if r.get("managerDecidedAt") else None
        ),
        "decidedByHrAt": (
            r["hrDecidedAt"].isoformat()
            if r.get("hrDecidedAt") else None
        ),
        "decisionNote": r.get("hrNote") or r.get("managerNote") or "",
        "createdAt": (
            r["createdAt"].isoformat()
            if r.get("createdAt") else None
        ),
    }


async def _user_basics(user_ids) -> dict:
    unique = {uid for uid in user_ids if uid}
    if not unique:
        return {}
    oids = []
    for uid in unique:
        try:
            oids.append(ObjectId(uid))
        except (InvalidId, TypeError):
            continue
    if not oids:
        return {}
    out: dict = {}
    async for u in db.users.find({"_id": {"$in": oids}}):
        out[str(u["_id"])] = {
            "id": str(u["_id"]),
            "name": u.get("name"),
            "email": u.get("email"),
        }
    return out


# ================= EMPLOYEE: SUBMIT / LIST =================
@user_router.post("")
async def create_reimbursement(
    data: ReimbursementCreate,
    user_id: str = Depends(get_current_user),
):
    if data.amount is None or data.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    if not (data.title or "").strip():
        raise HTTPException(400, "title is required")

    # If the submitter has no reportingManager assigned, skip the manager
    # stage entirely so HR can see and decide the request — otherwise it
    # gets stuck in PENDING_MANAGER with no one able to act on it.
    initial_status = "PENDING_MANAGER"
    try:
        submitter = await db.users.find_one(
            {"_id": ObjectId(user_id)},
            {"reportingManagerId": 1},
        )
    except (InvalidId, TypeError):
        submitter = None
    if not (submitter and submitter.get("reportingManagerId")):
        initial_status = "PENDING_HR"

    now = datetime.now(timezone.utc)
    doc = {
        "userId": user_id,
        "title": data.title.strip(),
        "category": data.category,
        "expenseDate": data.expenseDate,
        "amount": data.amount,
        "paymentMode": data.paymentMode,
        "vendorName": data.vendorName,
        "invoiceNumber": data.invoiceNumber,
        "taxAmount": data.taxAmount,
        "description": data.description or "",
        "attachments": data.attachments or [],
        "status": initial_status,
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.reimbursement_requests.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize(doc)


@user_router.get("/mine")
async def my_reimbursements(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user),
):
    query: dict = {"userId": user_id}
    if status:
        query["status"] = status
    out = []
    async for r in db.reimbursement_requests.find(query).sort(
        "createdAt", -1
    ).limit(limit):
        out.append(_serialize(r))
    return out


# ================= MANAGER: LIST + DECIDE =================
@manager_router.get("")
async def manager_list_reimbursements(
    status: Optional[str] = Query("PENDING_MANAGER"),
    actor: dict = Depends(require_manager_or_hr),
):
    actor_id = str(actor["_id"])
    if actor.get("role") == "HR":
        report_ids = None
    else:
        report_ids = [
            str(u["_id"])
            async for u in db.users.find(
                {"reportingManagerId": actor_id}, {"_id": 1}
            )
        ]
        if not report_ids:
            return []

    query: dict = {}
    if status:
        query["status"] = status
    if report_ids is not None:
        query["userId"] = {"$in": report_ids}

    raw: list[dict] = []
    async for r in db.reimbursement_requests.find(query).sort(
        "createdAt", -1
    ):
        raw.append(r)
    user_map = await _user_basics(r.get("userId") for r in raw)
    return [
        _serialize(r, user_map.get(r.get("userId")))
        for r in raw
    ]


@manager_router.post("/{id}/decide")
async def manager_decide_reimbursement(
    id: str,
    data: ReimbursementDecision,
    actor: dict = Depends(require_manager_or_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    r = await db.reimbursement_requests.find_one({"_id": oid})
    if not r:
        raise HTTPException(404, "Reimbursement not found")
    if r.get("status") != "PENDING_MANAGER":
        raise HTTPException(
            400, f"Cannot manager-decide in state {r.get('status')}",
        )

    try:
        employee = await db.users.find_one(
            {"_id": ObjectId(r["userId"])}
        )
    except (InvalidId, TypeError, KeyError):
        employee = None
    if not employee:
        raise HTTPException(404, "Requester no longer exists")
    if not can_decide_for_employee(actor, employee):
        raise HTTPException(
            403, "Not one of your direct reports",
        )

    now = datetime.now(timezone.utc)
    actor_id = str(actor["_id"])

    if data.action == "APPROVE":
        new_status = "PENDING_HR"
    else:
        new_status = "REJECTED"

    await db.reimbursement_requests.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": new_status,
                "managerDecision": data.action,
                "managerNote": data.note or "",
                "managerDecidedBy": actor_id,
                "managerDecidedAt": now,
                "updatedAt": now,
            }
        },
    )

    title = (
        "Reimbursement forwarded to HR"
        if data.action == "APPROVE"
        else "Reimbursement rejected by manager"
    )
    await notify_user(
        r["userId"],
        "reimbursement_decision",
        title,
        data.note or r.get("title", ""),
        {"requestId": id, "stage": "MANAGER", "outcome": data.action},
    )
    await log_audit(
        actor_id=actor_id,
        action=f"reimbursement.manager_{data.action.lower()}",
        entity_type="reimbursement_requests",
        entity_id=id,
    )
    return {"message": f"Reimbursement {data.action.lower()} by manager"}


# ================= HR: LIST + FINAL DECIDE =================
@hr_router.get("")
async def hr_list_reimbursements(
    status: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    """HR sees every reimbursement, optionally filtered by status. Default
    is unfiltered so manager-pending requests are also visible — HR can
    intervene when a manager is on leave or unassigned."""
    query: dict = {}
    if status:
        query["status"] = status
    raw: list[dict] = []
    async for r in db.reimbursement_requests.find(query).sort(
        "createdAt", -1
    ):
        raw.append(r)
    user_map = await _user_basics(r.get("userId") for r in raw)
    return [
        _serialize(r, user_map.get(r.get("userId")))
        for r in raw
    ]


@hr_router.post("/{id}/decide")
async def hr_decide_reimbursement(
    id: str,
    data: ReimbursementDecision,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    r = await db.reimbursement_requests.find_one({"_id": oid})
    if not r:
        raise HTTPException(404, "Reimbursement not found")
    # HR can act on anything that's still open — including manager-pending
    # requests where the manager is unassigned or unavailable.
    if r.get("status") not in ("PENDING_HR", "PENDING_MANAGER"):
        raise HTTPException(
            400, f"Cannot HR-decide in state {r.get('status')}",
        )

    now = datetime.now(timezone.utc)
    hr_id = str(hr["_id"])
    new_status = "APPROVED" if data.action == "APPROVE" else "REJECTED"

    await db.reimbursement_requests.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": new_status,
                "hrDecision": data.action,
                "hrNote": data.note or "",
                "hrDecidedBy": hr_id,
                "hrDecidedAt": now,
                "updatedAt": now,
            }
        },
    )

    title = (
        "Reimbursement approved"
        if data.action == "APPROVE"
        else "Reimbursement rejected by HR"
    )
    await notify_user(
        r["userId"],
        "reimbursement_decision",
        title,
        data.note or r.get("title", ""),
        {"requestId": id, "stage": "HR", "outcome": data.action},
    )
    await log_audit(
        actor_id=hr_id,
        action=f"reimbursement.hr_{data.action.lower()}",
        entity_type="reimbursement_requests",
        entity_id=id,
    )
    return {"message": f"Reimbursement {new_status.lower()}"}
