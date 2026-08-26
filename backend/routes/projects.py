from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from database import db
from utils.dependencies import (
    get_current_user,
    get_current_user_doc,
    require_hr,
)
from utils.audit import log_audit
from utils.notify import create_notification, notify_user
from utils.push import push_to_user
from utils.ist import now_ist_naive
from models.task import TaskCreate, TaskUpdate
from utils import projects as projects_util
from utils import project_members as pm
from models.project import ProjectCreate, ProjectUpdate


# /projects/...    — anyone authed (list + view)
user_router = APIRouter()
# /hr/projects/... — HR-only CRUD
hr_router = APIRouter()


def _serialize(p: dict, roster: Optional[dict] = None) -> dict:
    """`managerIds`/`memberIds` come from the membership collection, not the
    project doc — the doc no longer stores them."""
    roster = roster or {"managerIds": [], "memberIds": []}
    return {
        "id": str(p["_id"]),
        "name": p.get("name"),
        "code": p.get("code"),
        "description": p.get("description"),
        "departmentId": p.get("departmentId"),
        "managerIds": roster["managerIds"],
        # Legacy key, still read by older app builds.
        "projectManagerIds": roster["managerIds"],
        "memberIds": roster["memberIds"],
        "status": p.get("status", "Active"),
        "startDate": p.get("startDate"),
        "endDate": p.get("endDate"),
    }


async def _user_map(user_ids: set[str]) -> dict[str, dict]:
    """Minimal public identity for roster display."""
    oids = []
    for uid in user_ids:
        try:
            oids.append(ObjectId(uid))
        except (InvalidId, TypeError):
            continue
    out: dict[str, dict] = {}
    if not oids:
        return out
    async for u in db.users.find(
        {"_id": {"$in": oids}},
        {"name": 1, "profilePictureUrl": 1, "employeeCode": 1, "work.jobTitle": 1},
    ):
        out[str(u["_id"])] = {
            "id": str(u["_id"]),
            "name": u.get("name"),
            "profilePictureUrl": u.get("profilePictureUrl"),
            "employeeCode": u.get("employeeCode"),
            "jobTitle": (u.get("work") or {}).get("jobTitle"),
        }
    return out


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


def _oid(id: str) -> ObjectId:
    try:
        return ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")


async def _load(id: str) -> dict:
    p = await db.projects.find_one({"_id": _oid(id)})
    if not p:
        raise HTTPException(404, "Project not found")
    return p


async def _serialize_many(
    docs: list[dict],
    *,
    viewer_id: Optional[str] = None,
    viewer_is_hr: bool = False,
) -> list[dict]:
    """One roster query per project — fine at this scale, and avoids the
    denormalised member list that had already drifted out of sync.

    `viewerIsManager` lets the app show PM controls without a second call.
    """
    managed: set[str] = set()
    if viewer_id and not viewer_is_hr:
        managed = set(await pm.managed_project_ids(viewer_id))
    out = []
    for p in docs:
        pid = str(p["_id"])
        row = _serialize(p, await pm.roster(pid))
        row["viewerIsManager"] = viewer_is_hr or pid in managed
        out.append(row)
    return out


# ================= LIST (anyone authed) =================
@user_router.get("")
async def list_projects(
    status: Optional[str] = Query(None),
    user: dict = Depends(get_current_user_doc),
):
    query: dict = {}
    if status:
        query["status"] = status
    docs = [p async for p in db.projects.find(query).sort("name", 1)]
    return await _serialize_many(
        docs,
        viewer_id=str(user["_id"]),
        viewer_is_hr=user.get("role") in ("HR", "CEO"),
    )


@user_router.get("/mine")
async def my_projects(
    include_past: bool = Query(False),
    user: dict = Depends(get_current_user_doc),
):
    """Projects the caller is currently on (or has ever been on)."""
    user_id = str(user["_id"])
    ids = await pm.project_ids_for_user(user_id, include_past=include_past)
    if not ids:
        return []
    oids = []
    for pid in set(ids):
        try:
            oids.append(ObjectId(pid))
        except (InvalidId, TypeError):
            continue
    docs = [p async for p in db.projects.find({"_id": {"$in": oids}}).sort("name", 1)]
    return await _serialize_many(
        docs,
        viewer_id=user_id,
        viewer_is_hr=user.get("role") in ("HR", "CEO"),
    )


