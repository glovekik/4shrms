from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
)

from datetime import datetime, timezone, timedelta

from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId

from database import db
from utils.ist import now_ist_naive, parse_client_to_ist_naive, iso_naive
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
from models.client_visit import ClientVisitCreate, ClientVisitCheckout

router = APIRouter()

# ================= VALID TYPES =================
VALID_TYPES = [
    "OFFICE",
    "WFH",
    "CLIENT",
    "LEAVE",
    "HOLIDAY",
]

# How many days back a still-open check-in still gates today's check-in.
# Keeps the "finish your previous day" rule scoped to recent misses (incl. a
# weekend gap) so a months-old forgotten check-out never locks someone out.
PREV_DAY_BLOCK_WINDOW_DAYS = 7


async def _unfinished_prev_checkins(
    user_id: str, ref_date: str
) -> list[dict]:
    """Previous days the user forgot to check out of and hasn't corrected.

    A forgotten check-out is auto-closed by the 00:01 cron with a placeholder
    23:59:59 check-out and ``autoClosedByCron=True``. Returns the caller's rows
    from a date earlier than ``ref_date`` (within PREV_DAY_BLOCK_WINDOW_DAYS)
    that are either auto-closed OR (edge case: before the cron ran) still
    CHECKED_IN, EXCLUDING any that already have a correction request in ANY
    status — once the user has sent a correction, the day no longer nags or
    blocks them. Sorted oldest-first.
    """
    try:
        window_start = (
            datetime.strptime(ref_date, "%Y-%m-%d")
            - timedelta(days=PREV_DAY_BLOCK_WINDOW_DAYS)
        ).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        window_start = None

    date_filter: dict = {"$lt": ref_date}
    if window_start:
        date_filter["$gte"] = window_start

    records: list[dict] = []
    async for r in db.attendance.find(
        {
            "userId": user_id,
            "date": date_filter,
            "$or": [
                {"status": "CHECKED_IN"},
                {"autoClosedByCron": True},
            ],
        },
        {"date": 1, "checkIn": 1, "checkOut": 1, "attendanceType": 1},
    ).sort("date", 1):
        records.append(r)

    if not records:
        return []

    # Drop any row the user has already filed a correction for (any status).
    att_ids = [str(r["_id"]) for r in records]
    handled: set[str] = set()
    async for cr in db.correction_requests.find(
        {"attendanceId": {"$in": att_ids}},
        {"attendanceId": 1},
    ):
        if cr.get("attendanceId"):
            handled.add(cr["attendanceId"])

    return [r for r in records if str(r["_id"]) not in handled]


