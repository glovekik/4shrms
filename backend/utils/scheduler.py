import os
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from database import db
from utils.attendance_rules import is_weekend
from utils.ist import now_ist_naive


def _scheduler_timezone():
    """Timezone the cron jobs fire in. Defaults to Asia/Kolkata so the
    10:00 check-in / 18:00 check-out nudges land at the right IST wall-clock
    time even inside a Docker container (which otherwise runs in UTC).
    Overridable via SCHEDULER_TZ / TZ.

    CRITICAL: never return None — an unset scheduler timezone makes APScheduler
    fall back to the container clock (UTC), which fires the 10:00 job at
    10:00 UTC = 15:30 IST. If tzdata can't resolve the named zone, fall back to
    a fixed IST offset (India has no DST, so a static +05:30 is exact)."""
    name = os.getenv("SCHEDULER_TZ") or os.getenv("TZ") or "Asia/Kolkata"
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=5, minutes=30))


# In-process scheduler. Single uvicorn worker assumed for demo.
# For multi-worker prod, move jobs to an external scheduler.
_TZ = _scheduler_timezone()
scheduler = AsyncIOScheduler(timezone=_TZ)


# ================= AUTO-CLOSE ATTENDANCE =================
async def auto_close_attendance() -> None:
    """Runs daily at 00:01 server local time.

    Closes any attendance still in CHECKED_IN state from a date earlier than
    today: sets checkOut to 23:59:59 of that record's date, status=COMPLETED,
    and flags `autoClosedByCron=True` so the user can request a correction.

    Each auto-close spends one of the user's monthly auto-checkout credits
    (default 5) and notifies the user + their reporting manager + HR.
    """
    from bson import ObjectId
    from bson.errors import InvalidId
    from utils.ist import now_ist_naive
    from utils.notify import notify_user

    today = now_ist_naive().strftime("%Y-%m-%d")

    cursor = db.attendance.find({
        "status": "CHECKED_IN",
        "date": {"$lt": today},
    })

    # Resolve HR recipients once (few HR users).
    hr_ids: list[str] = []
    async for hr in db.users.find({"role": "HR"}, {"_id": 1}):
        hr_ids.append(str(hr["_id"]))

    closed = 0

    async for record in cursor:

        try:
            record_date = datetime.strptime(
                record["date"], "%Y-%m-%d"
            )
        except (ValueError, TypeError, KeyError):
            continue

        # 23:59:59 IST wall-clock (naive) — attendance times are stored in IST.
        check_out = record_date.replace(
            hour=23,
            minute=59,
            second=59,
        )

        await db.attendance.update_one(
            {"_id": record["_id"]},
            {
                "$set": {
                    "status": "COMPLETED",
                    "checkOut": check_out,
                    "autoClosedByCron": True,
                    "updatedAt": now_ist_naive(),
                }
            },
        )
        closed += 1

        # --- spend a credit + notify ---
        uid = record.get("userId")
        if not uid:
            continue
        try:
            user = await db.users.find_one({"_id": ObjectId(uid)})
        except (InvalidId, TypeError):
            user = None
        if not user:
            continue

        left = max(0, int(user.get("autoCheckoutQuota", 5)) - 1)
        await db.users.update_one(
            {"_id": user["_id"]}, {"$set": {"autoCheckoutQuota": left}}
        )
        name = user.get("name") or "A teammate"
        d = record["date"]
        try:
            await notify_user(
                uid, "auto_checkout",
                "Auto check-out used",
                f"You didn't check out on {d}, so we auto-closed it at 11:59 PM. "
                f"{left} of 5 auto check-outs left this month.",
                {"type": "auto_checkout", "date": d, "left": left},
            )
        except Exception:
            pass

        recipients = set(hr_ids)
        if user.get("reportingManagerId"):
            recipients.add(str(user["reportingManagerId"]))
        recipients.discard(uid)
        for rid in recipients:
            try:
                await notify_user(
                    rid, "auto_checkout_report",
                    "Team member auto checked-out",
                    f"{name} forgot to check out on {d} — auto-closed. "
                    f"{left}/5 credits left.",
                    {"type": "auto_checkout_report", "userId": uid, "date": d},
                )
            except Exception:
                pass

    print(
        f"[scheduler] auto_close_attendance: closed {closed} record(s)"
    )


