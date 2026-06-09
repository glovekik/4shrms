"""Notification helpers.

create_notification() persists an in-app row. notify_user() does both:
push + in-app, and never raises (a flaky FCM or DB write must not roll
back the surrounding business action).
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId

from database import db
from utils.push import push_to_user, push_to_users
from utils.realtime import publish as realtime_publish


# Notifications auto-expire 60 days after creation. The TTL index on
# `expiresAt` (see database.create_indexes) lets Mongo delete them in
# the background — no application code needed. 60 days is plenty of
# time for users to see and act on alerts; older entries are noise.
NOTIFICATION_TTL_DAYS = 60


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
            "expiresAt": now + timedelta(days=NOTIFICATION_TTL_DAYS),
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


async def notify_approvers(
    employee_id: str,
    type: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> None:
    """Notify an employee's approvers about a newly-submitted request.

    Recipients = the employee's reporting manager (if assigned) + every
    active HR user, deduped, never including the submitter. Used by the
    request/submit endpoints (leave, reimbursement, correction, manual
    attendance, timesheet) so approvers actually learn there's something
    pending. Best-effort: never raises.
    """
    recipient_ids: set[str] = set()

    try:
        emp = await db.users.find_one({"_id": ObjectId(employee_id)})
    except Exception:
        emp = None
    # Include the reporting manager only if they're still active — a
    # terminated manager can't act on the request, and HR (added below)
    # is the fallback approver anyway.
    mgr_id = emp.get("reportingManagerId") if emp else None
    if mgr_id:
        try:
            mgr = await db.users.find_one({"_id": ObjectId(str(mgr_id))})
        except Exception:
            mgr = None
        if mgr and mgr.get("status") != "Terminated":
            recipient_ids.add(str(mgr["_id"]))

    try:
        async for u in db.users.find(
            {"role": "HR", "status": {"$ne": "Terminated"}},
            {"_id": 1},
        ):
            recipient_ids.add(str(u["_id"]))
    except Exception as e:
        print(f"[notify] approver HR lookup failed: {e}")

    recipient_ids.discard(str(employee_id))

    for rid in recipient_ids:
        await notify_user(rid, type, title, body, data)


async def notify_hr(
    type: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> None:
    """Notify every active HR user. For events HR owns (e.g. an asset
    issue reported by an employee). Best-effort: never raises."""
    try:
        ids = [
            str(u["_id"])
            async for u in db.users.find(
                {"role": "HR", "status": {"$ne": "Terminated"}}, {"_id": 1}
            )
        ]
    except Exception as e:
        print(f"[notify] HR lookup failed: {e}")
        return
    for uid in ids:
        await notify_user(uid, type, title, body, data)


async def notify_all_active(
    type: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    exclude_id: Optional[str] = None,
) -> None:
    """Broadcast to every active (non-terminated) user — one bulk push +
    an in-app bell row each. For company-wide announcements (e.g. a newly
    declared holiday). Best-effort: never raises.
    """
    ids: list[str] = []
    try:
        async for u in db.users.find(
            {"status": {"$ne": "Terminated"}}, {"_id": 1}
        ):
            uid = str(u["_id"])
            if exclude_id and uid == exclude_id:
                continue
            ids.append(uid)
    except Exception as e:
        print(f"[notify] broadcast user lookup failed: {e}")
        return

    if not ids:
        return

    payload = (data or {}).copy()
    payload.setdefault("type", type)
    try:
        await push_to_users(ids, title, body, payload)
    except Exception as e:
        print(f"[notify] broadcast push failed: {e}")

    for uid in ids:
        await create_notification(uid, type, title, body, data)
