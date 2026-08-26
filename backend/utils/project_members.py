"""Project membership with history.

Membership lives in its own collection (`project_members`) rather than as
`memberIds` on the project doc, because HR needs to answer "who was on CCTV 360
in April?" — not just "who is on it today". Each row is a stay: it opens when
someone joins and closes when they leave. Nobody is ever deleted from history.

    { projectId, userId, role, joinedAt, leftAt, addedBy, removedBy }

Current members are the rows with `leftAt: None`. A role change (member ->
manager) closes the old row and opens a new one, so the timeline stays honest
about what someone actually was at any given date.

Dates are IST wall-clock date strings (YYYY-MM-DD) to match attendance and
leave, so ranges line up without timezone conversion.
"""

from datetime import datetime, timezone
from typing import Iterable, Literal, Optional

from database import db
from utils.ist import today_ist_str


MemberRole = Literal["manager", "member"]


async def ensure_indexes() -> None:
    """Called from database.py alongside the other index setup."""
    # Current roster for a project — the hottest read.
    await db.project_members.create_index([("projectId", 1), ("leftAt", 1)])
    # "Which projects am I on" — drives the user-facing project list.
    await db.project_members.create_index([("userId", 1), ("leftAt", 1)])
    # Point-in-time lookups scan by project then filter on the stay window.
    await db.project_members.create_index([("projectId", 1), ("joinedAt", 1)])


# ================= READS =================
async def current_members(project_id: str) -> list[dict]:
    """Open stays for a project, managers first then by join date."""
    out = []
    async for row in db.project_members.find(
        {"projectId": project_id, "leftAt": None}
    ):
        out.append(row)
    out.sort(key=lambda r: (r.get("role") != "manager", r.get("joinedAt") or ""))
    return out


async def current_member_ids(project_id: str) -> list[str]:
    return [r["userId"] for r in await current_members(project_id)]


async def current_manager_ids(project_id: str) -> list[str]:
    return [
        r["userId"]
        for r in await current_members(project_id)
        if r.get("role") == "manager"
    ]


async def roster(project_id: str) -> dict:
    """Both lists in one pass — routes almost always want both."""
    rows = await current_members(project_id)
    return {
        "managerIds": [r["userId"] for r in rows if r.get("role") == "manager"],
        "memberIds": [r["userId"] for r in rows if r.get("role") != "manager"],
    }


async def members_as_of(project_id: str, as_of: str) -> list[dict]:
    """Who was on the project on a specific date.

    `leftAt` is inclusive — someone who left on the 21st was still on the
    project that day.

    One row per person: a role change on `as_of` itself leaves two overlapping
    stays covering that date (the closed one and the new one), so we keep the
    later stay — the role they ended the day in.
    """
    rows = []
    async for row in db.project_members.find(
        {
            "projectId": project_id,
            "joinedAt": {"$lte": as_of},
            "$or": [{"leftAt": None}, {"leftAt": {"$gte": as_of}}],
        }
    ):
        rows.append(row)

    best: dict[str, dict] = {}
    for r in rows:
        prev = best.get(r["userId"])
        if prev is None:
            best[r["userId"]] = r
            continue
        # Still-open beats closed; otherwise the one that started later wins.
        if prev.get("leftAt") is not None and (
            r.get("leftAt") is None
            or (r.get("joinedAt") or "") > (prev.get("joinedAt") or "")
        ):
            best[r["userId"]] = r

    out = list(best.values())
    out.sort(key=lambda r: (r.get("role") != "manager", r.get("joinedAt") or ""))
    return out


async def members_in_range(project_id: str, start: str, end: str) -> list[dict]:
    """Every stay overlapping [start, end] — for effort rollups.

    Returns stays, not people: someone whose role changed mid-range appears
    once per stay. Dedupe on userId if you want a headcount.
    """
    out = []
    async for row in db.project_members.find(
        {
            "projectId": project_id,
            "joinedAt": {"$lte": end},
            "$or": [{"leftAt": None}, {"leftAt": {"$gte": start}}],
        }
    ):
        out.append(row)
    return out