@user_router.get("/{id}")
async def get_project(
    id: str,
    user: dict = Depends(get_current_user_doc),
):
    p = await _load(id)
    out = _serialize(p, await pm.roster(id))
    out["viewerIsManager"] = (
        user.get("role") in ("HR", "CEO")
        or await pm.is_project_manager(str(user["_id"]), id)
    )
    return out


# ================= MEMBERS =================
@user_router.get("/{id}/members")
async def project_members(
    id: str,
    asOf: Optional[str] = Query(None, description="YYYY-MM-DD; omit for today"),
    _user_id: str = Depends(get_current_user),
):
    """Current roster, or the roster as it stood on `asOf`."""
    await _load(id)
    rows = await pm.members_as_of(id, asOf) if asOf else await pm.current_members(id)
    umap = await _user_map({r["userId"] for r in rows})
    return {
        "asOf": asOf,
        "members": [
            {**pm.serialize(r), "user": umap.get(r["userId"])} for r in rows
        ],
    }


@user_router.get("/{id}/members/history")
async def project_member_history(
    id: str,
    user: dict = Depends(get_current_user_doc),
):
    """Every stay ever, newest first — who joined when, who left when.

    Everyone can see the timeline; only HR sees *who made* each change.
    Knowing you were removed from a project is fine — knowing which HR person
    did it invites conversations that aren't the app's to start.
    """
    await _load(id)
    rows = await pm.history(id)
    is_hr = user.get("role") in ("HR", "CEO")

    known = {r["userId"] for r in rows}
    if is_hr:
        known |= {
            a
            for r in rows
            for a in (r.get("addedBy"), r.get("removedBy"))
            if a
        }
    umap = await _user_map(known)

    out = []
    for r in rows:
        entry = {**pm.serialize(r), "user": umap.get(r["userId"])}
        if is_hr:
            entry["addedByUser"] = umap.get(r.get("addedBy"))
            entry["removedByUser"] = umap.get(r.get("removedBy"))
        else:
            entry.pop("addedBy", None)
            entry.pop("removedBy", None)
        out.append(entry)
    return {"history": out, "showsActors": is_hr}


# ================= PM PERMISSION =================
async def _require_pm(user: dict, project_id: str) -> str:
    """Project-manager access, derived from membership rather than a role.

    A project manager is whoever currently holds a `manager` stay on this
    project — they need no global MANAGER role, and their powers stop at this
    project's edge. HR is allowed everywhere.
    """
    actor_id = str(user["_id"])
    if user.get("role") in ("HR", "CEO"):
        return actor_id
    if await pm.is_project_manager(actor_id, project_id):
        return actor_id
    raise HTTPException(403, "You are not a manager of this project")


# ================= TASKS =================
@user_router.get("/{id}/tasks")
async def project_tasks(
    id: str,
    status: Optional[str] = Query(None, description="PENDING | ONGOING | COMPLETED"),
    assigneeId: Optional[str] = Query(None),
    user: dict = Depends(get_current_user_doc),
):
    """Every task on the project, with a status breakdown for the board.

    Members only. The roster is deliberately company-wide (the org chart shows
    who works on what), but task titles and descriptions are the work itself —
    "Migrate the payroll export", "Fix the client's failing audit" — and any
    signed-in employee could read every one of them on every project.
    """
    await _load(id)
    if user.get("role") not in ("HR", "CEO"):
        if not await pm.is_project_member(str(user["_id"]), id):
            raise HTTPException(
                403, "You're not a member of this project."
            )

    query: dict = {"projectId": id}
    if status:
        query["status"] = status
    if assigneeId:
        query["assigneeId"] = assigneeId

    raw = [t async for t in db.tasks.find(query).sort("createdAt", -1)]

    umap = await _user_map(
        {t.get("assigneeId") for t in raw if t.get("assigneeId")}
        | {t.get("createdBy") for t in raw if t.get("createdBy")}
    )

    tasks = []
    for t in raw:
        tasks.append({
            "id": str(t["_id"]),
            "projectId": t.get("projectId"),
            "title": t.get("title"),
            "description": t.get("description", ""),
            "status": t.get("status"),
            "priority": t.get("priority", "MEDIUM"),
            "dueDate": t.get("dueDate"),
            "assigneeId": t.get("assigneeId"),
            "assignee": umap.get(t.get("assigneeId")),
            "createdBy": t.get("createdBy"),
            "createdByUser": umap.get(t.get("createdBy")),
            "createdAt": (
                t["createdAt"].isoformat() if t.get("createdAt") else None
            ),
            "completedAt": (
                t["completedAt"].isoformat() if t.get("completedAt") else None
            ),
        })

    # Counts come from the whole project, not the filtered slice — otherwise
    # filtering to PENDING would show "PENDING 4, ONGOING 0, COMPLETED 0".
    counts = {"PENDING": 0, "ONGOING": 0, "COMPLETED": 0}
    async for t in db.tasks.find({"projectId": id}, {"status": 1}):
        st = t.get("status")
        if st in counts:
            counts[st] += 1

    return {"tasks": tasks, "counts": counts, "total": sum(counts.values())}


