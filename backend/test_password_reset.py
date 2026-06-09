"""Standalone test for the code-based password reset flow.

Runs the FastAPI app via httpx ASGI transport against an in-memory fake of
the two Mongo collections it touches (`users`, `otp_codes`,
`password_reset_tokens`). No live Mongo or SMTP needed.

Run from the backend/ directory:
    python test_password_reset.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# Make sure SMTP is "configured" so /forgot-password emails.
os.environ.setdefault("SMTP_HOST", "smtp.test.local")
os.environ.setdefault("SMTP_FROM", "noreply@test.local")
os.environ.setdefault("SMTP_USERNAME", "")
os.environ.setdefault("SMTP_PASSWORD", "")
os.environ.setdefault("REQUIRE_LOGIN_OTP", "false")

# Add backend/ to path so `import routes.auth` works when invoked from repo root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bson import ObjectId  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

import routes.auth as auth_module  # noqa: E402


# ---------------------------------------------------------------------------
# In-memory fakes for the three Mongo collections the reset flow touches.
# Only the methods actually called by routes.auth are implemented.
# ---------------------------------------------------------------------------
class FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    @staticmethod
    def _matches(doc: dict, query: dict) -> bool:
        for k, v in query.items():
            if isinstance(v, dict):
                # support {"$gt": value}
                actual = doc.get(k)
                for op, val in v.items():
                    if op == "$gt":
                        if not (actual is not None and actual > val):
                            return False
                    elif op == "$lt":
                        if not (actual is not None and actual < val):
                            return False
                    else:
                        raise NotImplementedError(f"op {op}")
            else:
                if doc.get(k) != v:
                    return False
        return True

    async def find_one(self, query: dict) -> dict | None:
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
        target = None
        for d in self.docs:
            if self._matches(d, query):
                target = d
                break

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
                raise NotImplementedError(f"op {op}")
        return None


class FakeDB:
    def __init__(self) -> None:
        self.users = FakeCollection()
        self.otp_codes = FakeCollection()
        self.password_reset_tokens = FakeCollection()


# ---------------------------------------------------------------------------
# Patch routes.auth's db + email so the route logic runs against fakes.
# ---------------------------------------------------------------------------
fake_db = FakeDB()
sent_emails: list[tuple[str, str, str]] = []


async def fake_send_email(to: str, subject: str, body: str) -> bool:
    sent_emails.append((to, subject, body))
    return True


auth_module.db = fake_db
auth_module.send_notification_email = fake_send_email


# ---------------------------------------------------------------------------
# Seed a user.
# ---------------------------------------------------------------------------
USER_EMAIL = "alice@example.com"
USER_OLD_PWD = "oldpassword123"

user_id = ObjectId()
fake_db.users.docs.append({
    "_id": user_id,
    "email": USER_EMAIL,
    "name": "Alice",
    "password": auth_module.hash_password(USER_OLD_PWD),
    "role": "USER",
})


# ---------------------------------------------------------------------------
# Build a minimal FastAPI app with just the auth router.
# ---------------------------------------------------------------------------
app = FastAPI()
app.include_router(auth_module.router, prefix="/auth")


# ---------------------------------------------------------------------------
# Test helpers.
# ---------------------------------------------------------------------------
class T:
    passed = 0
    failed = 0

    @classmethod
    def ok(cls, name: str) -> None:
        cls.passed += 1
        print(f"  PASS  {name}")

    @classmethod
    def fail(cls, name: str, detail: str) -> None:
        cls.failed += 1
        print(f"  FAIL  {name}\n        {detail}")

    @classmethod
    def expect(cls, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            cls.ok(name)
        else:
            cls.fail(name, detail)


def _latest_code_for(email: str) -> str | None:
    """Read the persisted OTP for the user — simulates the email recipient."""
    user = next((u for u in fake_db.users.docs if u["email"] == email), None)
    if not user:
        return None
    record = next(
        (
            r for r in fake_db.otp_codes.docs
            if r.get("userId") == str(user["_id"])
            and r.get("purpose") == "password_reset"
        ),
        None,
    )
    return record["code"] if record else None


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------
async def run() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:

        # --- 1. forgot-password issues a code and emails it ----------------
        r = await c.post("/auth/forgot-password", json={"email": USER_EMAIL})
        T.expect("forgot-password 200", r.status_code == 200, r.text)
        T.expect(
            "forgot-password generic message",
            "code has been sent" in r.json().get("message", ""),
            r.text,
        )
        T.expect(
            "email was sent",
            len(sent_emails) == 1 and sent_emails[0][0] == USER_EMAIL,
            f"sent_emails={sent_emails}",
        )
        code = _latest_code_for(USER_EMAIL)
        T.expect(
            "6-digit code persisted",
            code is not None and len(code) == 6 and code.isdigit(),
            f"code={code}",
        )

        # --- 2. forgot-password for unknown email returns same shape -------
        r = await c.post(
            "/auth/forgot-password", json={"email": "ghost@example.com"}
        )
        T.expect(
            "no-enum: unknown email same response",
            r.status_code == 200 and "code has been sent" in r.json()["message"],
            r.text,
        )
        T.expect(
            "no-enum: no email sent for unknown",
            len(sent_emails) == 1,
            f"sent_emails={sent_emails}",
        )

        # --- 3. cooldown: a second request within 60s does NOT issue a new code
        sent_before = len(sent_emails)
        r = await c.post("/auth/forgot-password", json={"email": USER_EMAIL})
        T.expect(
            "cooldown swallows rapid resend",
            r.status_code == 200 and len(sent_emails) == sent_before,
            f"emails after={len(sent_emails)}",
        )

        # --- 4. verify-reset-code: wrong code increments attempts ----------
        r = await c.post(
            "/auth/verify-reset-code",
            json={"email": USER_EMAIL, "code": "000000"},
        )
        T.expect("wrong code 400", r.status_code == 400, r.text)
        rec = await fake_db.otp_codes.find_one({
            "userId": str(user_id), "purpose": "password_reset",
        })
        T.expect(
            "attempts incremented",
            rec is not None and rec.get("attempts") == 1,
            f"rec={rec}",
        )

        # --- 5. verify-reset-code: correct code mints a ticket -------------
        r = await c.post(
            "/auth/verify-reset-code",
            json={"email": USER_EMAIL, "code": code},
        )
        T.expect("correct code 200", r.status_code == 200, r.text)
        body = r.json()
        ticket = body.get("resetToken")
        T.expect("resetToken returned", bool(ticket), f"body={body}")
        T.expect(
            "expiresInMinutes present",
            body.get("expiresInMinutes") == 15,
            f"body={body}",
        )

        # OTP should now be marked used.
        rec = await fake_db.otp_codes.find_one({
            "userId": str(user_id), "purpose": "password_reset",
        })
        T.expect("OTP marked used", rec is not None and rec.get("used") is True)

        # --- 6. replaying the same code fails ------------------------------
        r = await c.post(
            "/auth/verify-reset-code",
            json={"email": USER_EMAIL, "code": code},
        )
        T.expect("code replay rejected", r.status_code == 400, r.text)

        # --- 7. reset-password rejects short password ----------------------
        r = await c.post(
            "/auth/reset-password",
            json={"resetToken": ticket, "newPassword": "short"},
        )
        T.expect("short password 400", r.status_code == 400, r.text)

        # --- 8. reset-password with a bogus ticket fails -------------------
        r = await c.post(
            "/auth/reset-password",
            json={"resetToken": "not-a-real-ticket", "newPassword": "newpassword1"},
        )
        T.expect("bogus ticket 400", r.status_code == 400, r.text)

        # --- 9. reset-password with the real ticket succeeds & rotates pwd
        new_pwd = "brand-new-pwd-9"
        r = await c.post(
            "/auth/reset-password",
            json={"resetToken": ticket, "newPassword": new_pwd},
        )
        T.expect("reset-password 200", r.status_code == 200, r.text)

        updated = next(u for u in fake_db.users.docs if u["_id"] == user_id)
        T.expect(
            "password actually rotated",
            auth_module.verify_password(new_pwd, updated["password"]),
        )
        T.expect(
            "old password no longer works",
            not auth_module.verify_password(USER_OLD_PWD, updated["password"]),
        )

        # --- 10. ticket cannot be reused -----------------------------------
        r = await c.post(
            "/auth/reset-password",
            json={"resetToken": ticket, "newPassword": "another-pass-9"},
        )
        T.expect("ticket single-use", r.status_code == 400, r.text)

        # --- 11. expired code path: tamper expiresAt back in time ----------
        # Issue a fresh code by skipping cooldown — clear createdAt.
        for r2 in fake_db.otp_codes.docs:
            r2["createdAt"] = datetime.now(timezone.utc) - timedelta(hours=1)
        r = await c.post(
            "/auth/forgot-password", json={"email": USER_EMAIL}
        )
        assert r.status_code == 200
        fresh_code = _latest_code_for(USER_EMAIL)
        # Move expiry into the past.
        for r2 in fake_db.otp_codes.docs:
            if r2.get("userId") == str(user_id):
                r2["expiresAt"] = datetime.now(timezone.utc) - timedelta(minutes=1)
                r2["attempts"] = 0
                r2["used"] = False
        r = await c.post(
            "/auth/verify-reset-code",
            json={"email": USER_EMAIL, "code": fresh_code},
        )
        T.expect(
            "expired code rejected",
            r.status_code == 400 and "expired" in r.json().get("detail", "").lower(),
            r.text,
        )

        # --- 11b. back-compat: legacy `token` field still works -----------
        # Reset the user's password again via a fresh code, but send the
        # ticket back under the old field name.
        for r2 in fake_db.otp_codes.docs:
            r2["createdAt"] = datetime.now(timezone.utc) - timedelta(hours=1)
        await c.post("/auth/forgot-password", json={"email": USER_EMAIL})
        legacy_code = _latest_code_for(USER_EMAIL)
        v = await c.post(
            "/auth/verify-reset-code",
            json={"email": USER_EMAIL, "code": legacy_code},
        )
        legacy_ticket = v.json()["resetToken"]
        legacy_pwd = "legacy-flow-pwd-1"
        r = await c.post(
            "/auth/reset-password",
            json={"token": legacy_ticket, "newPassword": legacy_pwd},
        )
        T.expect("legacy token field accepted", r.status_code == 200, r.text)
        updated = next(u for u in fake_db.users.docs if u["_id"] == user_id)
        T.expect(
            "legacy flow actually rotated password",
            auth_module.verify_password(legacy_pwd, updated["password"]),
        )

        # --- 11c. neither field provided returns 400 ----------------------
        r = await c.post(
            "/auth/reset-password",
            json={"newPassword": "whatever12"},
        )
        T.expect(
            "missing ticket 400",
            r.status_code == 400,
            r.text,
        )

        # --- 12. attempt cap: 5 wrong tries → 429 --------------------------
        # Re-arm the record with a known code and zero attempts.
        for r2 in fake_db.otp_codes.docs:
            if r2.get("userId") == str(user_id):
                r2["code"] = "123456"
                r2["expiresAt"] = datetime.now(timezone.utc) + timedelta(minutes=5)
                r2["attempts"] = 0
                r2["used"] = False
        for _ in range(5):
            await c.post(
                "/auth/verify-reset-code",
                json={"email": USER_EMAIL, "code": "999999"},
            )
        r = await c.post(
            "/auth/verify-reset-code",
            json={"email": USER_EMAIL, "code": "123456"},
        )
        T.expect("attempt-cap 429", r.status_code == 429, r.text)


if __name__ == "__main__":
    asyncio.run(run())
    print(f"\n{T.passed} passed, {T.failed} failed")
    sys.exit(0 if T.failed == 0 else 1)
