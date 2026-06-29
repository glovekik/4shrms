from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone

from typing import Optional

from config import COMPANY_NAME
from database import db
from utils.dependencies import (
    get_current_user,
    get_current_user_doc,
    require_hr,
    require_hr_or_ceo,
    require_manager_or_hr,
    can_decide_for_employee,
)
from utils.email import send_notification_email
from utils.push import push_to_user
from utils.audit import log_audit
from utils.notify import create_notification, notify_approvers
from models.correction import (
    CorrectionRequestCreate,
    CorrectionDecision,
    CorrectionBulkDecision,
)


async def _notify_requester(
    user_id: str,
    decision: str,
    request_id: str,
    note: str,
) -> None:
    """Push + in-app + email the requester about a correction decision.
    Never raises."""
    title = f"Attendance correction {decision.lower()}"
    body = note or f"Your correction was {decision.lower()}."

    try:
        await push_to_user(
            user_id,
            title,
            body,
            {"type": "correction_decision", "requestId": request_id},
        )
    except Exception:
        pass

    await create_notification(
        user_id,
        "correction_decision",
        title,
        body,
        {"requestId": request_id, "outcome": decision},
    )

    try:
        u = await db.users.find_one({"_id": ObjectId(user_id)})
    except (InvalidId, TypeError):
        u = None
    if not u or not u.get("email"):
        return

    note_line = f"\n\nNote from HR:\n{note}\n" if note else ""
    await send_notification_email(
        u["email"],
        f"Attendance correction {decision.lower()}",
        (
            f"Hi {u.get('name', 'there')},\n\n"
            f"Your attendance correction request has been {decision}."
            + note_line
            + f"\n\nRegards,\n{COMPANY_NAME}"
        ),
    )

# User-facing endpoints under /attendance/...
user_router = APIRouter()

# HR endpoints under /hr/correction-requests
hr_router = APIRouter()

# Manager endpoints under /manager/correction-requests (HR + direct-report scope)
manager_router = APIRouter()


# ================= SERIALIZER =================
def _serialize(
    r: dict,
    user_info: Optional[dict] = None,
    attendance_info: Optional[dict] = None,
) -> dict:
    # `createdAt` ISO is reused as both `createdAt` and `requestedAt`
    # (the frontend uses the latter for display). Same for
    # `attendanceDate`, which the frontend reads top-level alongside the
    # nested `attendance` object.
    created_iso = (
        r["createdAt"].isoformat() + "Z"
        if r.get("createdAt") else None
    )
    decided_iso = (
        r["decidedAt"].isoformat() + "Z"
        if r.get("decidedAt") else None
    )
    attendance_date = (
        attendance_info.get("date") if attendance_info else None
    )
    return {
        "id": str(r["_id"]),
        "userId": r.get("userId"),
        "user": user_info,
        "attendanceId": r.get("attendanceId"),
        "attendance": attendance_info,
        # Top-level mirrors of nested attendance.date so the client can
        # render `corr.attendanceDate` without a null-check chain.
        "attendanceDate": attendance_date,
        # Requested fields — any subset may be present.
        "requestedDate": r.get("requestedDate"),
        # Anchor as UTC so the client doesn't misinterpret as local time.
        "requestedCheckIn": (
            r["requestedCheckIn"].isoformat() + "Z"
            if r.get("requestedCheckIn") else None
        ),
        "requestedCheckOut": (
            r["requestedCheckOut"].isoformat() + "Z"
            if r.get("requestedCheckOut") else None
        ),
        "requestedAttendanceType": r.get("requestedAttendanceType"),
        "requestedWorkNotes": r.get("requestedWorkNotes"),
        "reason": r.get("reason"),
        "status": r.get("status"),
        "decisionNote": r.get("decisionNote", ""),
        # Frontend types expect `rejectionReason` / `reviewedBy` /
        # `reviewedAt` — alias them off the canonical decision fields so
        # rejections render their note in the history card.
        "rejectionReason": (
            r.get("decisionNote") if r.get("status") == "REJECTED" else None
        ),
        "decidedBy": r.get("decidedBy"),
        "reviewedBy": r.get("decidedBy"),
        "decidedAt": decided_iso,
        "reviewedAt": decided_iso,
        "createdAt": created_iso,
        "requestedAt": created_iso,
    }


