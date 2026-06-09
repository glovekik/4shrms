from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone

from typing import Optional

from config import COMPANY_NAME
from database import db
from utils.dependencies import (
    get_current_user,
    get_current_user_doc,
)
from utils.email import send_notification_email
from utils.push import push_to_user
from utils.notify import create_notification, notify_user
from models.comment import CommentCreate

router = APIRouter()


# ================= SERIALIZERS =================
def _serialize(t: dict) -> dict:
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
        "startedAt": (
            t["startedAt"].isoformat()
            if t.get("startedAt") else None
        ),
        "completedAt": (
            t["completedAt"].isoformat()
            if t.get("completedAt") else None
        ),
        "createdAt": (
            t["createdAt"].isoformat()
            if t.get("createdAt") else None
        ),
    }


def _serialize_comment(
    c: dict,
    user_info: Optional[dict] = None,
) -> dict:
    return {
        "id": str(c["_id"]),
        "taskId": c.get("taskId"),
        "userId": c.get("userId"),
        "user": user_info,
        "text": c.get("text", ""),
        "createdAt": (
            c["createdAt"].isoformat()
            if c.get("createdAt") else None
        ),
    }


# ================= HELPERS =================
async def _get_user_basics(user_ids) -> dict:
    """Returns {userId(str): {id, name, email}} for the given ids."""
    unique = {uid for uid in user_ids if uid}
    if not unique:
        return {}

    oids = []
    for uid in unique:
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


async def _load_task_or_404(task_id: str) -> dict:
    try:
        oid = ObjectId(task_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid task id")

    task = await db.tasks.find_one({"_id": oid})

    if not task:
        raise HTTPException(404, "Task not found")

    return task


async def _ensure_can_view(task: dict, user: dict) -> None:
    """View access: assignee OR team's TL OR HR."""
    user_id = str(user["_id"])

    if user.get("role") == "HR":
        return

    if task.get("assigneeId") == user_id:
        return

    try:
        team_oid = ObjectId(task["teamId"])
    except (InvalidId, TypeError, KeyError):
        raise HTTPException(
            500,
            "Task has invalid team reference",
        )

    team = await db.teams.find_one({"_id": team_oid})

    if team and team.get("teamLeadId") == user_id:
        return

    raise HTTPException(
        403,
        "Not allowed to view this task",
    )


# ================= MY TASKS =================
@router.get("/my")
async def my_tasks(
    status: Optional[str] = Query(None),
    before: Optional[str] = Query(None),  # ISO 8601 createdAt
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user),
):

    query: dict = {"assigneeId": user_id}

    if status:
        query["status"] = status

    if before:
        s = before
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            before_dt = datetime.fromisoformat(s)
        except (TypeError, ValueError):
            raise HTTPException(
                400,
                "Invalid 'before' timestamp",
            )
        query["createdAt"] = {"$lt": before_dt}

    tasks = []

    cursor = (
        db.tasks.find(query)
        .sort("createdAt", -1)
        .limit(limit)
    )

    async for t in cursor:
        tasks.append(_serialize(t))

    return tasks


# ================= GET ONE TASK =================
@router.get("/{id}")
async def get_task(
    id: str,
    user: dict = Depends(get_current_user_doc),
):

    task = await _load_task_or_404(id)
    await _ensure_can_view(task, user)

    serialized = _serialize(task)

    user_map = await _get_user_basics([
        task.get("assigneeId"),
        task.get("createdBy"),
    ])

    serialized["assignee"] = user_map.get(
        task.get("assigneeId")
    )
    serialized["createdByUser"] = user_map.get(
        task.get("createdBy")
    )

    return serialized


# ================= START TASK (PENDING → ONGOING) =================
@router.post("/{id}/start")
async def start_task(
    id: str,
    user_id: str = Depends(get_current_user),
):
    """Assignee marks the task as started (in progress)."""
    task = await _load_task_or_404(id)

    if task.get("assigneeId") != user_id:
        raise HTTPException(403, "Not your task")

    if task.get("status") == "COMPLETED":
        raise HTTPException(400, "Task is already completed")

    if task.get("status") == "ONGOING":
        return {"message": "Task already in progress"}

    now = datetime.now(timezone.utc)
    await db.tasks.update_one(
        {"_id": task["_id"]},
        {
            "$set": {
                "status": "ONGOING",
                "startedAt": now,
                "updatedAt": now,
            }
        },
    )

    return {"message": "Task started"}


