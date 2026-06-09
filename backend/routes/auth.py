from fastapi import (
    APIRouter,
    HTTPException,
    Depends
)

from pydantic import BaseModel, EmailStr

from typing import Literal

from secrets import token_urlsafe, randbelow

from passlib.context import CryptContext

from jose import jwt

from datetime import (
    datetime,
    timedelta,
    timezone,
)

from bson import ObjectId

from database import db

from utils.dependencies import (
    get_current_user
)

from utils.email import send_notification_email

from config import (
    SECRET_KEY,
    ALGORITHM,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
    COMPANY_NAME,
    REQUIRE_LOGIN_OTP,
    OTP_TTL_MINUTES,
    is_email_configured,
)


router = APIRouter()


pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto"
)


# ================= MODELS =================
class SignupModel(BaseModel):

    name: str

    email: str

    password: str


class LoginModel(BaseModel):

    email: str

    password: str


# ================= HASH PASSWORD =================
def hash_password(
    password: str
):

    return pwd_context.hash(
        password
    )


# ================= VERIFY PASSWORD =================
def verify_password(
    plain_password: str,
    hashed_password: str
):

    return pwd_context.verify(

        plain_password,

        hashed_password
    )


# ================= CREATE TOKEN =================
def create_access_token(
    data: dict
):

    to_encode = data.copy()

    expire = datetime.now(timezone.utc) + timedelta(

        minutes=
        ACCESS_TOKEN_EXPIRE_MINUTES
    )

    to_encode.update({

        "exp": expire
    })

    encoded_jwt = jwt.encode(

        to_encode,

        SECRET_KEY,

        algorithm=ALGORITHM
    )

    return encoded_jwt


# ================= REFRESH TOKENS =================
# Opaque, server-stored session tokens (db.refresh_tokens). Unlike the
# stateless access JWT, these are long-lived AND revocable, so a session
# survives access-token expiry but can still be killed on logout. Stored
# raw to match the existing password_reset_tokens pattern.
async def _issue_refresh_token(user_id: str) -> str:

    token = token_urlsafe(48)

    now = datetime.now(timezone.utc)

    await db.refresh_tokens.insert_one({
        "token": token,
        "userId": user_id,
        "expiresAt": now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        "createdAt": now,
    })

    return token


async def _build_auth_response(user: dict) -> dict:
    """The token payload the app expects on login / verify-otp / refresh:
    a short-lived access JWT plus a fresh long-lived refresh token."""

    access_token = create_access_token({
        "sub": str(user["_id"])
    })

    refresh_token = await _issue_refresh_token(str(user["_id"]))

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token,
    }


# ================= SIGNUP =================
@router.post("/signup")
async def signup(
    data: SignupModel
):

    existing_user = \
        await db.users.find_one({

            "email": data.email
        })

    if existing_user:

        raise HTTPException(

            status_code=400,

            detail=
            "Email already exists"
        )

    # First signup ever bootstraps the HR account.
    # After that, public signup is locked — HR creates users via /hr/users.
    user_count = \
        await db.users.count_documents({})

    if user_count == 0:
        role = "HR"

    else:
        raise HTTPException(
            status_code=403,
            detail=
            "Signup is closed. Contact HR to be added.",
        )

    user = {

        "name":
        data.name,

        "email":
        data.email,

        "password":
        hash_password(
            data.password
        ),

        "role":
        role,

        "createdAt":
        datetime.now(timezone.utc),
    }

    result = \
        await db.users.insert_one(
            user
        )

    return {

        "message":
        "Signup successful",

        "userId":
        str(result.inserted_id),

        "role":
        role,
    }


# ================= LOGIN =================
@router.post("/login")
async def login(
    data: LoginModel
):

    user = await db.users.find_one({

        "email": data.email
    })

    if not user:

        raise HTTPException(

            status_code=400,

            detail=
            "Invalid email or password"
        )

    valid_password = \
        verify_password(

            data.password,

            user["password"]
        )

    if not valid_password:

        raise HTTPException(

            status_code=400,

            detail=
            "Invalid email or password"
        )

    # Terminated accounts can't sign in. We keep their record (for
    # audit/payroll history) but the token never issues. HR can
    # reactivate from the Employees screen if needed.
    if user.get("status") == "Terminated":
        raise HTTPException(
            status_code=403,
            detail=(
                "This account is no longer active. Contact HR if you "
                "believe this is a mistake."
            ),
        )

    # Optional second factor — when REQUIRE_LOGIN_OTP is true, the user
    # must call /auth/verify-otp with the emailed code instead of getting
    # the token here. Falls back gracefully if email isn't configured.
    if REQUIRE_LOGIN_OTP and is_email_configured() and user.get("email"):
        await _issue_login_otp(user)
        return {
            "step": "OTP_REQUIRED",
            "message": (
                "An OTP has been sent to your email. "
                "Call /auth/verify-otp to complete login."
            ),
        }

    return await _build_auth_response(user)


