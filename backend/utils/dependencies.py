from fastapi import (
    Depends,
    HTTPException,
    status,
)

from fastapi.security import (
    HTTPBearer,
    HTTPAuthorizationCredentials,
)

from jose import (
    jwt,
    JWTError,
)

from bson import ObjectId
from bson.errors import InvalidId

from database import db

# ================= JWT CONFIG =================
from config import SECRET_KEY, ALGORITHM

security = HTTPBearer()


# ================= GET CURRENT USER =================
async def get_current_user(

    credentials:
    HTTPAuthorizationCredentials = Depends(
        security
    )
):

    try:

        token = \
            credentials.credentials

        payload = jwt.decode(

            token,

            SECRET_KEY,

            algorithms=[ALGORITHM]
        )

        user_id = payload.get(
            "sub"
        )

        if not user_id:

            raise HTTPException(

                status_code=
                status.HTTP_401_UNAUTHORIZED,

                detail=
                "Invalid token"
            )

        return user_id

    except JWTError:

        raise HTTPException(

            status_code=
            status.HTTP_401_UNAUTHORIZED,

            detail=
            "Invalid token"
        )


# ================= LOAD FULL USER DOC =================
async def get_current_user_doc(
    user_id: str = Depends(get_current_user)
):

    try:
        oid = ObjectId(user_id)
    except (InvalidId, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

    user = await db.users.find_one({"_id": oid})

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )

    # Terminated users can't act on the API even if they hold a token
    # issued before termination. Force them back to /login.
    if user.get("status") == "Terminated":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is no longer active.",
        )

    return user


# ================= ROLE GUARD: HR =================
async def require_hr(
    user: dict = Depends(get_current_user_doc)
):

    if user.get("role") != "HR":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="HR access required"
        )

    return user


# ================= ROLE GUARD: CEO =================
async def require_ceo(
    user: dict = Depends(get_current_user_doc),
):
    """CEO-only — used for the few CEO-specific endpoints (e.g. global
    override). For shared read access prefer require_hr_or_ceo."""
    if user.get("role") != "CEO":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CEO access required",
        )
    return user


# ================= ROLE GUARD: HR OR CEO =================
async def require_hr_or_ceo(
    user: dict = Depends(get_current_user_doc),
):
    """Read-only shared access — HR retains write paths, CEO gets the
    same GETs (dashboards, reports, exports, payslip viewing).

    Endpoints that mutate state must keep using require_hr; CEO does not
    create/update/approve via these routes."""
    if user.get("role") not in ("HR", "CEO"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="HR or CEO access required",
        )
    return user


# ================= ROLE GUARD: MANAGER OR HR =================
async def require_manager_or_hr(
    user: dict = Depends(get_current_user_doc),
):
    """Allows users with role MANAGER or HR.

    Why: PRD wording — leave/correction/expense approvals are
    "Employee → Manager OR HR → Approved/Rejected". A Manager can act on
    their direct reports; HR can act on anyone. Scope filtering (only
    *my* reports) is enforced inside the endpoint, not here.
    """
    if user.get("role") not in ("HR", "MANAGER"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manager or HR access required",
        )

    return user


def can_decide_for_employee(actor: dict, employee: dict) -> bool:
    """True if actor (HR or that employee's reporting manager) may
    approve/reject requests raised by the given employee."""
    if actor.get("role") == "HR":
        return True

    if actor.get("role") != "MANAGER":
        return False

    return employee.get("reportingManagerId") == str(actor["_id"])