def _serialize_unfinished(records: list[dict]) -> list[dict]:
    return [
        {
            "id": str(r["_id"]),
            "date": r.get("date"),
            "checkIn": iso_naive(r.get("checkIn")),
            # Placeholder 23:59:59 from auto-close; the app pre-fills it so the
            # user adjusts to their real leave time.
            "checkOut": iso_naive(r.get("checkOut")),
            "attendanceType": r.get("attendanceType"),
        }
        for r in records
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

    # ---- Unfinished previous day gate ----
    # A user who forgot to check out on an earlier day has that record
    # auto-closed at 11:59 PM with a placeholder time (autoClosedByCron). Since
    # that time is almost certainly wrong, they cannot check in today until
    # they file an attendance correction request for the forgotten day
    # (approval can follow later). Filing the request clears this gate (see
    # _unfinished_prev_checkins). Scoped to recent misses so an old forgotten
    # day can't lock someone out forever.
    unfinished = await _unfinished_prev_checkins(user_id, today)
    if unfinished:
        dates = [r["date"] for r in unfinished if r.get("date")]
        shown = ", ".join(dates[:5])
        more = f" and {len(dates) - 5} more" if len(dates) > 5 else ""
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PREV_DAY_INCOMPLETE",
                "message": (
                    f"You didn't check out on {shown}{more}. "
                    "Send a correction request for that day to check in today."
                ),
                "dates": dates,
                "records": _serialize_unfinished(unfinished),
            },
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

    # CLIENT check-ins capture the client site location; reject if the person
    # is actually at the office (they should use OFFICE instead).
    if attendance_type == "CLIENT":
        if data.latitude is None or data.longitude is None:
            raise HTTPException(400, "Location required for client check-in")
        guard_lat = OFFICE_LATITUDE if is_geofence_configured() else CLIENT_GUARD_OFFICE_LAT
        guard_lng = OFFICE_LONGITUDE if is_geofence_configured() else CLIENT_GUARD_OFFICE_LNG
        guard_radius = OFFICE_RADIUS_METERS or 200.0
        if haversine_meters(guard_lat, guard_lng, data.latitude, data.longitude) <= guard_radius:
            raise HTTPException(
                400,
                "You're at the office — use Office instead of Client.",
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

    # Store the check-in as IST wall-clock (see utils/ist.py) so the raw DB
    # reads in office time. Prefer the device-supplied instant; fall back to
    # server now() for older clients.
    if data.checkIn:
        try:
            current_time = parse_client_to_ist_naive(data.checkIn)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid checkIn timestamp")
    else:
        current_time = now_ist_naive()
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

        "clientAddress":
        (data.clientAddress or "").strip()
        if attendance_type == "CLIENT" else None,

        "clientName":
        (data.clientName or "").strip()
        if attendance_type == "CLIENT" else None,

        "createdAt":
        current_time,

        "updatedAt":
        current_time,
    }

    await db.attendance.insert_one(
        attendance
    )

    # For client check-ins, tell the reporting manager + HR where the person
    # is working from (mirrors the old client-location notification).
    if attendance_type == "CLIENT":
        try:
            actor = None
            try:
                actor = await db.users.find_one({"_id": ObjectId(user_id)})
            except (InvalidId, TypeError):
                actor = None
            name = (actor or {}).get("name") or "A teammate"
            where = (
                (data.clientAddress or "").strip()
                or f"{data.latitude:.5f}, {data.longitude:.5f}"
            )
            recipients: set[str] = set()
            async for hr in db.users.find({"role": "HR"}, {"_id": 1}):
                recipients.add(str(hr["_id"]))
            if actor and actor.get("reportingManagerId"):
                recipients.add(str(actor["reportingManagerId"]))
            recipients.discard(user_id)
            for rid in recipients:
                try:
                    await notify_user(
                        rid,
                        "client_location",
                        "Client check-in",
                        f"{name} checked in from a client location — {where}.",
                        {"type": "client_location", "userId": user_id, "date": today},
                    )
                except Exception:
                    pass
        except Exception:
            pass

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

    # Only an open, checked-in day can be checked out. Without these guards a
    # checkout POST could overwrite a leave / absent / holiday row (which has
    # no checkIn) — classify_on_checkout(None, now) would flip it to HALF_DAY
    # with 0 hours and destroy the record — or double-check-out a finished day.
    if not existing.get("checkIn"):
        raise HTTPException(
            status_code=400,
            detail="You haven't checked in for this day.",
        )
    if existing.get("status") in ("ON_LEAVE", "ABSENT", "HOLIDAY"):
        raise HTTPException(
            status_code=400,
            detail="This day is marked as leave or holiday and can't be checked out.",
        )
    if existing.get("checkOut"):
        raise HTTPException(
            status_code=400,
            detail="You've already checked out for this day.",
        )

    # Honour the device-supplied checkOut timestamp (stored as IST wall-clock);
    # fall back to now().
    if data.checkOut:
        try:
            current_time = parse_client_to_ist_naive(data.checkOut)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid checkOut timestamp")
    else:
        current_time = now_ist_naive()

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


