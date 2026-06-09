from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone

from typing import Optional

from uuid import uuid4

from database import db
from utils.notify import notify_user
from utils.dependencies import (
    get_current_user,
    require_hr,
)
from utils.email import send_email_with_pdf
from config import COMPANY_NAME, is_email_configured
from models.onboarding import (
    OnboardingCreate,
    DocumentUpload,
    DocumentStatusUpdate,
    TaskStatusUpdate,
)

# /onboarding/...   — user-facing
user_router = APIRouter()
# /hr/onboardings   — HR
hr_router = APIRouter()


# ================= DEFAULT CHECKLISTS =================
DEFAULT_DOCUMENTS = [
    ("Aadhaar Card",                    True),
    ("PAN Card",                        True),
    ("Passport-size Photo",             True),
    ("Educational Certificates",        True),
    ("Previous Employment Letter",      False),
    ("Address Proof",                   True),
    ("Bank Cheque / Passbook copy",     True),
]

DEFAULT_HR_TASKS = [
    "Create work email",
    "Add to Slack / Teams",
    "Send welcome email",
    "Assign laptop",
    "Add to HRMS",
    "Schedule orientation",
]

DEFAULT_EMPLOYEE_TASKS = [
    "Fill bank details",
    "Submit required documents",
    "Complete profile",
    "Read employee handbook",
    "Sign NDA",
]


def _new_doc_item(title: str, required: bool) -> dict:
    return {
        "id": str(uuid4()),
        "title": title,
        "required": required,
        "status": "PENDING",
        "fileUrl": None,
        "note": "",
        "uploadedAt": None,
        "verifiedAt": None,
        "verifiedBy": None,
    }


def _new_task_item(title: str) -> dict:
    return {
        "id": str(uuid4()),
        "title": title,
        "status": "PENDING",
        "note": "",
        "completedAt": None,
        "completedBy": None,
    }


# ================= SERIALIZER =================
def _serialize(o: dict) -> dict:
    return {
        "id": str(o["_id"]),
        "userId": o.get("userId"),
        "status": o.get("status", "PENDING"),
        "documents": o.get("documents", []),
        "hrTasks": o.get("hrTasks", []),
        "employeeTasks": o.get("employeeTasks", []),
        "welcomeEmailSent": o.get("welcomeEmailSent", False),
        "welcomeEmailSentAt": (
            o["welcomeEmailSentAt"].isoformat()
            if o.get("welcomeEmailSentAt") else None
        ),
        "startedAt": (
            o["startedAt"].isoformat()
            if o.get("startedAt") else None
        ),
        "completedAt": (
            o["completedAt"].isoformat()
            if o.get("completedAt") else None
        ),
    }


# ================= HELPERS =================
async def _load_or_404(id: str) -> dict:
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    o = await db.onboardings.find_one({"_id": oid})
    if not o:
        raise HTTPException(404, "Onboarding not found")
    return o