# ================= OTP HELPERS + ENDPOINTS =================
class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp: str


async def _issue_login_otp(user: dict) -> None:
    """Generate a 6-digit OTP, persist with TTL, email to user. Best effort."""
    code = f"{randbelow(1_000_000):06d}"
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=OTP_TTL_MINUTES)

    # One pending OTP per user — replace any older code.
    await db.otp_codes.update_one(
        {"userId": str(user["_id"]), "purpose": "login"},
        {
            "$set": {
                "userId": str(user["_id"]),
                "purpose": "login",
                "code": code,
                "expiresAt": expires,
                "used": False,
                "createdAt": now,
            }
        },
        upsert=True,
    )

    body = (
        f"Hi {user.get('name', 'there')},\n\n"
        f"Your {COMPANY_NAME} login code is: {code}\n\n"
        f"This code expires in {OTP_TTL_MINUTES} minute(s). "
        "If you didn't try to log in, ignore this email.\n\n"
        f"Regards,\n{COMPANY_NAME}"
    )
    await send_notification_email(
        user["email"],
        f"{COMPANY_NAME} login code: {code}",
        body,
    )


@router.post("/verify-otp")
async def verify_otp(data: VerifyOtpRequest):
    """Step 2 of OTP login. Returns the JWT on a valid, unexpired code."""
    user = await db.users.find_one({"email": data.email})
    if not user:
        # Generic message — don't leak whether the email exists.
        raise HTTPException(400, "Invalid email or OTP")

    record = await db.otp_codes.find_one({
        "userId": str(user["_id"]),
        "purpose": "login",
        "used": False,
    })
    if not record or record.get("code") != data.otp:
        raise HTTPException(400, "Invalid email or OTP")

    expires = record.get("expiresAt")
    now = datetime.now(timezone.utc)
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires and expires < now:
        raise HTTPException(400, "OTP expired")

    await db.otp_codes.update_one(
        {"_id": record["_id"]},
        {"$set": {"used": True, "usedAt": now}},
    )

    return await _build_auth_response(user)


@router.post("/resend-otp")
async def resend_otp(data: VerifyOtpRequest):
    """Re-issues an OTP. `otp` field is ignored — kept for body-shape symmetry."""
    user = await db.users.find_one({"email": data.email})
    if not user or not user.get("email"):
        return {"message": "If that email is registered, an OTP has been sent."}
    if not is_email_configured():
        raise HTTPException(503, "Email is not configured on the server")
    await _issue_login_otp(user)
    return {"message": "OTP sent"}


# ================= REFRESH / LOGOUT =================
class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


@router.post("/refresh")
async def refresh(data: RefreshRequest):
    """Exchange a valid refresh token for a new access token (and a rotated
    refresh token). Rotation means the old token is consumed on use, so a
    stolen-then-replayed token stops working the moment the real client
    refreshes. Any failure returns 401 → the app falls back to login."""

    now = datetime.now(timezone.utc)

    record = await db.refresh_tokens.find_one({
        "token": data.refresh_token
    })
    if not record:
        raise HTTPException(401, "Invalid or expired session")

    expires = record.get("expiresAt")
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires and expires < now:
        await db.refresh_tokens.delete_one({"_id": record["_id"]})
        raise HTTPException(401, "Invalid or expired session")

    # The user must still exist and be active — a refresh token must not
    # outlive the account it belongs to.
    user = None
    try:
        user = await db.users.find_one({
            "_id": ObjectId(record["userId"])
        })
    except (Exception,):
        user = None

    if not user:
        await db.refresh_tokens.delete_one({"_id": record["_id"]})
        raise HTTPException(401, "Invalid or expired session")

    if user.get("status") == "Terminated":
        await db.refresh_tokens.delete_one({"_id": record["_id"]})
        raise HTTPException(403, "This account is no longer active.")

    # Rotate: burn the used token, then mint a brand-new access + refresh pair.
    await db.refresh_tokens.delete_one({"_id": record["_id"]})
    return await _build_auth_response(user)


@router.post("/logout")
async def logout(data: LogoutRequest):
    """Revoke a refresh token server-side. Best-effort and always 200 so the
    client can clear its local session regardless. The access JWT remains
    valid until its (short) expiry — keep ACCESS_TOKEN_EXPIRE_MINUTES low for
    a tight logout window."""

    if data.refresh_token:
        await db.refresh_tokens.delete_one({
            "token": data.refresh_token
        })

    return {"message": "Logged out"}