@user_router.post("/{id}/tasks")
async def create_project_task(
    id: str,
    data: TaskCreate,
    user: dict = Depends(get_current_user_doc),
):
    """A project manager assigns work on their own project."""
    project = await _load(id)
    actor_id = await _require_pm(user, id)

    if not data.assigneeId:
        raise HTTPException(400, "assigneeId is required")
    if not await pm.is_project_member(data.assigneeId, id):
        raise HTTPException(
            400,
            f"Assignee is not a member of \"{project.get('name')}\". "
            "Ask HR to add them to the project first.",
        )

    now = datetime.now(timezone.utc)
    task = {
        "teamId": None,
        "projectId": id,
        "title": data.title,
        "description": data.description or "",
        "assigneeId": data.assigneeId,
        "createdBy": actor_id,
        "createdByRole": user.get("role"),
        "status": "PENDING",
        "priority": data.priority or "MEDIUM",
        "reminderIntervalMinutes": data.reminderIntervalMinutes,
        "dueDate": data.dueDate,
        "attachments": data.attachments or [],
        "createdAt": now,
        "updatedAt": now,
        "startedAt": None,
        "completedAt": None,
    }
    result = await db.tasks.insert_one(task)
    task_id = str(result.inserted_id)

    try:
        await push_to_user(
            data.assigneeId,
            "New task",
            data.title,
            {"type": "task_assigned", "taskId": task_id, "projectId": id},
        )
    except Exception:
        pass
    await create_notification(
        data.assigneeId,
        "task_assigned",
        f"New task · {project.get('name')}",
        data.title,
        {
            "taskId": task_id,
            "projectId": id,
            "priority": data.priority or "MEDIUM",
        },
    )

    await log_audit(
        actor_id=actor_id,
        action="project.task.create",
        entity_type="tasks",
        entity_id=task_id,
        after={"projectId": id, "title": data.title, "assigneeId": data.assigneeId},
    )
    return {"id": task_id, "message": "Task created"}


