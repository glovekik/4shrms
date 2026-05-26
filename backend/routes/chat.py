from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone

from typing import Optional

from database import db
from utils.dependencies import get_current_user_doc
from utils.notify import notify_user
from utils.push import push_to_users
from utils.realtime import publish as realtime_publish
from models.message import MessageCreate

# Two routers, one helper set. Office and team chat are structurally identical
# — `channelType` + `channelId` discriminate which channel a message belongs to.
office_router = APIRouter()
team_router = APIRouter()


# ================= SERIALIZER =================
def _serialize_message(
    m: dict,
    user_info: Optional[dict] = None,
) -> dict:
    return {
        "id": str(m["_id"]),
        "userId": m.get("userId"),
        "user": user_info,
        "text": m.get("text", ""),
        "mentions": m.get("mentions", []),
        "createdAt": (
            m["createdAt"].isoformat()
            if m.get("createdAt") else None
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


async def _list_messages(
    channel_type: str,
    channel_id: Optional[str],
    before: Optional[str],
    limit: int,
) -> list[dict]:
    query: dict = {
        "channelType": channel_type,
        "channelId": channel_id,
    }

    before_dt = _parse_before(before)
    if before_dt:
        query["createdAt"] = {"$lt": before_dt}

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

    user_map = await _get_user_basics(
        m.get("userId") for m in raw
    )

    return [
        _serialize_message(
            m,
            user_map.get(m.get("userId")),
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
) -> dict:
    text = text.strip()

    if not text:
        raise HTTPException(
            400,
            "Message text required",
        )

    user_id = str(user["_id"])
    now = datetime.now(timezone.utc)

    resolved_mentions = await _validate_mentions(mentions)
    # Don't notify the author for self-mentions.
    resolved_mentions = [m for m in resolved_mentions if m != user_id]

    msg = {
        "channelType": channel_type,
        "channelId": channel_id,
        "userId": user_id,
        "text": text,
        "mentions": resolved_mentions,
        "createdAt": now,
    }

    result = await db.chat_messages.insert_one(msg)
    msg["_id"] = result.inserted_id

    user_info = {
        "id": user_id,
        "name": user.get("name"),
        "email": user.get("email"),
    }

    author_name = user.get("name") or "Someone"
    snippet = text if len(text) <= 140 else text[:137] + "..."

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

    # Office chat (company-wide) — push notification + lightweight SSE
    # tick to every authenticated user except the author and already-
    # mentioned users. No bell notification row is written (the bell
    # stays mention-only). The push gives an OS-level alert on real
    # devices; the SSE tick drives the dashboard chat-unread badge to
    # refresh live. realtime_publish is a no-op for users with no open
    # SSE subscriber, so the cost scales with connected users, not
    # total users in the org.
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
            try:
                await push_to_users(
                    recipient_ids,
                    f"{author_name} · Office chat",
                    snippet,
                    {
                        "type": "chat_message",
                        "channelType": "office",
                        "channelId": None,
                        "messageId": str(msg["_id"]),
                    },
                )
            except Exception:
                pass

        for rid in recipient_ids:
            try:
                await realtime_publish(
                    rid,
                    {
                        "type": "notification",
                        "data": {
                            "type": "chat_message",
                            "channelType": "office",
                            "channelId": None,
                            "messageId": str(msg["_id"]),
                            "authorId": user_id,
                        },
                    },
                )
            except Exception:
                pass

    # Team messages ping the rest of the team with a PUSH only — they are
    # deliberately NOT added to the in-app notification bell (chat activity
    # would flood it; the dashboard Chat tile badge tracks unread instead).
    # The bell is reserved for @mentions, handled above. Office chat is
    # company-wide, so it stays mention-only. Mentioned members already got
    # a full notification above — don't double-notify them here.
    #
    # We DO fan out a lightweight realtime "chat_message" event so the
    # dashboard's Chat tile badge updates live on every connected client
    # without polling. The event carries no in-app notification row.
    if channel_type == "team" and channel_id:
        try:
            team = await db.teams.find_one({"_id": ObjectId(channel_id)})
        except (InvalidId, TypeError):
            team = None
        if team:
            recipients = set(team.get("memberIds", []) or [])
            if team.get("teamLeadId"):
                recipients.add(team["teamLeadId"])
            recipients.discard(user_id)
            recipients.difference_update(resolved_mentions)
            if recipients:
                try:
                    await push_to_users(
                        recipients,
                        f"{author_name} · {team.get('name') or 'Team chat'}",
                        snippet,
                        {
                            "type": "chat_message",
                            "channelType": "team",
                            "channelId": channel_id,
                            "messageId": str(msg["_id"]),
                        },
                    )
                except Exception:
                    pass

                # Realtime tick — drives the dashboard's chat-unread badge
                # and lets open chat screens reload without waiting for the
                # 3s poll. Best-effort.
                for rid in recipients:
                    try:
                        await realtime_publish(
                            rid,
                            {
                                "type": "notification",
                                "data": {
                                    "type": "chat_message",
                                    "channelType": "team",
                                    "channelId": channel_id,
                                    "messageId": str(msg["_id"]),
                                    "authorId": user_id,
                                },
                            },
                        )
                    except Exception:
                        pass

    return _serialize_message(msg, user_info)


async def _delete_message(
    channel_type: str,
    channel_id: Optional[str],
    message_id: str,
    user: dict,
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
        raise HTTPException(
            403,
            "You can only delete your own messages",
        )

    await db.chat_messages.delete_one({"_id": oid})

    return {"message": "Message deleted"}


async def _ensure_team_chat_access(
    team_id: str,
    user: dict,
) -> dict:
    """Caller must be HR, the team's TL, or in memberIds."""
    try:
        oid = ObjectId(team_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid team id")

    team = await db.teams.find_one({"_id": oid})

    if not team:
        raise HTTPException(404, "Team not found")

    user_id = str(user["_id"])

    if user.get("role") == "HR":
        return team

    if team.get("teamLeadId") == user_id:
        return team

    if user_id in team.get("memberIds", []):
        return team

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
        "office", None, before, limit,
    )


@office_router.post("/messages")
async def post_office_message(
    data: MessageCreate,
    user: dict = Depends(get_current_user_doc),
):
    return await _insert_message(
        "office", None, data.text, data.mentions, user,
    )


@office_router.delete("/messages/{messageId}")
async def delete_office_message(
    messageId: str,
    user: dict = Depends(get_current_user_doc),
):
    return await _delete_message(
        "office", None, messageId, user,
    )


# ================= TEAM CHAT =================
@team_router.get("/{teamId}/messages")
async def list_team_messages(
    teamId: str,
    before: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    user: dict = Depends(get_current_user_doc),
):
    await _ensure_team_chat_access(teamId, user)
    return await _list_messages(
        "team", teamId, before, limit,
    )


@team_router.post("/{teamId}/messages")
async def post_team_message(
    teamId: str,
    data: MessageCreate,
    user: dict = Depends(get_current_user_doc),
):
    await _ensure_team_chat_access(teamId, user)
    return await _insert_message(
        "team", teamId, data.text, data.mentions, user,
    )


@team_router.delete("/{teamId}/messages/{messageId}")
async def delete_team_message(
    teamId: str,
    messageId: str,
    user: dict = Depends(get_current_user_doc),
):
    await _ensure_team_chat_access(teamId, user)
    return await _delete_message(
        "team", teamId, messageId, user,
    )
