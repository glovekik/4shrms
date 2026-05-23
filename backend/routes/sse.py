"""Server-Sent Events stream of in-app notifications.

A long-lived GET /sse/notifications connection that yields one event per
new notification for the authed user. Sends a comment heartbeat every
20 seconds so proxies and clients don't close the connection.
"""

import asyncio
import json
from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from utils.dependencies import get_current_user
from utils.realtime import subscribe, unsubscribe


router = APIRouter()

HEARTBEAT_SECONDS = 20


@router.get("/notifications")
async def sse_notifications(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Streams real-time notifications for the current user.

    Event types:
      - "notification" — payload is the notification doc as JSON
      - heartbeat ":\n\n" lines keep the connection alive
    """
    queue = subscribe(user_id)

    async def event_gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_SECONDS,
                    )
                    yield {
                        "event": event.get("type", "notification"),
                        "data": json.dumps(event.get("data", {})),
                    }
                except asyncio.TimeoutError:
                    # Comment line — keeps the connection alive without
                    # delivering a real event.
                    yield {"comment": "heartbeat"}
        finally:
            unsubscribe(user_id, queue)

    return EventSourceResponse(event_gen())
