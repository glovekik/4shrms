from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone, timedelta

from typing import Optional

from database import db
from utils import project_members as pm
from utils.dependencies import get_current_user_doc
from utils.ist import now_ist_naive, iso_naive, IST
from utils.notify import notify_user
from utils.push import push_to_users
from utils.realtime import publish as realtime_publish
from models.message import MessageCreate, MessageEdit
from config import CHAT_EDIT_WINDOW_MINUTES, CHAT_DELETE_WINDOW_MINUTES

# Two routers, one helper set. Office and project chat are structurally identical
# — `channelType` + `channelId` discriminate which channel a message belongs to.
office_router = APIRouter()
project_router = APIRouter()
# Conversation list — the chat home screen.
list_router = APIRouter()


# ================= SERIALIZER =================
def _serialize_message(
    m: dict,
    user_info: Optional[dict] = None,
    read_by_others: bool = False,
) -> dict:
    deleted = bool(m.get("deleted"))
    created = m.get("createdAt")
    return {
        "id": str(m["_id"]),
        "userId": m.get("userId"),
        "user": user_info,
        # A message deleted-for-everyone keeps its slot but drops its content.
        "text": "" if deleted else m.get("text", ""),
        "mentions": [] if deleted else m.get("mentions", []),
        "attachments": [] if deleted else m.get("attachments", []),
        "createdAt": iso_naive(created),
        "editedAt": (
            iso_naive(m["editedAt"])
            if m.get("editedAt") and not deleted else None
        ),
        "deleted": deleted,
        # Whether anyone other than the author has read it — drives read
        # receipts and whether edit/delete-for-everyone is still allowed.
        "readByOthers": read_by_others,
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
            "profilePictureUrl": u.get("profilePictureUrl"),
        }
    return result


def _parse_before(
    before: Optional[str],
) -> Optional[datetime]:
    """Accepts ISO 8601, including the trailing-Z form."""
    if not before:
        return None

    s = before
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        raise HTTPException(
            400,
            "Invalid 'before' timestamp",
        )


async def _channel_reads(
    channel_type: str, channel_id: Optional[str]
) -> dict:
    """{userId: lastReadAt} for everyone who has opened this channel."""
    reads: dict = {}
    async for r in db.chat_reads.find(
        {"channelType": channel_type, "channelId": channel_id}
    ):
        if r.get("userId") and r.get("lastReadAt"):
            reads[r["userId"]] = r["lastReadAt"]
    return reads


def _read_by_others(msg: dict, reads: dict) -> bool:
    """True if any user other than the author has read past this message."""
    created = msg.get("createdAt")
    if not created:
        return False
    author = msg.get("userId")
    for uid, last in reads.items():
        if uid != author and last and last >= created:
            return True
    return False


async def _list_messages(
    channel_type: str,
    channel_id: Optional[str],
    before: Optional[str],
    limit: int,
    user: dict,
) -> list[dict]:
    user_id = str(user["_id"])
    query: dict = {
        "channelType": channel_type,
        "channelId": channel_id,
        # Hide messages this user deleted just for themselves.
        "deletedFor": {"$ne": user_id},
    }

    # createdAt bounds: pagination cursor (< before) AND join-date floor
    # (>= account creation) so a new member never sees pre-join history.
    created_filter: dict = {}
    before_dt = _parse_before(before)
    if before_dt:
        created_filter["$lt"] = before_dt
    floor = user.get("createdAt")
    if floor:
        created_filter["$gte"] = floor
    if created_filter:
        query["createdAt"] = created_filter

    raw: list[dict] = []
    cursor = (
        db.chat_messages.find(query)
        .sort("createdAt", -1)
        .limit(limit)
    )
    async for m in cursor:
        raw.append(m)

    # Oldest-first within the page so the UI just appends.
    raw.reverse()

    user_map = await _get_user_basics(m.get("userId") for m in raw)
    reads = await _channel_reads(channel_type, channel_id)

    return [
        _serialize_message(
            m,
            user_map.get(m.get("userId")),
            _read_by_others(m, reads),
        )
        for m in raw
    ]


