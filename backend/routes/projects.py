from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from database import db
from utils.dependencies import (
    get_current_user,
    require_hr,
)
from utils.audit import log_audit
from models.project import ProjectCreate, ProjectUpdate


# /projects/...    — anyone authed (list + view)
user_router = APIRouter()
# /hr/projects/... — HR-only CRUD
hr_router = APIRouter()


def _serialize(p: dict) -> dict:
    return {
        "id": str(p["_id"]),
        "name": p.get("name"),
        "code": p.get("code"),
        "description": p.get("description"),
        "departmentId": p.get("departmentId"),
        "projectManagerIds": p.get("projectManagerIds", []),
        "memberIds": p.get("memberIds", []),
        "status": p.get("status", "Active"),
        "startDate": p.get("startDate"),
        "endDate": p.get("endDate"),
        "billable": p.get("billable", False),
    }


async def _validate_user_ids(ids: Optional[list[str]]) -> None:
    if not ids:
        return
    oids = []
    for uid in ids:
        try:
            oids.append(ObjectId(uid))
        except (InvalidId, TypeError):
            raise HTTPException(400, f"Invalid user id: {uid}")
    found = await db.users.count_documents({"_id": {"$in": oids}})
    if found != len(oids):
        raise HTTPException(400, "One or more user ids do not exist")


async def _validate_department(department_id: Optional[str]) -> None:
    if not department_id:
        return
    try:
        oid = ObjectId(department_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid departmentId")
    if not await db.departments.find_one({"_id": oid}):
        raise HTTPException(400, "departmentId references a non-existent department")


# ================= LIST (anyone authed) =================
@user_router.get("")
async def list_projects(
    status: Optional[str] = Query(None),
    _user_id: str = Depends(get_current_user),
):
    query: dict = {}
    if status:
        query["status"] = status
    out = []
    async for p in db.projects.find(query).sort("name", 1):
        out.append(_serialize(p))
    return out


@user_router.get("/{id}")
async def get_project(
    id: str,
    _user_id: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    p = await db.projects.find_one({"_id": oid})
    if not p:
        raise HTTPException(404, "Project not found")
    return _serialize(p)


# ================= HR CRUD =================
@hr_router.post("")
async def create_project(
    data: ProjectCreate,
    hr: dict = Depends(require_hr),
):
    name = (data.name or "").strip()
    code = (data.code or "").strip().upper()
    if not name or not code:
        raise HTTPException(400, "name and code are required")

    if await db.projects.find_one({"code": code}):
        raise HTTPException(400, f"Project code '{code}' already exists")

    await _validate_department(data.departmentId)
    await _validate_user_ids(data.projectManagerIds)
    await _validate_user_ids(data.memberIds)

    now = datetime.now(timezone.utc)
    doc = {
        "name": name,
        "code": code,
        "description": data.description or "",
        "departmentId": data.departmentId,
        "projectManagerIds": data.projectManagerIds or [],
        "memberIds": data.memberIds or [],
        "status": data.status or "Active",
        "startDate": data.startDate,
        "endDate": data.endDate,
        "billable": bool(data.billable),
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.projects.insert_one(doc)

    await log_audit(
        actor_id=str(hr["_id"]),
        action="project.create",
        entity_type="projects",
        entity_id=str(result.inserted_id),
        after={"name": name, "code": code},
    )
    return {"id": str(result.inserted_id), "message": "Project created"}


@hr_router.put("/{id}")
async def update_project(
    id: str,
    data: ProjectUpdate,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    existing = await db.projects.find_one({"_id": oid})
    if not existing:
        raise HTTPException(404, "Project not found")

    await _validate_department(data.departmentId)
    await _validate_user_ids(data.projectManagerIds)
    await _validate_user_ids(data.memberIds)

    update: dict = {"updatedAt": datetime.now(timezone.utc)}
    for field in (
        "name", "description", "departmentId",
        "status", "startDate", "endDate", "billable",
    ):
        v = getattr(data, field)
        if v is not None:
            update[field] = v
    if data.projectManagerIds is not None:
        update["projectManagerIds"] = data.projectManagerIds
    if data.memberIds is not None:
        update["memberIds"] = data.memberIds

    await db.projects.update_one({"_id": oid}, {"$set": update})

    await log_audit(
        actor_id=str(hr["_id"]),
        action="project.update",
        entity_type="projects",
        entity_id=id,
        after={k: v for k, v in update.items() if k != "updatedAt"},
    )
    return {"message": "Project updated"}


@hr_router.delete("/{id}")
async def delete_project(
    id: str,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    result = await db.projects.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(404, "Project not found")

    await log_audit(
        actor_id=str(hr["_id"]),
        action="project.delete",
        entity_type="projects",
        entity_id=id,
    )
    return {"message": "Project deleted"}