def _accrual_history_entries(
    last_month: int,
    current_month: int,
    per_month: float,
    total_added: float,
    at: datetime,
) -> list[dict]:
    """One row per month covered. The last row absorbs the cap so the
    sum of `addedDays` matches the actual allocated delta."""
    months = list(range(last_month + 1, current_month + 1))
    if not months:
        return []
    entries: list[dict] = []
    remaining = total_added
    for idx, m in enumerate(months):
        if idx == len(months) - 1:
            added = round(remaining, 2)
        else:
            added = round(min(per_month, remaining), 2)
        entries.append({"month": m, "addedDays": added, "at": at})
        remaining = round(remaining - added, 2)
        if remaining <= 0:
            break
    return entries


# ================= MONTHLY LEAVE ACCRUAL =================
async def monthly_leave_accrual() -> None:
    """Runs on the 1st of every month at 00:05.

    For each Active user × each active leave type with daysPerMonth > 0,
    increments leave_balances.allocated by `(currentMonth - lastAccruedMonth)
    * daysPerMonth`, capped at daysPerYear. This lets a downtime of one or
    more months catch up on the next successful run rather than silently
    skipping the accrual.

    Legacy rows (no lastAccruedMonth) default to currentMonth-1 so this run
    behaves like the old one-month bump. From the next run onward, the
    field is stamped and real backfill kicks in.
    """
    today = now_ist_naive()
    year = today.year
    current_month = today.month
    now = datetime.now(timezone.utc)

    # Refill everyone's monthly auto-checkout credits back to 5.
    await db.users.update_many({}, {"$set": {"autoCheckoutQuota": 5}})

    leave_types = []
    async for t in db.leave_types.find(
        {"isActive": True}
    ):
        leave_types.append(t)

    user_ids: list[str] = []
    async for u in db.users.find({
        "$or": [
            {"status": "Active"},
            {"status": {"$exists": False}},
        ]
    }):
        user_ids.append(str(u["_id"]))

    accrued = 0

    for user_id in user_ids:

        for lt in leave_types:

            code = lt.get("code")
            per_month = float(lt.get("daysPerMonth", 0))
            per_year = float(lt.get("daysPerYear", 0))

            if per_month <= 0:
                continue

            existing = await db.leave_balances.find_one({
                "userId": user_id,
                "leaveTypeCode": code,
                "year": year,
            })

            if existing:
                current = float(existing.get("allocated", 0))
                last = existing.get("lastAccruedMonth")
                if last is None:
                    # Pre-migration row — emulate the old one-month bump
                    # and stamp the field so future runs can backfill.
                    last_month = max(0, current_month - 1)
                else:
                    last_month = int(last)

                months_to_add = current_month - last_month
                if months_to_add <= 0:
                    continue

                bump = months_to_add * per_month
                new_allocated = current + bump
                if per_year > 0:
                    new_allocated = min(
                        new_allocated, per_year
                    )

                actually_added = new_allocated - current
                if (
                    new_allocated <= current
                    and last_month == current_month
                ):
                    continue

                # Append a history entry per month covered, so the FE can
                # render "1.5 / 18 this month" with a per-month breakdown.
                history_entries = _accrual_history_entries(
                    last_month, current_month, per_month,
                    actually_added, now,
                )

                update_doc: dict = {
                    "$set": {
                        "allocated": new_allocated,
                        "lastAccruedMonth": current_month,
                        "updatedAt": now,
                    }
                }
                if history_entries:
                    update_doc["$push"] = {
                        "accrualHistory": {"$each": history_entries}
                    }
                await db.leave_balances.update_one(
                    {"_id": existing["_id"]},
                    update_doc,
                )
            else:
                # Brand-new row: accrue from January up to now so a user
                # added mid-year sees their proportional balance.
                months_to_add = current_month
                new_allocated = months_to_add * per_month
                if per_year > 0:
                    new_allocated = min(
                        new_allocated, per_year
                    )
                history_entries = _accrual_history_entries(
                    0, current_month, per_month,
                    new_allocated, now,
                )
                await db.leave_balances.insert_one({
                    "userId": user_id,
                    "leaveTypeCode": code,
                    "year": year,
                    "allocated": new_allocated,
                    "used": 0.0,
                    "pending": 0.0,
                    "lastAccruedMonth": current_month,
                    "accrualHistory": history_entries,
                    "createdAt": now,
                    "updatedAt": now,
                })

            accrued += 1

    print(
        f"[scheduler] monthly_leave_accrual: "
        f"{accrued} balance update(s) across "
        f"{len(user_ids)} user(s) × "
        f"{len(leave_types)} type(s)"
    )


