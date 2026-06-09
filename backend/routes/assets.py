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
from utils.notify import notify_user, notify_hr
from models.asset import (
    AssetCreate,
    AssetUpdate,
    AssetAssign,
    AssetReturn,
    AssetIssueReport,
    AssetReportResolution,
)

# /assets/...      — user-facing (auth required)
user_router = APIRouter()
# /hr/assets/...   — HR only
hr_router = APIRouter()


# ================= SERIALIZERS =================
def _serialize_asset(
    a: dict,
    user_info: Optional[dict] = None,
) -> dict:
    return {
        "id": str(a["_id"]),
        "code": a.get("code"),
        "name": a.get("name"),
        "category": a.get("category"),
        "serialNumber": a.get("serialNumber"),
        "notes": a.get("notes", ""),
        "purchaseDate": a.get("purchaseDate"),
        "purchasePrice": a.get("purchasePrice"),
        "status": a.get("status", "AVAILABLE"),
        "assignedToUserId": a.get("assignedToUserId"),
        "assignedTo": user_info,
        "assignedAt": (
            a["assignedAt"].isoformat()
            if a.get("assignedAt") else None
        ),
        "createdAt": (
            a["createdAt"].isoformat()
            if a.get("createdAt") else None
        ),
    }


def _serialize_report(
    r: dict,
    asset_info: Optional[dict] = None,
    reporter_info: Optional[dict] = None,
) -> dict:
    return {
        "id": str(r["_id"]),
        "assetId": r.get("assetId"),
        "asset": asset_info,
        "reportedBy": r.get("reportedBy"),
        "reporter": reporter_info,
        "reportType": r.get("reportType"),
        "description": r.get("description"),
        "status": r.get("status"),
        "resolution": r.get("resolution", ""),
        "resolvedBy": r.get("resolvedBy"),
        "resolvedAt": (
            r["resolvedAt"].isoformat()
            if r.get("resolvedAt") else None
        ),
        "createdAt": (
            r["createdAt"].isoformat()
            if r.get("createdAt") else None
        ),
    }


# ================= HELPERS =================
async def _user_basics(ids) -> dict:
    unique = {uid for uid in ids if uid}
    if not unique:
        return {}
    oids = []
    for uid in unique:
        try:
            oids.append(ObjectId(uid))
        except (InvalidId, TypeError):
            continue
    if not oids:
        return {}
    result = {}
    async for u in db.users.find(
        {"_id": {"$in": oids}}
    ):
        result[str(u["_id"])] = {
            "id": str(u["_id"]),
            "name": u.get("name"),
            "email": u.get("email"),
        }
    return result


async def _asset_basics(ids) -> dict:
    unique = {aid for aid in ids if aid}
    if not unique:
        return {}
    oids = []
    for aid in unique:
        try:
            oids.append(ObjectId(aid))
        except (InvalidId, TypeError):
            continue
    if not oids:
        return {}
    result = {}
    async for a in db.assets.find(
        {"_id": {"$in": oids}}
    ):
        result[str(a["_id"])] = {
            "id": str(a["_id"]),
            "code": a.get("code"),
            "name": a.get("name"),
            "category": a.get("category"),
            "status": a.get("status"),
        }
    return result


async def _ensure_code_free(
    code: Optional[str],
    exclude_id: Optional[ObjectId] = None,
) -> None:
    if not code:
        return
    query: dict = {"code": code}
    if exclude_id is not None:
        query["_id"] = {"$ne": exclude_id}
    if await db.assets.find_one(query):
        raise HTTPException(
            400,
            f"Asset code '{code}' is already in use",
        )


def _append_note(existing: str, addition: str) -> str:
    addition = (addition or "").strip()
    if not addition:
        return existing or ""
    if not existing:
        return addition
    return (existing + "\n" + addition).strip()


