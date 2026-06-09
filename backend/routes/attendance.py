from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
)

from datetime import datetime, timezone

from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId

from database import db
from utils.notify import notify_user

from utils.dependencies import (
    get_current_user,
    require_hr,
    require_hr_or_ceo,
    require_manager_or_hr,
)

from utils.geo import haversine_meters

from utils.attendance_rules import (
    is_late,
    classify_on_checkout,
)

from config import (
    OFFICE_LATITUDE,
    OFFICE_LONGITUDE,
    OFFICE_RADIUS_METERS,
    is_geofence_configured,
)

from models.attendance import (
    AttendanceUpdate,
    AttendanceCheckIn,
    AttendanceCheckOut,
    AttendanceManualUpsert,
)

router = APIRouter()

# ================= VALID TYPES =================
VALID_TYPES = [
    "OFFICE",
    "WFH",
    "LEAVE",
    "HOLIDAY",
]


# ================= CHECK IN =================
@router.post("/checkin")
async def checkin(
    data: AttendanceCheckIn,
    user_id: str = Depends(
        get_current_user
    )
):

    today = data.date

    attendance_type = data.attendanceType

    if attendance_type not in VALID_TYPES:

        raise HTTPException(
            status_code=400,
            detail="Invalid attendance type"
        )

    # Server-side geofence for OFFICE check-ins.
    if attendance_type == "OFFICE":
        if (
            data.latitude is None
            or data.longitude is None
        ):
            raise HTTPException(
                400,
                "Location required for office check-in",
            )
        if is_geofence_configured():
            distance = haversine_meters(
                OFFICE_LATITUDE,
                OFFICE_LONGITUDE,
                data.latitude,
                data.longitude,
            )
            if distance > OFFICE_RADIUS_METERS:
                raise HTTPException(
                    400,
                    f"Too far from office "
                    f"({int(distance)}m, max "
                    f"{int(OFFICE_RADIUS_METERS)}m)",
                )

    existing = await db.attendance.find_one({

        "userId": user_id,
        "date": today
    })

    if existing:

        raise HTTPException(
            status_code=400,
            detail="Attendance already exists for this date"
        )

    # Prefer the device-supplied timestamp so the saved time matches
    # the user's local moment exactly. Falls back to server now() when
    # the field is absent (older clients).
    if data.checkIn:
        try:
            client_time = datetime.fromisoformat(
                data.checkIn.replace("Z", "+00:00")
            )
            if client_time.tzinfo is None:
                client_time = client_time.replace(tzinfo=timezone.utc)
            current_time = client_time.astimezone(timezone.utc)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid checkIn timestamp")
    else:
        current_time = datetime.now(timezone.utc)
    late_flag = is_late(current_time)

    attendance = {

        "userId": user_id,

        "date": today,

        "attendanceType":
        attendance_type,

        "status":
        "CHECKED_IN",

        "isLate":
        late_flag,

        "checkIn":
        current_time,

        "checkOut":
        None,

        "workNotes":
        "",

        "capturedLatitude":
        data.latitude,

        "capturedLongitude":
        data.longitude,

        "createdAt":
        current_time,

        "updatedAt":
        current_time,
    }

    await db.attendance.insert_one(
        attendance
    )

    return {
        "message":
        "Check in successful",
        "isLate": late_flag,
    }


# ================= CHECK OUT =================
@router.post("/checkout")
async def checkout(
    data: AttendanceCheckOut,
    user_id: str = Depends(
        get_current_user
    )
):

    today = data.date

    work_notes = (data.workNotes or "").strip()

    if len(work_notes) < 5:

        raise HTTPException(
            status_code=400,
            detail="Work notes must be at least 5 characters"
        )

    existing = await db.attendance.find_one({

        "userId": user_id,
        "date": today
    })

    if not existing:

        raise HTTPException(
            status_code=404,
            detail="Attendance not found"
        )

    # Honour the device-supplied checkOut timestamp; fall back to now().
    if data.checkOut:
        try:
            client_time = datetime.fromisoformat(
                data.checkOut.replace("Z", "+00:00")
            )
            if client_time.tzinfo is None:
                client_time = client_time.replace(tzinfo=timezone.utc)
            current_time = client_time.astimezone(timezone.utc)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid checkOut timestamp")
    else:
        current_time = datetime.now(timezone.utc)

    classification = classify_on_checkout(
        existing.get("checkIn"),
        current_time,
    )

    await db.attendance.update_one(

        {"_id": existing["_id"]},

        {
            "$set": {

                "checkOut":
                current_time,

                "workNotes":
                work_notes,

                "status":
                classification["status"],

                "hoursWorked":
                classification["hoursWorked"],

                "overtimeHours":
                classification["overtimeHours"],

                "isLate":
                classification["isLate"],

                "updatedAt":
                current_time,
            }
        }
    )

    return {
        "message": "Checked out successfully",
        "status": classification["status"],
        "hoursWorked": classification["hoursWorked"],
        "overtimeHours": classification["overtimeHours"],
    }