# ================= DAILY ABSENT MARKER =================
async def daily_absent_marker() -> None:
    """Runs at 00:30 server local time (after auto_close_attendance).

    For yesterday: every Active user with no attendance record AND no
    approved leave covering that date AND yesterday wasn't a weekend or
    public holiday gets a synthetic ABSENT row. Weekends and holidays are
    NOT auto-inserted — they're derivable at read time.
    """
    yesterday = (
        now_ist_naive() - timedelta(days=1)
    ).strftime("%Y-%m-%d")

    if is_weekend(yesterday):
        print(
            f"[scheduler] daily_absent_marker: {yesterday} is a weekend "
            "— skipping"
        )
        return

    if await db.holidays.find_one({"date": yesterday}):
        print(
            f"[scheduler] daily_absent_marker: {yesterday} is a holiday "
            "— skipping"
        )
        return

    # Active user ids (legacy users without `status` are treated as Active).
    user_ids: list[str] = []
    async for u in db.users.find({
        "$or": [
            {"status": "Active"},
            {"status": {"$exists": False}},
        ]
    }):
        user_ids.append(str(u["_id"]))

    if not user_ids:
        return

    # Users with an attendance row for yesterday — skip them.
    has_attendance: set[str] = set()
    async for r in db.attendance.find(
        {"date": yesterday, "userId": {"$in": user_ids}},
        {"userId": 1},
    ):
        if r.get("userId"):
            has_attendance.add(r["userId"])

    # Users with approved leave covering yesterday — skip them too.
    on_leave: set[str] = set()
    async for lr in db.leave_requests.find(
        {
            "status": "APPROVED",
            "userId": {"$in": user_ids},
            "fromDate": {"$lte": yesterday},
            "toDate": {"$gte": yesterday},
        },
        {"userId": 1},
    ):
        if lr.get("userId"):
            on_leave.add(lr["userId"])

    now = datetime.now(timezone.utc)
    inserted = 0

    for uid in user_ids:
        if uid in has_attendance or uid in on_leave:
            continue
        try:
            await db.attendance.insert_one({
                "userId": uid,
                "date": yesterday,
                "attendanceType": "ABSENT",
                "status": "ABSENT",
                "checkIn": None,
                "checkOut": None,
                "workNotes": "",
                "isLate": False,
                "hoursWorked": 0.0,
                "overtimeHours": 0.0,
                "syntheticAbsent": True,
                "createdAt": now,
                "updatedAt": now,
            })
            inserted += 1
        except Exception as e:
            # Unique (userId, date) index race — someone created the row
            # between our query and now. Safe to ignore.
            print(f"[scheduler] daily_absent_marker: skip {uid}: {e}")

    print(
        f"[scheduler] daily_absent_marker: {inserted} ABSENT row(s) "
        f"created for {yesterday}"
    )


# ================= MORNING CHECK-IN REMINDER =================
async def morning_checkin_reminder() -> None:
    """Runs hourly on the hour from 10:00–17:00 (scheduler tz): nudge every
    Active user who still hasn't started their day to check in, once per hour
    until they do.

    Skips weekends and public holidays, users who already have any
    attendance row for today (checked in, WFH, on-leave, etc.), and users on
    approved leave covering today — so nobody who's already accounted for, or
    who applied leave, gets pinged.
    """
    from utils.push import push_to_users

    today = now_ist_naive().strftime("%Y-%m-%d")

    if is_weekend(today):
        print(
            f"[scheduler] morning_checkin_reminder: {today} is a weekend "
            "— skipping"
        )
        return

    if await db.holidays.find_one({"date": today}):
        print(
            f"[scheduler] morning_checkin_reminder: {today} is a holiday "
            "— skipping"
        )
        return

    active_user_ids: list[str] = []
    async for u in db.users.find(
        {
            "$or": [
                {"status": "Active"},
                {"status": {"$exists": False}},
            ]
        },
        {"_id": 1},
    ):
        active_user_ids.append(str(u["_id"]))

    if not active_user_ids:
        return

    # Users who already have an attendance row for today — skip them.
    has_attendance: set[str] = set()
    async for r in db.attendance.find(
        {"date": today, "userId": {"$in": active_user_ids}},
        {"userId": 1},
    ):
        if r.get("userId"):
            has_attendance.add(r["userId"])

    # Users on approved leave covering today — skip them too.
    on_leave: set[str] = set()
    async for lr in db.leave_requests.find(
        {
            "status": "APPROVED",
            "userId": {"$in": active_user_ids},
            "fromDate": {"$lte": today},
            "toDate": {"$gte": today},
        },
        {"userId": 1},
    ):
        if lr.get("userId"):
            on_leave.add(lr["userId"])

    targets = [
        uid
        for uid in active_user_ids
        if uid not in has_attendance and uid not in on_leave
    ]

    if not targets:
        print("[scheduler] morning_checkin_reminder: nobody to nudge")
        return

    try:
        await push_to_users(
            targets,
            "You haven't checked in yet",
            "You're not checked in and have no leave for today — tap to check in.",
            {"type": "checkin_reminder"},
            channel_id="checkin",
        )
    except Exception as e:
        print(f"[scheduler] morning_checkin_reminder push failed: {e}")

    print(
        f"[scheduler] morning_checkin_reminder: nudged {len(targets)} user(s)"
    )