# ================= HR: CREATE =================
@hr_router.post("")
async def create_onboarding(
    data: OnboardingCreate,
    hr: dict = Depends(require_hr),
):
    try:
        user_oid = ObjectId(data.userId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid userId")

    user = await db.users.find_one({"_id": user_oid})
    if not user:
        raise HTTPException(404, "User not found")

    existing = await db.onboardings.find_one({
        "userId": data.userId,
    })
    if existing:
        raise HTTPException(
            400,
            "Onboarding already exists for this user",
        )

    now = datetime.now(timezone.utc)
    doc = {
        "userId": data.userId,
        "status": "IN_PROGRESS",
        "documents": [
            _new_doc_item(t, req)
            for t, req in DEFAULT_DOCUMENTS
        ],
        "hrTasks": [
            _new_task_item(t) for t in DEFAULT_HR_TASKS
        ],
        "employeeTasks": [
            _new_task_item(t) for t in DEFAULT_EMPLOYEE_TASKS
        ],
        "welcomeEmailSent": False,
        "welcomeEmailSentAt": None,
        "startedAt": now,
        "completedAt": None,
        "createdBy": str(hr["_id"]),
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.onboardings.insert_one(doc)
    doc["_id"] = result.inserted_id

    # Welcome the new hire + point them at their onboarding tasks.
    await notify_user(
        data.userId,
        "onboarding_started",
        "Welcome aboard!",
        "Your onboarding has started — check your tasks and documents.",
        {"onboardingId": str(result.inserted_id)},
    )

    return _serialize(doc)


# ================= HR: LIST =================
@hr_router.get("")
async def list_onboardings(
    status: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    query: dict = {}
    if status:
        query["status"] = status
    items = []
    async for o in db.onboardings.find(query).sort(
        "startedAt", -1
    ):
        items.append(_serialize(o))
    return items


# ================= HR: GET ONE =================
@hr_router.get("/{id}")
async def get_onboarding(
    id: str,
    _hr: dict = Depends(require_hr),
):
    o = await _load_or_404(id)
    return _serialize(o)


# ================= HR: WELCOME EMAIL =================
@hr_router.post("/{id}/welcome-email")
async def send_welcome_email(
    id: str,
    _hr: dict = Depends(require_hr),
):
    if not is_email_configured():
        raise HTTPException(
            503,
            "Email is not configured (SMTP_HOST and SMTP_FROM env vars required)",
        )

    o = await _load_or_404(id)

    try:
        user_oid = ObjectId(o["userId"])
    except (InvalidId, TypeError):
        raise HTTPException(500, "Onboarding has invalid userId")

    user = await db.users.find_one({"_id": user_oid})
    if not user or not user.get("email"):
        raise HTTPException(
            400,
            "Recipient user has no email address",
        )

    subject = f"Welcome to {COMPANY_NAME}!"
    body = (
        f"Hi {user.get('name', 'there')},\n\n"
        f"Welcome to {COMPANY_NAME}! We're glad to have you on board.\n\n"
        "Your HR will reach out with onboarding details. In the meantime, "
        "please log in to the HR app and complete your joining tasks "
        "and document uploads.\n\n"
        "If you have any questions, reach out to your HR contact.\n\n"
        f"Regards,\n{COMPANY_NAME}"
    )

    # send_email_with_pdf supports an empty attachment too — but we want a
    # plain email here. Reuse by passing empty bytes is awkward; do it inline.
    import asyncio
    import smtplib
    from email.mime.text import MIMEText
    from config import (
        SMTP_HOST, SMTP_PORT, SMTP_USERNAME,
        SMTP_PASSWORD, SMTP_FROM, SMTP_USE_TLS,
    )

    def _send_plain():
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = user["email"]
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls()
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)

    try:
        await asyncio.to_thread(_send_plain)
    except Exception as e:
        raise HTTPException(502, f"Email send failed: {e}")

    now = datetime.now(timezone.utc)
    await db.onboardings.update_one(
        {"_id": o["_id"]},
        {
            "$set": {
                "welcomeEmailSent": True,
                "welcomeEmailSentAt": now,
                "updatedAt": now,
            }
        },
    )
    return {"message": "Welcome email sent", "to": user["email"]}


# ================= HR: VERIFY DOCUMENT =================
@hr_router.put("/{id}/document-status")
async def update_document_status(
    id: str,
    data: DocumentStatusUpdate,
    hr: dict = Depends(require_hr),
):
    o = await _load_or_404(id)

    now = datetime.now(timezone.utc)

    set_fields = {
        "documents.$.status": data.status,
        "documents.$.note": data.note or "",
        "updatedAt": now,
    }
    if data.status in ("VERIFIED", "REJECTED"):
        set_fields["documents.$.verifiedAt"] = now
        set_fields["documents.$.verifiedBy"] = str(hr["_id"])

    result = await db.onboardings.update_one(
        {
            "_id": o["_id"],
            "documents.id": data.documentId,
        },
        {"$set": set_fields},
    )

    if result.matched_count == 0:
        raise HTTPException(404, "Document item not found")

    return {"message": "Document updated"}


# ================= HR: HR-TASK STATUS =================
@hr_router.put("/{id}/hr-task-status")
async def update_hr_task_status(
    id: str,
    data: TaskStatusUpdate,
    hr: dict = Depends(require_hr),
):
    o = await _load_or_404(id)

    now = datetime.now(timezone.utc)
    set_fields = {
        "hrTasks.$.status": data.status,
        "hrTasks.$.note": data.note or "",
        "updatedAt": now,
    }
    if data.status == "DONE":
        set_fields["hrTasks.$.completedAt"] = now
        set_fields["hrTasks.$.completedBy"] = str(hr["_id"])
    else:
        set_fields["hrTasks.$.completedAt"] = None
        set_fields["hrTasks.$.completedBy"] = None

    result = await db.onboardings.update_one(
        {
            "_id": o["_id"],
            "hrTasks.id": data.taskId,
        },
        {"$set": set_fields},
    )

    if result.matched_count == 0:
        raise HTTPException(404, "Task not found")

    return {"message": "Task updated"}


# ================= HR: COMPLETE =================
@hr_router.post("/{id}/complete")
async def complete_onboarding(
    id: str,
    _hr: dict = Depends(require_hr),
):
    o = await _load_or_404(id)
    now = datetime.now(timezone.utc)
    await db.onboardings.update_one(
        {"_id": o["_id"]},
        {
            "$set": {
                "status": "COMPLETED",
                "completedAt": now,
                "updatedAt": now,
            }
        },
    )

    # Let the HR who owns this onboarding know it's done.
    if o.get("createdBy"):
        emp = await db.users.find_one(
            {"_id": ObjectId(o["userId"])}, {"name": 1}
        ) if o.get("userId") else None
        who = (emp or {}).get("name") or "An employee"
        await notify_user(
            o["createdBy"],
            "onboarding_completed",
            "Onboarding completed",
            f"Onboarding for {who} is complete.",
            {"onboardingId": str(o["_id"])},
        )

    return {"message": "Onboarding completed"}


# ================= HR: DELETE =================
@hr_router.delete("/{id}")
async def delete_onboarding(
    id: str,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    result = await db.onboardings.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(404, "Onboarding not found")
    return {"message": "Onboarding deleted"}


# ================= USER: MINE =================
@user_router.get("/mine")
async def my_onboarding(
    user_id: str = Depends(get_current_user),
):
    o = await db.onboardings.find_one({"userId": user_id})
    if not o:
        return None
    return _serialize(o)


# ================= USER: DOCUMENT UPLOAD =================
@user_router.post("/document-upload")
async def upload_document(
    data: DocumentUpload,
    user_id: str = Depends(get_current_user),
):
    o = await db.onboardings.find_one({"userId": user_id})
    if not o:
        raise HTTPException(404, "No onboarding for this user")

    if not (data.fileUrl or "").strip():
        raise HTTPException(400, "fileUrl is required")

    now = datetime.now(timezone.utc)
    result = await db.onboardings.update_one(
        {
            "_id": o["_id"],
            "documents.id": data.documentId,
        },
        {
            "$set": {
                "documents.$.status": "UPLOADED",
                "documents.$.fileUrl": data.fileUrl,
                "documents.$.uploadedAt": now,
                "updatedAt": now,
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Document item not found")

    return {"message": "Document uploaded"}


# ================= USER: EMPLOYEE-TASK STATUS =================
@user_router.put("/employee-task-status")
async def update_employee_task_status(
    data: TaskStatusUpdate,
    user_id: str = Depends(get_current_user),
):
    o = await db.onboardings.find_one({"userId": user_id})
    if not o:
        raise HTTPException(404, "No onboarding for this user")

    now = datetime.now(timezone.utc)
    set_fields = {
        "employeeTasks.$.status": data.status,
        "employeeTasks.$.note": data.note or "",
        "updatedAt": now,
    }
    if data.status == "DONE":
        set_fields["employeeTasks.$.completedAt"] = now
    else:
        set_fields["employeeTasks.$.completedAt"] = None

    result = await db.onboardings.update_one(
        {
            "_id": o["_id"],
            "employeeTasks.id": data.taskId,
        },
        {"$set": set_fields},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Task not found")

    return {"message": "Task updated"}
