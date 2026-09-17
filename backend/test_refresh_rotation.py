"""Standalone test for refresh-token rotation and its grace window.

Runs the auth router via httpx ASGI transport against an in-memory fake of
the two collections it touches. No live Mongo, no network — and deliberately
so: this must never be pointed at a real database.

Run from the backend/ directory:
    python test_refresh_rotation.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

os.environ.setdefault("SECRET_KEY", "x" * 48)
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("REQUIRE_LOGIN_OTP", "false")
os.environ.setdefault("REFRESH_ROTATION_GRACE_SECONDS", "60")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bson import ObjectId  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

import routes.auth as auth_module  # noqa: E402
from config import REFRESH_ROTATION_GRACE_SECONDS  # noqa: E402


class FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    @staticmethod
    def _matches(doc: dict, query: dict) -> bool:
        for k, v in query.items():
            actual = doc.get(k)
            if isinstance(v, dict):
                for op, val in v.items():
                    if op == "$gt" and not (actual is not None and actual > val):
                        return False
                    if op == "$lt" and not (actual is not None and actual < val):
                        return False
                    if op not in ("$gt", "$lt"):
                        raise NotImplementedError(op)
            elif actual != v:
                return False
        return True

    async def find_one(self, query: dict, *a, **kw) -> dict | None:
        for d in self.docs:
            if self._matches(d, query):
                return d
        return None

    async def insert_one(self, doc: dict):
        doc.setdefault("_id", ObjectId())
        self.docs.append(doc)

        class _R:
            inserted_id = doc["_id"]

        return _R()

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        target = next((d for d in self.docs if self._matches(d, query)), None)
        if target is None:
            if not upsert:
                return None
            target = {"_id": ObjectId()}
            self.docs.append(target)
        for op, payload in update.items():
            if op == "$set":
                target.update(payload)
            elif op == "$inc":
                for k, v in payload.items():
                    target[k] = target.get(k, 0) + v
            else:
                raise NotImplementedError(op)
        return None

    async def delete_one(self, query: dict):
        target = next((d for d in self.docs if self._matches(d, query)), None)
        if target is not None:
            self.docs.remove(target)
        return None


class FakeDB:
    def __init__(self) -> None:
        self.users = FakeCollection()
        self.refresh_tokens = FakeCollection()
        self.otp_codes = FakeCollection()


fake_db = FakeDB()
auth_module.db = fake_db

USER_ID = ObjectId()
fake_db.users.docs.append({
    "_id": USER_ID,
    "email": "alice@example.com",
    "name": "Alice",
    "password": auth_module.hash_password("pw12345678"),
    "role": "USER",
})

app = FastAPI()
app.include_router(auth_module.router, prefix="/auth")


class T:
    passed = 0
    failed = 0

    @classmethod
    def expect(cls, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            cls.passed += 1
            print(f"  PASS  {name}")
        else:
            cls.failed += 1
            print(f"  FAIL  {name}\n        {detail}")


def _record(token: str) -> dict | None:
    return next(
        (d for d in fake_db.refresh_tokens.docs if d.get("token") == token),
        None,
    )


async def run() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:

        # --- login issues a pair ------------------------------------------
        r = await c.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": "pw12345678"},
        )
        T.expect("login 200", r.status_code == 200, r.text)
        rt0 = r.json().get("refresh_token")
        T.expect("login returns a refresh token", bool(rt0), r.text)

        # --- a normal refresh rotates -------------------------------------
        r = await c.post("/auth/refresh", json={"refresh_token": rt0})
        T.expect("refresh 200", r.status_code == 200, r.text)
        body = r.json()
        rt1 = body.get("refresh_token")
        T.expect("refresh returns an access token", bool(body.get("access_token")))
        T.expect("refresh rotates the token", rt1 and rt1 != rt0)

        old = _record(rt0)
        T.expect("old token is kept, not deleted", old is not None)
        T.expect("old token points at its successor",
                 old and old.get("replacedBy") == rt1, str(old))
        T.expect("old token's expiry is pulled in to the grace window",
                 old
                 and old["expiresAt"] - old["rotatedAt"]
                 == timedelta(seconds=REFRESH_ROTATION_GRACE_SECONDS),
                 str(old))

        # --- THE FIX: replaying the old token inside the window -----------
        # This is the client that never received the response above.
        r = await c.post("/auth/refresh", json={"refresh_token": rt0})
        T.expect("replay within grace succeeds", r.status_code == 200, r.text)
        replay = r.json()
        T.expect("replay returns the SAME successor, not a new chain",
                 replay.get("refresh_token") == rt1,
                 f"{replay.get('refresh_token')} != {rt1}")
        T.expect("replay still issues a usable access token",
                 bool(replay.get("access_token")))
        T.expect("replay didn't mint an extra token",
                 len(fake_db.refresh_tokens.docs) == 2,
                 str([d.get("token") for d in fake_db.refresh_tokens.docs]))

        # --- the successor itself still works -----------------------------
        r = await c.post("/auth/refresh", json={"refresh_token": rt1})
        T.expect("successor refreshes normally", r.status_code == 200, r.text)
        rt2 = r.json().get("refresh_token")
        T.expect("successor rotates too", rt2 and rt2 not in (rt0, rt1))

        # --- replay AFTER the window is rejected --------------------------
        rec = _record(rt1)
        rec["rotatedAt"] = datetime.now(timezone.utc) - timedelta(
            seconds=REFRESH_ROTATION_GRACE_SECONDS + 5
        )
        r = await c.post("/auth/refresh", json={"refresh_token": rt1})
        T.expect("replay outside grace is 401", r.status_code == 401, r.text)
        T.expect("...and the replayed token is burnt", _record(rt1) is None)

        # --- unknown / expired / terminated -------------------------------
        r = await c.post("/auth/refresh", json={"refresh_token": "nope"})
        T.expect("unknown token is 401", r.status_code == 401, r.text)

        expired = "expired-token"
        fake_db.refresh_tokens.docs.append({
            "_id": ObjectId(),
            "token": expired,
            "userId": str(USER_ID),
            "expiresAt": datetime.now(timezone.utc) - timedelta(days=1),
            "createdAt": datetime.now(timezone.utc) - timedelta(days=40),
        })
        r = await c.post("/auth/refresh", json={"refresh_token": expired})
        T.expect("expired token is 401", r.status_code == 401, r.text)
        T.expect("expired token is deleted", _record(expired) is None)

        user = fake_db.users.docs[0]
        user["status"] = "Terminated"
        r = await c.post("/auth/refresh", json={"refresh_token": rt2})
        T.expect("terminated account is 403", r.status_code == 403, r.text)
        T.expect("terminated account's token is revoked", _record(rt2) is None)
        user.pop("status")

        # --- logout revokes -----------------------------------------------
        r = await c.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": "pw12345678"},
        )
        rt3 = r.json()["refresh_token"]
        r = await c.post("/auth/logout", json={"refresh_token": rt3})
        T.expect("logout 200", r.status_code == 200, r.text)
        T.expect("logout deletes the token", _record(rt3) is None)
        r = await c.post("/auth/refresh", json={"refresh_token": rt3})
        T.expect("a logged-out token can't refresh", r.status_code == 401)

    print(f"\n{T.passed} passed, {T.failed} failed")
    sys.exit(1 if T.failed else 0)


asyncio.run(run())