# ================= EVENING CHECKOUT REMINDER =================
async def evening_checkout_reminder() -> None:
    """Runs hourly on the hour from 18:00–22:00 (scheduler tz): push every
    user still CHECKED_IN today, once per hour until they check out."""
    from utils.push import push_to_users

    today = now_ist_naive().strftime("%Y-%m-%d")

    user_ids: list[str] = []
    async for r in db.attendance.find({
        "status": "CHECKED_IN",
        "date": today,
    }):
        if r.get("userId"):
            user_ids.append(r["userId"])

    if not user_ids:
        print(
            "[scheduler] evening_checkout_reminder: nobody to nudge"
        )
        return

    try:
        await push_to_users(
            user_ids,
            "You haven't checked out yet",
            "You're still checked in — tap to check out and wrap up your day.",
            {"type": "checkout_reminder"},
            channel_id="checkout",
        )
    except Exception as e:
        print(f"[scheduler] evening_checkout_reminder push failed: {e}")

    print(
        f"[scheduler] evening_checkout_reminder: nudged "
        f"{len(user_ids)} user(s)"
    )


# ================= 8-HOUR CHECK-OUT REMINDER =================
async def checkout_reminder_8h() -> None:
    """Every 30 min: nudge anyone still CHECKED_IN 8+ hours after check-in to
    check out. Fires once per record (checkoutReminderSent). Skips leave days.
    """
    from utils.push import push_to_users
    from utils.ist import now_ist_naive

    today = now_ist_naive().strftime("%Y-%m-%d")
    threshold = now_ist_naive() - timedelta(hours=8)

    user_ids: list[str] = []
    ids_to_flag: list = []
    async for r in db.attendance.find({
        "status": "CHECKED_IN",
        "date": today,
        "attendanceType": {"$ne": "LEAVE"},
        "checkoutReminderSent": {"$ne": True},
        "checkIn": {"$lte": threshold},
    }):
        if r.get("userId"):
            user_ids.append(r["userId"])
            ids_to_flag.append(r["_id"])

    if not user_ids:
        return

    for _id in ids_to_flag:
        await db.attendance.update_one(
            {"_id": _id}, {"$set": {"checkoutReminderSent": True}}
        )

    try:
        await push_to_users(
            user_ids,
            "Time to check out",
            "You've been checked in for 8+ hours — don't forget to check out.",
            {"type": "checkout_8h"},
            channel_id="checkout",
        )
    except Exception as e:
        print(f"[scheduler] checkout_reminder_8h push failed: {e}")

    print(f"[scheduler] checkout_reminder_8h: nudged {len(user_ids)} user(s)")