# ================= HELPERS =================
def _parse_iso(value: str, field: str) -> datetime:
    s = value
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        raise HTTPException(
            400,
            f"Invalid {field} format (use ISO 8601)",
        )


async def _get_user_basics(user_ids) -> dict:
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
    result = {}
    async for u in db.users.find(
        {"_id": {"$in": oids}}
    ):
        result[str(u["_id"])] = {
            "id": str(u["_id"]),
            "name": u.get("name"),
            "email": u.get("email"),
        }
    return result


async def _get_attendance_summaries(att_ids) -> dict:
    unique = {aid for aid in att_ids if aid}
    if not unique:
        return {}
    oids = []
    for aid in unique:
        try:
            oids.append(ObjectId(aid))
        except (InvalidId, TypeError):
            continue
    if not oids:
        return {}
    result = {}
    async for a in db.attendance.find(
        {"_id": {"$in": oids}}
    ):
        result[str(a["_id"])] = {
            "id": str(a["_id"]),
            "date": a.get("date"),
            "checkIn": (
                a["checkIn"].isoformat()
                if a.get("checkIn") else None
            ),
            "checkOut": (
                a["checkOut"].isoformat()
                if a.get("checkOut") else None
            ),
            "autoClosedByCron": a.get(
                "autoClosedByCron",
                False,
            ),
        }
    return result


# ================= USER: CREATE REQUEST =================
@user_router.post(
    "/{id}/correction-request"
)
async def create_correction_request(
    id: str,
    data: CorrectionRequestCreate,
    user_id: str = Depends(get_current_user),
):

    try:
        att_oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid attendance id")

    attendance = await db.attendance.find_one(
        {"_id": att_oid}
    )

    if not attendance:
        raise HTTPException(
            404,
            "Attendance not found",
        )

    if attendance.get("userId") != user_id:
        raise HTTPException(
            403,
            "Not your attendance record",
        )

    reason = (data.reason or "").strip()
    if not reason:
        raise HTTPException(400, "Reason is required")

    # Parse the optional datetime/date fields up front so a malformed
    # value fails fast (before insert). Date is YYYY-MM-DD; check-in /
    # check-out are ISO 8601.
    requested_date = (data.requestedDate or "").strip() or None
    if requested_date:
        try:
            datetime.strptime(requested_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                400, "requestedDate must be YYYY-MM-DD",
            )

    requested_check_in = (
        _parse_iso(data.requestedCheckIn, "requestedCheckIn")
        if data.requestedCheckIn else None
    )
    requested_check_out = (
        _parse_iso(data.requestedCheckOut, "requestedCheckOut")
        if data.requestedCheckOut else None
    )
    if (
        requested_check_in
        and requested_check_out
        and requested_check_out < requested_check_in
    ):
        raise HTTPException(
            400, "Requested check-out cannot be before check-in",
        )

    requested_type = data.requestedAttendanceType or None
    requested_notes = (
        (data.requestedWorkNotes or "").strip()
        if data.requestedWorkNotes is not None else None
    )

    # At least one editable field must differ from the record — empty
    # corrections are noise on the HR queue.
    has_any_change = any([
        requested_date,
        requested_check_in,
        requested_check_out,
        requested_type,
        requested_notes is not None and requested_notes != attendance.get("workNotes", ""),
    ])
    if not has_any_change:
        raise HTTPException(
            400,
            "Request at least one change (date, check-in, check-out, type, or notes).",
        )

    # Block duplicate pending requests for the same attendance.
    existing = await db.correction_requests.find_one({
        "attendanceId": id,
        "userId": user_id,
        "status": "PENDING",
    })

    if existing:
        raise HTTPException(
            400,
            "A pending correction request already exists for this record",
        )

    now = datetime.now(timezone.utc)

    doc = {
        "userId": user_id,
        "attendanceId": id,
        "requestedDate": requested_date,
        "requestedCheckIn": requested_check_in,
        "requestedCheckOut": requested_check_out,
        "requestedAttendanceType": requested_type,
        "requestedWorkNotes": requested_notes,
        "reason": reason,
        "status": "PENDING",
        "decisionNote": "",
        "decidedBy": None,
        "decidedAt": None,
        "createdAt": now,
        "updatedAt": now,
    }

    result = await db.correction_requests.insert_one(doc)
    doc["_id"] = result.inserted_id

    # Notify approvers (reporting manager + HR) of the pending correction.
    who_doc = await db.users.find_one({"_id": ObjectId(user_id)}, {"name": 1})
    who = (who_doc or {}).get("name") or "An employee"
    await notify_approvers(
        user_id,
        "correction_requests",
        "New attendance correction",
        f"{who} requested a correction for {attendance.get('date', 'a record')}",
        {"correctionId": str(result.inserted_id), "attendanceId": id},
    )

    return _serialize(doc)


