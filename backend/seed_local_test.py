"""Seed a synthetic org into the LOCAL test database.

Exists because the production Mongo host isn't reachable from every network,
so there has to be a way to exercise the app without it. This writes ONLY to a
local mongod and refuses to run against anything else.

The data is shaped to exercise the bugs fixed on 2026-08-26, so each fix has
something to look at in the UI:

  * attendance KPI   — Eshan has a half-day leave on a day he also worked
                       (halfDay=True with status COMPLETED, the combination
                       that used to score 200% for that day)
  * project delete   — "CCTV 360" has tasks and chat, so deleting it must be
                       refused; "Scratch Project" is empty, so it deletes
  * group edit       — the group is created by Asha, so Bhavna (also HR) must
                       not be able to rename it or change its members
  * CEO access       — Chandra is a CEO and must be able to open any task
  * profile photos   — two people have profilePictureUrl set
  * PM permissions   — Divya manages CCTV 360 by membership alone; her global
                       role is plain USER
  * membership history — Farah has a closed stay on AI4AP Police

Salary structures are deliberately NOT seeded: they're created through the API
during testing so the backdating path is exercised for real.

    python seed_local_test.py            # wipe + seed
"""

import asyncio
from datetime import datetime, timedelta, timezone

from bson import ObjectId

import config

if "127.0.0.1" not in config.MONGO_URL and "localhost" not in config.MONGO_URL:
    raise SystemExit(
        f"Refusing to seed: MONGO_URL is {config.MONGO_URL!r}, which is not a "
        "local mongod. This script only ever writes to localhost."
    )

from database import db  # noqa: E402  (import after the guard above)
from routes.auth import hash_password  # noqa: E402

PASSWORD = "Test@1234"
NOW = datetime.now(timezone.utc)
TODAY = datetime.now().date()
MONTH_START = TODAY.replace(day=1)

WEEKEND = set(config.WEEKEND_DAYS)


def working_days(start, end):
    out, d = [], start
    while d <= end:
        if d.weekday() not in WEEKEND:
            out.append(d)
        d += timedelta(days=1)
    return out


def oid() -> ObjectId:
    return ObjectId()


