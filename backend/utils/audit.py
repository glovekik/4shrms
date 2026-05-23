"""Audit log helper. Records who did what to which entity.

Why: PRD section 23 lists "audit logs everywhere" as a high-priority
recommendation, and 22 mandates them for payroll. Calls to log_audit()
never raise — an audit failure must not break the underlying action.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from database import db


async def log_audit(
    *,
    actor_id: Optional[str],
    action: str,
    entity_type: str,
    entity_id: Optional[str] = None,
    before: Optional[Any] = None,
    after: Optional[Any] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Best-effort audit log insert.

    action: short verb_object, e.g. "user.create", "leave.approve",
            "department.delete", "role.change".
    entity_type: collection name or domain noun, e.g. "users", "leave_requests".
    before/after: serializable snapshots (no ObjectIds, no datetimes-with-tz —
                  caller is responsible for converting). Optional.
    """
    try:
        doc = {
            "actorId": actor_id,
            "action": action,
            "entityType": entity_type,
            "entityId": entity_id,
            "at": datetime.now(timezone.utc),
        }
        if before is not None:
            doc["before"] = before
        if after is not None:
            doc["after"] = after
        if metadata:
            doc["metadata"] = metadata

        await db.audit_logs.insert_one(doc)
    except Exception as e:
        print(f"[audit] failed to log {action} on {entity_type}: {e}")
