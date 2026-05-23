from fastapi import APIRouter, Depends, HTTPException

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
from models.department import DepartmentCreate, DepartmentUpdate


# Anyone authed can list departments (for dropdowns); HR-only for CRUD.
user_router = APIRouter()
hr_router = APIRouter()


def _serialize(d: dict) -> dict:
    return {
        "id": str(d["_id"]),
        "name": d.get("name"),
        "description": d.get("description"),
        "headUserId": d.get("headUserId"),
    }


async def _validate_head(head_user_id: Optional[str]) -> None:
    if not head_user_id:
        return
    try:
        oid = ObjectId(head_user_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid headUserId")

    user = await db.users.find_one({"_id": oid})
    if not user:
        raise HTTPException(400, "headUserId references a non-existent user")


# ================= LIST (anyone authed) =================
@user_router.get("")
async def list_departments(
    _user_id: str = Depends(get_current_user),
):
    out = []
    async for d in db.departments.find().sort("name", 1):
        out.append(_serialize(d))
    return out


# ================= HR CRUD =================
@hr_router.post("")
async def create_department(
    data: DepartmentCreate,
    hr: dict = Depends(require_hr),
):
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(400, "Department name is required")

    existing = await db.departments.find_one({"name": name})
    if existing:
        raise HTTPException(400, f"Department '{name}' already exists")

    await _validate_head(data.headUserId)

    now = datetime.now(timezone.utc)
    doc = {
        "name": name,
        "description": data.description or "",
        "headUserId": data.headUserId,
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.departments.insert_one(doc)

    await log_audit(
        actor_id=str(hr["_id"]),
        action="department.create",
        entity_type="departments",
        entity_id=str(result.inserted_id),
        after={"name": name, "headUserId": data.headUserId},
    )

    return {"id": str(result.inserted_id), "message": "Department created"}


@hr_router.get("/{id}")
async def get_department(
    id: str,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    d = await db.departments.find_one({"_id": oid})
    if not d:
        raise HTTPException(404, "Department not found")
    return _serialize(d)


@hr_router.put("/{id}")
async def update_department(
    id: str,
    data: DepartmentUpdate,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    existing = await db.departments.find_one({"_id": oid})
    if not existing:
        raise HTTPException(404, "Department not found")

    update: dict = {"updatedAt": datetime.now(timezone.utc)}

    if data.name is not None:
        new_name = data.name.strip()
        if not new_name:
            raise HTTPException(400, "Department name cannot be empty")
        clash = await db.departments.find_one({
            "name": new_name,
            "_id": {"$ne": oid},
        })
        if clash:
            raise HTTPException(400, f"Department '{new_name}' already exists")
        update["name"] = new_name

    if data.description is not None:
        update["description"] = data.description

    if data.headUserId is not None:
        # Empty string clears it.
        if data.headUserId == "":
            update["headUserId"] = None
        else:
            await _validate_head(data.headUserId)
            update["headUserId"] = data.headUserId

    await db.departments.update_one({"_id": oid}, {"$set": update})

    await log_audit(
        actor_id=str(hr["_id"]),
        action="department.update",
        entity_type="departments",
        entity_id=id,
        before={
            "name": existing.get("name"),
            "headUserId": existing.get("headUserId"),
        },
        after=update,
    )

    return {"message": "Department updated"}


@hr_router.delete("/{id}")
async def delete_department(
    id: str,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    # Refuse to delete a department that still has employees.
    in_use = await db.users.count_documents({"departmentId": id})
    if in_use:
        raise HTTPException(
            400,
            f"Cannot delete — {in_use} user(s) still belong to this department",
        )

    existing = await db.departments.find_one({"_id": oid})
    result = await db.departments.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(404, "Department not found")

    await log_audit(
        actor_id=str(hr["_id"]),
        action="department.delete",
        entity_type="departments",
        entity_id=id,
        before=_serialize(existing) if existing else None,
    )

    return {"message": "Department deleted"}
