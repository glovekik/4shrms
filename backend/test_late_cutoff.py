"""Boundary test for the late-arrival cutoff.

The rule is one line, but it is the line that decides whether someone's
attendance says LATE — so the boundary itself is worth pinning down: at the
cutoff is on time, one minute past is late.

Run from backend/:
    python test_late_cutoff.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import LATE_AFTER_HOUR, LATE_AFTER_MINUTE, GRACE_MINUTES  # noqa: E402
from utils.attendance_rules import is_late, classify_on_checkout  # noqa: E402

P, F = [0], [0]


def expect(name, cond, detail=""):
    if cond:
        P[0] += 1
        print(f"  PASS  {name}")
    else:
        F[0] += 1
        print(f"  FAIL  {name}\n        {detail}")


def at(h, m, s=0):
    return datetime(2026, 9, 9, h, m, s)


cutoff_h = LATE_AFTER_HOUR
cutoff_m = LATE_AFTER_MINUTE + GRACE_MINUTES
print(f"configured cutoff: {cutoff_h:02d}:{cutoff_m:02d}\n")

expect("cutoff is 10:50", (cutoff_h, cutoff_m) == (10, 50),
       f"got {cutoff_h}:{cutoff_m}")

expect("09:30 on time", not is_late(at(9, 30)))
expect("10:00 on time — was late before this change", not is_late(at(10, 0)))
expect("10:30 on time — the old effective cutoff", not is_late(at(10, 30)))
expect("10:49 on time", not is_late(at(10, 49)))
expect("10:50:00 exactly — on time", not is_late(at(10, 50)))
expect("10:50:01 late", is_late(at(10, 50, 1)))
expect("10:51 late", is_late(at(10, 51)))
expect("11:30 late", is_late(at(11, 30)))
expect("no check-in is not late", not is_late(None))

# A tz-aware value must be read as IST, not compared raw.
from utils.ist import IST  # noqa: E402
expect("tz-aware 10:45 IST on time",
       not is_late(datetime(2026, 9, 9, 10, 45, tzinfo=IST)))
expect("tz-aware 11:00 IST late",
       is_late(datetime(2026, 9, 9, 11, 0, tzinfo=IST)))

# Status must follow the same boundary through a full day.
on_time = classify_on_checkout(at(10, 50), at(19, 0))
expect("10:50 full day is PRESENT", on_time["status"] == "PRESENT",
       str(on_time))
expect("10:50 not flagged isLate", on_time["isLate"] is False, str(on_time))

late = classify_on_checkout(at(10, 51), at(19, 0))
expect("10:51 full day is LATE", late["status"] == "LATE", str(late))
expect("10:51 flagged isLate", late["isLate"] is True, str(late))

# A short day stays HALF_DAY regardless of arrival time.
short = classify_on_checkout(at(11, 30), at(13, 0))
expect("short day beats late in status", short["status"] == "HALF_DAY",
       str(short))
expect("short day still records lateness", short["isLate"] is True,
       str(short))

print(f"\n{P[0]} passed, {F[0]} failed")
sys.exit(1 if F[0] else 0)
