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

from config import (  # noqa: E402
    LATE_AFTER_HOUR,
    LATE_AFTER_MINUTE,
    GRACE_MINUTES,
    REQUIRED_DAY_HOURS,
    OVERTIME_AFTER_HOURS,
)
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

expect("cutoff is 10:30", (cutoff_h, cutoff_m) == (10, 30),
       f"got {cutoff_h}:{cutoff_m}")

expect("09:30 on time", not is_late(at(9, 30)))
expect("10:00 on time", not is_late(at(10, 0)))
expect("10:30:00 exactly — on time", not is_late(at(10, 30)))
expect("10:30:01 late", is_late(at(10, 30, 1)))
expect("10:31 late", is_late(at(10, 31)))
expect("11:30 late", is_late(at(11, 30)))
expect("no check-in is not late", not is_late(None))

# A tz-aware value must be read as IST, not compared raw.
from utils.ist import IST  # noqa: E402
expect("tz-aware 10:15 IST on time",
       not is_late(datetime(2026, 9, 9, 10, 15, tzinfo=IST)))
expect("tz-aware 11:00 IST late",
       is_late(datetime(2026, 9, 9, 11, 0, tzinfo=IST)))

# Status must follow the same boundary through a full day.
on_time = classify_on_checkout(at(10, 30), at(19, 30))
expect("10:30 full day is PRESENT", on_time["status"] == "PRESENT",
       str(on_time))
expect("10:30 not flagged isLate", on_time["isLate"] is False, str(on_time))

late = classify_on_checkout(at(10, 31), at(19, 30))
expect("10:31 full day is LATE", late["status"] == "LATE", str(late))
expect("10:31 flagged isLate", late["isLate"] is True, str(late))

# A short day stays HALF_DAY regardless of arrival time.
short = classify_on_checkout(at(11, 30), at(13, 0))
expect("short day beats late in status", short["status"] == "HALF_DAY",
       str(short))
expect("short day still records lateness", short["isLate"] is True,
       str(short))

# ---- a full day is 9 hours from each person's own check-in ----------------
from datetime import timedelta  # noqa: E402

print(f"\nrequired day: {REQUIRED_DAY_HOURS}h from check-in")
expect("a full day is 9 hours", REQUIRED_DAY_HOURS == 9,
       str(REQUIRED_DAY_HOURS))

# The finish time is personal: arriving earlier means finishing earlier.
for h, m, want_h, want_m in [
    (9, 15, 18, 15),
    (10, 0, 19, 0),
    (10, 30, 19, 30),   # the latest on-time arrival
    (11, 15, 20, 15),   # late arrival still owes a full 9 hours
]:
    finish = at(h, m) + timedelta(hours=REQUIRED_DAY_HOURS)
    expect(f"{h:02d}:{m:02d} in -> {want_h:02d}:{want_m:02d} out",
           (finish.hour, finish.minute) == (want_h, want_m),
           f"got {finish.hour:02d}:{finish.minute:02d}")

# Checking out on the dot is a complete day, a minute early is not. The app
# warns on the second case and still allows it.
complete = classify_on_checkout(at(10, 30), at(19, 30))
expect("exactly 9h counts as complete",
       complete["hoursWorked"] >= REQUIRED_DAY_HOURS, str(complete))
expect("...and earns no overtime", complete["overtimeHours"] == 0, str(complete))

short = classify_on_checkout(at(10, 30), at(19, 29))
expect("a minute short is under the day",
       short["hoursWorked"] < REQUIRED_DAY_HOURS, str(short))
expect("...but is still a normal present day, not flagged",
       short["status"] == "PRESENT", str(short))

over = classify_on_checkout(at(10, 30), at(20, 30))
expect("an hour past the day earns an hour of overtime",
       abs(over["overtimeHours"] - 1.0) < 0.01, str(over))

expect("required day and overtime threshold agree today",
       REQUIRED_DAY_HOURS == OVERTIME_AFTER_HOURS,
       f"{REQUIRED_DAY_HOURS} vs {OVERTIME_AFTER_HOURS}")

print(f"\n{P[0]} passed, {F[0]} failed")
sys.exit(1 if F[0] else 0)