# ================= UNFINISHED PREVIOUS DAYS =================
@router.get("/unfinished")
async def get_unfinished(
    date: str | None = None,
    user_id: str = Depends(get_current_user),
):
    """Previous days the caller forgot to check out of and hasn't yet filed a
    correction request for. The app polls this on open and prompts the user to
    send a correction request for each one. `date` is the client's today
    (YYYY-MM-DD); falls back to server IST date."""
    today = date or now_ist_naive().strftime("%Y-%m-%d")
    unfinished = await _unfinished_prev_checkins(user_id, today)
    return {"records": _serialize_unfinished(unfinished)}


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
    today = date or now_ist_naive().strftime(
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

        "clientAddress":
        record.get("clientAddress"),

        "clientName":
        record.get("clientName"),

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

        "checkIn": iso_naive(record.get("checkIn")),

        "checkOut": iso_naive(record.get("checkOut")),

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

            # Stored as IST wall-clock (see utils/ist.py) — emit as-is, no Z.
            "checkIn": iso_naive(item.get("checkIn")),

            "checkOut": iso_naive(item.get("checkOut")),

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
        now_ist_naive(),
    }

    if data.checkIn:

        try:

            update_data["checkIn"] = parse_client_to_ist_naive(data.checkIn)

        except (TypeError, ValueError):

            raise HTTPException(
                status_code=400,
                detail="Invalid checkIn format"
            )

    if data.checkOut:

        try:

            update_data["checkOut"] = parse_client_to_ist_naive(data.checkOut)

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
            parsed_in = parse_client_to_ist_naive(data.checkIn)
        except (TypeError, ValueError):
            raise HTTPException(
                400,
                "Invalid checkIn format",
            )

    if data.checkOut:
        try:
            parsed_out = parse_client_to_ist_naive(data.checkOut)
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

    now = now_ist_naive()

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

    final_notes = data.workNotes or (
        existing.get("workNotes", "") if existing else ""
    )

    set_fields = {
        "userId": user_id,
        "date": data.date,
        "attendanceType": data.attendanceType,
        "workNotes": final_notes,
        "status": status,
        "updatedAt": now,
    }

    # HR/a manager can supply the times but usually can't say what the person
    # actually did. Flag such a day so the employee's weekly timesheet can
    # single it out and demand the work notes — a day with hours and no notes
    # is exactly what the timesheet flow exists to close.
    filled_for_someone_else = target_user_id != actor_id
    set_fields["notesPendingFromEmployee"] = bool(
        filled_for_someone_else
        and not final_notes.strip()
        and data.attendanceType not in ("LEAVE", "HOLIDAY")
    )
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
        "checkIn": iso_naive(saved.get("checkIn")),
        "checkOut": iso_naive(saved.get("checkOut")),
        "workNotes": saved.get("workNotes", ""),
        "autoClosedByCron": saved.get(
            "autoClosedByCron", False
        ),
    }


# ================= CLIENT LOCATION VISITS =================
# An employee working from a client site logs their GPS location (+ optional
# reverse-geocoded address and a note). Stored in `client_visits`, separate
# from the attendance row, and surfaced to their manager + HR.
#
# Fallback office coordinates for the "you're at the office" guard, used only
# when the env geofence (OFFICE_LATITUDE/LONGITUDE) isn't configured. Kept in
# sync with the app's src/utils/location.ts OFFICE constant.
CLIENT_GUARD_OFFICE_LAT = 16.507020515758303
CLIENT_GUARD_OFFICE_LNG = 80.62279856266548


def _serialize_client_visit(v: dict, user: Optional[dict] = None) -> dict:
    return {
        "id": str(v["_id"]),
        "userId": v.get("userId"),
        "user": user,
        "date": v.get("date"),
        "latitude": v.get("latitude"),
        "longitude": v.get("longitude"),
        "address": v.get("address") or "",
        "notes": v.get("notes") or "",
        # capturedAt is the client check-in instant; checkOut closes the visit.
        "capturedAt": iso_naive(v.get("capturedAt")),
        "checkOut": iso_naive(v.get("checkOut")),
        "hoursWorked": v.get("hoursWorked", 0.0),
    }


