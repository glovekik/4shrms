"""In-process pub/sub for real-time notification fan-out.

Single-process uvicorn assumption (matches the scheduler). For
multi-worker deployments, swap this for Redis pub/sub — the public
interface (`subscribe`, `unsubscribe`, `publish`) stays the same.
"""

import asyncio
from collections import defaultdict
from typing import Any, AsyncIterator


# user_id -> set of asyncio.Queue
_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)


def subscribe(user_id: str) -> asyncio.Queue:
    """Returns a new Queue that will receive events published to user_id."""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers[user_id].add(q)
    return q


def unsubscribe(user_id: str, q: asyncio.Queue) -> None:
    _subscribers[user_id].discard(q)
    if not _subscribers[user_id]:
        _subscribers.pop(user_id, None)


async def publish(user_id: str, event: dict[str, Any]) -> None:
    """Fan-out to every open subscriber for this user. Never raises —
    a slow consumer with a full queue drops the event for that consumer."""
    queues = list(_subscribers.get(user_id, ()))
    for q in queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Don't block the publisher; the client will reconnect.
            pass


async def stream(user_id: str) -> AsyncIterator[dict[str, Any]]:
    """Async generator yielding events for the given user until cancelled."""
    q = subscribe(user_id)
    try:
        while True:
            event = await q.get()
            yield event
    finally:
        unsubscribe(user_id, q)
