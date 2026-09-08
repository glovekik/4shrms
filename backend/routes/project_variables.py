"""Project Variables — reusable snippets a project manager stores for the team.

A place for the connection string, the boilerplate config, the curl command
everyone keeps asking for in chat. The manager writes it once; members open it
and copy it.

Permissions are per file, not per project, because these hold exactly the kind
of thing that shouldn't be uniformly readable — a staging credential is for the
two people wiring it up, the API scaffold is for everyone.

    managers  the project's managers only
    selected  the managers plus named members
    team      everyone currently on the project

HR and the CEO can see everything, consistent with the rest of the project
routes. Reads are checked per file rather than filtered in the UI: hiding a
row in a list is not access control.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from database import db
from utils.dependencies import get_current_user_doc
from utils.audit import log_audit
from utils import project_members as pm
from models.project_variable import (
    ProjectVariableCreate,
    ProjectVariableUpdate,
)

router = APIRouter()

# A snippet, not a file upload. Large enough for a real config or schema,
# small enough that the collection stays a collection of snippets.
MAX_CONTENT_CHARS = 100_000


def _oid(value: str, label: str = "id") -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(400, f"Invalid {label}")


async def _load_project(project_id: str) -> dict:
    p = await db.projects.find_one({"_id": _oid(project_id, "project id")})
    if not p:
        raise HTTPException(404, "Project not found")
    return p


def _is_admin(user: dict) -> bool:
    return user.get("role") in ("HR", "CEO")


async def _access(project_id: str, user: dict) -> tuple[bool, bool]:
    """(can_read_project, can_manage) for this user on this project.

    Managing means creating and editing variables — the same bar as assigning
    tasks, so a project manager needs no extra grant.
    """
    if _is_admin(user):
        return True, True
    user_id = str(user["_id"])
    if await pm.is_project_manager(user_id, project_id):
        return True, True
    if await pm.is_project_member(user_id, project_id):
        return True, False
    return False, False


def _may_read(var: dict, user: dict, is_manager: bool) -> bool:
    if _is_admin(user) or is_manager:
        return True
    vis = var.get("visibility", "team")
    if vis == "team":
        return True
    if vis == "selected":
        return str(user["_id"]) in (var.get("allowedUserIds") or [])
    return False  # "managers"


def _serialize(var: dict, *, include_content: bool, can_manage: bool) -> dict:
    out = {
        "id": str(var["_id"]),
        "projectId": var.get("projectId"),
        "fileName": var.get("fileName"),
        "language": var.get("language"),
        "description": var.get("description") or "",
        "visibility": var.get("visibility", "team"),
        "createdBy": var.get("createdBy"),
        "createdAt": (
            var["createdAt"].isoformat() if var.get("createdAt") else None
        ),
        "updatedAt": (
            var["updatedAt"].isoformat() if var.get("updatedAt") else None
        ),
        # Lets the list show "128 lines" without shipping every byte.
        "lineCount": len((var.get("content") or "").splitlines()),
        "viewerCanManage": can_manage,
    }
    # allowedUserIds is a permission detail; only whoever can change it needs
    # to see it.
    if can_manage:
        out["allowedUserIds"] = var.get("allowedUserIds") or []
    if include_content:
        out["content"] = var.get("content") or ""
    return out


async def _validate_allowed(project_id: str, ids: Optional[list[str]]) -> list[str]:
    """Named viewers must actually be on the project."""
    if not ids:
        return []
    roster = set(await pm.current_member_ids(project_id))
    bad = [i for i in ids if i not in roster]
    if bad:
        raise HTTPException(
            400,
            "Some selected people are not on this project. Ask HR to add "
            "them first, or choose someone else.",
        )
    return list(dict.fromkeys(ids))


# ================= LIST =================
@router.get("/{projectId}/variables")
async def list_variables(
    projectId: str,
    user: dict = Depends(get_current_user_doc),
):
    """Every variable this user may read. Content is omitted from the list —
    it is fetched when a file is opened, so a long snippet doesn't inflate
    the index and a restricted one never travels at all."""
    await _load_project(projectId)
    can_read, can_manage = await _access(projectId, user)
    if not can_read:
        raise HTTPException(403, "You're not a member of this project.")

    out = []
    async for v in db.project_variables.find(
        {"projectId": projectId}
    ).sort("fileName", 1):
        if _may_read(v, user, can_manage):
            out.append(_serialize(v, include_content=False, can_manage=can_manage))
    return {"variables": out, "viewerCanManage": can_manage}


# ================= READ ONE =================
@router.get("/{projectId}/variables/{variableId}")
async def get_variable(
    projectId: str,
    variableId: str,
    user: dict = Depends(get_current_user_doc),
):
    await _load_project(projectId)
    can_read, can_manage = await _access(projectId, user)
    if not can_read:
        raise HTTPException(403, "You're not a member of this project.")

    var = await db.project_variables.find_one(
        {"_id": _oid(variableId, "variable id"), "projectId": projectId}
    )
    if not var:
        raise HTTPException(404, "Variable not found")
    if not _may_read(var, user, can_manage):
        # 403 rather than 404: the person is on the project and can see the
        # file exists in the list, so pretending it doesn't would be a lie
        # they could disprove.
        raise HTTPException(403, "You don't have access to this file.")
    return _serialize(var, include_content=True, can_manage=can_manage)


# ================= CREATE =================
@router.post("/{projectId}/variables")
async def create_variable(
    projectId: str,
    data: ProjectVariableCreate,
    user: dict = Depends(get_current_user_doc),
):
    await _load_project(projectId)
    _, can_manage = await _access(projectId, user)
    if not can_manage:
        raise HTTPException(
            403, "Only this project's managers can add variables."
        )

    name = (data.fileName or "").strip()
    if not name:
        raise HTTPException(400, "File name is required")
    if len(data.content or "") > MAX_CONTENT_CHARS:
        raise HTTPException(
            400, f"Content is too long (max {MAX_CONTENT_CHARS:,} characters)"
        )
    if await db.project_variables.find_one(
        {"projectId": projectId, "fileName": name}
    ):
        raise HTTPException(
            400, f"A file named \"{name}\" already exists on this project."
        )

    allowed = (
        await _validate_allowed(projectId, data.allowedUserIds)
        if data.visibility == "selected" else []
    )
    if data.visibility == "selected" and not allowed:
        raise HTTPException(
            400, "Choose at least one person, or change who can see this."
        )

    now = datetime.now(timezone.utc)
    actor_id = str(user["_id"])
    result = await db.project_variables.insert_one({
        "projectId": projectId,
        "fileName": name,
        "content": data.content or "",
        "language": (data.language or "").strip() or None,
        "description": (data.description or "").strip() or None,
        "visibility": data.visibility,
        "allowedUserIds": allowed,
        "createdBy": actor_id,
        "createdAt": now,
        "updatedAt": now,
    })
    await log_audit(
        actor_id=actor_id,
        action="project_variable.create",
        entity_type="project_variables",
        entity_id=str(result.inserted_id),
        # Never the content — these hold credentials by design.
        after={"projectId": projectId, "fileName": name,
               "visibility": data.visibility},
    )
    return {"id": str(result.inserted_id), "message": "Saved"}


# ================= UPDATE =================
@router.put("/{projectId}/variables/{variableId}")
async def update_variable(
    projectId: str,
    variableId: str,
    data: ProjectVariableUpdate,
    user: dict = Depends(get_current_user_doc),
):
    await _load_project(projectId)
    _, can_manage = await _access(projectId, user)
    if not can_manage:
        raise HTTPException(
            403, "Only this project's managers can edit variables."
        )

    oid = _oid(variableId, "variable id")
    existing = await db.project_variables.find_one(
        {"_id": oid, "projectId": projectId}
    )
    if not existing:
        raise HTTPException(404, "Variable not found")

    update: dict = {"updatedAt": datetime.now(timezone.utc)}

    if data.fileName is not None:
        name = data.fileName.strip()
        if not name:
            raise HTTPException(400, "File name cannot be empty")
        clash = await db.project_variables.find_one(
            {"projectId": projectId, "fileName": name, "_id": {"$ne": oid}}
        )
        if clash:
            raise HTTPException(
                400, f"A file named \"{name}\" already exists on this project."
            )
        update["fileName"] = name

    if data.content is not None:
        if len(data.content) > MAX_CONTENT_CHARS:
            raise HTTPException(
                400,
                f"Content is too long (max {MAX_CONTENT_CHARS:,} characters)",
            )
        update["content"] = data.content

    if data.language is not None:
        update["language"] = data.language.strip() or None
    if data.description is not None:
        update["description"] = data.description.strip() or None

    # Visibility and its allow-list move together: switching to "selected"
    # with nobody named would hide the file from everyone but the managers,
    # which is what "managers" already means.
    vis = data.visibility or existing.get("visibility", "team")
    if data.visibility is not None:
        update["visibility"] = data.visibility
    if vis == "selected":
        ids = (
            data.allowedUserIds
            if data.allowedUserIds is not None
            else existing.get("allowedUserIds")
        )
        allowed = await _validate_allowed(projectId, ids)
        if not allowed:
            raise HTTPException(
                400, "Choose at least one person, or change who can see this."
            )
        update["allowedUserIds"] = allowed
    elif data.visibility is not None:
        update["allowedUserIds"] = []

    await db.project_variables.update_one({"_id": oid}, {"$set": update})
    await log_audit(
        actor_id=str(user["_id"]),
        action="project_variable.update",
        entity_type="project_variables",
        entity_id=variableId,
        after={k: v for k, v in update.items() if k != "content"},
    )
    return {"message": "Saved"}


# ================= DELETE =================
@router.delete("/{projectId}/variables/{variableId}")
async def delete_variable(
    projectId: str,
    variableId: str,
    user: dict = Depends(get_current_user_doc),
):
    await _load_project(projectId)
    _, can_manage = await _access(projectId, user)
    if not can_manage:
        raise HTTPException(
            403, "Only this project's managers can delete variables."
        )

    oid = _oid(variableId, "variable id")
    existing = await db.project_variables.find_one(
        {"_id": oid, "projectId": projectId}
    )
    if not existing:
        raise HTTPException(404, "Variable not found")

    await db.project_variables.delete_one({"_id": oid})
    await log_audit(
        actor_id=str(user["_id"]),
        action="project_variable.delete",
        entity_type="project_variables",
        entity_id=variableId,
        before={"projectId": projectId, "fileName": existing.get("fileName")},
    )
    return {"message": "Deleted"}
