"""E2E test for the new /attendance/correction-requests/for-date endpoint.

Logs in (mints tokens) as gudivadalovekik + an HR user, submits a
correction for a previous day with NO record, verifies the placeholder +
correction were created, checks the duplicate guard, then HR-approves and
confirms the attendance row gets stamped. Cleans up all created docs.
"""
import sys, asyncio
from datetime import datetime, timedelta
sys.path.insert(0, "backend")
import config  # noqa: F401
import httpx
from database import db
from utils.auth import create_token
from bson import ObjectId

BASE = "http://127.0.0.1:8000"
UID = "6a11ab0ea8562d8a05472896"  # gudivadalovekik


async def pick_missing_weekday():
    """A past weekday with no attendance row for the user."""
    for n in range(2, 40):
        d = (datetime.now() - timedelta(days=n)).date()
        if d.weekday() >= 5:
            continue
        ds = d.strftime("%Y-%m-%d")
        if not await db.attendance.find_one({"userId": UID, "date": ds}):
            return ds
    return None


async def main():
    emp = create_token({"sub": UID})
    hr_doc = await db.users.find_one({"role": {"$in": ["HR", "CEO"]}})
    hr = create_token({"sub": str(hr_doc["_id"])})
    eh = {"Authorization": f"Bearer {emp}"}
    hh = {"Authorization": f"Bearer {hr}"}

    date = await pick_missing_weekday()
    print(f"target missed day (no record): {date}")
    assert date

    created_att_id = None
    created_corr_id = None
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            ci = f"{date}T09:30:00"
            co = f"{date}T18:15:00"
            body = {
                "date": date, "requestedCheckIn": ci, "requestedCheckOut": co,
                "requestedAttendanceType": "OFFICE",
                "requestedWorkNotes": "Forgot to check in",
                "reason": "Was at office, forgot to mark attendance",
            }
            # 1. Submit for-date correction
            r = await c.post(f"{BASE}/attendance/correction-requests/for-date",
                             headers=eh, json=body)
            print(f"\n1) POST for-date -> HTTP {r.status_code}")
            assert r.status_code == 200, r.text
            corr = r.json()
            created_corr_id = corr["id"]
            created_att_id = corr["attendanceId"]
            print(f"   correctionId={created_corr_id} attendanceId={created_att_id} "
                  f"status={corr['status']}")

            # 2. Placeholder attendance row exists + is ABSENT
            att = await db.attendance.find_one({"_id": ObjectId(created_att_id)})
            print(f"2) placeholder row: date={att['date']} status={att['status']} "
                  f"placeholder={att.get('placeholderForCorrection')}")
            assert att["status"] == "ABSENT"

            # 3. Duplicate guard
            r2 = await c.post(f"{BASE}/attendance/correction-requests/for-date",
                              headers=eh, json=body)
            print(f"3) duplicate POST -> HTTP {r2.status_code} "
                  f"(expect 400): {r2.json().get('detail')}")
            assert r2.status_code == 400

            # 4. Bad date
            r3 = await c.post(f"{BASE}/attendance/correction-requests/for-date",
                              headers=eh, json={**body, "date": "13-06-2026"})
            print(f"4) bad-date POST -> HTTP {r3.status_code} (expect 400)")
            assert r3.status_code == 400

            # 5. Shows up in user's correction list
            r4 = await c.get(f"{BASE}/attendance/correction-requests/mine",
                             headers=eh)
            mine_ids = [x["id"] for x in r4.json()]
            print(f"5) appears in /mine: {created_corr_id in mine_ids}")

            # 6. HR approves -> row stamped PRESENT with hours
            r5 = await c.post(
                f"{BASE}/hr/correction-requests/{created_corr_id}/decide",
                headers=hh, json={"action": "APPROVE", "note": "ok"})
            print(f"6) HR approve -> HTTP {r5.status_code}: {r5.json().get('message')}")
            assert r5.status_code == 200
            att2 = await db.attendance.find_one({"_id": ObjectId(created_att_id)})
            print(f"   stamped row: status={att2['status']} "
                  f"checkIn={att2.get('checkIn')} checkOut={att2.get('checkOut')} "
                  f"hoursWorked={att2.get('hoursWorked')}")
            assert att2["status"] in ("PRESENT", "LATE", "HALF_DAY")
            assert att2.get("hoursWorked", 0) > 0
            print("\nALL CHECKS PASSED")
    finally:
        # cleanup
        if created_corr_id:
            await db.correction_requests.delete_one({"_id": ObjectId(created_corr_id)})
        if created_att_id:
            await db.attendance.delete_one({"_id": ObjectId(created_att_id)})
        print("cleaned up created test docs")


asyncio.run(main())
