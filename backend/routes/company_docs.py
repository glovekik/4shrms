"""Company-wide documents (policies, handbooks, forms).

Distinct from per-employee documents under /me/documents and
/hr/users/{id}/documents — those are tied to an employee record. These
are org-wide: HR uploads once, everyone authenticated can read.
"""

from fastapi import APIRouter, Depends, HTTPException

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel

from database import db
from utils.dependencies import get_current_user, require_hr
from utils.audit import log_audit


user_router = APIRouter()  # /company-docs — any authed user can list
hr_router = APIRouter()    # /hr/company-docs — HR creates/updates/deletes


class CompanyDocumentCreate(BaseModel):
    title: str
    category: str           # e.g. "Policy", "Handbook", "Form", "Notice"
    fileName: str
    fileUrl: str
    description: Optional[str] = None
    effectiveFrom: Optional[str] = None  # YYYY-MM-DD
    expiresOn: Optional[str] = None      # YYYY-MM-DD


class CompanyDocumentUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    fileName: Optional[str] = None
    fileUrl: Optional[str] = None
    description: Optional[str] = None
    effectiveFrom: Optional[str] = None
    expiresOn: Optional[str] = None


def _serialize(d: dict) -> dict:
    return {
        "id": str(d["_id"]),
        "title": d.get("title"),
        "category": d.get("category"),
        "fileName": d.get("fileName"),
        "fileUrl": d.get("fileUrl"),
        "description": d.get("description"),
        "effectiveFrom": d.get("effectiveFrom"),
        "expiresOn": d.get("expiresOn"),
        "uploadedBy": d.get("uploadedBy"),
        "uploadedAt": (
            d["uploadedAt"].isoformat() if d.get("uploadedAt") else None
        ),
        "updatedAt": (
            d["updatedAt"].isoformat() if d.get("updatedAt") else None
        ),
    }


# ================= USER: LIST =================
@user_router.get("")
async def list_company_documents(
    _user_id: str = Depends(get_current_user),
):
    """Any authed user can list company documents. Newest first."""
    out: list[dict] = []
    async for d in db.company_documents.find().sort("uploadedAt", -1):
        out.append(_serialize(d))
    return out


# ================= HR: CRUD =================
@hr_router.post("")
async def hr_create_company_document(
    data: CompanyDocumentCreate,
    hr: dict = Depends(require_hr),
):
    title = (data.title or "").strip()
    file_name = (data.fileName or "").strip()
    file_url = (data.fileUrl or "").strip()
    category = (data.category or "").strip()
    if not title or not file_name or not file_url or not category:
        raise HTTPException(
            400, "title, category, fileName, fileUrl are required",
        )

    now = datetime.now(timezone.utc)
    doc = {
        "title": title,
        "category": category,
        "fileName": file_name,
        "fileUrl": file_url,
        "description": data.description or "",
        "effectiveFrom": data.effectiveFrom,
        "expiresOn": data.expiresOn,
        "uploadedBy": str(hr["_id"]),
        "uploadedAt": now,
        "updatedAt": now,
    }
    result = await db.company_documents.insert_one(doc)
    doc["_id"] = result.inserted_id

    await log_audit(
        actor_id=str(hr["_id"]),
        action="company_document.create",
        entity_type="company_documents",
        entity_id=str(result.inserted_id),
        after={"title": title, "category": category},
    )
    return _serialize(doc)


@hr_router.put("/{id}")
async def hr_update_company_document(
    id: str,
    data: CompanyDocumentUpdate,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    update: dict = {"updatedAt": datetime.now(timezone.utc)}
    for field in (
        "title",
        "category",
        "fileName",
        "fileUrl",
        "description",
        "effectiveFrom",
        "expiresOn",
    ):
        val = getattr(data, field)
        if val is not None:
            update[field] = val

    result = await db.company_documents.update_one(
        {"_id": oid}, {"$set": update}
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Document not found")

    await log_audit(
        actor_id=str(hr["_id"]),
        action="company_document.update",
        entity_type="company_documents",
        entity_id=id,
        after={k: v for k, v in update.items() if k != "updatedAt"},
    )
    return {"message": "Document updated"}


@hr_router.delete("/{id}")
async def hr_delete_company_document(
    id: str,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    existing = await db.company_documents.find_one({"_id": oid})
    result = await db.company_documents.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(404, "Document not found")

    await log_audit(
        actor_id=str(hr["_id"]),
        action="company_document.delete",
        entity_type="company_documents",
        entity_id=id,
        before=_serialize(existing) if existing else None,
    )
    return {"message": "Document deleted"}
