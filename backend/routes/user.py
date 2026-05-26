import base64
import json
import re
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel
from typing import Optional

from database import db
from models.user import PersonalInfo, EmergencyContact
from utils.audit import log_audit
from utils.dependencies import get_current_user

router = APIRouter()


@router.get("/me")
async def get_me(
    user_id: str = Depends(get_current_user)
):

    user = await db.users.find_one({
        "_id": ObjectId(user_id)
    })

    if not user:
        return {
            "message": "User not found"
        }

    return {
        "id": str(user["_id"]),
        "name": user.get("name"),
        "email": user.get("email"),
    }


def _encode_dir_cursor(name: Optional[str], oid: ObjectId) -> str:
    payload = json.dumps({"n": name or "", "i": str(oid)})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_dir_cursor(token: str) -> tuple[str, ObjectId]:
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(token.encode()).decode()
        )
        return payload["n"], ObjectId(payload["i"])
    except (ValueError, KeyError, InvalidId, TypeError):
        raise HTTPException(400, "Invalid cursor")


# Lightweight directory for @-mentions and people pickers — every
# authenticated user can read it; we expose only non-sensitive fields.
# Cursor pagination: sort by (name asc, _id asc); the opaque cursor is the
# last (name, _id) of the previous page so subsequent reads skip rows we've
# already returned even when names collide.
@router.get("/users/directory")
async def list_user_directory(
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None),
    _user_id: str = Depends(get_current_user),
):
    query: dict = {
        "status": {"$ne": "Terminated"},
    }
    if search:
        safe = re.escape(search)
        query["$or"] = [
            {"name": {"$regex": safe, "$options": "i"}},
            {"email": {"$regex": safe, "$options": "i"}},
        ]

    if cursor:
        last_name, last_id = _decode_dir_cursor(cursor)
        cursor_clause = {
            "$or": [
                {"name": {"$gt": last_name}},
                {"name": last_name, "_id": {"$gt": last_id}},
            ]
        }
        query = {"$and": [query, cursor_clause]} if query else cursor_clause

    items: list[dict] = []
    last_doc: Optional[dict] = None
    # Over-fetch by 1 to detect if another page exists without a second query.
    async for u in (
        db.users.find(query)
        .sort([("name", 1), ("_id", 1)])
        .limit(limit + 1)
    ):
        last_doc = u
        if len(items) >= limit:
            continue
        items.append({
            "id": str(u["_id"]),
            "name": u.get("name"),
            "email": u.get("email"),
            "tag": u.get("tag"),
        })

    has_more = last_doc is not None and len(items) == limit and (
        # last_doc was the extra (limit+1)th row
        str(last_doc["_id"]) != items[-1]["id"]
    )
    next_cursor: Optional[str] = None
    if has_more and items:
        # Encode cursor from the last returned item, not the overflow row.
        tail = items[-1]
        next_cursor = _encode_dir_cursor(
            tail.get("name"), ObjectId(tail["id"])
        )

    return {"items": items, "nextCursor": next_cursor}


# ================= Employee self-service profile =================
# The employee can view their own profile and fill in personal details
# that HR left blank ("pending"). Bank account & statutory IDs are HR-only
# and are returned for display but never accepted from this endpoint.

class MyProfileUpdate(BaseModel):
    personal: Optional[PersonalInfo] = None
    emergencyContact: Optional[EmergencyContact] = None


def _serialize_profile(user: dict) -> dict:
    return {
        "id": str(user["_id"]),
        "name": user.get("name"),
        "email": user.get("email"),
        "role": user.get("role"),
        "employeeCode": user.get("employeeCode"),
        "workPhone": user.get("workPhone"),
        "joiningDate": user.get("joiningDate"),
        "status": user.get("status"),
        "profilePictureUrl": user.get("profilePictureUrl"),
        "personal": user.get("personal") or {},
        "emergencyContact": user.get("emergencyContact") or {},
        # Display-only — not editable from /me/profile.
        "bankAccounts": user.get("bankAccounts") or [],
        "statutory": user.get("statutory") or {},
    }