# ================= COMPLETE TASK =================
@router.post("/{id}/complete")
async def complete_task(
    id: str,
    user_id: str = Depends(get_current_user),
):

    task = await _load_task_or_404(id)

    if task.get("assigneeId") != user_id:
        raise HTTPException(403, "Not your task")

    if task.get("status") == "COMPLETED":
        raise HTTPException(
            400,
            "Task already completed",
        )

    now = datetime.now(timezone.utc)

    # Server local date — matches /attendance/today's fallback.
    today = datetime.now().strftime("%Y-%m-%d")

    # On-time flag for KPI: completed on or before the due date. If no
    # due date was set, treat as on-time (can't be late if no deadline).
    on_time = True
    due_date_str = task.get("dueDate")
    if due_date_str:
        try:
            # dueDate is stored as YYYY-MM-DD; compare against today's
            # date in the same format to avoid tz drift.
            on_time = today <= due_date_str
        except Exception:
            on_time = True

    # 1. Mark the task done
    await db.tasks.update_one(
        {"_id": task["_id"]},
        {
            "$set": {
                "status": "COMPLETED",
                "completedAt": now,
                "onTime": on_time,
                "updatedAt": now,
            }
        },
    )

    # 2. Append to today's attendance, auto-creating if missing
    title_line = f"- {task.get('title', '')}"

    attendance = await db.attendance.find_one({
        "userId": user_id,
        "date": today,
    })

    if attendance:

        existing_notes = attendance.get("workNotes", "")

        new_notes = (
            (existing_notes + "\n" + title_line).strip()
            if existing_notes
            else title_line
        )

        await db.attendance.update_one(
            {"_id": attendance["_id"]},
            {
                "$addToSet": {
                    "completedTasks": str(task["_id"]),
                },
                "$set": {
                    "workNotes": new_notes,
                    "updatedAt": now,
                },
            },
        )

    else:

        await db.attendance.insert_one({
            "userId": user_id,
            "date": today,
            "attendanceType": "OFFICE",
            "status": "CHECKED_IN",
            "checkIn": now,
            "checkOut": None,
            "workNotes": title_line,
            "completedTasks": [str(task["_id"])],
            "createdAt": now,
            "updatedAt": now,
        })

    # Notify the team's TL.
    try:
        team = await db.teams.find_one({
            "_id": ObjectId(task["teamId"])
        })
        if team and team.get("teamLeadId"):
            assignee = await db.users.find_one({
                "_id": ObjectId(user_id)
            })
            actor_name = (
                assignee.get("name")
                if assignee else "Someone"
            )
            await push_to_user(
                team["teamLeadId"],
                "Task completed",
                f"{actor_name}: {task.get('title', '')}",
                {"type": "task_complete", "taskId": str(task["_id"])},
            )
            await create_notification(
                team["teamLeadId"],
                "task_complete",
                "Task completed",
                f"{actor_name}: {task.get('title', '')}",
                {"taskId": str(task["_id"])},
            )

            try:
                tl = await db.users.find_one({
                    "_id": ObjectId(team["teamLeadId"])
                })
            except (InvalidId, TypeError):
                tl = None
            if tl and tl.get("email"):
                await send_notification_email(
                    tl["email"],
                    f"Task completed: {task.get('title', '')}",
                    (
                        f"Hi {tl.get('name', 'there')},\n\n"
                        f"{actor_name} marked the following task complete "
                        f"in team \"{team.get('name', '')}\":\n\n"
                        f"Title: {task.get('title', '')}\n\n"
                        f"Open the app to review.\n\n"
                        f"Regards,\n{COMPANY_NAME}"
                    ),
                )
    except Exception:
        pass

    return {"message": "Task completed"}


