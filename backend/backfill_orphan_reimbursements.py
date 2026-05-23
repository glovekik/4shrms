"""One-shot migration: move PENDING_MANAGER reimbursements stuck on
users with no reportingManagerId to PENDING_HR so HR can act on them.

Run: python backfill_orphan_reimbursements.py
Add --dry-run to preview without writing.

Idempotent: re-running has no effect once orphans have been moved.
"""

import argparse
import asyncio
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId

from database import db


async def main(dry_run: bool) -> None:
    no_manager_ids: list[str] = []
    async for u in db.users.find(
        {
            "$or": [
                {"reportingManagerId": None},
                {"reportingManagerId": ""},
                {"reportingManagerId": {"$exists": False}},
            ]
        },
        {"_id": 1},
    ):
        no_manager_ids.append(str(u["_id"]))

    if not no_manager_ids:
        print("No users without a reporting manager — nothing to migrate.")
        return

    query = {
        "status": "PENDING_MANAGER",
        "userId": {"$in": no_manager_ids},
    }

    affected: list[dict] = []
    async for r in db.reimbursement_requests.find(query):
        affected.append(r)

    print(
        f"Users without manager: {len(no_manager_ids)} | "
        f"PENDING_MANAGER reimbursements to redirect: {len(affected)}"
    )

    if not affected:
        return

    for r in affected:
        print(
            f"  - {r.get('_id')}  user={r.get('userId')}  "
            f"title={r.get('title')!r}  amount={r.get('amount')}"
        )

    if dry_run:
        print("Dry run — no changes written.")
        return

    now = datetime.now(timezone.utc)
    result = await db.reimbursement_requests.update_many(
        query,
        {
            "$set": {
                "status": "PENDING_HR",
                "updatedAt": now,
                "managerBypassReason": "no_reporting_manager_backfill",
                "managerBypassAt": now,
            }
        },
    )
    print(
        f"Matched: {result.matched_count}  "
        f"Modified: {result.modified_count}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
