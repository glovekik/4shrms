import logging
import time
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from fastapi.middleware.cors import (
    CORSMiddleware
)

from config import UPLOAD_DIR

from routes.auth import (
    router as auth_router
)

from routes.attendance import (
    router as attendance_router,
    hr_router as attendance_hr_router,
)

from routes.hr import (
    router as hr_router
)

from routes.tl import (
    router as tl_router
)

from routes.manager_tasks import (
    router as manager_tasks_router,
)

from routes.company_docs import (
    user_router as company_docs_user_router,
    hr_router as company_docs_hr_router,
)

from routes.tasks import (
    router as tasks_router
)

from routes.chat import (
    office_router as office_chat_router,
    team_router as team_chat_router,
)

from routes.corrections import (
    user_router as corrections_user_router,
    hr_router as corrections_hr_router,
    manager_router as corrections_manager_router,
)

from routes.leave import (
    user_router as leave_user_router,
    hr_router as leave_hr_router,
    manager_router as leave_manager_router,
)

from routes.departments import (
    user_router as departments_user_router,
    hr_router as departments_hr_router,
)

from routes.audit import router as audit_router

from routes.notifications import router as notifications_router

from routes.dashboard import router as dashboard_router

from routes.projects import (
    user_router as projects_user_router,
    hr_router as projects_hr_router,
)

from routes.todos import router as todos_router

from routes.documents import (
    user_router as documents_user_router,
    hr_router as documents_hr_router,
)

from routes.reimbursements import (
    user_router as reimb_user_router,
    manager_router as reimb_manager_router,
    hr_router as reimb_hr_router,
)

from routes.timesheets import (
    user_router as timesheets_user_router,
    manager_router as timesheets_manager_router,
    hr_router as timesheets_hr_router,
)

from routes.reports import (
    router as reports_router,
    manager_router as reports_manager_router,
)

from routes.exports import router as exports_router

from routes.recruitment import (
    openings_hr_router,
    candidates_hr_router,
    interviews_hr_router,
    interviews_my_router,
    offers_hr_router,
)

from routes.performance import (
    goals_router,
    goals_mgr_router,
    goals_hr_router,
    reviews_router,
    reviews_mgr_router,
    reviews_hr_router,
    feedback_router,
    feedback_hr_router,
)

from routes.uploads import router as uploads_router

from routes.user import router as user_router

from routes.public import (
    careers_router,
    offers_public_router,
)

from routes.sse import router as sse_router

from routes.assets import (
    user_router as assets_user_router,
    hr_router as assets_hr_router,
)

from routes.expenses import (
    router as expenses_router
)

from routes.payroll import (
    user_router as payroll_user_router,
    hr_router as payroll_hr_router,
)

from routes.onboarding import (
    user_router as onboarding_user_router,
    hr_router as onboarding_hr_router,
)

from routes.exit import (
    user_router as exit_user_router,
    hr_router as exit_hr_router,
)

from routes.holidays import (
    user_router as holidays_user_router,
    hr_router as holidays_hr_router,
)

from routes.manual_attendance import (
    user_router as manual_attendance_user_router,
    manager_router as manual_attendance_manager_router,
    hr_router as manual_attendance_hr_router,
)

from database import create_indexes
from utils.scheduler import (
    start_scheduler,
    stop_scheduler,
)

app = FastAPI()


@app.on_event("startup")
async def on_startup():
    await create_indexes()
    start_scheduler()


@app.on_event("shutdown")
async def on_shutdown():
    stop_scheduler()


