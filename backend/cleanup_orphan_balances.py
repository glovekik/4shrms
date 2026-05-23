"""One-shot cleanup for orphan leave_balances rows.

Deletes any row in leave_balances whose `leaveTypeCode` no longer matches
an existing entry in leave_types. Run once after deploying the cascade
fix in routes/leave.delete_leave_type to scrub historical orphans like
"EARN68c0d4cd" that were created before the cascade was in place.

Usage:
    cd attendence_app_backend/backend
    venv\\Scripts\\python cleanup_orphan_balances.py
"""

import asyncio

from database import db


async def main() -> None:
    valid_codes = await db.leave_types.distinct("code")
    if not valid_codes:
        print(
            "[cleanup] No leave_types found — refusing to nuke every balance row. "
            "Seed at least one type first."
        )
        return

    # Count first so we can print a useful summary.
    before = await db.leave_balances.count_documents({})
    result = await db.leave_balances.delete_many(
        {"leaveTypeCode": {"$nin": valid_codes}}
    )
    after = await db.leave_balances.count_documents({})

    print(
        f"[cleanup] leave_balances: {before} -> {after} "
        f"(deleted {result.deleted_count} orphan row(s))"
    )
    print(f"[cleanup] valid codes kept: {valid_codes}")


if __name__ == "__main__":
    asyncio.run(main())