async def _validate_mentions(raw: Optional[list[str]]) -> list[str]:
    """Drops unknown / malformed user IDs and de-duplicates. We persist
    only IDs the server can verify so a stale FE can't fan out notifications
    to arbitrary string values."""
    if not raw:
        return []
    seen: set[str] = set()
    oids: list[ObjectId] = []
    for uid in raw:
        if not isinstance(uid, str) or uid in seen:
            continue
        seen.add(uid)
        try:
            oids.append(ObjectId(uid))
        except (InvalidId, TypeError):
            continue
    if not oids:
        return []
    valid: list[str] = []
    async for u in db.users.find({"_id": {"$in": oids}}, {"_id": 1}):
        valid.append(str(u["_id"]))
    return valid


async def _insert_message(
    channel_type: str,
    channel_id: Optional[str],
    text: str,
    mentions: Optional[list[str]],
    user: dict,
    attachments: Optional[list] = None,
) -> dict:
    text = (text or "").strip()
    # Serialize Pydantic attachment models to plain dicts.
    atts = [
        a.model_dump() if hasattr(a, "model_dump") else dict(a)
        for a in (attachments or [])
    ]

    if not text and not atts:
        raise HTTPException(
            400,
            "Message text or attachment required",
        )

    user_id = str(user["_id"])
    now = now_ist_naive()

    resolved_mentions = await _validate_mentions(mentions)
    # Don't notify the author for self-mentions.
    resolved_mentions = [m for m in resolved_mentions if m != user_id]

    msg = {
        "channelType": channel_type,
        "channelId": channel_id,
        "userId": user_id,
        "text": text,
        "mentions": resolved_mentions,
        "attachments": atts,
        "createdAt": now,
    }

    result = await db.chat_messages.insert_one(msg)
    msg["_id"] = result.inserted_id

    user_info = {
        "id": user_id,
        "name": user.get("name"),
        "email": user.get("email"),
        "profilePictureUrl": user.get("profilePictureUrl"),
    }

    author_name = user.get("name") or "Someone"
    if text:
        snippet = text if len(text) <= 140 else text[:137] + "..."
    elif atts:
        kinds = {a.get("type") for a in atts}
        if "image" in kinds:
            snippet = "📷 Photo"
        elif "voice" in kinds:
            snippet = "🎤 Voice message"
        elif "sticker" in kinds:
            snippet = "Sticker"
        else:
            snippet = "📎 Attachment"
    else:
        snippet = ""

    if resolved_mentions:
        for mentioned_id in resolved_mentions:
            try:
                await notify_user(
                    mentioned_id,
                    "chat_mention",
                    f"{author_name} mentioned you",
                    snippet,
                    {
                        "channelType": channel_type,
                        "channelId": channel_id,
                        "messageId": str(msg["_id"]),
                    },
                )
            except Exception:
                pass

    # Plain chat messages (no @mention) deliver via three lightweight
    # channels — none of them write rows to db.notifications, which
    # would otherwise grow by one row per recipient per message and
    # eat the Atlas free-tier quota fast:
    #   1. Expo PUSH (OS-level alert on devices with a registered token).
    #   2. SSE realtime tick → drives the dashboard chat-tile badge to
    #      refresh /me/chat-unread live. realtime_publish is a no-op
    #      for users with no open SSE subscriber, so it's basically
    #      free.
    #   3. The chat thread's existing 3s poll picks up the message
    #      when the recipient opens the screen.
    # @mentions earlier in this function DO write a bell row via
    # notify_user — those are deliberate.
    chat_data = {
        "type": "chat_message",
        "channelType": channel_type,
        "channelId": channel_id,
        "messageId": str(msg["_id"]),
        "authorId": user_id,
        # The Android client rebuilds the notification itself with notifee
        # (MessagingStyle), so the sender and the text have to travel in the
        # DATA payload — an FCM `notification` block only carries one title
        # and one body, which is why ten messages used to mean ten cards.
        "authorName": author_name or "Someone",
        "body": snippet,
    }

    if channel_type == "office":
        recipient_ids: list[str] = []
        async for u in db.users.find(
            {"status": {"$ne": "Terminated"}},
            {"_id": 1},
        ):
            uid = str(u["_id"])
            if uid == user_id:
                continue
            if uid in resolved_mentions:
                continue
            recipient_ids.append(uid)

        if recipient_ids:
            title = f"{author_name} · Office chat"
            office_data = {**chat_data, "channelName": "Office chat"}
            try:
                # One notification per conversation, not per message — a busy
                # office chat used to bury everything else in the shade.
                await push_to_users(
                    recipient_ids,
                    title,
                    snippet,
                    office_data,
                    collapse_key="chat:office",
                    # Data-only on Android: the client builds a MessagingStyle
                    # card so ten messages land in one expandable entry.
                    data_only_android=True,
                )
            except Exception:
                pass
            for rid in recipient_ids:
                try:
                    await realtime_publish(
                        rid,
                        {"type": "notification", "data": chat_data},
                    )
                except Exception:
                    pass

    if channel_type == "group" and channel_id:
        try:
            group = await db.chat_groups.find_one({"_id": ObjectId(channel_id)})
        except (InvalidId, TypeError):
            group = None
        if group:
            recipients = set(group.get("memberIds") or [])
            recipients.discard(user_id)
            recipients.difference_update(resolved_mentions)
            if recipients:
                gname = group.get("name") or "Group chat"
                try:
                    await push_to_users(
                        list(recipients),
                        f"{author_name} · {gname}",
                        snippet,
                        {**chat_data, "channelName": gname},
                        collapse_key=f"chat:group:{channel_id}",
                        data_only_android=True,
                    )
                except Exception:
                    pass
                for rid in recipients:
                    try:
                        await realtime_publish(
                            rid,
                            {"type": "notification", "data": chat_data},
                        )
                    except Exception:
                        pass

    if channel_type == "project" and channel_id:
        try:
            project = await db.projects.find_one({"_id": ObjectId(channel_id)})
        except (InvalidId, TypeError):
            project = None
        if project:
            # Current members only — someone who left the project stops
            # getting its chat notifications.
            recipients = set(await pm.current_member_ids(channel_id))
            recipients.discard(user_id)
            recipients.difference_update(resolved_mentions)
            if recipients:
                project_name = project.get("name") or "Project chat"
                title = f"{author_name} · {project_name}"
                team_data = {**chat_data, "channelName": project_name}
                try:
                    await push_to_users(
                        list(recipients),
                        title,
                        snippet,
                        team_data,
                        collapse_key=f"chat:project:{channel_id}",
                        data_only_android=True,
                    )
                except Exception:
                    pass
                for rid in recipients:
                    try:
                        await realtime_publish(
                            rid,
                            {"type": "notification", "data": chat_data},
                        )
                    except Exception:
                        pass

    return _serialize_message(msg, user_info)


