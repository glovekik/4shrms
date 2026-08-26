"""Ad-hoc chat groups.

Unlike project chats — whose membership follows the project roster — these are
free-form: "Cricket Team", "Diwali Planning", anything. They carry no tasks,
attendance or payroll, only a conversation.

Only HR can create, rename, delete or change who is in one. Everyone else can
read and post in the groups they belong to. That is a deliberate product
choice, not a technical limit: it keeps the chat list from filling with
abandoned groups nobody cleans up.

Messages reuse the same helpers as office and project chat, so editing,
deleting, read receipts and mention handling behave identically everywhere.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from database import db
from utils.dependencies import get_current_user_doc, require_hr
from utils.audit import log_audit
from utils.notify import notify_user
from models.chat_group import ChatGroupCreate, ChatGroupUpdate
from models.message import MessageCreate, MessageEdit
from routes.chat import (
    _list_messages,
    _insert_message,
    _mark_read,
    _edit_message,
    _delete_message,
)


router = APIRouter()

CHANNEL = "group"


def _oid(value: str, label: str = "id") -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(400, f"Invalid {label}")


def _serialize(g: dict, viewer_id: Optional[str] = None) -> dict:
    return {
        "id": str(g["_id"]),
        # Drives whether the UI offers a delete control at all.
        "viewerCanDelete": bool(viewer_id) and g.get("createdBy") == viewer_id,
        "name": g.get("name"),
        "description": g.get("description") or "",
        "memberIds": g.get("memberIds", []),
        "createdBy": g.get("createdBy"),
        "createdAt": (
            g["createdAt"].isoformat() if g.get("createdAt") else None
        ),
    }


async def _validate_members(ids: Optional[list[str]]) -> list[str]:
    if not ids:
        return []
    oids = []
    for uid in ids:
        try:
            oids.append(ObjectId(uid))
        except (InvalidId, TypeError):
            raise HTTPException(400, f"Invalid user id: {uid}")
    found = [str(u["_id"]) async for u in db.users.find({"_id": {"$in": oids}}, {"_id": 1})]
    if len(found) != len(set(ids)):
        raise HTTPException(400, "One or more user ids do not exist")
    return found


async def _ensure_access(group_id: str, user: dict) -> dict:
    """HR sees every group; everyone else only the ones they're in."""
    group = await db.chat_groups.find_one({"_id": _oid(group_id, "group id")})
    if not group:
        raise HTTPException(404, "Group not found")
    if user.get("role") in ("HR", "CEO"):
        return group
    if str(user["_id"]) in (group.get("memberIds") or []):
        return group
    raise HTTPException(403, "You don't have access to this chat")


# ================= GROUP CRUD =================
@router.get("")
async def list_groups(
    all: bool = Query(False, description="HR only — include groups you're not in"),
    user: dict = Depends(get_current_user_doc),
):
    user_id = str(user["_id"])
    is_hr = user.get("role") in ("HR", "CEO")
    query: dict = {} if (is_hr and all) else {"memberIds": user_id}
    out = []
    async for g in db.chat_groups.find(query).sort("name", 1):
        out.append(_serialize(g, user_id))
    return out


@router.post("")
async def create_group(
    data: ChatGroupCreate,
    hr: dict = Depends(require_hr),
):
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")

    actor_id = str(hr["_id"])
    members = await _validate_members(data.memberIds)
    # The creator is always in the group — an HR user shouldn't have to add
    # themselves to a group they just made.
    if actor_id not in members:
        members.append(actor_id)

    now = datetime.now(timezone.utc)
    result = await db.chat_groups.insert_one({
        "name": name,
        "description": data.description or "",
        "memberIds": members,
        "createdBy": actor_id,
        "createdAt": now,
        "updatedAt": now,
    })
    group_id = str(result.inserted_id)

    await log_audit(
        actor_id=actor_id,
        action="chat_group.create",
        entity_type="chat_groups",
        entity_id=group_id,
        after={"name": name, "members": len(members)},
    )

    for uid in set(members) - {actor_id}:
        await notify_user(
            uid,
            "chat_group_added",
            "Added to a group",
            f"You've been added to \"{name}\".",
            {"channelType": CHANNEL, "channelId": group_id},
        )

    return {"id": group_id, "message": "Group created"}


@router.get("/{groupId}")
async def get_group(
    groupId: str,
    user: dict = Depends(get_current_user_doc),
):
    return _serialize(await _ensure_access(groupId, user), str(user["_id"]))