# ================= USER: LIST OWN =================
@user_router.get("/correction-requests/mine")
async def list_my_correction_requests(
    status: Optional[str] = Query(None),
    user_id: str = Depends(get_current_user),
):

    query: dict = {"userId": user_id}

    if status:
        query["status"] = status

    raw = []

    async for r in db.correction_requests.find(
        query
    ).sort("createdAt", -1):
        raw.append(r)

    att_map = await _get_attendance_summaries(
        r.get("attendanceId") for r in raw
    )

    return [
        _serialize(
            r,
            None,
            att_map.get(r.get("attendanceId")),
        )
        for r in raw
    ]


# ================= HR: LIST =================
@hr_router.get("")
async def list_correction_requests(
    status: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr_or_ceo),
):

    query: dict = {}

    if status:
        query["status"] = status

    raw = []

    async for r in db.correction_requests.find(
        query
    ).sort("createdAt", -1):
        raw.append(r)

    user_map = await _get_user_basics(
        r.get("userId") for r in raw
    )

    att_map = await _get_attendance_summaries(
        r.get("attendanceId") for r in raw
    )

    return [
        _serialize(
            r,
            user_map.get(r.get("userId")),
            att_map.get(r.get("attendanceId")),
        )
        for r in raw
    ]


async def _decide_correction_internal(
    oid: ObjectId,
    req: dict,
    decider: dict,
    data: CorrectionDecision,
) -> dict:
    """Shared approve/reject logic for HR and Manager endpoints. Caller
    must ensure the request is PENDING and the decider is authorized."""
    now = datetime.now(timezone.utc)
    decider_id = str(decider["_id"])
    decider_role = decider.get("role", "HR")

    if data.action == "APPROVE":
        try:
            att_oid = ObjectId(req["attendanceId"])
        except (InvalidId, TypeError, KeyError):
            raise HTTPException(
                500, "Request has invalid attendance reference",
            )

        # Build the $set payload by walking each editable field. For
        # each one: take HR's override if provided, else the user's
        # request, else leave the field untouched.
        att_updates: dict = {"updatedAt": now}

        # Date
        final_date = data.overrideDate or req.get("requestedDate")
        if final_date:
            try:
                datetime.strptime(final_date, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    400, "overrideDate must be YYYY-MM-DD",
                )
            att_updates["date"] = final_date

        # Check-in
        final_check_in = (
            _parse_iso(data.overrideCheckIn, "overrideCheckIn")
            if data.overrideCheckIn else req.get("requestedCheckIn")
        )
        if final_check_in:
            att_updates["checkIn"] = final_check_in

        # Check-out
        final_check_out = (
            _parse_iso(data.overrideCheckOut, "overrideCheckOut")
            if data.overrideCheckOut else req.get("requestedCheckOut")
        )
        if final_check_out:
            att_updates["checkOut"] = final_check_out

        # Attendance type
        final_type = (
            data.overrideAttendanceType
            or req.get("requestedAttendanceType")
        )
        if final_type:
            att_updates["attendanceType"] = final_type

        # Work notes
        final_notes = (
            data.overrideWorkNotes
            if data.overrideWorkNotes is not None
            else req.get("requestedWorkNotes")
        )
        if final_notes is not None:
            att_updates["workNotes"] = final_notes

        # Recompute status from the final check-in/out (use the existing
        # record values for whatever we're not changing). When both
        # timestamps are present run the same classify_on_checkout rules
        # the live checkout path uses, so an approved correction maps to
        # PRESENT / LATE / HALF_DAY consistently with the rest of the
        # app — never the legacy "COMPLETED" string the UI doesn't
        # recognise.
        from utils.attendance_rules import classify_on_checkout
        existing_att = await db.attendance.find_one({"_id": att_oid})
        if not existing_att:
            raise HTTPException(404, "Attendance record no longer exists")
        merged_in = att_updates.get("checkIn", existing_att.get("checkIn"))
        merged_out = att_updates.get("checkOut", existing_att.get("checkOut"))
        if merged_in and merged_out:
            classification = classify_on_checkout(merged_in, merged_out)
            att_updates["status"] = classification["status"]
            att_updates["hoursWorked"] = classification["hoursWorked"]
            att_updates["overtimeHours"] = classification["overtimeHours"]
            att_updates["isLate"] = classification["isLate"]
        elif merged_in:
            att_updates["status"] = "CHECKED_IN"

        att_result = await db.attendance.update_one(
            {"_id": att_oid},
            {
                "$set": att_updates,
                # Clear the auto-close flag on any approved correction
                # — the record is now manually verified.
                "$unset": {"autoClosedByCron": ""},
            },
        )
        if att_result.matched_count == 0:
            raise HTTPException(404, "Attendance record no longer exists")

        await db.correction_requests.update_one(
            {"_id": oid},
            {
                "$set": {
                    "status": "APPROVED",
                    "decidedBy": decider_id,
                    "decidedByRole": decider_role,
                    "decidedAt": now,
                    "decisionNote": data.note or "",
                    "updatedAt": now,
                }
            },
        )
        await _notify_requester(
            req["userId"], "APPROVED", str(oid), data.note or "",
        )
        await log_audit(
            actor_id=decider_id,
            action="correction.approve",
            entity_type="correction_requests",
            entity_id=str(oid),
            after={"decidedByRole": decider_role},
        )
        return {"message": "Correction approved and applied"}

    # REJECT
    await db.correction_requests.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": "REJECTED",
                "decidedBy": decider_id,
                "decidedByRole": decider_role,
                "decidedAt": now,
                "decisionNote": data.note or "",
                "updatedAt": now,
            }
        },
    )
    await _notify_requester(
        req["userId"], "REJECTED", str(oid), data.note or "",
    )
    await log_audit(
        actor_id=decider_id,
        action="correction.reject",
        entity_type="correction_requests",
        entity_id=str(oid),
        after={"decidedByRole": decider_role},
    )
    return {"message": "Correction rejected"}