# ================= HEALTH / ROOT =================
# Render's deploy probe scans for an open port AND looks for an HTTP
# response at "/" — without a root route it logs 404 and the load
# balancer can flag the port as "not really alive". Returning a tiny
# JSON keeps the deploy healthy and gives a quick "is the service up"
# endpoint for ops.
@app.get("/")
async def root():
    return {"status": "ok", "service": "attendance-api"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ================= CORS =================
# allow_credentials must be False when allow_origins=["*"]
# (browsers reject the wildcard with credentials enabled).
app.add_middleware(

    CORSMiddleware,

    allow_origins=["*"],

    allow_credentials=False,

    allow_methods=["*"],

    allow_headers=["*"],
)


# ================= REQUEST TIMING =================
# There was no per-request timing anywhere, so "which API is slow" was
# unanswerable from real traffic. This middleware times every request,
# logs the duration (flagging anything over SLOW_REQUEST_MS), and keeps
# in-process aggregates per route template so GET /_metrics/timings gives
# a live count / avg / p95 / max per endpoint. Single uvicorn worker, so
# the in-memory store is the whole picture; restart clears it.
logger = logging.getLogger("api.timing")
logging.basicConfig(level=logging.INFO)

SLOW_REQUEST_MS = 1000.0

# route-template -> list of recent durations (ms). Capped per route so
# memory stays bounded regardless of traffic.
_TIMING_SAMPLES: dict = defaultdict(list)
_MAX_SAMPLES_PER_ROUTE = 500


@app.middleware("http")
async def time_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    # The matched route template (e.g. "/hr/users/{user_id}") is populated
    # on the scope during routing; fall back to the raw path for unmatched
    # requests (404s) so they don't all collapse into one bucket.
    route = request.scope.get("route")
    label = getattr(route, "path", None) or request.url.path
    key = f"{request.method} {label}"

    samples = _TIMING_SAMPLES[key]
    samples.append(elapsed_ms)
    if len(samples) > _MAX_SAMPLES_PER_ROUTE:
        del samples[0]

    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.1f}"

    line = f"{key} {response.status_code} {elapsed_ms:.1f}ms"
    if elapsed_ms >= SLOW_REQUEST_MS:
        logger.warning("SLOW %s", line)
    else:
        logger.info(line)

    return response


@app.get("/_metrics/timings", tags=["Ops"])
async def timing_metrics():
    """Per-endpoint response-time aggregates since the last restart.

    Sorted slowest-first by p95 so the worst endpoints surface at the top.
    """
    out = []
    for key, samples in _TIMING_SAMPLES.items():
        if not samples:
            continue
        ordered = sorted(samples)
        n = len(ordered)
        p95 = ordered[min(n - 1, int(round(0.95 * (n - 1))))]
        out.append({
            "endpoint": key,
            "count": n,
            "avg_ms": round(sum(ordered) / n, 1),
            "p95_ms": round(p95, 1),
            "max_ms": round(ordered[-1], 1),
            "min_ms": round(ordered[0], 1),
        })
    out.sort(key=lambda r: r["p95_ms"], reverse=True)
    return {"slow_threshold_ms": SLOW_REQUEST_MS, "endpoints": out}


# ================= ROUTES =================
app.include_router(

    auth_router,

    prefix="/auth",

    tags=["Auth"]
)

app.include_router(

    attendance_router,

    prefix="/attendance",

    tags=["Attendance"]
)

# HR attendance listing — separate router so we can require HR/CEO.
app.include_router(
    attendance_hr_router,
    prefix="/hr",
    tags=["HR"],
)

app.include_router(

    hr_router,

    prefix="/hr",

    tags=["HR"]
)

app.include_router(

    tl_router,

    prefix="/tl",

    tags=["TL"]
)

app.include_router(

    tasks_router,

    prefix="/tasks",

    tags=["Tasks"]
)

app.include_router(

    office_chat_router,

    prefix="/office",

    tags=["Office Chat"]
)

app.include_router(

    team_chat_router,

    prefix="/teams",

    tags=["Team Chat"]
)

# Correction-request endpoints attach under /attendance/... and /hr/...
app.include_router(

    corrections_user_router,

    prefix="/attendance",

    tags=["Attendance"]
)

