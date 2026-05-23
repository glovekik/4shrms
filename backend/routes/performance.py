"""Performance management — PRD section 18.

Three sub-modules in one file because they're rarely used independently:
  - Goals (KPIs): manager assigns to direct report, employee updates progress
  - Reviews: structured periodic review with self + manager eval + acknowledge
  - Feedback: 360-degree feedback, optionally anonymous
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from bson import ObjectId
from bson.errors import InvalidId

from datetime import datetime, timezone
from typing import Optional

from database import db
from utils.dependencies import (
    get_current_user,
    get_current_user_doc,
    require_hr,
    require_manager_or_hr,
    can_decide_for_employee,
)
from utils.audit import log_audit
from utils.notify import notify_user
from models.performance import (
    GoalCreate, GoalUpdate, GoalProgress,
    ReviewCreate, ReviewSelfEval, ReviewManagerEval, ReviewAcknowledge,
    FeedbackCreate,
)


# ================= ROUTERS =================
goals_router = APIRouter()        # /goals (user-facing)
goals_mgr_router = APIRouter()    # /manager/goals
goals_hr_router = APIRouter()     # /hr/goals

reviews_router = APIRouter()      # /reviews (user-facing)
reviews_mgr_router = APIRouter()  # /manager/reviews
reviews_hr_router = APIRouter()   # /hr/reviews

feedback_router = APIRouter()     # /feedback (user-facing)
feedback_hr_router = APIRouter()  # /hr/feedback


# ================= GOALS =================
def _serialize_goal(g: dict) -> dict:
    return {
        "id": str(g["_id"]),
        "userId": g.get("userId"),
        "title": g.get("title"),
        "description": g.get("description"),
        "dueDate": g.get("dueDate"),
        "targetValue": g.get("targetValue"),
        "achievedValue": g.get("achievedValue", 0.0),
        "unit": g.get("unit"),
        "weight": g.get("weight"),
        "status": g.get("status", "ACTIVE"),
        "createdBy": g.get("createdBy"),
        "createdAt": (
            g["createdAt"].isoformat()
            if g.get("createdAt") else None
        ),
        "completedAt": (
            g["completedAt"].isoformat()
            if g.get("completedAt") else None
        ),
        "progressNotes": g.get("progressNotes", []),
    }


@goals_router.get("/mine")
async def list_my_goals(
    status: Optional[str] = Query(None),
    user_id: str = Depends(get_current_user),
):
    query: dict = {"userId": user_id}
    if status:
        query["status"] = status
    out = []
    async for g in db.goals.find(query).sort("createdAt", -1):
        out.append(_serialize_goal(g))
    return out


@goals_router.post("/{id}/progress")
async def update_my_goal_progress(
    id: str,
    data: GoalProgress,
    user_id: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    g = await db.goals.find_one({"_id": oid, "userId": user_id})
    if not g:
        raise HTTPException(404, "Goal not found")

    now = datetime.now(timezone.utc)
    await db.goals.update_one(
        {"_id": oid},
        {
            "$set": {
                "achievedValue": float(data.achievedValue),
                "updatedAt": now,
            },
            "$push": {
                "progressNotes": {
                    "at": now,
                    "achievedValue": float(data.achievedValue),
                    "note": data.note or "",
                }
            },
        },
    )
    return {"message": "Progress updated"}


@goals_mgr_router.post("")
async def manager_create_goal(
    data: GoalCreate,
    actor: dict = Depends(require_manager_or_hr),
):
    """Manager assigns a goal to one of their direct reports (or HR
    assigns to anyone)."""
    try:
        emp_oid = ObjectId(data.userId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid userId")
    employee = await db.users.find_one({"_id": emp_oid})
    if not employee:
        raise HTTPException(400, "User not found")
    if not can_decide_for_employee(actor, employee):
        raise HTTPException(
            403, "Not one of your direct reports",
        )

    now = datetime.now(timezone.utc)
    doc = data.model_dump(exclude_none=True)
    doc["status"] = "ACTIVE"
    doc["achievedValue"] = 0.0
    doc["progressNotes"] = []
    doc["createdBy"] = str(actor["_id"])
    doc["createdAt"] = now
    doc["updatedAt"] = now
    result = await db.goals.insert_one(doc)

    await notify_user(
        data.userId,
        "goal_assigned",
        "New goal assigned",
        data.title,
        {"goalId": str(result.inserted_id)},
    )
    await log_audit(
        actor_id=str(actor["_id"]),
        action="goal.create",
        entity_type="goals",
        entity_id=str(result.inserted_id),
        after={"userId": data.userId, "title": data.title},
    )
    return {"id": str(result.inserted_id), "message": "Goal assigned"}


@goals_mgr_router.get("")
async def manager_list_goals(
    userId: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    actor: dict = Depends(require_manager_or_hr),
):
    actor_id = str(actor["_id"])
    if actor.get("role") == "HR":
        scope_ids = None
    else:
        scope_ids = [
            str(u["_id"])
            async for u in db.users.find(
                {"reportingManagerId": actor_id}, {"_id": 1}
            )
        ]
        if not scope_ids:
            return []

    query: dict = {}
    if userId:
        query["userId"] = userId
    if status:
        query["status"] = status
    if scope_ids is not None:
        query["userId"] = {"$in": scope_ids}

    out = []
    async for g in db.goals.find(query).sort("createdAt", -1):
        out.append(_serialize_goal(g))
    return out


@goals_mgr_router.put("/{id}")
async def manager_update_goal(
    id: str,
    data: GoalUpdate,
    actor: dict = Depends(require_manager_or_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    g = await db.goals.find_one({"_id": oid})
    if not g:
        raise HTTPException(404, "Goal not found")

    employee = await db.users.find_one(
        {"_id": ObjectId(g["userId"])}
    ) if g.get("userId") else None
    if not employee or not can_decide_for_employee(actor, employee):
        raise HTTPException(
            403, "Not one of your direct reports",
        )

    update = data.model_dump(exclude_none=True)
    if data.status == "COMPLETED" and "completedAt" not in update:
        update["completedAt"] = datetime.now(timezone.utc)
    update["updatedAt"] = datetime.now(timezone.utc)
    await db.goals.update_one({"_id": oid}, {"$set": update})
    return {"message": "Goal updated"}


@goals_hr_router.get("")
async def hr_list_all_goals(
    userId: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    query: dict = {}
    if userId:
        query["userId"] = userId
    if status:
        query["status"] = status
    out = []
    async for g in db.goals.find(query).sort("createdAt", -1):
        out.append(_serialize_goal(g))
    return out


# ================= REVIEWS =================
def _serialize_review(r: dict) -> dict:
    return {
        "id": str(r["_id"]),
        "employeeId": r.get("employeeId"),
        "managerId": r.get("managerId"),
        "type": r.get("type"),
        "periodStart": r.get("periodStart"),
        "periodEnd": r.get("periodEnd"),
        "dimensions": r.get("dimensions", []),
        "status": r.get("status", "DRAFT"),
        "selfEval": r.get("selfEval"),
        "managerEval": r.get("managerEval"),
        "acknowledgedNote": r.get("acknowledgedNote"),
        "submittedAt": (
            r["submittedAt"].isoformat()
            if r.get("submittedAt") else None
        ),
        "acknowledgedAt": (
            r["acknowledgedAt"].isoformat()
            if r.get("acknowledgedAt") else None
        ),
        "createdAt": (
            r["createdAt"].isoformat()
            if r.get("createdAt") else None
        ),
    }


@reviews_mgr_router.post("")
async def manager_create_review(
    data: ReviewCreate,
    actor: dict = Depends(require_manager_or_hr),
):
    """Manager starts a review cycle for a direct report."""
    try:
        emp_oid = ObjectId(data.employeeId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid employeeId")
    employee = await db.users.find_one({"_id": emp_oid})
    if not employee:
        raise HTTPException(400, "Employee not found")
    if not can_decide_for_employee(actor, employee):
        raise HTTPException(
            403, "Not one of your direct reports",
        )

    now = datetime.now(timezone.utc)
    default_dims = data.dimensions or [
        "Quality of Work",
        "Ownership",
        "Collaboration",
        "Communication",
        "Growth",
    ]
    doc = {
        "employeeId": data.employeeId,
        "managerId": str(actor["_id"]),
        "type": data.type,
        "periodStart": data.periodStart,
        "periodEnd": data.periodEnd,
        "dimensions": default_dims,
        "status": "SELF_EVAL",
        "selfEval": None,
        "managerEval": None,
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.reviews.insert_one(doc)

    await notify_user(
        data.employeeId,
        "review_started",
        f"{data.type.replace('_', ' ').title()} review started",
        f"Please complete your self-evaluation for "
        f"{data.periodStart} → {data.periodEnd}.",
        {"reviewId": str(result.inserted_id)},
    )
    await log_audit(
        actor_id=str(actor["_id"]),
        action="review.create",
        entity_type="reviews",
        entity_id=str(result.inserted_id),
        after={"employeeId": data.employeeId, "type": data.type},
    )
    return {"id": str(result.inserted_id), "message": "Review created"}


@reviews_router.get("/mine")
async def my_reviews(
    user_id: str = Depends(get_current_user),
):
    out = []
    async for r in db.reviews.find(
        {"employeeId": user_id}
    ).sort("createdAt", -1):
        out.append(_serialize_review(r))
    return out


@reviews_router.post("/{id}/self-eval")
async def submit_self_eval(
    id: str,
    data: ReviewSelfEval,
    user_id: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    r = await db.reviews.find_one({"_id": oid})
    if not r or r.get("employeeId") != user_id:
        raise HTTPException(404, "Review not found")
    if r.get("status") not in ("SELF_EVAL", "MANAGER_EVAL"):
        raise HTTPException(
            400, f"Cannot submit self-eval in status {r.get('status')}",
        )

    now = datetime.now(timezone.utc)
    self_eval = data.model_dump(exclude_none=True)
    self_eval["submittedAt"] = now

    new_status = (
        "MANAGER_EVAL" if r.get("status") == "SELF_EVAL"
        else r.get("status")
    )
    await db.reviews.update_one(
        {"_id": oid},
        {"$set": {
            "selfEval": self_eval,
            "status": new_status,
            "updatedAt": now,
        }},
    )

    # Notify the manager that self-eval is in
    if r.get("managerId"):
        await notify_user(
            r["managerId"],
            "review_self_eval_submitted",
            "Self-evaluation submitted",
            "Your direct report has completed their self-evaluation.",
            {"reviewId": id},
        )
    return {"message": "Self-eval saved"}


@reviews_mgr_router.post("/{id}/manager-eval")
async def submit_manager_eval(
    id: str,
    data: ReviewManagerEval,
    actor: dict = Depends(require_manager_or_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    r = await db.reviews.find_one({"_id": oid})
    if not r:
        raise HTTPException(404, "Review not found")
    if (
        r.get("managerId") != str(actor["_id"])
        and actor.get("role") != "HR"
    ):
        raise HTTPException(403, "Not your review to fill")
    if r.get("status") not in ("MANAGER_EVAL", "SELF_EVAL"):
        raise HTTPException(
            400, f"Cannot fill manager-eval in status {r.get('status')}",
        )

    now = datetime.now(timezone.utc)
    manager_eval = data.model_dump(exclude_none=True)
    manager_eval["filledAt"] = now
    await db.reviews.update_one(
        {"_id": oid},
        {"$set": {
            "managerEval": manager_eval,
            "status": "MANAGER_EVAL",
            "updatedAt": now,
        }},
    )
    return {"message": "Manager-eval saved"}


@reviews_mgr_router.post("/{id}/submit")
async def submit_review(
    id: str,
    actor: dict = Depends(require_manager_or_hr),
):
    """Manager submits the finalized review; employee can then acknowledge."""
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    r = await db.reviews.find_one({"_id": oid})
    if not r:
        raise HTTPException(404, "Review not found")
    if (
        r.get("managerId") != str(actor["_id"])
        and actor.get("role") != "HR"
    ):
        raise HTTPException(403, "Not your review to submit")
    if not r.get("managerEval"):
        raise HTTPException(400, "Manager-eval is empty; fill it first")
    if r.get("status") == "ACKNOWLEDGED":
        raise HTTPException(400, "Already acknowledged")

    now = datetime.now(timezone.utc)
    await db.reviews.update_one(
        {"_id": oid},
        {"$set": {"status": "SUBMITTED", "submittedAt": now, "updatedAt": now}},
    )
    await notify_user(
        r["employeeId"],
        "review_submitted",
        "Performance review submitted",
        "Your manager has submitted your review. Please acknowledge it.",
        {"reviewId": id},
    )
    await log_audit(
        actor_id=str(actor["_id"]),
        action="review.submit",
        entity_type="reviews",
        entity_id=id,
    )
    return {"message": "Review submitted"}


@reviews_router.post("/{id}/acknowledge")
async def acknowledge_review(
    id: str,
    data: ReviewAcknowledge,
    user_id: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    r = await db.reviews.find_one({"_id": oid})
    if not r or r.get("employeeId") != user_id:
        raise HTTPException(404, "Review not found")
    if r.get("status") != "SUBMITTED":
        raise HTTPException(
            400, f"Cannot acknowledge from status {r.get('status')}",
        )

    now = datetime.now(timezone.utc)
    await db.reviews.update_one(
        {"_id": oid},
        {"$set": {
            "status": "ACKNOWLEDGED",
            "acknowledgedNote": data.note or "",
            "acknowledgedAt": now,
            "updatedAt": now,
        }},
    )
    return {"message": "Review acknowledged"}


@reviews_hr_router.get("")
async def hr_list_reviews(
    employeeId: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr),
):
    query: dict = {}
    if employeeId:
        query["employeeId"] = employeeId
    if status:
        query["status"] = status
    out = []
    async for r in db.reviews.find(query).sort("createdAt", -1):
        out.append(_serialize_review(r))
    return out


# ================= FEEDBACK (360°) =================
def _serialize_feedback(f: dict) -> dict:
    return {
        "id": str(f["_id"]),
        "toUserId": f.get("toUserId"),
        "fromUserId": (
            None if f.get("anonymous") else f.get("fromUserId")
        ),
        "type": f.get("type"),
        "text": f.get("text"),
        "anonymous": f.get("anonymous", False),
        "createdAt": (
            f["createdAt"].isoformat()
            if f.get("createdAt") else None
        ),
    }


@feedback_router.post("")
async def give_feedback(
    data: FeedbackCreate,
    user: dict = Depends(get_current_user_doc),
):
    try:
        to_oid = ObjectId(data.toUserId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid toUserId")
    target = await db.users.find_one({"_id": to_oid})
    if not target:
        raise HTTPException(400, "Recipient not found")

    user_id = str(user["_id"])
    if user_id == data.toUserId:
        raise HTTPException(400, "Cannot send feedback to yourself")

    text = (data.text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")

    now = datetime.now(timezone.utc)
    doc = {
        "toUserId": data.toUserId,
        "fromUserId": user_id,
        "type": data.type,
        "text": text,
        "anonymous": bool(data.anonymous),
        "createdAt": now,
    }
    result = await db.feedback.insert_one(doc)

    # In-app notification — only reveal sender if not anonymous.
    title = "New feedback"
    body = text if len(text) < 80 else text[:77] + "..."
    await notify_user(
        data.toUserId,
        "feedback_received",
        title,
        body,
        {"feedbackId": str(result.inserted_id), "type": data.type},
    )
    return {"id": str(result.inserted_id), "message": "Feedback recorded"}


@feedback_router.get("/about-me")
async def feedback_about_me(
    type: Optional[str] = Query(None),
    user_id: str = Depends(get_current_user),
):
    query: dict = {"toUserId": user_id}
    if type:
        query["type"] = type
    out = []
    async for f in db.feedback.find(query).sort("createdAt", -1):
        out.append(_serialize_feedback(f))
    return out


@feedback_router.get("/sent")
async def feedback_sent_by_me(
    user_id: str = Depends(get_current_user),
):
    """Returns feedback you've sent, including anonymous ones (so you can
    review what you've written)."""
    out = []
    async for f in db.feedback.find(
        {"fromUserId": user_id}
    ).sort("createdAt", -1):
        # Caller can see their own fromUserId regardless of anonymity flag.
        serialized = _serialize_feedback(f)
        serialized["fromUserId"] = user_id
        out.append(serialized)
    return out


@feedback_hr_router.get("")
async def hr_list_feedback(
    toUserId: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _hr: dict = Depends(require_hr),
):
    """HR audit view — sees fromUserId even on anonymous entries."""
    query: dict = {}
    if toUserId:
        query["toUserId"] = toUserId
    if type:
        query["type"] = type
    out = []
    async for f in db.feedback.find(query).sort(
        "createdAt", -1
    ).limit(limit):
        # Include fromUserId for HR even on anonymous entries.
        out.append({
            "id": str(f["_id"]),
            "toUserId": f.get("toUserId"),
            "fromUserId": f.get("fromUserId"),
            "type": f.get("type"),
            "text": f.get("text"),
            "anonymous": f.get("anonymous", False),
            "createdAt": (
                f["createdAt"].isoformat()
                if f.get("createdAt") else None
            ),
        })
    return out