@router.put("/{groupId}")
async def update_group(
    groupId: str,
    data: ChatGroupUpdate,
    hr: dict = Depends(require_hr),
):
    oid = _oid(groupId, "group id")
    existing = await db.chat_groups.find_one({"_id": oid})
    if not existing:
        raise HTTPException(404, "Group not found")

    # Same rule as delete, for the same reason. Restricting only deletion left
    # the door open: another HR user could rename a group and strip every
    # member out of it, which destroys it just as thoroughly.
    if existing.get("createdBy") != str(hr["_id"]):
        raise HTTPException(
            403,
            "Only the person who created this group can change it.",
        )

    update: dict = {"updatedAt": datetime.now(timezone.utc)}
    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(400, "name cannot be empty")
        update["name"] = name
    if data.description is not None:
        update["description"] = data.description

    added: set[str] = set()
    if data.memberIds is not None:
        members = await _validate_members(data.memberIds)
        added = set(members) - set(existing.get("memberIds") or [])
        update["memberIds"] = members

    await db.chat_groups.update_one({"_id": oid}, {"$set": update})
    await log_audit(
        actor_id=str(hr["_id"]),
        action="chat_group.update",
        entity_type="chat_groups",
        entity_id=groupId,
        after={k: v for k, v in update.items() if k != "updatedAt"},
    )

    gname = update.get("name", existing.get("name", "a group"))
    for uid in added - {str(hr["_id"])}:
        await notify_user(
            uid,
            "chat_group_added",
            "Added to a group",
            f"You've been added to \"{gname}\".",
            {"channelType": CHANNEL, "channelId": groupId},
        )
    return {"message": "Group updated"}


@router.delete("/{groupId}")
async def delete_group(
    groupId: str,
    hr: dict = Depends(require_hr),
):
    """Only the HR user who created the group can delete it.

    Deletion destroys the whole conversation, so it stays with the person who
    started it rather than any HR account being able to wipe a group they know
    nothing about.
    """
    oid = _oid(groupId, "group id")
    group = await db.chat_groups.find_one({"_id": oid})
    if not group:
        raise HTTPException(404, "Group not found")

    actor_id = str(hr["_id"])
    if group.get("createdBy") != actor_id:
        raise HTTPException(
            403,
            "Only the person who created this group can delete it.",
        )

    await db.chat_groups.delete_one({"_id": oid})

    # The conversation goes with the group — leaving messages behind would
    # make them unreachable but still countable as unread.
    msgs = await db.chat_messages.delete_many(
        {"channelType": CHANNEL, "channelId": groupId}
    )
    await db.chat_reads.delete_many({"channelType": CHANNEL, "channelId": groupId})

    await log_audit(
        actor_id=actor_id,
        action="chat_group.delete",
        entity_type="chat_groups",
        entity_id=groupId,
        before={"messagesDeleted": msgs.deleted_count},
    )
    return {"message": "Group deleted"}


# ================= GROUP MESSAGES =================
@router.get("/{groupId}/messages")
async def list_group_messages(
    groupId: str,
    before: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    user: dict = Depends(get_current_user_doc),
):
    await _ensure_access(groupId, user)
    return await _list_messages(CHANNEL, groupId, before, limit, user)


@router.post("/{groupId}/messages")
async def post_group_message(
    groupId: str,
    data: MessageCreate,
    user: dict = Depends(get_current_user_doc),
):
    await _ensure_access(groupId, user)
    return await _insert_message(
        CHANNEL, groupId, data.text, data.mentions, user, data.attachments,
    )


@router.post("/{groupId}/messages/read")
async def read_group_messages(
    groupId: str,
    user: dict = Depends(get_current_user_doc),
):
    await _ensure_access(groupId, user)
    return await _mark_read(CHANNEL, groupId, user)


@router.put("/{groupId}/messages/{messageId}")
async def edit_group_message(
    groupId: str,
    messageId: str,
    data: MessageEdit,
    user: dict = Depends(get_current_user_doc),
):
    await _ensure_access(groupId, user)
    return await _edit_message(
        CHANNEL, groupId, messageId, data.text, data.mentions, user,
    )


@router.delete("/{groupId}/messages/{messageId}")
async def delete_group_message(
    groupId: str,
    messageId: str,
    scope: str = Query("everyone"),
    user: dict = Depends(get_current_user_doc),
):
    await _ensure_access(groupId, user)
    return await _delete_message(CHANNEL, groupId, messageId, user, scope)
