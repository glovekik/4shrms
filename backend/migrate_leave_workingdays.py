"""One-time migration: recompute PENDING leave requests' `totalDays` under
the new working-days rule (Sundays + declared holidays excluded) and
adjust the reserved `pending` balance by the difference.

Only PENDING requests are touched — APPROVED/REJECTED/CANCELLED ones are
historical and left as-is. Idempotent: re-running after a fix finds
nothing to change. Requests whose range now has ZERO working days are
flagged for manual HR review rather than auto-zeroed.

The balance row is keyed by {userId, leaveTypeCode, year} where year is
the fromDate's year — exactly how create_leave_request reserves it.
"""
import asyncio

from database import db
from routes.leave import _working_dates_in_range, _parse_date


async def _new_total(req: dict):
    """Returns the recomputed chargeable days, or None if the range has no
    working days (invalid → needs manual review)."""
    working = await _working_dates_in_range(req["fromDate"], req["toDate"])
    if req.get("halfDay"):
        return 0.5 if working else None
    return float(len(working)) if working else None


async def main():
    pending = 0
    unchanged = 0
    updated = 0
    flagged: list[str] = []
    no_balance: list[str] = []

    async for req in db.leave_requests.find({"status": "PENDING"}):
        pending += 1
        rid = str(req["_id"])
        old = float(req.get("totalDays", 0) or 0)

        new = await _new_total(req)
        if new is None:
            flagged.append(
                f"{rid} ({req.get('fromDate')}..{req.get('toDate')}, "
                f"{req.get('leaveTypeCode')})"
            )
            continue

        if new == old:
            unchanged += 1
            continue

        delta = new - old  # negative when fewer days now charged
        year = _parse_date(req["fromDate"], "fromDate").year

        await db.leave_requests.update_one(
            {"_id": req["_id"]},
            {"$set": {"totalDays": new}},
        )

        res = await db.leave_balances.update_one(
            {
                "userId": req["userId"],
                "leaveTypeCode": req["leaveTypeCode"],
                "year": year,
            },
            {"$inc": {"pending": delta}},
        )
        if res.matched_count == 0:
            no_balance.append(rid)

        updated += 1
        print(f"  {rid}: totalDays {old} -> {new} (pending {delta:+})")

    print()
    print(f"PENDING requests scanned: {pending}")
    print(f"  unchanged:              {unchanged}")
    print(f"  updated:                {updated}")
    print(f"  flagged (0 work days):  {len(flagged)}")
    for f in flagged:
        print(f"     ! {f}")
    if no_balance:
        print(f"  balance row missing for: {no_balance}")


if __name__ == "__main__":
    asyncio.run(main())
