"""Project meetings — what was discussed, decided, and who owes what.

Notes are the feature. Anyone on the project can write them up: the person
who takes the minutes is rarely the manager, and requiring approval to
record what was said is how meetings stop being recorded at all.

Editing is narrower than writing. An author can revise their own notes and
managers can fix anyone's, because a record everybody can rewrite is not a
record.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from database import db
from utils.dependencies import get_current_user_doc
from utils.audit import log_audit
from utils.ist import today_ist_str
from utils import project_members as pm
from models.project_meeting import (
    ProjectMeetingCreate,
    ProjectMeetingUpdate,
)

router = APIRouter()

MAX_NOTES_CHARS = 20_000


def _oid(value: str, label: str = "id") -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(400, f"Invalid {label}")


async def _access(project_id: str, user: dict) -> tuple[bool, bool]:
    if user.get("role") in ("HR", "CEO"):
        return True, True
    uid = str(user["_id"])
    if await pm.is_project_manager(uid, project_id):
        return True, True
    if await pm.is_project_member(uid, project_id):
        return True, False
    return False, False


async def _require(project_id: str, user: dict) -> bool:
    if not await db.projects.find_one({"_id": _oid(project_id, "project id")}):
        raise HTTPException(404, "Project not found")
    can_read, can_manage = await _access(project_id, user)
    if not can_read:
        raise HTTPException(403, "You're not a member of this project.")
    return can_manage


async def _names(ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in set(i for i in ids if i):
        try:
            u = await db.users.find_one({"_id": ObjectId(i)}, {"name": 1})
        except Exception:
            u = None
        if u:
            out[i] = u.get("name") or ""
    return out


def _serialize(m: dict, names: dict[str, str], can_edit: bool) -> dict:
    attendees = m.get("attendeeIds") or []
    return {
        "id": str(m["_id"]),
        "projectId": m.get("projectId"),
        "title": m.get("title"),
        "date": m.get("date"),
        "time": m.get("time"),
        "attendeeIds": attendees,
        "attendees": [
            {"id": a, "name": names.get(a, "Unknown")} for a in attendees
        ],
        "externalAttendees": m.get("externalAttendees") or "",
        "notes": m.get("notes") or "",
        "decisions": m.get("decisions") or "",
        "actionItems": m.get("actionItems") or "",
        "phaseId": m.get("phaseId"),
        "createdBy": m.get("createdBy"),
        "createdByName": names.get(m.get("createdBy") or "", ""),
        "createdAt": (
            m["createdAt"].isoformat() if m.get("createdAt") else None
        ),
        "updatedAt": (
            m["updatedAt"].isoformat() if m.get("updatedAt") else None
        ),
        "viewerCanEdit": can_edit,
    }


# ================= LIST =================
@router.get("/{id}/meetings")
async def list_meetings(
    id: str,
    phaseId: Optional[str] = Query(None),
    user: dict = Depends(get_current_user_doc),
):
    """Meetings newest first — the one you want is almost always the last."""
    can_manage = await _require(id, user)
    uid = str(user["_id"])

    query: dict = {"projectId": id}
    if phaseId:
        query["phaseId"] = phaseId

    rows = []
    async for m in db.project_meetings.find(query):
        rows.append(m)
    rows.sort(
        key=lambda m: (m.get("date") or "", str(m.get("createdAt") or "")),
        reverse=True,
    )

    ids = [m.get("createdBy") for m in rows]
    for m in rows:
        ids.extend(m.get("attendeeIds") or [])
    names = await _names(ids)

    return {
        "meetings": [
            _serialize(m, names, can_manage or m.get("createdBy") == uid)
            for m in rows
        ],
        "viewerCanManage": can_manage,
    }


# ================= CREATE =================
@router.post("/{id}/meetings")
async def create_meeting(
    id: str,
    data: ProjectMeetingCreate,
    user: dict = Depends(get_current_user_doc),
):
    """Any member can write up a meeting — see the module docstring."""
    await _require(id, user)

    title = (data.title or "").strip()
    if not title:
        raise HTTPException(400, "Give the meeting a title")
    for field, value in (
        ("notes", data.notes),
        ("decisions", data.decisions),
        ("actionItems", data.actionItems),
    ):
        if value and len(value) > MAX_NOTES_CHARS:
            raise HTTPException(
                400, f"{field} is too long (max {MAX_NOTES_CHARS:,} characters)"
            )

    now = datetime.now(timezone.utc)
    res = await db.project_meetings.insert_one({
        "projectId": id,
        "title": title,
        # Writing up a meeting you just left shouldn't need a date field.
        "date": data.date or today_ist_str(),
        "time": data.time,
        "attendeeIds": data.attendeeIds or [],
        "externalAttendees": (data.externalAttendees or "").strip() or None,
        "notes": data.notes or "",
        "decisions": data.decisions or "",
        "actionItems": data.actionItems or "",
        "phaseId": data.phaseId,
        "createdBy": str(user["_id"]),
        "createdAt": now,
        "updatedAt": now,
    })
    await log_audit(
        actor_id=str(user["_id"]),
        action="project_meeting.create",
        entity_type="project_meetings",
        entity_id=str(res.inserted_id),
        after={"projectId": id, "title": title},
    )
    return {"id": str(res.inserted_id), "message": "Meeting saved"}


# ================= UPDATE =================
@router.put("/{id}/meetings/{meetingId}")
async def update_meeting(
    id: str,
    meetingId: str,
    data: ProjectMeetingUpdate,
    user: dict = Depends(get_current_user_doc),
):
    can_manage = await _require(id, user)
    oid = _oid(meetingId, "meeting id")
    existing = await db.project_meetings.find_one(
        {"_id": oid, "projectId": id}
    )
    if not existing:
        raise HTTPException(404, "Meeting not found")

    # The author or a manager. Minutes everyone can rewrite aren't minutes.
    if not can_manage and existing.get("createdBy") != str(user["_id"]):
        raise HTTPException(
            403,
            "Only whoever wrote these notes, or a project manager, can "
            "edit them.",
        )

    update: dict = {"updatedAt": datetime.now(timezone.utc)}
    for field in (
        "title", "date", "time", "externalAttendees",
        "notes", "decisions", "actionItems", "phaseId",
    ):
        value = getattr(data, field)
        if value is not None:
            if field == "title":
                value = value.strip()
                if not value:
                    raise HTTPException(400, "A meeting needs a title")
            if field in ("notes", "decisions", "actionItems") and (
                len(value) > MAX_NOTES_CHARS
            ):
                raise HTTPException(400, f"{field} is too long")
            update[field] = value
    if data.attendeeIds is not None:
        update["attendeeIds"] = data.attendeeIds

    await db.project_meetings.update_one({"_id": oid}, {"$set": update})
    return {"message": "Saved"}


# ================= DELETE =================
@router.delete("/{id}/meetings/{meetingId}")
async def delete_meeting(
    id: str,
    meetingId: str,
    user: dict = Depends(get_current_user_doc),
):
    can_manage = await _require(id, user)
    oid = _oid(meetingId, "meeting id")
    existing = await db.project_meetings.find_one(
        {"_id": oid, "projectId": id}
    )
    if not existing:
        raise HTTPException(404, "Meeting not found")
    if not can_manage and existing.get("createdBy") != str(user["_id"]):
        raise HTTPException(
            403, "Only the author or a project manager can delete these notes."
        )

    await db.project_meetings.delete_one({"_id": oid})
    await log_audit(
        actor_id=str(user["_id"]),
        action="project_meeting.delete",
        entity_type="project_meetings",
        entity_id=meetingId,
        before={"projectId": id, "title": existing.get("title")},
    )
    return {"message": "Deleted"}
