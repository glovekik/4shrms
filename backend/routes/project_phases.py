"""Project phases — the stages a project runs through, and its progress.

A phase groups tasks: "Phase 1: Survey", "Phase 2: Installation". Progress
is read off those tasks rather than typed in, so there is no percentage to
keep in step with reality.

Phases are weighted because they are rarely equal. A two-week survey and a
three-month rollout should not each count for half a project, so overall
progress is the weighted average of phase completion.

A manual override exists for the case the arithmetic can't see — a phase
that is 80% done in substance while its last task drags on. It is stored
separately from the derived figure and always labelled as manual, so nobody
mistakes a judgement call for a measurement.
"""

from fastapi import APIRouter, Depends, HTTPException

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from database import db
from utils.dependencies import get_current_user_doc
from utils.audit import log_audit
from utils import project_members as pm
from models.project_phase import (
    ProjectPhaseCreate,
    ProjectPhaseUpdate,
    ProjectPhaseReorder,
)

router = APIRouter()


def _oid(value: str, label: str = "id") -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(400, f"Invalid {label}")


async def _access(project_id: str, user: dict) -> tuple[bool, bool]:
    """(can_read, can_manage) — the same bar as tasks and variables."""
    if user.get("role") in ("HR", "CEO"):
        return True, True
    uid = str(user["_id"])
    if await pm.is_project_manager(uid, project_id):
        return True, True
    if await pm.is_project_member(uid, project_id):
        return True, False
    return False, False


async def _require(project_id: str, user: dict, manage: bool = False):
    if not await db.projects.find_one({"_id": _oid(project_id, "project id")}):
        raise HTTPException(404, "Project not found")
    can_read, can_manage = await _access(project_id, user)
    if not can_read:
        raise HTTPException(403, "You're not a member of this project.")
    if manage and not can_manage:
        raise HTTPException(403, "Only this project's managers can do that.")
    return can_manage


# Weights are percentages of a whole, so they have a budget: the phases of
# a project share 100, and the tasks inside a phase share 100. Enforced here
# rather than only in the form, because a number that must add up is exactly
# the kind of rule a second client would otherwise get wrong.
WEIGHT_BUDGET = 100.0
# Floating point: 33.3 x 3 is 99.89999999999999, which should pass.
WEIGHT_EPSILON = 0.05


def _check_budget(existing: list[float], incoming: float, what: str) -> None:
    """Raise unless `incoming` still fits alongside `existing`."""
    used = sum(max(0.0, w) for w in existing)
    if incoming < 0:
        raise HTTPException(400, "A weight can't be negative.")
    if used + incoming > WEIGHT_BUDGET + WEIGHT_EPSILON:
        spare = max(0.0, WEIGHT_BUDGET - used)
        raise HTTPException(
            400,
            f"That would take the {what} to "
            f"{round(used + incoming, 1)}%. There's "
            f"{round(spare, 1)}% left to allocate.",
        )


def _weight_of(doc: dict, default: float = 0.0) -> float:
    """A weight, distinguishing "not set" from a deliberate zero.

    `float(doc.get("weight") or 1.0)` reads 0 as missing, so a task worth
    0% silently became 1% and pushed its phase over budget.
    """
    v = doc.get("weight")
    return float(v) if isinstance(v, (int, float)) else default


def _serialize(p: dict, tasks: list[dict]) -> dict:
    """Phase rollup, weighted by each task's own weight.

    Counting tasks equally makes a one-line fix worth as much as a month of
    installation. Weight is per task, so a phase's percentage is the share
    of its *work* that is done, not the share of its rows.
    """
    total_w = sum(max(0.0, _weight_of(t)) for t in tasks)
    done_w = sum(
        max(0.0, _weight_of(t))
        for t in tasks if t.get("status") == "COMPLETED"
    )
    done = sum(1 for t in tasks if t.get("status") == "COMPLETED")
    total = len(tasks)
    return {
        "id": str(p["_id"]),
        "projectId": p.get("projectId"),
        "name": p.get("name"),
        "description": p.get("description") or "",
        "order": p.get("order", 0),
        "status": p.get("status", "PENDING"),
        "startDate": p.get("startDate"),
        "endDate": p.get("endDate"),
        "weight": _weight_of(p),
        "taskCount": total,
        "completedCount": done,
        # A phase with no tasks yet contributes 0, not 100 — an empty phase
        # is unstarted work, not finished work.
        "percent": round(done_w / total_w * 100) if total_w else 0,
        "taskWeightAllocated": round(total_w, 1),
        "taskWeightRemaining": round(max(0.0, 100.0 - total_w), 1),
        "taskWeightBalanced": abs(total_w - 100.0) <= 0.05 or total_w == 0,
        "tasks": [
            {
                "id": str(t["_id"]),
                "title": t.get("title"),
                "status": t.get("status"),
                "weight": _weight_of(t),
                "priority": t.get("priority"),
                "dueDate": t.get("dueDate"),
            }
            for t in sorted(tasks, key=lambda x: str(x.get("title") or ""))
        ],
    }


