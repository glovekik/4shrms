"""Dashboard aggregation endpoints — one HTTP call per dashboard.

PRD section 4 defines three audiences (HR, Manager, Employee). Each
endpoint returns a single object with widget data, so the UI doesn't
need to fan-out into N calls and re-aggregate.
"""

from fastapi import APIRouter, Depends, HTTPException

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone, timedelta
from typing import Optional

from database import db
from utils.dependencies import (
    get_current_user_doc,
    require_hr,
    require_hr_or_ceo,
    require_manager_or_hr,
)

router = APIRouter()

# Statuses that count as "present" for the day. Kept in sync with
# reports.py — COMPLETED (auto-checkout / manual attendance) and HALF_DAY
# are present-equivalent, so omitting them undercounts attendance.
PRESENT_STATUSES = ["CHECKED_IN", "PRESENT", "LATE", "HALF_DAY", "COMPLETED"]


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _upcoming_birthdays_match(days_ahead: int = 14) -> list[dict]:
    """Builds a list of (month, day) tuples for the next `days_ahead` days
    so we can match against `personal.birthday` strings (YYYY-MM-DD)."""
    today = datetime.now().date()
    out = []
    for n in range(days_ahead + 1):
        d = today + timedelta(days=n)
        out.append({"month": d.month, "day": d.day})
    return out


