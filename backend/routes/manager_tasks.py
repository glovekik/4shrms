"""Manager-scoped team + task endpoints.

A manager's "team" is defined as users whose `reportingManagerId` points
to the manager. This is distinct from the Team-Lead model in /tl, which
operates on the explicit `teams` collection. Both can coexist.

Tasks created here live in the same `tasks` collection but with no
`teamId` (or teamId=None) — the manager scope is enforced via assignee
direct-report check, not via team membership.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from database import db
from utils.dependencies import get_current_user_doc
from utils.notify import create_notification
from utils.push import push_to_user
from models.task import TaskCreate, TaskUpdate


router = APIRouter()


def _require_manager(user: dict) -> None:
    if user.get("role") not in ("MANAGER", "HR"):
        raise HTTPException(403, "Manager or HR access required")


async def _direct_report_ids(manager_id: str) -> set[str]:
    ids: set[str] = set()
    async for u in db.users.find(
        {"reportingManagerId": manager_id},
        {"_id": 1},
    ):
        ids.add(str(u["_id"]))
    return ids


def _serialize_task(t: dict) -> dict:
    return {
        "id": str(t["_id"]),
        "teamId": t.get("teamId"),
        "title": t.get("title"),
        "description": t.get("description", ""),
        "assigneeId": t.get("assigneeId"),
        "createdBy": t.get("createdBy"),
        "status": t.get("status"),
        "priority": t.get("priority", "MEDIUM"),
        "reminderIntervalMinutes": t.get("reminderIntervalMinutes"),
        "dueDate": t.get("dueDate"),
        "attachments": t.get("attachments", []),
        "createdAt": (
            t["createdAt"].isoformat()
            if t.get("createdAt") else None
        ),
        "startedAt": (
            t["startedAt"].isoformat()
            if t.get("startedAt") else None
        ),
        "completedAt": (
            t["completedAt"].isoformat()
            if t.get("completedAt") else None
        ),
    }


# ================= MY TEAM =================
@router.get("/team")
async def my_team(
    user: dict = Depends(get_current_user_doc),
):
    """Returns the manager's direct reports as User-shaped objects."""
    _require_manager(user)
    actor_id = str(user["_id"])

    out: list[dict] = []
    async for u in db.users.find(
        {"reportingManagerId": actor_id}
    ).sort("name", 1):
        out.append({
            "id": str(u["_id"]),
            "name": u.get("name"),
            "email": u.get("email"),
            "employeeCode": u.get("employeeCode"),
            "role": u.get("role"),
            "tag": u.get("tag"),
            "status": u.get("status"),
            "departmentId": u.get("departmentId"),
            "profilePictureUrl": u.get("profilePictureUrl"),
        })
    return out


# ================= TEAM TASKS =================
@router.post("/tasks")
async def create_team_task(
    data: TaskCreate,
    user: dict = Depends(get_current_user_doc),
):
    """Manager assigns a task to one of their direct reports."""
    _require_manager(user)
    actor_id = str(user["_id"])

    if not data.assigneeId:
        raise HTTPException(400, "assigneeId is required")

    # HR can assign to anyone; manager only to direct reports.
    if user.get("role") == "MANAGER":
        report_ids = await _direct_report_ids(actor_id)
        if data.assigneeId not in report_ids:
            raise HTTPException(
                400,
                "Assignee is not one of your direct reports",
            )

    now = datetime.now(timezone.utc)
    task = {
        # No teamId for manager-issued tasks — scope is reporter-based.
        "teamId": None,
        "title": data.title,
        "description": data.description or "",
        "assigneeId": data.assigneeId,
        "createdBy": actor_id,
        "createdByRole": user.get("role"),
        "status": "PENDING",
        "priority": data.priority or "MEDIUM",
        "reminderIntervalMinutes": data.reminderIntervalMinutes,
        "dueDate": data.dueDate,
        "attachments": data.attachments or [],
        "createdAt": now,
        "updatedAt": now,
        "startedAt": None,
        "completedAt": None,
    }
    result = await db.tasks.insert_one(task)

    try:
        await push_to_user(
            data.assigneeId,
            "New task",
            data.title,
            {
                "type": "task_assigned",
                "taskId": str(result.inserted_id),
            },
        )
    except Exception:
        pass

    await create_notification(
        data.assigneeId,
        "task_assigned",
        "New task",
        data.title,
        {
            "taskId": str(result.inserted_id),
            "priority": data.priority or "MEDIUM",
        },
    )

    return {"id": str(result.inserted_id), "message": "Task created"}