async def _phase_rows(project_id: str) -> list[dict]:
    """Phases with their task rollups, in display order."""
    phases = []
    async for p in db.project_phases.find({"projectId": project_id}):
        phases.append(p)
    phases.sort(key=lambda p: (p.get("order", 0), str(p.get("createdAt", ""))))

    out = []
    for p in phases:
        pid = str(p["_id"])
        tasks = []
        async for t in db.tasks.find(
            {"projectId": project_id, "phaseId": pid},
            {"title": 1, "status": 1, "weight": 1, "priority": 1,
             "dueDate": 1, "assigneeId": 1},
        ):
            tasks.append(t)
        out.append(_serialize(p, tasks))
    return out


def _weighted(phases: list[dict]) -> Optional[int]:
    """Overall completion as the weighted average of phase percentages."""
    total_weight = sum(max(0.0, p["weight"]) for p in phases)
    if total_weight <= 0:
        return None
    acc = sum(p["percent"] * max(0.0, p["weight"]) for p in phases)
    return round(acc / total_weight)


# ================= LIST + PROGRESS =================
@router.get("/{id}/phases")
async def list_phases(id: str, user: dict = Depends(get_current_user_doc)):
    """Phases, their rollups, and the project's overall progress.

    `progress` says which number to show and where it came from:
      source "phases"   — weighted average of the phases
      source "tasks"    — no phases defined, so plain task completion
      source "manual"   — a manager set it by hand; `derived` still carries
                          what the tasks say, so the gap is visible
    """
    can_manage = await _require(id, user)
    phases = await _phase_rows(id)

    total = await db.tasks.count_documents({"projectId": id})
    done = await db.tasks.count_documents(
        {"projectId": id, "status": "COMPLETED"}
    )
    task_percent = round(done / total * 100) if total else 0

    derived = _weighted(phases) if phases else task_percent
    project = await db.projects.find_one({"_id": _oid(id)})
    override = (project or {}).get("progressOverride")

    allocated = round(sum(max(0.0, p["weight"]) for p in phases), 1)
    return {
        "phases": phases,
        "viewerCanManage": can_manage,
        "weight": {
            "allocated": allocated,
            "remaining": round(max(0.0, WEIGHT_BUDGET - allocated), 1),
            "balanced": abs(allocated - WEIGHT_BUDGET) <= WEIGHT_EPSILON,
        },
        "progress": {
            "percent": override if override is not None else derived,
            "derived": derived,
            "source": (
                "manual" if override is not None
                else "phases" if phases
                else "tasks"
            ),
            "taskTotal": total,
            "taskCompleted": done,
        },
    }


# ================= CREATE =================
@router.post("/{id}/phases")
async def create_phase(
    id: str,
    data: ProjectPhaseCreate,
    user: dict = Depends(get_current_user_doc),
):
    await _require(id, user, manage=True)

    name = (data.name or "").strip()
    if not name:
        raise HTTPException(400, "Give the phase a name")

    if data.order is None:
        # Append: one past the highest, so a new phase lands at the bottom.
        last = await db.project_phases.find_one(
            {"projectId": id}, sort=[("order", -1)]
        )
        order = (last or {}).get("order", -1) + 1
    else:
        order = data.order

    # Default a new phase to whatever is left, so the common case adds up
    # without the person doing arithmetic.
    others = [
        float(x.get("weight") or 0)
        async for x in db.project_phases.find({"projectId": id}, {"weight": 1})
    ]
    used = sum(max(0.0, w) for w in others)
    weight = (
        float(data.weight) if data.weight is not None
        else round(max(0.0, WEIGHT_BUDGET - used), 1)
    )
    _check_budget(others, weight, "project's phases")

    now = datetime.now(timezone.utc)
    res = await db.project_phases.insert_one({
        "projectId": id,
        "name": name,
        "description": (data.description or "").strip() or None,
        "order": order,
        "status": data.status or "PENDING",
        "startDate": data.startDate,
        "endDate": data.endDate,
        "weight": weight,
        "createdBy": str(user["_id"]),
        "createdAt": now,
        "updatedAt": now,
    })
    await log_audit(
        actor_id=str(user["_id"]),
        action="project_phase.create",
        entity_type="project_phases",
        entity_id=str(res.inserted_id),
        after={"projectId": id, "name": name},
    )
    return {"id": str(res.inserted_id), "message": "Phase added"}


