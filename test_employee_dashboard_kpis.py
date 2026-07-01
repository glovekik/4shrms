"""Test the EMPLOYEE dashboard KPI strip for every real user.

The app home screen ("AT A GLANCE") renders 4 KPIs from /dashboard/me:
  Attendance = attendanceRatePctMTD
  On-time    = onTimeCheckInRatePctMTD
  This week  = avgHoursPerDayThisWeek   (shown as "—h" when null)
  Tasks done = myTaskCompletionRatePct30d
fmtPct() renders null/None as "—".

This calls my_dashboard() directly for each user and prints what the
strip would display, plus the 3 backend KPI fields the strip doesn't
show. Flags any user whose dashboard raises.

Run: backend\\venv\\Scripts\\python.exe test_employee_dashboard_kpis.py
"""

import sys
import asyncio
import math
import traceback

sys.path.insert(0, "backend")
import config  # noqa: F401 — loads .env (Atlas)
from database import db  # noqa: E402
from routes.dashboard import my_dashboard  # noqa: E402


def fmt_pct(v):
    """Mirror of the app's fmtPct()."""
    if v is None or not isinstance(v, (int, float)) or not math.isfinite(v):
        return "—"
    return f"{round(v)}%"


async def main():
    users = []
    async for u in db.users.find({}):
        users.append(u)

    failures = []
    print(f"Testing /dashboard/me for {len(users)} users\n")
    print(f"{'email':<38} {'Attend':>7} {'On-time':>8} "
          f"{'Week':>6} {'Tasks':>6}  | extras")
    print("-" * 100)

    for u in users:
        try:
            d = await my_dashboard(u)
        except Exception:
            failures.append(u.get("email"))
            print(f"{u.get('email','?'):<38}  RAISED:")
            traceback.print_exc()
            continue

        attend = fmt_pct(d.get("attendanceRatePctMTD"))
        ontime = fmt_pct(d.get("onTimeCheckInRatePctMTD"))
        wk = d.get("avgHoursPerDayThisWeek")
        week = f"{wk}h" if wk is not None else "—h"
        tasks = fmt_pct(d.get("myTaskCompletionRatePct30d"))
        extras = (f"ot={d.get('overtimeHoursThisMonth')} "
                  f"pendReq={d.get('pendingRequestsTotal')} "
                  f"docs={fmt_pct(d.get('requiredDocCompletenessPct'))}")
        print(f"{u.get('email','?'):<38} {attend:>7} {ontime:>8} "
              f"{week:>6} {tasks:>6}  | {extras}")

    print("-" * 100)
    print(f"\n{len(users) - len(failures)}/{len(users)} users: "
          f"dashboard returned without error")
    if failures:
        print(f"FAILURES (raised): {failures}")
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
