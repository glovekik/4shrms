from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from io import BytesIO
from calendar import month_name as _calendar_month_name
month_name = _calendar_month_name  # alias kept for clarity in callers

from bson import ObjectId
from bson.errors import InvalidId

from motor.motor_asyncio import AsyncIOMotorGridFSBucket

from datetime import datetime, timezone, date, timedelta

from typing import Optional

from database import db
from utils.dependencies import (
    get_current_user,
    require_hr,
    require_hr_or_ceo,
)
from utils.payroll_calc import (
    auto_pf,
    resolve_structure_amounts,
    compute_totals,
    compute_lop_deduction,
    days_in_month,
)
from utils.pdf import build_payslip_pdf
from utils.email import send_email_with_pdf
from utils.push import push_to_users
from utils.notify import create_notification
from config import (
    COMPANY_NAME,
    is_email_configured,
    RESTRICT_PAYROLL_FOR_INTERNS,
)
from models.payroll import (
    SalaryStructureCreate,
    PayrollRunCreate,
    PayslipOverride,
)

# GridFS bucket for cached payslip PDFs (lazy-init on first use).
_pdf_bucket: Optional[AsyncIOMotorGridFSBucket] = None


def _bucket() -> AsyncIOMotorGridFSBucket:
    global _pdf_bucket
    if _pdf_bucket is None:
        _pdf_bucket = AsyncIOMotorGridFSBucket(
            db,
            bucket_name="payslip_pdfs",
        )
    return _pdf_bucket

# /payroll/...    — user-facing payslip access
user_router = APIRouter()
# /hr/...         — HR full payroll control
hr_router = APIRouter()


# ================= SERIALIZERS =================
def _serialize_structure(s: dict) -> dict:
    return {
        "id": str(s["_id"]),
        "userId": s.get("userId"),
        "effectiveFrom": s.get("effectiveFrom"),
        "effectiveTo": s.get("effectiveTo"),
        # Earnings
        "basic": s.get("basic", 0),
        "hra": s.get("hra", 0),
        "communicationAllowance": s.get(
            "communicationAllowance", 0
        ),
        "otherAllowance": s.get("otherAllowance", 0),
        "employerPF": s.get("employerPF", 0),
        "employerInsurance": s.get("employerInsurance", 0),
        # Deductions
        "employeePF": s.get("employeePF", 0),
        "professionalTax": s.get("professionalTax", 0),
        "tds": s.get("tds", 0),
        "employeeInsurance": s.get("employeeInsurance", 0),
        # Identity
        "panNumber": s.get("panNumber"),
        "uanNumber": s.get("uanNumber"),
        "bankAccountNumber": s.get("bankAccountNumber"),
        "bankIfsc": s.get("bankIfsc"),
        "bankName": s.get("bankName"),
        "tdsRegime": s.get("tdsRegime", "NEW"),
        # Computed totals (from snapshot at save time)
        "totalGross": s.get("totalGross", 0),
        "totalDeductions": s.get("totalDeductions", 0),
        "netPay": s.get("netPay", 0),
        "createdAt": (
            s["createdAt"].isoformat()
            if s.get("createdAt") else None
        ),
    }


def _serialize_run(r: dict) -> dict:
    return {
        "id": str(r["_id"]),
        "year": r.get("year"),
        "month": r.get("month"),
        "workingDays": r.get("workingDays"),
        "status": r.get("status"),
        "payslipCount": r.get("payslipCount", 0),
        "createdBy": r.get("createdBy"),
        "createdAt": (
            r["createdAt"].isoformat()
            if r.get("createdAt") else None
        ),
        "processedAt": (
            r["processedAt"].isoformat()
            if r.get("processedAt") else None
        ),
        "lockedAt": (
            r["lockedAt"].isoformat()
            if r.get("lockedAt") else None
        ),
    }


