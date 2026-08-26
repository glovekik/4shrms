"""Project lookups shared by the task routers.

Tasks may belong to a project, but they don't have to — a personal to-do or a
one-off request from a manager is legitimate work with no project behind it.
`projectId` is therefore optional everywhere; these helpers only run when one
is actually supplied.
"""

from typing import Iterable, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException

from database import db
from utils import project_members as pm


async def get_project_or_400(project_id: str) -> dict:
    try:
        oid = ObjectId(project_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, f"Invalid projectId: {project_id}")
    project = await db.projects.find_one({"_id": oid})
    if not project:
        raise HTTPException(400, "projectId references a non-existent project")
    return project


async def validate_task_project(
    project_id: Optional[str],
    assignee_id: Optional[str],
) -> Optional[dict]:
    """Check the project exists and the assignee is actually on it.

    Assigning project work to someone who isn't a member would quietly break
    the effort rollups later — the hours would belong to a project the person
    was never on. HR owns membership, so the error names that as the fix.
    """
    if not project_id:
        return None

    project = await get_project_or_400(project_id)

    if assignee_id and not await pm.is_project_member(assignee_id, project_id):
        name = project.get("name") or "this project"
        raise HTTPException(
            400,
            f"Assignee is not a member of \"{name}\". "
            "Ask HR to add them to the project first.",
        )
    return project


def brief(project: Optional[dict]) -> Optional[dict]:
    if not project:
        return None
    return {
        "id": str(project["_id"]),
        "name": project.get("name"),
        "code": project.get("code"),
        "status": project.get("status", "Active"),
    }


async def brief_map(project_ids: Iterable[Optional[str]]) -> dict[str, dict]:
    """Resolve a batch of ids to {id: brief} so task lists can show the project
    name without a round-trip per row."""
    oids = []
    for pid in {p for p in project_ids if p}:
        try:
            oids.append(ObjectId(pid))
        except (InvalidId, TypeError):
            continue
    if not oids:
        return {}
    out: dict[str, dict] = {}
    async for p in db.projects.find(
        {"_id": {"$in": oids}},
        {"name": 1, "code": 1, "status": 1},
    ):
        out[str(p["_id"])] = brief(p)
    return out
