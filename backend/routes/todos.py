from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from database import db
from utils.dependencies import get_current_user, get_current_user_doc
from models.todo import TodoCreate, TodoUpdate

router = APIRouter()


def _serialize(t: dict) -> dict:
    return {
        "id": str(t["_id"]),
        "title": t.get("title"),
        "description": t.get("description"),
        "dueDate": t.get("dueDate"),
        "priority": t.get("priority", "MEDIUM"),
        "reminderAt": t.get("reminderAt"),
        "status": t.get("status", "OPEN"),
        "isPrivate": bool(t.get("isPrivate")),
        "userId": t.get("userId"),
        "ownerName": t.get("_ownerName"),
        "createdAt": (
            t["createdAt"].isoformat()
            if t.get("createdAt") else None
        ),
        "completedAt": (
            t["completedAt"].isoformat()
            if t.get("completedAt") else None
        ),
    }


# ================= LIST MINE =================
@router.get("")
async def list_todos(
    status: Optional[str] = Query(None),  # OPEN | DONE
    limit: int = Query(100, ge=1, le=500),
    user_id: str = Depends(get_current_user),
):
    query: dict = {"userId": user_id}
    if status:
        query["status"] = status
    out = []
    async for t in db.todos.find(query).sort("createdAt", -1).limit(limit):
        out.append(_serialize(t))
    return out


# ================= CREATE =================
@router.post("")
async def create_todo(
    data: TodoCreate,
    user_id: str = Depends(get_current_user),
):
    title = (data.title or "").strip()
    if not title:
        raise HTTPException(400, "title is required")

    now = datetime.now(timezone.utc)
    doc = {
        "userId": user_id,
        "title": title,
        "description": data.description or "",
        "dueDate": data.dueDate,
        "priority": data.priority or "MEDIUM",
        "reminderAt": data.reminderAt,
        "status": "OPEN",
        "isPrivate": bool(data.isPrivate),
        "createdAt": now,
        "updatedAt": now,
        "completedAt": None,
    }
    result = await db.todos.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize(doc)


# ================= UPDATE =================
@router.put("/{id}")
async def update_todo(
    id: str,
    data: TodoUpdate,
    user_id: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    now = datetime.now(timezone.utc)
    update: dict = {"updatedAt": now}
    for field in ("title", "description", "dueDate", "priority", "reminderAt"):
        v = getattr(data, field)
        if v is not None:
            update[field] = v
    if data.isPrivate is not None:
        update["isPrivate"] = bool(data.isPrivate)
    if data.status is not None:
        update["status"] = data.status
        # completedAt has to follow the column, or a card dragged out of
        # Done keeps a completion date and the attendance pull picks it up
        # again tomorrow.
        update["completedAt"] = now if data.status == "DONE" else None
    # If the reminder time was (re)set, clear the sent flag so the
    # scheduler will fire the new reminder.
    if data.reminderAt is not None:
        update["reminderSent"] = False

    result = await db.todos.update_one(
        {"_id": oid, "userId": user_id},
        {"$set": update},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Todo not found")
    return {"message": "Todo updated"}


# ================= A REPORT'S BOARD =================
@router.get("/of/{userId}")
async def todos_of_report(
    userId: str,
    viewer: dict = Depends(get_current_user_doc),
):
    """One of your reports' boards, minus anything they marked private.

    Scoped to the reporting line rather than to a role: being a manager
    somewhere doesn't entitle you to read the personal board of someone who
    doesn't report to you. HR and the CEO see everyone, as elsewhere.
    """
    try:
        target = await db.users.find_one({"_id": ObjectId(userId)})
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid user id")
    if not target:
        raise HTTPException(404, "User not found")

    viewer_id = str(viewer["_id"])
    allowed = (
        viewer.get("role") in ("HR", "CEO")
        or viewer_id == userId
        or str(target.get("reportingManagerId") or "") == viewer_id
        or str((target.get("work") or {}).get("reportingManagerId") or "")
        == viewer_id
    )
    if not allowed:
        raise HTTPException(
            403, "You can only open the board of someone who reports to you."
        )

    query: dict = {"userId": userId}
    # Your own board shows everything; a manager's view never shows the
    # items the owner chose to keep back.
    if viewer_id != userId:
        query["isPrivate"] = {"$ne": True}

    out = []
    async for t in db.todos.find(query).sort("createdAt", -1):
        t["_ownerName"] = target.get("name")
        out.append(_serialize(t))
    return {
        "todos": out,
        "owner": {"id": userId, "name": target.get("name")},
        "hiddenCount": (
            await db.todos.count_documents(
                {"userId": userId, "isPrivate": True}
            )
            if viewer_id != userId else 0
        ),
    }


# ================= COMPLETE =================
@router.post("/{id}/complete")
async def complete_todo(
    id: str,
    user_id: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    now = datetime.now(timezone.utc)
    result = await db.todos.update_one(
        {"_id": oid, "userId": user_id},
        {"$set": {"status": "DONE", "completedAt": now, "updatedAt": now}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Todo not found")
    return {"message": "Todo completed"}


# ================= REOPEN =================
@router.post("/{id}/reopen")
async def reopen_todo(
    id: str,
    user_id: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    now = datetime.now(timezone.utc)
    result = await db.todos.update_one(
        {"_id": oid, "userId": user_id},
        {"$set": {"status": "OPEN", "completedAt": None, "updatedAt": now}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Todo not found")
    return {"message": "Todo reopened"}


# ================= DELETE =================
@router.delete("/{id}")
async def delete_todo(
    id: str,
    user_id: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    result = await db.todos.delete_one({"_id": oid, "userId": user_id})
    if result.deleted_count == 0:
        raise HTTPException(404, "Todo not found")
    return {"message": "Todo deleted"}