def _serialize_payslip(
    p: dict,
    user_info: Optional[dict] = None,
) -> dict:
    return {
        "id": str(p["_id"]),
        "payrollRunId": p.get("payrollRunId"),
        "userId": p.get("userId"),
        "user": user_info,
        "year": p.get("year"),
        "month": p.get("month"),
        # Snapshot of structure used
        "basic": p.get("basic", 0),
        "hra": p.get("hra", 0),
        "communicationAllowance": p.get(
            "communicationAllowance", 0
        ),
        "otherAllowance": p.get("otherAllowance", 0),
        "employerPF": p.get("employerPF", 0),
        "employerInsurance": p.get("employerInsurance", 0),
        "employeePF": p.get("employeePF", 0),
        "professionalTax": p.get("professionalTax", 0),
        "tds": p.get("tds", 0),
        "employeeInsurance": p.get("employeeInsurance", 0),
        # Identity snapshot
        "panNumber": p.get("panNumber"),
        "uanNumber": p.get("uanNumber"),
        "bankAccountNumber": p.get("bankAccountNumber"),
        "bankIfsc": p.get("bankIfsc"),
        "bankName": p.get("bankName"),
        "tdsRegime": p.get("tdsRegime", "NEW"),
        # Attendance breakdown
        "workingDays": p.get("workingDays"),
        "attendedDays": p.get("attendedDays"),
        "lopDays": p.get("lopDays", 0),
        "lopDeduction": p.get("lopDeduction", 0),
        # Computed totals
        "totalGross": p.get("totalGross", 0),
        "totalDeductions": p.get("totalDeductions", 0),
        "netPay": p.get("netPay", 0),
        # Status
        "status": p.get("status", "GENERATED"),
        # Release state — whether HR has sent this to the employee yet.
        "sent": bool(p.get("sent", False)),
        "sentAt": (
            p["sentAt"].isoformat()
            if p.get("sentAt") else None
        ),
        "notes": p.get("notes", ""),
        "generatedAt": (
            p["generatedAt"].isoformat()
            if p.get("generatedAt") else None
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
            "employeeCode": u.get("employeeCode"),
        }
    return result


async def _block_if_intern_restricted(user_id: str) -> None:
    """Raises 403 when the caller is an Intern and the feature flag is on.

    Why a config flag instead of always-on: some companies pay interns and
    want them to see payslips; the flag lets HR opt in per deployment.
    """
    if not RESTRICT_PAYROLL_FOR_INTERNS:
        return
    try:
        u = await db.users.find_one(
            {"_id": ObjectId(user_id)},
            {"tag": 1},
        )
    except (InvalidId, TypeError):
        return
    if u and u.get("tag") == "Intern":
        raise HTTPException(
            403,
            "Payroll details are not available for your role.",
        )


def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def _yesterday_str() -> str:
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


# ================= HR: SALARY STRUCTURE =================
@hr_router.post("/users/{userId}/salary-structure")
async def set_salary_structure(
    userId: str,
    data: SalaryStructureCreate,
    hr: dict = Depends(require_hr),
):
    """Sets a NEW salary structure (closes the previous one for history)."""
    try:
        user_oid = ObjectId(userId)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid user id")

    user = await db.users.find_one({"_id": user_oid})
    if not user:
        raise HTTPException(404, "User not found")

    if data.basic <= 0:
        raise HTTPException(400, "Basic must be > 0")

    payload = data.model_dump()
    resolved = resolve_structure_amounts(payload)
    totals = compute_totals(resolved)

    now = datetime.now(timezone.utc)
    today = _today_str()
    yesterday = _yesterday_str()

    # Close any currently-active structure.
    await db.salary_structures.update_many(
        {"userId": userId, "effectiveTo": None},
        {
            "$set": {
                "effectiveTo": yesterday,
                "updatedAt": now,
            }
        },
    )

    doc = {
        "userId": userId,
        "effectiveFrom": today,
        "effectiveTo": None,
        **resolved,
        **totals,
        "createdBy": str(hr["_id"]),
        "createdAt": now,
        "updatedAt": now,
    }

    result = await db.salary_structures.insert_one(doc)
    doc["_id"] = result.inserted_id

    return _serialize_structure(doc)


@hr_router.get("/users/{userId}/salary-structure")
async def get_current_salary_structure(
    userId: str,
    _hr: dict = Depends(require_hr),
):
    s = await db.salary_structures.find_one({
        "userId": userId,
        "effectiveTo": None,
    })
    if not s:
        raise HTTPException(
            404,
            "No active salary structure for this user",
        )
    return _serialize_structure(s)


@hr_router.get("/users/{userId}/salary-history")
async def get_salary_history(
    userId: str,
    _hr: dict = Depends(require_hr),
):
    items = []
    async for s in db.salary_structures.find(
        {"userId": userId}
    ).sort("effectiveFrom", -1):
        items.append(_serialize_structure(s))
    return items


# ================= HR: PAYROLL RUNS =================
@hr_router.post("/payroll/runs")
async def create_payroll_run(
    data: PayrollRunCreate,
    hr: dict = Depends(require_hr),
):
    if not (1 <= data.month <= 12):
        raise HTTPException(400, "month must be 1..12")
    if data.workingDays <= 0:
        raise HTTPException(400, "workingDays must be > 0")

    existing = await db.payroll_runs.find_one({
        "year": data.year,
        "month": data.month,
    })
    if existing:
        raise HTTPException(
            400,
            f"Payroll run for {data.year}-{data.month:02d} already exists",
        )

    now = datetime.now(timezone.utc)
    doc = {
        "year": data.year,
        "month": data.month,
        "workingDays": data.workingDays,
        "status": "DRAFT",
        "payslipCount": 0,
        "createdBy": str(hr["_id"]),
        "createdAt": now,
        "updatedAt": now,
        "processedAt": None,
        "lockedAt": None,
    }
    result = await db.payroll_runs.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize_run(doc)


@hr_router.get("/payroll/runs")
async def list_payroll_runs(
    _hr: dict = Depends(require_hr_or_ceo),
):
    items = []
    async for r in db.payroll_runs.find().sort(
        [("year", -1), ("month", -1)]
    ):
        items.append(_serialize_run(r))
    return items


@hr_router.get("/payroll/runs/{id}")
async def get_payroll_run(
    id: str,
    _hr: dict = Depends(require_hr_or_ceo),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    r = await db.payroll_runs.find_one({"_id": oid})
    if not r:
        raise HTTPException(404, "Payroll run not found")
    return _serialize_run(r)


@hr_router.delete("/payroll/runs/{id}")
async def delete_payroll_run(
    id: str,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    r = await db.payroll_runs.find_one({"_id": oid})
    if not r:
        raise HTTPException(404, "Payroll run not found")
    if r.get("status") != "DRAFT":
        raise HTTPException(
            400,
            "Only DRAFT runs can be deleted",
        )
    await db.payslips.delete_many({"payrollRunId": id})
    await db.payroll_runs.delete_one({"_id": oid})
    return {"message": "Payroll run deleted"}


# ================= HR: PROCESS RUN =================
@hr_router.post("/payroll/runs/{id}/process")
async def process_payroll_run(
    id: str,
    _hr: dict = Depends(require_hr),
):
    """Generates payslips for every Active user with a salary structure.

    Re-processing a run wipes existing payslips for that run and recomputes —
    useful if structures or attendance changed mid-month. Disallowed once
    LOCKED.
    """
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    run = await db.payroll_runs.find_one({"_id": oid})
    if not run:
        raise HTTPException(404, "Payroll run not found")
    if run.get("status") == "LOCKED":
        raise HTTPException(
            400,
            "Payroll run is LOCKED — cannot reprocess",
        )

    year = run["year"]
    month = run["month"]
    working_days = int(run.get("workingDays", 22))

    # Date range for attendance lookup
    month_str = f"{year}-{month:02d}"
    last_day = days_in_month(year, month)
    from_d = f"{month_str}-01"
    to_d = f"{month_str}-{last_day:02d}"

    # Count holidays in this month — they reduce expected working days,
    # so a user who didn't show up on a holiday isn't penalised as LOP.
    holiday_count = await db.holidays.count_documents({
        "date": {"$gte": from_d, "$lte": to_d},
    })
    effective_working_days = max(
        0, working_days - holiday_count
    )

    # Get all Active users (or status missing → treat as Active)
    user_ids: list[str] = []
    async for u in db.users.find({
        "$or": [
            {"status": "Active"},
            {"status": {"$exists": False}},
        ]
    }):
        user_ids.append(str(u["_id"]))

    # Wipe existing payslips for this run (re-process)
    await db.payslips.delete_many({"payrollRunId": id})

    now = datetime.now(timezone.utc)
    generated = 0
    skipped: list[str] = []

    for user_id in user_ids:

        structure = await db.salary_structures.find_one({
            "userId": user_id,
            "effectiveTo": None,
        })
        if not structure:
            skipped.append(user_id)
            continue

        # Count attendance records this month — any present record
        # (any attendanceType) counts as a paid day.
        attended = await db.attendance.count_documents({
            "userId": user_id,
            "date": {"$gte": from_d, "$lte": to_d},
        })

        # LOP = days where user was *expected* to work but didn't show up.
        # Holidays reduce the expected count, so they're never LOP.
        lop_days = max(0, effective_working_days - attended)

        resolved = resolve_structure_amounts(structure)
        totals_full = compute_totals(resolved)
        gross_full = totals_full["totalGross"]

        lop_deduction = compute_lop_deduction(
            gross_full, working_days, lop_days
        )

        final_gross = round(gross_full - lop_deduction, 2)
        total_deductions = totals_full["totalDeductions"]
        net_pay = round(final_gross - total_deductions, 2)

        slip = {
            "payrollRunId": id,
            "userId": user_id,
            "year": year,
            "month": month,
            # Structure snapshot
            "basic": resolved.get("basic", 0),
            "hra": resolved.get("hra", 0),
            "communicationAllowance": resolved.get(
                "communicationAllowance", 0
            ),
            "otherAllowance": resolved.get("otherAllowance", 0),
            "employerPF": resolved.get("employerPF", 0),
            "employerInsurance": resolved.get(
                "employerInsurance", 0
            ),
            "employeePF": resolved.get("employeePF", 0),
            "professionalTax": resolved.get(
                "professionalTax", 0
            ),
            "tds": resolved.get("tds", 0),
            "employeeInsurance": resolved.get(
                "employeeInsurance", 0
            ),
            # Identity snapshot
            "panNumber": resolved.get("panNumber"),
            "uanNumber": resolved.get("uanNumber"),
            "bankAccountNumber": resolved.get(
                "bankAccountNumber"
            ),
            "bankIfsc": resolved.get("bankIfsc"),
            "bankName": resolved.get("bankName"),
            "tdsRegime": resolved.get("tdsRegime", "NEW"),
            # Attendance breakdown
            "workingDays": working_days,
            "attendedDays": attended,
            "lopDays": lop_days,
            "lopDeduction": lop_deduction,
            # Computed
            "totalGross": final_gross,
            "totalDeductions": total_deductions,
            "netPay": net_pay,
            "status": "GENERATED",
            # Payslips are NOT visible to the employee until HR explicitly
            # sends them. Processing only prepares them for HR to review.
            "sent": False,
            "sentAt": None,
            "notes": "",
            "generatedAt": now,
            "updatedAt": now,
        }

        await db.payslips.insert_one(slip)
        generated += 1

    await db.payroll_runs.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": "PROCESSED",
                "payslipCount": generated,
                "processedAt": now,
                "updatedAt": now,
            }
        },
    )

    # NOTE: employees are intentionally NOT notified here. Processing only
    # generates payslips for HR review; the employee notification + push
    # fire when HR explicitly sends the payslip(s) — see the /send and
    # /send-all endpoints below.

    return {
        "message": "Payroll processed",
        "generated": generated,
        "skipped": skipped,
        "holidayCount": holiday_count,
        "effectiveWorkingDays": effective_working_days,
    }