def _fill_blanks(current: dict, incoming: dict, prefix: str) -> dict:
    """Build a {dotted-path: value} $set map for `incoming`, but ONLY for
    fields that are currently blank (None/"") in `current`. A value HR (or
    the employee earlier) already set is left untouched — the implicit
    "blank = pending" rule, enforced server-side. Recurses into nested
    objects (address, education)."""
    out: dict = {}
    for key, val in incoming.items():
        if val is None or val == "":
            continue
        cur = current.get(key) if isinstance(current, dict) else None
        path = f"{prefix}.{key}"
        if isinstance(val, dict):
            out.update(
                _fill_blanks(cur if isinstance(cur, dict) else {}, val, path)
            )
        elif cur is None or cur == "":
            out[path] = val
    return out


@router.get("/me/profile")
async def get_my_profile(user_id: str = Depends(get_current_user)):
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(404, "User not found")
    return _serialize_profile(user)


@router.put("/me/profile")
async def update_my_profile(
    data: MyProfileUpdate,
    user_id: str = Depends(get_current_user),
):
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(404, "User not found")

    incoming = data.model_dump(exclude_none=True)
    updates: dict = {}
    if incoming.get("personal"):
        updates.update(
            _fill_blanks(user.get("personal") or {}, incoming["personal"], "personal")
        )
    if incoming.get("emergencyContact"):
        updates.update(
            _fill_blanks(
                user.get("emergencyContact") or {},
                incoming["emergencyContact"],
                "emergencyContact",
            )
        )

    changed = sorted(updates.keys())
    if updates:
        updates["updatedAt"] = datetime.now(timezone.utc)
        await db.users.update_one(
            {"_id": ObjectId(user_id)}, {"$set": updates}
        )
        await log_audit(
            actor_id=user_id,
            action="profile.self_update",
            entity_type="users",
            entity_id=user_id,
            after={"fields": changed},
        )

    fresh = await db.users.find_one({"_id": ObjectId(user_id)})
    return {**_serialize_profile(fresh), "updatedFields": changed}


# ================= Profile picture — every user can set their own =================
# Separate from /me/profile because that endpoint enforces the
# "blank-only" rule on personal info (HR-set values stay locked). A
# profile picture is owned by the user — they can replace it freely.
# Accepting `url: null` clears it (used by the "Remove photo" action).
class MyProfilePictureUpdate(BaseModel):
    url: Optional[str] = None


@router.put("/me/profile-picture")
async def update_my_profile_picture(
    data: MyProfilePictureUpdate,
    user_id: str = Depends(get_current_user),
):
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(404, "User not found")

    new_url = (data.url or "").strip() or None
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "profilePictureUrl": new_url,
                "updatedAt": datetime.now(timezone.utc),
            }
        },
    )
    await log_audit(
        actor_id=user_id,
        action="profile.picture_update",
        entity_type="users",
        entity_id=user_id,
        after={"profilePictureUrl": new_url},
    )

    fresh = await db.users.find_one({"_id": ObjectId(user_id)})
    return _serialize_profile(fresh)


# ================= Chat unread badge =================
# The dashboard Chat tile shows a count of unread chat messages —
# anything newer than the user's last chat-read marker, across BOTH
# office chat (company-wide) and team chats they belong to. Author's
# own messages are excluded.

@router.get("/me/chat-unread")
async def my_chat_unread(user_id: str = Depends(get_current_user)):
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(404, "User not found")

    team_ids: list[str] = []
    async for t in db.teams.find(
        {"$or": [{"teamLeadId": user_id}, {"memberIds": user_id}]},
        {"_id": 1},
    ):
        team_ids.append(str(t["_id"]))

    since = user.get("chatLastReadAt")
    if since is None:
        since = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elif since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    # Office (company-wide) + the user's team channels.
    channel_clause: list[dict] = [{"channelType": "office"}]
    if team_ids:
        channel_clause.append({
            "channelType": "team",
            "channelId": {"$in": team_ids},
        })

    count = await db.chat_messages.count_documents({
        "$or": channel_clause,
        "userId": {"$ne": user_id},
        "createdAt": {"$gt": since},
    })
    return {"count": count}


@router.post("/me/chat-read")
async def mark_chat_read(user_id: str = Depends(get_current_user)):
    """Called when the user opens a team chat — clears the unread badge."""
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"chatLastReadAt": datetime.now(timezone.utc)}},
    )
    return {"ok": True}
