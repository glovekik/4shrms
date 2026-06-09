from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone, date, timedelta

from typing import Optional

from database import db
from utils.dependencies import (
    get_current_user,
    require_hr,
    require_hr_or_ceo,
    require_manager_or_hr,
    can_decide_for_employee,
)
from utils.push import push_to_user
from utils.email import send_notification_email
from utils.audit import log_audit
from utils.notify import create_notification, notify_approvers, notify_user
from config import COMPANY_NAME
from models.leave import (
    LeaveTypeCreate,
    LeaveTypeUpdate,
    LeaveRequestCreate,
    LeaveDecision,
    LeaveBalanceUpsert,
)


async def _lookup_user_email(user_id: str) -> tuple[Optional[str], Optional[str]]:
    """Returns (name, email) for a user id, or (None, None) on miss."""
    try:
        u = await db.users.find_one({"_id": ObjectId(user_id)})
    except (InvalidId, TypeError):
        return (None, None)
    if not u:
        return (None, None)
    return (u.get("name"), u.get("email"))

# /leaves/...    — anyone authenticated
user_router = APIRouter()

# /hr/...        — HR only
hr_router = APIRouter()

# /manager/...   — MANAGER or HR; scoped to direct reports
manager_router = APIRouter()


async def _decide_leave_internal(
    oid: ObjectId,
    req: dict,
    decider: dict,
    data: "LeaveDecision",
) -> dict:
    """Shared approve/reject logic for HR and Manager endpoints.

    Caller is responsible for: looking up `req`, confirming PENDING status,
    and authorizing the decider against `req["userId"]`. This function only
    handles balance updates, status change, notifications, and audit.
    """
    now = datetime.now(timezone.utc)
    decider_id = str(decider["_id"])
    decider_role = decider.get("role", "HR")
    actor_label = "HR" if decider_role == "HR" else "Manager"
    year = _parse_date(req["fromDate"], "fromDate").year
    total_days = req.get("totalDays", 0)

    if data.action == "APPROVE":
        await db.leave_balances.update_one(
            {
                "userId": req["userId"],
                "leaveTypeCode": req["leaveTypeCode"],
                "year": year,
            },
            {
                "$inc": {
                    "pending": -total_days,
                    "used": total_days,
                },
                "$set": {"updatedAt": now},
            },
        )
        await db.leave_requests.update_one(
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

        # Auto-mark each approved leave day as LEAVE attendance so it shows
        # in the employee's history/calendar. Only working days are marked
        # (Sundays + declared holidays are skipped, same as the balance
        # charge). Race-safe: never clobber a date that already has a
        # record (e.g. a real check-in).
        leave_note = f"{req.get('leaveTypeCode', '')} leave".strip()
        if req.get("halfDay"):
            part = req.get("halfDayPart")
            leave_note += f" (half day{f' — {part}' if part else ''})"
        for ymd in await _working_dates_in_range(
            req["fromDate"], req["toDate"]
        ):
            already = await db.attendance.find_one(
                {"userId": req["userId"], "date": ymd}
            )
            if not already:
                await db.attendance.insert_one({
                    "userId": req["userId"],
                    "date": ymd,
                    "attendanceType": "LEAVE",
                    "status": "ON_LEAVE",
                    "checkIn": None,
                    "checkOut": None,
                    "workNotes": leave_note,
                    "autoAppliedFromLeave": True,
                    "leaveRequestId": str(oid),
                    "createdAt": now,
                    "updatedAt": now,
                })

        try:
            await push_to_user(
                req["userId"],
                "Leave approved",
                f"{req['fromDate']} to {req['toDate']} ({total_days} day(s))",
                {"type": "leave_decision", "requestId": str(oid)},
            )
        except Exception:
            pass

        await create_notification(
            req["userId"],
            "leave_decision",
            "Leave approved",
            f"{req['fromDate']} to {req['toDate']} ({total_days} day(s))",
            {"requestId": str(oid), "outcome": "APPROVED"},
        )

        name, email = await _lookup_user_email(req["userId"])
        if email:
            note_line = (
                f"\n\nNote from {actor_label}:\n{data.note}\n"
                if data.note else ""
            )
            await send_notification_email(
                email,
                f"Leave approved — {req['fromDate']} to {req['toDate']}",
                (
                    f"Hi {name or 'there'},\n\n"
                    f"Your leave request has been APPROVED.\n\n"
                    f"From: {req['fromDate']}\n"
                    f"To:   {req['toDate']}\n"
                    f"Days: {total_days}\n"
                    f"Type: {req.get('leaveTypeCode', '')}"
                    + note_line
                    + f"\n\nRegards,\n{COMPANY_NAME}"
                ),
            )

        await log_audit(
            actor_id=decider_id,
            action="leave.approve",
            entity_type="leave_requests",
            entity_id=str(oid),
            after={"days": total_days, "decidedByRole": decider_role},
        )
        return {"message": "Leave approved"}

    # REJECT — release pending hold
    await db.leave_balances.update_one(
        {
            "userId": req["userId"],
            "leaveTypeCode": req["leaveTypeCode"],
            "year": year,
        },
        {
            "$inc": {"pending": -total_days},
            "$set": {"updatedAt": now},
        },
    )
    await db.leave_requests.update_one(
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
    try:
        await push_to_user(
            req["userId"],
            "Leave rejected",
            data.note or f"{req['fromDate']} to {req['toDate']}",
            {"type": "leave_decision", "requestId": str(oid)},
        )
    except Exception:
        pass

    await create_notification(
        req["userId"],
        "leave_decision",
        "Leave rejected",
        data.note or f"{req['fromDate']} to {req['toDate']}",
        {"requestId": str(oid), "outcome": "REJECTED"},
    )

    name, email = await _lookup_user_email(req["userId"])
    if email:
        note_line = (
            f"\n\nReason from {actor_label}:\n{data.note}\n"
            if data.note else ""
        )
        await send_notification_email(
            email,
            f"Leave rejected — {req['fromDate']} to {req['toDate']}",
            (
                f"Hi {name or 'there'},\n\n"
                f"Your leave request has been REJECTED.\n\n"
                f"From: {req['fromDate']}\n"
                f"To:   {req['toDate']}\n"
                f"Days: {total_days}\n"
                f"Type: {req.get('leaveTypeCode', '')}"
                + note_line
                + "\n\nIf you have questions, contact your HR team.\n"
                + f"\nRegards,\n{COMPANY_NAME}"
            ),
        )

    await log_audit(
        actor_id=decider_id,
        action="leave.reject",
        entity_type="leave_requests",
        entity_id=str(oid),
        after={"days": total_days, "decidedByRole": decider_role},
    )
    return {"message": "Leave rejected"}


# ================= SERIALIZERS =================
def _serialize_leave_type(t: dict) -> dict:
    return {
        "id": str(t["_id"]),
        "code": t.get("code"),
        "name": t.get("name"),
        "daysPerMonth": t.get("daysPerMonth", 0.0),
        "daysPerYear": t.get("daysPerYear", 0.0),
        "allowHalfDay": t.get("allowHalfDay", True),
        "requiresAttachment": t.get(
            "requiresAttachment", False
        ),
        "description": t.get("description", ""),
        "isActive": t.get("isActive", True),
    }


def _serialize_balance(
    b: dict,
    leave_type: Optional[dict] = None,
) -> dict:
    allocated = b.get("allocated", 0.0)
    used = b.get("used", 0.0)
    pending = b.get("pending", 0.0)

    history = b.get("accrualHistory") or []
    year = b.get("year")
    current_year = datetime.now().year
    current_month = datetime.now().month
    accrued_this_month = 0.0
    accrued_ytd = 0.0
    summary: dict[int, float] = {}
    for entry in history:
        try:
            month = int(entry.get("month", 0))
            added = float(entry.get("addedDays", 0) or 0)
        except (TypeError, ValueError):
            continue
        if year == current_year:
            accrued_ytd += added
            if month == current_month:
                accrued_this_month += added
        summary[month] = summary.get(month, 0.0) + added

    return {
        "leaveTypeCode": b.get("leaveTypeCode"),
        "leaveType": (
            _serialize_leave_type(leave_type)
            if leave_type else None
        ),
        "year": year,
        "allocated": allocated,
        "used": used,
        "pending": pending,
        "remaining": allocated - used - pending,
        "accruedThisMonth": round(accrued_this_month, 2),
        "accruedYTD": round(accrued_ytd, 2),
        "monthlyAccrualHistory": [
            {"month": m, "addedDays": round(d, 2)}
            for m, d in sorted(summary.items())
        ],
    }


def _serialize_request(
    r: dict,
    user_info: Optional[dict] = None,
    leave_type: Optional[dict] = None,
) -> dict:
    return {
        "id": str(r["_id"]),
        "userId": r.get("userId"),
        "user": user_info,
        "leaveTypeCode": r.get("leaveTypeCode"),
        "leaveType": (
            _serialize_leave_type(leave_type)
            if leave_type else None
        ),
        "fromDate": r.get("fromDate"),
        "toDate": r.get("toDate"),
        "halfDay": r.get("halfDay", False),
        "halfDayPart": r.get("halfDayPart"),
        "totalDays": r.get("totalDays"),
        "reason": r.get("reason"),
        "attachmentUrl": r.get("attachmentUrl"),
        "status": r.get("status"),
        "decisionNote": r.get("decisionNote", ""),
        "decidedBy": r.get("decidedBy"),
        "decidedAt": (
            r["decidedAt"].isoformat()
            if r.get("decidedAt") else None
        ),
        "createdAt": (
            r["createdAt"].isoformat()
            if r.get("createdAt") else None
        ),
    }


# ================= HELPERS =================
def _parse_date(s: str, field: str) -> date:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise HTTPException(
            400,
            f"Invalid {field}, expected YYYY-MM-DD",
        )


async def _working_dates_in_range(
    from_date: str,
    to_date: str,
) -> list[str]:
    """Working days (YYYY-MM-DD) in [from_date, to_date] inclusive, with
    Sundays and HR-declared holidays excluded. Shared by the balance calc
    and the on-approval attendance auto-apply so they always agree."""
    f = _parse_date(from_date, "fromDate")
    t = _parse_date(to_date, "toDate")
    if t < f:
        raise HTTPException(400, "toDate cannot be before fromDate")

    holiday_dates: set[str] = set()
    async for h in db.holidays.find(
        {"date": {"$gte": from_date, "$lte": to_date}},
        {"date": 1},
    ):
        if h.get("date"):
            holiday_dates.add(h["date"])

    out: list[str] = []
    d = f
    while d <= t:
        ymd = d.isoformat()
        # weekday(): Monday=0 … Sunday=6. Skip Sundays + declared holidays.
        if d.weekday() != 6 and ymd not in holiday_dates:
            out.append(ymd)
        d += timedelta(days=1)
    return out


async def _calc_total_days(
    from_date: str,
    to_date: str,
    half_day: bool,
) -> float:
    """Chargeable leave days — excludes Sundays and declared holidays."""
    working = await _working_dates_in_range(from_date, to_date)
    if half_day:
        f = _parse_date(from_date, "fromDate")
        t = _parse_date(to_date, "toDate")
        if f != t:
            raise HTTPException(
                400, "Half-day leave must be a single day"
            )
        if not working:
            raise HTTPException(
                400,
                "Half-day must fall on a working day (not a Sunday/holiday).",
            )
        return 0.5
    if not working:
        raise HTTPException(
            400,
            "The selected range has no working days "
            "(only Sundays/holidays).",
        )
    return float(len(working))


def _default_allocated(leave_type: Optional[dict]) -> float:
    """Allocation a freshly-seeded balance starts at.

    Two leave-type shapes:
      * Full-upfront (daysPerMonth == 0): allocate the full daysPerYear
        on day 1 — employee can take the whole quota immediately.
      * Monthly accrual (daysPerMonth > 0): start at 0; the cron ramps
        the row up each month, capped at daysPerYear. Seeding upfront
        here would let the employee take a full year's quota in January.
    """
    if not leave_type:
        return 0.0
    per_month = float(leave_type.get("daysPerMonth", 0) or 0)
    per_year = float(leave_type.get("daysPerYear", 0) or 0)
    if per_month > 0:
        return 0.0
    return max(per_year, 0.0)


async def _get_or_create_balance(
    user_id: str,
    leave_type_code: str,
    year: int,
    leave_type: Optional[dict] = None,
) -> dict:
    balance = await db.leave_balances.find_one({
        "userId": user_id,
        "leaveTypeCode": leave_type_code,
        "year": year,
    })
    if balance:
        return balance
    if leave_type is None:
        leave_type = await db.leave_types.find_one(
            {"code": leave_type_code}
        )
    now = datetime.now(timezone.utc)
    doc = {
        "userId": user_id,
        "leaveTypeCode": leave_type_code,
        "year": year,
        "allocated": _default_allocated(leave_type),
        "used": 0.0,
        "pending": 0.0,
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.leave_balances.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def _active_user_ids() -> list[str]:
    """All users treated as Active (missing status counts as Active)."""
    ids: list[str] = []
    async for u in db.users.find(
        {
            "$or": [
                {"status": "Active"},
                {"status": {"$exists": False}},
            ]
        },
        {"_id": 1},
    ):
        ids.append(str(u["_id"]))
    return ids


async def _seed_balances_for_user(
    user_id: str,
    year: int,
    now: datetime,
) -> None:
    """Ensures the user has a balance row for every active leave type.

    Behavior per type:
      * Full-upfront (daysPerMonth == 0): `$max` on allocated so an
        existing row stuck at 0 (created before HR configured the quota)
        gets topped up to daysPerYear on the next read. Won't trample
        carryover/manual-bump rows that are already higher.
      * Monthly-accrual (daysPerMonth > 0): `$setOnInsert` only — the
        monthly cron is the only thing that grows allocated. Setting
        $max here would skip the accrual ramp.
    """
    async for lt in db.leave_types.find({"isActive": True}):
        per_month = float(lt.get("daysPerMonth", 0) or 0)
        per_year = float(lt.get("daysPerYear", 0) or 0)

        if per_month > 0:
            # Monthly accrual — let the cron handle allocated.
            await db.leave_balances.update_one(
                {
                    "userId": user_id,
                    "leaveTypeCode": lt.get("code"),
                    "year": year,
                },
                {
                    "$setOnInsert": {
                        "allocated": 0.0,
                        "used": 0.0,
                        "pending": 0.0,
                        "createdAt": now,
                    },
                    "$set": {"updatedAt": now},
                },
                upsert=True,
            )
        else:
            # Full-upfront — $max ensures allocated >= daysPerYear, so
            # users whose rows were created before HR set the quota get
            # topped up next time they open My Leaves.
            target = max(per_year, 0.0)
            await db.leave_balances.update_one(
                {
                    "userId": user_id,
                    "leaveTypeCode": lt.get("code"),
                    "year": year,
                },
                {
                    "$max": {"allocated": target},
                    "$setOnInsert": {
                        "used": 0.0,
                        "pending": 0.0,
                        "createdAt": now,
                    },
                    "$set": {"updatedAt": now},
                },
                upsert=True,
            )


async def _seed_balances_for_type(
    leave_type: dict,
    year: int,
    now: datetime,
) -> int:
    """Inverse of _seed_balances_for_user — seeds one type across every
    active user. For full-upfront types (daysPerMonth == 0) this $max's
    allocated up to daysPerYear, so re-running after HR raises the cap
    tops everyone up. Monthly-accrual types ($default = 0) only get a
    placeholder row on first run."""
    user_ids = await _active_user_ids()
    if not user_ids:
        return 0
    allocated = _default_allocated(leave_type)
    per_month = float(leave_type.get("daysPerMonth", 0) or 0)
    code = leave_type.get("code")
    for uid in user_ids:
        if per_month > 0:
            # Monthly type — only insert a placeholder if no row exists;
            # never bump allocated (cron owns that field).
            await db.leave_balances.update_one(
                {
                    "userId": uid,
                    "leaveTypeCode": code,
                    "year": year,
                },
                {
                    "$setOnInsert": {
                        "allocated": allocated,
                        "used": 0.0,
                        "pending": 0.0,
                        "createdAt": now,
                    },
                    "$set": {"updatedAt": now},
                },
                upsert=True,
            )
        else:
            # Full-upfront type — $max so HR raising daysPerYear tops
            # existing balances up to the new annual.
            await db.leave_balances.update_one(
                {
                    "userId": uid,
                    "leaveTypeCode": code,
                    "year": year,
                },
                {
                    "$max": {"allocated": allocated},
                    "$setOnInsert": {
                        "used": 0.0,
                        "pending": 0.0,
                        "createdAt": now,
                    },
                    "$set": {"updatedAt": now},
                },
                upsert=True,
            )
    return len(user_ids)


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


async def _types_by_code() -> dict:
    """Returns {code: leaveTypeDoc} for all types (active + inactive)."""
    result = {}
    async for t in db.leave_types.find():
        result[t.get("code")] = t
    return result


# ================= USER: LIST ACTIVE TYPES =================
@user_router.get("/types")
async def list_active_leave_types(
    _user_id: str = Depends(get_current_user),
):
    types = []
    async for t in db.leave_types.find(
        {"isActive": True}
    ).sort("name", 1):
        types.append(_serialize_leave_type(t))
    return types


# ================= USER: BALANCE =================
@user_router.get("/balance")
async def my_leave_balance(
    user_id: str = Depends(get_current_user),
):
    year = datetime.now().year
    now = datetime.now(timezone.utc)

    types_map = await _types_by_code()

    # Lazy-seed any missing rows for active types so a brand-new employee
    # sees their full annual quota immediately and subsequent decrements
    # have a row to mutate. Idempotent (uses $max on allocated).
    await _seed_balances_for_user(user_id, year, now)

    balances = []
    async for b in db.leave_balances.find({
        "userId": user_id,
        "year": year,
    }):
        balances.append(b)

    # Skip orphan rows whose leaveTypeCode no longer matches an active
    # leave_type — these are leftovers from deleted/inactive types and
    # would otherwise render as raw codes like "EARN68c0d4cd" on the UI.
    return [
        _serialize_balance(b, types_map[b["leaveTypeCode"]])
        for b in balances
        if b.get("leaveTypeCode") in types_map
        and types_map[b["leaveTypeCode"]].get("isActive", True)
    ]


# ================= USER: MY REQUESTS =================
@user_router.get("/mine")
async def my_leave_requests(
    status: Optional[str] = Query(None),
    user_id: str = Depends(get_current_user),
):
    query: dict = {"userId": user_id}
    if status:
        query["status"] = status

    raw = []
    async for r in db.leave_requests.find(
        query
    ).sort("createdAt", -1):
        raw.append(r)

    types_map = await _types_by_code()

    return [
        _serialize_request(
            r,
            None,
            types_map.get(r.get("leaveTypeCode")),
        )
        for r in raw
    ]


# ================= USER: REQUEST LEAVE =================
@user_router.post("/request")
async def create_leave_request(
    data: LeaveRequestCreate,
    user_id: str = Depends(get_current_user),
):
    leave_type = await db.leave_types.find_one({
        "code": data.leaveTypeCode,
    })

    if not leave_type:
        raise HTTPException(
            400,
            f"Unknown leave type: {data.leaveTypeCode}",
        )

    if not leave_type.get("isActive", True):
        raise HTTPException(
            400,
            f"Leave type '{data.leaveTypeCode}' is inactive",
        )

    if data.halfDay and not leave_type.get(
        "allowHalfDay", True
    ):
        raise HTTPException(
            400,
            "Half-day not allowed for this leave type",
        )

    if data.halfDay and not data.halfDayPart:
        raise HTTPException(
            400,
            "halfDayPart is required when halfDay=true",
        )

    if leave_type.get("requiresAttachment") and not data.attachmentUrl:
        raise HTTPException(
            400,
            "Attachment is required for this leave type",
        )

    reason = (data.reason or "").strip()
    if not reason:
        raise HTTPException(400, "Reason is required")

    total_days = await _calc_total_days(
        data.fromDate,
        data.toDate,
        data.halfDay,
    )

    # Block overlapping pending/approved requests for this user.
    overlap = await db.leave_requests.find_one({
        "userId": user_id,
        "status": {"$in": ["PENDING", "APPROVED"]},
        "fromDate": {"$lte": data.toDate},
        "toDate": {"$gte": data.fromDate},
    })

    if overlap:
        raise HTTPException(
            409,
            "An existing leave request overlaps these dates",
        )

    # Balance year tied to the fromDate's year.
    year = _parse_date(data.fromDate, "fromDate").year

    balance = await _get_or_create_balance(
        user_id,
        data.leaveTypeCode,
        year,
        leave_type,
    )

    available = (
        balance["allocated"]
        - balance["used"]
        - balance["pending"]
    )

    if total_days > available:
        raise HTTPException(
            400,
            (
                f"Insufficient {data.leaveTypeCode} balance: "
                f"requested {total_days}, available {available}. "
                f"Ask HR to allocate."
            ),
        )

    now = datetime.now(timezone.utc)

    request_doc = {
        "userId": user_id,
        "leaveTypeCode": data.leaveTypeCode,
        "fromDate": data.fromDate,
        "toDate": data.toDate,
        "halfDay": data.halfDay,
        "halfDayPart": data.halfDayPart,
        "totalDays": total_days,
        "reason": reason,
        "attachmentUrl": data.attachmentUrl,
        "status": "PENDING",
        "decisionNote": "",
        "decidedBy": None,
        "decidedAt": None,
        "createdAt": now,
        "updatedAt": now,
    }

    result = await db.leave_requests.insert_one(request_doc)
    request_doc["_id"] = result.inserted_id

    await db.leave_balances.update_one(
        {"_id": balance["_id"]},
        {
            "$inc": {"pending": total_days},
            "$set": {"updatedAt": now},
        },
    )

    # Notify approvers (reporting manager + HR) that a request is pending.
    submitter = await db.users.find_one({"_id": ObjectId(user_id)}, {"name": 1})
    who = (submitter or {}).get("name") or "An employee"
    await notify_approvers(
        user_id,
        "leave_requests",
        "New leave request",
        f"{who} requested {total_days}d {data.leaveTypeCode} "
        f"({data.fromDate} → {data.toDate})",
        {"leaveRequestId": str(result.inserted_id)},
    )

    return _serialize_request(request_doc, None, leave_type)


# ================= USER: CANCEL =================
@user_router.post("/{id}/cancel")
async def cancel_leave_request(
    id: str,
    user_id: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid request id")

    req = await db.leave_requests.find_one({"_id": oid})

    if not req:
        raise HTTPException(404, "Request not found")

    if req.get("userId") != user_id:
        raise HTTPException(403, "Not your request")

    if req.get("status") != "PENDING":
        raise HTTPException(
            400,
            f"Cannot cancel a {req.get('status')} request",
        )

    now = datetime.now(timezone.utc)
    year = _parse_date(req["fromDate"], "fromDate").year

    await db.leave_requests.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": "CANCELLED",
                "updatedAt": now,
            }
        },
    )

    # Release the pending balance.
    await db.leave_balances.update_one(
        {
            "userId": user_id,
            "leaveTypeCode": req["leaveTypeCode"],
            "year": year,
        },
        {
            "$inc": {"pending": -req.get("totalDays", 0)},
            "$set": {"updatedAt": now},
        },
    )

    # Tell the approvers the pending request was withdrawn so they don't
    # act on it.
    submitter = await db.users.find_one(
        {"_id": ObjectId(user_id)}, {"name": 1}
    )
    who = (submitter or {}).get("name") or "An employee"
    await notify_approvers(
        user_id,
        "leave_cancelled",
        "Leave request cancelled",
        f"{who} withdrew their {req['fromDate']} → {req['toDate']} "
        f"leave request",
        {"leaveRequestId": id},
    )

    return {"message": "Leave cancelled"}


# ================= HR: LEAVE TYPES — CREATE =================
@hr_router.post("/leave-types")
async def create_leave_type(
    data: LeaveTypeCreate,
    _hr: dict = Depends(require_hr),
):
    code = (data.code or "").strip()
    if not code:
        raise HTTPException(400, "Code is required")

    existing = await db.leave_types.find_one({"code": code})
    if existing:
        raise HTTPException(
            400,
            f"Leave type code '{code}' already exists",
        )

    now = datetime.now(timezone.utc)
    doc = {
        "code": code,
        "name": data.name,
        "daysPerMonth": data.daysPerMonth,
        "daysPerYear": data.daysPerYear,
        "allowHalfDay": data.allowHalfDay,
        "requiresAttachment": data.requiresAttachment,
        "description": data.description or "",
        "isActive": data.isActive,
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.leave_types.insert_one(doc)
    doc["_id"] = result.inserted_id

    # Seed balances for every active user so the new type shows up with
    # its full annual allocation immediately. Skips inactive types so
    # disabled-by-default types don't litter balances.
    if doc.get("isActive", True):
        try:
            await _seed_balances_for_type(
                doc, datetime.now().year, now
            )
        except Exception as e:
            print(f"[leave] seed-on-create failed: {e}")

    return _serialize_leave_type(doc)


# ================= HR: LEAVE TYPES — LIST =================
@hr_router.get("/leave-types")
async def list_all_leave_types(
    _hr: dict = Depends(require_hr),
):
    types = []
    async for t in db.leave_types.find().sort("name", 1):
        types.append(_serialize_leave_type(t))
    return types


# ================= HR: LEAVE TYPES — UPDATE =================
@hr_router.put("/leave-types/{id}")
async def update_leave_type(
    id: str,
    data: LeaveTypeUpdate,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    existing = await db.leave_types.find_one({"_id": oid})
    if not existing:
        raise HTTPException(404, "Leave type not found")

    now = datetime.now(timezone.utc)
    update: dict = {"updatedAt": now}
    for field in (
        "name",
        "daysPerMonth",
        "daysPerYear",
        "allowHalfDay",
        "requiresAttachment",
        "description",
        "isActive",
    ):
        value = getattr(data, field)
        if value is not None:
            update[field] = value

    await db.leave_types.update_one(
        {"_id": oid},
        {"$set": update},
    )

    # Detect whether the change implies a top-up:
    #   - daysPerYear went up,
    #   - or daysPerMonth went up (and the cron now needs to accrue more),
    #   - or the type was just reactivated (isActive false→true).
    new_per_year = float(
        update.get("daysPerYear", existing.get("daysPerYear", 0)) or 0
    )
    old_per_year = float(existing.get("daysPerYear", 0) or 0)
    was_active = bool(existing.get("isActive", True))
    is_active_now = bool(update.get("isActive", was_active))

    reactivated = (not was_active) and is_active_now
    raised_year = new_per_year > old_per_year

    if is_active_now and (raised_year or reactivated):
        # #6 top-up: bump existing balances up to the new annual cap, and
        # seed any missing rows for active users. Skipped for monthly-
        # accrual types — raising daysPerYear there only raises the cap
        # (cron will catch up); we don't grant the whole year retroactively.
        merged_type = {**existing, **update}
        new_per_month = float(
            update.get("daysPerMonth", existing.get("daysPerMonth", 0))
            or 0
        )
        try:
            if new_per_month == 0:
                await db.leave_balances.update_many(
                    {
                        "leaveTypeCode": existing.get("code"),
                        "year": datetime.now().year,
                    },
                    [
                        {
                            "$set": {
                                "allocated": {
                                    "$max": ["$allocated", new_per_year]
                                },
                                "updatedAt": now,
                            }
                        }
                    ],
                )
            await _seed_balances_for_type(
                merged_type, datetime.now().year, now
            )
        except Exception as e:
            print(f"[leave] top-up on update failed: {e}")

    return {"message": "Leave type updated"}


# ================= HR: LEAVE TYPES — DELETE =================
@hr_router.delete("/leave-types/{id}")
async def delete_leave_type(
    id: str,
    _hr: dict = Depends(require_hr),
):
    """Hard delete. Existing leave_requests keep their leaveTypeCode reference
    (historical records); leave_balances for the same code are cascade-deleted
    so they don't render as orphans on the user's My Leaves screen."""
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    # Fetch the code BEFORE deletion so we can cascade balances by code.
    existing = await db.leave_types.find_one(
        {"_id": oid}, {"code": 1}
    )
    if not existing:
        raise HTTPException(404, "Leave type not found")

    code = existing.get("code")
    await db.leave_types.delete_one({"_id": oid})

    balances_deleted = 0
    if code:
        bal_result = await db.leave_balances.delete_many(
            {"leaveTypeCode": code}
        )
        balances_deleted = bal_result.deleted_count

    return {
        "message": "Leave type deleted",
        "balancesDeleted": balances_deleted,
    }


# ================= HR: REQUESTS — LIST =================
@hr_router.get("/leave-requests")
async def list_leave_requests(
    status: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr_or_ceo),
):
    query: dict = {}
    if status:
        query["status"] = status

    raw = []
    async for r in db.leave_requests.find(
        query
    ).sort("createdAt", -1):
        raw.append(r)

    user_map = await _user_basics(
        r.get("userId") for r in raw
    )
    types_map = await _types_by_code()

    return [
        _serialize_request(
            r,
            user_map.get(r.get("userId")),
            types_map.get(r.get("leaveTypeCode")),
        )
        for r in raw
    ]


# ================= HR: REQUESTS — DECIDE =================
@hr_router.post("/leave-requests/{id}/decide")
async def decide_leave_request(
    id: str,
    data: LeaveDecision,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    req = await db.leave_requests.find_one({"_id": oid})
    if not req:
        raise HTTPException(404, "Request not found")
    if req.get("status") != "PENDING":
        raise HTTPException(400, f"Already {req.get('status')}")

    return await _decide_leave_internal(oid, req, hr, data)


# ================= MANAGER: PENDING + DECIDE =================
@manager_router.get("/leave-requests")
async def list_leave_requests_for_my_reports(
    status: Optional[str] = Query("PENDING"),
    actor: dict = Depends(require_manager_or_hr),
):
    """Lists leave requests raised by the manager's direct reports.

    HR sees all requests (matches /hr/leave-requests). MANAGER sees only
    requests from users whose reportingManagerId == manager._id.
    """
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
    async for r in db.leave_requests.find(query).sort("createdAt", -1):
        raw.append(r)

    user_map = await _user_basics(r.get("userId") for r in raw)
    types_map = await _types_by_code()
    return [
        _serialize_request(
            r,
            user_map.get(r.get("userId")),
            types_map.get(r.get("leaveTypeCode")),
        )
        for r in raw
    ]


@manager_router.post("/leave-requests/{id}/decide")
async def manager_decide_leave_request(
    id: str,
    data: LeaveDecision,
    actor: dict = Depends(require_manager_or_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    req = await db.leave_requests.find_one({"_id": oid})
    if not req:
        raise HTTPException(404, "Request not found")
    if req.get("status") != "PENDING":
        raise HTTPException(400, f"Already {req.get('status')}")

    # Manager may only act on their own direct reports.
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

    return await _decide_leave_internal(oid, req, actor, data)


# ================= HR: UPSERT USER BALANCE =================
@hr_router.put("/users/{userId}/leave-balance")
async def hr_upsert_user_balance(
    userId: str,
    data: LeaveBalanceUpsert,
    hr: dict = Depends(require_hr),
):
    """HR manually grants or adjusts a user's leave balance for a given
    type and year. Validates the type exists; refuses negative values."""
    try:
        ObjectId(userId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid user id")

    user_exists = await db.users.find_one(
        {"_id": ObjectId(userId)},
        {"_id": 1},
    )
    if not user_exists:
        raise HTTPException(404, "User not found")

    leave_type = await db.leave_types.find_one(
        {"code": data.leaveTypeCode}
    )
    if not leave_type:
        raise HTTPException(
            400, f"Unknown leave type: {data.leaveTypeCode}"
        )

    if data.allocated < 0:
        raise HTTPException(400, "allocated must be >= 0")
    if data.used is not None and data.used < 0:
        raise HTTPException(400, "used must be >= 0")
    if data.pending is not None and data.pending < 0:
        raise HTTPException(400, "pending must be >= 0")

    target_year = data.year or datetime.now().year
    now = datetime.now(timezone.utc)

    set_fields: dict = {
        "allocated": data.allocated,
        "updatedAt": now,
    }
    if data.used is not None:
        set_fields["used"] = data.used
    if data.pending is not None:
        set_fields["pending"] = data.pending

    await db.leave_balances.update_one(
        {
            "userId": userId,
            "leaveTypeCode": data.leaveTypeCode,
            "year": target_year,
        },
        {
            "$set": set_fields,
            "$setOnInsert": {
                "used": 0.0,
                "pending": 0.0,
                "createdAt": now,
            },
        },
        upsert=True,
    )

    row = await db.leave_balances.find_one({
        "userId": userId,
        "leaveTypeCode": data.leaveTypeCode,
        "year": target_year,
    })

    await log_audit(
        actor_id=str(hr["_id"]),
        action="leave_balance.upsert",
        entity_type="leave_balances",
        entity_id=str(row["_id"]) if row else "",
        after={
            "userId": userId,
            "leaveTypeCode": data.leaveTypeCode,
            "year": target_year,
            "allocated": data.allocated,
            "used": data.used,
            "pending": data.pending,
            "note": data.note,
        },
    )

    # Let the employee know their balance was changed by HR.
    await notify_user(
        userId,
        "leave_balance_updated",
        "Leave balance updated",
        f"Your {data.leaveTypeCode} balance for {target_year} is now "
        f"{data.allocated} day(s)."
        + (f" {data.note}" if data.note else ""),
        {"leaveTypeCode": data.leaveTypeCode, "year": target_year},
    )

    return _serialize_balance(row or {}, leave_type)


# ================= HR: VIEW USER BALANCE =================
@hr_router.get("/users/{userId}/leave-balance")
async def hr_view_user_balance(
    userId: str,
    year: Optional[int] = Query(None),
    _hr: dict = Depends(require_hr),
):
    try:
        ObjectId(userId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid user id")

    target_year = year or datetime.now().year

    types_map = await _types_by_code()

    balances = []
    async for b in db.leave_balances.find({
        "userId": userId,
        "year": target_year,
    }):
        balances.append(b)

    have = {b["leaveTypeCode"] for b in balances}

    # Fill in placeholders for any active type with no row — HR view stays
    # read-only (no lazy-insert) so a stray HR page-load on the wrong user
    # doesn't litter the collection. Allocation falls back to daysPerYear
    # so the UI shows the quota the employee will get on first /balance.
    for code, t in types_map.items():
        if not t.get("isActive", True):
            continue
        if code in have:
            continue
        balances.append({
            "userId": userId,
            "leaveTypeCode": code,
            "year": target_year,
            "allocated": _default_allocated(t),
            "used": 0.0,
            "pending": 0.0,
        })

    # Same orphan filter as the user-side endpoint — drop balance rows
    # whose leaveTypeCode points at a deleted/inactive type.
    return [
        _serialize_balance(b, types_map[b["leaveTypeCode"]])
        for b in balances
        if b.get("leaveTypeCode") in types_map
        and types_map[b["leaveTypeCode"]].get("isActive", True)
    ]
