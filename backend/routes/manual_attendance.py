"""Manual attendance request workflow.

Per PRD: employees can no longer add attendance manually — they raise a
request that a Manager OR HR can approve. On approve, a regular row is
inserted into the `attendance` collection with autoApprovedFromRequest=true
so downstream reports treat it as a normal day.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone, date
from typing import Optional

from database import db
from utils.dependencies import (
    get_current_user,
    require_hr,
    require_manager_or_hr,
    can_decide_for_employee,
)
from utils.audit import log_audit
from utils.notify import create_notification
from utils.push import push_to_user
from models.manual_attendance import (
    ManualAttendanceCreate,
    ManualAttendanceDecision,
)


# /attendance/manual-request[/...]    — employee
user_router = APIRouter()
# /manager/manual-requests[/...]      — manager + HR (own-reports scope)
manager_router = APIRouter()
# /hr/manual-requests[/...]           — HR (all)
hr_router = APIRouter()


def _serialize(r: dict, user_info: Optional[dict] = None) -> dict:
    return {
        "id": str(r["_id"]),
        "userId": r.get("userId"),
        "user": user_info,
        "date": r.get("date"),
        "checkIn": (
            r["checkIn"].isoformat()
            if isinstance(r.get("checkIn"), datetime)
            else r.get("checkIn")
        ),
        "checkOut": (
            r["checkOut"].isoformat()
            if isinstance(r.get("checkOut"), datetime)
            else r.get("checkOut")
        ),
        "reason": r.get("reason"),
        "status": r.get("status"),
        "decidedBy": r.get("decidedBy"),
        "decidedByRole": r.get("decidedByRole"),
        "decisionNote": r.get("decisionNote", ""),
        "decidedAt": (
            r["decidedAt"].isoformat()
            if r.get("decidedAt") else None
        ),
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
    out: dict = {}
    async for u in db.users.find({"_id": {"$in": oids}}):
        out[str(u["_id"])] = {
            "id": str(u["_id"]),
            "name": u.get("name"),
            "email": u.get("email"),
        }
    return out


def _parse_iso(value: str, field: str) -> datetime:
    s = value
    if isinstance(s, str) and s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        raise HTTPException(400, f"Invalid {field} (ISO 8601 required)")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ================= EMPLOYEE: SUBMIT =================
@user_router.post("")
async def submit_manual_request(
    data: ManualAttendanceCreate,
    user_id: str = Depends(get_current_user),
):
    try:
        date.fromisoformat(data.date)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid date (YYYY-MM-DD)")

    check_in = _parse_iso(data.checkIn, "checkIn")
    check_out = (
        _parse_iso(data.checkOut, "checkOut")
        if data.checkOut else None
    )
    if check_out and check_out < check_in:
        raise HTTPException(400, "checkOut cannot be before checkIn")

    reason = (data.reason or "").strip()
    if not reason:
        raise HTTPException(400, "Reason is required")

    # Block duplicate pending request for the same date.
    existing = await db.manual_attendance_requests.find_one({
        "userId": user_id,
        "date": data.date,
        "status": "PENDING",
    })
    if existing:
        raise HTTPException(
            400,
            f"You already have a pending request for {data.date}",
        )

    # Block if an attendance row already exists — request should be a
    # correction in that case.
    attendance_exists = await db.attendance.find_one({
        "userId": user_id,
        "date": data.date,
    })
    if attendance_exists:
        raise HTTPException(
            400,
            "Attendance already exists for that date — raise a "
            "correction request instead.",
        )

    now = datetime.now(timezone.utc)
    doc = {
        "userId": user_id,
        "date": data.date,
        "checkIn": check_in,
        "checkOut": check_out,
        "reason": reason,
        "status": "PENDING",
        "decisionNote": "",
        "decidedBy": None,
        "decidedByRole": None,
        "decidedAt": None,
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.manual_attendance_requests.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize(doc)


# ================= EMPLOYEE: MINE =================
@user_router.get("/mine")
async def my_manual_requests(
    status: Optional[str] = Query(None),
    user_id: str = Depends(get_current_user),
):
    query: dict = {"userId": user_id}
    if status:
        query["status"] = status
    out = []
    async for r in db.manual_attendance_requests.find(
        query
    ).sort("createdAt", -1):
        out.append(_serialize(r))
    return out


# ================= EMPLOYEE: CANCEL =================
@user_router.post("/{id}/cancel")
async def cancel_my_manual_request(
    id: str,
    user_id: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    r = await db.manual_attendance_requests.find_one({"_id": oid})
    if not r:
        raise HTTPException(404, "Request not found")
    if r.get("userId") != user_id:
        raise HTTPException(403, "Not your request")
    if r.get("status") != "PENDING":
        raise HTTPException(
            400, f"Cannot cancel a {r.get('status')} request",
        )
    await db.manual_attendance_requests.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": "CANCELLED",
                "updatedAt": datetime.now(timezone.utc),
            }
        },
    )
    return {"message": "Request cancelled"}


# ================= MANAGER / HR: LIST + DECIDE =================
async def _list_for_actor(
    actor: dict,
    status: Optional[str],
) -> list[dict]:
    actor_id = str(actor["_id"])
    is_hr = actor.get("role") == "HR"

    if is_hr:
        scope_user_ids = None
    else:
        scope_user_ids = [
            str(u["_id"])
            async for u in db.users.find(
                {"reportingManagerId": actor_id}, {"_id": 1}
            )
        ]
        if not scope_user_ids:
            return []

    query: dict = {}
    if status:
        query["status"] = status
    if scope_user_ids is not None:
        query["userId"] = {"$in": scope_user_ids}

    raw: list[dict] = []
    async for r in db.manual_attendance_requests.find(
        query
    ).sort("createdAt", -1):
        raw.append(r)
    user_map = await _user_basics(r.get("userId") for r in raw)
    return [_serialize(r, user_map.get(r.get("userId"))) for r in raw]


@manager_router.get("")
async def manager_list_manual_requests(
    status: Optional[str] = Query("PENDING"),
    actor: dict = Depends(require_manager_or_hr),
):
    return await _list_for_actor(actor, status)


@hr_router.get("")
async def hr_list_manual_requests(
    status: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    raw: list[dict] = []
    query: dict = {}
    if status:
        query["status"] = status
    async for r in db.manual_attendance_requests.find(
        query
    ).sort("createdAt", -1):
        raw.append(r)
    user_map = await _user_basics(r.get("userId") for r in raw)
    return [_serialize(r, user_map.get(r.get("userId"))) for r in raw]


async def _decide(
    id: str,
    data: ManualAttendanceDecision,
    actor: dict,
    require_report_scope: bool,
) -> dict:
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    r = await db.manual_attendance_requests.find_one({"_id": oid})
    if not r:
        raise HTTPException(404, "Request not found")
    if r.get("status") != "PENDING":
        raise HTTPException(400, f"Already {r.get('status')}")

    if require_report_scope:
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
                403,
                "This request is not from one of your direct reports",
            )

    now = datetime.now(timezone.utc)
    actor_id = str(actor["_id"])
    actor_role = actor.get("role", "HR")
    new_status = "APPROVED" if data.action == "APPROVE" else "REJECTED"

    await db.manual_attendance_requests.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": new_status,
                "decidedBy": actor_id,
                "decidedByRole": actor_role,
                "decisionNote": data.note or "",
                "decidedAt": now,
                "updatedAt": now,
            }
        },
    )

    if data.action == "APPROVE":
        # Race-safe: only insert if no attendance row exists for that date.
        existing = await db.attendance.find_one({
            "userId": r["userId"],
            "date": r["date"],
        })
        if not existing:
            attendance_doc = {
                "userId": r["userId"],
                "date": r["date"],
                "attendanceType": "MANUAL",
                "status": (
                    "COMPLETED" if r.get("checkOut") else "CHECKED_IN"
                ),
                "checkIn": r.get("checkIn"),
                "checkOut": r.get("checkOut"),
                "workNotes": r.get("reason", ""),
                "autoApprovedFromRequest": True,
                "manualRequestId": str(oid),
                "createdAt": now,
                "updatedAt": now,
            }
            await db.attendance.insert_one(attendance_doc)

    try:
        await push_to_user(
            r["userId"],
            f"Manual attendance {new_status.lower()}",
            data.note or r.get("reason", ""),
            {
                "type": "manual_attendance_decision",
                "requestId": id,
            },
        )
    except Exception:
        pass

    await create_notification(
        r["userId"],
        "manual_attendance_decision",
        f"Manual attendance {new_status.lower()}",
        data.note or f"Date: {r.get('date')}",
        {
            "requestId": id,
            "outcome": new_status,
            "decidedByRole": actor_role,
        },
    )

    await log_audit(
        actor_id=actor_id,
        action=f"manual_attendance.{data.action.lower()}",
        entity_type="manual_attendance_requests",
        entity_id=id,
        after={"decidedByRole": actor_role},
    )

    return {"message": f"Manual attendance {new_status.lower()}"}


@manager_router.post("/{id}/decide")
async def manager_decide_manual_request(
    id: str,
    data: ManualAttendanceDecision,
    actor: dict = Depends(require_manager_or_hr),
):
    # HR uses the same endpoint and bypasses the report-scope check.
    require_scope = actor.get("role") != "HR"
    return await _decide(id, data, actor, require_scope)


@hr_router.post("/{id}/decide")
async def hr_decide_manual_request(
    id: str,
    data: ManualAttendanceDecision,
    actor: dict = Depends(require_hr),
):
    return await _decide(id, data, actor, require_report_scope=False)