async def _bulk_decide_corrections(
    ids: list[str],
    decider: dict,
    action: str,
    note: str,
    scope_check: bool,
) -> dict:
    """Apply the same APPROVE/REJECT decision to many correction requests.

    Each id is processed independently so one bad/already-decided row
    doesn't abort the rest — the per-item outcome is collected and a
    summary is returned. `scope_check=True` enforces the manager
    direct-report rule per item (HR passes False).
    """
    results: list[dict] = []
    approved = 0
    failed = 0

    for rid in ids:
        try:
            oid = ObjectId(rid)
        except (InvalidId, TypeError):
            failed += 1
            results.append({"id": rid, "ok": False, "error": "Invalid id"})
            continue

        req = await db.correction_requests.find_one({"_id": oid})
        if not req:
            failed += 1
            results.append({"id": rid, "ok": False, "error": "Not found"})
            continue
        if req.get("status") != "PENDING":
            failed += 1
            results.append({
                "id": rid,
                "ok": False,
                "error": f"Already {req.get('status')}",
            })
            continue

        if scope_check:
            try:
                employee = await db.users.find_one(
                    {"_id": ObjectId(req["userId"])}
                )
            except (InvalidId, TypeError, KeyError):
                employee = None
            if not employee or not can_decide_for_employee(decider, employee):
                failed += 1
                results.append({
                    "id": rid,
                    "ok": False,
                    "error": "Not one of your direct reports",
                })
                continue

        try:
            await _decide_correction_internal(
                oid,
                req,
                decider,
                CorrectionDecision(action=action, note=note or ""),
            )
            approved += 1
            results.append({"id": rid, "ok": True})
        except HTTPException as e:
            failed += 1
            results.append({"id": rid, "ok": False, "error": e.detail})
        except Exception as e:  # noqa: BLE001 - keep the batch going
            failed += 1
            results.append({"id": rid, "ok": False, "error": str(e)})

    verb = "approved" if action == "APPROVE" else "rejected"
    return {
        "message": f"{approved} {verb}, {failed} failed",
        "total": len(ids),
        "succeeded": approved,
        "failed": failed,
        "results": results,
    }