# ================= HR: ASSET CRUD =================
@hr_router.post("/assets")
async def create_asset(
    data: AssetCreate,
    hr: dict = Depends(require_hr),
):
    code = (data.code or "").strip()
    if not code:
        raise HTTPException(400, "Code is required")

    await _ensure_code_free(code)

    now = datetime.now(timezone.utc)

    doc = {
        "code": code,
        "name": data.name,
        "category": data.category,
        "serialNumber": data.serialNumber,
        "notes": data.notes or "",
        "purchaseDate": data.purchaseDate,
        "purchasePrice": data.purchasePrice,
        "status": "AVAILABLE",
        "assignedToUserId": None,
        "assignedAt": None,
        "createdBy": str(hr["_id"]),
        "createdAt": now,
        "updatedAt": now,
    }

    result = await db.assets.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize_asset(doc)


@hr_router.get("/assets")
async def list_assets(
    status: Optional[str] = Query(None),
    assignedToUserId: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    query: dict = {}
    if status:
        query["status"] = status
    if assignedToUserId:
        query["assignedToUserId"] = assignedToUserId
    if category:
        query["category"] = category
    if search:
        regex = {"$regex": search, "$options": "i"}
        query["$or"] = [
            {"code": regex},
            {"name": regex},
            {"serialNumber": regex},
        ]

    raw = []
    async for a in db.assets.find(query).sort("code", 1):
        raw.append(a)

    user_map = await _user_basics(
        a.get("assignedToUserId") for a in raw
    )

    return [
        _serialize_asset(
            a,
            user_map.get(a.get("assignedToUserId")),
        )
        for a in raw
    ]


@hr_router.get("/assets/{id}")
async def get_asset(
    id: str,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    a = await db.assets.find_one({"_id": oid})
    if not a:
        raise HTTPException(404, "Asset not found")

    user_map = await _user_basics(
        [a.get("assignedToUserId")]
    )

    return _serialize_asset(
        a,
        user_map.get(a.get("assignedToUserId")),
    )


@hr_router.put("/assets/{id}")
async def update_asset(
    id: str,
    data: AssetUpdate,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    update: dict = {
        "updatedAt": datetime.now(timezone.utc),
    }
    for field in (
        "name",
        "category",
        "serialNumber",
        "notes",
        "purchaseDate",
        "purchasePrice",
        "status",
    ):
        v = getattr(data, field)
        if v is not None:
            update[field] = v

    result = await db.assets.update_one(
        {"_id": oid},
        {"$set": update},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Asset not found")

    return {"message": "Asset updated"}


@hr_router.delete("/assets/{id}")
async def delete_asset(
    id: str,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    result = await db.assets.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(404, "Asset not found")

    return {"message": "Asset deleted"}


# ================= HR: ASSIGN / RETURN =================
@hr_router.post("/assets/{id}/assign")
async def assign_asset(
    id: str,
    data: AssetAssign,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    a = await db.assets.find_one({"_id": oid})
    if not a:
        raise HTTPException(404, "Asset not found")

    current_status = a.get("status", "AVAILABLE")
    if current_status != "AVAILABLE":
        raise HTTPException(
            400,
            f"Asset is currently {current_status}; "
            "must be AVAILABLE to assign",
        )

    try:
        user_oid = ObjectId(data.userId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid userId")

    user = await db.users.find_one({"_id": user_oid})
    if not user:
        raise HTTPException(400, "User not found")

    now = datetime.now(timezone.utc)

    await db.assets.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": "ASSIGNED",
                "assignedToUserId": data.userId,
                "assignedAt": now,
                "notes": _append_note(
                    a.get("notes", ""), data.notes
                ),
                "updatedAt": now,
            }
        },
    )

    try:
        await notify_user(
            data.userId,
            "asset_assigned",
            "Asset assigned to you",
            f"{a.get('name', '')} ({a.get('code', '')})",
            {"assetId": str(oid)},
        )
    except Exception:
        pass

    return {"message": "Asset assigned"}


@hr_router.post("/assets/{id}/return")
async def return_asset(
    id: str,
    data: AssetReturn,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    a = await db.assets.find_one({"_id": oid})
    if not a:
        raise HTTPException(404, "Asset not found")

    if a.get("status") != "ASSIGNED":
        raise HTTPException(
            400,
            "Asset is not currently assigned",
        )

    now = datetime.now(timezone.utc)

    await db.assets.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": data.status,
                "assignedToUserId": None,
                "assignedAt": None,
                "notes": _append_note(
                    a.get("notes", ""), data.notes
                ),
                "updatedAt": now,
            }
        },
    )

    return {"message": "Asset returned"}


# ================= HR: ISSUE REPORTS =================
@hr_router.get("/asset-reports")
async def list_asset_reports(
    status: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    query: dict = {}
    if status:
        query["status"] = status

    raw = []
    async for r in db.asset_reports.find(
        query
    ).sort("createdAt", -1):
        raw.append(r)

    asset_map = await _asset_basics(
        r.get("assetId") for r in raw
    )
    user_map = await _user_basics(
        r.get("reportedBy") for r in raw
    )

    return [
        _serialize_report(
            r,
            asset_map.get(r.get("assetId")),
            user_map.get(r.get("reportedBy")),
        )
        for r in raw
    ]


@hr_router.post("/asset-reports/{id}/resolve")
async def resolve_asset_report(
    id: str,
    data: AssetReportResolution,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    report = await db.asset_reports.find_one({"_id": oid})
    if not report:
        raise HTTPException(404, "Report not found")

    if report.get("status") != "PENDING":
        raise HTTPException(
            400,
            f"Already {report.get('status')}",
        )

    now = datetime.now(timezone.utc)
    new_status = (
        "RESOLVED" if data.action == "RESOLVE" else "REJECTED"
    )

    await db.asset_reports.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": new_status,
                "resolution": data.resolution or "",
                "resolvedBy": str(hr["_id"]),
                "resolvedAt": now,
            }
        },
    )

    # On RESOLVE, optionally update the underlying asset.
    if (
        data.action == "RESOLVE"
        and data.newAssetStatus
    ):
        try:
            asset_oid = ObjectId(report["assetId"])
            await db.assets.update_one(
                {"_id": asset_oid},
                {
                    "$set": {
                        "status": data.newAssetStatus,
                        "updatedAt": now,
                    }
                },
            )
        except (InvalidId, TypeError, KeyError):
            pass

    # Tell the employee who raised the issue how it was handled.
    if report.get("reportedBy"):
        await notify_user(
            report["reportedBy"],
            "asset_issue_resolved",
            f"Asset issue {new_status.lower()}",
            data.resolution or "Your reported asset issue was updated.",
            {"reportId": id, "outcome": new_status},
        )

    return {"message": f"Report {new_status.lower()}"}


# ================= USER: MY ASSETS =================
@user_router.get("/mine")
async def my_assets(
    user_id: str = Depends(get_current_user),
):
    raw = []
    async for a in db.assets.find(
        {"assignedToUserId": user_id}
    ).sort("name", 1):
        raw.append(a)
    return [_serialize_asset(a) for a in raw]


# ================= USER: REPORT ISSUE =================
@user_router.post("/{id}/report-issue")
async def report_asset_issue(
    id: str,
    data: AssetIssueReport,
    user_id: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid asset id")

    a = await db.assets.find_one({"_id": oid})
    if not a:
        raise HTTPException(404, "Asset not found")

    if a.get("assignedToUserId") != user_id:
        raise HTTPException(
            403,
            "Asset is not assigned to you",
        )

    description = (data.description or "").strip()
    if not description:
        raise HTTPException(400, "Description is required")

    now = datetime.now(timezone.utc)

    doc = {
        "assetId": id,
        "reportedBy": user_id,
        "reportType": data.reportType,
        "description": description,
        "status": "PENDING",
        "resolution": "",
        "resolvedBy": None,
        "resolvedAt": None,
        "createdAt": now,
    }

    result = await db.asset_reports.insert_one(doc)
    doc["_id"] = result.inserted_id

    # Alert HR that an asset issue needs attention.
    reporter = await db.users.find_one(
        {"_id": ObjectId(user_id)}, {"name": 1}
    )
    who = (reporter or {}).get("name") or "An employee"
    await notify_hr(
        "asset_issue_reported",
        "Asset issue reported",
        f"{who} reported an issue with {a.get('name', 'an asset')} "
        f"({a.get('code', '')})",
        {"reportId": str(result.inserted_id), "assetId": id},
    )

    return _serialize_report(doc)
