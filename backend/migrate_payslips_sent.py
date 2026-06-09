"""One-time migration: back-fill the `sent` release flag on payslips that
predate send-gating.

Before send-gating, any processed payslip was immediately visible to the
employee. The new employee queries filter on `sent == True`, so payslips
created before this change (which have no `sent` field) would silently
disappear from My Payslips. This marks them as already-sent so existing
payslips stay visible. Idempotent — safe to run multiple times.
"""
import asyncio
from datetime import datetime, timezone
from database import db


async def main():
    missing = await db.payslips.count_documents({"sent": {"$exists": False}})
    print(f"payslips missing 'sent': {missing}")
    if missing == 0:
        print("Nothing to migrate.")
        return

    now = datetime.now(timezone.utc)
    updated = 0
    async for p in db.payslips.find({"sent": {"$exists": False}}):
        # These were effectively released when generated under the old
        # behavior — stamp sentAt from generatedAt when we have it.
        sent_at = p.get("generatedAt") or now
        await db.payslips.update_one(
            {"_id": p["_id"]},
            {"$set": {"sent": True, "sentAt": sent_at}},
        )
        updated += 1

    remaining = await db.payslips.count_documents({"sent": {"$exists": False}})
    print(f"updated {updated} payslip(s) to sent=True; remaining missing: {remaining}")


if __name__ == "__main__":
    asyncio.run(main())
