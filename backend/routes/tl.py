from fastapi import APIRouter, Depends, HTTPException

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone

from config import COMPANY_NAME
from database import db
from utils.dependencies import get_current_user_doc
from utils.email import send_notification_email
from utils.push import push_to_user
from utils.notify import create_notification, notify_user
from models.task import TaskCreate, TaskUpdate

router = APIRouter()


# ================= HELPERS =================
def _serialize_team(t: dict) -> dict:
    return {
        "id": str(t["_id"]),
        "name": t.get("name"),
        "teamLeadId": t.get("teamLeadId"),
        "memberIds": t.get("memberIds", []),
    }


async def _build_user_map(user_ids) -> dict:
    """Returns {userId(str): {id, name, email}} for the given ids.
    Skips ids that aren't valid ObjectIds or aren't found."""

    unique_ids = {uid for uid in user_ids if uid}

    if not unique_ids:
        return {}

    oids = []

    for uid in unique_ids:
        try:
            oids.append(ObjectId(uid))
        except (InvalidId, TypeError):
            continue

    if not oids:
        return {}

    result = {}

    async for u in db.users.find(
        {"_id": {"$in": oids}}
    ):
        result[str(u["_id"])] = {
            "id": str(u["_id"]),
            "name": u.get("name"),
            "email": u.get("email"),
        }

    return result


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
        "reminderIntervalMinutes": t.get(
            "reminderIntervalMinutes"
        ),
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