# ================= TODAY =================
@router.get("/today")
async def get_today(
    date: str | None = None,
    user_id: str = Depends(
        get_current_user
    )
):

    # Prefer the client-supplied date (YYYY-MM-DD) so it matches
    # whatever date /checkin used. Fall back to server local date.
    today = date or datetime.now().strftime(
        "%Y-%m-%d"
    )

    record = await db.attendance.find_one({

        "userId": user_id,
        "date": today
    })

    if not record:
        return {}

    return {

        "id":
        str(record["_id"]),

        "date":
        record["date"],

        "attendanceType":
        record.get(
            "attendanceType"
        ),

        "status":
        record.get(
            "status"
        ),

        "isLate":
        record.get("isLate", False),

        "hoursWorked":
        record.get("hoursWorked", 0.0),

        "overtimeHours":
        record.get("overtimeHours", 0.0),

        "checkIn":
        record.get("checkIn")
        .isoformat() + "Z"
        if record.get("checkIn")
        else None,

        "checkOut":
        record.get("checkOut")
        .isoformat() + "Z"
        if record.get("checkOut")
        else None,

        "workNotes":
        record.get(
            "workNotes",
            ""
        ),

        "autoClosedByCron":
        record.get(
            "autoClosedByCron",
            False,
        ),
    }


# ================= HISTORY =================
@router.get("/history")
async def get_history(
    before: Optional[str] = Query(None),  # YYYY-MM-DD
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(
        get_current_user
    )
):

    query: dict = {"userId": user_id}

    if before:
        query["date"] = {"$lt": before}

    records = []

    cursor = (
        db.attendance.find(query)
        .sort("date", -1)
        .limit(limit)
    )

    async for item in cursor:

        records.append({

            "id":
            str(item["_id"]),

            "date":
            item["date"],

            "attendanceType":
            item.get(
                "attendanceType"
            ),

            "status":
            item.get(
                "status"
            ),

            "isLate":
            item.get("isLate", False),

            "hoursWorked":
            item.get("hoursWorked", 0.0),

            "overtimeHours":
            item.get("overtimeHours", 0.0),

            # Always emit explicit-UTC ISO so the client doesn't
            # interpret a naive string as local time. Motor returns
            # tz-naive datetimes from BSON, so we append "Z" manually.
            "checkIn":
            item.get("checkIn")
            .isoformat() + "Z"
            if item.get("checkIn")
            else None,

            "checkOut":
            item.get("checkOut")
            .isoformat() + "Z"
            if item.get("checkOut")
            else None,

            "workNotes":
            item.get(
                "workNotes",
                ""
            ),

            "autoClosedByCron":
            item.get(
                "autoClosedByCron",
                False,
            ),
        })

    return records


# ================= DELETE (HR-only) =================
# Employees must raise a correction request via
# /attendance/correction-requests; direct mutation here is HR-only so
# audit history can't be silently rewritten.
@router.delete("/delete/{id}")
async def delete_attendance(
    id: str,
    _hr: dict = Depends(require_hr),
):

    try:

        object_id = ObjectId(id)

    except (InvalidId, TypeError):

        raise HTTPException(
            status_code=400,
            detail="Invalid id"
        )

    # Fetch first so we know whose record it is (for the notification).
    existing = await db.attendance.find_one({"_id": object_id})
    if not existing:

        raise HTTPException(
            status_code=404,
            detail="Attendance not found"
        )

    await db.attendance.delete_one({"_id": object_id})

    if existing.get("userId"):
        await notify_user(
            existing["userId"],
            "attendance_edited",
            "Attendance removed by HR",
            f"Your attendance record for "
            f"{existing.get('date', 'a day')} was removed by HR.",
            {"date": existing.get("date")},
        )

    return {
        "message":
        "Attendance deleted"
    }


