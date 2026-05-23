from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timedelta, timezone

from secrets import token_urlsafe

import re
from typing import Optional

from passlib.context import CryptContext

from config import (
    COMPANY_NAME,
    PASSWORD_RESET_TTL_HOURS,
    PASSWORD_RESET_URL_TEMPLATE,
    is_email_configured,
)
from database import db
from utils.dependencies import require_hr, require_hr_or_ceo
from utils.email import send_notification_email
from utils.audit import log_audit
from models.user import HRCreateUser, HRUserUpdate
from models.team import TeamCreate, TeamUpdate

router = APIRouter()

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
)


# ================= HELPERS =================
def _serialize_user(u: dict) -> dict:
    """Full user view for HR. Legacy users (no Phase A fields) still render
    cleanly — missing sub-objects return as null, not 500."""
    return {
        "id": str(u["_id"]),
        "name": u.get("name"),
        "email": u.get("email"),
        "role": u.get("role", "USER"),
        # Header
        "tag": u.get("tag", "Employee"),
        "employeeCode": u.get("employeeCode"),
        "workPhone": u.get("workPhone"),
        "joiningDate": u.get("joiningDate"),
        "status": u.get("status", "Active"),
        "profilePictureUrl": u.get("profilePictureUrl"),
        # Org structure
        "departmentId": u.get("departmentId"),
        "reportingManagerId": u.get("reportingManagerId"),
        "projectManagerIds": u.get("projectManagerIds", []),
        # Profile tabs
        "work": u.get("work"),
        "personal": u.get("personal"),
        "bankAccounts": u.get("bankAccounts", []),
        "emergencyContact": u.get("emergencyContact"),
        "documents": u.get("documents"),
        "statutory": u.get("statutory"),
        "contract": u.get("contract"),
        # Termination metadata — populated only when status=Terminated.
        "terminationReason": u.get("terminationReason"),
        "terminatedAt": (
            u["terminatedAt"].isoformat()
            if u.get("terminatedAt") else None
        ),
        "terminatedBy": u.get("terminatedBy"),
    }


async def _validate_department(department_id: Optional[str]) -> None:
    if not department_id:
        return
    try:
        oid = ObjectId(department_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid departmentId")
    if not await db.departments.find_one({"_id": oid}):
        raise HTTPException(400, "departmentId references a non-existent department")


async def _validate_manager_ref(
    user_id: Optional[str],
    field_name: str,
    *,
    require_manager_role: bool = False,
) -> None:
    """Validates a referenced user exists. When require_manager_role=True,
    also enforces role=MANAGER (used for reportingManagerId)."""
    if not user_id:
        return
    try:
        oid = ObjectId(user_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, f"Invalid {field_name}")
    target = await db.users.find_one({"_id": oid})
    if not target:
        raise HTTPException(400, f"{field_name} references a non-existent user")
    if require_manager_role and target.get("role") not in ("MANAGER", "HR"):
        raise HTTPException(
            400,
            f"{field_name} must reference a user with role MANAGER (or HR)",
        )


async def _ensure_employee_code_free(
    code: Optional[str],
    exclude_user_id: Optional[ObjectId] = None,
) -> None:
    """Friendly upfront check before relying on the unique index."""
    if not code:
        return

    query: dict = {"employeeCode": code}
    if exclude_user_id is not None:
        query["_id"] = {"$ne": exclude_user_id}

    existing = await db.users.find_one(query)

    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Employee code '{code}' is already in use",
        )


_CODE_PATTERN = re.compile(r"^(?P<prefix>.*?)(?P<num>\d+)$")


async def _generate_next_employee_code() -> Optional[str]:
    """Find the highest existing employeeCode that ends in digits and bump
    it by 1, preserving the prefix and zero-padding width. HR seeds the
    first one manually (e.g. EMP-0001); subsequent creates can omit it.

    Returns None if no codes have ever been issued — caller must require
    HR to provide the first one.
    """
    latest_prefix: Optional[str] = None
    latest_num: int = -1
    latest_width: int = 0

    cursor = db.users.find(
        {"employeeCode": {"$exists": True, "$ne": None}},
        {"employeeCode": 1},
    )
    async for u in cursor:
        code = u.get("employeeCode")
        if not isinstance(code, str):
            continue
        m = _CODE_PATTERN.match(code)
        if not m:
            continue
        num = int(m.group("num"))
        if num > latest_num:
            latest_num = num
            latest_prefix = m.group("prefix")
            latest_width = len(m.group("num"))

    if latest_prefix is None:
        return None

    next_num = latest_num + 1
    return f"{latest_prefix}{str(next_num).zfill(latest_width)}"


