"""Seed sample data for gudivadalovekik so the 'This week' and
'Tasks done' dashboard KPIs show real values instead of '—'.

Every inserted doc carries  _seed: "kpi-demo"  so it can be removed with
  python seed_kpi_demo.py --clean
"""
import sys, asyncio
from datetime import datetime, timedelta, timezone
sys.path.insert(0, "backend")
import config  # noqa: F401  — Atlas
from database import db

UID = "6a11ab0ea8562d8a05472896"  # gudivadalovekik
MGR = "6a1444179f0d49e13cf51bde"  # reporting manager
TAG = {"_seed": "kpi-demo"}


async def clean():
    a = await db.attendance.delete_many(TAG)
    t = await db.tasks.delete_many(TAG)
    print(f"removed {a.deleted_count} attendance + {t.deleted_count} task seeded docs")


async def seed():
    now = datetime.now()
    nowu = datetime.now(timezone.utc)

    # ---- attendance: two checked-out days THIS week ----
    # week_start = Mon 2026-06-29; today = 2026-06-30. Insert both (skip if a
    # row already exists for that date — the (userId,date) index is unique).
    days = [
        ("2026-06-29", 9, 0, 17, 30),   # 8.5h
        ("2026-06-30", 9, 0, 18, 0),    # 9.0h
    ]
    att_inserted = 0
    for date, h1, m1, h2, m2 in days:
        if await db.attendance.find_one({"userId": UID, "date": date}):
            print(f"  attendance {date} already exists — skipped")
            continue
        ci = datetime.strptime(f"{date} {h1:02d}:{m1:02d}", "%Y-%m-%d %H:%M")
        co = datetime.strptime(f"{date} {h2:02d}:{m2:02d}", "%Y-%m-%d %H:%M")
        hours = round((co - ci).total_seconds() / 3600, 2)
        await db.attendance.insert_one({
            **TAG,
            "userId": UID,
            "date": date,
            "attendanceType": "WFH",
            "status": "PRESENT",
            "isLate": False,
            "checkIn": ci,
            "checkOut": co,
            "workNotes": "Demo seeded workday",
            "createdAt": ci,
            "updatedAt": co,
            "hoursWorked": hours,
            "overtimeHours": round(max(0.0, hours - 9), 2),
        })
        att_inserted += 1
        print(f"  + attendance {date} hoursWorked={hours}")

    # ---- tasks: 5 created in last 30d; 3 completed -> 60% done ----
    specs = [
        ("Finalize attendance module QA", "COMPLETED", 8, True),
        ("Write dashboard KPI docs",      "COMPLETED", 6, True),
        ("Fix leave-balance rounding",    "COMPLETED", 4, True),
        ("Refactor reimbursement form",   "ONGOING",   3, None),
        ("Add payslip PDF unit tests",    "PENDING",   2, None),
    ]
    task_inserted = 0
    for title, status, created_days_ago, on_time in specs:
        created = now - timedelta(days=created_days_ago)
        due = (created + timedelta(days=5)).strftime("%Y-%m-%d")
        doc = {
            **TAG,
            "title": title,
            "description": "Demo seeded task",
            "assigneeId": UID,
            "createdById": MGR,
            "priority": "MEDIUM",
            "status": status,
            "dueDate": due,
            "createdAt": created,
            "updatedAt": created,
        }
        if status == "COMPLETED":
            doc["completedAt"] = created + timedelta(days=1)
            doc["onTime"] = bool(on_time)
        await db.tasks.insert_one(doc)
        task_inserted += 1
        print(f"  + task [{status:9}] {title}")

    print(f"\nseeded {att_inserted} attendance + {task_inserted} tasks for gudivadalovekik")


async def main():
    if "--clean" in sys.argv:
        await clean()
    else:
        await seed()

asyncio.run(main())