# ================= HR: LOCK RUN =================
@hr_router.post("/payroll/runs/{id}/lock")
async def lock_payroll_run(
    id: str,
    _hr: dict = Depends(require_hr),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    run = await db.payroll_runs.find_one({"_id": oid})
    if not run:
        raise HTTPException(404, "Payroll run not found")
    if run.get("status") != "PROCESSED":
        raise HTTPException(
            400,
            "Only PROCESSED runs can be locked",
        )
    now = datetime.now(timezone.utc)
    await db.payroll_runs.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": "LOCKED",
                "lockedAt": now,
                "updatedAt": now,
            }
        },
    )
    return {"message": "Payroll run locked"}


# ================= HR: PAYSLIPS =================
@hr_router.get("/payroll/runs/{id}/payslips")
async def list_payslips_in_run(
    id: str,
    _hr: dict = Depends(require_hr_or_ceo),
):
    raw = []
    async for p in db.payslips.find(
        {"payrollRunId": id}
    ).sort("netPay", -1):
        raw.append(p)
    user_map = await _user_basics(
        p.get("userId") for p in raw
    )
    return [
        _serialize_payslip(
            p,
            user_map.get(p.get("userId")),
        )
        for p in raw
    ]


@hr_router.get("/payslips/{id}")
async def hr_get_payslip(
    id: str,
    _hr: dict = Depends(require_hr_or_ceo),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    p = await db.payslips.find_one({"_id": oid})
    if not p:
        raise HTTPException(404, "Payslip not found")
    user_map = await _user_basics([p.get("userId")])
    return _serialize_payslip(
        p, user_map.get(p.get("userId"))
    )


@hr_router.put("/payslips/{id}")
async def override_payslip(
    id: str,
    data: PayslipOverride,
    _hr: dict = Depends(require_hr),
):
    """HR adjusts individual line items on a payslip.

    Recomputes totals and net pay after the override. Disallowed if the
    parent run is LOCKED.
    """
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    p = await db.payslips.find_one({"_id": oid})
    if not p:
        raise HTTPException(404, "Payslip not found")

    run = await db.payroll_runs.find_one({
        "_id": ObjectId(p["payrollRunId"]),
    })
    if run and run.get("status") == "LOCKED":
        raise HTTPException(
            400,
            "Parent payroll run is LOCKED",
        )

    update: dict = {"updatedAt": datetime.now(timezone.utc)}
    overridable = (
        "basic",
        "hra",
        "communicationAllowance",
        "otherAllowance",
        "employerPF",
        "employerInsurance",
        "employeePF",
        "professionalTax",
        "tds",
        "employeeInsurance",
        "lopDays",
        "workingDays",
        "attendedDays",
        "notes",
    )
    for field in overridable:
        v = getattr(data, field)
        if v is not None:
            update[field] = v

    # If HR rewrote attendedDays/workingDays but not lopDays, derive lopDays
    # from the new attendance so the LOP deduction below uses fresh numbers.
    if (
        ("workingDays" in update or "attendedDays" in update)
        and "lopDays" not in update
    ):
        new_wd = update.get("workingDays", p.get("workingDays") or 0)
        new_ad = update.get("attendedDays", p.get("attendedDays") or 0)
        update["lopDays"] = max(0, (new_wd or 0) - (new_ad or 0))

    # Build the post-override slip in memory to recompute totals.
    merged = {**p, **update}
    totals_full = compute_totals(merged)
    lop_deduction = compute_lop_deduction(
        totals_full["totalGross"],
        merged.get("workingDays") or 0,
        merged.get("lopDays") or 0,
    )
    final_gross = round(
        totals_full["totalGross"] - lop_deduction,
        2,
    )
    update["lopDeduction"] = lop_deduction
    update["totalGross"] = final_gross
    update["totalDeductions"] = totals_full["totalDeductions"]
    update["netPay"] = round(
        final_gross - totals_full["totalDeductions"], 2
    )
    update["status"] = "OVERRIDDEN"

    # Invalidate cached PDF — next download regenerates from new values.
    if p.get("pdfFileId"):
        try:
            await _bucket().delete(
                ObjectId(p["pdfFileId"])
            )
        except Exception:
            pass
        update["pdfFileId"] = None

    await db.payslips.update_one(
        {"_id": oid},
        {"$set": update},
    )

    return {"message": "Payslip updated"}


# ================= USER: MY PAYSLIPS =================
@user_router.get("/payslips")
async def my_payslips(
    user_id: str = Depends(get_current_user),
):
    """Caller's own payslip history. Only payslips HR has explicitly SENT
    are visible — unsent (processed-but-not-released) payslips stay hidden
    until HR hits Send."""
    await _block_if_intern_restricted(user_id)
    raw = []
    async for p in db.payslips.find(
        {"userId": user_id, "sent": True}
    ).sort([("year", -1), ("month", -1)]):
        raw.append(p)
    return [_serialize_payslip(p) for p in raw]


@user_router.get("/payslips/{id}")
async def my_payslip(
    id: str,
    user_id: str = Depends(get_current_user),
):
    await _block_if_intern_restricted(user_id)
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    p = await db.payslips.find_one({"_id": oid})
    if not p:
        raise HTTPException(404, "Payslip not found")
    if p.get("userId") != user_id:
        raise HTTPException(403, "Not your payslip")
    # Unsent payslips don't exist as far as the employee is concerned.
    if not p.get("sent"):
        raise HTTPException(404, "Payslip not found")
    return _serialize_payslip(p)


# ================= PDF GENERATION =================
async def _load_pdf_bytes(payslip: dict) -> bytes:
    """Returns PDF bytes — from GridFS cache if present, else generates
    and caches."""
    file_id = payslip.get("pdfFileId")

    if file_id:
        try:
            stream = await _bucket().open_download_stream(
                ObjectId(file_id)
            )
            data = await stream.read()
            return data
        except Exception:
            # Cache row pointed at a missing GridFS file — fall through
            # to regeneration.
            pass

    # Generate fresh.
    user_oid_str = payslip.get("userId")
    user = None
    try:
        user = await db.users.find_one({
            "_id": ObjectId(user_oid_str)
        })
    except (InvalidId, TypeError):
        pass

    user_dict = user or {}
    pdf_bytes = build_payslip_pdf(payslip, user_dict)

    # Cache it.
    filename = (
        f"payslip-{payslip.get('year')}-"
        f"{payslip.get('month'):02d}-"
        f"{user_oid_str}.pdf"
    )
    new_id = await _bucket().upload_from_stream(
        filename,
        pdf_bytes,
        metadata={
            "payslipId": str(payslip["_id"]),
            "userId": user_oid_str,
            "year": payslip.get("year"),
            "month": payslip.get("month"),
        },
    )
    await db.payslips.update_one(
        {"_id": payslip["_id"]},
        {"$set": {"pdfFileId": str(new_id)}},
    )

    return pdf_bytes


def _pdf_filename_for(payslip: dict, user: dict) -> str:
    safe_name = (user.get("name") or "employee").replace(
        " ", "_"
    )
    return (
        f"Payslip_{safe_name}_"
        f"{month_name[payslip.get('month', 1)]}_"
        f"{payslip.get('year', '')}.pdf"
    )


def _stream(pdf_bytes: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            )
        },
    )


