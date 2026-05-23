"""Notification helpers.

create_notification() persists an in-app row. notify_user() does both:
push + in-app, and never raises (a flaky FCM or DB write must not roll
back the surrounding business action).
"""

from datetime import datetime, timezone
from typing import Optional

from database import db
from utils.push import push_to_user
from utils.realtime import publish as realtime_publish


async def create_notification(
    user_id: str,
    type: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> None:
    """Insert an in-app notification + fan-out via SSE. Never raises."""
    if not user_id:
        return
    now = datetime.now(timezone.utc)
    inserted_id = None
    try:
        result = await db.notifications.insert_one({
            "userId": user_id,
            "type": type,
            "title": title,
            "body": body,
            "data": data or {},
            "read": False,
            "createdAt": now,
        })
        inserted_id = str(result.inserted_id)
    except Exception as e:
        print(f"[notify] in-app insert failed for {user_id}: {e}")

    # Real-time fan-out — never blocks the caller.
    try:
        await realtime_publish(
            user_id,
            {
                "type": "notification",
                "data": {
                    "id": inserted_id,
                    "type": type,
                    "title": title,
                    "body": body,
                    "data": data or {},
                    "createdAt": now.isoformat(),
                    "read": False,
                },
            },
        )
    except Exception as e:
        print(f"[notify] realtime publish failed for {user_id}: {e}")


async def notify_user(
    user_id: str,
    type: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> None:
    """Convenience: push + in-app, both best-effort."""
    payload = (data or {}).copy()
    payload.setdefault("type", type)

    try:
        await push_to_user(user_id, title, body, payload)
    except Exception as e:
        print(f"[notify] push failed for {user_id}: {e}")

    await create_notification(user_id, type, title, body, payload)
