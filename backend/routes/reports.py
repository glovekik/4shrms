"""HR + Manager analytics endpoints (PRD section 13).

These return JSON shapes that mirror the Excel exports in routes/exports.py
— UI can render the dashboards directly, and HR can download the same
data as a spreadsheet without a second query path.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from io import BytesIO

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timedelta, timezone, date as _date
from typing import Optional

from database import db
from utils.ist import now_ist_naive
from utils.dependencies import (
    require_hr,
    require_hr_or_ceo,
    require_manager_or_hr,
)
from utils.work_report import build_work_xlsx, build_work_pdf

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
            "profilePictureUrl": by_user[uid].get("profilePictureUrl"),
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
    y = year or now_ist_naive().year
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
            "profilePictureUrl": u.get("profilePictureUrl") if u else None,
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
    userId: Optional[str] = Query(None),      # scope to one employee
    actor: dict = Depends(require_manager_or_hr),
):
    """For each direct report: open tasks, completed-last-30d, avg hours/week.

    Pass `userId` to scope to a single employee (used by the employee
    detail / work-performance view). HR may target anyone; a MANAGER may
    only target their own direct reports.
    """
    actor_id = str(actor["_id"])
    reports = []
    if userId:
        try:
            target_oid = ObjectId(userId)
        except (InvalidId, TypeError):
            raise HTTPException(400, "Invalid userId")
        if actor.get("role") != "HR":
            # MANAGER may only inspect their own direct reports.
            target = await db.users.find_one({
                "_id": target_oid,
                "reportingManagerId": actor_id,
            })
            if not target:
                raise HTTPException(403, "Not one of your direct reports")
        else:
            target = await db.users.find_one({"_id": target_oid})
        if target:
            reports.append(target)
    else:
        async for u in db.users.find({"reportingManagerId": actor_id}):
            reports.append(u)

    if not reports:
        return []

    thirty_days_ago = (now_ist_naive() - timedelta(days=30)).strftime(
        "%Y-%m-%d"
    )
    seven_days_ago = (now_ist_naive() - timedelta(days=7)).strftime(
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
                "$gte": datetime.now(timezone.utc) - timedelta(days=30)
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
            "userName": u.get("name"),
            "openTasks": open_tasks,
            "completedTasksLast30d": completed_30d,
            "avgHoursPerDayLast7d": avg_per_day,
        })
    return out


# ================= WORK REPORT (daily / weekly / monthly) =================
# One detail row per employee per day — name, in/out, hours, work done —
# for HR (whole company) and managers (direct reports). Downloadable as a
# clean Excel or PDF.

_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _fmt_clock(dt) -> Optional[str]:
    """A stored IST wall-clock datetime -> '9:15 AM'."""
    if not dt:
        return None
    try:
        return dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return None


async def _collect_work_rows(user_ids, from_date, to_date) -> list:
    if not user_ids:
        return []
    oids = []
    for x in user_ids:
        try:
            oids.append(ObjectId(x))
        except (InvalidId, TypeError):
            pass
    users = {}
    async for u in db.users.find({"_id": {"$in": oids}}):
        users[str(u["_id"])] = u

    q: dict = {"userId": {"$in": user_ids}}
    dq = _date_range_query(from_date, to_date)
    if dq:
        q["date"] = dq

    rows = []
    async for r in db.attendance.find(q):
        u = users.get(r.get("userId")) or {}
        d = r.get("date") or ""
        day = ""
        try:
            y, m, dd = (int(p) for p in d.split("-"))
            day = _DOW[_date(y, m, dd).weekday()]
        except Exception:
            pass
        rows.append({
            "name": u.get("name") or "—",
            "employeeCode": u.get("employeeCode") or "",
            "date": d,
            "day": day,
            "checkIn": _fmt_clock(r.get("checkIn")),
            "checkOut": _fmt_clock(r.get("checkOut")),
            "hours": round(float(r.get("hoursWorked", 0) or 0), 2),
            "type": r.get("attendanceType") or "",
            "status": r.get("status") or "",
            "workNotes": (r.get("workNotes") or "").strip(),
        })
    rows.sort(key=lambda x: ((x["name"] or "").lower(), x["date"]))
    return rows


def _report_meta(period, from_date, to_date, scope):
    p = (period or "").strip().capitalize()
    title = f"{p} Work Report" if p else "Work Report"
    rng = from_date if from_date == to_date else f"{from_date} to {to_date}"
    return title, f"{rng}  ·  {scope}"


def _xlsx(data: bytes, from_date, to_date):
    return StreamingResponse(
        BytesIO(data),
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={"Content-Disposition":
                 f'attachment; filename="work-report-{from_date}_{to_date}.xlsx"'},
    )


def _pdf(data: bytes, from_date, to_date):
    return StreamingResponse(
        BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition":
                 f'attachment; filename="work-report-{from_date}_{to_date}.pdf"'},
    )


async def _hr_user_ids(department_id: Optional[str]) -> list:
    uq: dict = {}
    if department_id:
        uq["departmentId"] = department_id
    return [str(u["_id"]) async for u in db.users.find(uq, {"_id": 1})]


async def _team_user_ids(manager_id: str) -> list:
    return [
        str(u["_id"])
        async for u in db.users.find(
            {"reportingManagerId": manager_id}, {"_id": 1}
        )
    ]


# ---- HR (whole company) ----
@router.get("/work")
async def hr_work_report(
    fromDate: str = Query(...),
    toDate: str = Query(...),
    departmentId: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr_or_ceo),
):
    uids = await _hr_user_ids(departmentId)
    return await _collect_work_rows(uids, fromDate, toDate)


@router.get("/work/export.xlsx")
async def hr_work_xlsx(
    fromDate: str = Query(...),
    toDate: str = Query(...),
    period: Optional[str] = Query(None),
    departmentId: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr_or_ceo),
):
    rows = await _collect_work_rows(
        await _hr_user_ids(departmentId), fromDate, toDate
    )
    title, sub = _report_meta(period, fromDate, toDate, "All employees")
    return _xlsx(build_work_xlsx(rows, title, sub), fromDate, toDate)


@router.get("/work/export.pdf")
async def hr_work_pdf(
    fromDate: str = Query(...),
    toDate: str = Query(...),
    period: Optional[str] = Query(None),
    departmentId: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr_or_ceo),
):
    rows = await _collect_work_rows(
        await _hr_user_ids(departmentId), fromDate, toDate
    )
    title, sub = _report_meta(period, fromDate, toDate, "All employees")
    return _pdf(build_work_pdf(rows, title, sub), fromDate, toDate)


# ---- Manager (direct reports) ----
@manager_router.get("/work")
async def mgr_work_report(
    fromDate: str = Query(...),
    toDate: str = Query(...),
    actor: dict = Depends(require_manager_or_hr),
):
    uids = await _team_user_ids(str(actor["_id"]))
    return await _collect_work_rows(uids, fromDate, toDate)


@manager_router.get("/work/export.xlsx")
async def mgr_work_xlsx(
    fromDate: str = Query(...),
    toDate: str = Query(...),
    period: Optional[str] = Query(None),
    actor: dict = Depends(require_manager_or_hr),
):
    rows = await _collect_work_rows(
        await _team_user_ids(str(actor["_id"])), fromDate, toDate
    )
    title, sub = _report_meta(period, fromDate, toDate, "My team")
    return _xlsx(build_work_xlsx(rows, title, sub), fromDate, toDate)


@manager_router.get("/work/export.pdf")
async def mgr_work_pdf(
    fromDate: str = Query(...),
    toDate: str = Query(...),
    period: Optional[str] = Query(None),
    actor: dict = Depends(require_manager_or_hr),
):
    rows = await _collect_work_rows(
        await _team_user_ids(str(actor["_id"])), fromDate, toDate
    )
    title, sub = _report_meta(period, fromDate, toDate, "My team")
    return _pdf(build_work_pdf(rows, title, sub), fromDate, toDate)
