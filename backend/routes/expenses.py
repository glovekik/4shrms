from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone

from typing import Optional

from database import db
from utils.dependencies import require_hr, require_hr_or_ceo
from models.expense import ExpenseCreate, ExpenseUpdate

router = APIRouter()


# ================= SERIALIZER =================
def _serialize(e: dict) -> dict:
    return {
        "id": str(e["_id"]),
        "title": e.get("title"),
        "amount": e.get("amount"),
        "category": e.get("category"),
        "date": e.get("date"),
        "description": e.get("description", ""),
        "receiptUrl": e.get("receiptUrl"),
        "vendor": e.get("vendor", ""),
        "paymentMethod": e.get("paymentMethod"),
        "createdBy": e.get("createdBy"),
        "createdAt": (
            e["createdAt"].isoformat()
            if e.get("createdAt") else None
        ),
    }


# ================= CREATE =================
@router.post("")
async def create_expense(
    data: ExpenseCreate,
    hr: dict = Depends(require_hr),
):
    if data.amount < 0:
        raise HTTPException(400, "Amount must be >= 0")

    now = datetime.now(timezone.utc)

    doc = {
        "title": data.title,
        "amount": float(data.amount),
        "category": data.category,
        "date": data.date,
        "description": data.description or "",
        "receiptUrl": data.receiptUrl,
        "vendor": data.vendor or "",
        "paymentMethod": data.paymentMethod,
        "createdBy": str(hr["_id"]),
        "createdAt": now,
        "updatedAt": now,
    }

    result = await db.expenses.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize(doc)


# ================= LIST =================
_SORTABLE_FIELDS = {"date", "amount", "category", "title", "vendor"}


@router.get("")
async def list_expenses(
    fromDate: Optional[str] = Query(None, alias="from"),
    toDate: Optional[str] = Query(None, alias="to"),
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort: str = Query("date"),
    order: str = Query("desc"),
    _hr: dict = Depends(require_hr_or_ceo),
):
    """HR + CEO can read. CEO is read-only (creates/edits still gated to
    HR below).
    """
    query: dict = {}

    date_q: dict = {}
    if fromDate:
        date_q["$gte"] = fromDate
    if toDate:
        date_q["$lte"] = toDate
    if date_q:
        query["date"] = date_q

    if category:
        query["category"] = category

    if search:
        regex = {"$regex": search, "$options": "i"}
        query["$or"] = [
            {"title": regex},
            {"vendor": regex},
            {"description": regex},
        ]

    sort_field = sort if sort in _SORTABLE_FIELDS else "date"
    sort_dir = 1 if order.lower() == "asc" else -1

    raw = []
    async for e in db.expenses.find(query).sort(
        sort_field, sort_dir
    ):
        raw.append(e)

    return [_serialize(e) for e in raw]


# ================= SUMMARY (must be before /{id}) =================
@router.get("/summary")
async def expense_summary(
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    _hr: dict = Depends(require_hr_or_ceo),
):
    now = datetime.now()
    target_year = year or now.year
    target_month = month or now.month

    from_d = f"{target_year}-{target_month:02d}-01"

    if target_month == 12:
        next_year, next_month = target_year + 1, 1
    else:
        next_year, next_month = target_year, target_month + 1

    to_d = f"{next_year}-{next_month:02d}-01"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_d, "$lt": to_d}
            }
        },
        {
            "$group": {
                "_id": "$category",
                "total": {"$sum": "$amount"},
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"total": -1}},
    ]

    by_category = []
    grand_total = 0.0

    async for row in db.expenses.aggregate(pipeline):
        by_category.append({
            "category": row["_id"],
            "total": row["total"],
            "count": row["count"],
        })
        grand_total += row["total"]

    return {
        "year": target_year,
        "month": target_month,
        "totalAmount": grand_total,
        "byCategory": by_category,
    }


# ================= GET ONE =================
@router.get("/{id}")
async def get_expense(
    id: str,
    _hr: dict = Depends(require_hr_or_ceo),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    e = await db.expenses.find_one({"_id": oid})
    if not e:
        raise HTTPException(404, "Expense not found")

    return _serialize(e)


# ================= UPDATE =================
@router.put("/{id}")
async def update_expense(
    id: str,
    data: ExpenseUpdate,
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
        "title",
        "amount",
        "category",
        "date",
        "description",
        "receiptUrl",
        "vendor",
        "paymentMethod",
    ):
        v = getattr(data, field)
        if v is not None:
            update[field] = v

    if "amount" in update and update["amount"] < 0:
        raise HTTPException(400, "Amount must be >= 0")

    result = await db.expenses.update_one(
        {"_id": oid},
        {"$set": update},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Expense not found")

    return {"message": "Expense updated"}


# ================= DELETE =================
@router.delete("/{id}")
async def delete_expense(
    id: str,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    result = await db.expenses.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(404, "Expense not found")

    return {"message": "Expense deleted"}
