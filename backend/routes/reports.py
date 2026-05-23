"""HR + Manager analytics endpoints (PRD section 13).

These return JSON shapes that mirror the Excel exports in routes/exports.py
— UI can render the dashboards directly, and HR can download the same
data as a spreadsheet without a second query path.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timedelta
from typing import Optional

from database import db
from utils.dependencies import (
    require_hr,
    require_hr_or_ceo,
    require_manager_or_hr,
)

router = APIRouter()       # /hr/reports/...
manager_router = APIRouter()  # /manager/reports/...


def _date_range_query(
    from_date: Optional[str], to_date: Optional[str],
) -> dict:
    q: dict = {}
    if from_date:
        q["$gte"] = from_date
    if to_date:
        q["$lte"] = to_date
    return q


# ================= ATTENDANCE SUMMARY =================
@router.get("/attendance")
async def attendance_report(
    fromDate: Optional[str] = Query(None),
    toDate: Optional[str] = Query(None),
    departmentId: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr_or_ceo),
):
    """Per-user attendance summary over a date range.

    Output rows: {userId, name, email, departmentId, totalDays, present,
    late, halfDay, absent, leave, totalHours, overtimeHours}.
    """
    user_query: dict = {}
    if departmentId:
        user_query["departmentId"] = departmentId
    users = []
    async for u in db.users.find(user_query):
        users.append(u)
    user_ids = [str(u["_id"]) for u in users]
    by_user = {str(u["_id"]): u for u in users}

    att_query: dict = {"userId": {"$in": user_ids}}
    date_q = _date_range_query(fromDate, toDate)
    if date_q:
        att_query["date"] = date_q

    summary: dict[str, dict] = {
        uid: {
            "userId": uid,
            "name": by_user[uid].get("name"),
            "email": by_user[uid].get("email"),
            "departmentId": by_user[uid].get("departmentId"),
            "totalDays": 0,
            "present": 0,
            "late": 0,
            "halfDay": 0,
            "absent": 0,
            "totalHours": 0.0,
            "overtimeHours": 0.0,
        }
        for uid in user_ids
    }

    async for r in db.attendance.find(att_query):
        uid = r.get("userId")
        if uid not in summary:
            continue
        row = summary[uid]
        row["totalDays"] += 1
        st = r.get("status")
        if st == "PRESENT" or st == "CHECKED_IN" or st == "COMPLETED":
            row["present"] += 1
        elif st == "LATE":
            row["late"] += 1
            row["present"] += 1  # late still counts as present
        elif st == "HALF_DAY":
            row["halfDay"] += 1
        elif st == "ABSENT":
            row["absent"] += 1
        row["totalHours"] += float(r.get("hoursWorked", 0) or 0)
        row["overtimeHours"] += float(r.get("overtimeHours", 0) or 0)

    # Leave days approved in this range (counted separately to "absent")
    leave_count: dict[str, float] = {}
    leave_q: dict = {"userId": {"$in": user_ids}, "status": "APPROVED"}
    if fromDate:
        leave_q["toDate"] = {"$gte": fromDate}
    if toDate:
        leave_q.setdefault("fromDate", {})["$lte"] = toDate
    async for lr in db.leave_requests.find(leave_q):
        uid = lr.get("userId")
        leave_count[uid] = leave_count.get(uid, 0.0) + float(
            lr.get("totalDays", 0) or 0
        )
    for uid, row in summary.items():
        row["leaveDays"] = round(leave_count.get(uid, 0.0), 2)
        row["totalHours"] = round(row["totalHours"], 2)
        row["overtimeHours"] = round(row["overtimeHours"], 2)

    return list(summary.values())


# ================= LEAVE USAGE =================
@router.get("/leave")
async def leave_report(
    year: int = Query(default=None),
    _hr: dict = Depends(require_hr_or_ceo),
):
    """Per-user leave balance snapshot for a year."""
    y = year or datetime.now().year
    users = {}
    async for u in db.users.find():
        users[str(u["_id"])] = u

    out = []
    async for b in db.leave_balances.find({"year": y}):
        u = users.get(b.get("userId"))
        out.append({
            "userId": b.get("userId"),
            "name": u.get("name") if u else None,
            "email": u.get("email") if u else None,
            "leaveTypeCode": b.get("leaveTypeCode"),
            "allocated": float(b.get("allocated", 0)),
            "used": float(b.get("used", 0)),
            "pending": float(b.get("pending", 0)),
            "remaining": round(
                float(b.get("allocated", 0))
                - float(b.get("used", 0))
                - float(b.get("pending", 0)),
                2,
            ),
            "year": y,
        })
    return out


# ================= PAYROLL COST =================
@router.get("/payroll")
async def payroll_report(
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    _hr: dict = Depends(require_hr_or_ceo),
):
    """Per-payslip net pay + employer cost for a (year, month)."""
    out = []
    async for p in db.payslips.find({"year": year, "month": month}):
        out.append({
            "userId": p.get("userId"),
            "name": p.get("employeeName") or p.get("name"),
            "totalGross": p.get("totalGross"),
            "totalDeductions": p.get("totalDeductions"),
            "netPay": p.get("netPay"),
            "status": p.get("status"),
        })
    return out


# ================= DEPARTMENT HEADCOUNT =================
@router.get("/departments")
async def department_summary(
    _hr: dict = Depends(require_hr_or_ceo),
):
    """Headcount by department + manager + members count."""
    deps = {}
    async for d in db.departments.find():
        deps[str(d["_id"])] = {
            "departmentId": str(d["_id"]),
            "name": d.get("name"),
            "headUserId": d.get("headUserId"),
            "headcount": 0,
        }

    counts: dict[str, int] = {}
    async for u in db.users.find({}, {"departmentId": 1}):
        dep = u.get("departmentId") or "UNASSIGNED"
        counts[dep] = counts.get(dep, 0) + 1

    out = []
    for dep_id, d in deps.items():
        d["headcount"] = counts.get(dep_id, 0)
        out.append(d)
    if "UNASSIGNED" in counts:
        out.append({
            "departmentId": None,
            "name": "Unassigned",
            "headUserId": None,
            "headcount": counts["UNASSIGNED"],
        })
    return out


# ================= ATTRITION TRACKING =================
@router.get("/attrition")
async def attrition_report(
    fromDate: Optional[str] = Query(None),  # YYYY-MM-DD
    toDate: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr_or_ceo),
):
    """Exits between fromDate and toDate."""
    query: dict = {"status": "APPROVED"}
    if fromDate or toDate:
        date_q: dict = {}
        if fromDate:
            try:
                date_q["$gte"] = datetime.strptime(fromDate, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(400, "Invalid fromDate")
        if toDate:
            try:
                date_q["$lte"] = datetime.strptime(toDate, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(400, "Invalid toDate")
        query["lastWorkingDay"] = date_q

    out = []
    async for e in db.exits.find(query).sort("lastWorkingDay", -1):
        out.append({
            "userId": e.get("userId"),
            "name": e.get("employeeName"),
            "reason": e.get("reason"),
            "lastWorkingDay": (
                e["lastWorkingDay"].isoformat()
                if hasattr(e.get("lastWorkingDay"), "isoformat")
                else e.get("lastWorkingDay")
            ),
            "type": e.get("type"),
        })
    return out


# ================= MANAGER: TEAM PRODUCTIVITY =================
@manager_router.get("/team-productivity")
async def team_productivity(
    actor: dict = Depends(require_manager_or_hr),
):
    """For each direct report: open tasks, completed-last-30d, avg hours/week."""
    actor_id = str(actor["_id"])
    reports = []
    async for u in db.users.find({"reportingManagerId": actor_id}):
        reports.append(u)

    if not reports:
        return []

    thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime(
        "%Y-%m-%d"
    )
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime(
        "%Y-%m-%d"
    )

    out = []
    for u in reports:
        uid = str(u["_id"])
        open_tasks = await db.tasks.count_documents({
            "assigneeId": uid,
            "status": {"$in": ["PENDING", "ONGOING"]},
        })
        completed_30d = await db.tasks.count_documents({
            "assigneeId": uid,
            "status": "COMPLETED",
            "completedAt": {
                "$gte": datetime.now() - timedelta(days=30)
            },
        })

        # Avg hours/day over last 7 days
        total = 0.0
        days = 0
        async for r in db.attendance.find({
            "userId": uid,
            "date": {"$gte": seven_days_ago},
            "hoursWorked": {"$gt": 0},
        }):
            total += float(r.get("hoursWorked", 0))
            days += 1
        avg_per_day = round(total / days, 2) if days else 0.0

        out.append({
            "userId": uid,
            "name": u.get("name"),
            "openTasks": open_tasks,
            "completedTasksLast30d": completed_30d,
            "avgHoursPerDayLast7d": avg_per_day,
        })
    return out