# ================= DAILY HR ATTENDANCE BRIEF =================
async def hr_daily_attendance_brief() -> None:
    """Runs Mon–Sat at 11:00 server local time.

    Sends every HR user a quick summary of today's attendance posture:
    how many employees are on leave, working from home, and in office.
    Push + in-app notification both fired so HR sees it either way.
    """
    from utils.push import push_to_users
    from utils.notify import create_notification

    today = now_ist_naive().strftime("%Y-%m-%d")

    # Active employee pool — terminated users are excluded from counts.
    active_user_ids: list[str] = []
    async for u in db.users.find(
        {
            "$or": [
                {"status": "Active"},
                {"status": {"$exists": False}},
            ]
        },
        {"_id": 1},
    ):
        active_user_ids.append(str(u["_id"]))

    if not active_user_ids:
        print("[scheduler] hr_daily_attendance_brief: no active users")
        return

    office_count = 0
    wfh_count = 0
    on_leave_attendance_count = 0
    holiday_count = 0

    async for r in db.attendance.find(
        {"date": today, "userId": {"$in": active_user_ids}},
        {"attendanceType": 1, "userId": 1},
    ):
        t = (r.get("attendanceType") or "").upper()
        if t == "OFFICE":
            office_count += 1
        elif t == "WFH":
            wfh_count += 1
        elif t == "LEAVE":
            on_leave_attendance_count += 1
        elif t == "HOLIDAY":
            holiday_count += 1

    # Approved leaves covering today — separate count because employees
    # on approved leave may not have an attendance row.
    on_leave_users: set[str] = set()
    async for lr in db.leave_requests.find(
        {
            "status": "APPROVED",
            "userId": {"$in": active_user_ids},
            "fromDate": {"$lte": today},
            "toDate": {"$gte": today},
        },
        {"userId": 1},
    ):
        if lr.get("userId"):
            on_leave_users.add(lr["userId"])
    leave_count = len(on_leave_users)

    # All HR recipients.
    hr_user_ids: list[str] = []
    async for h in db.users.find(
        {"role": "HR", "status": {"$ne": "Terminated"}},
        {"_id": 1},
    ):
        hr_user_ids.append(str(h["_id"]))

    if not hr_user_ids:
        print("[scheduler] hr_daily_attendance_brief: no HR recipients")
        return

    title = f"Today's attendance — {today}"
    body = (
        f"On leave: {leave_count}  ·  WFH: {wfh_count}  ·  "
        f"Office: {office_count}"
        + (f"  ·  Holiday: {holiday_count}" if holiday_count else "")
    )

    data_payload = {
        "type": "hr_daily_brief",
        "date": today,
        "onLeave": leave_count,
        "wfh": wfh_count,
        "office": office_count,
        "holiday": holiday_count,
    }

    # Push (best-effort) — failures here shouldn't suppress the in-app
    # notification, which is the more reliable channel.
    try:
        await push_to_users(hr_user_ids, title, body, data_payload)
    except Exception as e:
        print(f"[scheduler] hr_daily_attendance_brief push failed: {e}")

    # In-app notification — guaranteed channel HR can see on next open.
    for hr_id in hr_user_ids:
        try:
            await create_notification(
                hr_id,
                "hr_daily_brief",
                title,
                body,
                data_payload,
            )
        except Exception as e:
            print(
                f"[scheduler] hr_daily_attendance_brief notify {hr_id} "
                f"failed: {e}"
            )

    print(
        f"[scheduler] hr_daily_attendance_brief: notified "
        f"{len(hr_user_ids)} HR — leave={leave_count}, wfh={wfh_count}, "
        f"office={office_count}, holiday={holiday_count}"
    )


