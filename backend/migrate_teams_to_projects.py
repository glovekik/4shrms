"""Merge the `teams` collection into `projects`, with membership history.

Why this shape: teams and projects were the same two real things entered twice
(CCTV 360, AI4AP Police), and their member lists had already drifted apart.
This collapses them into one project per real thing.

The merged project **keeps the TEAM's `_id`**, not the project's. That is
deliberate: `tasks.teamId` and `chat_messages.channelId` both reference team
ids, while nothing anywhere references a project id (every timesheet
`projectId` is null). Preserving the team id turns a risky id remap into a
rename.

Member lists are merged as a UNION — the team and project rosters disagreed and
picking one would silently drop real people. HR can trim afterwards.

`db.teams` is left in place. Chat still reads it (routes/chat.py); Phase 4
retires it.

Usage:
    python migrate_teams_to_projects.py            # dry run, writes nothing
    python migrate_teams_to_projects.py --apply
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

APPLY = "--apply" in sys.argv


def _norm(s):
    return (s or "").strip().lower()


def _code_from(name):
    parts = [p for p in (name or "").replace("-", " ").split() if p]
    return ("".join(p[0] for p in parts) or "PROJ").upper()[:10]


def _join_date(project, team):
    """Best available 'this membership started' date."""
    if project and project.get("startDate"):
        return project["startDate"]
    for doc in (team, project):
        created = (doc or {}).get("createdAt")
        if isinstance(created, datetime):
            return created.date().isoformat()
    return datetime.now(timezone.utc).date().isoformat()


async def main():
    uri = os.getenv("MONGO_URL") or os.getenv("MONGO_URI")
    dbname = os.getenv("MONGO_DB_NAME") or os.getenv("DB_NAME") or "attendance_db"
    if not uri:
        raise SystemExit("MONGO_URL not set")
    db = AsyncIOMotorClient(uri)[dbname]

    print(f"DB: {dbname}   mode: {'APPLY' if APPLY else 'DRY RUN'}")
    print("=" * 62)

    users = {str(u["_id"]): u.get("name") for u in await db.users.find({}, {"name": 1}).to_list(None)}
    # Stale ids left behind by deleted accounts — creating membership rows for
    # them would put permanent ghosts in the history.
    dropped_ghosts: set[str] = set()

    def live(ids):
        out = []
        for uid in ids:
            if not uid:
                continue
            if uid in users:
                out.append(uid)
            else:
                dropped_ghosts.add(uid)
        return out

    teams = await db.teams.find({}).to_list(None)
    projects = await db.projects.find({}).to_list(None)
    by_name = {_norm(p.get("name")): p for p in projects}

    now = datetime.now(timezone.utc)
    matched_project_ids: set[str] = set()
    plan: list[dict] = []

    # ---- teams become projects, keeping the team _id ----
    for t in teams:
        tid = str(t["_id"])
        proj = by_name.get(_norm(t.get("name")))
        if proj:
            matched_project_ids.add(str(proj["_id"]))

        managers, seen = [], set()
        for uid in [t.get("teamLeadId")] + list((proj or {}).get("projectManagerIds") or []):
            if uid and uid not in seen:
                seen.add(uid)
                managers.append(uid)

        members = []
        for uid in list(t.get("memberIds") or []) + list((proj or {}).get("memberIds") or []):
            if uid and uid not in seen:
                seen.add(uid)
                members.append(uid)

        live_managers, live_members = live(managers), live(members)

        plan.append({
            "kind": "merge" if proj else "team-only",
            "targetId": tid,
            "dropProjectId": str(proj["_id"]) if proj else None,
            "doc": {
                "name": t.get("name"),
                "code": (proj or {}).get("code") or _code_from(t.get("name")),
                "description": (proj or {}).get("description") or "",
                "departmentId": (proj or {}).get("departmentId"),
                "status": (proj or {}).get("status") or "Active",
                "startDate": (proj or {}).get("startDate"),
                "endDate": (proj or {}).get("endDate"),
                # The merged project is UPSERTED at the team's _id, so it is a
                # brand-new document — the one that carried these fields is
                # the duplicate we delete. Without copying them across, the
                # code running in production at migration time reads a project
                # with no roster at all. They are dead to the new code and
                # dropped in a later cleanup pass.
                "projectManagerIds": live_managers,
                "memberIds": live_members,
                "createdAt": (proj or {}).get("createdAt") or t.get("createdAt") or now,
                "updatedAt": now,
            },
            "managers": live_managers,
            "members": live_members,
            "joinedAt": _join_date(proj, t),
        })

    # ---- projects with no team stay put; just backfill their membership ----
    for p in projects:
        pid = str(p["_id"])
        if pid in matched_project_ids:
            continue
        managers = live(p.get("projectManagerIds") or [])
        members = [u for u in live(p.get("memberIds") or []) if u not in set(managers)]
        plan.append({
            "kind": "backfill",
            "targetId": pid,
            "dropProjectId": None,
            "doc": None,
            "managers": managers,
            "members": members,
            "joinedAt": _join_date(p, None),
        })

    # ---- report ----
    def nm(uid):
        return users.get(uid, f"<unknown {uid}>")

    for item in plan:
        doc = item["doc"] or {}
        label = doc.get("name") or item["targetId"]
        print(f"\n[{item['kind']}] {label}")
        print(f"    project _id -> {item['targetId']}")
        if item["dropProjectId"]:
            print(f"    delete duplicate project doc {item['dropProjectId']}")
        print(f"    joinedAt    -> {item['joinedAt']}")
        print(f"    managers ({len(item['managers'])}): " + ", ".join(nm(u) for u in item['managers']))
        print(f"    members  ({len(item['members'])}): " + ", ".join(nm(u) for u in item['members']))

    # `code` is uniquely indexed. A collision only surfaces mid-write as an
    # E11000, by which point some projects are migrated and some aren't — so
    # check it up front and refuse rather than fail halfway.
    survivors = {str(p["_id"]) for p in projects} - {
        i["dropProjectId"] for i in plan if i["dropProjectId"]
    }
    taken: dict[str, str] = {}
    for p in projects:
        if str(p["_id"]) in survivors and not any(
            i["targetId"] == str(p["_id"]) for i in plan
        ):
            taken[p.get("code")] = f"existing project {p.get('name')}"
    clashes = []
    for item in plan:
        code = (item["doc"] or {}).get("code")
        if not code:
            continue
        label = (item["doc"] or {}).get("name") or item["targetId"]
        if code in taken:
            clashes.append(f"    '{code}': {label} vs {taken[code]}")
        else:
            taken[code] = label
    if clashes:
        print("\n" + "=" * 62)
        print("ABORT — duplicate project codes (db.projects.code is unique):")
        print("\n".join(clashes))
        print("Rename a team or set an explicit code before migrating.")
        raise SystemExit(1)

    # The merged project keeps the TEAM's _id, so the duplicate PROJECT doc is
    # deleted. Anything still pointing at that dead id silently stops
    # resolving — a task with no project, a chat thread nobody can open. The
    # design assumes nothing references project ids; verify it rather than
    # trust it, because being wrong here is only discovered after the fact.
    dropped_ids = [i["dropProjectId"] for i in plan if i["dropProjectId"]]
    if dropped_ids:
        dangling = []
        for col, field, extra in (
            ("tasks", "projectId", {}),
            ("timesheets", "entries.projectId", {}),
            ("chat_messages", "channelId", {"channelType": "project"}),
            ("chat_reads", "channelId", {"channelType": "project"}),
            ("project_members", "projectId", {}),
        ):
            n = await db[col].count_documents(
                {field: {"$in": dropped_ids}, **extra}
            )
            if n:
                dangling.append(f"    {col}.{field}: {n} row(s)")
        if dangling:
            print("\n" + "=" * 62)
            print("ABORT — these reference a project doc this migration deletes:")
            print("\n".join(dangling))
            print("They would be left pointing at an id that no longer exists.")
            print("Repoint them at the surviving (team) id first.")
            raise SystemExit(1)
        print("\nnothing references the duplicate project ids being deleted — safe")

    tasks_with_team = await db.tasks.count_documents({"teamId": {"$nin": [None, ""]}})
    chat_team_msgs = await db.chat_messages.count_documents({"channelType": "team"})
    print("\n" + "=" * 62)
    print(f"tasks carrying a teamId : {tasks_with_team}  (ids stay valid — no remap)")
    print(f"team chat messages      : {chat_team_msgs}  (ids stay valid — no remap)")
    print(f"projects after migration: {len(plan)}")
    if dropped_ghosts:
        print(f"skipped {len(dropped_ghosts)} member id(s) with no user account: "
              + ", ".join(sorted(dropped_ghosts)))

    if not APPLY:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    # ---- write ----
    written = rows = 0
    for item in plan:
        pid = item["targetId"]
        # Drop the duplicate FIRST. The merged doc reuses that duplicate's
        # `code`, and db.projects has a unique index on code — upserting
        # before the delete raises E11000 on the very first merged project
        # and leaves the database half-migrated.
        if item["dropProjectId"]:
            await db.projects.delete_one({"_id": ObjectId(item["dropProjectId"])})
        if item["doc"] is not None:
            await db.projects.update_one(
                {"_id": ObjectId(pid)},
                {"$set": item["doc"]},
                upsert=True,
            )
            written += 1
        # The denormalised `memberIds` / `projectManagerIds` are deliberately
        # LEFT IN PLACE. The roster now lives in project_members and no new
        # code reads them — but the code running in production at migration
        # time still does, and unsetting them mid-deploy blanked every roster
        # in the window between migrating and shipping the new build. Dead
        # fields for a few minutes cost nothing; an empty Employees screen on
        # live HR data costs a lot. Drop them in a later cleanup pass, once
        # the new build is confirmed healthy.

        for uid, role in (
            [(u, "manager") for u in item["managers"]]
            + [(u, "member") for u in item["members"]]
        ):
            # Idempotent: re-running must not duplicate an open stay.
            existing = await db.project_members.find_one(
                {"projectId": pid, "userId": uid, "leftAt": None}
            )
            if existing:
                if existing.get("role") != role:
                    await db.project_members.update_one(
                        {"_id": existing["_id"]},
                        {"$set": {"role": role, "updatedAt": now}},
                    )
                continue
            await db.project_members.insert_one({
                "projectId": pid,
                "userId": uid,
                "role": role,
                "joinedAt": item["joinedAt"],
                "leftAt": None,
                "addedBy": None,       # migrated, not added by a person
                "removedBy": None,
                "migrated": True,
                "createdAt": now,
                "updatedAt": now,
            })
            rows += 1

    print(f"\nAPPLIED: {written} projects written, {rows} membership rows created.")
    print("teams collection left intact (chat still reads it until Phase 4).")


asyncio.run(main())