# ================= HR =================
@router.get("/hr")
async def hr_dashboard(
    hr: dict = Depends(require_hr_or_ceo),
):
    today = _today_str()

    total_employees = await db.users.count_documents({
        "$or": [
            {"status": "Active"},
            {"status": {"$exists": False}},
        ]
    })

    # Today's attendance counters
    present_today = await db.attendance.count_documents({
        "date": today,
        "status": {"$in": PRESENT_STATUSES},
    })
    absent_today = await db.attendance.count_documents({
        "date": today,
        "status": "ABSENT",
    })
    on_leave_today = await db.leave_requests.count_documents({
        "status": "APPROVED",
        "fromDate": {"$lte": today},
        "toDate": {"$gte": today},
    })

    pending_leave = await db.leave_requests.count_documents({
        "status": "PENDING",
    })
    pending_correction = await db.correction_requests.count_documents({
        "status": "PENDING",
    })
    # Per-queue counts so the HR Admin tiles can each show their own
    # badge without an extra round-trip.
    pending_reimb_hr = await db.reimbursement_requests.count_documents({
        "status": "PENDING_HR",
    })
    pending_timesheet_hr = await db.timesheets.count_documents({
        "status": "PENDING",
    })
    pending_manual_hr = await db.manual_attendance_requests.count_documents({
        "status": "PENDING",
    })
    pending_onboardings_hr = await db.onboardings.count_documents({
        "status": {"$in": ["PENDING", "IN_PROGRESS"]},
    })

    # Latest payroll run (if any)
    latest_payroll = await db.payroll_runs.find_one(
        sort=[("year", -1), ("month", -1)],
    )
    payroll_status = None
    if latest_payroll:
        payroll_status = {
            "year": latest_payroll.get("year"),
            "month": latest_payroll.get("month"),
            "status": latest_payroll.get("status"),
        }

    # Upcoming birthdays in next 14 days. birthday is stored as YYYY-MM-DD
    # under personal.birthday — we match on month + day, ignoring year.
    targets = _upcoming_birthdays_match(14)
    birthdays: list[dict] = []
    async for u in db.users.find({"personal.birthday": {"$exists": True}}):
        bday = u.get("personal", {}).get("birthday")
        if not bday:
            continue
        try:
            b = datetime.strptime(bday, "%Y-%m-%d").date()
        except ValueError:
            continue
        if {"month": b.month, "day": b.day} in targets:
            birthdays.append({
                "id": str(u["_id"]),
                "name": u.get("name"),
                "birthday": bday,
                "tag": u.get("tag"),
            })

    # Employee distribution by department
    distribution: dict[str, int] = {}
    async for u in db.users.find({}, {"departmentId": 1}):
        dep = u.get("departmentId") or "UNASSIGNED"
        distribution[dep] = distribution.get(dep, 0) + 1

    # Resolve department names for display
    dep_names: dict[str, str] = {}
    dep_ids = [d for d in distribution.keys() if d != "UNASSIGNED"]
    if dep_ids:
        oids = []
        for d in dep_ids:
            try:
                oids.append(ObjectId(d))
            except (InvalidId, TypeError):
                continue
        async for d in db.departments.find({"_id": {"$in": oids}}):
            dep_names[str(d["_id"])] = d.get("name") or ""

    employee_distribution = [
        {
            "departmentId": dep,
            "departmentName": dep_names.get(dep, "Unassigned"),
            "count": count,
        }
        for dep, count in distribution.items()
    ]

    # ===== KPIs =====

    # WFH vs OFFICE split today (counts) — supplements onLeaveToday.
    wfh_today = await db.attendance.count_documents({
        "date": today,
        "attendanceType": "WFH",
    })
    office_today = await db.attendance.count_documents({
        "date": today,
        "attendanceType": "OFFICE",
    })

    # Pending approvals queue — sum of every queue HR sees. Per-queue
    # counts already computed above so we just sum here.
    pending_approvals_total = (
        pending_leave
        + pending_correction
        + pending_reimb_hr
        + pending_timesheet_hr
        + pending_manual_hr
    )

    # Pay-cycle accuracy: % of payslips in the latest run generated
    # without manual override. Returns None if no run yet.
    pay_cycle_accuracy_pct: Optional[float] = None
    if latest_payroll:
        run_id = str(latest_payroll["_id"])
        total_slips = await db.payslips.count_documents({
            "payrollRunId": run_id,
        })
        generated = await db.payslips.count_documents({
            "payrollRunId": run_id,
            "status": "GENERATED",
        })
        if total_slips > 0:
            pay_cycle_accuracy_pct = round(
                (generated / total_slips) * 100, 1
            )

    # Holiday calendar coverage — count for current year.
    current_year = datetime.now().year
    year_prefix = f"{current_year}-"
    holiday_count_this_year = await db.holidays.count_documents({
        "date": {"$regex": f"^{year_prefix}"},
    })

    # Late-arrival rate (last 30 days) — only meaningful when there are
    # rows to count. Skip ABSENT/holiday rows so denominator reflects
    # actual check-ins.
    thirty_days_ago = (
        datetime.now() - timedelta(days=30)
    ).strftime("%Y-%m-%d")
    total_recent = await db.attendance.count_documents({
        "date": {"$gte": thirty_days_ago},
        "checkIn": {"$ne": None},
    })
    late_recent = await db.attendance.count_documents({
        "date": {"$gte": thirty_days_ago},
        "checkIn": {"$ne": None},
        "isLate": True,
    })
    late_arrival_rate_pct = (
        round((late_recent / total_recent) * 100, 1)
        if total_recent > 0 else None
    )

    return {
        "totalEmployees": total_employees,
        "presentToday": present_today,
        "absentToday": absent_today,
        "onLeaveToday": on_leave_today,
        "pendingLeaveApprovals": pending_leave,
        "pendingCorrectionApprovals": pending_correction,
        # Per-queue pending counts so each tile shows its own badge.
        "pendingReimbursementApprovals": pending_reimb_hr,
        "pendingTimesheetApprovals": pending_timesheet_hr,
        "pendingManualAttendanceApprovals": pending_manual_hr,
        "pendingOnboardings": pending_onboardings_hr,
        "payrollStatus": payroll_status,
        "upcomingBirthdays": birthdays,
        "employeeDistribution": employee_distribution,
        # KPI fields
        "wfhToday": wfh_today,
        "officeToday": office_today,
        "pendingApprovalsTotal": pending_approvals_total,
        "payCycleAccuracyPct": pay_cycle_accuracy_pct,
        "holidayCountThisYear": holiday_count_this_year,
        "lateArrivalRatePct": late_arrival_rate_pct,
    }


