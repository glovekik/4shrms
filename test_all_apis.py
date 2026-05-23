"""End-to-end API smoke test.

Walks the entire endpoint surface (~232 routes) in dependency order:
  1. Bootstrap HR account (or reuse existing)
  2. Create department, manager, employee, project
  3. Exercise each module's endpoints with valid data
  4. Capture every failing call (5xx + unexpected 4xx)

Run while uvicorn is up on http://localhost:8000.
Prints a final summary of failures.
"""

import sys
import json
import uuid
import asyncio
from typing import Any, Optional
from datetime import datetime, timezone

import httpx

# Direct DB access — used only to seed a leave balance so the
# /leaves/request → manager decide flow can be exercised end-to-end.
sys.path.insert(0, "backend")
from database import db as _db  # noqa: E402

BASE = "http://localhost:8001"

# Random suffix so re-runs don't collide on unique fields (email, code, etc.)
RUN = uuid.uuid4().hex[:8]

# (label, method, path, status_code, body_or_error)
results: list[tuple[str, str, str, int, Any]] = []


def record(label: str, method: str, path: str, r: httpx.Response) -> Any:
    body: Any
    try:
        body = r.json()
    except Exception:
        body = r.text[:500]
    ok = 200 <= r.status_code < 300
    results.append((label, method, path, r.status_code, None if ok else body))
    if not ok:
        print(f"  FAIL {method} {path} -> {r.status_code}: {body}")
    else:
        print(f"  ok   {method} {path} -> {r.status_code}")
    return body


def expect_ok(label: str, method: str, path: str, r: httpx.Response) -> Any:
    body = record(label, method, path, r)
    return body


async def try_login(client: httpx.AsyncClient, email: str, pwd: str) -> Optional[str]:
    r = await client.post(f"{BASE}/auth/login", json={"email": email, "password": pwd})
    if r.status_code == 200:
        data = r.json()
        if "access_token" in data:
            return data["access_token"]
    return None


