from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from io import BytesIO

from bson import ObjectId
from bson.errors import InvalidId

from motor.motor_asyncio import AsyncIOMotorGridFSBucket

from datetime import datetime, timezone, date

from typing import Optional

from uuid import uuid4

from database import db
from utils.dependencies import (
    get_current_user,
    get_current_user_doc,
    require_hr,
)
from utils.pdf import build_experience_letter_pdf
from models.exit import (
    ResignationCreate,
    ResignationDecision,
    ExitTaskStatusUpdate,
    FFSUpdate,
)

# /exit/...   — user-facing
user_router = APIRouter()
# /hr/exits   — HR
hr_router = APIRouter()


# GridFS bucket for experience letter PDFs (lazy init).
_letter_bucket: Optional[AsyncIOMotorGridFSBucket] = None


def _bucket() -> AsyncIOMotorGridFSBucket:
    global _letter_bucket
    if _letter_bucket is None:
        _letter_bucket = AsyncIOMotorGridFSBucket(
            db,
            bucket_name="experience_letters",
        )
    return _letter_bucket


# ================= DEFAULT EXIT CHECKLISTS =================
DEFAULT_HR_EXIT_TASKS = [
    "Disable email & system access",
    "Collect IT assets",
    "Process Full & Final settlement",
    "Issue experience letter",
    "Update employee status to Terminated",
    "Archive employee records",
]

DEFAULT_EMPLOYEE_EXIT_TASKS = [
    "Knowledge transfer with replacement",
    "Return all assigned assets",
    "Submit handover document",
    "Clear pending tasks",
    "Hand over relevant access / credentials",
]


def _new_task(title: str) -> dict:
    return {
        "id": str(uuid4()),
        "title": title,
        "status": "PENDING",
        "note": "",
        "completedAt": None,
    }


# ================= SERIALIZER =================
def _serialize(e: dict, user_info: Optional[dict] = None) -> dict:
    ffs = e.get("ffsCalculation") or {}
    return {
        "id": str(e["_id"]),
        "userId": e.get("userId"),
        "user": user_info,
        "status": e.get("status"),
        "resignationDate": (
            e["resignationDate"].isoformat()
            if e.get("resignationDate") else None
        ),
        "requestedLastWorkingDay": e.get(
            "requestedLastWorkingDay"
        ),
        "approvedLastWorkingDay": e.get(
            "approvedLastWorkingDay"
        ),
        "reason": e.get("reason"),
        "decisionNote": e.get("decisionNote", ""),
        "approvedBy": e.get("approvedBy"),
        "approvedAt": (
            e["approvedAt"].isoformat()
            if e.get("approvedAt") else None
        ),
        "rejectedAt": (
            e["rejectedAt"].isoformat()
            if e.get("rejectedAt") else None
        ),
        "hrTasks": e.get("hrTasks", []),
        "employeeTasks": e.get("employeeTasks", []),
        "ffsCalculation": {
            "pendingSalary": ffs.get("pendingSalary", 0),
            "leaveEncashment": ffs.get("leaveEncashment", 0),
            "bonus": ffs.get("bonus", 0),
            "deductions": ffs.get("deductions", 0),
            "totalPayable": ffs.get("totalPayable", 0),
            "status": ffs.get("status", "DRAFT"),
            "notes": ffs.get("notes", ""),
            "finalizedAt": (
                ffs["finalizedAt"].isoformat()
                if ffs.get("finalizedAt") else None
            ),
            "paidAt": (
                ffs["paidAt"].isoformat()
                if ffs.get("paidAt") else None
            ),
        },
        "experienceLetterFileId": e.get(
            "experienceLetterFileId"
        ),
        "experienceLetterIssuedAt": (
            e["experienceLetterIssuedAt"].isoformat()
            if e.get("experienceLetterIssuedAt") else None
        ),
        "completedAt": (
            e["completedAt"].isoformat()
            if e.get("completedAt") else None
        ),
        "createdAt": (
            e["createdAt"].isoformat()
            if e.get("createdAt") else None
        ),
    }