@router.get("/tasks")
async def list_team_tasks(
    status: Optional[str] = Query(None),       # PENDING | ONGOING | COMPLETED
    assigneeId: Optional[str] = Query(None),
    user: dict = Depends(get_current_user_doc),
):
    """Lists all tasks the manager has created OR whose assignee reports
    to them. HR sees every task. Filters by status / assignee."""
    _require_manager(user)
    actor_id = str(user["_id"])

    query: dict = {}
    if status:
        query["status"] = status

    if user.get("role") == "MANAGER":
        report_ids = await _direct_report_ids(actor_id)
        # Either the manager created it OR it was assigned to a report.
        # Covers "show me everything I have visibility on".
        query["$or"] = [
            {"createdBy": actor_id},
            {"assigneeId": {"$in": list(report_ids)}},
        ]

    if assigneeId:
        query["assigneeId"] = assigneeId

    raw: list[dict] = []
    async for t in db.tasks.find(query).sort("createdAt", -1):
        raw.append(t)

    # Enrich each task with the assignee's basic info.
    assignee_ids = {t.get("assigneeId") for t in raw if t.get("assigneeId")}
    oids = []
    for aid in assignee_ids:
        try:
            oids.append(ObjectId(aid))
        except (InvalidId, TypeError):
            continue

    user_map: dict[str, dict] = {}
    if oids:
        async for u in db.users.find({"_id": {"$in": oids}}):
            user_map[str(u["_id"])] = {
                "id": str(u["_id"]),
                "name": u.get("name"),
                "email": u.get("email"),
            }

    out: list[dict] = []
    for t in raw:
        serialized = _serialize_task(t)
        serialized["assignee"] = user_map.get(t.get("assigneeId"))
        out.append(serialized)
    return out


@router.put("/tasks/{id}")
async def update_team_task(
    id: str,
    data: TaskUpdate,
    user: dict = Depends(get_current_user_doc),
):
    _require_manager(user)
    actor_id = str(user["_id"])

    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    task = await db.tasks.find_one({"_id": oid})
    if not task:
        raise HTTPException(404, "Task not found")

    if user.get("role") == "MANAGER":
        report_ids = await _direct_report_ids(actor_id)
        if (
            task.get("createdBy") != actor_id
            and task.get("assigneeId") not in report_ids
        ):
            raise HTTPException(403, "Not your task")

    update: dict = {"updatedAt": datetime.now(timezone.utc)}
    if data.title is not None:
        update["title"] = data.title
    if data.description is not None:
        update["description"] = data.description
    if data.assigneeId is not None:
        if user.get("role") == "MANAGER":
            report_ids = await _direct_report_ids(actor_id)
            if data.assigneeId not in report_ids:
                raise HTTPException(
                    400, "Assignee is not one of your direct reports",
                )
        update["assigneeId"] = data.assigneeId
    if data.priority is not None:
        update["priority"] = data.priority
    if data.dueDate is not None:
        update["dueDate"] = data.dueDate
    if data.reminderIntervalMinutes is not None:
        update["reminderIntervalMinutes"] = data.reminderIntervalMinutes
    if data.attachments is not None:
        update["attachments"] = data.attachments

    await db.tasks.update_one({"_id": oid}, {"$set": update})
    return {"message": "Task updated"}


@router.delete("/tasks/{id}")
async def delete_team_task(
    id: str,
    user: dict = Depends(get_current_user_doc),
):
    _require_manager(user)
    actor_id = str(user["_id"])

    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    task = await db.tasks.find_one({"_id": oid})
    if not task:
        raise HTTPException(404, "Task not found")

    if user.get("role") == "MANAGER" and task.get("createdBy") != actor_id:
        raise HTTPException(403, "You can only delete tasks you created")

    await db.tasks.delete_one({"_id": oid})
    return {"message": "Task deleted"}


