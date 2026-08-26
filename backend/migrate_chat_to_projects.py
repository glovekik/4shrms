"""Phase 4: retag team chat channels as project channels.

Channel ids don't change — the Phase 1 migration kept the team `_id` as the
project `_id` — so this only rewrites `channelType` from "team" to "project"
on chat messages and read markers.

Safe to run more than once.

    python migrate_chat_to_projects.py            # dry run
    python migrate_chat_to_projects.py --apply
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

APPLY = "--apply" in sys.argv


async def main():
    uri = os.getenv("MONGO_URL")
    dbname = os.getenv("MONGO_DB_NAME") or "attendance_db"
    db = AsyncIOMotorClient(uri)[dbname]
    print(f"DB: {dbname}   mode: {'APPLY' if APPLY else 'DRY RUN'}")

    for col in ("chat_messages", "chat_reads"):
        n = await db[col].count_documents({"channelType": "team"})
        print(f"  {col}: {n} row(s) tagged 'team'")
        if n and APPLY:
            res = await db[col].update_many(
                {"channelType": "team"},
                {"$set": {"channelType": "project"}},
            )
            print(f"    -> retagged {res.modified_count}")

    # Any channel id that no longer resolves to a project would become
    # unreachable, so surface it rather than silently orphaning the thread.
    orphans = []
    async for m in db.chat_messages.find(
        {"channelType": {"$in": ["team", "project"]}}, {"channelId": 1}
    ):
        cid = m.get("channelId")
        if cid and cid not in orphans:
            from bson import ObjectId
            from bson.errors import InvalidId
            try:
                exists = await db.projects.find_one({"_id": ObjectId(cid)})
            except (InvalidId, TypeError):
                exists = None
            if not exists:
                orphans.append(cid)
    if orphans:
        print(f"  WARNING: {len(orphans)} channel id(s) have no project: {orphans}")
    else:
        print("  every chat channel resolves to a project")

    if not APPLY:
        print("\nDRY RUN — nothing written.")


asyncio.run(main())