async def _load_own_message(
    channel_type, channel_id, message_id, user, action: str
) -> dict:
    try:
        oid = ObjectId(message_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid message id")
    msg = await db.chat_messages.find_one({
        "_id": oid,
        "channelType": channel_type,
        "channelId": channel_id,
    })
    if not msg:
        raise HTTPException(404, "Message not found")
    if msg.get("userId") != str(user["_id"]):
        raise HTTPException(403, f"You can only {action} your own messages")
    return msg


def _within(created, minutes: int) -> bool:
    if not created:
        return False
    # createdAt is IST wall-clock (naive); compare against IST now.
    if created.tzinfo is not None:
        created = created.astimezone(IST).replace(tzinfo=None)
    return now_ist_naive() - created <= timedelta(minutes=minutes)


async def _mark_read(channel_type, channel_id, user) -> dict:
    """Stamp this user's read pointer for the channel to now."""
    await db.chat_reads.update_one(
        {
            "channelType": channel_type,
            "channelId": channel_id,
            "userId": str(user["_id"]),
        },
        {"$set": {"lastReadAt": now_ist_naive()}},
        upsert=True,
    )
    return {"message": "ok"}


async def _edit_message(
    channel_type, channel_id, message_id, text, mentions, user,
) -> dict:
    msg = await _load_own_message(channel_type, channel_id, message_id, user, "edit")
    if msg.get("deleted"):
        raise HTTPException(400, "This message was deleted")

    # Bounded only by the edit window now — authors may edit their own message
    # even after others have read it.
    if not _within(msg.get("createdAt"), CHAT_EDIT_WINDOW_MINUTES):
        raise HTTPException(403, "Edit window has passed")
    reads = await _channel_reads(channel_type, channel_id)

    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "Message text required")

    resolved = await _validate_mentions(mentions)
    resolved = [m for m in resolved if m != str(user["_id"])]
    now = now_ist_naive()
    await db.chat_messages.update_one(
        {"_id": msg["_id"]},
        {"$set": {"text": text, "mentions": resolved, "editedAt": now}},
    )
    msg.update({"text": text, "mentions": resolved, "editedAt": now})
    user_info = {"id": str(user["_id"]), "name": user.get("name"), "email": user.get("email"), "profilePictureUrl": user.get("profilePictureUrl")}
    return _serialize_message(msg, user_info, _read_by_others(msg, reads))