def _serialize_team(t: dict) -> dict:
    return {
        "id": str(t["_id"]),
        "name": t.get("name"),
        "teamLeadId": t.get("teamLeadId"),
        "memberIds": t.get("memberIds", []),
    }


async def _validate_user_ids(ids: list[str]) -> None:
    """Raises 400 if any id is malformed or doesn't exist."""
    oids = []
    for uid in ids:
        try:
            oids.append(ObjectId(uid))
        except (InvalidId, TypeError):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid user id: {uid}",
            )

    if not oids:
        return

    found = await db.users.count_documents(
        {"_id": {"$in": oids}}
    )

    if found != len(oids):
        raise HTTPException(
            status_code=400,
            detail="One or more user ids do not exist",
        )


# ================= USERS =================
@router.post("/users")
async def create_user(
    data: HRCreateUser,
    hr: dict = Depends(require_hr),
):

    existing = await db.users.find_one({
        "email": data.email
    })

    if existing:
        raise HTTPException(
            status_code=400,
            detail="Email already exists",
        )

    # If HR omitted the code, derive the next one from the most recent
    # existing code. First-ever user must be created with an explicit
    # code (we need a prefix + width to extrapolate from).
    if not data.employeeCode:
        generated = await _generate_next_employee_code()
        if not generated:
            raise HTTPException(
                400,
                "Employee code is required for the first employee. "
                "Subsequent employees will auto-increment.",
            )
        data.employeeCode = generated

    await _ensure_employee_code_free(data.employeeCode)

    # HR can create any role (USER / MANAGER / HR). Caller is already
    # HR via the require_hr dependency on this route, so HR-creating-HR
    # is allowed by product spec.
    requested_role = data.role or "USER"
    if requested_role not in ("USER", "MANAGER", "HR"):
        raise HTTPException(
            400,
            "role must be USER, MANAGER, or HR",
        )

    # Validate org-structure references before insert.
    await _validate_department(data.departmentId)
    await _validate_manager_ref(
        data.reportingManagerId,
        "reportingManagerId",
        require_manager_role=True,
    )
    if data.projectManagerIds:
        for pmid in data.projectManagerIds:
            await _validate_manager_ref(pmid, "projectManagerIds[item]")

    now = datetime.now(timezone.utc)

    user = {
        "name": data.name,
        "email": data.email,
        "password": pwd_context.hash(data.password),
        "role": requested_role,
        "tag": data.tag or "Employee",
        "status": data.status or "Active",
        "createdAt": now,
        "updatedAt": now,
    }

    # Only persist optional fields when set (keeps documents tidy and
    # avoids the sparse index recording None values).
    if data.employeeCode:
        user["employeeCode"] = data.employeeCode

    if data.workPhone:
        user["workPhone"] = data.workPhone

    if data.joiningDate:
        user["joiningDate"] = data.joiningDate

    if data.profilePictureUrl:
        user["profilePictureUrl"] = data.profilePictureUrl

    if data.departmentId:
        user["departmentId"] = data.departmentId
    if data.reportingManagerId:
        user["reportingManagerId"] = data.reportingManagerId
    if data.projectManagerIds:
        user["projectManagerIds"] = data.projectManagerIds

    # Nested profile tabs — store the dict if any field was provided.
    for sub_field in (
        "work", "personal", "bankAccounts",
        "emergencyContact", "documents", "statutory", "contract",
    ):
        sub_value = getattr(data, sub_field)
        if sub_value is not None:
            user[sub_field] = (
                sub_value
                if isinstance(sub_value, list)
                else sub_value.model_dump(exclude_none=True)
            )

    result = await db.users.insert_one(user)
    new_user_id = str(result.inserted_id)

    # Seed leave balances so the new hire opens the app and sees their
    # quota — best-effort; a failure here must not roll back user creation.
    try:
        from routes.leave import _seed_balances_for_user
        await _seed_balances_for_user(
            new_user_id,
            datetime.now().year,
            now,
        )
    except Exception as e:
        print(f"[hr.create_user] seed balances failed: {e}")

    # Assign any initialAssetIds the HR ticked in the create modal. We
    # validate after insert so a partial asset failure doesn't kill the
    # user record — the user is created, problematic asset IDs come back
    # in the response so HR can fix them.
    asset_assignment_errors: list[dict] = []
    asset_assigned_ids: list[str] = []
    if data.initialAssetIds:
        for asset_id in data.initialAssetIds:
            try:
                asset_oid = ObjectId(asset_id)
            except (InvalidId, TypeError):
                asset_assignment_errors.append(
                    {"id": asset_id, "error": "invalid id"}
                )
                continue
            asset = await db.assets.find_one({"_id": asset_oid})
            if not asset:
                asset_assignment_errors.append(
                    {"id": asset_id, "error": "not found"}
                )
                continue
            if asset.get("status") != "AVAILABLE":
                asset_assignment_errors.append({
                    "id": asset_id,
                    "error": f"status is {asset.get('status')}",
                })
                continue
            await db.assets.update_one(
                {"_id": asset_oid, "status": "AVAILABLE"},
                {
                    "$set": {
                        "status": "ASSIGNED",
                        "assignedToUserId": new_user_id,
                        "assignedAt": now,
                        "updatedAt": now,
                    }
                },
            )
            asset_assigned_ids.append(asset_id)

    await _send_welcome_email(result.inserted_id, data.name, data.email)

    await log_audit(
        actor_id=str(hr["_id"]),
        action="user.create",
        entity_type="users",
        entity_id=str(result.inserted_id),
        after={
            "email": data.email,
            "role": requested_role,
            "departmentId": data.departmentId,
            "reportingManagerId": data.reportingManagerId,
        },
    )

    response: dict = {
        "id": new_user_id,
        "message": "User created",
        "employeeCode": data.employeeCode,
    }
    if data.initialAssetIds is not None:
        response["assignedAssetIds"] = asset_assigned_ids
        if asset_assignment_errors:
            response["assetErrors"] = asset_assignment_errors
    return response


