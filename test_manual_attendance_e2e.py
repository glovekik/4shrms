"""End-to-end smoke for the manual attendance request workflow.

Runs against a live backend at http://localhost:8000. Uses the
bootstrap_test_hr.py credentials for the HR actor and provisions a
disposable employee for the request.
"""

import asyncio
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

sys.path.insert(0, "backend")
from database import db  # noqa: E402

BASE = "http://localhost:8000"
HR_EMAIL = "test-hr@apitest.example.com"
HR_PWD = "test-hr-pass-123"

EMP_PWD = "emp-test-pass-123"


def _req(method, path, token=None, body=None):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read().decode()
            return resp.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def login(email, pwd):
    code, body = _req("POST", "/auth/login", body={"email": email, "password": pwd})
    assert code == 200, f"login {email} failed: {code} {body}"
    return body["access_token"]


async def provision_employee():
    """Create a fresh employee directly in Mongo so the test owns it."""
    from passlib.context import CryptContext
    pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    email = f"emp-mae2e-{uuid4().hex[:8]}@apitest.example.com"
    now = datetime.now(timezone.utc)
    doc = {
        "email": email,
        "name": "MA E2E Employee",
        "password": pwd_ctx.hash(EMP_PWD),
        "role": "USER",
        "status": "Active",
        "tag": "Employee",
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.users.insert_one(doc)
    return email, str(result.inserted_id)


async def cleanup(emp_email, emp_id):
    await db.users.delete_one({"email": emp_email})
    await db.manual_attendance_requests.delete_many({"userId": emp_id})
    await db.attendance.delete_many({"userId": emp_id})
    await db.notifications.delete_many({"userId": emp_id})


def line(label):
    print(f"\n=== {label} ===")


async def main():
    # 0. Make sure HR exists
    line("setup")
    os.system(f'"{sys.executable}" bootstrap_test_hr.py')
    emp_email, emp_id = await provision_employee()
    print(f"employee: {emp_email}  id={emp_id}")

    try:
        hr_token = login(HR_EMAIL, HR_PWD)
        emp_token = login(emp_email, EMP_PWD)
        print("logged in HR + employee")

        target_date = (date.today() + timedelta(days=1)).isoformat()
        check_in = f"{target_date}T09:30:00Z"
        check_out = f"{target_date}T18:00:00Z"

        # 1. Employee submits a request
        line("1. employee submits manual-request")
        code, body = _req(
            "POST",
            "/attendance/manual-request",
            token=emp_token,
            body={
                "date": target_date,
                "checkIn": check_in,
                "checkOut": check_out,
                "reason": "Was on client site, forgot to clock in",
            },
        )
        print(code, json.dumps(body, indent=2, default=str))
        assert code == 200, f"submit failed: {code} {body}"
        request_id = body["id"]
        assert body["status"] == "PENDING"
        assert body["date"] == target_date

        # 2. Duplicate-pending guard
        line("2. duplicate submission for same date should 400")
        code, body = _req(
            "POST",
            "/attendance/manual-request",
            token=emp_token,
            body={
                "date": target_date,
                "checkIn": check_in,
                "reason": "dup",
            },
        )
        print(code, body)
        assert code == 400 and "pending request" in str(body).lower()

        # 3. Employee /mine lists it
        line("3. GET /attendance/manual-request/mine")
        code, body = _req(
            "GET", "/attendance/manual-request/mine", token=emp_token
        )
        print(code, json.dumps(body, indent=2, default=str))
        assert code == 200 and any(r["id"] == request_id for r in body)

        # 4. HR list shows it under PENDING
        line("4. HR /hr/manual-requests?status=PENDING")
        code, body = _req(
            "GET",
            "/hr/manual-requests?status=PENDING",
            token=hr_token,
        )
        print(code, f"({len(body)} pending; ours present:", any(r["id"] == request_id for r in body), ")")
        assert code == 200 and any(r["id"] == request_id for r in body)

        # 5. HR approves
        line("5. HR approves")
        code, body = _req(
            "POST",
            f"/hr/manual-requests/{request_id}/decide",
            token=hr_token,
            body={"action": "APPROVE", "note": "Verified with TL"},
        )
        print(code, body)
        assert code == 200 and "approved" in body["message"].lower()

        # 6. Attendance row should now exist
        line("6. attendance row inserted")
        row = await db.attendance.find_one({
            "userId": emp_id, "date": target_date,
        })
        print(json.dumps(
            {k: str(v) for k, v in row.items() if k != "_id"} if row else None,
            indent=2,
        ))
        assert row is not None, "no attendance row inserted"
        assert row.get("autoApprovedFromRequest") is True
        assert row.get("manualRequestId") == request_id
        assert row.get("attendanceType") == "MANUAL"
        assert row.get("status") == "COMPLETED"  # had checkOut

        # 7. Request is now APPROVED
        line("7. request status is APPROVED")
        req = await db.manual_attendance_requests.find_one({
            "_id": __import__("bson").ObjectId(request_id)
        })
        print(req.get("status"), "by role:", req.get("decidedByRole"))
        assert req["status"] == "APPROVED"

        # 8. Re-decide should fail (already decided)
        line("8. cannot re-decide an approved request")
        code, body = _req(
            "POST",
            f"/hr/manual-requests/{request_id}/decide",
            token=hr_token,
            body={"action": "APPROVE"},
        )
        print(code, body)
        assert code == 400 and "already" in str(body).lower()

        # 9. Same-date submission should now fail (attendance exists)
        line("9. submission on a date that has attendance should 400")
        code, body = _req(
            "POST",
            "/attendance/manual-request",
            token=emp_token,
            body={"date": target_date, "checkIn": check_in, "reason": "x"},
        )
        print(code, body)
        assert code == 400 and "attendance already exists" in str(body).lower()

        # 10. REJECT path on a new request
        line("10. REJECT path")
        other_date = (date.today() + timedelta(days=2)).isoformat()
        code, body = _req(
            "POST",
            "/attendance/manual-request",
            token=emp_token,
            body={
                "date": other_date,
                "checkIn": f"{other_date}T10:00:00Z",
                "reason": "Working from home, app crashed",
            },
        )
        assert code == 200
        rej_id = body["id"]
        code, body = _req(
            "POST",
            f"/hr/manual-requests/{rej_id}/decide",
            token=hr_token,
            body={"action": "REJECT", "note": "No prior approval"},
        )
        print(code, body)
        assert code == 200 and "rejected" in body["message"].lower()
        # No attendance row should have been inserted
        rejected_row = await db.attendance.find_one({
            "userId": emp_id, "date": other_date,
        })
        assert rejected_row is None, "reject should NOT insert attendance"

        print("\nAll assertions passed.")
    finally:
        await cleanup(emp_email, emp_id)
        print("(cleanup done)")


if __name__ == "__main__":
    asyncio.run(main())