async def _ensure_tl_of_team(
    team_id: str,
    user_id: str,
) -> dict:
    try:
        oid = ObjectId(team_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid team id")

    team = await db.teams.find_one({"_id": oid})

    if not team:
        raise HTTPException(404, "Team not found")

    if team.get("teamLeadId") != user_id:
        raise HTTPException(
            403,
            "Only the team lead can perform this action",
        )

    return team


async def _ensure_tl_of_task(
    task_id: str,
    user_id: str,
) -> tuple[dict, dict]:
    try:
        oid = ObjectId(task_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid task id")

    task = await db.tasks.find_one({"_id": oid})

    if not task:
        raise HTTPException(404, "Task not found")

    try:
        team_oid = ObjectId(task["teamId"])
    except (InvalidId, TypeError, KeyError):
        raise HTTPException(500, "Task has invalid team reference")

    team = await db.teams.find_one({"_id": team_oid})

    if not team or team.get("teamLeadId") != user_id:
        raise HTTPException(
            403,
            "Only the team lead can perform this action",
        )

    return task, team


def _is_team_member(team: dict, user_id: str) -> bool:
    return (
        user_id in team.get("memberIds", [])
        or team.get("teamLeadId") == user_id
    )


# ================= MY LED TEAMS =================
@router.get("/teams/mine")
async def my_led_teams(
    user: dict = Depends(get_current_user_doc),
):

    user_id = str(user["_id"])

    teams_raw = []

    async for t in db.teams.find(
        {"teamLeadId": user_id}
    ).sort("name", 1):
        teams_raw.append(t)

    # Batch-fetch every referenced user across all teams in one query.
    all_user_ids = set()

    for t in teams_raw:
        if t.get("teamLeadId"):
            all_user_ids.add(t["teamLeadId"])
        all_user_ids.update(t.get("memberIds", []))

    user_map = await _build_user_map(all_user_ids)

    teams = []

    for t in teams_raw:
        serialized = _serialize_team(t)
        serialized["members"] = [
            user_map[mid]
            for mid in t.get("memberIds", [])
            if mid in user_map
        ]
        serialized["teamLead"] = user_map.get(
            t.get("teamLeadId")
        )
        teams.append(serialized)

    return teams


# ================= CREATE TASK =================
@router.post("/teams/{teamId}/tasks")
async def create_task(
    teamId: str,
    data: TaskCreate,
    user: dict = Depends(get_current_user_doc),
):

    user_id = str(user["_id"])

    team = await _ensure_tl_of_team(teamId, user_id)

    if not _is_team_member(team, data.assigneeId):
        raise HTTPException(
            400,
            "Assignee is not a member of this team",
        )

    now = datetime.now(timezone.utc)

    task = {
        "teamId": teamId,
        "title": data.title,
        "description": data.description or "",
        "assigneeId": data.assigneeId,
        "createdBy": user_id,
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
                "teamId": teamId,
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
            "teamId": teamId,
            "priority": data.priority or "MEDIUM",
        },
    )

    try:
        assignee = await db.users.find_one({"_id": ObjectId(data.assigneeId)})
    except (InvalidId, TypeError):
        assignee = None
    if assignee and assignee.get("email"):
        due_line = f"\nDue: {data.dueDate}\n" if data.dueDate else ""
        desc_line = (
            f"\nDescription:\n{data.description}\n"
            if data.description else ""
        )
        await send_notification_email(
            assignee["email"],
            f"New task: {data.title}",
            (
                f"Hi {assignee.get('name', 'there')},\n\n"
                f"{user.get('name', 'Your TL')} assigned you a new task in "
                f"team \"{team.get('name', '')}\":\n\n"
                f"Title: {data.title}\n"
                + due_line
                + desc_line
                + f"\nOpen the app to view and respond.\n"
                + f"\nRegards,\n{COMPANY_NAME}"
            ),
        )

    return {
        "id": str(result.inserted_id),
        "message": "Task created",
    }


# ================= LIST TEAM TASKS =================
@router.get("/teams/{teamId}/tasks")
async def list_team_tasks(
    teamId: str,
    user: dict = Depends(get_current_user_doc),
):

    user_id = str(user["_id"])

    await _ensure_tl_of_team(teamId, user_id)

    tasks_raw = []

    async for t in db.tasks.find(
        {"teamId": teamId}
    ).sort("createdAt", -1):
        tasks_raw.append(t)

    assignee_ids = {
        t.get("assigneeId") for t in tasks_raw
    }

    user_map = await _build_user_map(assignee_ids)

    tasks = []

    for t in tasks_raw:
        serialized = _serialize_task(t)
        serialized["assignee"] = user_map.get(
            t.get("assigneeId")
        )
        tasks.append(serialized)

    return tasks


# ================= UPDATE TASK =================
@router.put("/tasks/{id}")
async def update_task(
    id: str,
    data: TaskUpdate,
    user: dict = Depends(get_current_user_doc),
):

    user_id = str(user["_id"])

    task, team = await _ensure_tl_of_task(id, user_id)

    update: dict = {
        "updatedAt": datetime.now(timezone.utc),
    }

    if data.title is not None:
        update["title"] = data.title

    if data.description is not None:
        update["description"] = data.description

    if data.assigneeId is not None:
        if not _is_team_member(team, data.assigneeId):
            raise HTTPException(
                400,
                "Assignee is not a member of this team",
            )
        update["assigneeId"] = data.assigneeId

    if data.reminderIntervalMinutes is not None:
        update["reminderIntervalMinutes"] = (
            data.reminderIntervalMinutes
        )

    if data.dueDate is not None:
        update["dueDate"] = data.dueDate

    if data.priority is not None:
        update["priority"] = data.priority

    if data.attachments is not None:
        update["attachments"] = data.attachments

    await db.tasks.update_one(
        {"_id": ObjectId(id)},
        {"$set": update},
    )

    # Notify the new assignee on reassignment.
    new_assignee = update.get("assigneeId")
    if new_assignee and new_assignee != task.get("assigneeId"):
        await notify_user(
            new_assignee,
            "task_assigned",
            "Task assigned to you",
            update.get("title") or task.get("title", ""),
            {"taskId": id, "teamId": str(team["_id"])},
        )

    return {"message": "Task updated"}


# ================= DELETE TASK =================
@router.delete("/tasks/{id}")
async def delete_task(
    id: str,
    user: dict = Depends(get_current_user_doc),
):

    user_id = str(user["_id"])

    await _ensure_tl_of_task(id, user_id)

    await db.tasks.delete_one({"_id": ObjectId(id)})

    return {"message": "Task deleted"}