async def _delete_message(
    channel_type: str,
    channel_id: Optional[str],
    message_id: str,
    user: dict,
    scope: str = "everyone",
) -> dict:
    user_id = str(user["_id"])
    try:
        oid = ObjectId(message_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid message id")
    msg = await db.chat_messages.find_one({
        "_id": oid,
        "channelType": channel_type,
        "channelId": channel_id,
    })
    if not msg:
        raise HTTPException(404, "Message not found")

    # Delete for me: hide it only from this user. Always allowed.
    if scope == "me":
        await db.chat_messages.update_one(
            {"_id": oid}, {"$addToSet": {"deletedFor": user_id}}
        )
        return {"message": "Message deleted for you"}

    # Delete for everyone: author only, within the delete window. Allowed even
    # after others have read the message.
    if msg.get("userId") != user_id:
        raise HTTPException(403, "You can only delete your own messages")
    if not _within(msg.get("createdAt"), CHAT_DELETE_WINDOW_MINUTES):
        raise HTTPException(403, "Delete window has passed")

    await db.chat_messages.update_one(
        {"_id": oid},
        {"$set": {"deleted": True, "text": "", "mentions": [],
                  "deletedAt": now_ist_naive()}},
    )
    return {"message": "Message deleted"}


async def _ensure_project_chat_access(
    project_id: str,
    user: dict,
) -> dict:
    """Caller must be HR/CEO or a current member of the project.

    Access follows project membership, so someone removed from a project
    loses the chat with it — and someone added gains it — without a separate
    list to keep in step.
    """
    try:
        oid = ObjectId(project_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid project id")

    project = await db.projects.find_one({"_id": oid})

    if not project:
        raise HTTPException(404, "Project not found")

    if user.get("role") in ("HR", "CEO"):
        return project

    if await pm.is_project_member(str(user["_id"]), project_id):
        return project

    raise HTTPException(
        403,
        "You don't have access to this chat",
    )


# ================= OFFICE CHAT =================
@office_router.get("/messages")
async def list_office_messages(
    before: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    user: dict = Depends(get_current_user_doc),
):
    return await _list_messages(
        "office", None, before, limit, user,
    )


@office_router.post("/messages")
async def post_office_message(
    data: MessageCreate,
    user: dict = Depends(get_current_user_doc),
):
    return await _insert_message(
        "office", None, data.text, data.mentions, user, data.attachments,
    )


@office_router.post("/messages/read")
async def read_office_messages(
    user: dict = Depends(get_current_user_doc),
):
    return await _mark_read("office", None, user)


@office_router.put("/messages/{messageId}")
async def edit_office_message(
    messageId: str,
    data: MessageEdit,
    user: dict = Depends(get_current_user_doc),
):
    return await _edit_message(
        "office", None, messageId, data.text, data.mentions, user,
    )


@office_router.delete("/messages/{messageId}")
async def delete_office_message(
    messageId: str,
    scope: str = Query("everyone"),
    user: dict = Depends(get_current_user_doc),
):
    return await _delete_message(
        "office", None, messageId, user, scope,
    )


# ================= TEAM CHAT =================
@project_router.get("/{projectId}/messages")
async def list_project_messages(
    projectId: str,
    before: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    user: dict = Depends(get_current_user_doc),
):
    await _ensure_project_chat_access(projectId, user)
    return await _list_messages(
        "project", projectId, before, limit, user,
    )


@project_router.post("/{projectId}/messages")
async def post_project_message(
    projectId: str,
    data: MessageCreate,
    user: dict = Depends(get_current_user_doc),
):
    await _ensure_project_chat_access(projectId, user)
    return await _insert_message(
        "project", projectId, data.text, data.mentions, user, data.attachments,
    )


@project_router.post("/{projectId}/messages/read")
async def read_project_messages(
    projectId: str,
    user: dict = Depends(get_current_user_doc),
):
    await _ensure_project_chat_access(projectId, user)
    return await _mark_read("project", projectId, user)


@project_router.put("/{projectId}/messages/{messageId}")
async def edit_project_message(
    projectId: str,
    messageId: str,
    data: MessageEdit,
    user: dict = Depends(get_current_user_doc),
):
    await _ensure_project_chat_access(projectId, user)
    return await _edit_message(
        "project", projectId, messageId, data.text, data.mentions, user,
    )


@project_router.delete("/{projectId}/messages/{messageId}")
async def delete_project_message(
    projectId: str,
    messageId: str,
    scope: str = Query("everyone"),
    user: dict = Depends(get_current_user_doc),
):
    await _ensure_project_chat_access(projectId, user)
    return await _delete_message(
        "project", projectId, messageId, user, scope,
    )


# ================= CONVERSATION LIST =================
@list_router.get("/conversations")
async def list_conversations(user: dict = Depends(get_current_user_doc)):
    """Every chat the caller can open: office plus their current projects.

    Unread is counted per channel from `chat_reads`, which already stores a
    real per-channel pointer. The older `/me/chat-unread` badge used a single
    `chatLastReadAt` on the user, so opening office chat silently marked
    project chats read too.
    """
    user_id = str(user["_id"])

    project_ids = await pm.project_ids_for_user(user_id)
    channels: list[tuple[str, Optional[str], str]] = [("office", None, "Office chat")]

    if project_ids:
        oids = []
        for pid in project_ids:
            try:
                oids.append(ObjectId(pid))
            except (InvalidId, TypeError):
                continue
        async for p in db.projects.find(
            {"_id": {"$in": oids}}, {"name": 1, "code": 1}
        ):
            channels.append(("project", str(p["_id"]), p.get("name") or "Project"))

    # One read-pointer lookup for the whole list.
    pointers: dict[tuple[str, Optional[str]], object] = {}
    async for r in db.chat_reads.find({"userId": user_id}):
        pointers[(r.get("channelType"), r.get("channelId"))] = r.get("lastReadAt")

    async for g in db.chat_groups.find(
        {"memberIds": user_id}, {"name": 1}
    ).sort("name", 1):
        channels.append(("group", str(g["_id"]), g.get("name") or "Group"))

    out = []
    for ctype, cid, name in channels:
        q: dict = {"channelType": ctype}
        q["channelId"] = cid
        last = await db.chat_messages.find_one(q, sort=[("createdAt", -1)])

        unread_q = {**q, "userId": {"$ne": user_id}}
        since = pointers.get((ctype, cid))
        if since is not None:
            unread_q["createdAt"] = {"$gt": since}
        unread = await db.chat_messages.count_documents(unread_q)

        author = None
        if last and last.get("userId"):
            basics = await _get_user_basics([last["userId"]])
            author = basics.get(last["userId"], {}).get("name")

        out.append({
            "channelType": ctype,
            "channelId": cid,
            "name": name,
            "unread": unread,
            "lastMessage": (
                {
                    "text": "" if last.get("deleted") else last.get("text", ""),
                    "authorName": author,
                    "authorId": last.get("userId"),
                    "createdAt": iso_naive(last.get("createdAt")),
                    "hasAttachments": bool(last.get("attachments")),
                }
                if last else None
            ),
        })

    # Most recent conversation first; ones with no messages sink to the bottom
    # but office chat stays pinned at the top.
    office = [r for r in out if r["channelType"] == "office"]
    others = sorted(
        [r for r in out if r["channelType"] != "office"],
        key=lambda r: (r["lastMessage"] or {}).get("createdAt") or "",
        reverse=True,
    )
    rows = office + others
    return {"conversations": rows, "totalUnread": sum(r["unread"] for r in rows)}
