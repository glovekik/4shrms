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
    PASSWORD_RESET_TTL_HOURS,
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

    access_token = \
        create_access_token({

            "sub":
            str(user["_id"])
        })

    return {

        "access_token":
        access_token,

        "token_type":
        "bearer",
    }


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

    access_token = create_access_token({"sub": str(user["_id"])})
    return {"access_token": access_token, "token_type": "bearer"}


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
class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    newPassword: str


@router.post("/forgot-password")
async def forgot_password(data: ForgotPasswordRequest):
    """Always returns the same message regardless of whether the email
    exists, so attackers can't enumerate accounts."""
    generic = {
        "message": (
            "If that email is registered, a reset link has been sent."
        )
    }

    user = await db.users.find_one({"email": data.email})
    if not user:
        return generic

    # Don't try to send if email isn't configured — but still return
    # the generic response (don't leak that fact either).
    if not is_email_configured():
        return generic

    token = token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(
        hours=PASSWORD_RESET_TTL_HOURS
    )

    await db.password_reset_tokens.insert_one({
        "userId": str(user["_id"]),
        "token": token,
        "expiresAt": expires,
        "used": False,
        "createdAt": now,
    })

    # The app's reset screen asks the user to paste this token, so the
    # email must always carry it in plain, copyable form. (No URL link:
    # the mobile app has no fixed web reset page to deep-link to.)
    token_line = (
        f"\n\nReset token:\n{token}\n\n"
        'Open the app, tap "Forgot password" then "I have a token", '
        "and paste this token to choose a new password.\n"
    )

    body_text = (
        f"Hi {user.get('name', 'there')},\n\n"
        f"We received a password reset request for your "
        f"{COMPANY_NAME} account."
        + token_line
        + f"\nThis token expires in "
        f"{PASSWORD_RESET_TTL_HOURS} hour(s). "
        "If you didn't request this, ignore this email.\n\n"
        f"Regards,\n{COMPANY_NAME}"
    )

    # Inline plain-text send (we don't want a PDF attachment helper here).
    import asyncio
    import smtplib
    from email.mime.text import MIMEText
    from config import (
        SMTP_HOST, SMTP_PORT, SMTP_USERNAME,
        SMTP_PASSWORD, SMTP_FROM, SMTP_USE_TLS,
    )

    def _send_plain():
        msg = MIMEText(body_text, "plain")
        msg["Subject"] = "Password reset"
        msg["From"] = SMTP_FROM
        msg["To"] = user["email"]
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls()
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)

    try:
        await asyncio.to_thread(_send_plain)
    except Exception as e:
        # Log but still return generic — don't leak failure.
        print(f"[auth] password reset email failed: {e}")

    return generic


@router.post("/reset-password")
async def reset_password(data: ResetPasswordRequest):
    if len(data.newPassword) < 8:
        raise HTTPException(
            400,
            "Password must be at least 8 characters",
        )

    record = await db.password_reset_tokens.find_one({
        "token": data.token,
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