# ================= USER: DOWNLOAD OWN PDF =================
@user_router.get("/payslips/{id}/pdf")
async def my_payslip_pdf(
    id: str,
    user_id: str = Depends(get_current_user),
):
    await _block_if_intern_restricted(user_id)
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    p = await db.payslips.find_one({"_id": oid})
    if not p:
        raise HTTPException(404, "Payslip not found")
    if p.get("userId") != user_id:
        raise HTTPException(403, "Not your payslip")
    if not p.get("sent"):
        raise HTTPException(404, "Payslip not found")

    pdf_bytes = await _load_pdf_bytes(p)
    user = await db.users.find_one({
        "_id": ObjectId(user_id)
    })
    return _stream(pdf_bytes, _pdf_filename_for(p, user or {}))


# ================= HR: DOWNLOAD ANY PDF =================
@hr_router.get("/payslips/{id}/pdf")
async def hr_payslip_pdf(
    id: str,
    _hr: dict = Depends(require_hr_or_ceo),
):
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    p = await db.payslips.find_one({"_id": oid})
    if not p:
        raise HTTPException(404, "Payslip not found")

    pdf_bytes = await _load_pdf_bytes(p)
    user = None
    try:
        user = await db.users.find_one({
            "_id": ObjectId(p.get("userId"))
        })
    except (InvalidId, TypeError):
        pass
    return _stream(pdf_bytes, _pdf_filename_for(p, user or {}))