# ================= MANAGER =================
@router.get("/manager")
async def manager_dashboard(
    actor: dict = Depends(require_manager_or_hr),
):
    actor_id = str(actor["_id"])
    today = _today_str()

    # Direct reports (HR sees no scope filter — falls back to "their" reports,
    # i.e. usually none, so HR effectively gets an empty dashboard here. HR
    # should use /dashboard/hr instead).
    report_ids: list[str] = []
    async for u in db.users.find(
        {"reportingManagerId": actor_id},
        {"_id": 1},
    ):
        report_ids.append(str(u["_id"]))

    if not report_ids:
        return {
            "directReports": 0,
            "teamAttendanceToday": [],
            "pendingLeaveApprovals": 0,
            "pendingCorrectionApprovals": 0,
            "pendingReimbursementApprovals": 0,
            "pendingTimesheetApprovals": 0,
            "pendingManualAttendanceApprovals": 0,
            "openTasksForReports": 0,
            "upcomingDeadlines": [],
            # KPI defaults
            "teamAttendanceRatePctMTD": None,
            "teamWfhRatioPctToday": None,
            "pendingApprovalsTotal": 0,
            "onTimeTaskDeliveryPct30d": None,
            "teamAvgHoursPerDay7d": None,
        }

    # Today's attendance for direct reports
    team_attendance: list[dict] = []
    async for r in db.attendance.find({
        "userId": {"$in": report_ids},
        "date": today,
    }):
        team_attendance.append({
            "userId": r.get("userId"),
            "status": r.get("status"),
            "isLate": r.get("isLate", False),
            "checkIn": (
                r["checkIn"].isoformat()
                if r.get("checkIn") else None
            ),
        })

    pending_leave = await db.leave_requests.count_documents({
        "status": "PENDING",
        "userId": {"$in": report_ids},
    })
    pending_correction = await db.correction_requests.count_documents({
        "status": "PENDING",
        "userId": {"$in": report_ids},
    })

    # Open tasks assigned to direct reports
    open_tasks = await db.tasks.count_documents({
        "assigneeId": {"$in": report_ids},
        "status": {"$in": ["PENDING", "ONGOING"]},
    })

    # Upcoming due-date tasks (next 7 days) for direct reports
    end = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    upcoming: list[dict] = []
    async for t in db.tasks.find({
        "assigneeId": {"$in": report_ids},
        "status": {"$in": ["PENDING", "ONGOING"]},
        "dueDate": {"$gte": today, "$lte": end},
    }).sort("dueDate", 1).limit(10):
        upcoming.append({
            "id": str(t["_id"]),
            "title": t.get("title"),
            "assigneeId": t.get("assigneeId"),
            "dueDate": t.get("dueDate"),
            "priority": t.get("priority", "MEDIUM"),
        })

    # ===== KPIs =====

    # MTD attendance rate for the team. Numerator: days in this month
    # where a report has a non-ABSENT row. Denominator: working-day
    # estimate = team_size * elapsed_workdays_this_month. We use a
    # simpler proxy here — denominator is the count of all rows in the
    # window — to stay accurate against the data we actually store.
    month_start = datetime.now().replace(day=1).strftime("%Y-%m-%d")
    total_mtd = await db.attendance.count_documents({
        "userId": {"$in": report_ids},
        "date": {"$gte": month_start, "$lte": today},
    })
    present_mtd = await db.attendance.count_documents({
        "userId": {"$in": report_ids},
        "date": {"$gte": month_start, "$lte": today},
        "status": {"$nin": ["ABSENT"]},
    })
    team_attendance_rate_pct = (
        round((present_mtd / total_mtd) * 100, 1)
        if total_mtd > 0 else None
    )

    # WFH ratio today — among today's rows for the team.
    today_total = await db.attendance.count_documents({
        "userId": {"$in": report_ids},
        "date": today,
    })
    today_wfh = await db.attendance.count_documents({
        "userId": {"$in": report_ids},
        "date": today,
        "attendanceType": "WFH",
    })
    team_wfh_ratio_pct = (
        round((today_wfh / today_total) * 100, 1)
        if today_total > 0 else None
    )

    # Pending approvals total at this manager (everything queued for them).
    pending_reimb = await db.reimbursement_requests.count_documents({
        "status": "PENDING_MANAGER",
        "userId": {"$in": report_ids},
    })
    pending_timesheet = await db.timesheets.count_documents({
        "status": "PENDING",
        "userId": {"$in": report_ids},
    })
    pending_manual = await db.manual_attendance_requests.count_documents({
        "status": "PENDING",
        "userId": {"$in": report_ids},
    })
    pending_approvals_total = (
        pending_leave
        + pending_correction
        + pending_reimb
        + pending_timesheet
        + pending_manual
    )

    # On-time task delivery (last 30d) — uses the new `onTime` flag.
    # Denominator restricted to completed tasks with a dueDate set, so
    # tasks without a deadline don't skew the ratio.
    thirty_days_ago = datetime.now() - timedelta(days=30)
    completed_with_due = await db.tasks.count_documents({
        "assigneeId": {"$in": report_ids},
        "status": "COMPLETED",
        "completedAt": {"$gte": thirty_days_ago},
        "dueDate": {"$ne": None},
    })
    on_time_done = await db.tasks.count_documents({
        "assigneeId": {"$in": report_ids},
        "status": "COMPLETED",
        "completedAt": {"$gte": thirty_days_ago},
        "dueDate": {"$ne": None},
        "onTime": True,
    })
    on_time_pct = (
        round((on_time_done / completed_with_due) * 100, 1)
        if completed_with_due > 0 else None
    )

    # Team avg hours/day over last 7 days.
    seven_days_ago = (
        datetime.now() - timedelta(days=7)
    ).strftime("%Y-%m-%d")
    total_hours = 0.0
    counted_days = 0
    async for r in db.attendance.find({
        "userId": {"$in": report_ids},
        "date": {"$gte": seven_days_ago},
        "hoursWorked": {"$gt": 0},
    }):
        total_hours += float(r.get("hoursWorked", 0))
        counted_days += 1
    team_avg_hours_per_day_7d = (
        round(total_hours / counted_days, 2)
        if counted_days > 0 else None
    )

    return {
        "directReports": len(report_ids),
        "teamAttendanceToday": team_attendance,
        "pendingLeaveApprovals": pending_leave,
        "pendingCorrectionApprovals": pending_correction,
        # Per-queue counts for tile badges
        "pendingReimbursementApprovals": pending_reimb,
        "pendingTimesheetApprovals": pending_timesheet,
        "pendingManualAttendanceApprovals": pending_manual,
        "openTasksForReports": open_tasks,
        "upcomingDeadlines": upcoming,
        # KPI fields
        "teamAttendanceRatePctMTD": team_attendance_rate_pct,
        "teamWfhRatioPctToday": team_wfh_ratio_pct,
        "pendingApprovalsTotal": pending_approvals_total,
        "onTimeTaskDeliveryPct30d": on_time_pct,
        "teamAvgHoursPerDay7d": team_avg_hours_per_day_7d,
    }