# ================= TEAM ATTENDANCE =================
@router.get("/attendance")
async def team_attendance(
    date: Optional[str] = Query(None),       # YYYY-MM-DD — single day
    month: Optional[str] = Query(None),      # YYYY-MM — whole month
    userId: Optional[str] = Query(None),     # one report
    user: dict = Depends(get_current_user_doc),
):
    """Attendance for the manager's direct reports. Same shape as the HR
    listing but scoped to reports only (HR sees all)."""
    _require_manager(user)
    actor_id = str(user["_id"])

    if user.get("role") == "MANAGER":
        report_ids = await _direct_report_ids(actor_id)
        if not report_ids:
            return []
        if userId and userId not in report_ids:
            raise HTTPException(403, "Not one of your direct reports")
        scope_ids = [userId] if userId else list(report_ids)
    else:
        # HR using the manager endpoint — show all if userId given, else
        # require a userId to avoid accidentally returning everyone via
        # the manager scope.
        scope_ids = [userId] if userId else None

    query: dict = {}
    if scope_ids is not None:
        query["userId"] = {"$in": scope_ids}

    if date:
        query["date"] = date
    elif month:
        if not (len(month) == 7 and month[4] == "-"):
            raise HTTPException(400, "Invalid month (YYYY-MM required)")
        query["date"] = {"$regex": f"^{month}-"}
    else:
        query["date"] = datetime.now().strftime("%Y-%m-%d")

    records: list[dict] = []
    async for r in db.attendance.find(query).sort("date", -1):
        records.append(r)

    unique_user_ids = {r.get("userId") for r in records if r.get("userId")}
    oids = []
    for uid in unique_user_ids:
        try:
            oids.append(ObjectId(uid))
        except (InvalidId, TypeError):
            continue

    user_map: dict[str, dict] = {}
    if oids:
        async for u in db.users.find({"_id": {"$in": oids}}):
            user_map[str(u["_id"])] = {
                "id": str(u["_id"]),
                "name": u.get("name"),
                "email": u.get("email"),
                "employeeCode": u.get("employeeCode"),
            }

    out: list[dict] = []
    for r in records:
        out.append({
            "id": str(r["_id"]),
            "userId": r.get("userId"),
            "user": user_map.get(r.get("userId")),
            "date": r.get("date"),
            "attendanceType": r.get("attendanceType"),
            "status": r.get("status"),
            "isLate": r.get("isLate", False),
            "hoursWorked": r.get("hoursWorked", 0.0),
            "overtimeHours": r.get("overtimeHours", 0.0),
            "checkIn": (
                r["checkIn"].isoformat() if r.get("checkIn") else None
            ),
            "checkOut": (
                r["checkOut"].isoformat() if r.get("checkOut") else None
            ),
            "workNotes": r.get("workNotes", ""),
        })

    return out


# ================= TEAM LEAVE BALANCES =================
@router.get("/leave-balances")
async def team_leave_balances(
    user: dict = Depends(get_current_user_doc),
):
    """Per-direct-report leave balance roll-up for the current year."""
    _require_manager(user)
    actor_id = str(user["_id"])

    if user.get("role") == "MANAGER":
        report_ids = await _direct_report_ids(actor_id)
    else:
        # HR can call this to see anyone's reports; if not theirs, return
        # empty rather than 403 — this endpoint is for the manager view.
        report_ids = await _direct_report_ids(actor_id)

    if not report_ids:
        return []

    year = datetime.now().year

    # Resolve user info in one round-trip.
    oids = []
    for uid in report_ids:
        try:
            oids.append(ObjectId(uid))
        except (InvalidId, TypeError):
            continue
    user_map: dict[str, dict] = {}
    async for u in db.users.find({"_id": {"$in": oids}}):
        user_map[str(u["_id"])] = {
            "id": str(u["_id"]),
            "name": u.get("name"),
            "email": u.get("email"),
            "employeeCode": u.get("employeeCode"),
        }

    # Pull balances + types in batches.
    balances: list[dict] = []
    async for b in db.leave_balances.find({
        "userId": {"$in": list(report_ids)},
        "year": year,
    }):
        balances.append(b)

    type_codes = {b.get("leaveTypeCode") for b in balances}
    type_map: dict[str, dict] = {}
    async for lt in db.leave_types.find({"code": {"$in": list(type_codes)}}):
        type_map[lt["code"]] = {
            "code": lt.get("code"),
            "name": lt.get("name"),
        }

    # Group by user.
    by_user: dict[str, list[dict]] = {uid: [] for uid in report_ids}
    for b in balances:
        uid = b.get("userId")
        if uid not in by_user:
            continue
        allocated = float(b.get("allocated", 0) or 0)
        used = float(b.get("used", 0) or 0)
        pending = float(b.get("pending", 0) or 0)
        remaining = allocated - used - pending
        by_user[uid].append({
            "code": b.get("leaveTypeCode"),
            "leaveType": type_map.get(b.get("leaveTypeCode")),
            "allocated": allocated,
            "used": used,
            "pending": pending,
            "remaining": remaining,
        })

    out: list[dict] = []
    for uid in report_ids:
        out.append({
            "user": user_map.get(uid, {"id": uid, "name": None, "email": None}),
            "balances": by_user.get(uid, []),
        })
    # Stable order by name so the UI list doesn't reshuffle.
    out.sort(key=lambda r: (r["user"].get("name") or "").lower())
    return out