async def history(project_id: str) -> list[dict]:
    """Full timeline, newest stay first — powers the 'member changes' view."""
    out = []
    async for row in db.project_members.find({"projectId": project_id}):
        out.append(row)
    out.sort(key=lambda r: (r.get("joinedAt") or "", r.get("userId")), reverse=True)
    return out


async def project_ids_for_user(user_id: str, *, include_past: bool = False) -> list[str]:
    q: dict = {"userId": user_id}
    if not include_past:
        q["leftAt"] = None
    return [row["projectId"] async for row in db.project_members.find(q, {"projectId": 1})]


async def is_project_manager(user_id: str, project_id: str) -> bool:
    return await db.project_members.find_one(
        {
            "projectId": project_id,
            "userId": user_id,
            "role": "manager",
            "leftAt": None,
        }
    ) is not None


async def is_project_member(user_id: str, project_id: str) -> bool:
    return await db.project_members.find_one(
        {"projectId": project_id, "userId": user_id, "leftAt": None}
    ) is not None


async def managed_project_ids(user_id: str) -> list[str]:
    """Projects where this user is a current manager — the PM permission scope."""
    return [
        row["projectId"]
        async for row in db.project_members.find(
            {"userId": user_id, "role": "manager", "leftAt": None},
            {"projectId": 1},
        )
    ]


# ================= WRITES =================
def _desired(
    manager_ids: Optional[Iterable[str]],
    member_ids: Optional[Iterable[str]],
) -> dict[str, MemberRole]:
    """Manager wins when someone appears in both lists."""
    out: dict[str, MemberRole] = {}
    for uid in member_ids or []:
        if uid:
            out[uid] = "member"
    for uid in manager_ids or []:
        if uid:
            out[uid] = "manager"
    return out


async def sync_members(
    project_id: str,
    *,
    manager_ids: Optional[Iterable[str]],
    member_ids: Optional[Iterable[str]],
    actor_id: str,
    effective: Optional[str] = None,
) -> dict:
    """Reconcile the roster to the given lists, recording joins and leaves.

    Returns {added, removed, changed} as user-id lists so the caller can notify
    only the people whose membership actually moved.
    """
    when = effective or today_ist_str()
    now = datetime.now(timezone.utc)

    desired = _desired(manager_ids, member_ids)
    open_rows = {r["userId"]: r for r in await current_members(project_id)}

    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []

    async def _close(uid: str) -> None:
        await db.project_members.update_one(
            {"projectId": project_id, "userId": uid, "leftAt": None},
            {"$set": {"leftAt": when, "removedBy": actor_id, "updatedAt": now}},
        )

    async def _open(uid: str, role: MemberRole) -> None:
        await db.project_members.insert_one(
            {
                "projectId": project_id,
                "userId": uid,
                "role": role,
                "joinedAt": when,
                "leftAt": None,
                "addedBy": actor_id,
                "removedBy": None,
                "createdAt": now,
                "updatedAt": now,
            }
        )

    # Left the project.
    for uid in open_rows.keys() - desired.keys():
        await _close(uid)
        removed.append(uid)

    # Joined the project.
    for uid in desired.keys() - open_rows.keys():
        await _open(uid, desired[uid])
        added.append(uid)

    # Still on it, but the role moved — close the old stay, open a new one so
    # "was Ravi a manager in April?" stays answerable.
    for uid in desired.keys() & open_rows.keys():
        if open_rows[uid].get("role") != desired[uid]:
            await _close(uid)
            await _open(uid, desired[uid])
            changed.append(uid)

    return {"added": added, "removed": removed, "changed": changed}


def serialize(row: dict) -> dict:
    return {
        "userId": row.get("userId"),
        "role": row.get("role", "member"),
        "joinedAt": row.get("joinedAt"),
        "leftAt": row.get("leftAt"),
        "addedBy": row.get("addedBy"),
        "removedBy": row.get("removedBy"),
    }
