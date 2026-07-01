"""One-off backfill: create LEAVE attendance records for leaves that were
APPROVED before the auto-apply-on-approval logic shipped.

Why: approving a leave now writes an `attendanceType: "LEAVE"` row per working
day so it shows on the employee's calendar. Leaves approved before that change
have no such rows, so they don't appear. This script walks every APPROVED leave
request and inserts the missing rows — idempotent (never clobbers an existing
attendance record for a date, including a real check-in).

Run from backend/:  python migrate_backfill_leave_attendance.py
"""

import asyncio
from datetime import datetime, timezone

from database import db
# Reuse the SAME helper the approval path uses so weekends/holidays are
# excluded identically — the backfill and live behaviour can't drift.
from routes.leave import _working_dates_in_range


async def main() -> None:
    now = datetime.now(timezone.utc)
    scanned = 0
    created = 0
    skipped_existing = 0
    bad = 0

    async for req in db.leave_requests.find({"status": "APPROVED"}):
        scanned += 1
        oid = req.get("_id")
        user_id = req.get("userId")
        from_date = req.get("fromDate")
        to_date = req.get("toDate")
        if not (user_id and from_date and to_date):
            bad += 1
            continue

        # Same human-readable note the approval handler stamps.
        leave_note = f"{req.get('leaveTypeCode', '')} leave".strip()
        if req.get("halfDay"):
            part = req.get("halfDayPart")
            leave_note += f" (half day{f' — {part}' if part else ''})"

        try:
            working_days = await _working_dates_in_range(from_date, to_date)
        except Exception as e:  # noqa: BLE001 — log + continue on bad ranges
            print(f"  ! skip leave {oid}: bad range {from_date}..{to_date} ({e})")
            bad += 1
            continue

        for ymd in working_days:
            already = await db.attendance.find_one(
                {"userId": user_id, "date": ymd}
            )
            if already:
                skipped_existing += 1
                continue
            await db.attendance.insert_one({
                "userId": user_id,
                "date": ymd,
                "attendanceType": "LEAVE",
                "status": "ON_LEAVE",
                "checkIn": None,
                "checkOut": None,
                "workNotes": leave_note,
                "autoAppliedFromLeave": True,
                "leaveRequestId": str(oid),
                "createdAt": now,
                "updatedAt": now,
            })
            created += 1

    print("\nBackfill complete:")
    print(f"  approved leaves scanned : {scanned}")
    print(f"  attendance rows created : {created}")
    print(f"  days already had a row  : {skipped_existing}")
    print(f"  leaves skipped (bad)    : {bad}")


if __name__ == "__main__":
    asyncio.run(main())
