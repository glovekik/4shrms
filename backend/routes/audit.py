from fastapi import APIRouter, Depends, Query, HTTPException

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime
from typing import Optional

from database import db
from utils.dependencies import require_hr

router = APIRouter()


def _serialize(log: dict) -> dict:
    return {
        "id": str(log["_id"]),
        "actorId": log.get("actorId"),
        "action": log.get("action"),
        "entityType": log.get("entityType"),
        "entityId": log.get("entityId"),
        "at": log["at"].isoformat() if log.get("at") else None,
        "before": log.get("before"),
        "after": log.get("after"),
        "metadata": log.get("metadata"),
    }


def _parse_iso(value: str, field: str) -> datetime:
    s = value
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        raise HTTPException(400, f"Invalid {field} (use ISO 8601)")


@router.get("")
async def list_audit_logs(
    actorId: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    entityType: Optional[str] = Query(None),
    entityId: Optional[str] = Query(None),
    fromDate: Optional[str] = Query(None),  # ISO 8601
    toDate: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _hr: dict = Depends(require_hr),
):
    """HR-only audit-log viewer. Newest first. All filters optional."""
    query: dict = {}
    if actorId:
        query["actorId"] = actorId
    if action:
        query["action"] = action
    if entityType:
        query["entityType"] = entityType
    if entityId:
        query["entityId"] = entityId

    if fromDate or toDate:
        date_q: dict = {}
        if fromDate:
            date_q["$gte"] = _parse_iso(fromDate, "fromDate")
        if toDate:
            date_q["$lte"] = _parse_iso(toDate, "toDate")
        query["at"] = date_q

    out = []
    cursor = db.audit_logs.find(query).sort("at", -1).limit(limit)
    async for log in cursor:
        out.append(_serialize(log))
    return out