# ================= TO-DO REMINDERS =================
def _parse_reminder_dt(value) -> "datetime | None":
    """Parse a stored reminderAt into an aware UTC datetime.

    reminderAt is stored as the ISO string the client sent, which may end
    in 'Z', carry an offset, or (older rows) be naive. Normalise all of
    them to aware UTC so the <= now comparison is correct."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def todo_reminder_dispatch() -> None:
    """Runs every minute. Fires a push + in-app notification for any OPEN
    to-do whose reminderAt has arrived and hasn't been sent yet, then
    stamps reminderSent so it never double-fires.

    This is the server-side companion to the client's local reminder — it
    delivers even when the app is closed or the user is on the web.
    """
    from utils.notify import notify_user

    now = datetime.now(timezone.utc)

    candidates = []
    async for t in db.todos.find({
        "status": {"$ne": "DONE"},
        "reminderAt": {"$ne": None},
        "reminderSent": {"$ne": True},
    }):
        candidates.append(t)

    sent = 0
    for t in candidates:
        due = _parse_reminder_dt(t.get("reminderAt"))
        if not due or due > now:
            continue

        user_id = t.get("userId")
        title = "To-do reminder"
        body = t.get("title") or "You have a to-do due."

        # Stamp first so a notify failure can't cause an infinite re-fire
        # loop; the worst case is a missed reminder, not a notification
        # storm.
        await db.todos.update_one(
            {"_id": t["_id"]},
            {"$set": {"reminderSent": True, "updatedAt": now}},
        )

        if user_id:
            try:
                await notify_user(
                    user_id,
                    "todo_reminder",
                    title,
                    body,
                    {"todoId": str(t["_id"])},
                )
                sent += 1
            except Exception as e:
                print(f"[scheduler] todo_reminder notify failed: {e}")

    if sent:
        print(f"[scheduler] todo_reminder_dispatch: sent {sent} reminder(s)")


# ================= LIFECYCLE =================
# Held for the process lifetime once acquired — never closed on purpose, so
# the OS releases it only when this worker exits.
_scheduler_lock_fd = None


def _acquire_singleton_lock() -> bool:
    """Elect a single scheduler owner across gunicorn workers.

    The production image runs WEB_CONCURRENCY gunicorn workers that share one
    filesystem, so without this every worker would boot its own APScheduler
    and each job would fire once *per worker* — e.g. duplicate check-in /
    check-out reminder pushes. An flock on a shared file lets exactly one
    worker win; it's released automatically if that worker dies, so a
    respawned worker can take the scheduler over.

    On platforms without fcntl (Windows dev) or if the lock can't be taken
    for an unexpected reason, returns True so a single dev process still
    schedules normally.
    """
    global _scheduler_lock_fd
    try:
        import fcntl
    except ImportError:
        return True  # Windows dev — single process, no contention.

    path = os.getenv("SCHEDULER_LOCK_FILE", "/tmp/hrms-scheduler.lock")
    try:
        fd = open(path, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False  # Another worker already owns the scheduler.
    except OSError:
        return True  # Can't lock (e.g. odd FS) — better to run than not.
    _scheduler_lock_fd = fd
    return True


def start_scheduler() -> None:
    if not _acquire_singleton_lock():
        print(
            "[scheduler] another worker owns the scheduler — skipping start "
            "in this worker"
        )
        return

    scheduler.add_job(
        auto_close_attendance,
        CronTrigger(hour=0, minute=1, timezone=_TZ),
        id="auto_close_attendance",
        replace_existing=True,
    )

    scheduler.add_job(
        monthly_leave_accrual,
        CronTrigger(day=1, hour=0, minute=5, timezone=_TZ),
        id="monthly_leave_accrual",
        replace_existing=True,
    )

    # Hourly 10:00–17:00 IST — repeatedly nudge anyone who hasn't checked in
    # yet (and hasn't applied leave) until they start their day.
    scheduler.add_job(
        morning_checkin_reminder,
        CronTrigger(hour="10-17", minute=0, timezone=_TZ),
        id="morning_checkin_reminder",
        replace_existing=True,
    )

    # Hourly 18:00–22:00 IST — repeatedly nudge anyone still checked in to
    # check out until they wrap up their day.
    scheduler.add_job(
        evening_checkout_reminder,
        CronTrigger(hour="18-22", minute=0, timezone=_TZ),
        id="evening_checkout_reminder",
        replace_existing=True,
    )

    scheduler.add_job(
        daily_absent_marker,
        CronTrigger(hour=0, minute=30, timezone=_TZ),
        id="daily_absent_marker",
        replace_existing=True,
    )

    # Mon-Sat at 11:00 IST. Pushes the daily attendance brief to
    # every HR user. Skipped automatically on Sundays via day_of_week.
    scheduler.add_job(
        hr_daily_attendance_brief,
        CronTrigger(day_of_week="mon-sat", hour=11, minute=0, timezone=_TZ),
        id="hr_daily_attendance_brief",
        replace_existing=True,
    )

    # Every minute: deliver any due to-do reminders (server-side, so they
    # fire even when the app is closed / on web).
    scheduler.add_job(
        todo_reminder_dispatch,
        IntervalTrigger(minutes=1),
        id="todo_reminder_dispatch",
        replace_existing=True,
    )

    # Every 30 min: remind anyone checked in 8+ hours to check out.
    scheduler.add_job(
        checkout_reminder_8h,
        IntervalTrigger(minutes=30),
        id="checkout_reminder_8h",
        replace_existing=True,
    )

    scheduler.start()

    print("[scheduler] started — jobs:", [
        j.id for j in scheduler.get_jobs()
    ])


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
