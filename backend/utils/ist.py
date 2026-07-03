"""IST wall-clock helpers for ATTENDANCE times.

Design decision (deliberate): check-in / check-out timestamps are stored as
**IST wall-clock, naive** (no tzinfo) so the raw DB reads in office time — e.g.
a 6:26 PM IST check-in is stored as `2026-07-02 18:26:00`, not `12:56 UTC`.

Rules:
  * Store   : convert the client's UTC instant to IST, drop tzinfo  -> to_ist_naive / parse_client_to_ist_naive
  * Serialize: emit the naive value as-is (NO trailing "Z")          -> iso_naive
  * Display : the frontend shows it as-is (no timezone conversion)
  * Compare : is_late() treats a naive check-in as IST wall-clock

NOTE: this convention applies ONLY to attendance check-in/out (the times users
read). All other timestamps in the system (createdAt/updatedAt on tasks, chat,
audit logs, etc.) remain UTC.
"""
from datetime import datetime, timezone, timedelta

# India has no DST, so a fixed +05:30 offset is exact.
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist_naive() -> datetime:
    """Current time as IST wall-clock, naive."""
    return datetime.now(IST).replace(tzinfo=None)


def today_ist_str() -> str:
    """Today's date in IST as 'YYYY-MM-DD'.

    Use this — NOT datetime.now().strftime(...) — anywhere the result is
    compared against a stored attendance/leave/holiday `date` (all of which
    are IST wall-clock). In a UTC container, datetime.now() is behind IST by
    5.5h, so between 00:00–05:29 IST it returns *yesterday's* date, which
    silently mis-scopes "today" queries and midnight cron jobs.
    """
    return now_ist_naive().strftime("%Y-%m-%d")


def today_ist_date():
    """Today's date in IST as a `datetime.date`."""
    return now_ist_naive().date()


def to_ist_naive(dt):
    """Any datetime (aware, or naive assumed-UTC) -> IST wall-clock, naive."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).replace(tzinfo=None)


def parse_client_to_ist_naive(iso: str):
    """Client ISO string (UTC, maybe trailing Z) -> IST wall-clock, naive."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).replace(tzinfo=None)


def iso_naive(dt):
    """Serialize a stored (IST wall-clock, naive) datetime for the API — no Z."""
    return dt.isoformat() if dt else None
