"""Audit log helper. Records who did what to which entity.

Why: PRD section 23 lists "audit logs everywhere" as a high-priority
recommendation, and 22 mandates them for payroll. Calls to log_audit()
never raise — an audit failure must not break the underlying action.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from database import db


# Audit rows auto-expire after 2 days via a TTL index on `expiresAt`
# (see database.create_indexes). Tight retention during the pilot to
# keep Mongo footprint small. For real compliance / investigation
# needs (3+ months), bump this back up — recommended values are 90
# (default) or 365 (annual reviews).
AUDIT_TTL_DAYS = 2


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
        now = datetime.now(timezone.utc)
        doc = {
            "actorId": actor_id,
            "action": action,
            "entityType": entity_type,
            "entityId": entity_id,
            "at": now,
            "expiresAt": now + timedelta(days=AUDIT_TTL_DAYS),
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
