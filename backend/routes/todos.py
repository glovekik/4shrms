from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from database import db
from utils.dependencies import get_current_user
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

    update: dict = {"updatedAt": datetime.now(timezone.utc)}
    for field in ("title", "description", "dueDate", "priority", "reminderAt"):
        v = getattr(data, field)
        if v is not None:
            update[field] = v
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
