"""Per-employee document store.

This is the rich version (one row per document, with category/uploader/
timestamp). The `user.documents` dict on the user profile is the legacy
single-slot-per-category shape — both can coexist; this collection is
the source of truth for richer flows (multiple ID copies, audit, etc.).
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from database import db
from utils.dependencies import (
    get_current_user,
    require_hr,
    require_hr_or_ceo,
)
from utils.audit import log_audit
from models.document import DocumentCreate, RequiredDocumentsSet


# /me/documents  — current user
user_router = APIRouter()
# /hr/users/{userId}/documents — HR view of any user
hr_router = APIRouter()


def _serialize(d: dict) -> dict:
    return {
        "id": str(d["_id"]),
        "userId": d.get("userId"),
        "category": d.get("category"),
        "fileName": d.get("fileName"),
        "fileUrl": d.get("fileUrl"),
        "notes": d.get("notes"),
        "expiresOn": d.get("expiresOn"),
        "uploadedBy": d.get("uploadedBy"),
        "uploadedByRole": d.get("uploadedByRole"),
        "lockedByHR": bool(d.get("lockedByHR", False)),
        "uploadedAt": (
            d["uploadedAt"].isoformat()
            if d.get("uploadedAt") else None
        ),
    }


async def _insert_document(
    target_user_id: str,
    data: DocumentCreate,
    uploader_id: str,
    uploader_role: str,
) -> dict:
    category = (data.category or "").strip()
    file_name = (data.fileName or "").strip()
    file_url = (data.fileUrl or "").strip()
    if not category or not file_name or not file_url:
        raise HTTPException(
            400, "category, fileName, fileUrl are required",
        )

    # HR uploads on behalf of the employee are locked so the employee
    # can't replace/delete them — protects offer letters, signed forms,
    # ID copies HR has already verified.
    locked = uploader_role == "HR" and uploader_id != target_user_id

    now = datetime.now(timezone.utc)
    doc = {
        "userId": target_user_id,
        "category": category,
        "fileName": file_name,
        "fileUrl": file_url,
        "notes": data.notes or "",
        "expiresOn": data.expiresOn,
        "uploadedBy": uploader_id,
        "uploadedByRole": uploader_role,
        "lockedByHR": locked,
        "uploadedAt": now,
    }
    result = await db.documents.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


# ================= USER: MINE =================
@user_router.get("")
async def list_my_documents(
    category: Optional[str] = Query(None),
    user_id: str = Depends(get_current_user),
):
    query: dict = {"userId": user_id}
    if category:
        query["category"] = category
    out = []
    async for d in db.documents.find(query).sort("uploadedAt", -1):
        out.append(_serialize(d))
    return out


@user_router.post("")
async def upload_my_document(
    data: DocumentCreate,
    user_id: str = Depends(get_current_user),
):
    # Block the employee from overwriting an HR-locked category. Re-upload
    # is allowed only if the existing latest doc for this category was not
    # locked by HR — checked here (not via index) because the docs table
    # is append-only.
    locked_existing = await db.documents.find_one(
        {
            "userId": user_id,
            "category": data.category,
            "lockedByHR": True,
        },
        sort=[("uploadedAt", -1)],
    )
    if locked_existing:
        raise HTTPException(
            400,
            "This document was uploaded by HR and is locked. "
            "Ask HR to replace it.",
        )

    doc = await _insert_document(user_id, data, user_id, "USER")

    # If HR has marked this category as required and still pending, flip
    # it to UPLOADED so the employee's pending list shrinks automatically.
    await db.users.update_one(
        {
            "_id": ObjectId(user_id),
            "requiredDocuments.category": data.category,
        },
        {
            "$set": {
                "requiredDocuments.$.status": "UPLOADED",
                "requiredDocuments.$.uploadedAt":
                    doc["uploadedAt"],
                "requiredDocuments.$.documentId":
                    str(doc["_id"]),
            }
        },
    )

    return _serialize(doc)


# ================= REQUIRED DOCUMENTS (employee view) =================
@user_router.get("/required")
async def my_required_documents(
    user_id: str = Depends(get_current_user),
):
    """Returns the list HR set for the employee. Each item:
       { category, status, note, uploadedAt?, documentId? }
    """
    u = await db.users.find_one(
        {"_id": ObjectId(user_id)},
        {"requiredDocuments": 1},
    )
    return (u or {}).get("requiredDocuments", [])


# ================= REQUIRED DOCUMENTS (HR set) =================
@hr_router.put("/{userId}/required-documents")
async def hr_set_required_documents(
    userId: str,
    data: RequiredDocumentsSet,
    hr: dict = Depends(require_hr),
):
    """HR replaces the required-docs list for an employee.

    Preserves status for categories that were previously UPLOADED or
    VERIFIED — HR sets the *expected* list, the employee fills it in.
    """
    try:
        oid = ObjectId(userId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid userId")
    if not await db.users.find_one({"_id": oid}):
        raise HTTPException(404, "User not found")

    u = await db.users.find_one(
        {"_id": oid}, {"requiredDocuments": 1}
    )
    prior = {
        item.get("category"): item
        for item in (u or {}).get("requiredDocuments", [])
        if item.get("category")
    }

    new_items: list[dict] = []
    for item in data.items:
        category = (item.category or "").strip()
        if not category:
            continue
        existing = prior.get(category)
        record = {
            "category": category,
            "note": item.note or "",
            "status": (
                existing.get("status")
                if existing and existing.get("status") in (
                    "UPLOADED", "VERIFIED"
                )
                else "PENDING"
            ),
        }
        if existing:
            for k in ("uploadedAt", "documentId", "verifiedAt"):
                if existing.get(k):
                    record[k] = existing[k]
        new_items.append(record)

    await db.users.update_one(
        {"_id": oid},
        {
            "$set": {
                "requiredDocuments": new_items,
                "updatedAt": datetime.now(timezone.utc),
            }
        },
    )

    await log_audit(
        actor_id=str(hr["_id"]),
        action="required_documents.set",
        entity_type="users",
        entity_id=userId,
        after={"categories": [i["category"] for i in new_items]},
    )

    return {"items": new_items}


@hr_router.get("/{userId}/required-documents")
async def hr_list_required_documents(
    userId: str,
    _hr: dict = Depends(require_hr_or_ceo),
):
    try:
        oid = ObjectId(userId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid userId")
    u = await db.users.find_one(
        {"_id": oid}, {"requiredDocuments": 1}
    )
    if not u:
        raise HTTPException(404, "User not found")
    return u.get("requiredDocuments", [])


@hr_router.post("/{userId}/required-documents/{category}/verify")
async def hr_verify_required_document(
    userId: str,
    category: str,
    hr: dict = Depends(require_hr),
):
    """HR marks an uploaded document as VERIFIED. Only meaningful once
    the employee has uploaded it (status=UPLOADED)."""
    try:
        oid = ObjectId(userId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid userId")

    now = datetime.now(timezone.utc)
    result = await db.users.update_one(
        {
            "_id": oid,
            "requiredDocuments": {
                "$elemMatch": {
                    "category": category,
                    "status": {"$in": ["UPLOADED", "VERIFIED"]},
                }
            },
        },
        {
            "$set": {
                "requiredDocuments.$.status": "VERIFIED",
                "requiredDocuments.$.verifiedAt": now,
                "requiredDocuments.$.verifiedBy": str(hr["_id"]),
                "updatedAt": now,
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(
            400,
            "No uploaded document found for that category",
        )
    return {"message": "Document verified"}


@user_router.delete("/{id}")
async def delete_my_document(
    id: str,
    user_id: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    existing = await db.documents.find_one(
        {"_id": oid, "userId": user_id}
    )
    if not existing:
        raise HTTPException(404, "Document not found")
    if existing.get("lockedByHR"):
        raise HTTPException(
            400,
            "This document was uploaded by HR and cannot be deleted by you.",
        )
    await db.documents.delete_one({"_id": oid, "userId": user_id})
    return {"message": "Document deleted"}


# ================= HR: VIEW / UPLOAD / DELETE ANY USER =================
@hr_router.get("/{userId}/documents")
async def list_user_documents(
    userId: str,
    category: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    try:
        ObjectId(userId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid userId")
    query: dict = {"userId": userId}
    if category:
        query["category"] = category
    out = []
    async for d in db.documents.find(query).sort("uploadedAt", -1):
        out.append(_serialize(d))
    return out


@hr_router.post("/{userId}/documents")
async def hr_upload_document(
    userId: str,
    data: DocumentCreate,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(userId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid userId")
    if not await db.users.find_one({"_id": oid}):
        raise HTTPException(404, "User not found")

    doc = await _insert_document(userId, data, str(hr["_id"]), "HR")
    await log_audit(
        actor_id=str(hr["_id"]),
        action="document.upload",
        entity_type="documents",
        entity_id=str(doc["_id"]),
        after={
            "userId": userId,
            "category": doc["category"],
            "fileName": doc["fileName"],
        },
    )
    return _serialize(doc)


@hr_router.delete("/{userId}/documents/{id}")
async def hr_delete_document(
    userId: str,
    id: str,
    hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    result = await db.documents.delete_one(
        {"_id": oid, "userId": userId}
    )
    if result.deleted_count == 0:
        raise HTTPException(404, "Document not found")
    await log_audit(
        actor_id=str(hr["_id"]),
        action="document.delete",
        entity_type="documents",
        entity_id=id,
        before={"userId": userId},
    )
    return {"message": "Document deleted"}