async def _send_welcome_email(
    user_oid: ObjectId,
    name: str,
    to_email: str,
) -> None:
    """Issues a one-time password-setup token and emails a welcome note.

    Why a setup link instead of mailing the password HR typed: plaintext
    creds in an inbox stay compromised forever and SMTP TLS isn't
    guaranteed end-to-end. The link reuses the same password_reset_tokens
    collection that /auth/forgot-password uses, so /auth/reset-password
    accepts it as-is.
    """
    token = token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=PASSWORD_RESET_TTL_HOURS)

    await db.password_reset_tokens.insert_one({
        "userId": str(user_oid),
        "token": token,
        "expiresAt": expires,
        "used": False,
        "createdAt": now,
    })

    if PASSWORD_RESET_URL_TEMPLATE and "{token}" in PASSWORD_RESET_URL_TEMPLATE:
        link = PASSWORD_RESET_URL_TEMPLATE.replace("{token}", token)
        link_line = f"\n\nSet your password here:\n{link}\n"
    else:
        link_line = (
            f"\n\nSetup token: {token}\n"
            "Open the app's password reset screen and paste this token "
            "to set your password.\n"
        )

    body = (
        f"Hi {name},\n\n"
        f"An account has been created for you on {COMPANY_NAME}.\n\n"
        f"Login email: {to_email}"
        + link_line
        + f"\nThis link/token expires in {PASSWORD_RESET_TTL_HOURS} hour(s). "
        "If it expires, ask HR to resend it or use the 'Forgot password' "
        "flow on the login screen.\n\n"
        f"Welcome aboard,\n{COMPANY_NAME}"
    )

    await send_notification_email(to_email, f"Welcome to {COMPANY_NAME}", body)


