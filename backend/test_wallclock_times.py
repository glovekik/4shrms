"""The two time contracts, and which endpoints owe which.

An attendance time reaches the server one of two ways, and they are not
interchangeable:

  * an INSTANT a device captured  — "check me in, now" — which arrives in UTC
    and must be converted to IST before it is stored;
  * a WALL-CLOCK time a person TYPED — "I actually left at 8:15 PM" — which is
    already an office time and must be stored exactly as typed.

Running a typed time through the instant parser added 5:30 to it. On a phone
set to IST nobody noticed, because the app built the Date locally and
toISOString() cancelled the error out. On a desktop left on UTC, an Android
emulator, or a laptop carried abroad it did not cancel: a correction for
8:15 PM was stored as 1:45 AM the following morning, approval recomputed the
day at sixteen hours, and the register showed a shift nobody worked.

Run:  python test_wallclock_times.py
"""
import sys
from datetime import datetime, timedelta, timezone

from utils.ist import (
    parse_client_to_ist_naive,
    parse_wallclock_to_ist_naive,
    iso_naive,
)
from utils.attendance_rules import classify_on_checkout, hours_between


class T:
    passed = 0
    failed = 0


def ck(name, cond, detail=""):
    if cond:
        T.passed += 1
        print(f"   ok   {name}")
    else:
        T.failed += 1
        print(f"   FAIL {name}")
        if detail:
            print(f"        {detail}")


IST = timezone(timedelta(hours=5, minutes=30))


def as_utc_instant(wall: str) -> str:
    """What an older installed build sends: the typed time, localised on an
    IST device, then serialised by toISOString()."""
    return (
        datetime.fromisoformat(wall).replace(tzinfo=IST)
        .astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )


print("TYPED TIMES  (corrections, manual entry, HR edits)")

# The case that was broken: an offset-less string is the office clock.
got = parse_wallclock_to_ist_naive("2026-09-04T20:15:00")
ck("a typed 8:15 PM stays 8:15 PM", got == datetime(2026, 9, 4, 20, 15), str(got))
ck("and does not roll onto the next day", got.date() == datetime(2026, 9, 4).date(), str(got))

# Builds already on people's phones keep working: they send an explicit offset.
got = parse_wallclock_to_ist_naive(as_utc_instant("2026-09-04T20:15:00"))
ck("an older build's UTC instant still lands on 8:15 PM",
   got == datetime(2026, 9, 4, 20, 15), str(got))

got = parse_wallclock_to_ist_naive("2026-09-04T20:15:00+05:30")
ck("an explicit +05:30 is honoured", got == datetime(2026, 9, 4, 20, 15), str(got))

got = parse_wallclock_to_ist_naive("2026-09-04T14:45:00Z")
ck("an explicit Z is converted", got == datetime(2026, 9, 4, 20, 15), str(got))

ck("nothing in, nothing out", parse_wallclock_to_ist_naive("") is None)

print("\nCAPTURED INSTANTS  (live check-in / check-out)")

got = parse_client_to_ist_naive("2026-09-04T14:45:00Z")
ck("a device's UTC instant becomes IST", got == datetime(2026, 9, 4, 20, 15), str(got))
got = parse_client_to_ist_naive("2026-09-04T14:45:00")
ck("an offset-less instant is still read as UTC",
   got == datetime(2026, 9, 4, 20, 15), str(got))

print("\nWHAT THE OLD BEHAVIOUR COST")

wrong = parse_client_to_ist_naive("2026-09-04T20:15:00")   # the old path
right = parse_wallclock_to_ist_naive("2026-09-04T20:15:00")
check_in = datetime(2026, 9, 4, 9, 30)
ck("the old path produced a 16-hour day",
   round(hours_between(check_in, wrong), 2) == 16.25,
   str(hours_between(check_in, wrong)))
ck("the fixed path produces 10.75",
   round(hours_between(check_in, right), 2) == 10.75,
   str(hours_between(check_in, right)))

print("\nA DAY THAT ENDS BEFORE IT STARTS")

# hours_between clamps a negative span to zero, which is why a mistyped AM/PM
# has to be refused before it reaches the classifier rather than after.
out_before_in = datetime(2026, 9, 4, 7, 0)
ck("a reversed span silently clamps to 0.0",
   hours_between(check_in, out_before_in) == 0.0,
   str(hours_between(check_in, out_before_in)))
ck("...and would be filed as a half day",
   classify_on_checkout(check_in, out_before_in)["status"] == "HALF_DAY",
   classify_on_checkout(check_in, out_before_in)["status"])
# The refusal itself lives in routes/corrections.py and is covered against a
# running API (a reversed correction must be rejected at submit, and the day
# must keep its hours) — it can't be asserted from here.

print("\nSERIALISATION")

ck("a stored time goes out without a Z",
   iso_naive(datetime(2026, 9, 4, 20, 15)) == "2026-09-04T20:15:00",
   iso_naive(datetime(2026, 9, 4, 20, 15)))
ck("a round trip through the wall-clock parser is stable",
   parse_wallclock_to_ist_naive(iso_naive(datetime(2026, 9, 4, 20, 15)))
   == datetime(2026, 9, 4, 20, 15))

print(f"\n{T.passed} passed, {T.failed} failed")
sys.exit(1 if T.failed else 0)
