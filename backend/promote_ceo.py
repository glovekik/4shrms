"""Promote an existing user account to role=CEO.

Why a script and not an API endpoint: granting CEO is a one-time bootstrap
action that should not be reachable via HR's normal admin surface. Running
this requires shell access to the host, which is the access boundary we
trust for granting org-wide visibility.

Usage: python promote_ceo.py <email>
"""

import asyncio
import sys

from database import db


async def main(email: str) -> None:
    user = await db.users.find_one({"email": email})
    if not user:
        print(f"No user with email: {email}")
        return

    result = await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"role": "CEO"}},
    )
    print(
        f"Matched: {result.matched_count}  "
        f"Modified: {result.modified_count}"
    )

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
        print("Usage: python promote_ceo.py <email>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