# ================= HR: EMAIL ONE PAYSLIP =================
def _default_subject(payslip: dict) -> str:
    return (
        f"Payslip — {month_name[payslip.get('month', 1)]} "
        f"{payslip.get('year', '')}"
    )


def _default_body(user: dict, payslip: dict) -> str:
    name = user.get("name", "Team member")
    return (
        f"Hi {name},\n\n"
        f"Please find attached your payslip for "
        f"{month_name[payslip.get('month', 1)]} "
        f"{payslip.get('year', '')}.\n\n"
        f"Net pay: INR {payslip.get('netPay', 0):,.2f}\n\n"
        f"Regards,\n{COMPANY_NAME}"
    )


@hr_router.post("/payslips/{id}/email")
async def email_payslip(
    id: str,
    _hr: dict = Depends(require_hr),
):
    if not is_email_configured():
        raise HTTPException(
            503,
            "Email is not configured (SMTP_HOST and SMTP_FROM env vars required)",
        )

    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    p = await db.payslips.find_one({"_id": oid})
    if not p:
        raise HTTPException(404, "Payslip not found")

    user = None
    try:
        user = await db.users.find_one({
            "_id": ObjectId(p.get("userId"))
        })
    except (InvalidId, TypeError):
        pass

    if not user or not user.get("email"):
        raise HTTPException(
            400,
            "Recipient user has no email address",
        )

    pdf_bytes = await _load_pdf_bytes(p)

    try:
        await send_email_with_pdf(
            to_email=user["email"],
            subject=_default_subject(p),
            body=_default_body(user, p),
            pdf_bytes=pdf_bytes,
            pdf_filename=_pdf_filename_for(p, user),
        )
    except Exception as e:
        raise HTTPException(
            502,
            f"Email send failed: {e}",
        )

    now = datetime.now(timezone.utc)
    await db.payslips.update_one(
        {"_id": oid},
        {
            "$set": {
                "lastEmailedAt": now,
                "lastEmailedTo": user["email"],
            }
        },
    )

    return {
        "message": "Payslip emailed",
        "to": user["email"],
    }