async def _fetch_user_info(user_id: Optional[str]) -> Optional[dict]:
    if not user_id:
        return None
    try:
        u = await db.users.find_one({"_id": ObjectId(user_id)})
    except (InvalidId, TypeError):
        return None
    if not u:
        return None
    return {
        "id": str(u["_id"]),
        "name": u.get("name"),
        "email": u.get("email"),
    }


async def _users_by_ids(user_ids) -> dict:
    unique = {uid for uid in user_ids if uid}
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
    out: dict = {}
    async for u in db.users.find({"_id": {"$in": oids}}):
        out[str(u["_id"])] = {
            "id": str(u["_id"]),
            "name": u.get("name"),
            "email": u.get("email"),
        }
    return out


# ================= HELPERS =================
async def _load_or_404(id: str) -> dict:
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    e = await db.exits.find_one({"_id": oid})
    if not e:
        raise HTTPException(404, "Exit process not found")
    return e


def _ffs_total(ffs: dict) -> float:
    return round(
        float(ffs.get("pendingSalary", 0) or 0)
        + float(ffs.get("leaveEncashment", 0) or 0)
        + float(ffs.get("bonus", 0) or 0)
        - float(ffs.get("deductions", 0) or 0),
        2,
    )


# ================= USER: RESIGN =================
@user_router.post("/resign")
async def submit_resignation(
    data: ResignationCreate,
    user_id: str = Depends(get_current_user),
):
    # Block duplicate active exits.
    existing = await db.exits.find_one({
        "userId": user_id,
        "status": {
            "$in": ["REQUESTED", "APPROVED", "IN_PROGRESS"]
        },
    })
    if existing:
        raise HTTPException(
            400,
            "You already have an active resignation",
        )

    try:
        last_day = date.fromisoformat(data.requestedLastWorkingDay)
    except (TypeError, ValueError):
        raise HTTPException(
            400,
            "Invalid requestedLastWorkingDay (YYYY-MM-DD)",
        )

    if last_day < date.today():
        raise HTTPException(
            400,
            "requestedLastWorkingDay cannot be in the past",
        )

    reason = (data.reason or "").strip()
    if not reason:
        raise HTTPException(400, "Reason is required")

    now = datetime.now(timezone.utc)
    doc = {
        "userId": user_id,
        "status": "REQUESTED",
        "resignationDate": now,
        "requestedLastWorkingDay": data.requestedLastWorkingDay,
        "approvedLastWorkingDay": None,
        "reason": reason,
        "decisionNote": "",
        "approvedBy": None,
        "approvedAt": None,
        "rejectedAt": None,
        "hrTasks": [
            _new_task(t) for t in DEFAULT_HR_EXIT_TASKS
        ],
        "employeeTasks": [
            _new_task(t) for t in DEFAULT_EMPLOYEE_EXIT_TASKS
        ],
        "ffsCalculation": {
            "pendingSalary": 0,
            "leaveEncashment": 0,
            "bonus": 0,
            "deductions": 0,
            "totalPayable": 0,
            "status": "DRAFT",
            "notes": "",
            "finalizedAt": None,
            "paidAt": None,
        },
        "experienceLetterFileId": None,
        "experienceLetterIssuedAt": None,
        "completedAt": None,
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.exits.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize(doc)


# ================= USER: MINE =================
@user_router.get("/mine")
async def my_exit(
    user_id: str = Depends(get_current_user),
):
    e = await db.exits.find_one(
        {"userId": user_id},
        sort=[("createdAt", -1)],
    )
    if not e:
        return None
    return _serialize(e)


# ================= USER: EMPLOYEE-TASK STATUS =================
@user_router.put("/employee-task-status")
async def update_employee_task_status(
    data: ExitTaskStatusUpdate,
    user_id: str = Depends(get_current_user),
):
    e = await db.exits.find_one(
        {"userId": user_id},
        sort=[("createdAt", -1)],
    )
    if not e:
        raise HTTPException(404, "No exit process found")

    now = datetime.now(timezone.utc)
    set_fields = {
        "employeeTasks.$.status": data.status,
        "employeeTasks.$.note": data.note or "",
        "updatedAt": now,
    }
    set_fields["employeeTasks.$.completedAt"] = (
        now if data.status == "DONE" else None
    )

    result = await db.exits.update_one(
        {
            "_id": e["_id"],
            "employeeTasks.id": data.taskId,
        },
        {"$set": set_fields},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Task not found")
    return {"message": "Task updated"}


# ================= USER: DOWNLOAD OWN LETTER =================
@user_router.get("/experience-letter")
async def my_experience_letter(
    user_id: str = Depends(get_current_user),
):
    e = await db.exits.find_one(
        {"userId": user_id},
        sort=[("createdAt", -1)],
    )
    if not e or not e.get("experienceLetterFileId"):
        raise HTTPException(
            404,
            "Experience letter not yet issued",
        )
    return await _stream_letter(
        e["experienceLetterFileId"],
        e["userId"],
    )


# ================= HR: LIST =================
@hr_router.get("")
async def list_exits(
    status: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    query: dict = {}
    if status:
        query["status"] = status
    raw: list[dict] = []
    async for e in db.exits.find(query).sort(
        "createdAt", -1
    ):
        raw.append(e)
    user_map = await _users_by_ids(e.get("userId") for e in raw)
    return [
        _serialize(e, user_map.get(e.get("userId")))
        for e in raw
    ]


# ================= HR: GET ONE =================
@hr_router.get("/{id}")
async def hr_get_exit(
    id: str,
    _hr: dict = Depends(require_hr),
):
    e = await _load_or_404(id)
    user_info = await _fetch_user_info(e.get("userId"))
    return _serialize(e, user_info)


# ================= HR: APPROVE / REJECT =================
@hr_router.post("/{id}/decide")
async def decide_resignation(
    id: str,
    data: ResignationDecision,
    hr: dict = Depends(require_hr),
):
    e = await _load_or_404(id)
    if e.get("status") != "REQUESTED":
        raise HTTPException(
            400,
            f"Already {e.get('status')}",
        )

    now = datetime.now(timezone.utc)
    hr_id = str(hr["_id"])

    if data.action == "APPROVE":

        if not data.approvedLastWorkingDay:
            raise HTTPException(
                400,
                "approvedLastWorkingDay is required on APPROVE",
            )
        try:
            date.fromisoformat(data.approvedLastWorkingDay)
        except (TypeError, ValueError):
            raise HTTPException(
                400,
                "Invalid approvedLastWorkingDay",
            )

        await db.exits.update_one(
            {"_id": e["_id"]},
            {
                "$set": {
                    "status": "APPROVED",
                    "approvedLastWorkingDay":
                        data.approvedLastWorkingDay,
                    "decisionNote": data.note or "",
                    "approvedBy": hr_id,
                    "approvedAt": now,
                    "updatedAt": now,
                }
            },
        )
        return {"message": "Resignation approved"}

    else:  # REJECT

        await db.exits.update_one(
            {"_id": e["_id"]},
            {
                "$set": {
                    "status": "REJECTED",
                    "decisionNote": data.note or "",
                    "rejectedAt": now,
                    "updatedAt": now,
                }
            },
        )
        return {"message": "Resignation rejected"}


# ================= HR: HR-TASK STATUS =================
@hr_router.put("/{id}/hr-task-status")
async def update_hr_task_status(
    id: str,
    data: ExitTaskStatusUpdate,
    hr: dict = Depends(require_hr),
):
    e = await _load_or_404(id)
    now = datetime.now(timezone.utc)

    set_fields = {
        "hrTasks.$.status": data.status,
        "hrTasks.$.note": data.note or "",
        "updatedAt": now,
    }
    set_fields["hrTasks.$.completedAt"] = (
        now if data.status == "DONE" else None
    )

    # Bump status to IN_PROGRESS once HR starts ticking tasks
    # on an APPROVED resignation.
    if (
        e.get("status") == "APPROVED"
        and data.status == "DONE"
    ):
        set_fields["status"] = "IN_PROGRESS"

    result = await db.exits.update_one(
        {
            "_id": e["_id"],
            "hrTasks.id": data.taskId,
        },
        {"$set": set_fields},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Task not found")
    return {"message": "Task updated"}


# ================= HR: F&F UPDATE =================
@hr_router.put("/{id}/ffs")
async def update_ffs(
    id: str,
    data: FFSUpdate,
    _hr: dict = Depends(require_hr),
):
    e = await _load_or_404(id)
    ffs = dict(e.get("ffsCalculation") or {})

    if ffs.get("status") in ("FINALIZED", "PAID"):
        raise HTTPException(
            400,
            f"F&F is already {ffs.get('status')}",
        )

    for field in (
        "pendingSalary",
        "leaveEncashment",
        "bonus",
        "deductions",
        "notes",
    ):
        v = getattr(data, field)
        if v is not None:
            ffs[field] = v

    ffs["totalPayable"] = _ffs_total(ffs)
    ffs.setdefault("status", "DRAFT")

    now = datetime.now(timezone.utc)
    await db.exits.update_one(
        {"_id": e["_id"]},
        {
            "$set": {
                "ffsCalculation": ffs,
                "updatedAt": now,
            }
        },
    )
    return {"message": "F&F updated", "totalPayable": ffs["totalPayable"]}


@hr_router.post("/{id}/ffs/finalize")
async def finalize_ffs(
    id: str,
    _hr: dict = Depends(require_hr),
):
    e = await _load_or_404(id)
    ffs = dict(e.get("ffsCalculation") or {})
    if ffs.get("status") != "DRAFT":
        raise HTTPException(
            400,
            f"F&F is already {ffs.get('status')}",
        )
    ffs["status"] = "FINALIZED"
    ffs["totalPayable"] = _ffs_total(ffs)
    ffs["finalizedAt"] = datetime.now(timezone.utc)
    await db.exits.update_one(
        {"_id": e["_id"]},
        {
            "$set": {
                "ffsCalculation": ffs,
                "updatedAt": datetime.now(timezone.utc),
            }
        },
    )
    return {"message": "F&F finalized"}


@hr_router.post("/{id}/ffs/mark-paid")
async def mark_ffs_paid(
    id: str,
    _hr: dict = Depends(require_hr),
):
    e = await _load_or_404(id)
    ffs = dict(e.get("ffsCalculation") or {})
    if ffs.get("status") != "FINALIZED":
        raise HTTPException(
            400,
            "F&F must be FINALIZED before marking paid",
        )
    ffs["status"] = "PAID"
    ffs["paidAt"] = datetime.now(timezone.utc)
    await db.exits.update_one(
        {"_id": e["_id"]},
        {
            "$set": {
                "ffsCalculation": ffs,
                "updatedAt": datetime.now(timezone.utc),
            }
        },
    )
    return {"message": "F&F marked as paid"}


# ================= EXPERIENCE LETTER =================
async def _stream_letter(
    file_id: str,
    user_id_str: str,
) -> StreamingResponse:
    try:
        stream = await _bucket().open_download_stream(
            ObjectId(file_id)
        )
    except Exception:
        raise HTTPException(
            404,
            "Experience letter file is missing",
        )
    data = await stream.read()

    user = None
    try:
        user = await db.users.find_one({
            "_id": ObjectId(user_id_str)
        })
    except (InvalidId, TypeError):
        pass

    safe_name = (
        (user.get("name") if user else None) or "Employee"
    ).replace(" ", "_")
    filename = f"Experience_Letter_{safe_name}.pdf"

    return StreamingResponse(
        BytesIO(data),
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            )
        },
    )


@hr_router.post("/{id}/experience-letter")
async def issue_experience_letter(
    id: str,
    _hr: dict = Depends(require_hr),
):
    """Generates the PDF, caches in GridFS, stores fileId on the exit doc.
    Re-issuing replaces the previous file."""
    e = await _load_or_404(id)

    if e.get("status") not in (
        "APPROVED",
        "IN_PROGRESS",
        "COMPLETED",
    ):
        raise HTTPException(
            400,
            "Resignation must be APPROVED before issuing letter",
        )

    try:
        user_oid = ObjectId(e["userId"])
    except (InvalidId, TypeError, KeyError):
        raise HTTPException(500, "Invalid userId on exit doc")

    user = await db.users.find_one({"_id": user_oid})
    if not user:
        raise HTTPException(404, "User not found")

    joining_date = user.get("joiningDate") or "—"
    last_day = (
        e.get("approvedLastWorkingDay")
        or e.get("requestedLastWorkingDay")
        or "—"
    )
    designation = user.get("tag") or "Employee"

    pdf_bytes = build_experience_letter_pdf(
        user,
        joining_date,
        last_day,
        designation,
    )

    # Replace any previous letter.
    if e.get("experienceLetterFileId"):
        try:
            await _bucket().delete(
                ObjectId(e["experienceLetterFileId"])
            )
        except Exception:
            pass

    safe_name = (user.get("name") or "employee").replace(
        " ", "_"
    )
    new_id = await _bucket().upload_from_stream(
        f"experience_letter_{safe_name}.pdf",
        pdf_bytes,
        metadata={
            "exitId": str(e["_id"]),
            "userId": str(user["_id"]),
        },
    )

    now = datetime.now(timezone.utc)
    await db.exits.update_one(
        {"_id": e["_id"]},
        {
            "$set": {
                "experienceLetterFileId": str(new_id),
                "experienceLetterIssuedAt": now,
                "updatedAt": now,
            }
        },
    )

    return {
        "message": "Experience letter issued",
        "fileId": str(new_id),
    }


@hr_router.get("/{id}/experience-letter")
async def hr_download_letter(
    id: str,
    _hr: dict = Depends(require_hr),
):
    e = await _load_or_404(id)
    if not e.get("experienceLetterFileId"):
        raise HTTPException(
            404,
            "Experience letter not yet issued",
        )
    return await _stream_letter(
        e["experienceLetterFileId"],
        e["userId"],
    )


# ================= HR: COMPLETE EXIT =================
@hr_router.post("/{id}/complete")
async def complete_exit(
    id: str,
    _hr: dict = Depends(require_hr),
):
    """Marks the exit COMPLETED and flips the user's status to Terminated."""
    e = await _load_or_404(id)

    if e.get("status") not in (
        "APPROVED",
        "IN_PROGRESS",
    ):
        raise HTTPException(
            400,
            f"Cannot complete from status {e.get('status')}",
        )

    # Sanity check: any assets still assigned to this user?
    if e.get("userId"):
        still_assigned = await db.assets.count_documents({
            "assignedToUserId": e["userId"],
            "status": "ASSIGNED",
        })
        if still_assigned > 0:
            raise HTTPException(
                400,
                f"Employee still has {still_assigned} asset(s) "
                "assigned. Return them before completing the exit.",
            )

    now = datetime.now(timezone.utc)

    await db.exits.update_one(
        {"_id": e["_id"]},
        {
            "$set": {
                "status": "COMPLETED",
                "completedAt": now,
                "updatedAt": now,
            }
        },
    )

    # Flip user status.
    try:
        user_oid = ObjectId(e["userId"])
        await db.users.update_one(
            {"_id": user_oid},
            {
                "$set": {
                    "status": "Terminated",
                    "updatedAt": now,
                }
            },
        )
    except (InvalidId, TypeError, KeyError):
        pass

    return {"message": "Exit completed; user marked Terminated"}