# ================= HR: DECIDE =================
@hr_router.post("/bulk-decide")
async def hr_bulk_decide_correction_requests(
    data: CorrectionBulkDecision,
    hr: dict = Depends(require_hr),
):
    """Approve/reject many requests at once (HR scope — any request)."""
    if not data.ids:
        raise HTTPException(400, "No correction ids provided")
    return await _bulk_decide_corrections(
        data.ids, hr, data.action, data.note or "", scope_check=False,
    )


@hr_router.post("/{id}/decide")
async def decide_correction_request(
    id: str,
    data: CorrectionDecision,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid request id")

    req = await db.correction_requests.find_one({"_id": oid})
    if not req:
        raise HTTPException(404, "Correction request not found")
    if req.get("status") != "PENDING":
        raise HTTPException(400, f"Already {req.get('status')}")

    return await _decide_correction_internal(oid, req, hr, data)


# ================= MANAGER: LIST + DECIDE =================
@manager_router.get("")
async def list_correction_requests_for_my_reports(
    status: Optional[str] = Query("PENDING"),
    actor: dict = Depends(require_manager_or_hr),
):
    actor_id = str(actor["_id"])

    if actor.get("role") == "HR":
        report_user_ids = None
    else:
        report_user_ids = [
            str(u["_id"])
            async for u in db.users.find(
                {"reportingManagerId": actor_id},
                {"_id": 1},
            )
        ]
        if not report_user_ids:
            return []

    query: dict = {}
    if status:
        query["status"] = status
    if report_user_ids is not None:
        query["userId"] = {"$in": report_user_ids}

    raw = []
    async for r in db.correction_requests.find(query).sort("createdAt", -1):
        raw.append(r)

    user_map = await _get_user_basics(r.get("userId") for r in raw)
    att_map = await _get_attendance_summaries(
        r.get("attendanceId") for r in raw
    )
    return [
        _serialize(
            r,
            user_map.get(r.get("userId")),
            att_map.get(r.get("attendanceId")),
        )
        for r in raw
    ]


@manager_router.post("/bulk-decide")
async def manager_bulk_decide_correction_requests(
    data: CorrectionBulkDecision,
    actor: dict = Depends(require_manager_or_hr),
):
    """Approve/reject many requests at once. HR may act on any request;
    a manager is restricted to their own direct reports (enforced per
    item, so out-of-scope ids are reported as failed rather than 403ing
    the whole batch)."""
    if not data.ids:
        raise HTTPException(400, "No correction ids provided")
    scope = actor.get("role") != "HR"
    return await _bulk_decide_corrections(
        data.ids, actor, data.action, data.note or "", scope_check=scope,
    )


@manager_router.post("/{id}/decide")
async def manager_decide_correction_request(
    id: str,
    data: CorrectionDecision,
    actor: dict = Depends(require_manager_or_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid request id")

    req = await db.correction_requests.find_one({"_id": oid})
    if not req:
        raise HTTPException(404, "Correction request not found")
    if req.get("status") != "PENDING":
        raise HTTPException(400, f"Already {req.get('status')}")

    try:
        employee = await db.users.find_one(
            {"_id": ObjectId(req["userId"])}
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

    return await _decide_correction_internal(oid, req, actor, data)