# ================= GET CURRENT USER =================
@router.get("/me")
async def get_me(
    user_id: str = Depends(
        get_current_user
    )
):

    user = await db.users.find_one({

        "_id": ObjectId(user_id)
    })

    if not user:

        raise HTTPException(

            status_code=404,

            detail=
            "User not found"
        )

    # Team memberships — UI uses these to decide which sections/tabs to show.
    led_team_ids: list[str] = []
    member_of_team_ids: list[str] = []

    async for t in db.teams.find({
        "$or": [
            {"teamLeadId": user_id},
            {"memberIds": user_id},
        ]
    }):

        team_id = str(t["_id"])

        if t.get("teamLeadId") == user_id:
            led_team_ids.append(team_id)

        if user_id in t.get("memberIds", []):
            member_of_team_ids.append(team_id)

    return {

        "id":
        str(user["_id"]),

        "name":
        user.get("name"),

        "email":
        user.get("email"),

        "role":
        user.get("role", "USER"),

        # Profile fields (defaults for legacy users)
        "tag":
        user.get("tag", "Employee"),

        "employeeCode":
        user.get("employeeCode"),

        "workPhone":
        user.get("workPhone"),

        "joiningDate":
        user.get("joiningDate"),

        "status":
        user.get("status", "Active"),

        "profilePictureUrl":
        user.get("profilePictureUrl"),

        "ledTeamIds":
        led_team_ids,

        "memberOfTeamIds":
        member_of_team_ids,
    }


# ================= PUSH TOKEN REGISTRATION =================
class PushTokenRegister(BaseModel):
    token: str
    platform: Literal["ios", "android", "web"]


class PushTokenDelete(BaseModel):
    token: str


@router.post("/push-token")
async def register_push_token(
    data: PushTokenRegister,
    user_id: str = Depends(get_current_user),
):
    token = (data.token or "").strip()
    if not token:
        raise HTTPException(400, "token is required")

    now = datetime.now(timezone.utc)

    # Upsert by token: if the same token was previously registered to a
    # different user (e.g. shared device), it's reassigned to the new user.
    await db.push_tokens.update_one(
        {"token": token},
        {
            "$set": {
                "userId": user_id,
                "token": token,
                "platform": data.platform,
                "updatedAt": now,
            },
            "$setOnInsert": {"createdAt": now},
        },
        upsert=True,
    )
    return {"message": "Push token registered"}


@router.delete("/push-token")
async def delete_push_token(
    data: PushTokenDelete,
    user_id: str = Depends(get_current_user),
):
    token = (data.token or "").strip()
    if not token:
        raise HTTPException(400, "token is required")

    await db.push_tokens.delete_one({
        "token": token,
        "userId": user_id,
    })
    return {"message": "Push token removed"}


# ================= PASSWORD RESET =================
# Flow: /forgot-password emails a 6-digit code → /verify-reset-code exchanges
# code for a short-lived ticket → /reset-password sets new password using
# ticket. The ticket split keeps the brute-force surface on the code step
# (5 attempts, 10-min TTL) separate from the password-change step.
PASSWORD_RESET_CODE_TTL_MINUTES = 10
PASSWORD_RESET_TICKET_TTL_MINUTES = 15
PASSWORD_RESET_MAX_ATTEMPTS = 5
PASSWORD_RESET_RESEND_COOLDOWN_SECONDS = 60


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class VerifyResetCodeRequest(BaseModel):
    email: EmailStr
    code: str


class ResetPasswordRequest(BaseModel):
    # Back-compat: the old token-based flow sent {"token": "..."}. New
    # clients send {"resetToken": "..."}. Accept either for one release so
    # mid-rollout app versions don't break; drop `token` once everyone's on
    # the code-based flow.
    resetToken: str | None = None
    token: str | None = None
    newPassword: str

    @property
    def ticket(self) -> str:
        return self.resetToken or self.token or ""


