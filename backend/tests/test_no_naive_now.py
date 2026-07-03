"""Guard test: no naive/local 'now' or 'today' in request/scheduler code.

Attendance, leave, holiday and payroll dates are stored as IST wall-clock
(see utils/ist.py). The production container runs in UTC, so `datetime.now()`,
`datetime.utcnow()`, `datetime.today()` and `date.today()` return the *UTC*
day — which is yesterday for the 5.5h window 00:00–05:29 IST, and the wrong
month/year at those boundaries. That silently mis-scopes "today" queries and
midnight cron jobs.

The rule this test enforces:
  * For a business DATE (compared against a stored IST date) -> use
    utils.ist.today_ist_str() / today_ist_date() / now_ist_naive().
  * For a UTC audit timestamp (createdAt/updatedAt/…) -> use the *explicit*
    datetime.now(timezone.utc), which is unambiguous regardless of container TZ.

Either way, the bare/naive forms below are banned in routes/ and utils/. If a
new hit appears, convert it to one of the two sanctioned forms above rather
than adding an exception here.
"""
import re
from pathlib import Path

# backend/ root (this file is backend/tests/test_no_naive_now.py)
BACKEND = Path(__file__).resolve().parent.parent

# Directories whose code runs per-request or on the scheduler.
SCAN_DIRS = ["routes", "utils"]

# utils/ist.py is the sanctioned home of time logic (it *defines* the helpers
# and documents the banned forms in its docstring), so it's exempt.
EXEMPT = {Path("utils") / "ist.py"}

# Bare naive/local now/today. datetime.now(timezone.utc) has an argument and so
# is intentionally NOT matched.
BANNED = re.compile(
    r"\b("
    r"datetime\.now\(\s*\)"      # datetime.now()
    r"|datetime\.utcnow\(\s*\)"  # datetime.utcnow()
    r"|datetime\.today\(\s*\)"   # datetime.today()
    r"|date\.today\(\s*\)"       # date.today()
    r")"
)


def _iter_offenders():
    for d in SCAN_DIRS:
        for path in (BACKEND / d).rglob("*.py"):
            rel = path.relative_to(BACKEND)
            if rel in EXEMPT:
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                # Ignore comments so prose mentioning the banned form is fine.
                code = line.split("#", 1)[0]
                if BANNED.search(code):
                    yield f"{rel}:{lineno}: {line.strip()}"


def test_no_naive_now_in_request_or_scheduler_code():
    offenders = list(_iter_offenders())
    assert not offenders, (
        "Naive/local now/today found in request or scheduler code. Use "
        "utils.ist.today_ist_str()/today_ist_date()/now_ist_naive() for a "
        "business date, or datetime.now(timezone.utc) for a UTC timestamp:\n  "
        + "\n  ".join(offenders)
    )


if __name__ == "__main__":
    hits = list(_iter_offenders())
    if hits:
        print("FAIL: banned naive now/today:")
        for h in hits:
            print("  " + h)
        raise SystemExit(1)
    print("OK: no naive now/today in routes/ or utils/")