# ================= HR: BULK EMAIL =================
@hr_router.post("/payroll/runs/{id}/email-all")
async def email_all_payslips(
    id: str,
    _hr: dict = Depends(require_hr),
):
    """Sequentially emails every payslip in a run. For demo scale only —
    a few hundred employees max. For larger volumes, move to a background
    queue."""
    if not is_email_configured():
        raise HTTPException(
            503,
            "Email is not configured",
        )

    try:
        run_oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    run = await db.payroll_runs.find_one({"_id": run_oid})
    if not run:
        raise HTTPException(404, "Payroll run not found")

    sent: list[str] = []
    failed: list[dict] = []
    skipped: list[str] = []

    async for p in db.payslips.find({"payrollRunId": id}):

        try:
            user_oid = ObjectId(p.get("userId"))
        except (InvalidId, TypeError):
            skipped.append(str(p["_id"]))
            continue

        user = await db.users.find_one({"_id": user_oid})
        if not user or not user.get("email"):
            skipped.append(str(p["_id"]))
            continue

        try:
            pdf_bytes = await _load_pdf_bytes(p)
            await send_email_with_pdf(
                to_email=user["email"],
                subject=_default_subject(p),
                body=_default_body(user, p),
                pdf_bytes=pdf_bytes,
                pdf_filename=_pdf_filename_for(p, user),
            )
            now = datetime.now(timezone.utc)
            await db.payslips.update_one(
                {"_id": p["_id"]},
                {
                    "$set": {
                        "lastEmailedAt": now,
                        "lastEmailedTo": user["email"],
                    }
                },
            )
            sent.append(user["email"])
        except Exception as e:
            failed.append({
                "userId": p.get("userId"),
                "email": user.get("email"),
                "error": str(e),
            })

    return {
        "message": "Bulk email complete",
        "sentCount": len(sent),
        "failedCount": len(failed),
        "skippedCount": len(skipped),
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
    }