async def _load_project_task(project_id: str, task_id: str) -> dict:
    try:
        toid = ObjectId(task_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid task id")
    task = await db.tasks.find_one({"_id": toid})
    if not task:
        raise HTTPException(404, "Task not found")
    # Guards against managing another project's task through your own
    # project's URL.
    if task.get("projectId") != project_id:
        raise HTTPException(404, "Task is not on this project")
    return task


@user_router.put("/{id}/tasks/{taskId}")
async def update_project_task(
    id: str,
    taskId: str,
    data: TaskUpdate,
    user: dict = Depends(get_current_user_doc),
):
    project = await _load(id)
    actor_id = await _require_pm(user, id)
    task = await _load_project_task(id, taskId)

    update: dict = {"updatedAt": datetime.now(timezone.utc)}
    for field in ("title", "description", "priority", "dueDate",
                  "reminderIntervalMinutes", "attachments"):
        v = getattr(data, field)
        if v is not None:
            update[field] = v

    if data.assigneeId is not None:
        if not await pm.is_project_member(data.assigneeId, id):
            raise HTTPException(
                400,
                f"Assignee is not a member of \"{project.get('name')}\".",
            )
        update["assigneeId"] = data.assigneeId

    unset: dict = {}
    if data.status is not None and data.status != task.get("status"):
        # Mirror the assignee-facing start/complete endpoints so a task moved
        # by a PM ends up in exactly the same shape.
        stamp = now_ist_naive()
        update["status"] = data.status
        if data.status == "ONGOING":
            update["startedAt"] = task.get("startedAt") or stamp
            unset["completedAt"] = ""
            unset["onTime"] = ""
        elif data.status == "COMPLETED":
            update["completedAt"] = stamp
            update["startedAt"] = task.get("startedAt") or stamp
        else:  # PENDING
            unset["completedAt"] = ""
            unset["onTime"] = ""
            unset["startedAt"] = ""

    # A PM may move work off their project, but not onto someone else's —
    # that would be assigning work to a project they don't manage.
    if data.projectId is not None and data.projectId != id:
        if data.projectId == "":
            update["projectId"] = None
        else:
            raise HTTPException(
                403,
                "Move the task off this project first, or ask HR to reassign it.",
            )

    ops: dict = {"$set": update}
    if unset:
        ops["$unset"] = unset
    await db.tasks.update_one({"_id": ObjectId(taskId)}, ops)

    new_assignee = update.get("assigneeId")
    if new_assignee and new_assignee != task.get("assigneeId"):
        await notify_user(
            new_assignee,
            "task_assigned",
            "Task assigned to you",
            update.get("title") or task.get("title", ""),
            {"taskId": taskId, "projectId": id},
        )

    await log_audit(
        actor_id=actor_id,
        action="project.task.update",
        entity_type="tasks",
        entity_id=taskId,
        after={k: v for k, v in update.items() if k != "updatedAt"},
    )
    return {"message": "Task updated"}


@user_router.delete("/{id}/tasks/{taskId}")
async def delete_project_task(
    id: str,
    taskId: str,
    user: dict = Depends(get_current_user_doc),
):
    await _load(id)
    actor_id = await _require_pm(user, id)
    await _load_project_task(id, taskId)

    await db.tasks.delete_one({"_id": ObjectId(taskId)})
    await log_audit(
        actor_id=actor_id,
        action="project.task.delete",
        entity_type="tasks",
        entity_id=taskId,
        before={"projectId": id},
    )
    return {"message": "Task deleted"}


# ================= ATTENDANCE =================
@user_router.get("/{id}/attendance")
async def project_attendance(
    id: str,
    date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to today"),
    month: Optional[str] = Query(None, description="YYYY-MM"),
    userId: Optional[str] = Query(None),
    user: dict = Depends(get_current_user_doc),
):
    """Attendance for the project's current members only.

    Deliberately narrow: a PM sees who on *their project* is in today, and
    nothing about anyone else in the company.
    """
    await _load(id)
    await _require_pm(user, id)

    member_ids = await pm.current_member_ids(id)
    if not member_ids:
        return {"members": [], "records": []}
    if userId:
        if userId not in member_ids:
            raise HTTPException(403, "Not a member of this project")
        member_ids = [userId]

    query: dict = {"userId": {"$in": member_ids}}
    if date:
        query["date"] = date
    elif month:
        if not (len(month) == 7 and month[4] == "-"):
            raise HTTPException(400, "Invalid month (YYYY-MM required)")
        query["date"] = {"$regex": f"^{month}-"}
    else:
        query["date"] = now_ist_naive().strftime("%Y-%m-%d")

    records = [r async for r in db.attendance.find(query).sort("date", -1)]
    umap = await _user_map(set(member_ids))

    out = []
    for r in records:
        out.append({
            "id": str(r["_id"]),
            "userId": r.get("userId"),
            "user": umap.get(r.get("userId")),
            "date": r.get("date"),
            "attendanceType": r.get("attendanceType"),
            "status": r.get("status"),
            "isLate": r.get("isLate", False),
            "hoursWorked": r.get("hoursWorked", 0.0),
            "overtimeHours": r.get("overtimeHours", 0.0),
            "halfDay": r.get("halfDay", False),
            # IST wall-clock (utils/ist.py) — emitted as stored, no Z suffix.
            "checkIn": r["checkIn"].isoformat() if r.get("checkIn") else None,
            "checkOut": r["checkOut"].isoformat() if r.get("checkOut") else None,
            "workNotes": r.get("workNotes", ""),
        })

    # Members with no record for the requested day still need a row, or the
    # PM cannot tell "absent" from "not loaded".
    return {
        "members": [umap.get(m) for m in member_ids if umap.get(m)],
        "records": out,
    }


# ================= HR CRUD =================
@hr_router.get("/{id}")
async def hr_get_project(
    id: str,
    _hr: dict = Depends(require_hr),
):
    p = await _load(id)
    return _serialize(p, await pm.roster(id))


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
    await _validate_user_ids(data.managerIds)
    await _validate_user_ids(data.memberIds)

    now = datetime.now(timezone.utc)
    doc = {
        "name": name,
        "code": code,
        "description": data.description or "",
        "departmentId": data.departmentId,
        "status": data.status or "Active",
        "startDate": data.startDate,
        "endDate": data.endDate,
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.projects.insert_one(doc)
    project_id = str(result.inserted_id)

    actor_id = str(hr["_id"])
    # Members join on the project's start date when there is one, so the
    # history doesn't claim everyone joined the day HR happened to type it in.
    changes = await pm.sync_members(
        project_id,
        manager_ids=data.managerIds,
        member_ids=data.memberIds,
        actor_id=actor_id,
        effective=data.startDate,
    )

    await log_audit(
        actor_id=actor_id,
        action="project.create",
        entity_type="projects",
        entity_id=project_id,
        after={"name": name, "code": code, "members": changes["added"]},
    )

    for uid in set(changes["added"]) - {actor_id}:
        await notify_user(
            uid,
            "project_added",
            "Added to a project",
            f"You've been added to the project \"{name}\".",
            {"projectId": project_id},
        )

    return {"id": project_id, "message": "Project created"}


@hr_router.put("/{id}")
async def update_project(
    id: str,
    data: ProjectUpdate,
    hr: dict = Depends(require_hr),
):
    existing = await _load(id)

    await _validate_department(data.departmentId)
    await _validate_user_ids(data.managerIds)
    await _validate_user_ids(data.memberIds)

    update: dict = {"updatedAt": datetime.now(timezone.utc)}
    for field in (
        "name", "description", "departmentId",
        "status", "startDate", "endDate",
    ):
        v = getattr(data, field)
        if v is not None:
            update[field] = v

    await db.projects.update_one({"_id": _oid(id)}, {"$set": update})

    actor_id = str(hr["_id"])
    changes = {"added": [], "removed": [], "changed": []}
    # Only reconcile when the caller actually sent a roster — a PUT that just
    # renames the project must not empty it.
    if data.managerIds is not None or data.memberIds is not None:
        current = await pm.roster(id)
        changes = await pm.sync_members(
            id,
            manager_ids=(
                data.managerIds if data.managerIds is not None
                else current["managerIds"]
            ),
            member_ids=(
                data.memberIds if data.memberIds is not None
                else current["memberIds"]
            ),
            actor_id=actor_id,
        )

    await log_audit(
        actor_id=actor_id,
        action="project.update",
        entity_type="projects",
        entity_id=id,
        after={
            **{k: v for k, v in update.items() if k != "updatedAt"},
            **({"membership": changes} if any(changes.values()) else {}),
        },
    )

    pname = update.get("name", existing.get("name", "a project"))
    for uid in set(changes["added"]) - {actor_id}:
        await notify_user(
            uid,
            "project_added",
            "Added to a project",
            f"You've been added to the project \"{pname}\".",
            {"projectId": id},
        )

    return {"message": "Project updated"}


@hr_router.delete("/{id}")
async def delete_project(
    id: str,
    hr: dict = Depends(require_hr),
):
    oid = _oid(id)
    project = await db.projects.find_one({"_id": oid})
    if not project:
        raise HTTPException(404, "Project not found")

    # A project that has been worked on is history, not a typo. Deleting it
    # would take the membership timeline with it, strand every task on a
    # projectId that resolves to nothing, and orphan the chat thread. Finished
    # work belongs in status "Completed", which stays fully editable.
    task_count = await db.tasks.count_documents({"projectId": id})
    msg_count = await db.chat_messages.count_documents(
        {"channelType": "project", "channelId": id}
    )
    if task_count or msg_count:
        detail = " and ".join(
            filter(None, [
                f"{task_count} task(s)" if task_count else "",
                f"{msg_count} chat message(s)" if msg_count else "",
            ])
        )
        raise HTTPException(
            400,
            f"\"{project.get('name')}\" has {detail} and can't be deleted. "
            "Set its status to Completed instead — the project stays "
            "editable and its history is preserved.",
        )

    # Nothing was ever recorded against it, so this really is a mis-entry.
    await db.projects.delete_one({"_id": oid})
    dropped = await db.project_members.delete_many({"projectId": id})

    await log_audit(
        actor_id=str(hr["_id"]),
        action="project.delete",
        entity_type="projects",
        entity_id=id,
        before={
            "name": project.get("name"),
            "code": project.get("code"),
            "membershipRows": dropped.deleted_count,
        },
    )
    return {"message": "Project deleted"}
