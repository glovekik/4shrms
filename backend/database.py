from motor.motor_asyncio import (
    AsyncIOMotorClient
)

# Connection string + DB name come from config.py, which loads backend/.env
# on import — so importing them here guarantees the .env values are used
# (not whatever os.getenv sees before dotenv runs). Same code runs against a
# local mongod in dev and the self-hosted Mongo in production purely via .env.
from config import MONGO_URL, MONGO_DB_NAME

# Fail fast on a missing MONGO_URL rather than letting motor silently default
# to mongodb://localhost:27017 — inside a container that "localhost" is the
# container itself, which surfaces as a baffling "connection refused" only
# once the first query runs. A clear error at startup points straight at the
# real cause: MONGO_URL wasn't provided (check .env / --env-file).
if not MONGO_URL:
    raise RuntimeError(
        "MONGO_URL is not set. Define it in backend/.env (loaded by "
        "config.py) or pass it to the container (--env-file .env / -e "
        "MONGO_URL=...). Refusing to fall back to localhost."
    )

client = AsyncIOMotorClient(
    MONGO_URL
)

db = client[MONGO_DB_NAME]


# ================= CREATE INDEXES =================
async def create_indexes():

    # ONE USER = ONE ATTENDANCE PER DATE
    await db.attendance.create_index(
        [("userId", 1), ("date", 1)],
        unique=True,
    )

    # UNIQUE EMAIL ON USERS
    await db.users.create_index(
        "email",
        unique=True,
    )

    # UNIQUE EMPLOYEE CODE (sparse — only enforces when set)
    await db.users.create_index(
        "employeeCode",
        unique=True,
        sparse=True,
    )

    # TASKS: lookup by assignee + status, and by team
    await db.tasks.create_index(
        [("assigneeId", 1), ("status", 1)],
    )

    await db.tasks.create_index(
        [("teamId", 1)],
    )

    # TEAMS: lookup by team lead
    await db.teams.create_index(
        [("teamLeadId", 1)],
    )

    # COMMENTS: thread per task, oldest first
    await db.comments.create_index(
        [("taskId", 1), ("createdAt", 1)],
    )

    # CHAT MESSAGES: latest-first per channel for paginated loads
    await db.chat_messages.create_index(
        [
            ("channelType", 1),
            ("channelId", 1),
            ("createdAt", -1),
        ],
    )

    # CORRECTION REQUESTS: per user (mine view) and HR pending-list
    await db.correction_requests.create_index(
        [("userId", 1), ("createdAt", -1)],
    )
    await db.correction_requests.create_index(
        [("status", 1), ("createdAt", -1)],
    )

    # LEAVE TYPES: unique code
    await db.leave_types.create_index(
        "code",
        unique=True,
    )

    # LEAVE BALANCES: one row per (user, type, year)
    await db.leave_balances.create_index(
        [
            ("userId", 1),
            ("leaveTypeCode", 1),
            ("year", 1),
        ],
        unique=True,
    )

    # LEAVE REQUESTS: per user (mine), HR pending-list, overlap check
    await db.leave_requests.create_index(
        [("userId", 1), ("createdAt", -1)],
    )
    await db.leave_requests.create_index(
        [("status", 1), ("createdAt", -1)],
    )
    await db.leave_requests.create_index(
        [("userId", 1), ("fromDate", 1), ("toDate", 1)],
    )

    # ASSETS: unique code, common filters
    await db.assets.create_index(
        "code",
        unique=True,
    )
    await db.assets.create_index(
        [("status", 1)],
    )
    await db.assets.create_index(
        [("assignedToUserId", 1)],
    )

    # ASSET REPORTS: HR list and per-asset history
    await db.asset_reports.create_index(
        [("status", 1), ("createdAt", -1)],
    )
    await db.asset_reports.create_index(
        [("assetId", 1), ("createdAt", -1)],
    )

    # EXPENSES: by date and category
    await db.expenses.create_index(
        [("date", -1)],
    )
    await db.expenses.create_index(
        [("category", 1), ("date", -1)],
    )

    # SALARY STRUCTURES: lookup current per user, plus history sort
    await db.salary_structures.create_index(
        [("userId", 1), ("effectiveTo", 1)],
    )
    await db.salary_structures.create_index(
        [("userId", 1), ("effectiveFrom", -1)],
    )

    # PAYROLL RUNS: one per (year, month)
    await db.payroll_runs.create_index(
        [("year", 1), ("month", 1)],
        unique=True,
    )

    # PAYSLIPS: one per (user, year, month) and per-run lookups
    await db.payslips.create_index(
        [("userId", 1), ("year", 1), ("month", 1)],
        unique=True,
    )
    await db.payslips.create_index(
        [("payrollRunId", 1)],
    )

    # ONBOARDINGS: one per user
    await db.onboardings.create_index(
        "userId",
        unique=True,
    )
    await db.onboardings.create_index(
        [("status", 1), ("startedAt", -1)],
    )

    # EXITS: lookup by user + status
    await db.exits.create_index(
        [("userId", 1), ("createdAt", -1)],
    )
    await db.exits.create_index(
        [("status", 1), ("createdAt", -1)],
    )

    # HOLIDAYS: unique date
    await db.holidays.create_index(
        "date",
        unique=True,
    )

    # PUSH TOKENS: unique token, plus per-user lookup
    await db.push_tokens.create_index(
        "token",
        unique=True,
    )
    await db.push_tokens.create_index(
        "userId",
    )

    # PASSWORD RESET TOKENS: TTL on expiresAt + unique token
    await db.password_reset_tokens.create_index(
        "token",
        unique=True,
    )
    await db.password_reset_tokens.create_index(
        "expiresAt",
        expireAfterSeconds=0,
    )

    # REFRESH TOKENS: long-lived, revocable session tokens. Unique token,
    # TTL cleanup on expiresAt (Mongo purges expired sessions), and per-user
    # lookup for "revoke all my sessions".
    await db.refresh_tokens.create_index(
        "token",
        unique=True,
    )
    await db.refresh_tokens.create_index(
        "expiresAt",
        expireAfterSeconds=0,
    )
    await db.refresh_tokens.create_index(
        "userId",
    )

    # DEPARTMENTS: unique name (case-sensitive)
    await db.departments.create_index(
        "name",
        unique=True,
    )

    # USERS: lookup by reporting manager + department (manager dashboards,
    # department reports)
    await db.users.create_index([("reportingManagerId", 1)])
    await db.users.create_index([("departmentId", 1)])

    # AUDIT LOGS: HR viewer sorts newest-first, filters by entity + actor
    await db.audit_logs.create_index([("at", -1)])
    await db.audit_logs.create_index(
        [("entityType", 1), ("entityId", 1), ("at", -1)]
    )
    await db.audit_logs.create_index([("actorId", 1), ("at", -1)])
    # TTL: rows auto-delete 0 seconds after `expiresAt` (utils/audit.py
    # stamps it as now + 90 days at insert). Mongo's background thread
    # does the cleanup; no application code or cron involved.
    await db.audit_logs.create_index(
        "expiresAt", expireAfterSeconds=0,
    )

    # NOTIFICATIONS: per-user feed sorted newest-first; unread filter is common
    await db.notifications.create_index(
        [("userId", 1), ("createdAt", -1)]
    )
    await db.notifications.create_index(
        [("userId", 1), ("read", 1), ("createdAt", -1)]
    )
    # TTL: rows auto-delete after `expiresAt` (utils/notify.py stamps
    # it as now + 60 days at insert). Older legacy rows lacking the
    # field stay forever — they'll naturally age out as users mark
    # them read or as fresh writes overwrite them.
    await db.notifications.create_index(
        "expiresAt", expireAfterSeconds=0,
    )

    # PROJECTS: unique code, listing by department/status
    await db.projects.create_index("code", unique=True)
    await db.projects.create_index([("departmentId", 1)])
    await db.projects.create_index([("status", 1)])

    # TODOS: per-user, newest-first
    await db.todos.create_index(
        [("userId", 1), ("status", 1), ("createdAt", -1)]
    )

    # DOCUMENTS: per-user, by category
    await db.documents.create_index(
        [("userId", 1), ("category", 1), ("uploadedAt", -1)]
    )

    # REIMBURSEMENTS: per-user history + status queues
    await db.reimbursement_requests.create_index(
        [("userId", 1), ("createdAt", -1)]
    )
    await db.reimbursement_requests.create_index(
        [("status", 1), ("createdAt", -1)]
    )

    # TIMESHEETS: one row per (user, weekStart)
    await db.timesheets.create_index(
        [("userId", 1), ("weekStart", 1)],
        unique=True,
    )
    await db.timesheets.create_index(
        [("status", 1), ("weekStart", -1)]
    )

    # OTP CODES: TTL cleanup + lookup by (user, purpose)
    await db.otp_codes.create_index(
        [("userId", 1), ("purpose", 1)]
    )
    await db.otp_codes.create_index(
        "expiresAt",
        expireAfterSeconds=0,
    )

    # ===== RECRUITMENT =====
    # Job openings: filter by status, newest first
    await db.job_openings.create_index(
        [("status", 1), ("createdAt", -1)]
    )

    # Candidates: filter by stage + opening; dedup email per opening
    await db.candidates.create_index(
        [("stage", 1), ("createdAt", -1)]
    )
    await db.candidates.create_index(
        [("jobOpeningId", 1), ("email", 1)]
    )

    # Interviews: by candidate (timeline) and by interviewer (their queue)
    await db.interviews.create_index(
        [("candidateId", 1), ("scheduledAt", -1)]
    )
    await db.interviews.create_index(
        [("interviewerIds", 1), ("scheduledAt", -1)]
    )

    # Offers: by candidate + status
    await db.offers.create_index(
        [("candidateId", 1), ("createdAt", -1)]
    )
    await db.offers.create_index(
        [("status", 1), ("createdAt", -1)]
    )

    # ===== PERFORMANCE =====
    # Goals: per-user with status filter
    await db.goals.create_index(
        [("userId", 1), ("status", 1), ("createdAt", -1)]
    )

    # Reviews: per-employee timeline and HR status queue
    await db.reviews.create_index(
        [("employeeId", 1), ("createdAt", -1)]
    )
    await db.reviews.create_index(
        [("status", 1), ("createdAt", -1)]
    )

    # Feedback: per-recipient feed and per-sender review
    await db.feedback.create_index(
        [("toUserId", 1), ("createdAt", -1)]
    )
    await db.feedback.create_index(
        [("fromUserId", 1), ("createdAt", -1)]
    )

    # OFFERS: lookup by publicToken (sparse — only offers that have been
    # sent get a token). Unique so two offers can't collide.
    await db.offers.create_index(
        "publicToken",
        unique=True,
        sparse=True,
    )