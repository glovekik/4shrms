"""Undo migrate_teams_to_projects.py from the pre-phase1 backup.

Restores `projects` and `teams` exactly as they were and drops every
project_members row. Safe to run more than once.

    python rollback_phase1.py backups/pre-phase1-2026-08-21.json
"""

import asyncio
import os
import sys

from bson import json_util
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


async def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    with open(sys.argv[1]) as f:
        data = json_util.loads(f.read())

    uri = os.getenv("MONGO_URL")
    dbname = os.getenv("MONGO_DB_NAME") or "attendance_db"
    db = AsyncIOMotorClient(uri)[dbname]
    print(f"DB: {dbname}  restoring from {sys.argv[1]}")

    for col in ("projects", "teams"):
        docs = data.get(col) or []
        await db[col].delete_many({})
        if docs:
            await db[col].insert_many(docs)
        print(f"  {col}: restored {len(docs)} docs")

    dropped = await db.project_members.delete_many({})
    print(f"  project_members: dropped {dropped.deleted_count} rows")
    print("rollback complete")


asyncio.run(main())