# ================= HR: SEND (RELEASE) PAYSLIPS =================
async def _notify_payslip_sent(payslip: dict) -> None:
    """Push + in-app bell telling the employee their payslip is available."""
    uid = payslip.get("userId")
    if not uid:
        return
    month = payslip.get("month", 1)
    year = payslip.get("year", "")
    title = "Payslip ready"
    body = f"{month_name[month]} {year} payslip is ready"
    try:
        await push_to_users(
            [uid], title, body,
            {"type": "payslip_ready", "payslipId": str(payslip["_id"])},
        )
    except Exception:
        pass
    try:
        await create_notification(
            uid, "payslip_ready", title, body,
            {"payslipId": str(payslip["_id"])},
        )
    except Exception:
        pass


@hr_router.post("/payslips/{id}/send")
async def send_payslip(
    id: str,
    _hr: dict = Depends(require_hr),
):
    """Release a single payslip to the employee: make it visible in My
    Payslips and notify them. Idempotent — re-sending just re-notifies."""
    try:
        oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")

    p = await db.payslips.find_one({"_id": oid})
    if not p:
        raise HTTPException(404, "Payslip not found")

    now = datetime.now(timezone.utc)
    await db.payslips.update_one(
        {"_id": oid},
        {"$set": {"sent": True, "sentAt": now}},
    )
    await _notify_payslip_sent(p)

    return {"message": "Payslip sent", "payslipId": id}


@hr_router.post("/payroll/runs/{id}/send-all")
async def send_all_payslips(
    id: str,
    _hr: dict = Depends(require_hr),
):
    """Release every payslip in a run to its employee + notify each."""
    try:
        run_oid = ObjectId(id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid id")
    run = await db.payroll_runs.find_one({"_id": run_oid})
    if not run:
        raise HTTPException(404, "Payroll run not found")

    now = datetime.now(timezone.utc)
    sent_count = 0
    async for p in db.payslips.find({"payrollRunId": id}):
        await db.payslips.update_one(
            {"_id": p["_id"]},
            {"$set": {"sent": True, "sentAt": now}},
        )
        await _notify_payslip_sent(p)
        sent_count += 1

    return {"message": "Payslips sent", "sentCount": sent_count}