# Manual attendance request workflow — employee submit, manager/HR approve.
app.include_router(
    manual_attendance_user_router,
    prefix="/attendance/manual-request",
    tags=["Attendance"],
)
app.include_router(
    manual_attendance_manager_router,
    prefix="/manager/manual-requests",
    tags=["Manager"],
)
app.include_router(
    manual_attendance_hr_router,
    prefix="/hr/manual-requests",
    tags=["HR"],
)

app.include_router(

    corrections_hr_router,

    prefix="/hr/correction-requests",

    tags=["HR"]
)

app.include_router(

    leave_user_router,

    prefix="/leaves",

    tags=["Leave"]
)

app.include_router(

    leave_hr_router,

    prefix="/hr",

    tags=["HR"]
)

app.include_router(

    assets_user_router,

    prefix="/assets",

    tags=["Assets"]
)

app.include_router(

    assets_hr_router,

    prefix="/hr",

    tags=["HR"]
)

app.include_router(

    expenses_router,

    prefix="/hr/expenses",

    tags=["HR Expenses"]
)

app.include_router(

    payroll_user_router,

    prefix="/payroll",

    tags=["Payroll"]
)

app.include_router(

    payroll_hr_router,

    prefix="/hr",

    tags=["HR Payroll"]
)

app.include_router(

    onboarding_user_router,

    prefix="/onboarding",

    tags=["Onboarding"]
)

app.include_router(

    onboarding_hr_router,

    prefix="/hr/onboardings",

    tags=["HR Onboarding"]
)

app.include_router(

    exit_user_router,

    prefix="/exit",

    tags=["Exit"]
)

app.include_router(

    exit_hr_router,

    prefix="/hr/exits",

    tags=["HR Exit"]
)

app.include_router(

    holidays_user_router,

    prefix="/holidays",

    tags=["Holidays"]
)

app.include_router(

    holidays_hr_router,

    prefix="/hr/holidays",

    tags=["HR Holidays"]
)

# ================= DEPARTMENTS =================
app.include_router(
    departments_user_router,
    prefix="/departments",
    tags=["Departments"],
)

app.include_router(
    departments_hr_router,
    prefix="/hr/departments",
    tags=["HR Departments"],
)

# ================= MANAGER (leave + correction approvals) =================
app.include_router(
    leave_manager_router,
    prefix="/manager",
    tags=["Manager"],
)

# Manager-scoped team + tasks (direct-report based, no team-lead required).
app.include_router(
    manager_tasks_router,
    prefix="/manager",
    tags=["Manager"],
)

# Company-wide documents (policies, handbooks). Any authed user can list;
# HR creates/updates/deletes.
app.include_router(
    company_docs_user_router,
    prefix="/company-docs",
    tags=["Company Docs"],
)
app.include_router(
    company_docs_hr_router,
    prefix="/hr/company-docs",
    tags=["HR Company Docs"],
)

app.include_router(
    corrections_manager_router,
    prefix="/manager/correction-requests",
    tags=["Manager"],
)

# ================= HR AUDIT LOGS =================
app.include_router(
    audit_router,
    prefix="/hr/audit-logs",
    tags=["HR Audit"],
)

# ================= NOTIFICATIONS (in-app feed) =================
app.include_router(
    notifications_router,
    prefix="/notifications",
    tags=["Notifications"],
)

# ================= DASHBOARDS =================
app.include_router(
    dashboard_router,
    prefix="/dashboard",
    tags=["Dashboard"],
)

# ================= PROJECTS =================
app.include_router(
    projects_user_router,
    prefix="/projects",
    tags=["Projects"],
)
app.include_router(
    projects_hr_router,
    prefix="/hr/projects",
    tags=["HR Projects"],
)

# ================= TO-DO =================
app.include_router(
    todos_router,
    prefix="/todos",
    tags=["Todos"],
)