# ================= EMPLOYEE (me) =================
@router.get("/me")
async def my_dashboard(
    user: dict = Depends(get_current_user_doc),
):
    user_id = str(user["_id"])
    today = _today_str()
    year = datetime.now().year

    # Today's attendance
    today_att = await db.attendance.find_one({
        "userId": user_id,
        "date": today,
    })

    if today_att:
        today_summary = {
            "status": today_att.get("status"),
            "attendanceType": today_att.get("attendanceType"),
            "isLate": today_att.get("isLate", False),
            "checkIn": (
                today_att["checkIn"].isoformat()
                if today_att.get("checkIn") else None
            ),
            "checkOut": (
                today_att["checkOut"].isoformat()
                if today_att.get("checkOut") else None
            ),
            "hoursWorked": today_att.get("hoursWorked", 0.0),
        }
    else:
        today_summary = None

    # Leave balances summary
    balances: list[dict] = []
    async for b in db.leave_balances.find({
        "userId": user_id,
        "year": year,
    }):
        allocated = float(b.get("allocated", 0))
        used = float(b.get("used", 0))
        pending = float(b.get("pending", 0))
        balances.append({
            "code": b.get("leaveTypeCode"),
            "allocated": allocated,
            "used": used,
            "pending": pending,
            "remaining": round(allocated - used - pending, 2),
        })

    # Open tasks assigned to me
    open_tasks_count = await db.tasks.count_documents({
        "assigneeId": user_id,
        "status": {"$in": ["PENDING", "ONGOING"]},
    })
    recent_tasks: list[dict] = []
    async for t in db.tasks.find({
        "assigneeId": user_id,
        "status": {"$in": ["PENDING", "ONGOING"]},
    }).sort("createdAt", -1).limit(5):
        recent_tasks.append({
            "id": str(t["_id"]),
            "title": t.get("title"),
            "status": t.get("status"),
            "priority": t.get("priority", "MEDIUM"),
            "dueDate": t.get("dueDate"),
        })

    # My pending requests
    pending_leave = await db.leave_requests.count_documents({
        "userId": user_id,
        "status": "PENDING",
    })
    pending_correction = await db.correction_requests.count_documents({
        "userId": user_id,
        "status": "PENDING",
    })

    # Recent payslips (latest 3)
    recent_payslips: list[dict] = []
    async for p in db.payslips.find({
        "userId": user_id,
    }).sort([("year", -1), ("month", -1)]).limit(3):
        recent_payslips.append({
            "year": p.get("year"),
            "month": p.get("month"),
            "netPay": p.get("netPay"),
            "status": p.get("status"),
        })

    # Unread in-app notifications
    unread_notifications = await db.notifications.count_documents({
        "userId": user_id,
        "read": False,
    })

    # ===== KPIs =====

    month_start = datetime.now().replace(day=1).strftime("%Y-%m-%d")

    # Personal attendance rate MTD. Denominator = all attendance rows
    # for me this month; numerator = non-ABSENT rows. Skipped when zero.
    my_mtd_total = await db.attendance.count_documents({
        "userId": user_id,
        "date": {"$gte": month_start, "$lte": today},
    })
    my_mtd_present = await db.attendance.count_documents({
        "userId": user_id,
        "date": {"$gte": month_start, "$lte": today},
        "status": {"$nin": ["ABSENT"]},
    })
    attendance_rate_pct = (
        round((my_mtd_present / my_mtd_total) * 100, 1)
        if my_mtd_total > 0 else None
    )

    # On-time check-in rate MTD — only rows where I actually checked in.
    my_mtd_checkins = await db.attendance.count_documents({
        "userId": user_id,
        "date": {"$gte": month_start, "$lte": today},
        "checkIn": {"$ne": None},
    })
    my_mtd_ontime = await db.attendance.count_documents({
        "userId": user_id,
        "date": {"$gte": month_start, "$lte": today},
        "checkIn": {"$ne": None},
        "isLate": {"$ne": True},
    })
    on_time_checkin_pct = (
        round((my_mtd_ontime / my_mtd_checkins) * 100, 1)
        if my_mtd_checkins > 0 else None
    )

    # Avg hours/day this week + overtime this month.
    week_start = (
        datetime.now() - timedelta(days=datetime.now().weekday())
    ).strftime("%Y-%m-%d")
    total_hours_week = 0.0
    counted_days = 0
    overtime_month = 0.0
    async for r in db.attendance.find({
        "userId": user_id,
        "date": {"$gte": month_start, "$lte": today},
    }):
        if r.get("date", "") >= week_start and r.get("hoursWorked", 0) > 0:
            total_hours_week += float(r.get("hoursWorked", 0))
            counted_days += 1
        overtime_month += float(r.get("overtimeHours", 0) or 0)
    avg_hours_week = (
        round(total_hours_week / counted_days, 2)
        if counted_days > 0 else None
    )

    # Task completion rate (rolling 30d). Numerator and denominator must
    # cover the SAME population — tasks created in the window — otherwise a
    # task created earlier but completed recently inflates the rate past
    # 100%. So we ask: of tasks created in the last 30d, how many are done.
    thirty_days_ago = datetime.now() - timedelta(days=30)
    total_my_30d = await db.tasks.count_documents({
        "assigneeId": user_id,
        "createdAt": {"$gte": thirty_days_ago},
    })
    completed_my_30d = await db.tasks.count_documents({
        "assigneeId": user_id,
        "createdAt": {"$gte": thirty_days_ago},
        "status": "COMPLETED",
    })
    task_completion_rate_pct = (
        round((completed_my_30d / total_my_30d) * 100, 1)
        if total_my_30d > 0 else None
    )

    # Pending requests — per type so the tiles can each show their own
    # badge — and total for the KPI strip.
    pending_reimb_me = await db.reimbursement_requests.count_documents({
        "userId": user_id,
        "status": {"$in": ["PENDING_MANAGER", "PENDING_HR"]},
    })
    pending_requests_total = pending_leave + pending_correction + pending_reimb_me

    # Required document upload completeness — counts the user's
    # requiredDocuments list. PENDING = not yet uploaded.
    required_total = 0
    required_uploaded = 0
    for item in (user.get("requiredDocuments") or []):
        required_total += 1
        if item.get("status") in ("UPLOADED", "VERIFIED"):
            required_uploaded += 1
    required_completeness_pct = (
        round((required_uploaded / required_total) * 100, 1)
        if required_total > 0 else None
    )

    return {
        "todayAttendance": today_summary,
        "leaveBalances": balances,
        "openTasksCount": open_tasks_count,
        "recentTasks": recent_tasks,
        "pendingLeaveRequests": pending_leave,
        "pendingCorrectionRequests": pending_correction,
        "pendingReimbursementRequests": pending_reimb_me,
        "recentPayslips": recent_payslips,
        "unreadNotifications": unread_notifications,
        # New KPI fields
        "attendanceRatePctMTD": attendance_rate_pct,
        "onTimeCheckInRatePctMTD": on_time_checkin_pct,
        "avgHoursPerDayThisWeek": avg_hours_week,
        "overtimeHoursThisMonth": round(overtime_month, 2),
        "myTaskCompletionRatePct30d": task_completion_rate_pct,
        "pendingRequestsTotal": pending_requests_total,
        "requiredDocCompletenessPct": required_completeness_pct,
    }


