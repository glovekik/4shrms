import base64
import json
import re
from datetime import datetime, timezone
from utils.ist import IST
from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel
from typing import Optional

from database import db
from utils import project_members as pm
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
        "profilePictureUrl": user.get("profilePictureUrl"),
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
            "profilePictureUrl": u.get("profilePictureUrl"),
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


# ================= Public-ish profile card =================
@router.get("/users/{user_id}/card")
async def get_user_card(
    user_id: str,
    _viewer_id: str = Depends(get_current_user),
):
    """Basic profile any signed-in colleague may see (chat, directory).

    Deliberately NARROW. The HR profile endpoint carries salary, PAN, bank,
    statutory IDs and home address; none of that belongs in a card a peer can
    open from a chat message, so this builds an explicit allow-list rather
    than filtering an HR document down — a new sensitive field added to the
    user model can't leak in through here by accident.
    """
    try:
        oid = ObjectId(user_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid user id")

    u = await db.users.find_one({"_id": oid})
    if not u:
        raise HTTPException(404, "User not found")

    work = u.get("work") or {}
    personal = u.get("personal") or {}

    department = None
    dept_id = u.get("departmentId") or work.get("departmentId")
    if dept_id:
        try:
            d = await db.departments.find_one({"_id": ObjectId(dept_id)})
            department = (d or {}).get("name")
        except (InvalidId, TypeError):
            department = None

    return {
        "id": str(u["_id"]),
        "name": u.get("name"),
        "email": u.get("email"),
        "profilePictureUrl": u.get("profilePictureUrl"),
        "employeeCode": u.get("employeeCode"),
        "tag": u.get("tag"),
        "status": u.get("status"),
        "designation": work.get("jobTitle") or work.get("jobPosition"),
        "department": department,
        "workLocation": work.get("workLocation"),
        # Work phone only — the personal number is not a colleague's business.
        "workPhone": u.get("workPhone"),
        "joiningDate": u.get("joiningDate"),
        # Birthday without the year: the app already surfaces birthdays in
        # the sidebar, but age is not ours to publish.
        "birthday": (personal.get("birthday") or "")[5:] or None,
    }


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
        "autoCheckoutQuota": user.get("autoCheckoutQuota", 5),
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
# office chat (company-wide) and project chats they belong to. Author's
# own messages are excluded.

@router.get("/me/chat-unread")
async def my_chat_unread(user_id: str = Depends(get_current_user)):
    """Total unread across office chat and the caller's project chats.

    Counted per channel from `chat_reads`. It previously used a single
    `chatLastReadAt` on the user, which meant opening office chat marked every
    project chat read as well and their unread messages disappeared unseen.
    """
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(404, "User not found")

    channels: list[tuple[str, Optional[str]]] = [("office", None)]
    for pid in await pm.project_ids_for_user(user_id):
        channels.append(("project", pid))
    async for g in db.chat_groups.find({"memberIds": user_id}, {"_id": 1}):
        channels.append(("group", str(g["_id"])))

    pointers: dict[tuple, object] = {}
    async for r in db.chat_reads.find({"userId": user_id}):
        pointers[(r.get("channelType"), r.get("channelId"))] = r.get("lastReadAt")

    count = 0
    for ctype, cid in channels:
        q: dict = {
            "channelType": ctype,
            "channelId": cid,
            "userId": {"$ne": user_id},
        }
        since = pointers.get((ctype, cid))
        if since is not None:
            q["createdAt"] = {"$gt": since}
        count += await db.chat_messages.count_documents(q)

    return {"count": count}


# `POST /me/chat-read` used to live here. It stamped a single
# `users.chatLastReadAt`, which is what made reading office chat silently mark
# every project chat read too. Unread is now per-channel in `chat_reads`, and
# each channel is marked read by its own endpoint
# (/chat/office|project|group/.../messages/read), so this endpoint wrote a
# field nothing reads any more. Removed rather than left as a no-op, so no
# future caller mistakes it for a working "clear the badge" call.