@router.post("/client-location")
async def submit_client_location(
    data: ClientVisitCreate,
    user_id: str = Depends(get_current_user),
):
    """Employee logs that they're working from a client location today."""
    # A "client location" that's actually the office is a normal check-in —
    # reject it so client visits stay limited to genuine off-site work.
    # Prefer the configured env office; when the geofence env isn't set, fall
    # back to the app's known office coordinates so the limit still applies.
    if is_geofence_configured():
        office_lat = OFFICE_LATITUDE
        office_lng = OFFICE_LONGITUDE
    else:
        office_lat = CLIENT_GUARD_OFFICE_LAT
        office_lng = CLIENT_GUARD_OFFICE_LNG
    office_radius = OFFICE_RADIUS_METERS or 200.0
    office_dist = haversine_meters(
        office_lat, office_lng, data.latitude, data.longitude
    )
    if office_dist <= office_radius:
        raise HTTPException(
            400,
            "You're at the office — check in normally instead of "
            "logging a client location.",
        )

    if data.capturedAt:
        try:
            when = parse_client_to_ist_naive(data.capturedAt)
        except (TypeError, ValueError):
            when = now_ist_naive()
    else:
        when = now_ist_naive()

    doc = {
        "userId": user_id,
        "date": data.date,
        "latitude": data.latitude,
        "longitude": data.longitude,
        "address": (data.address or "").strip(),
        "notes": (data.notes or "").strip(),
        "capturedAt": when,
        "createdAt": when,
        "updatedAt": when,
    }
    result = await db.client_visits.insert_one(doc)

    # Notify the reporting manager + all HR so it shows up for them.
    try:
        actor = None
        try:
            actor = await db.users.find_one({"_id": ObjectId(user_id)})
        except (InvalidId, TypeError):
            actor = None
        name = (actor or {}).get("name") or "A teammate"
        where = doc["address"] or f"{data.latitude:.5f}, {data.longitude:.5f}"

        recipients: set[str] = set()
        async for hr in db.users.find({"role": "HR"}, {"_id": 1}):
            recipients.add(str(hr["_id"]))
        if actor and actor.get("reportingManagerId"):
            recipients.add(str(actor["reportingManagerId"]))
        recipients.discard(user_id)

        for rid in recipients:
            try:
                await notify_user(
                    rid,
                    "client_location",
                    "Client-location check-in",
                    f"{name} is working from a client location — {where}.",
                    {
                        "type": "client_location",
                        "userId": user_id,
                        "date": data.date,
                    },
                )
            except Exception:
                pass
    except Exception:
        pass

    return {"message": "Client location saved", "id": str(result.inserted_id)}


@router.get("/client-location/mine")
async def my_client_locations(
    limit: int = Query(30, ge=1, le=200),
    user_id: str = Depends(get_current_user),
):
    """The caller's own recent client-location visits (newest first)."""
    out: list[dict] = []
    cursor = (
        db.client_visits.find({"userId": user_id})
        .sort("capturedAt", -1)
        .limit(limit)
    )
    async for v in cursor:
        out.append(_serialize_client_visit(v))
    return out