# ================= UNCOMPLETE TASK =================
@router.post("/{id}/uncomplete")
async def uncomplete_task(
    id: str,
    user_id: str = Depends(get_current_user),
):

    task = await _load_task_or_404(id)

    if task.get("assigneeId") != user_id:
        raise HTTPException(403, "Not your task")

    if task.get("status") != "COMPLETED":
        raise HTTPException(400, "Task is not completed")

    now = datetime.now(timezone.utc)

    # 1. Reset the task
    await db.tasks.update_one(
        {"_id": task["_id"]},
        {
            "$set": {
                "status": "PENDING",
                "updatedAt": now,
            },
            "$unset": {"completedAt": "", "onTime": ""},
        },
    )

    # 2. Find the attendance record holding this taskId (any day)
    #    and remove the auto-added title line + completedTasks entry.
    task_id_str = str(task["_id"])

    attendance = await db.attendance.find_one({
        "userId": user_id,
        "completedTasks": task_id_str,
    })

    if attendance:

        title_line = f"- {task.get('title', '')}"

        # Best-effort line removal — matches the auto-added line.
        # If the title was edited after completion, the old line stays;
        # user can edit workNotes manually.
        existing_notes = attendance.get("workNotes", "")

        new_notes = "\n".join(
            line
            for line in existing_notes.split("\n")
            if line != title_line
        )

        await db.attendance.update_one(
            {"_id": attendance["_id"]},
            {
                "$pull": {
                    "completedTasks": task_id_str,
                },
                "$set": {
                    "workNotes": new_notes,
                    "updatedAt": now,
                },
            },
        )

    return {"message": "Task reopened"}


# ================= COMMENTS — LIST =================
@router.get("/{id}/comments")
async def list_comments(
    id: str,
    user: dict = Depends(get_current_user_doc),
):

    task = await _load_task_or_404(id)
    await _ensure_can_view(task, user)

    comments_raw = []

    async for c in db.comments.find(
        {"taskId": id}
    ).sort("createdAt", 1):
        comments_raw.append(c)

    user_map = await _get_user_basics(
        c.get("userId") for c in comments_raw
    )

    return [
        _serialize_comment(
            c,
            user_map.get(c.get("userId")),
        )
        for c in comments_raw
    ]


# ================= COMMENTS — ADD =================
@router.post("/{id}/comments")
async def add_comment(
    id: str,
    data: CommentCreate,
    user: dict = Depends(get_current_user_doc),
):

    task = await _load_task_or_404(id)
    await _ensure_can_view(task, user)

    text = data.text.strip()

    if not text:
        raise HTTPException(400, "Comment text required")

    user_id = str(user["_id"])
    now = datetime.now(timezone.utc)

    comment = {
        "taskId": id,
        "userId": user_id,
        "text": text,
        "createdAt": now,
    }

    result = await db.comments.insert_one(comment)
    comment["_id"] = result.inserted_id

    # Notify the other party on the task (assignee + creator), minus the
    # commenter, so a conversation doesn't go unseen.
    author = user.get("name") or "Someone"
    snippet = text if len(text) <= 120 else text[:117] + "..."
    targets = {
        t for t in (task.get("assigneeId"), task.get("createdBy")) if t
    }
    targets.discard(user_id)
    for tid in targets:
        await notify_user(
            tid,
            "task_comment",
            f"{author} commented",
            f"{task.get('title', 'Task')}: {snippet}",
            {"taskId": id},
        )

    user_info = {
        "id": user_id,
        "name": user.get("name"),
        "email": user.get("email"),
    }

    return _serialize_comment(comment, user_info)


# ================= COMMENTS — DELETE =================
@router.delete("/{id}/comments/{commentId}")
async def delete_comment(
    id: str,
    commentId: str,
    user: dict = Depends(get_current_user_doc),
):

    try:
        c_oid = ObjectId(commentId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid comment id")

    comment = await db.comments.find_one({
        "_id": c_oid,
        "taskId": id,
    })

    if not comment:
        raise HTTPException(404, "Comment not found")

    if comment.get("userId") != str(user["_id"]):
        raise HTTPException(
            403,
            "You can only delete your own comments",
        )

    await db.comments.delete_one({"_id": c_oid})

    return {"message": "Comment deleted"}
