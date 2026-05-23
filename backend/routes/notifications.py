from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from database import db
from utils.dependencies import get_current_user

router = APIRouter()


def _serialize(n: dict) -> dict:
    return {
        "id": str(n["_id"]),
        "type": n.get("type"),
        "title": n.get("title"),
        "body": n.get("body"),
        "data": n.get("data", {}),
        "read": n.get("read", False),
        "createdAt": (
            n["createdAt"].isoformat()
            if n.get("createdAt") else None
        ),
        "readAt": (
            n["readAt"].isoformat()
            if n.get("readAt") else None
        ),
    }


# ================= LIST =================
@router.get("")
async def list_notifications(
    onlyUnread: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    before: Optional[str] = Query(None),  # ISO 8601 createdAt
    user_id: str = Depends(get_current_user),
):
    query: dict = {"userId": user_id}
    if onlyUnread:
        query["read"] = False
    if before:
        s = before
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            before_dt = datetime.fromisoformat(s)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid 'before' timestamp")
        query["createdAt"] = {"$lt": before_dt}

    out = []
    cursor = (
        db.notifications.find(query)
        .sort("createdAt", -1)
        .limit(limit)
    )
    async for n in cursor:
        out.append(_serialize(n))
    return out


# ================= UNREAD COUNT =================
@router.get("/unread-count")
async def unread_count(
    user_id: str = Depends(get_current_user),
):
    count = await db.notifications.count_documents({
        "userId": user_id,
        "read": False,
    })
    return {"count": count}


# ================= MARK ONE READ =================
@router.post("/{id}/read")
async def mark_read(
    id: str,
    user_id: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    now = datetime.now(timezone.utc)
    result = await db.notifications.update_one(
        {"_id": oid, "userId": user_id},
        {"$set": {"read": True, "readAt": now}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Notification not found")
    return {"message": "Marked as read"}


# ================= MARK ALL READ =================
@router.post("/read-all")
async def mark_all_read(
    user_id: str = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    result = await db.notifications.update_many(
        {"userId": user_id, "read": False},
        {"$set": {"read": True, "readAt": now}},
    )
    return {"updated": result.modified_count}