@router.post("/client-location/checkout")
async def checkout_client_location(
    data: ClientVisitCheckout,
    user_id: str = Depends(get_current_user),
):
    """Close the caller's open client-location visit for today: stamps the
    check-out time, computes hours worked, and optionally saves work notes."""
    today = now_ist_naive().strftime("%Y-%m-%d")
    visit = await db.client_visits.find_one(
        {
            "userId": user_id,
            "date": today,
            "$or": [{"checkOut": None}, {"checkOut": {"$exists": False}}],
        },
        sort=[("capturedAt", -1)],
    )
    if not visit:
        raise HTTPException(404, "No open client-location visit to check out")

    if data.checkOut:
        try:
            out_at = parse_client_to_ist_naive(data.checkOut)
        except (TypeError, ValueError):
            out_at = now_ist_naive()
    else:
        out_at = now_ist_naive()

    started = visit.get("capturedAt")
    hours = 0.0
    if isinstance(started, datetime):
        hours = max(0.0, (out_at - started).total_seconds() / 3600.0)
        hours = round(hours, 2)

    set_fields: dict = {
        "checkOut": out_at,
        "hoursWorked": hours,
        "updatedAt": out_at,
    }
    if data.notes is not None and data.notes.strip():
        set_fields["notes"] = data.notes.strip()

    await db.client_visits.update_one(
        {"_id": visit["_id"]}, {"$set": set_fields}
    )
    visit.update(set_fields)
    return _serialize_client_visit(visit)


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
        query["date"] = now_ist_naive().strftime("%Y-%m-%d")

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
                "profilePictureUrl": u.get("profilePictureUrl"),
            }

    out: list[dict] = []
    for r in records:
        out.append({
            "id": str(r["_id"]),
            "userId": r.get("userId"),
            "user": user_map.get(r.get("userId")),
            "date": r.get("date"),
            "attendanceType": r.get("attendanceType"),
            "clientAddress": r.get("clientAddress"),
            "clientName": r.get("clientName"),
            "status": r.get("status"),
            "isLate": r.get("isLate", False),
            "hoursWorked": r.get("hoursWorked", 0.0),
            "overtimeHours": r.get("overtimeHours", 0.0),
            # Stored as IST wall-clock (see utils/ist.py) — emit as-is, no Z.
            "checkIn": iso_naive(r.get("checkIn")),
            "checkOut": iso_naive(r.get("checkOut")),
            "workNotes": r.get("workNotes", ""),
            "unpaid": bool(r.get("unpaid", False)),
        })

    return out


@hr_router.post("/attendance/unpaid-leave")
async def hr_mark_unpaid_leave(
    body: dict,
    hr: dict = Depends(require_hr_or_ceo),
):
    """HR marks (or clears) a day as UNPAID leave for one employee.

    Unpaid leave is a LEAVE-type day that is NOT a paid/worked day, so payroll
    excludes it from `attended` and it becomes Loss-of-Pay. Paid leave, by
    contrast, is what an employee requests against their leave balance.

    Body: { userId: str, date: "YYYY-MM-DD", unpaid: bool }
      unpaid=true  → upsert an unpaid LEAVE row for that day
      unpaid=false → remove the unpaid-leave marker (deletes the row)
    """
    user_id = (body.get("userId") or "").strip()
    date_str = (body.get("date") or "").strip()
    make_unpaid = bool(body.get("unpaid", True))

    if not user_id or not date_str:
        raise HTTPException(400, "userId and date are required")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    try:
        ObjectId(user_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid userId")

    now = now_ist_naive()

    if not make_unpaid:
        # Undo: only remove a row that IS an unpaid-leave marker, so we never
        # delete a genuine attendance record.
        await db.attendance.delete_one(
            {"userId": user_id, "date": date_str, "unpaid": True}
        )
        return {"message": "Unpaid leave removed", "unpaid": False}

    # Mark as unpaid leave — overwrite/create the day's row.
    await db.attendance.update_one(
        {"userId": user_id, "date": date_str},
        {
            "$set": {
                "userId": user_id,
                "date": date_str,
                "attendanceType": "LEAVE",
                "status": "ON_LEAVE",
                "unpaid": True,
                "checkIn": None,
                "checkOut": None,
                "hoursWorked": 0.0,
                "isLate": False,
                "workNotes": "Unpaid leave (marked by HR)",
                "markedUnpaidBy": str(hr["_id"]),
                "autoClosedByCron": False,
                "updatedAt": now,
            },
            "$setOnInsert": {"createdAt": now},
        },
        upsert=True,
    )
    return {"message": "Marked as unpaid leave", "unpaid": True}