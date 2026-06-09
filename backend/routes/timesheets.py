"""Weekly timesheets per PRD section 10.

Default behavior: auto-fill from attendance.hoursWorked. The employee
can override per-day on submit (add notes / project allocation).
Manager (or HR) approves the submitted week.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timedelta, timezone
from typing import Optional

from database import db
from utils.dependencies import (
    get_current_user,
    require_hr,
    require_manager_or_hr,
    can_decide_for_employee,
)
from utils.audit import log_audit
from utils.notify import notify_user, notify_approvers
from models.timesheet import (
    TimesheetEntry,
    TimesheetSubmit,
    TimesheetDecision,
)


user_router = APIRouter()      # /timesheets/...
manager_router = APIRouter()   # /manager/timesheets/...
hr_router = APIRouter()        # /hr/timesheets/...


def _parse_date(s: str, field: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise HTTPException(400, f"Invalid {field} (use YYYY-MM-DD)")


def _week_dates(week_start: str) -> list[str]:
    start = _parse_date(week_start, "weekStart")
    if start.weekday() != 0:
        raise HTTPException(400, "weekStart must be a Monday")
    return [
        (start + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(7)
    ]


async def _auto_entries_from_attendance(
    user_id: str, dates: list[str],
) -> list[dict]:
    attendance = {}
    async for r in db.attendance.find(
        {"userId": user_id, "date": {"$in": dates}}
    ):
        attendance[r["date"]] = r
    return [
        {
            "date": d,
            "hours": float(attendance.get(d, {}).get("hoursWorked", 0.0)),
            "projectId": None,
            "notes": "",
            "billable": False,
            "attendanceStatus": attendance.get(d, {}).get("status"),
        }
        for d in dates
    ]


def _serialize(t: dict) -> dict:
    return {
        "id": str(t["_id"]),
        "userId": t.get("userId"),
        "weekStart": t.get("weekStart"),
        "entries": t.get("entries", []),
        "totalHours": t.get("totalHours", 0.0),
        "note": t.get("note"),
        "status": t.get("status"),
        "decidedBy": t.get("decidedBy"),
        "decisionNote": t.get("decisionNote"),
        "decidedAt": (
            t["decidedAt"].isoformat()
            if t.get("decidedAt") else None
        ),
        "createdAt": (
            t["createdAt"].isoformat()
            if t.get("createdAt") else None
        ),
    }


# ================= USER: WEEKLY VIEW =================
@user_router.get("/my")
async def my_week(
    weekStart: str = Query(...),
    user_id: str = Depends(get_current_user),
):
    """Returns the user's timesheet for the given week. If a submitted
    timesheet exists, returns it. Otherwise auto-builds a draft from
    attendance — the UI can show it and POST /timesheets/submit when done.
    """
    dates = _week_dates(weekStart)

    existing = await db.timesheets.find_one({
        "userId": user_id, "weekStart": weekStart,
    })
    if existing:
        return {**_serialize(existing), "draft": False}

    entries = await _auto_entries_from_attendance(user_id, dates)
    total = round(sum(e["hours"] for e in entries), 2)
    return {
        "id": None,
        "userId": user_id,
        "weekStart": weekStart,
        "entries": entries,
        "totalHours": total,
        "status": "DRAFT",
        "draft": True,
    }


@user_router.post("/submit")
async def submit_timesheet(
    data: TimesheetSubmit,
    user_id: str = Depends(get_current_user),
):
    dates = _week_dates(data.weekStart)

    existing = await db.timesheets.find_one({
        "userId": user_id, "weekStart": data.weekStart,
    })
    if existing and existing.get("status") not in ("REJECTED",):
        raise HTTPException(
            400,
            f"Timesheet already in status {existing.get('status')} — "
            "ask your manager to reject it first to resubmit",
        )

    if data.entries:
        # Validate each entry's date is within the week.
        for e in data.entries:
            if e.date not in dates:
                raise HTTPException(
                    400, f"entry date {e.date} not in this week",
                )
        entries = [
            {
                "date": e.date,
                "hours": float(e.hours or 0),
                "projectId": e.projectId,
                "notes": e.notes or "",
                "billable": bool(e.billable),
            }
            for e in data.entries
        ]
    else:
        entries = await _auto_entries_from_attendance(user_id, dates)

    total = round(sum(float(e.get("hours", 0)) for e in entries), 2)
    now = datetime.now(timezone.utc)

    doc = {
        "userId": user_id,
        "weekStart": data.weekStart,
        "entries": entries,
        "totalHours": total,
        "note": data.note or "",
        "status": "PENDING",
        "decidedBy": None,
        "decisionNote": "",
        "decidedAt": None,
        "createdAt": now,
        "updatedAt": now,
    }

    if existing:
        await db.timesheets.update_one(
            {"_id": existing["_id"]},
            {"$set": doc},
        )
        record_id = existing["_id"]
    else:
        result = await db.timesheets.insert_one(doc)
        record_id = result.inserted_id

    # Notify approvers (reporting manager + HR) of the submitted timesheet.
    who_doc = await db.users.find_one({"_id": ObjectId(user_id)}, {"name": 1})
    who = (who_doc or {}).get("name") or "An employee"
    await notify_approvers(
        user_id,
        "timesheet_submitted",
        "Timesheet submitted",
        f"{who} submitted a timesheet for week of {data.weekStart} "
        f"({total}h)",
        {"timesheetId": str(record_id)},
    )

    saved = await db.timesheets.find_one({"_id": record_id})
    return _serialize(saved)


# ================= MANAGER: LIST + DECIDE =================
@manager_router.get("")
async def manager_list_timesheets(
    status: Optional[str] = Query("PENDING"),
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

    out = []
    async for t in db.timesheets.find(query).sort("weekStart", -1):
        out.append(_serialize(t))
    return out


@manager_router.post("/{id}/decide")
async def manager_decide_timesheet(
    id: str,
    data: TimesheetDecision,
    actor: dict = Depends(require_manager_or_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    t = await db.timesheets.find_one({"_id": oid})
    if not t:
        raise HTTPException(404, "Timesheet not found")
    if t.get("status") != "PENDING":
        raise HTTPException(400, f"Already {t.get('status')}")

    try:
        employee = await db.users.find_one(
            {"_id": ObjectId(t["userId"])}
        )
    except (InvalidId, TypeError, KeyError):
        employee = None
    if not employee:
        raise HTTPException(404, "Employee no longer exists")
    if not can_decide_for_employee(actor, employee):
        raise HTTPException(403, "Not one of your direct reports")

    now = datetime.now(timezone.utc)
    actor_id = str(actor["_id"])
    new_status = "APPROVED" if data.action == "APPROVE" else "REJECTED"

    await db.timesheets.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": new_status,
                "decidedBy": actor_id,
                "decisionNote": data.note or "",
                "decidedAt": now,
                "updatedAt": now,
            }
        },
    )

    title = (
        f"Timesheet approved (week of {t.get('weekStart')})"
        if data.action == "APPROVE"
        else f"Timesheet rejected (week of {t.get('weekStart')})"
    )
    await notify_user(
        t["userId"],
        "timesheet_decision",
        title,
        data.note or "",
        {"timesheetId": id, "outcome": data.action},
    )
    await log_audit(
        actor_id=actor_id,
        action=f"timesheet.{data.action.lower()}",
        entity_type="timesheets",
        entity_id=id,
    )
    return {"message": f"Timesheet {new_status.lower()}"}


# ================= HR: ORG-WIDE VIEW =================
@hr_router.get("")
async def hr_list_timesheets(
    status: Optional[str] = Query(None),
    userId: Optional[str] = Query(None),
    weekStart: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _hr: dict = Depends(require_hr),
):
    query: dict = {}
    if status:
        query["status"] = status
    if userId:
        query["userId"] = userId
    if weekStart:
        query["weekStart"] = weekStart
    out = []
    async for t in db.timesheets.find(query).sort(
        "weekStart", -1
    ).limit(limit):
        out.append(_serialize(t))
    return out
