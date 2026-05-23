import asyncio
import sys
from database import db


async def main(email: str):
    user = await db.users.find_one({"email": email})
    if not user:
        print(f"No user with email: {email}")
        return

    result = await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"role": "HR"}},
    )
    print(f"Matched: {result.matched_count}  Modified: {result.modified_count}")

    updated = await db.users.find_one({"_id": user["_id"]})
    print(
        "Now:",
        updated.get("email"),
        "|",
        updated.get("name"),
        "|",
        "role=" + str(updated.get("role")),
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python promote_hr.py <email>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