# ================= UPDATE =================
@router.put("/{id}/phases/{phaseId}")
async def update_phase(
    id: str,
    phaseId: str,
    data: ProjectPhaseUpdate,
    user: dict = Depends(get_current_user_doc),
):
    await _require(id, user, manage=True)
    oid = _oid(phaseId, "phase id")
    if not await db.project_phases.find_one({"_id": oid, "projectId": id}):
        raise HTTPException(404, "Phase not found")

    update: dict = {"updatedAt": datetime.now(timezone.utc)}
    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(400, "A phase needs a name")
        update["name"] = name
    if data.description is not None:
        update["description"] = data.description.strip() or None
    if data.order is not None:
        update["order"] = data.order
    if data.status is not None:
        update["status"] = data.status
    if data.startDate is not None:
        update["startDate"] = data.startDate or None
    if data.endDate is not None:
        update["endDate"] = data.endDate or None
    if data.weight is not None:
        others = [
            float(x.get("weight") or 0)
            async for x in db.project_phases.find(
                {"projectId": id, "_id": {"$ne": oid}}, {"weight": 1}
            )
        ]
        _check_budget(others, float(data.weight), "project's phases")
        update["weight"] = float(data.weight)

    await db.project_phases.update_one({"_id": oid}, {"$set": update})
    return {"message": "Saved"}


# ================= DELETE =================
@router.delete("/{id}/phases/{phaseId}")
async def delete_phase(
    id: str,
    phaseId: str,
    user: dict = Depends(get_current_user_doc),
):
    """Remove a phase. Its tasks survive, unassigned from any phase — losing
    work because its grouping was deleted would be indefensible."""
    await _require(id, user, manage=True)
    oid = _oid(phaseId, "phase id")
    if not await db.project_phases.find_one({"_id": oid, "projectId": id}):
        raise HTTPException(404, "Phase not found")

    await db.project_phases.delete_one({"_id": oid})
    freed = await db.tasks.update_many(
        {"projectId": id, "phaseId": phaseId}, {"$set": {"phaseId": None}}
    )
    await log_audit(
        actor_id=str(user["_id"]),
        action="project_phase.delete",
        entity_type="project_phases",
        entity_id=phaseId,
        before={"projectId": id},
    )
    return {
        "message": "Phase removed",
        "tasksUnassigned": freed.modified_count,
    }


# ================= REORDER =================
@router.put("/{id}/phases-order")
async def reorder_phases(
    id: str,
    data: ProjectPhaseReorder,
    user: dict = Depends(get_current_user_doc),
):
    await _require(id, user, manage=True)
    for index, phase_id in enumerate(data.phaseIds):
        await db.project_phases.update_one(
            {"_id": _oid(phase_id, "phase id"), "projectId": id},
            {"$set": {"order": index}},
        )
    return {"message": "Reordered"}


# ================= PROGRESS OVERRIDE =================
class ProgressOverride(BaseModel):
    # None clears it and hands the number back to the tasks.
    percent: Optional[int] = Field(default=None, ge=0, le=100)


@router.put("/{id}/progress")
async def set_progress_override(
    id: str,
    data: ProgressOverride,
    user: dict = Depends(get_current_user_doc),
):
    """Pin the project's progress, or clear the pin.

    Kept in its own field rather than overwriting the derived figure, so the
    two are always both available and the UI can say "you set 60%, the tasks
    say 33%". A silent override is how a dashboard ends up lying.
    """
    await _require(id, user, manage=True)
    await db.projects.update_one(
        {"_id": _oid(id)},
        {"$set": {
            "progressOverride": data.percent,
            "updatedAt": datetime.now(timezone.utc),
        }},
    )
    await log_audit(
        actor_id=str(user["_id"]),
        action="project.progress_override",
        entity_type="projects",
        entity_id=id,
        after={"percent": data.percent},
    )
    return {"percent": data.percent}
