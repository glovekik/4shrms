"""Attendance classification rules from PRD sections 5 + 22.

Pure functions — no DB, no side effects, no exceptions. Inputs are
already-parsed datetimes; outputs are status strings + numeric fields.
"""

from datetime import datetime, timedelta, time, timezone
from typing import Optional


def _normalize(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce a possibly-naive Mongo datetime into UTC-aware.

    Why: Motor returns BSON dates as offset-naive even though they were
    written as UTC. Mixing them with `datetime.now(timezone.utc)` would
    raise TypeError on subtraction. Treat every naive datetime as UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

from config import (
    LATE_AFTER_HOUR,
    LATE_AFTER_MINUTE,
    GRACE_MINUTES,
    HALF_DAY_MIN_HOURS,
    OVERTIME_AFTER_HOURS,
    WEEKEND_DAYS,
)


# Possible status values for an attendance record.
# CHECKED_IN  — transient, between checkin and checkout
# PRESENT     — checked out, full day, not late
# LATE        — checked out (or in), checked in after grace period
# HALF_DAY    — checked out, worked < HALF_DAY_MIN_HOURS
# ABSENT      — synthesized for a working day with no check-in
# ON_LEAVE    — derived from approved leave covering this date
# WEEK_OFF    — derived from calendar (weekend by default)
# HOLIDAY     — derived from holidays collection
# COMPLETED   — legacy value kept for backward compatibility


def is_late(check_in_dt: datetime) -> bool:
    """True if check-in time is later than the configured cutoff + grace."""
    check_in_dt = _normalize(check_in_dt)
    if not check_in_dt:
        return False
    cutoff_dt = check_in_dt.replace(
        hour=LATE_AFTER_HOUR,
        minute=LATE_AFTER_MINUTE,
        second=0,
        microsecond=0,
    )
    cutoff_dt = cutoff_dt + timedelta(minutes=GRACE_MINUTES)
    return check_in_dt > cutoff_dt


def hours_between(check_in_dt: datetime, check_out_dt: datetime) -> float:
    check_in_dt = _normalize(check_in_dt)
    check_out_dt = _normalize(check_out_dt)
    if not check_in_dt or not check_out_dt:
        return 0.0
    seconds = (check_out_dt - check_in_dt).total_seconds()
    return max(0.0, seconds / 3600.0)


def overtime_hours(hours_worked: float) -> float:
    if hours_worked <= OVERTIME_AFTER_HOURS:
        return 0.0
    return round(hours_worked - OVERTIME_AFTER_HOURS, 2)


def classify_on_checkout(
    check_in_dt: datetime,
    check_out_dt: datetime,
) -> dict:
    """Returns dict with status + computed fields applied at checkout."""
    hours = hours_between(check_in_dt, check_out_dt)
    late = is_late(check_in_dt)

    if hours < HALF_DAY_MIN_HOURS:
        status = "HALF_DAY"
    elif late:
        status = "LATE"
    else:
        status = "PRESENT"

    return {
        "status": status,
        "hoursWorked": round(hours, 2),
        "overtimeHours": overtime_hours(hours),
        "isLate": late,
    }


def is_weekend(date_str: str) -> bool:
    """date_str = YYYY-MM-DD. Returns True if the date is a configured weekend
    day."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    return d.weekday() in WEEKEND_DAYS