# ================= UPDATE (HR-only) =================
# Same rationale as delete — only HR can rewrite an existing attendance
# row. Employees go through the correction-request workflow.
@router.put("/update/{id}")
async def update_attendance(
    id: str,
    data: AttendanceUpdate,
    _hr: dict = Depends(require_hr),
):

    try:

        object_id = ObjectId(id)

    except (InvalidId, TypeError):

        raise HTTPException(
            status_code=400,
            detail="Invalid id"
        )

    if data.attendanceType not in VALID_TYPES:

        raise HTTPException(
            status_code=400,
            detail="Invalid attendance type"
        )

    existing = await db.attendance.find_one({"_id": object_id})

    if not existing:

        raise HTTPException(
            status_code=404,
            detail="Attendance not found"
        )

    update_data = {

        "attendanceType":
        data.attendanceType,

        "workNotes":
        data.workNotes or "",

        "updatedAt":
        datetime.now(timezone.utc),
    }

    if data.checkIn:

        try:

            update_data["checkIn"] = \
                datetime.fromisoformat(
                    data.checkIn.replace("Z", "+00:00")
                )

        except (TypeError, ValueError):

            raise HTTPException(
                status_code=400,
                detail="Invalid checkIn format"
            )

    if data.checkOut:

        try:

            update_data["checkOut"] = \
                datetime.fromisoformat(
                    data.checkOut.replace("Z", "+00:00")
                )

        except (TypeError, ValueError):

            raise HTTPException(
                status_code=400,
                detail="Invalid checkOut format"
            )

    # Status reflects the merged state of new + existing values,
    # so editing only checkOut still flips status to COMPLETED.
    final_check_in = update_data.get(
        "checkIn",
        existing.get("checkIn"),
    )

    final_check_out = update_data.get(
        "checkOut",
        existing.get("checkOut"),
    )

    if final_check_in and final_check_out:

        update_data["status"] = \
            "COMPLETED"

    elif final_check_in:

        update_data["status"] = \
            "CHECKED_IN"

    await db.attendance.update_one(

        {"_id": object_id},

        {
            "$set":
            update_data
        }
    )

    # Let the employee know HR edited their attendance record.
    if existing.get("userId"):
        await notify_user(
            existing["userId"],
            "attendance_edited",
            "Attendance updated by HR",
            f"Your attendance for {existing.get('date', 'a day')} "
            f"was updated by HR.",
            {"attendanceId": id, "date": existing.get("date")},
        )

    return {
        "message":
        "Attendance updated"
    }


