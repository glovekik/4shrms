"""One-shot migration: collapse duplicate leave_balances rows.

A unique index on (userId, leaveTypeCode, year) is already declared in
database.create_indexes(), but it cannot take effect while duplicates
exist — Mongo silently leaves the index uncreated (or, depending on the
driver path, raises a DuplicateKeyError). This script collapses every
duplicate cluster into a single row, then re-creates the index.

Merge policy: keep the highest allocated/used/pending across duplicates.
Rationale: approvals only ever decrement one row at a time, so summing
`used` would double-count. `max` preserves the worst-case (highest used /
pending), which is the safe direction for an employee.

Run: python dedupe_leave_balances.py
Add --dry-run to preview without writing.
"""

import argparse
import asyncio

from pymongo import IndexModel, ASCENDING

from database import db


async def main(dry_run: bool) -> None:
    pipeline = [
        {
            "$group": {
                "_id": {
                    "userId": "$userId",
                    "leaveTypeCode": "$leaveTypeCode",
                    "year": "$year",
                },
                "ids": {"$push": "$_id"},
                "allocated": {"$max": "$allocated"},
                "used": {"$max": "$used"},
                "pending": {"$max": "$pending"},
                "count": {"$sum": 1},
            }
        },
        {"$match": {"count": {"$gt": 1}}},
    ]

    clusters: list[dict] = []
    async for row in db.leave_balances.aggregate(pipeline):
        clusters.append(row)

    print(f"Duplicate clusters found: {len(clusters)}")

    if not clusters:
        if not dry_run:
            await _ensure_unique_index()
        return

    for c in clusters:
        key = c["_id"]
        ids = c["ids"]
        print(
            f"  - {key['userId']} / {key['leaveTypeCode']} / "
            f"{key['year']}: {c['count']} rows  "
            f"(allocated={c['allocated']}, used={c['used']}, "
            f"pending={c['pending']})"
        )
        if dry_run:
            continue

        keeper = ids[0]
        dropouts = ids[1:]
        await db.leave_balances.update_one(
            {"_id": keeper},
            {
                "$set": {
                    "allocated": c["allocated"] or 0.0,
                    "used": c["used"] or 0.0,
                    "pending": c["pending"] or 0.0,
                }
            },
        )
        if dropouts:
            await db.leave_balances.delete_many(
                {"_id": {"$in": dropouts}}
            )

    if dry_run:
        print("Dry run — no changes written.")
        return

    print("Dedupe complete. Ensuring unique index…")
    await _ensure_unique_index()
    print("Done.")


async def _ensure_unique_index() -> None:
    # Idempotent: create_indexes is a no-op if the index already exists
    # with the same spec.
    await db.leave_balances.create_indexes([
        IndexModel(
            [
                ("userId", ASCENDING),
                ("leaveTypeCode", ASCENDING),
                ("year", ASCENDING),
            ],
            unique=True,
            name="userId_1_leaveTypeCode_1_year_1",
        )
    ])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
