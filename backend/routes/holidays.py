from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone, date

from typing import Optional

from database import db
from utils.notify import notify_all_active
from utils.dependencies import (
    get_current_user,
    require_hr,
)
from models.holiday import HolidayCreate, HolidayUpdate

# /holidays      — anyone authenticated
user_router = APIRouter()
# /hr/holidays   — HR
hr_router = APIRouter()


def _serialize(h: dict) -> dict:
    return {
        "id": str(h["_id"]),
        "date": h.get("date"),
        "name": h.get("name"),
        "description": h.get("description", ""),
    }


def _validate_date(s: str) -> str:
    try:
        date.fromisoformat(s)
        return s
    except (TypeError, ValueError):
        raise HTTPException(
            400,
            "Invalid date (YYYY-MM-DD)",
        )


# ================= USER: LIST (read-only) =================
@user_router.get("")
async def list_holidays(
    year: Optional[int] = Query(None),
    fromDate: Optional[str] = Query(None, alias="from"),
    toDate: Optional[str] = Query(None, alias="to"),
    _user_id: str = Depends(get_current_user),
):
    query: dict = {}

    if year:
        query["date"] = {
            "$gte": f"{year}-01-01",
            "$lte": f"{year}-12-31",
        }
    else:
        date_q: dict = {}
        if fromDate:
            date_q["$gte"] = fromDate
        if toDate:
            date_q["$lte"] = toDate
        if date_q:
            query["date"] = date_q

    items = []
    async for h in db.holidays.find(query).sort("date", 1):
        items.append(_serialize(h))
    return items


# ================= HR: LIST =================
@hr_router.get("")
async def hr_list_holidays(
    fromDate: Optional[str] = Query(None),
    toDate: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    """Same shape as the user-facing list; included so HR has parity in
    Swagger and frontends scoped to /hr/* don't have to know the public
    /holidays path."""
    query: dict = {}
    if fromDate or toDate:
        date_q: dict = {}
        if fromDate:
            date_q["$gte"] = fromDate
        if toDate:
            date_q["$lte"] = toDate
        if date_q:
            query["date"] = date_q

    items = []
    async for h in db.holidays.find(query).sort("date", 1):
        items.append(_serialize(h))
    return items


# ================= HR: CREATE =================
@hr_router.post("")
async def create_holiday(
    data: HolidayCreate,
    _hr: dict = Depends(require_hr),
):
    _validate_date(data.date)

    existing = await db.holidays.find_one({
        "date": data.date,
    })
    if existing:
        raise HTTPException(
            400,
            f"Holiday already exists on {data.date}",
        )

    now = datetime.now(timezone.utc)
    doc = {
        "date": data.date,
        "name": data.name,
        "description": data.description or "",
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.holidays.insert_one(doc)
    doc["_id"] = result.inserted_id

    # Announce the new holiday to everyone.
    await notify_all_active(
        "holiday_declared",
        "Holiday declared",
        f"{data.name} — {data.date}",
        {"holidayId": str(result.inserted_id), "date": data.date},
    )

    return _serialize(doc)


# ================= HR: UPDATE =================
@hr_router.put("/{id}")
async def update_holiday(
    id: str,
    data: HolidayUpdate,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    update: dict = {
        "updatedAt": datetime.now(timezone.utc),
    }
    if data.name is not None:
        update["name"] = data.name
    if data.description is not None:
        update["description"] = data.description

    result = await db.holidays.update_one(
        {"_id": oid},
        {"$set": update},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Holiday not found")
    return {"message": "Holiday updated"}


# ================= HR: DELETE =================
@hr_router.delete("/{id}")
async def delete_holiday(
    id: str,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    result = await db.holidays.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(404, "Holiday not found")
    return {"message": "Holiday deleted"}
