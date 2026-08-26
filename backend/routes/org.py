"""Company org chart: CEO → department → project → employee.

Read-only and company-wide — everyone can see the shape of the organisation.
Sensitive fields are deliberately excluded; this returns names, titles and
avatars only, never contact details, salary or personal data.

The tree is assembled from data that is only partly populated in practice, so
gaps are surfaced rather than hidden: departments with no head, employees with
no department, and projects not attached to a department each get their own
place in the response. A chart that silently drops eleven people is worse than
one that shows them in an "unassigned" bucket.
"""

from fastapi import APIRouter, Depends

from bson import ObjectId
from bson.errors import InvalidId

from database import db
from utils.dependencies import get_current_user
from utils import project_members as pm


router = APIRouter()


# People who have left shouldn't appear on the chart.
_EXCLUDED_STATUSES = ("Terminated",)


def _person(u: dict) -> dict:
    work = u.get("work") or {}
    return {
        "id": str(u["_id"]),
        "name": u.get("name"),
        "profilePictureUrl": u.get("profilePictureUrl"),
        "employeeCode": u.get("employeeCode"),
        "jobTitle": work.get("jobTitle") or u.get("tag"),
        "role": u.get("role"),
        "departmentId": work.get("departmentId"),
        "reportingManagerId": (
            u.get("reportingManagerId") or work.get("reportingManagerId")
        ),
    }


@router.get("/chart")
async def org_chart(_user_id: str = Depends(get_current_user)):
    people: dict[str, dict] = {}
    async for u in db.users.find(
        {"status": {"$nin": list(_EXCLUDED_STATUSES)}},
        {
            "name": 1, "profilePictureUrl": 1, "employeeCode": 1, "role": 1,
            "tag": 1, "work": 1, "reportingManagerId": 1,
        },
    ):
        people[str(u["_id"])] = _person(u)

    ceo = next((p for p in people.values() if p.get("role") == "CEO"), None)

    # Projects grouped by department, rosters resolved to people.
    projects_by_dept: dict[str, list] = {}
    orphan_projects: list[dict] = []
    async for p in db.projects.find({}):
        pid = str(p["_id"])
        roster = await pm.roster(pid)
        node = {
            "id": pid,
            "name": p.get("name"),
            "code": p.get("code"),
            "status": p.get("status", "Active"),
            "departmentId": p.get("departmentId"),
            "managers": [people[m] for m in roster["managerIds"] if m in people],
            "members": [people[m] for m in roster["memberIds"] if m in people],
        }
        node["headcount"] = len(node["managers"]) + len(node["members"])
        if p.get("departmentId"):
            projects_by_dept.setdefault(p["departmentId"], []).append(node)
        else:
            orphan_projects.append(node)

    departments = []
    assigned_dept_ids: set[str] = set()
    async for d in db.departments.find({}).sort("name", 1):
        did = str(d["_id"])
        assigned_dept_ids.add(did)
        dept_people = [p for p in people.values() if p.get("departmentId") == did]
        projects = projects_by_dept.get(did, [])

        # Anyone in the department who isn't on one of its projects — they'd
        # otherwise vanish between the department and project levels.
        on_a_project = {
            m["id"] for proj in projects for m in proj["managers"] + proj["members"]
        }
        head_id = d.get("headUserId")
        departments.append({
            "id": did,
            "name": d.get("name"),
            "description": d.get("description"),
            "head": people.get(head_id) if head_id else None,
            "headcount": len(dept_people),
            "projects": sorted(projects, key=lambda x: x["name"] or ""),
            "directMembers": [
                p for p in dept_people if p["id"] not in on_a_project
            ],
        })

    unassigned = [
        p
        for p in people.values()
        if not p.get("departmentId") or p.get("departmentId") not in assigned_dept_ids
    ]
    if ceo:
        unassigned = [p for p in unassigned if p["id"] != ceo["id"]]

    return {
        "ceo": ceo,
        "departments": departments,
        "projectsWithoutDepartment": orphan_projects,
        "unassigned": sorted(unassigned, key=lambda p: p["name"] or ""),
        "totals": {
            "people": len(people),
            "departments": len(departments),
            "projects": sum(len(v) for v in projects_by_dept.values())
            + len(orphan_projects),
            "unassigned": len(unassigned),
            "hasCeo": bool(ceo),
        },
    }


@router.get("/people/{user_id}/projects")
async def person_projects(
    user_id: str,
    _viewer_id: str = Depends(get_current_user),
):
    """Every project this person is on, and every one they've left.

    Membership is date-ranged, so past stays are real history rather than
    deletions — "was on CCTV 360 from Nov to Aug" is answerable.
    """
    rows = [r async for r in db.project_members.find({"userId": user_id})]
    if not rows:
        return {"current": [], "past": []}

    oids = []
    for r in rows:
        try:
            oids.append(ObjectId(r["projectId"]))
        except (InvalidId, TypeError, KeyError):
            continue

    projects: dict[str, dict] = {}
    async for p in db.projects.find(
        {"_id": {"$in": oids}},
        {"name": 1, "code": 1, "status": 1, "departmentId": 1},
    ):
        projects[str(p["_id"])] = p

    dept_names: dict[str, str] = {}
    dept_ids = {
        p.get("departmentId") for p in projects.values() if p.get("departmentId")
    }
    if dept_ids:
        d_oids = []
        for did in dept_ids:
            try:
                d_oids.append(ObjectId(did))
            except (InvalidId, TypeError):
                continue
        async for d in db.departments.find({"_id": {"$in": d_oids}}, {"name": 1}):
            dept_names[str(d["_id"])] = d.get("name")

    current, past = [], []
    for r in rows:
        proj = projects.get(r.get("projectId"))
        if not proj:
            continue  # project deleted — nothing meaningful to show
        entry = {
            "projectId": r["projectId"],
            "name": proj.get("name"),
            "code": proj.get("code"),
            "status": proj.get("status", "Active"),
            "departmentName": dept_names.get(proj.get("departmentId")),
            "role": r.get("role", "member"),
            "joinedAt": r.get("joinedAt"),
            "leftAt": r.get("leftAt"),
        }
        (current if r.get("leftAt") is None else past).append(entry)

    current.sort(key=lambda e: (e["role"] != "manager", e["name"] or ""))
    past.sort(key=lambda e: e.get("leftAt") or "", reverse=True)
    return {"current": current, "past": past}