async def main():
    print(f"DB: {config.MONGO_DB_NAME} @ {config.MONGO_URL}")

    for col in (
        "users", "departments", "projects", "project_members", "teams",
        "tasks", "chat_groups", "chat_messages", "chat_reads", "attendance",
        "leave_requests", "salary_structures", "notifications", "holidays",
        "audit_logs", "payroll_runs", "payslips",
    ):
        await db[col].delete_many({})
    print("cleared collections")

    # ---- departments ----
    tech_id, hr_id = oid(), oid()
    await db.departments.insert_many([
        {"_id": tech_id, "name": "Tech", "description": "Engineering & delivery",
         "createdAt": NOW, "updatedAt": NOW},
        {"_id": hr_id, "name": "HR", "description": "People & operations",
         "createdAt": NOW, "updatedAt": NOW},
    ])

    # ---- people ----
    def person(name, email, role, title, dept, code, photo=None, mgr=None):
        return {
            "_id": oid(),
            "name": name,
            "email": email,
            "password": hash_password(PASSWORD),
            "role": role,
            "tag": {"HR": "HR", "CEO": "CEO", "MANAGER": "Manager"}.get(role, "Employee"),
            "status": "Active",
            "employeeCode": code,
            "joiningDate": "2025-04-01",
            "workPhone": "+91 90000 0000",
            "profilePictureUrl": photo,
            "reportingManagerId": mgr,
            "departmentId": str(dept),
            "work": {
                "departmentId": str(dept),
                "jobTitle": title,
                "reportingManagerId": mgr,
                "workLocation": "Hyderabad",
            },
            "personal": {"birthday": "1995-07-14"},
            "createdAt": NOW,
            "updatedAt": NOW,
        }

    ceo = person("Chandra Rao", "ceo@test.local", "CEO", "Chief Executive Officer", tech_id, "E001")
    asha = person("Asha Reddy", "hr@test.local", "HR", "HR Manager", hr_id, "E002")
    bhavna = person("Bhavna Iyer", "hr2@test.local", "HR", "HR Executive", hr_id, "E003")
    gopal = person("Gopal Menon", "mgr@test.local", "MANAGER", "Engineering Manager", tech_id, "E004")
    divya = person("Divya Sharma", "pm@test.local", "USER", "Senior Engineer", tech_id, "E005",
                   photo="https://i.pravatar.cc/300?img=47", mgr=str(gopal["_id"]))
    eshan = person("Eshan Kumar", "emp1@test.local", "USER", "Engineer", tech_id, "E006",
                   photo="https://i.pravatar.cc/300?img=12", mgr=str(gopal["_id"]))
    farah = person("Farah Naz", "emp2@test.local", "USER", "Engineer", tech_id, "E007",
                   mgr=str(gopal["_id"]))
    # Nobody assigned to a department — exercises the org chart's "unassigned"
    # bucket, which exists precisely so people don't silently vanish.
    idris = person("Idris Khan", "emp3@test.local", "USER", "Designer", tech_id, "E008")
    idris["departmentId"] = None
    idris["work"]["departmentId"] = None

    people = [ceo, asha, bhavna, gopal, divya, eshan, farah, idris]
    await db.users.insert_many(people)
    await db.departments.update_one({"_id": tech_id}, {"$set": {"headUserId": str(gopal["_id"])}})
    await db.departments.update_one({"_id": hr_id}, {"$set": {"headUserId": str(asha["_id"])}})
    print(f"{len(people)} users (password for all: {PASSWORD})")

    # ---- projects ----
    cctv, ai4ap, scratch = oid(), oid(), oid()
    await db.projects.insert_many([
        {"_id": cctv, "name": "CCTV 360", "code": "CCTV", "description": "City surveillance rollout",
         "departmentId": str(tech_id), "status": "Active", "startDate": "2025-09-01",
         "endDate": None, "createdAt": NOW, "updatedAt": NOW},
        {"_id": ai4ap, "name": "AI4AP Police", "code": "AI4AP", "description": "Police analytics",
         "departmentId": str(tech_id), "status": "Active", "startDate": "2025-06-01",
         "endDate": None, "createdAt": NOW, "updatedAt": NOW},
        {"_id": scratch, "name": "Scratch Project", "code": "SCRATCH",
         "description": "Empty on purpose — deleting this one should succeed",
         "departmentId": str(tech_id), "status": "OnHold", "startDate": "2026-08-01",
         "endDate": None, "createdAt": NOW, "updatedAt": NOW},
    ])

    def stay(pid, uid, role, joined, left=None):
        return {
            "projectId": str(pid), "userId": str(uid), "role": role,
            "joinedAt": joined, "leftAt": left,
            "addedBy": str(asha["_id"]),
            "removedBy": str(asha["_id"]) if left else None,
            "createdAt": NOW, "updatedAt": NOW,
        }

    await db.project_members.insert_many([
        stay(cctv, divya["_id"], "manager", "2025-09-01"),
        stay(cctv, eshan["_id"], "member", "2025-09-01"),
        stay(cctv, farah["_id"], "member", "2025-11-15"),
        stay(ai4ap, divya["_id"], "manager", "2025-06-01"),
        stay(ai4ap, eshan["_id"], "member", "2025-06-01"),
        # Closed stay — gives the member-history view something real to show.
        stay(ai4ap, farah["_id"], "member", "2025-06-01", "2026-03-31"),
    ])
    print("3 projects, 6 membership stays (1 closed)")

    # ---- tasks (CCTV has work on it, so it must refuse deletion) ----
    def task(pid, title, assignee, status, priority="MEDIUM", days_ago=3):
        created = NOW - timedelta(days=days_ago)
        return {
            "_id": oid(), "teamId": None, "projectId": str(pid) if pid else None,
            "title": title, "description": "Seeded for local testing.",
            "assigneeId": str(assignee), "createdBy": str(divya["_id"]),
            "createdByRole": "USER", "status": status, "priority": priority,
            "dueDate": (TODAY + timedelta(days=5)).isoformat(),
            "attachments": [], "reminderIntervalMinutes": None,
            "createdAt": created, "updatedAt": created,
            "startedAt": created if status != "PENDING" else None,
            "completedAt": NOW if status == "COMPLETED" else None,
        }

    await db.tasks.insert_many([
        task(cctv, "Wire up camera health endpoint", eshan["_id"], "ONGOING", "HIGH"),
        task(cctv, "Night-vision calibration pass", farah["_id"], "PENDING", "CRITICAL"),
        task(cctv, "Ship the operator dashboard", eshan["_id"], "COMPLETED"),
        task(ai4ap, "FIR text classifier v2", eshan["_id"], "ONGOING", "HIGH"),
        # No project — a personal task, which is legitimate and must stay so.
        task(None, "Book the quarterly team offsite", gopal["_id"], "PENDING", "LOW"),
    ])
    print("5 tasks (4 on projects, 1 projectless)")

    # ---- chat: office + project + an HR-created group ----
    def msg(ctype, cid, uid, text, mins_ago):
        return {
            "_id": oid(), "channelType": ctype, "channelId": cid,
            "userId": str(uid), "text": text, "mentions": [], "attachments": [],
            "createdAt": NOW - timedelta(minutes=mins_ago),
            "updatedAt": NOW - timedelta(minutes=mins_ago),
        }

    group_id = oid()
    await db.chat_groups.insert_one({
        "_id": group_id, "name": "Diwali Planning",
        "description": "Ad-hoc group — only Asha (its creator) may rename or delete it",
        "memberIds": [str(asha["_id"]), str(bhavna["_id"]), str(eshan["_id"]), str(divya["_id"])],
        "createdBy": str(asha["_id"]), "createdAt": NOW, "updatedAt": NOW,
    })
    await db.chat_messages.insert_many([
        msg("office", None, ceo["_id"], "Morning all — good week ahead.", 240),
        msg("office", None, asha["_id"], "Payslips go out Friday.", 180),
        msg("project", str(cctv), divya["_id"], "Camera 12 is offline again.", 90),
        msg("project", str(cctv), eshan["_id"], "On it — looks like the PoE switch.", 60),
        msg("project", str(ai4ap), divya["_id"], "Classifier v2 numbers look good.", 45),
        msg("group", str(group_id), asha["_id"], "Venue options by Thursday please 🪔", 30),
        msg("group", str(group_id), bhavna["_id"], "I'll collect the quotes.", 20),
    ])
    print("1 chat group + 7 messages across office/project/group (all unread)")

    # ---- attendance: the half-day-leave case the KPI fix is about ----
    wd = working_days(MONTH_START, TODAY)
    half_day = wd[1] if len(wd) > 1 else wd[0]
    absent_days = set(wd[-2:]) if len(wd) > 3 else set()

    rows = []
    for d in wd:
        ds = d.isoformat()
        for u in (eshan, farah, divya, gopal):
            if u is eshan and d in absent_days:
                rows.append({"userId": str(u["_id"]), "date": ds, "status": "ABSENT",
                             "attendanceType": None, "hoursWorked": 0.0,
                             "checkIn": None, "checkOut": None,
                             "createdAt": NOW, "updatedAt": NOW})
                continue
            rec = {
                "userId": str(u["_id"]), "date": ds, "status": "COMPLETED",
                "attendanceType": "OFFICE", "hoursWorked": 8.5,
                "isLate": False, "halfDay": False, "overtimeHours": 0.0,
                "checkIn": datetime.combine(d, datetime.min.time()) + timedelta(hours=9, minutes=30),
                "checkOut": datetime.combine(d, datetime.min.time()) + timedelta(hours=18),
                "workNotes": "", "createdAt": NOW, "updatedAt": NOW,
            }
            # THE case: Eshan worked, then HR approved a half-day leave for the
            # same date. leave.py annotates the record — halfDay goes True but
            # status stays COMPLETED. Before the fix this day scored 200%.
            if u is eshan and d == half_day:
                rec.update({
                    "halfDay": True, "halfDayPart": "SECOND_HALF",
                    "hoursWorked": 4.25,
                    "workNotes": "Half day — personal",
                })
            rows.append(rec)
    await db.attendance.insert_many(rows)

    await db.leave_requests.insert_one({
        "_id": oid(), "userId": str(eshan["_id"]), "leaveTypeCode": "CL",
        "fromDate": half_day.isoformat(), "toDate": half_day.isoformat(),
        "isHalfDay": True, "halfDayPart": "SECOND_HALF", "totalDays": 0.5,
        "reason": "Personal errand", "status": "APPROVED",
        "decidedBy": str(asha["_id"]), "decidedAt": NOW,
        "createdAt": NOW - timedelta(days=6), "updatedAt": NOW,
    })
    print(f"{len(rows)} attendance rows; Eshan half-day on {half_day}, "
          f"absent {sorted(x.isoformat() for x in absent_days)}")

    # ---- legacy teams, so the migration dry run has something to chew on ----
    await db.teams.insert_many([
        {"_id": oid(), "name": "CCTV 360", "teamLeadId": str(divya["_id"]),
         "memberIds": [str(eshan["_id"]), str(farah["_id"])],
         "createdAt": NOW, "updatedAt": NOW},
        {"_id": oid(), "name": "AI4AP Police", "teamLeadId": str(divya["_id"]),
         "memberIds": [str(eshan["_id"])], "createdAt": NOW, "updatedAt": NOW},
    ])

    print("\n" + "=" * 58)
    print("LOGINS — password for every account:", PASSWORD)
    for u in people:
        note = ""
        if u is divya:
            note = "  (plain USER, but manages CCTV 360 + AI4AP by membership)"
        if u is asha:
            note = "  (created the Diwali group)"
        if u is bhavna:
            note = "  (HR, but NOT the group creator)"
        print(f"  {u['email']:<20} {u['role']:<8} {u['name']}{note}")
    print("=" * 58)


asyncio.run(main())