@router.post("/forgot-password")
async def forgot_password(data: ForgotPasswordRequest):
    """Always returns the same message regardless of whether the email
    exists, so attackers can't enumerate accounts."""
    generic = {
        "message": (
            "If that email is registered, a code has been sent."
        )
    }

    user = await db.users.find_one({"email": data.email})
    if not user or not is_email_configured():
        return generic

    now = datetime.now(timezone.utc)

    # Cooldown: ignore back-to-back requests so the email isn't a spam vector.
    recent = await db.otp_codes.find_one({
        "userId": str(user["_id"]),
        "purpose": "password_reset",
        "createdAt": {
            "$gt": now - timedelta(
                seconds=PASSWORD_RESET_RESEND_COOLDOWN_SECONDS
            )
        },
    })
    if recent:
        return generic

    code = f"{randbelow(1_000_000):06d}"
    expires = now + timedelta(minutes=PASSWORD_RESET_CODE_TTL_MINUTES)

    await db.otp_codes.update_one(
        {"userId": str(user["_id"]), "purpose": "password_reset"},
        {
            "$set": {
                "userId": str(user["_id"]),
                "purpose": "password_reset",
                "code": code,
                "expiresAt": expires,
                "used": False,
                "attempts": 0,
                "createdAt": now,
            }
        },
        upsert=True,
    )

    body_text = (
        f"Hi {user.get('name', 'there')},\n\n"
        f"Your {COMPANY_NAME} password reset code is: {code}\n\n"
        f"Enter this code in the app to choose a new password. "
        f"It expires in {PASSWORD_RESET_CODE_TTL_MINUTES} minute(s). "
        "If you didn't request this, ignore this email.\n\n"
        f"Regards,\n{COMPANY_NAME}"
    )

    # send_notification_email never raises — failure is logged but we still
    # return the generic message so SMTP outages don't leak account state.
    await send_notification_email(
        user["email"],
        f"{COMPANY_NAME} password reset code",
        body_text,
    )

    return generic


@router.post("/verify-reset-code")
async def verify_reset_code(data: VerifyResetCodeRequest):
    """Validates the 6-digit code and mints a one-time reset ticket.

    Keeping verification separate means the new password is never sent
    in the same request that's being brute-forced for the code.
    """
    user = await db.users.find_one({"email": data.email})
    if not user:
        raise HTTPException(400, "Invalid email or code")

    record = await db.otp_codes.find_one({
        "userId": str(user["_id"]),
        "purpose": "password_reset",
        "used": False,
    })
    if not record:
        raise HTTPException(400, "Invalid email or code")

    if record.get("attempts", 0) >= PASSWORD_RESET_MAX_ATTEMPTS:
        raise HTTPException(
            429,
            "Too many attempts. Request a new code.",
        )

    expires = record.get("expiresAt")
    now = datetime.now(timezone.utc)
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires and expires < now:
        raise HTTPException(400, "Code expired")

    if record.get("code") != data.code:
        await db.otp_codes.update_one(
            {"_id": record["_id"]},
            {"$inc": {"attempts": 1}},
        )
        raise HTTPException(400, "Invalid email or code")

    # Code verified — burn it and mint a single-use ticket for the
    # password-change step. Ticket lives in password_reset_tokens so
    # the existing TTL index expires stale tickets automatically.
    ticket = token_urlsafe(32)
    ticket_expires = now + timedelta(
        minutes=PASSWORD_RESET_TICKET_TTL_MINUTES
    )

    await db.password_reset_tokens.insert_one({
        "userId": str(user["_id"]),
        "token": ticket,
        "expiresAt": ticket_expires,
        "used": False,
        "createdAt": now,
    })
    await db.otp_codes.update_one(
        {"_id": record["_id"]},
        {"$set": {"used": True, "usedAt": now}},
    )

    return {
        "resetToken": ticket,
        "expiresInMinutes": PASSWORD_RESET_TICKET_TTL_MINUTES,
    }


@router.post("/reset-password")
async def reset_password(data: ResetPasswordRequest):
    if len(data.newPassword) < 8:
        raise HTTPException(
            400,
            "Password must be at least 8 characters",
        )

    ticket = data.ticket
    if not ticket:
        raise HTTPException(
            400,
            "resetToken is required",
        )

    record = await db.password_reset_tokens.find_one({
        "token": ticket,
        "used": False,
    })
    if not record:
        raise HTTPException(
            400,
            "Invalid or expired token",
        )

    expires = record.get("expiresAt")
    now = datetime.now(timezone.utc)
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires and expires < now:
        raise HTTPException(400, "Token expired")

    try:
        user_oid = ObjectId(record["userId"])
    except (Exception,):
        raise HTTPException(400, "Invalid token payload")

    await db.users.update_one(
        {"_id": user_oid},
        {
            "$set": {
                "password": hash_password(data.newPassword),
                "updatedAt": now,
            }
        },
    )

    await db.password_reset_tokens.update_one(
        {"_id": record["_id"]},
        {"$set": {"used": True, "usedAt": now}},
    )

    return {"message": "Password reset successful"}