@router.get("/users")
async def list_users(
    search: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr_or_ceo),
):

    query: dict = {}

    if search:
        regex = {"$regex": search, "$options": "i"}
        query = {
            "$or": [
                {"name": regex},
                {"email": regex},
                {"employeeCode": regex},
            ]
        }

    users = []

    async for u in db.users.find(query).sort("name", 1):
        users.append(_serialize_user(u))

    return users


@router.get("/users/{id}")
async def get_user(
    id: str,
    _hr: dict = Depends(require_hr_or_ceo),
):

    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    u = await db.users.find_one({"_id": oid})

    if not u:
        raise HTTPException(404, "User not found")

    return _serialize_user(u)


@router.put("/users/{id}")
async def update_user(
    id: str,
    data: HRUserUpdate,
    hr: dict = Depends(require_hr),
):

    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    existing = await db.users.find_one({"_id": oid})
    if not existing:
        raise HTTPException(404, "User not found")

    if data.employeeCode is not None:
        await _ensure_employee_code_free(
            data.employeeCode,
            exclude_user_id=oid,
        )

    # Validate org-structure references when changing them.
    if data.departmentId not in (None, ""):
        await _validate_department(data.departmentId)
    if data.reportingManagerId not in (None, ""):
        await _validate_manager_ref(
            data.reportingManagerId,
            "reportingManagerId",
            require_manager_role=True,
        )
    if data.projectManagerIds:
        for pmid in data.projectManagerIds:
            await _validate_manager_ref(pmid, "projectManagerIds[item]")

    update: dict = {
        "updatedAt": datetime.now(timezone.utc),
    }

    # Only set provided fields. Sending an empty string clears optional
    # fields (workPhone, departmentId, etc.); use null/omit to leave them
    # untouched.
    scalar_fields = (
        "name",
        "role",
        "tag",
        "employeeCode",
        "workPhone",
        "joiningDate",
        "status",
        "profilePictureUrl",
        "departmentId",
        "reportingManagerId",
    )
    for field in scalar_fields:
        value = getattr(data, field)
        if value is not None:
            update[field] = value if value != "" else None

    if data.projectManagerIds is not None:
        update["projectManagerIds"] = data.projectManagerIds

    # Nested profile tabs — replace the whole sub-doc when provided.
    nested_fields = (
        "work", "personal", "emergencyContact",
        "documents", "statutory", "contract",
    )
    for sub_field in nested_fields:
        sub_value = getattr(data, sub_field)
        if sub_value is not None:
            update[sub_field] = sub_value.model_dump(exclude_none=True)

    if data.bankAccounts is not None:
        update["bankAccounts"] = [
            b.model_dump(exclude_none=True) for b in data.bankAccounts
        ]

    # Termination metadata — when status flips to Terminated, stamp the
    # reason + who/when so audit and reporting can attribute it.
    if (
        data.status == "Terminated"
        and existing.get("status") != "Terminated"
    ):
        update["terminationReason"] = (data.terminationReason or "").strip() or None
        update["terminatedAt"] = datetime.now(timezone.utc)
        update["terminatedBy"] = str(hr["_id"])
    elif data.status == "Active" and existing.get("status") == "Terminated":
        # Reactivation — clear the termination metadata so the next
        # termination starts fresh and reports show the correct state.
        update["terminationReason"] = None
        update["terminatedAt"] = None
        update["terminatedBy"] = None

    result = await db.users.update_one(
        {"_id": oid},
        {"$set": update},
    )

    if result.matched_count == 0:
        raise HTTPException(404, "User not found")

    # Audit role changes specifically (separate action for easier filtering).
    if data.role is not None and data.role != existing.get("role"):
        await log_audit(
            actor_id=str(hr["_id"]),
            action="role.change",
            entity_type="users",
            entity_id=id,
            before={"role": existing.get("role")},
            after={"role": data.role},
        )

    await log_audit(
        actor_id=str(hr["_id"]),
        action="user.update",
        entity_type="users",
        entity_id=id,
        after={k: v for k, v in update.items() if k != "updatedAt"},
    )

    return {"message": "User updated"}


