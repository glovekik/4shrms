import asyncio
from database import db


async def main():
    count = await db.users.count_documents({})
    print("Total users:", count)
    print()
    print("EMAIL".ljust(40), "NAME".ljust(25), "ROLE")
    print("-" * 80)
    async for u in db.users.find().sort("email", 1):
        email = u.get("email", "(no email)")
        name = u.get("name", "(no name)")
        role = u.get("role", "(none)")
        print(str(email).ljust(40), str(name).ljust(25), role)


if __name__ == "__main__":
    asyncio.run(main())