async def main() -> int:
    async with httpx.AsyncClient(timeout=30) as client:
        # ===== 1. Bootstrap HR =====
        # Pre-seeded by bootstrap_test_hr.py
        hr_email = "test-hr@apitest.example.com"
        hr_pwd = "test-hr-pass-123"
        hr_token = await try_login(client, hr_email, hr_pwd)
        if not hr_token:
            print("[bootstrap] FATAL: HR login failed. "
                  "Run bootstrap_test_hr.py first.")
            return 1
        print(f"[bootstrap] HR logged in: {hr_email}")

        hr_h = {"Authorization": f"Bearer {hr_token}"}

        # ===== 2. Self =====
        print("\n=== AUTH / SELF ===")
        expect_ok("auth.me", "GET", "/auth/me",
                  await client.get(f"{BASE}/auth/me", headers=hr_h))

        # Push token (register + delete)
        expect_ok("push.register", "POST", "/auth/push-token",
                  await client.post(f"{BASE}/auth/push-token", headers=hr_h,
                                    json={"token": f"tok-{RUN}", "platform": "ios"}))
        expect_ok("push.delete", "DELETE", "/auth/push-token",
                  await client.request("DELETE", f"{BASE}/auth/push-token", headers=hr_h,
                                       json={"token": f"tok-{RUN}"}))

        # ===== 3. Department + users =====
        print("\n=== DEPARTMENTS + USERS ===")
        dep = expect_ok("dept.create", "POST", "/hr/departments",
                        await client.post(f"{BASE}/hr/departments", headers=hr_h,
                                          json={"name": f"Eng-{RUN}", "description": "test"}))
        dep_id = dep.get("id") if isinstance(dep, dict) else None

        expect_ok("dept.list.user", "GET", "/departments",
                  await client.get(f"{BASE}/departments", headers=hr_h))
        expect_ok("dept.list.hr", "GET", "/hr/departments/{id}",
                  await client.get(f"{BASE}/hr/departments/{dep_id}", headers=hr_h))
        expect_ok("dept.update", "PUT", "/hr/departments/{id}",
                  await client.put(f"{BASE}/hr/departments/{dep_id}", headers=hr_h,
                                   json={"description": "updated"}))

        # Manager user
        mgr_email = f"mgr-{RUN}@apitest.example.com"
        mgr_pwd = "mgrpass123"
        mgr = expect_ok("user.create.mgr", "POST", "/hr/users",
                        await client.post(f"{BASE}/hr/users", headers=hr_h,
                                          json={
                                              "name": "Test Manager",
                                              "email": mgr_email,
                                              "password": mgr_pwd,
                                              "role": "MANAGER",
                                              "departmentId": dep_id,
                                              "employeeCode": f"M-{RUN}",
                                          }))
        mgr_id = mgr.get("id") if isinstance(mgr, dict) else None

        emp_email = f"emp-{RUN}@apitest.example.com"
        emp_pwd = "emppass123"
        emp = expect_ok("user.create.emp", "POST", "/hr/users",
                        await client.post(f"{BASE}/hr/users", headers=hr_h,
                                          json={
                                              "name": "Test Employee",
                                              "email": emp_email,
                                              "password": emp_pwd,
                                              "role": "USER",
                                              "departmentId": dep_id,
                                              "reportingManagerId": mgr_id,
                                              "employeeCode": f"E-{RUN}",
                                          }))
        emp_id = emp.get("id") if isinstance(emp, dict) else None

        expect_ok("user.list", "GET", "/hr/users",
                  await client.get(f"{BASE}/hr/users", headers=hr_h))
        expect_ok("user.get", "GET", "/hr/users/{id}",
                  await client.get(f"{BASE}/hr/users/{emp_id}", headers=hr_h))
        expect_ok("user.update", "PUT", "/hr/users/{id}",
                  await client.put(f"{BASE}/hr/users/{emp_id}", headers=hr_h,
                                   json={"workPhone": "+91-9000000001"}))

        # Log in as manager + employee
        mgr_token = await try_login(client, mgr_email, mgr_pwd)
        emp_token = await try_login(client, emp_email, emp_pwd)
        if not (mgr_token and emp_token):
            print("[bootstrap] could not log in manager/employee — abort")
            return 1
        mgr_h = {"Authorization": f"Bearer {mgr_token}"}
        emp_h = {"Authorization": f"Bearer {emp_token}"}

        # ===== 4. Teams =====
        print("\n=== TEAMS ===")
        team = expect_ok("team.create", "POST", "/hr/teams",
                         await client.post(f"{BASE}/hr/teams", headers=hr_h,
                                           json={"name": f"Team-{RUN}",
                                                 "teamLeadId": mgr_id,
                                                 "memberIds": [emp_id]}))
        team_id = team.get("id") if isinstance(team, dict) else None
        expect_ok("team.list", "GET", "/hr/teams",
                  await client.get(f"{BASE}/hr/teams", headers=hr_h))
        expect_ok("team.get", "GET", "/hr/teams/{id}",
                  await client.get(f"{BASE}/hr/teams/{team_id}", headers=hr_h))
        expect_ok("team.update", "PUT", "/hr/teams/{id}",
                  await client.put(f"{BASE}/hr/teams/{team_id}", headers=hr_h,
                                   json={"name": f"Team-{RUN}-renamed"}))
        expect_ok("tl.myteams", "GET", "/tl/teams/mine",
                  await client.get(f"{BASE}/tl/teams/mine", headers=mgr_h))

        # ===== 5. Attendance =====
        print("\n=== ATTENDANCE ===")
        from datetime import datetime, timedelta
        today = datetime.now().strftime("%Y-%m-%d")
        expect_ok("att.checkin", "POST", "/attendance/checkin",
                  await client.post(f"{BASE}/attendance/checkin", headers=emp_h,
                                    json={"date": today, "attendanceType": "WFH"}))
        expect_ok("att.today", "GET", "/attendance/today",
                  await client.get(f"{BASE}/attendance/today", headers=emp_h))
        expect_ok("att.history", "GET", "/attendance/history",
                  await client.get(f"{BASE}/attendance/history", headers=emp_h))
        expect_ok("att.checkout", "POST", "/attendance/checkout",
                  await client.post(f"{BASE}/attendance/checkout", headers=emp_h,
                                    json={"date": today, "workNotes": "Worked on tests"}))

        # ===== 6. Tasks =====
        print("\n=== TASKS ===")
        task = expect_ok("task.create", "POST", "/tl/teams/{teamId}/tasks",
                         await client.post(f"{BASE}/tl/teams/{team_id}/tasks", headers=mgr_h,
                                           json={"title": "Test task",
                                                 "assigneeId": emp_id,
                                                 "priority": "HIGH"}))
        task_id = task.get("id") if isinstance(task, dict) else None
        expect_ok("tasks.my", "GET", "/tasks/my",
                  await client.get(f"{BASE}/tasks/my", headers=emp_h))
        expect_ok("tasks.get", "GET", "/tasks/{id}",
                  await client.get(f"{BASE}/tasks/{task_id}", headers=emp_h))
        expect_ok("tasks.start", "POST", "/tasks/{id}/start",
                  await client.post(f"{BASE}/tasks/{task_id}/start", headers=emp_h))
        expect_ok("tasks.complete", "POST", "/tasks/{id}/complete",
                  await client.post(f"{BASE}/tasks/{task_id}/complete", headers=emp_h))
        expect_ok("tasks.uncomplete", "POST", "/tasks/{id}/uncomplete",
                  await client.post(f"{BASE}/tasks/{task_id}/uncomplete", headers=emp_h))
        expect_ok("tasks.list-team", "GET", "/tl/teams/{teamId}/tasks",
                  await client.get(f"{BASE}/tl/teams/{team_id}/tasks", headers=mgr_h))
        expect_ok("tasks.update", "PUT", "/tl/tasks/{id}",
                  await client.put(f"{BASE}/tl/tasks/{task_id}", headers=mgr_h,
                                   json={"priority": "MEDIUM"}))
        # Comments
        cmt = expect_ok("tasks.comment.add", "POST", "/tasks/{id}/comments",
                        await client.post(f"{BASE}/tasks/{task_id}/comments", headers=emp_h,
                                          json={"text": "Test comment"}))
        cmt_id = cmt.get("id") if isinstance(cmt, dict) else None
        expect_ok("tasks.comment.list", "GET", "/tasks/{id}/comments",
                  await client.get(f"{BASE}/tasks/{task_id}/comments", headers=emp_h))
        if cmt_id:
            expect_ok("tasks.comment.del", "DELETE", "/tasks/{id}/comments/{commentId}",
                      await client.delete(f"{BASE}/tasks/{task_id}/comments/{cmt_id}", headers=emp_h))

        # ===== 7. Leave =====
        print("\n=== LEAVE ===")
        # Create a leave type
        lt = expect_ok("leave.type.create", "POST", "/hr/leave-types",
                       await client.post(f"{BASE}/hr/leave-types", headers=hr_h,
                                         json={"code": f"EARN{RUN}", "name": "Earned",
                                               "daysPerMonth": 1, "daysPerYear": 12}))
        expect_ok("leave.types.list-hr", "GET", "/hr/leave-types",
                  await client.get(f"{BASE}/hr/leave-types", headers=hr_h))
        expect_ok("leave.types.list-user", "GET", "/leaves/types",
                  await client.get(f"{BASE}/leaves/types", headers=emp_h))
        if lt:
            expect_ok("leave.type.update", "PUT", "/hr/leave-types/{id}",
                      await client.put(f"{BASE}/hr/leave-types/{lt['id']}", headers=hr_h,
                                       json={"description": "Updated"}))

        expect_ok("leave.balance", "GET", "/leaves/balance",
                  await client.get(f"{BASE}/leaves/balance", headers=emp_h))

        # Seed a balance directly so the full request → decide flow runs.
        await _db.leave_balances.update_one(
            {"userId": emp_id, "leaveTypeCode": f"EARN{RUN}",
             "year": datetime.now().year},
            {"$set": {
                "allocated": 12.0, "used": 0.0, "pending": 0.0,
                "updatedAt": datetime.now(timezone.utc),
            },
             "$setOnInsert": {"createdAt": datetime.now(timezone.utc)}},
            upsert=True,
        )

        future_date = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
        req_resp = await client.post(
            f"{BASE}/leaves/request", headers=emp_h,
            json={"leaveTypeCode": f"EARN{RUN}",
                  "fromDate": future_date, "toDate": future_date,
                  "reason": "Test", "halfDay": True,
                  "halfDayPart": "FIRST"},
        )
        record("leave.request", "POST", "/leaves/request", req_resp)
        leave_id = (
            req_resp.json().get("id")
            if req_resp.status_code == 200 else None
        )
        expect_ok("leave.mine", "GET", "/leaves/mine",
                  await client.get(f"{BASE}/leaves/mine", headers=emp_h))
        expect_ok("leave.hr.list", "GET", "/hr/leave-requests",
                  await client.get(f"{BASE}/hr/leave-requests", headers=hr_h))
        expect_ok("leave.hr.balance", "GET", "/hr/users/{userId}/leave-balance",
                  await client.get(f"{BASE}/hr/users/{emp_id}/leave-balance", headers=hr_h))
        expect_ok("leave.mgr.list", "GET", "/manager/leave-requests",
                  await client.get(f"{BASE}/manager/leave-requests", headers=mgr_h))

        # Exercise the manager decide path
        if leave_id:
            expect_ok("leave.mgr.decide", "POST", "/manager/leave-requests/{id}/decide",
                      await client.post(
                          f"{BASE}/manager/leave-requests/{leave_id}/decide",
                          headers=mgr_h,
                          json={"action": "APPROVE", "note": "OK"}))

        # ===== 8. Corrections =====
        print("\n=== CORRECTIONS ===")
        # Need a CHECKED_IN attendance row — use a separate date
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        await client.post(f"{BASE}/attendance/checkin", headers=emp_h,
                          json={"date": yesterday, "attendanceType": "WFH"})
        # We don't have the attendance ID handy — fetch today's instead
        at_today = (await client.get(f"{BASE}/attendance/today",
                                     headers=emp_h, params={"date": yesterday})).json()
        att_id = at_today.get("id")
        if att_id:
            corr_resp = await client.post(
                f"{BASE}/attendance/{att_id}/correction-request",
                headers=emp_h,
                json={
                    "requestedCheckOut": f"{yesterday}T18:00:00",
                    "reason": "Forgot to checkout",
                },
            )
            record("corr.create", "POST",
                   "/attendance/{id}/correction-request", corr_resp)
        expect_ok("corr.mine", "GET", "/attendance/correction-requests/mine",
                  await client.get(f"{BASE}/attendance/correction-requests/mine", headers=emp_h))
        expect_ok("corr.hr.list", "GET", "/hr/correction-requests",
                  await client.get(f"{BASE}/hr/correction-requests", headers=hr_h))
        expect_ok("corr.mgr.list", "GET", "/manager/correction-requests",
                  await client.get(f"{BASE}/manager/correction-requests", headers=mgr_h))

        # ===== 9. Assets =====
        print("\n=== ASSETS ===")
        asset = expect_ok("asset.create", "POST", "/hr/assets",
                          await client.post(f"{BASE}/hr/assets", headers=hr_h,
                                            json={"code": f"A-{RUN}",
                                                  "name": "Test laptop",
                                                  "category": "Laptop"}))
        expect_ok("asset.list-hr", "GET", "/hr/assets",
                  await client.get(f"{BASE}/hr/assets", headers=hr_h))
        expect_ok("asset.list-mine", "GET", "/assets/mine",
                  await client.get(f"{BASE}/assets/mine", headers=emp_h))

        # ===== 10. Expenses =====
        print("\n=== EXPENSES (HR direct entry) ===")
        expect_ok("expense.list", "GET", "/hr/expenses",
                  await client.get(f"{BASE}/hr/expenses", headers=hr_h))

        # ===== 11. Payroll =====
        print("\n=== PAYROLL ===")
        expect_ok("payroll.my", "GET", "/payroll/payslips",
                  await client.get(f"{BASE}/payroll/payslips", headers=emp_h))

        # ===== 12. Holidays =====
        print("\n=== HOLIDAYS ===")
        future_holiday = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        hol = expect_ok("holiday.create", "POST", "/hr/holidays",
                        await client.post(f"{BASE}/hr/holidays", headers=hr_h,
                                          json={"date": future_holiday, "name": f"Test-{RUN}"}))
        expect_ok("holiday.list", "GET", "/holidays",
                  await client.get(f"{BASE}/holidays", headers=emp_h))
        expect_ok("holiday.hr.list", "GET", "/hr/holidays",
                  await client.get(f"{BASE}/hr/holidays", headers=hr_h))
        if hol and "id" in hol:
            expect_ok("holiday.delete", "DELETE", "/hr/holidays/{id}",
                      await client.delete(f"{BASE}/hr/holidays/{hol['id']}", headers=hr_h))

        # ===== 13. Onboarding / Exit =====
        print("\n=== ONBOARDING / EXIT ===")
        expect_ok("onb.mine", "GET", "/onboarding/mine",
                  await client.get(f"{BASE}/onboarding/mine", headers=emp_h))
        expect_ok("onb.hr.list", "GET", "/hr/onboardings",
                  await client.get(f"{BASE}/hr/onboardings", headers=hr_h))
        expect_ok("exit.mine", "GET", "/exit/mine",
                  await client.get(f"{BASE}/exit/mine", headers=emp_h))
        expect_ok("exit.hr.list", "GET", "/hr/exits",
                  await client.get(f"{BASE}/hr/exits", headers=hr_h))

        # ===== 14. Notifications =====
        print("\n=== NOTIFICATIONS ===")
        expect_ok("notif.list", "GET", "/notifications",
                  await client.get(f"{BASE}/notifications", headers=emp_h))
        expect_ok("notif.unread", "GET", "/notifications/unread-count",
                  await client.get(f"{BASE}/notifications/unread-count", headers=emp_h))
        expect_ok("notif.readall", "POST", "/notifications/read-all",
                  await client.post(f"{BASE}/notifications/read-all", headers=emp_h))

        # ===== 15. Dashboards =====
        print("\n=== DASHBOARDS ===")
        expect_ok("dash.hr", "GET", "/dashboard/hr",
                  await client.get(f"{BASE}/dashboard/hr", headers=hr_h))
        expect_ok("dash.mgr", "GET", "/dashboard/manager",
                  await client.get(f"{BASE}/dashboard/manager", headers=mgr_h))
        expect_ok("dash.me", "GET", "/dashboard/me",
                  await client.get(f"{BASE}/dashboard/me", headers=emp_h))

        # ===== 16. Projects =====
        print("\n=== PROJECTS ===")
        proj = expect_ok("proj.create", "POST", "/hr/projects",
                         await client.post(f"{BASE}/hr/projects", headers=hr_h,
                                           json={"name": f"Proj-{RUN}", "code": f"P{RUN[:5]}"}))
        proj_id = proj.get("id") if isinstance(proj, dict) else None
        expect_ok("proj.list-user", "GET", "/projects",
                  await client.get(f"{BASE}/projects", headers=emp_h))
        if proj_id:
            expect_ok("proj.get-user", "GET", "/projects/{id}",
                      await client.get(f"{BASE}/projects/{proj_id}", headers=emp_h))
            expect_ok("proj.update", "PUT", "/hr/projects/{id}",
                      await client.put(f"{BASE}/hr/projects/{proj_id}", headers=hr_h,
                                       json={"description": "updated"}))

        # ===== 17. To-Do =====
        print("\n=== TODOS ===")
        todo = expect_ok("todo.create", "POST", "/todos",
                         await client.post(f"{BASE}/todos", headers=emp_h,
                                           json={"title": "Test todo", "priority": "HIGH"}))
        todo_id = todo.get("id") if isinstance(todo, dict) else None
        expect_ok("todo.list", "GET", "/todos",
                  await client.get(f"{BASE}/todos", headers=emp_h))
        if todo_id:
            expect_ok("todo.update", "PUT", "/todos/{id}",
                      await client.put(f"{BASE}/todos/{todo_id}", headers=emp_h,
                                       json={"description": "updated"}))
            expect_ok("todo.complete", "POST", "/todos/{id}/complete",
                      await client.post(f"{BASE}/todos/{todo_id}/complete", headers=emp_h))
            expect_ok("todo.reopen", "POST", "/todos/{id}/reopen",
                      await client.post(f"{BASE}/todos/{todo_id}/reopen", headers=emp_h))
            expect_ok("todo.delete", "DELETE", "/todos/{id}",
                      await client.delete(f"{BASE}/todos/{todo_id}", headers=emp_h))

        # ===== 18. Documents =====
        print("\n=== DOCUMENTS ===")
        doc = expect_ok("doc.create", "POST", "/me/documents",
                        await client.post(f"{BASE}/me/documents", headers=emp_h,
                                          json={"category": "Resume",
                                                "fileName": "r.pdf",
                                                "fileUrl": "/static/uploads/test/r.pdf"}))
        doc_id = doc.get("id") if isinstance(doc, dict) else None
        expect_ok("doc.list", "GET", "/me/documents",
                  await client.get(f"{BASE}/me/documents", headers=emp_h))
        expect_ok("doc.hr.list", "GET", "/hr/users/{id}/documents",
                  await client.get(f"{BASE}/hr/users/{emp_id}/documents", headers=hr_h))
        if doc_id:
            expect_ok("doc.delete", "DELETE", "/me/documents/{id}",
                      await client.delete(f"{BASE}/me/documents/{doc_id}", headers=emp_h))

        # ===== 19. Reimbursements =====
        print("\n=== REIMBURSEMENTS ===")
        reimb = expect_ok("reimb.create", "POST", "/expenses/reimbursements",
                          await client.post(f"{BASE}/expenses/reimbursements", headers=emp_h,
                                            json={"title": "Lunch",
                                                  "category": "Food",
                                                  "expenseDate": today,
                                                  "amount": 1200,
                                                  "paymentMode": "UPI"}))
        reimb_id = reimb.get("id") if isinstance(reimb, dict) else None
        expect_ok("reimb.mine", "GET", "/expenses/reimbursements/mine",
                  await client.get(f"{BASE}/expenses/reimbursements/mine", headers=emp_h))
        expect_ok("reimb.mgr.list", "GET", "/manager/reimbursements",
                  await client.get(f"{BASE}/manager/reimbursements", headers=mgr_h))
        if reimb_id:
            expect_ok("reimb.mgr.decide", "POST", "/manager/reimbursements/{id}/decide",
                      await client.post(f"{BASE}/manager/reimbursements/{reimb_id}/decide",
                                        headers=mgr_h, json={"action": "APPROVE"}))
            expect_ok("reimb.hr.list", "GET", "/hr/reimbursements",
                      await client.get(f"{BASE}/hr/reimbursements", headers=hr_h))
            expect_ok("reimb.hr.decide", "POST", "/hr/reimbursements/{id}/decide",
                      await client.post(f"{BASE}/hr/reimbursements/{reimb_id}/decide",
                                        headers=hr_h, json={"action": "APPROVE"}))

        # ===== 20. Timesheets =====
        print("\n=== TIMESHEETS ===")
        # Find this week's Monday
        d = datetime.now()
        monday = (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
        expect_ok("ts.my", "GET", "/timesheets/my",
                  await client.get(f"{BASE}/timesheets/my", headers=emp_h,
                                   params={"weekStart": monday}))
        ts = expect_ok("ts.submit", "POST", "/timesheets/submit",
                       await client.post(f"{BASE}/timesheets/submit", headers=emp_h,
                                         json={"weekStart": monday}))
        ts_id = ts.get("id") if isinstance(ts, dict) else None
        expect_ok("ts.mgr.list", "GET", "/manager/timesheets",
                  await client.get(f"{BASE}/manager/timesheets", headers=mgr_h))
        if ts_id:
            expect_ok("ts.mgr.decide", "POST", "/manager/timesheets/{id}/decide",
                      await client.post(f"{BASE}/manager/timesheets/{ts_id}/decide",
                                        headers=mgr_h, json={"action": "APPROVE"}))
        expect_ok("ts.hr.list", "GET", "/hr/timesheets",
                  await client.get(f"{BASE}/hr/timesheets", headers=hr_h))

        # ===== 21. Reports =====
        print("\n=== REPORTS ===")
        expect_ok("rep.att", "GET", "/hr/reports/attendance",
                  await client.get(f"{BASE}/hr/reports/attendance", headers=hr_h))
        expect_ok("rep.leave", "GET", "/hr/reports/leave",
                  await client.get(f"{BASE}/hr/reports/leave", headers=hr_h))
        expect_ok("rep.payroll", "GET", "/hr/reports/payroll",
                  await client.get(f"{BASE}/hr/reports/payroll", headers=hr_h,
                                   params={"year": d.year, "month": d.month}))
        expect_ok("rep.dept", "GET", "/hr/reports/departments",
                  await client.get(f"{BASE}/hr/reports/departments", headers=hr_h))
        expect_ok("rep.attrition", "GET", "/hr/reports/attrition",
                  await client.get(f"{BASE}/hr/reports/attrition", headers=hr_h))
        expect_ok("rep.mgr.prod", "GET", "/manager/reports/team-productivity",
                  await client.get(f"{BASE}/manager/reports/team-productivity", headers=mgr_h))

        # ===== 22. Exports =====
        print("\n=== EXPORTS ===")
        expect_ok("exp.users", "GET", "/hr/export/users.xlsx",
                  await client.get(f"{BASE}/hr/export/users.xlsx", headers=hr_h))
        expect_ok("exp.att", "GET", "/hr/export/attendance.xlsx",
                  await client.get(f"{BASE}/hr/export/attendance.xlsx", headers=hr_h))
        expect_ok("exp.leave", "GET", "/hr/export/leave-requests.xlsx",
                  await client.get(f"{BASE}/hr/export/leave-requests.xlsx", headers=hr_h))
        expect_ok("exp.payroll", "GET", "/hr/export/payroll/{y}/{m}.xlsx",
                  await client.get(f"{BASE}/hr/export/payroll/{d.year}/{d.month}.xlsx", headers=hr_h))

        # ===== 23. Audit logs =====
        print("\n=== AUDIT ===")
        expect_ok("audit.list", "GET", "/hr/audit-logs",
                  await client.get(f"{BASE}/hr/audit-logs", headers=hr_h))

        # ===== 24. Recruitment =====
        print("\n=== RECRUITMENT ===")
        opening = expect_ok("rec.open.create", "POST", "/hr/job-openings",
                            await client.post(f"{BASE}/hr/job-openings", headers=hr_h,
                                              json={"title": "Test SDE",
                                                    "location": "Remote",
                                                    "openings": 1}))
        opening_id = opening.get("id") if isinstance(opening, dict) else None
        expect_ok("rec.open.list", "GET", "/hr/job-openings",
                  await client.get(f"{BASE}/hr/job-openings", headers=hr_h))
        if opening_id:
            expect_ok("rec.open.get", "GET", "/hr/job-openings/{id}",
                      await client.get(f"{BASE}/hr/job-openings/{opening_id}", headers=hr_h))
            expect_ok("rec.open.update", "PUT", "/hr/job-openings/{id}",
                      await client.put(f"{BASE}/hr/job-openings/{opening_id}", headers=hr_h,
                                       json={"description": "Updated"}))

        cand = expect_ok("rec.cand.create", "POST", "/hr/candidates",
                         await client.post(f"{BASE}/hr/candidates", headers=hr_h,
                                           json={"name": "Test Cand",
                                                 "email": f"cand-{RUN}@apitest.example.com",
                                                 "jobOpeningId": opening_id}))
        cand_id = cand.get("id") if isinstance(cand, dict) else None
        expect_ok("rec.cand.list", "GET", "/hr/candidates",
                  await client.get(f"{BASE}/hr/candidates", headers=hr_h))
        if cand_id:
            expect_ok("rec.cand.get", "GET", "/hr/candidates/{id}",
                      await client.get(f"{BASE}/hr/candidates/{cand_id}", headers=hr_h))
            expect_ok("rec.cand.update", "PUT", "/hr/candidates/{id}",
                      await client.put(f"{BASE}/hr/candidates/{cand_id}", headers=hr_h,
                                       json={"phone": "+91-9999999999"}))
            expect_ok("rec.cand.move", "POST", "/hr/candidates/{id}/move",
                      await client.post(f"{BASE}/hr/candidates/{cand_id}/move", headers=hr_h,
                                        json={"stage": "SCREENING", "note": "Pass"}))

        if cand_id:
            iv = expect_ok("rec.iv.create", "POST", "/hr/interviews",
                           await client.post(f"{BASE}/hr/interviews", headers=hr_h,
                                             json={"candidateId": cand_id,
                                                   "scheduledAt": f"{future_date}T15:00:00",
                                                   "interviewerIds": [mgr_id]}))
            iv_id = iv.get("id") if isinstance(iv, dict) else None
            expect_ok("rec.iv.list", "GET", "/hr/interviews",
                      await client.get(f"{BASE}/hr/interviews", headers=hr_h))
            expect_ok("rec.iv.mine", "GET", "/interviews/mine",
                      await client.get(f"{BASE}/interviews/mine", headers=mgr_h))
            if iv_id:
                expect_ok("rec.iv.fb", "POST", "/hr/interviews/{id}/feedback",
                          await client.post(f"{BASE}/hr/interviews/{iv_id}/feedback", headers=mgr_h,
                                            json={"rating": 4, "recommendation": "HIRE"}))

            offer = expect_ok("rec.off.create", "POST", "/hr/offers",
                              await client.post(f"{BASE}/hr/offers", headers=hr_h,
                                                json={"candidateId": cand_id,
                                                      "position": "SDE",
                                                      "annualCtc": 1800000,
                                                      "joiningDate": future_date}))
            off_id = offer.get("id") if isinstance(offer, dict) else None
            expect_ok("rec.off.list", "GET", "/hr/offers",
                      await client.get(f"{BASE}/hr/offers", headers=hr_h))

        # ===== 25. Performance =====
        print("\n=== PERFORMANCE ===")
        goal = expect_ok("perf.goal.create", "POST", "/manager/goals",
                         await client.post(f"{BASE}/manager/goals", headers=mgr_h,
                                           json={"userId": emp_id,
                                                 "title": "Ship feature X"}))
        goal_id = goal.get("id") if isinstance(goal, dict) else None
        expect_ok("perf.goal.mine", "GET", "/goals/mine",
                  await client.get(f"{BASE}/goals/mine", headers=emp_h))
        expect_ok("perf.goal.mgr.list", "GET", "/manager/goals",
                  await client.get(f"{BASE}/manager/goals", headers=mgr_h))
        expect_ok("perf.goal.hr.list", "GET", "/hr/goals",
                  await client.get(f"{BASE}/hr/goals", headers=hr_h))
        if goal_id:
            expect_ok("perf.goal.progress", "POST", "/goals/{id}/progress",
                      await client.post(f"{BASE}/goals/{goal_id}/progress", headers=emp_h,
                                        json={"achievedValue": 50, "note": "Halfway"}))
            expect_ok("perf.goal.update", "PUT", "/manager/goals/{id}",
                      await client.put(f"{BASE}/manager/goals/{goal_id}", headers=mgr_h,
                                       json={"status": "COMPLETED"}))

        review = expect_ok("perf.rev.create", "POST", "/manager/reviews",
                           await client.post(f"{BASE}/manager/reviews", headers=mgr_h,
                                             json={"employeeId": emp_id,
                                                   "type": "QUARTERLY",
                                                   "periodStart": "2026-01-01",
                                                   "periodEnd": "2026-03-31"}))
        rev_id = review.get("id") if isinstance(review, dict) else None
        expect_ok("perf.rev.mine", "GET", "/reviews/mine",
                  await client.get(f"{BASE}/reviews/mine", headers=emp_h))
        expect_ok("perf.rev.hr.list", "GET", "/hr/reviews",
                  await client.get(f"{BASE}/hr/reviews", headers=hr_h))
        if rev_id:
            expect_ok("perf.rev.self", "POST", "/reviews/{id}/self-eval",
                      await client.post(f"{BASE}/reviews/{rev_id}/self-eval", headers=emp_h,
                                        json={"accomplishments": "X"}))
            expect_ok("perf.rev.mgr-eval", "POST", "/manager/reviews/{id}/manager-eval",
                      await client.post(f"{BASE}/manager/reviews/{rev_id}/manager-eval", headers=mgr_h,
                                        json={"strengths": "Y", "overallRating": 4}))
            expect_ok("perf.rev.submit", "POST", "/manager/reviews/{id}/submit",
                      await client.post(f"{BASE}/manager/reviews/{rev_id}/submit", headers=mgr_h))
            expect_ok("perf.rev.ack", "POST", "/reviews/{id}/acknowledge",
                      await client.post(f"{BASE}/reviews/{rev_id}/acknowledge", headers=emp_h,
                                        json={"note": "ok"}))

        fb = expect_ok("perf.fb.create", "POST", "/feedback",
                       await client.post(f"{BASE}/feedback", headers=mgr_h,
                                         json={"toUserId": emp_id, "type": "POSITIVE",
                                               "text": "Great work"}))
        expect_ok("perf.fb.about-me", "GET", "/feedback/about-me",
                  await client.get(f"{BASE}/feedback/about-me", headers=emp_h))
        expect_ok("perf.fb.sent", "GET", "/feedback/sent",
                  await client.get(f"{BASE}/feedback/sent", headers=mgr_h))
        expect_ok("perf.fb.hr.list", "GET", "/hr/feedback",
                  await client.get(f"{BASE}/hr/feedback", headers=hr_h))

        # ===== 26. Public endpoints =====
        print("\n=== PUBLIC (no auth) ===")
        expect_ok("pub.careers.list", "GET", "/careers/openings",
                  await client.get(f"{BASE}/careers/openings"))
        if opening_id:
            # First, mark the opening as Open (default) and try public detail
            expect_ok("pub.careers.get", "GET", "/careers/openings/{id}",
                      await client.get(f"{BASE}/careers/openings/{opening_id}"))
            expect_ok("pub.careers.apply", "POST", "/careers/openings/{id}/apply",
                      await client.post(f"{BASE}/careers/openings/{opening_id}/apply",
                                        json={"name": "Public Applicant",
                                              "email": f"pub-{RUN}@apitest.example.com"}))

        # HR email test
        print("\n=== HR EMAIL TEST ===")
        # 503 expected when SMTP disabled; otherwise 200
        et = await client.post(f"{BASE}/hr/email/test", headers=hr_h)
        record("hr.email.test", "POST", "/hr/email/test", et)

        # ===== 27. Forgot password (no auth) =====
        print("\n=== FORGOT PASSWORD ===")
        expect_ok("forgot.pw", "POST", "/auth/forgot-password",
                  await client.post(f"{BASE}/auth/forgot-password",
                                    json={"email": hr_email}))

        # ===== Cleanup: delete test entities =====
        print("\n=== CLEANUP ===")
        if task_id:
            await client.delete(f"{BASE}/tl/tasks/{task_id}", headers=mgr_h)
        if proj_id:
            await client.delete(f"{BASE}/hr/projects/{proj_id}", headers=hr_h)
        if team_id:
            await client.delete(f"{BASE}/hr/teams/{team_id}", headers=hr_h)

    # ===== Summary =====
    failures = [r for r in results if r[3] >= 400]
    print("\n" + "=" * 60)
    print(f"TOTAL CALLS:  {len(results)}")
    print(f"SUCCESSES:    {len(results) - len(failures)}")
    print(f"FAILURES:     {len(failures)}")
    print("=" * 60)

    if failures:
        print("\nFAILURE DETAILS:")
        for label, method, path, code, body in failures:
            print(f"\n[{label}] {method} {path} -> {code}")
            print(f"  body: {json.dumps(body)[:300]}")

    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
