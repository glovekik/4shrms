"""End-to-end smoke for the leave-balance workflow.

Covers:
  - HR creates a leave type → active employees get a balance row seeded
  - GET /leaves/balance lazy-seeds + returns the right allocation
  - Submit + approve a leave request → pending/used roll correctly
  - Insufficient-balance error includes the type code
  - HR raises daysPerYear → allocated tops up via $max
  - PUT /hr/users/{id}/leave-balance — manual override
  - Cancel releases pending
  - Monthly accrual cron stamps accrualHistory and serializer surfaces
    accruedThisMonth / accruedYTD
"""

import asyncio
import json
import sys
import urllib.request
import urllib.error
import os
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

sys.path.insert(0, "backend")
from database import db  # noqa: E402
from bson import ObjectId  # noqa: E402

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
    from passlib.context import CryptContext
    pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    email = f"emp-lvbal-{uuid4().hex[:8]}@apitest.example.com"
    now = datetime.now(timezone.utc)
    doc = {
        "email": email,
        "name": "LV E2E Employee",
        "password": pwd_ctx.hash(EMP_PWD),
        "role": "USER",
        "status": "Active",
        "tag": "Employee",
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.users.insert_one(doc)
    return email, str(result.inserted_id)


async def cleanup(emp_email, emp_id, leave_codes):
    await db.users.delete_one({"email": emp_email})
    await db.leave_balances.delete_many({"userId": emp_id})
    await db.leave_requests.delete_many({"userId": emp_id})
    if leave_codes:
        await db.leave_types.delete_many({"code": {"$in": leave_codes}})
        await db.leave_balances.delete_many(
            {"leaveTypeCode": {"$in": leave_codes}}
        )


def find(items, code):
    return next((x for x in items if x.get("leaveTypeCode") == code), None)


def line(label):
    print(f"\n=== {label} ===")


async def main():
    line("setup")
    os.system(f'"{sys.executable}" bootstrap_test_hr.py')
    emp_email, emp_id = await provision_employee()
    print(f"employee: {emp_email}  id={emp_id}")

    upfront_code = f"E2EUP-{uuid4().hex[:6].upper()}"
    accrual_code = f"E2EAC-{uuid4().hex[:6].upper()}"
    leave_codes = [upfront_code, accrual_code]

    try:
        hr_token = login(HR_EMAIL, HR_PWD)
        emp_token = login(emp_email, EMP_PWD)

        # 1. HR creates a full-upfront type — seeds active users (incl. us)
        line(f"1. HR creates leave type {upfront_code} (daysPerYear=12)")
        code, body = _req(
            "POST", "/hr/leave-types", token=hr_token,
            body={
                "code": upfront_code,
                "name": "E2E Upfront Leave",
                "daysPerMonth": 0,
                "daysPerYear": 12,
                "allowHalfDay": True,
                "requiresAttachment": False,
                "isActive": True,
            },
        )
        print(code, json.dumps(body, indent=2))
        assert code == 200 and body["code"] == upfront_code

        seeded = await db.leave_balances.find_one({
            "userId": emp_id, "leaveTypeCode": upfront_code,
        })
        assert seeded and seeded["allocated"] == 12, seeded
        print(f"  [ok]seed-on-create wrote allocated={seeded['allocated']}")

        # 2. Employee GET /leaves/balance — should include the new type
        line("2. employee GET /leaves/balance")
        code, body = _req("GET", "/leaves/balance", token=emp_token)
        assert code == 200
        row = find(body, upfront_code)
        print(f"  {upfront_code}: allocated={row['allocated']}  "
              f"used={row['used']}  pending={row['pending']}  "
              f"remaining={row['remaining']}")
        assert row["allocated"] == 12 and row["remaining"] == 12

        # 3. Submit a 3-day leave request
        line("3. employee POST /leaves/request (3 days)")
        f_date = (date.today() + timedelta(days=10)).isoformat()
        t_date = (date.today() + timedelta(days=12)).isoformat()
        code, body = _req(
            "POST", "/leaves/request", token=emp_token,
            body={
                "leaveTypeCode": upfront_code,
                "fromDate": f_date,
                "toDate": t_date,
                "reason": "Personal time",
                "halfDay": False,
            },
        )
        print(code, "totalDays=", body.get("totalDays"), "status=", body.get("status"))
        assert code == 200 and body["totalDays"] == 3 and body["status"] == "PENDING"
        req_id = body["id"]

        # 4. Balance reflects the pending hold
        line("4. balance shows pending=3, remaining=9")
        code, body = _req("GET", "/leaves/balance", token=emp_token)
        row = find(body, upfront_code)
        print(f"  allocated={row['allocated']}  used={row['used']}  "
              f"pending={row['pending']}  remaining={row['remaining']}")
        assert row["pending"] == 3 and row["remaining"] == 9

        # 5. Insufficient-balance error includes the type code
        line("5. requesting 10 days now must 400 with type-code hint")
        f2 = (date.today() + timedelta(days=30)).isoformat()
        t2 = (date.today() + timedelta(days=39)).isoformat()
        code, body = _req(
            "POST", "/leaves/request", token=emp_token,
            body={
                "leaveTypeCode": upfront_code,
                "fromDate": f2, "toDate": t2,
                "reason": "Too greedy", "halfDay": False,
            },
        )
        print(code, body)
        assert code == 400
        detail = body["detail"] if isinstance(body, dict) else str(body)
        assert upfront_code in detail and "Ask HR to allocate" in detail

        # 6. HR approves — pending→used
        line("6. HR approves the 3-day request")
        code, body = _req(
            "POST", f"/hr/leave-requests/{req_id}/decide", token=hr_token,
            body={"action": "APPROVE", "note": "OK"},
        )
        print(code, body)
        assert code == 200
        code, body = _req("GET", "/leaves/balance", token=emp_token)
        row = find(body, upfront_code)
        print(f"  used={row['used']} pending={row['pending']} remaining={row['remaining']}")
        assert row["used"] == 3 and row["pending"] == 0 and row["remaining"] == 9

        # 7. Raise daysPerYear from 12 → 18 — $max bumps existing rows
        line("7. HR PUT /hr/leave-types/{id} daysPerYear 12 -> 18")
        lt = await db.leave_types.find_one({"code": upfront_code})
        code, body = _req(
            "PUT", f"/hr/leave-types/{lt['_id']}", token=hr_token,
            body={"daysPerYear": 18},
        )
        print(code, body)
        code, body = _req("GET", "/leaves/balance", token=emp_token)
        row = find(body, upfront_code)
        print(f"  allocated={row['allocated']} remaining={row['remaining']}")
        assert row["allocated"] == 18 and row["remaining"] == 15  # 18-3-0

        # 8. PUT /hr/users/{id}/leave-balance — manual override to 20
        line("8. HR PUT /hr/users/{id}/leave-balance (override allocated=20)")
        code, body = _req(
            "PUT", f"/hr/users/{emp_id}/leave-balance", token=hr_token,
            body={
                "leaveTypeCode": upfront_code,
                "allocated": 20,
                "note": "Adjustment after audit",
            },
        )
        print(code, json.dumps(body, indent=2))
        assert code == 200 and body["allocated"] == 20

        # 9. Cancel logic — submit another pending request, then cancel
        line("9. submit another pending request, then cancel — pending released")
        f3 = (date.today() + timedelta(days=50)).isoformat()
        t3 = (date.today() + timedelta(days=51)).isoformat()
        code, body = _req(
            "POST", "/leaves/request", token=emp_token,
            body={
                "leaveTypeCode": upfront_code,
                "fromDate": f3, "toDate": t3,
                "reason": "tentative",
                "halfDay": False,
            },
        )
        assert code == 200, body
        cancel_id = body["id"]
        code, body = _req("GET", "/leaves/balance", token=emp_token)
        row = find(body, upfront_code)
        assert row["pending"] == 2, row
        print(f"  before cancel: pending={row['pending']}")

        code, body = _req(
            "POST", f"/leaves/{cancel_id}/cancel", token=emp_token,
        )
        print("  cancel:", code, body)
        code, body = _req("GET", "/leaves/balance", token=emp_token)
        row = find(body, upfront_code)
        print(f"  after cancel:  pending={row['pending']} remaining={row['remaining']}")
        assert row["pending"] == 0

        # 10. Monthly accrual: create a per-month type, simulate a missed
        # month, run the cron, verify history + accruedThisMonth.
        line(f"10. monthly accrual: create {accrual_code} (perMonth=1.5, perYear=18)")
        # Insert directly with no isActive seed so we control state.
        await db.leave_types.insert_one({
            "code": accrual_code,
            "name": "E2E Monthly Accrual",
            "daysPerMonth": 1.5,
            "daysPerYear": 18,
            "allowHalfDay": True,
            "requiresAttachment": False,
            "isActive": True,
            "createdAt": datetime.now(timezone.utc),
        })
        # Seed a balance row stamped 2 months ago so the cron has to backfill.
        current_month = datetime.now().month
        await db.leave_balances.insert_one({
            "userId": emp_id,
            "leaveTypeCode": accrual_code,
            "year": datetime.now().year,
            "allocated": 1.5,  # one prior accrual
            "used": 0.0,
            "pending": 0.0,
            "lastAccruedMonth": max(0, current_month - 2),
            "accrualHistory": [],
            "createdAt": datetime.now(timezone.utc),
            "updatedAt": datetime.now(timezone.utc),
        })

        from utils.scheduler import monthly_leave_accrual
        await monthly_leave_accrual()

        row = await db.leave_balances.find_one({
            "userId": emp_id, "leaveTypeCode": accrual_code,
        })
        print(f"  after cron: allocated={row['allocated']} "
              f"lastAccruedMonth={row['lastAccruedMonth']} "
              f"history={row.get('accrualHistory')}")
        # 1.5 prior + 2 months * 1.5 = 4.5 total
        assert row["allocated"] == 4.5
        assert row["lastAccruedMonth"] == current_month
        assert len(row.get("accrualHistory") or []) == 2

        # Serializer surfaces accruedThisMonth / accruedYTD
        code, body = _req("GET", "/leaves/balance", token=emp_token)
        srow = find(body, accrual_code)
        print(f"  serializer: accruedThisMonth={srow['accruedThisMonth']} "
              f"accruedYTD={srow['accruedYTD']} "
              f"history={srow['monthlyAccrualHistory']}")
        assert srow["accruedThisMonth"] == 1.5
        assert srow["accruedYTD"] == 3.0  # the 2 new entries; the 1.5 prior had no history row

        # 11. Re-running the cron in the same month is a no-op
        line("11. re-running accrual same month is a no-op")
        await monthly_leave_accrual()
        row2 = await db.leave_balances.find_one({
            "userId": emp_id, "leaveTypeCode": accrual_code,
        })
        print(f"  allocated={row2['allocated']} history len={len(row2.get('accrualHistory') or [])}")
        assert row2["allocated"] == 4.5
        assert len(row2.get("accrualHistory") or []) == 2

        # 12. Deactivating a type hides the row from /balance (response
        # filters out inactive types so the UI doesn't render orphans).
        # The underlying row stays in the DB for audit.
        line("12. deactivate upfront type — row hidden from /balance, kept in DB")
        code, body = _req(
            "PUT", f"/hr/leave-types/{lt['_id']}", token=hr_token,
            body={"isActive": False},
        )
        print("  deactivate:", code, body)
        code, body = _req("GET", "/leaves/balance", token=emp_token)
        row = find(body, upfront_code)
        db_row = await db.leave_balances.find_one({
            "userId": emp_id, "leaveTypeCode": upfront_code,
        })
        print(
            f"  in /balance response? {row is not None}  "
            f"in DB? {db_row is not None}  "
            f"DB allocated={(db_row or {}).get('allocated')}"
        )
        assert row is None, "deactivated type should not appear in /balance"
        assert db_row is not None and db_row["allocated"] == 20, \
            "row must remain in DB for audit"

        print("\nAll leave-balance assertions passed.")
    finally:
        await cleanup(emp_email, emp_id, leave_codes)
        print("(cleanup done)")


if __name__ == "__main__":
    asyncio.run(main())