# ================= MANUAL UPSERT =================
@router.post("/manual")
async def manual_attendance(
    data: AttendanceManualUpsert,
    user: dict = Depends(
        require_manager_or_hr
    )
):
    """Atomic upsert by (userId, date). HR/MANAGER only — employees must
    use the /attendance/manual-request workflow. Manager can only act on
    direct reports; HR can act on anyone. The acting user_id is taken
    from `data.userId` (HR can target anyone) or falls back to the
    caller's own id (legacy callers).
    """

    actor_id = str(user["_id"])
    actor_role = user.get("role")

    # Target user — defaults to caller (preserves any internal callers
    # that don't set userId). HR can target anyone; MANAGER limited to
    # direct reports.
    target_user_id = (
        getattr(data, "userId", None) or actor_id
    )
    if target_user_id != actor_id:
        if actor_role == "MANAGER":
            target = await db.users.find_one(
                {"_id": ObjectId(target_user_id)}
                if ObjectId.is_valid(target_user_id) else {"_id": None}
            )
            if not target or target.get("reportingManagerId") != actor_id:
                raise HTTPException(
                    403,
                    "Manager can only add manual attendance for direct reports",
                )
        elif actor_role != "HR":
            raise HTTPException(403, "Not allowed")

    user_id = target_user_id

    if data.attendanceType not in VALID_TYPES:
        raise HTTPException(
            400,
            "Invalid attendance type",
        )

    parsed_in = None
    parsed_out = None

    if data.checkIn:
        try:
            parsed_in = datetime.fromisoformat(
                data.checkIn.replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            raise HTTPException(
                400,
                "Invalid checkIn format",
            )

    if data.checkOut:
        try:
            parsed_out = datetime.fromisoformat(
                data.checkOut.replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            raise HTTPException(
                400,
                "Invalid checkOut format",
            )

    if (
        parsed_in
        and parsed_out
        and parsed_out < parsed_in
    ):
        raise HTTPException(
            400,
            "checkOut cannot be before checkIn",
        )

    now = datetime.now(timezone.utc)

    existing = await db.attendance.find_one({
        "userId": user_id,
        "date": data.date,
    })

    final_in = parsed_in or (
        existing.get("checkIn") if existing else None
    )
    final_out = parsed_out or (
        existing.get("checkOut") if existing else None
    )

    if final_in and final_out:
        status = "COMPLETED"
    elif final_in:
        status = "CHECKED_IN"
    else:
        status = (
            existing.get("status") if existing else "CHECKED_IN"
        )

    set_fields = {
        "userId": user_id,
        "date": data.date,
        "attendanceType": data.attendanceType,
        "workNotes": data.workNotes or (
            existing.get("workNotes", "") if existing else ""
        ),
        "status": status,
        "updatedAt": now,
    }
    if parsed_in is not None:
        set_fields["checkIn"] = parsed_in
    if parsed_out is not None:
        set_fields["checkOut"] = parsed_out

    if existing:
        await db.attendance.update_one(
            {"_id": existing["_id"]},
            {"$set": set_fields},
        )
        record_id = existing["_id"]
    else:
        # New record — fill in the rest of the defaults.
        set_fields.setdefault("checkIn", parsed_in)
        set_fields.setdefault("checkOut", parsed_out)
        set_fields["createdAt"] = now
        result = await db.attendance.insert_one(set_fields)
        record_id = result.inserted_id

    saved = await db.attendance.find_one({"_id": record_id})

    return {
        "id": str(saved["_id"]),
        "date": saved.get("date"),
        "attendanceType": saved.get("attendanceType"),
        "status": saved.get("status"),
        "checkIn": (
            saved["checkIn"].isoformat() + "Z"
            if saved.get("checkIn") else None
        ),
        "checkOut": (
            saved["checkOut"].isoformat() + "Z"
            if saved.get("checkOut") else None
        ),
        "workNotes": saved.get("workNotes", ""),
        "autoClosedByCron": saved.get(
            "autoClosedByCron", False
        ),
    }


# ================= HR: ALL EMPLOYEES' ATTENDANCE =================
# Mounted under /hr (see main.py). Returns one row per (user, date) match,
# enriched with the user's name/email so the HR screen can render a list
# without a second round-trip.
hr_router = APIRouter()


@hr_router.get("/attendance")
async def hr_list_attendance(
    date: Optional[str] = Query(None),       # YYYY-MM-DD — single day
    month: Optional[str] = Query(None),      # YYYY-MM — whole month
    userId: Optional[str] = Query(None),     # filter to one employee
    _hr: dict = Depends(require_hr_or_ceo),
):
    """List attendance records across all employees with optional
    filters. `date` takes precedence over `month`. Defaults to today
    if neither is given — that's the common 'who's in today' view.
    """
    query: dict = {}

    if date:
        query["date"] = date
    elif month:
        # YYYY-MM → match the stored YYYY-MM-DD strings via prefix.
        if not (len(month) == 7 and month[4] == "-"):
            raise HTTPException(400, "Invalid month (YYYY-MM required)")
        query["date"] = {"$regex": f"^{month}-"}
    else:
        query["date"] = datetime.now().strftime("%Y-%m-%d")

    if userId:
        query["userId"] = userId

    records: list[dict] = []
    async for r in db.attendance.find(query).sort("date", -1):
        records.append(r)

    # Batch lookup of user details so the UI shows names without N+1 calls.
    unique_user_ids = {r.get("userId") for r in records if r.get("userId")}
    oids = []
    for uid in unique_user_ids:
        try:
            oids.append(ObjectId(uid))
        except (InvalidId, TypeError):
            continue

    user_map: dict[str, dict] = {}
    if oids:
        async for u in db.users.find({"_id": {"$in": oids}}):
            user_map[str(u["_id"])] = {
                "id": str(u["_id"]),
                "name": u.get("name"),
                "email": u.get("email"),
                "employeeCode": u.get("employeeCode"),
            }

    out: list[dict] = []
    for r in records:
        out.append({
            "id": str(r["_id"]),
            "userId": r.get("userId"),
            "user": user_map.get(r.get("userId")),
            "date": r.get("date"),
            "attendanceType": r.get("attendanceType"),
            "status": r.get("status"),
            "isLate": r.get("isLate", False),
            "hoursWorked": r.get("hoursWorked", 0.0),
            "overtimeHours": r.get("overtimeHours", 0.0),
            # Anchor as UTC so the client renders correctly in any tz.
            "checkIn": (
                r["checkIn"].isoformat() + "Z"
                if r.get("checkIn") else None
            ),
            "checkOut": (
                r["checkOut"].isoformat() + "Z"
                if r.get("checkOut") else None
            ),
            "workNotes": r.get("workNotes", ""),
        })

    return out