"""Inserts a test HR user with known credentials so the API tester can log in.

Idempotent — re-running just resets the password.
"""

import sys
sys.path.insert(0, "backend")

import asyncio
from datetime import datetime, timezone
from passlib.context import CryptContext

from database import db

TEST_HR_EMAIL = "test-hr@apitest.example.com"
TEST_HR_PWD = "test-hr-pass-123"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


async def main():
    now = datetime.now(timezone.utc)
    hashed = pwd_context.hash(TEST_HR_PWD)
    await db.users.update_one(
        {"email": TEST_HR_EMAIL},
        {
            "$set": {
                "email": TEST_HR_EMAIL,
                "name": "API Test HR",
                "password": hashed,
                "role": "HR",
                "status": "Active",
                "updatedAt": now,
            },
            "$setOnInsert": {"createdAt": now},
        },
        upsert=True,
    )
    print(f"OK: HR user ready — {TEST_HR_EMAIL} / {TEST_HR_PWD}")


if __name__ == "__main__":
    asyncio.run(main())
