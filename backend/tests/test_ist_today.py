"""Regression test for the IST wall-clock date helpers.

Reproduces the production scenario that used to break: a UTC container instant
that has already crossed midnight in IST. The helpers must resolve to the IST
business day, not the (earlier) UTC day.
"""
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Allow `python tests/test_ist_today.py` from backend/ without pytest.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.ist import (  # noqa: E402
    IST,
    to_ist_naive,
    now_ist_naive,
    today_ist_str,
    today_ist_date,
)

# 2026-07-02 20:00:00 UTC == 2026-07-03 01:30 IST.
# Under the old code this resolved to 2026-07-02 ("yesterday") for the first
# 5.5 hours of every IST day.
_UTC_JUST_PAST_IST_MIDNIGHT = datetime(2026, 7, 2, 20, 0, 0, tzinfo=timezone.utc)


def test_utc_instant_past_ist_midnight_is_next_ist_day():
    ist = to_ist_naive(_UTC_JUST_PAST_IST_MIDNIGHT)
    assert ist.strftime("%Y-%m-%d") == "2026-07-03"
    assert ist.hour == 1 and ist.minute == 30
    # The naive UTC day would have been the wrong (earlier) date.
    assert _UTC_JUST_PAST_IST_MIDNIGHT.replace(
        tzinfo=None
    ).strftime("%Y-%m-%d") == "2026-07-02"


def test_month_boundary_uses_ist_month():
    # 2026-06-30 20:00 UTC == 2026-07-01 01:30 IST — the accrual job runs at
    # 00:05 IST on the 1st and must accrue for July, not June.
    utc = datetime(2026, 6, 30, 20, 0, 0, tzinfo=timezone.utc)
    ist = to_ist_naive(utc)
    assert ist.month == 7 and ist.day == 1


def test_year_boundary_uses_ist_year():
    # 2026-12-31 20:00 UTC == 2027-01-01 01:30 IST.
    utc = datetime(2026, 12, 31, 20, 0, 0, tzinfo=timezone.utc)
    assert to_ist_naive(utc).year == 2027


def test_helpers_are_offset_based_and_consistent():
    # now_ist_naive must equal converting the current UTC instant to IST,
    # independent of the machine/container local timezone.
    ref = datetime.now(timezone.utc).astimezone(IST).replace(tzinfo=None)
    got = now_ist_naive()
    # Within a couple of seconds of each other.
    assert abs((got - ref).total_seconds()) < 5
    assert today_ist_str() == today_ist_date().strftime("%Y-%m-%d")
    assert IST.utcoffset(None) == timedelta(hours=5, minutes=30)


if __name__ == "__main__":
    test_utc_instant_past_ist_midnight_is_next_ist_day()
    test_month_boundary_uses_ist_month()
    test_year_boundary_uses_ist_year()
    test_helpers_are_offset_based_and_consistent()
    print("OK: IST today/date helpers resolve to the IST business day")