# ================= DOCUMENTS =================
app.include_router(
    documents_user_router,
    prefix="/me/documents",
    tags=["Documents"],
)
app.include_router(
    documents_hr_router,
    prefix="/hr/users",
    tags=["HR Documents"],
)

# ================= REIMBURSEMENTS =================
app.include_router(
    reimb_user_router,
    prefix="/expenses/reimbursements",
    tags=["Reimbursements"],
)
app.include_router(
    reimb_manager_router,
    prefix="/manager/reimbursements",
    tags=["Manager"],
)
app.include_router(
    reimb_hr_router,
    prefix="/hr/reimbursements",
    tags=["HR Reimbursements"],
)

# ================= TIMESHEETS =================
app.include_router(
    timesheets_user_router,
    prefix="/timesheets",
    tags=["Timesheets"],
)
app.include_router(
    timesheets_manager_router,
    prefix="/manager/timesheets",
    tags=["Manager"],
)
app.include_router(
    timesheets_hr_router,
    prefix="/hr/timesheets",
    tags=["HR Timesheets"],
)

# ================= REPORTS =================
app.include_router(
    reports_router,
    prefix="/hr/reports",
    tags=["HR Reports"],
)
app.include_router(
    reports_manager_router,
    prefix="/manager/reports",
    tags=["Manager"],
)

# ================= EXCEL EXPORTS =================
app.include_router(
    exports_router,
    prefix="/hr/export",
    tags=["HR Exports"],
)

# ================= RECRUITMENT / ATS =================
app.include_router(
    openings_hr_router,
    prefix="/hr/job-openings",
    tags=["HR Recruitment"],
)
app.include_router(
    candidates_hr_router,
    prefix="/hr/candidates",
    tags=["HR Recruitment"],
)
app.include_router(
    interviews_hr_router,
    prefix="/hr/interviews",
    tags=["HR Recruitment"],
)
app.include_router(
    interviews_my_router,
    prefix="/interviews/mine",
    tags=["Recruitment"],
)
app.include_router(
    offers_hr_router,
    prefix="/hr/offers",
    tags=["HR Recruitment"],
)

# ================= PERFORMANCE =================
app.include_router(
    goals_router,
    prefix="/goals",
    tags=["Performance"],
)
app.include_router(
    goals_mgr_router,
    prefix="/manager/goals",
    tags=["Manager"],
)
app.include_router(
    goals_hr_router,
    prefix="/hr/goals",
    tags=["HR Performance"],
)
app.include_router(
    reviews_router,
    prefix="/reviews",
    tags=["Performance"],
)
app.include_router(
    reviews_mgr_router,
    prefix="/manager/reviews",
    tags=["Manager"],
)
app.include_router(
    reviews_hr_router,
    prefix="/hr/reviews",
    tags=["HR Performance"],
)
app.include_router(
    feedback_router,
    prefix="/feedback",
    tags=["Performance"],
)
app.include_router(
    feedback_hr_router,
    prefix="/hr/feedback",
    tags=["HR Performance"],
)

# ================= UPLOADS =================
app.include_router(
    uploads_router,
    prefix="/uploads",
    tags=["Uploads"],
)

# ================= USER DIRECTORY =================
# Lightweight, non-HR endpoints (e.g. directory for @-mentions). Mounted
# at the root since paths inside the router are absolute (/users/directory).
app.include_router(
    user_router,
    tags=["User"],
)

# Serve uploaded files at /static/uploads/<rel-path>
Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
app.mount(
    "/static/uploads",
    StaticFiles(directory=UPLOAD_DIR),
    name="uploads",
)

# ================= PUBLIC (NO-AUTH) =================
app.include_router(
    careers_router,
    prefix="/careers",
    tags=["Public Careers"],
)
app.include_router(
    offers_public_router,
    prefix="/public/offers",
    tags=["Public Offers"],
)

# ================= SSE REAL-TIME =================
app.include_router(
    sse_router,
    prefix="/sse",
    tags=["Realtime"],
)