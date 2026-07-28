"""One-off: lowercase every stored user email.

Accounts created before email normalization may hold mixed-case emails, which a
now-lowercased login query won't match. Run once per database. Collisions (two
accounts that differ only by case) are reported and skipped, never merged.

    python migrate_lowercase_emails.py
"""
import asyncio
import sys

sys.path.insert(0, ".")
from database import db  # noqa: E402


async def main():
    changed = 0
    collisions = []
    async for u in db.users.find({}, {"email": 1}):
        email = u.get("email")
        if not isinstance(email, str):
            continue
        low = email.strip().lower()
        if low == email:
            continue
        clash = await db.users.find_one(
            {"email": low, "_id": {"$ne": u["_id"]}}
        )
        if clash:
            collisions.append((email, low))
            continue
        await db.users.update_one(
            {"_id": u["_id"]}, {"$set": {"email": low}}
        )
        changed += 1

    print(f"lowercased {changed} email(s)")
    if collisions:
        print("SKIPPED collisions — resolve manually before they can log in:")
        for original, low in collisions:
            print(f"  {original}  ->  {low}  (already exists)")


if __name__ == "__main__":
    asyncio.run(main())
