"""Expo Push notifications.

Best-effort: failures are logged, never raised — push isn't worth failing
the originating request over.
"""

from typing import Iterable, Optional

import httpx

from database import db


EXPO_URL = "https://exp.host/--/api/v2/push/send"


async def _send_messages(messages: list[dict]) -> None:
    if not messages:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(EXPO_URL, json=messages)
            if r.status_code >= 400:
                print(
                    f"[push] non-2xx from Expo: "
                    f"{r.status_code} {r.text[:200]}"
                )
    except Exception as e:
        print(f"[push] send failed: {e}")


def _message(token, title, body, data, channel_id, sound):
    """Build one Expo push message. channel_id routes Android notifications to
    a specific channel (its own sound + vibration); ignored on iOS."""
    msg = {
        "to": token,
        "title": title,
        "body": body,
        "sound": sound,
        "data": data or {},
    }
    if channel_id:
        msg["channelId"] = channel_id
    return msg


async def push_to_user(
    user_id: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    channel_id: Optional[str] = None,
    sound: str = "default",
) -> None:
    """Send to all of one user's registered tokens."""
    tokens = []
    async for t in db.push_tokens.find({"userId": user_id}):
        tokens.append(t.get("token"))
    tokens = [t for t in tokens if t]

    if not tokens:
        return

    payload = [
        _message(token, title, body, data, channel_id, sound)
        for token in tokens
    ]
    await _send_messages(payload)


async def push_to_users(
    user_ids: Iterable[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    channel_id: Optional[str] = None,
    sound: str = "default",
) -> None:
    """Bulk send to many users in one Expo call."""
    user_ids = list({uid for uid in user_ids if uid})
    if not user_ids:
        return

    tokens: list[str] = []
    async for t in db.push_tokens.find(
        {"userId": {"$in": user_ids}}
    ):
        if t.get("token"):
            tokens.append(t["token"])

    if not tokens:
        return

    payload = [
        _message(token, title, body, data, channel_id, sound)
        for token in tokens
    ]
    await _send_messages(payload)