# ================= UPCOMING (sidebar widget) =================
@router.get("/upcoming")
async def upcoming_events(
    _user: dict = Depends(get_current_user_doc),
):
    """Next holidays and employee birthdays for the sidebar widget.
    Available to every authenticated user."""
    today = datetime.now().date()
    today_str = today.strftime("%Y-%m-%d")

    # Next holidays (today onward), soonest first.
    holidays: list[dict] = []
    async for h in (
        db.holidays.find({"date": {"$gte": today_str}})
        .sort("date", 1)
        .limit(5)
    ):
        hd = h.get("date")
        try:
            d = datetime.strptime(hd, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        holidays.append({
            "name": h.get("name"),
            "date": hd,
            "daysUntil": (d - today).days,
        })

    # Upcoming birthdays in the next 30 days (match month + day, ignoring
    # year). days_map maps each (month, day) to how many days away it is so
    # we can sort and label without re-deriving per user.
    horizon = 30
    targets = _upcoming_birthdays_match(horizon)
    days_map: dict[tuple, int] = {}
    occ_map: dict[tuple, "datetime.date"] = {}
    for n in range(horizon + 1):
        d = today + timedelta(days=n)
        days_map.setdefault((d.month, d.day), n)
        occ_map.setdefault((d.month, d.day), d)

    birthdays: list[dict] = []
    async for u in db.users.find({
        "personal.birthday": {"$exists": True},
        "status": {"$ne": "Terminated"},
    }):
        bday = u.get("personal", {}).get("birthday")
        if not bday:
            continue
        try:
            b = datetime.strptime(bday, "%Y-%m-%d").date()
        except ValueError:
            continue
        if {"month": b.month, "day": b.day} in targets:
            birthdays.append({
                "id": str(u["_id"]),
                "name": u.get("name"),
                "birthday": bday,
                "daysUntil": days_map.get((b.month, b.day), 0),
                "profilePictureUrl": u.get("profilePictureUrl"),
            })

    birthdays.sort(key=lambda x: x["daysUntil"])

    # Upcoming work anniversaries in the next 30 days. Same month+day match
    # as birthdays, but the user must have completed >= 1 year (so a brand
    # new joiner's first joining-date doesn't show as an "anniversary").
    anniversaries: list[dict] = []
    # New joiners — anyone who joined within the last 30 days.
    new_joiners: list[dict] = []
    thirty_days_ago_str = (today - timedelta(days=30)).strftime("%Y-%m-%d")

    async for u in db.users.find({
        "joiningDate": {"$exists": True, "$ne": None},
        "status": {"$ne": "Terminated"},
    }):
        jd = u.get("joiningDate")
        if not jd:
            continue
        try:
            j = datetime.strptime(jd, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue

        # New joiner (within the last 30 days, joined today or earlier).
        if thirty_days_ago_str <= jd <= today_str:
            new_joiners.append({
                "id": str(u["_id"]),
                "name": u.get("name"),
                "joiningDate": jd,
                "daysAgo": (today - j).days,
                "profilePictureUrl": u.get("profilePictureUrl"),
            })

        # Upcoming anniversary.
        if {"month": j.month, "day": j.day} in targets:
            occ = occ_map.get((j.month, j.day))
            years = (occ.year - j.year) if occ else 0
            if years >= 1:
                anniversaries.append({
                    "id": str(u["_id"]),
                    "name": u.get("name"),
                    "joiningDate": jd,
                    "years": years,
                    "daysUntil": days_map.get((j.month, j.day), 0),
                    "profilePictureUrl": u.get("profilePictureUrl"),
                })

    anniversaries.sort(key=lambda x: x["daysUntil"])
    new_joiners.sort(key=lambda x: x["daysAgo"])

    return {
        "holidays": holidays,
        "birthdays": birthdays,
        "anniversaries": anniversaries,
        "newJoiners": new_joiners,
    }