# ================= TEAMS =================
@router.post("/teams")
async def create_team(
    data: TeamCreate,
    hr: dict = Depends(require_hr),
):

    await _validate_user_ids(
        [data.teamLeadId] + data.memberIds
    )

    now = datetime.now(timezone.utc)

    team = {
        "name": data.name,
        "teamLeadId": data.teamLeadId,
        "memberIds": data.memberIds,
        "createdBy": str(hr["_id"]),
        "createdAt": now,
        "updatedAt": now,
    }

    result = await db.teams.insert_one(team)

    return {
        "id": str(result.inserted_id),
        "message": "Team created",
    }


@router.get("/teams")
async def list_teams(
    _hr: dict = Depends(require_hr),
):

    teams = []

    async for t in db.teams.find().sort("name", 1):
        teams.append(_serialize_team(t))

    return teams


@router.get("/teams/{id}")
async def get_team(
    id: str,
    _hr: dict = Depends(require_hr),
):

    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(
            status_code=400,
            detail="Invalid id",
        )

    t = await db.teams.find_one({"_id": oid})

    if not t:
        raise HTTPException(
            status_code=404,
            detail="Team not found",
        )

    serialized = _serialize_team(t)

    # Expand the team lead into a {id, name, email} object so the UI can
    # render the lead's profile in the header (memberIds doesn't include
    # the lead, and slicing the id was the visible "User" bug).
    lead_id = t.get("teamLeadId")
    if lead_id:
        try:
            lead_user = await db.users.find_one(
                {"_id": ObjectId(lead_id)}
            )
        except (InvalidId, TypeError):
            lead_user = None
        if lead_user:
            serialized["teamLead"] = {
                "id": str(lead_user["_id"]),
                "name": lead_user.get("name"),
                "email": lead_user.get("email"),
            }
            serialized["leadName"] = lead_user.get("name")

    return serialized


@router.put("/teams/{id}")
async def update_team(
    id: str,
    data: TeamUpdate,
    _hr: dict = Depends(require_hr),
):

    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(
            status_code=400,
            detail="Invalid id",
        )

    update: dict = {
        "updatedAt": datetime.now(timezone.utc),
    }

    if data.name is not None:
        update["name"] = data.name

    if data.teamLeadId is not None:
        await _validate_user_ids([data.teamLeadId])
        update["teamLeadId"] = data.teamLeadId

    if data.memberIds is not None:
        await _validate_user_ids(data.memberIds)
        update["memberIds"] = data.memberIds

    result = await db.teams.update_one(
        {"_id": oid},
        {"$set": update},
    )

    if result.matched_count == 0:
        raise HTTPException(
            status_code=404,
            detail="Team not found",
        )

    return {"message": "Team updated"}


@router.delete("/teams/{id}")
async def delete_team(
    id: str,
    _hr: dict = Depends(require_hr),
):

    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(
            status_code=400,
            detail="Invalid id",
        )

    result = await db.teams.delete_one({"_id": oid})

    if result.deleted_count == 0:
        raise HTTPException(
            status_code=404,
            detail="Team not found",
        )

    return {"message": "Team deleted"}


# ================= EMAIL TEST =================
@router.post("/email/test")
async def email_test(
    hr: dict = Depends(require_hr),
):
    """Sends a test email to the caller's own address to verify SMTP."""
    if not is_email_configured():
        raise HTTPException(
            status_code=503,
            detail="Email is not configured (SMTP_HOST / SMTP_FROM missing)",
        )

    to_email = hr.get("email")
    if not to_email:
        raise HTTPException(
            status_code=400,
            detail="Your account has no email address on file",
        )

    now = datetime.now(timezone.utc)
    body = (
        f"Hi {hr.get('name', 'there')},\n\n"
        f"This is a test email from {COMPANY_NAME}.\n"
        f"If you received it, SMTP delivery is working.\n\n"
        f"Sent at: {now.isoformat()}\n"
    )

    sent = await send_notification_email(
        to_email,
        f"{COMPANY_NAME} SMTP test",
        body,
    )

    if not sent:
        raise HTTPException(
            status_code=502,
            detail="SMTP delivery failed — check backend logs",
        )

    return {"message": f"Test email sent to {to_email}"}
